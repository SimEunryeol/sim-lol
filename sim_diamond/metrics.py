"""참가자 1명 · 1경기 단위 지표 계산 → participant_metrics / deaths_detail.

파싱 테이블(matches/participants/frames/events)만 보고 계산하므로 언제든 재계산할 수 있다.
좌표에 대한 주의:
  - 타임라인의 WARD_PLACED / WARD_KILL 이벤트에는 좌표가 없다. 그래서 와드 위치는
    "설치자의 그 시각 위치"로 근사한다(프레임 사이 선형보간 + 킬 이벤트의 정확 좌표 보정).
  - 오브젝트 참여 판정에 쓰는 본인 위치도 같은 방식의 근사값이다.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from typing import Any, Iterable, Sequence

from . import geo
from .db import METRIC_COLUMNS, now_iso, upsert_many
from .ddragon import CONTROL_WARD_ID, Static

WARD_LOOKBACK_MS = 60_000      # 데스 직전 이 시간 안에 설치된 아군 와드만 본다
WARD_RADIUS = 1500.0
# 오브젝트 참여 판정. 2500 은 너무 넓어 "근처에 있었다"가 "참여했다"로 뭉개진다.
OBJECTIVE_RADII = (800.0, 1200.0, 2500.0)
OBJECTIVE_RADIUS = 1200.0          # 리포트 기본값
OBJECTIVE_WINDOW_MS = 30_000       # 처치 시각 ±30초 안의 위치 중 가장 가까운 값을 쓴다
# 첫 풀캠프 완료 근사 기준(정글 몬스터 처치 수).
# 6캠프 전체는 두꺼비1 + 블루1 + 늑대3 + 칼날부리6 + 레드1 + 돌거북7 ≈ 20마리다.
# 12 로 두면 실측상 2분에 걸리는데(= 3~4캠프) 풀클리어는 3분 15초 이후다.
FULL_CLEAR_JUNGLE_CS = 20
FIRST_GANK_AFTER_MS = 180_000
LANE_ZONES = {"TOP", "MID", "BOT"}   # 첫 갱으로 인정하는 구역
BACK_CLUSTER_GAP_MS = 20_000   # 이 간격보다 벌어지면 다른 상점 방문으로 본다
# 상점 방문 직전(= 지난 방문 이후)에 죽은 적이 있으면 그 방문은 부활 귀환으로 본다.
# 부활 시간은 레벨·게임시간에 따라 10~50초로 크게 변해서 고정 시간창은 오분류가 많다.
DEATH_BUCKETS = ((0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 10**6))
DEATH_BUCKET_LABELS = ["0-5", "5-10", "10-15", "15-20", "20-25", "25+"]
FAKE_WARDS = {"UNDEFINED", "TEEMO_MUSHROOM"}

METRIC_NAMES = [name for name, _ in METRIC_COLUMNS]


class Track:
    """시각(ms) → 좌표. 프레임 + 정확한 이벤트 좌표를 섞어 선형보간한다."""

    def __init__(self, samples: Sequence[tuple[int, float, float]]):
        uniq: dict[int, tuple[float, float]] = {}
        for ts, x, y in samples:
            if x is None or y is None:
                continue
            uniq[int(ts)] = (float(x), float(y))
        self.ts = sorted(uniq)
        self.pts = [uniq[t] for t in self.ts]

    def at(self, ts: int) -> tuple[float, float] | None:
        if not self.ts:
            return None
        i = bisect_left(self.ts, ts)
        if i == 0:
            return self.pts[0]
        if i >= len(self.ts):
            return self.pts[-1]
        t0, t1 = self.ts[i - 1], self.ts[i]
        (x0, y0), (x1, y1) = self.pts[i - 1], self.pts[i]
        if t1 == t0:
            return x0, y0
        w = (ts - t0) / (t1 - t0)
        return x0 + (x1 - x0) * w, y0 + (y1 - y0) * w


class MatchContext:
    """한 경기에 필요한 모든 파싱 데이터를 메모리에 올린다."""

    def __init__(self, conn: sqlite3.Connection, match_id: str):
        self.match_id = match_id
        self.match = conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        self.parts = {
            r["puuid"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM participants WHERE match_id = ?", (match_id,)
            )
        }
        self.frames: dict[str, dict[int, dict]] = defaultdict(dict)
        for r in conn.execute("SELECT * FROM frames WHERE match_id = ?", (match_id,)):
            self.frames[r["puuid"]][r["minute"]] = dict(r)
        self.events = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE match_id = ? ORDER BY ts_ms", (match_id,)
        )]
        self.by_type: dict[str, list[dict]] = defaultdict(list)
        for e in self.events:
            e["extra_d"] = json.loads(e["extra"]) if e["extra"] else {}
            self.by_type[e["type"]].append(e)

        # 위치 트랙 = 분 단위 프레임 + 킬 이벤트의 정확 좌표
        self.tracks: dict[str, Track] = {}
        extra_pts: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        for e in self.by_type.get("CHAMPION_KILL", []):
            if e["x"] is None:
                continue
            for puuid in (e["killer_puuid"], e["victim_puuid"]):
                if puuid:
                    extra_pts[puuid].append((e["ts_ms"], e["x"], e["y"]))
        for puuid, fr in self.frames.items():
            samples = [(m * 60_000, f["x"], f["y"]) for m, f in fr.items()]
            samples += extra_pts.get(puuid, [])
            self.tracks[puuid] = Track(samples)

        self.duration_s = (self.match["duration_s"] if self.match else None) or 0

    # -------------------------------------------------------------------
    def team_of(self, puuid: str) -> int | None:
        p = self.parts.get(puuid)
        return p["team_id"] if p else None

    def position_at(self, puuid: str, ts: int) -> tuple[float, float] | None:
        track = self.tracks.get(puuid)
        return track.at(ts) if track else None

    def min_dist_in_window(self, puuid: str, ts: int, window_ms: int,
                           point: tuple[float, float]) -> float | None:
        """ts ± window_ms 안에서 그 사람이 point 에 가장 가까웠던 거리.

        위치는 분 단위 프레임을 보간한 근사값이라, 한 시점만 보면 오차가 크다.
        창 안의 실제 샘플 지점들과 창 양 끝을 함께 본다.
        """
        track = self.tracks.get(puuid)
        if not track or not track.ts:
            return None
        times = {ts, ts - window_ms, ts + window_ms}
        times |= {t for t in track.ts if ts - window_ms <= t <= ts + window_ms}
        best = None
        for t in sorted(times):
            pos = track.at(t)
            if pos is None:
                continue
            d = geo.dist(pos, point)
            best = d if best is None else min(best, d)
        return best

    def frame(self, puuid: str, minute: int) -> dict | None:
        return self.frames.get(puuid, {}).get(minute)

    def lane_opponent(self, puuid: str) -> dict | None:
        me = self.parts.get(puuid)
        if not me or not me["team_position"] or me["team_position"] in ("", "NONE", "Invalid"):
            return None
        for other in self.parts.values():
            if other["team_id"] != me["team_id"] and other["team_position"] == me["team_position"]:
                return other
        return None

    def jungler_of(self, team_id: int) -> dict | None:
        for p in self.parts.values():
            if p["team_id"] == team_id and p["team_position"] == "JUNGLE":
                return p
        return None


# ------------------------------------------------------------------ 계산 --
def _fdiff(a: dict | None, b: dict | None, key: str) -> int | None:
    if not a or not b or a.get(key) is None or b.get(key) is None:
        return None
    return int(a[key]) - int(b[key])


def _cs_at(ctx: MatchContext, puuid: str, minute: int) -> int | None:
    f = ctx.frame(puuid, minute)
    if not f:
        return None
    return (f["cs"] or 0) + (f["jungle_cs"] or 0)


def _interp_cross(points: list[tuple[int, float]], target: float) -> float | None:
    """(분, 값) 시계열에서 값이 target 을 처음 넘는 시각을 선형보간으로 추정."""
    prev = None
    for minute, val in points:
        if val is not None and val >= target:
            if prev is None or prev[1] is None or val == prev[1]:
                return float(minute)
            span = val - prev[1]
            w = (target - prev[1]) / span
            return prev[0] + (minute - prev[0]) * w
        prev = (minute, val)
    return None


def _death_bucket(minute: float) -> str:
    for (lo, hi), label in zip(DEATH_BUCKETS, DEATH_BUCKET_LABELS):
        if lo <= minute < hi:
            return label
    return DEATH_BUCKET_LABELS[-1]


def _deaths(ctx: MatchContext, puuid: str) -> tuple[list[dict], dict]:
    me = ctx.parts[puuid]
    team = me["team_id"]
    ally_wards = [
        e for e in ctx.by_type.get("WARD_PLACED", [])
        if e["killer_puuid"] and ctx.team_of(e["killer_puuid"]) == team
        and (e["ward_type"] or "") not in FAKE_WARDS
    ]
    rows: list[dict] = []
    zones: dict[str, int] = defaultdict(int)
    buckets: dict[str, int] = defaultdict(int)
    warded = unwarded = 0
    seq = 0
    for e in ctx.by_type.get("CHAMPION_KILL", []):
        if e["victim_puuid"] != puuid:
            continue
        seq += 1
        ts = e["ts_ms"] or 0
        minute = ts / 60_000
        zkey = geo.zone(e["x"], e["y"], team)
        zones[zkey] += 1
        buckets[_death_bucket(minute)] += 1

        is_warded: int | None = None
        if e["x"] is not None and ctx.tracks:
            is_warded = 0
            for w in ally_wards:
                wts = w["ts_ms"] or 0
                if not (ts - WARD_LOOKBACK_MS <= wts <= ts):
                    continue
                wpos = ctx.position_at(w["killer_puuid"], wts)
                if wpos and geo.dist(wpos, (e["x"], e["y"])) <= WARD_RADIUS:
                    is_warded = 1
                    break
        if is_warded == 1:
            warded += 1
        elif is_warded == 0:
            unwarded += 1
        rows.append(
            {
                "match_id": ctx.match_id, "puuid": puuid, "seq": seq, "ts_ms": ts,
                "minute": round(minute, 2), "x": e["x"], "y": e["y"],
                "zone": zkey, "warded": is_warded,
            }
        )
    summary = {
        "deaths_before_15": sum(1 for r in rows if r["minute"] < 15),
        "deaths_warded": warded,
        "deaths_unwarded": unwarded,
        "deaths_zone_json": json.dumps(dict(zones), ensure_ascii=False),
        "deaths_bucket_json": json.dumps(dict(buckets), ensure_ascii=False),
    }
    return rows, summary


def _objectives(ctx: MatchContext, puuid: str) -> dict:
    me = ctx.parts[puuid]
    team = me["team_id"]
    detail: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    total = 0
    hits = {r: 0 for r in OBJECTIVE_RADII}
    for e in ctx.by_type.get("ELITE_MONSTER_KILL", []):
        killer = e["killer_puuid"]
        killer_team = ctx.team_of(killer) if killer else e["extra_d"].get("killerTeamId")
        if killer_team != team:
            continue
        mtype = e["monster_type"] or "UNKNOWN"
        total += 1
        detail[mtype][0] += 1

        credited = killer == puuid or puuid in (e["extra_d"].get("assisting_puuids") or [])
        dist = None
        if not credited and e["x"] is not None:
            dist = ctx.min_dist_in_window(
                puuid, e["ts_ms"] or 0, OBJECTIVE_WINDOW_MS, (e["x"], e["y"]))
        for r in OBJECTIVE_RADII:
            if credited or (dist is not None and dist <= r):
                hits[r] += 1
        if credited or (dist is not None and dist <= OBJECTIVE_RADIUS):
            detail[mtype][1] += 1
    out = {
        "obj_team_total": total,
        "obj_participated": hits[OBJECTIVE_RADIUS],
        "obj_participation": (hits[OBJECTIVE_RADIUS] / total) if total else None,
        "obj_detail_json": json.dumps({k: v for k, v in detail.items()}, ensure_ascii=False),
    }
    for r in OBJECTIVE_RADII:
        out[f"obj_participation_{int(r)}"] = (hits[r] / total) if total else None
    return out


def _jungle(ctx: MatchContext, puuid: str) -> dict:
    me = ctx.parts[puuid]
    out: dict[str, Any] = {
        "is_jungle": int(me["team_position"] == "JUNGLE"),
        "first_full_clear_min": None, "first_gank_min": None,
        "first_counter_jungled_min": None,
        "jg_gold_diff_5": None, "jg_gold_diff_10": None,
        "jg_level_diff_5": None, "jg_level_diff_10": None,
        "enemy_jungle_minutes": None,
    }
    if me["team_position"] != "JUNGLE":
        return out

    fr = ctx.frames.get(puuid, {})
    series = [(m, fr[m]["jungle_cs"]) for m in sorted(fr)]
    out["first_full_clear_min"] = _interp_cross(series, FULL_CLEAR_JUNGLE_CS)

    # 첫 갱: 3분 이후, 라인 구역(탑/미드/봇)에서 내가 딴 첫 킬/어시.
    # 정글(내/적)이나 강·둥지에서 난 교전, 그리고 내 데스는 포함하지 않는다.
    for e in ctx.by_type.get("CHAMPION_KILL", []):
        if (e["ts_ms"] or 0) < FIRST_GANK_AFTER_MS:
            continue
        if geo.zone_raw(e["x"], e["y"]) not in LANE_ZONES:
            continue
        credited = (
            e["killer_puuid"] == puuid
            or puuid in (e["extra_d"].get("assisting_puuids") or [])
        )
        if credited:
            out["first_gank_min"] = round((e["ts_ms"] or 0) / 60_000, 2)
            break

    # 첫 카정 피해: 내 정글에서 내가 죽은 첫 시각. 초반 인베이드가 핵심이라 시간 하한은 두지 않는다.
    for e in ctx.by_type.get("CHAMPION_KILL", []):
        if e["victim_puuid"] != puuid:
            continue
        if geo.zone(e["x"], e["y"], me["team_id"]) == "OWN_JUNGLE":
            out["first_counter_jungled_min"] = round((e["ts_ms"] or 0) / 60_000, 2)
            break

    enemy_team = 200 if me["team_id"] == 100 else 100
    opp = ctx.jungler_of(enemy_team)
    if opp:
        for minute in (5, 10):
            a, b = ctx.frame(puuid, minute), ctx.frame(opp["puuid"], minute)
            out[f"jg_gold_diff_{minute}"] = _fdiff(a, b, "gold")
            lvl = _fdiff(a, b, "level")
            out[f"jg_level_diff_{minute}"] = float(lvl) if lvl is not None else None

    out["enemy_jungle_minutes"] = sum(
        1 for m, f in fr.items() if geo.zone(f["x"], f["y"], me["team_id"]) == "ENEMY_JUNGLE"
    )
    return out


def _purchases(ctx: MatchContext, puuid: str) -> list[dict]:
    buys = [e for e in ctx.by_type.get("ITEM_PURCHASED", []) if e["killer_puuid"] == puuid]
    undos = [e for e in ctx.by_type.get("ITEM_UNDO", []) if e["killer_puuid"] == puuid]
    for u in undos:
        before = u["extra_d"].get("beforeId")
        if not before:
            continue
        for i in range(len(buys) - 1, -1, -1):
            if buys[i]["item_id"] == before and (buys[i]["ts_ms"] or 0) <= (u["ts_ms"] or 0):
                buys.pop(i)
                break
    return sorted(buys, key=lambda e: e["ts_ms"] or 0)


def _items_and_backs(ctx: MatchContext, puuid: str, static: Static, death_ts: list[int]) -> dict:
    buys = _purchases(ctx, puuid)
    out: dict[str, Any] = {
        "first_core_item_min": None, "first_core_item_id": None,
        "back_count": 0, "voluntary_back_count": 0, "back_times_json": "[]",
        "first_control_ward_min": None,
        "control_wards_bought_events": sum(1 for b in buys if b["item_id"] == CONTROL_WARD_ID),
    }
    for b in buys:
        if static.is_core_item(b["item_id"]):
            out["first_core_item_min"] = round((b["ts_ms"] or 0) / 60_000, 2)
            out["first_core_item_id"] = b["item_id"]
            break
    for b in buys:
        if b["item_id"] == CONTROL_WARD_ID:
            out["first_control_ward_min"] = round((b["ts_ms"] or 0) / 60_000, 2)
            break

    clusters: list[int] = []
    last = None
    for b in buys:
        ts = b["ts_ms"] or 0
        if last is None or ts - last > BACK_CLUSTER_GAP_MS:
            clusters.append(ts)
        last = ts
    visits = [ts for ts in clusters if ts > 30_000]  # 시작 아이템 구매 제외
    voluntary, prev = [], 0
    for ts in visits:
        died_since = any(prev < d <= ts for d in death_ts)
        if not died_since:
            voluntary.append(ts)
        prev = ts
    out["back_count"] = len(visits)
    out["voluntary_back_count"] = len(voluntary)
    out["back_times_json"] = json.dumps([round(ts / 60_000, 2) for ts in voluntary])
    return out


def compute_participant(ctx: MatchContext, puuid: str, static: Static, player: dict | None) -> tuple[dict, list[dict]]:
    p = ctx.parts[puuid]
    dur_s = ctx.duration_s or 0
    dur_min = dur_s / 60 if dur_s else None
    opp = ctx.lane_opponent(puuid)
    opp_puuid = opp["puuid"] if opp else None

    deaths_rows, death_summary = _deaths(ctx, puuid)
    death_ts = [r["ts_ms"] for r in deaths_rows]

    cs10, cs15 = _cs_at(ctx, puuid, 10), _cs_at(ctx, puuid, 15)
    ocs10 = _cs_at(ctx, opp_puuid, 10) if opp_puuid else None
    ocs15 = _cs_at(ctx, opp_puuid, 15) if opp_puuid else None
    f15 = ctx.frame(puuid, 15)

    row: dict[str, Any] = {
        "match_id": ctx.match_id,
        "puuid": puuid,
        "game_start": ctx.match["game_start"] if ctx.match else None,
        "patch": ctx.match["patch"] if ctx.match else None,
        "team_position": p["team_position"],
        "champion_id": p["champion_id"],
        "champion_name": p["champion_name"],
        "tier_at_collect": (player or {}).get("tier"),
        "is_me": int((player or {}).get("is_me") or 0),
        "is_bench": int((player or {}).get("is_bench") or 0),
        "duration_s": dur_s or None,
        "win": p["win"],
        "kills": p["kills"], "deaths": p["deaths"], "assists": p["assists"],
        "kda": round(((p["kills"] or 0) + (p["assists"] or 0)) / max(1, p["deaths"] or 0), 3),
        "cs": p["cs"],
        "cs_per_min": round((p["cs"] or 0) / dur_min, 2) if dur_min else None,
        "cs10": cs10, "cs15": cs15,
        "gold15": f15["gold"] if f15 else None,
        "damage_to_champs": p["damage_to_champs"],
        "opp_puuid": opp_puuid,
        "cs10_diff": (cs10 - ocs10) if (cs10 is not None and ocs10 is not None) else None,
        "cs15_diff": (cs15 - ocs15) if (cs15 is not None and ocs15 is not None) else None,
        "gold10_diff": _fdiff(ctx.frame(puuid, 10), ctx.frame(opp_puuid, 10) if opp_puuid else None, "gold"),
        "gold15_diff": _fdiff(f15, ctx.frame(opp_puuid, 15) if opp_puuid else None, "gold"),
        "level10_diff": _fdiff(ctx.frame(puuid, 10), ctx.frame(opp_puuid, 10) if opp_puuid else None, "level"),
        "level15_diff": _fdiff(f15, ctx.frame(opp_puuid, 15) if opp_puuid else None, "level"),
        "vision_score": p["vision_score"],
        "vision_per_min": round((p["vision_score"] or 0) / dur_min, 3) if dur_min else None,
        "control_wards_bought": p["control_wards_bought"],
        "wards_placed": p["wards_placed"],
        "wards_killed": p["wards_killed"],
        "computed_at": now_iso(),
    }
    row.update(death_summary)
    row.update(_objectives(ctx, puuid))
    row.update(_jungle(ctx, puuid))
    items = _items_and_backs(ctx, puuid, static, death_ts)
    row["first_control_ward_min"] = items.pop("first_control_ward_min")
    items.pop("control_wards_bought_events", None)
    row.update(items)

    return {k: row.get(k) for k in METRIC_NAMES}, deaths_rows


# --------------------------------------------------------------- 진입점 --
def compute_match(
    conn: sqlite3.Connection, match_id: str, static: Static, players: dict[str, dict],
    all_participants: bool = False,
) -> int:
    ctx = MatchContext(conn, match_id)
    if not ctx.match or not ctx.parts:
        return 0
    targets = [
        puuid for puuid in ctx.parts
        if all_participants or puuid in players
    ]
    if not targets:
        return 0
    rows, death_rows = [], []
    for puuid in targets:
        row, drows = compute_participant(ctx, puuid, static, players.get(puuid))
        rows.append(row)
        death_rows.extend(drows)
        conn.execute(
            "DELETE FROM deaths_detail WHERE match_id = ? AND puuid = ?", (match_id, puuid)
        )
    upsert_many(conn, "participant_metrics", rows, ["match_id", "puuid"])
    upsert_many(conn, "deaths_detail", death_rows, ["match_id", "puuid", "seq"])
    return len(rows)


def load_players(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["puuid"]: dict(r) for r in conn.execute("SELECT * FROM players")}


def compute_all(
    conn: sqlite3.Connection,
    match_ids: Iterable[str] | None = None,
    all_participants: bool = False,
    recompute: bool = False,
    verbose: bool = True,
) -> dict[str, int]:
    static = Static(conn)
    players = load_players(conn)
    if match_ids is None:
        if recompute:
            sql = "SELECT match_id FROM matches"
        else:
            # 매치 단위로 "지표 있음"을 판정하면, 한 매치에 추적 대상(내 계정/벤치)이
            # 2명 이상일 때 두 번째부터 영영 계산되지 않는다. 인원수로 비교한다.
            sql = (
                "SELECT m.match_id FROM matches m WHERE "
                "(SELECT COUNT(*) FROM participants p JOIN players pl ON pl.puuid = p.puuid "
                " WHERE p.match_id = m.match_id) > "
                "(SELECT COUNT(*) FROM participant_metrics pm WHERE pm.match_id = m.match_id)"
            )
        match_ids = [r["match_id"] for r in conn.execute(sql)]
    match_ids = list(match_ids)
    total_rows = 0
    for i, mid in enumerate(match_ids, 1):
        total_rows += compute_match(conn, mid, static, players, all_participants)
        if verbose and (i % 25 == 0 or i == len(match_ids)):
            print(f"  metrics {i}/{len(match_ids)} (rows={total_rows})", flush=True)
        if i % 50 == 0:
            conn.commit()
    conn.commit()
    if verbose and not static.available:
        print("  ! Data Dragon 캐시가 없어 first_core_item_min 은 비어 있습니다.")
    return {"matches": len(match_ids), "rows": total_rows}
