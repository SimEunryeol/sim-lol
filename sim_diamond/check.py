"""수집된 데이터가 실제로 말이 되는지 점검한다.

  python -m sim_diamond.check            # 요약 + 이상 징후
  python -m sim_diamond.check --match KR_1234  # 한 경기 상세

지표가 통째로 비어 있거나(파싱 실패) 값이 물리적으로 불가능하면 여기서 잡힌다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime

from . import config, geo, metrics
from .db import counts, session
from .ddragon import Static

# (컬럼, 라벨, 있어야 정상인지, 정상 범위)
WATCH = [
    ("cs10", "10분 CS", True, (0, 150)),
    ("cs15", "15분 CS", True, (0, 220)),
    ("gold15", "15분 골드", True, (1000, 20000)),
    ("cs15_diff", "15분 CS 차이", True, (-150, 150)),
    ("gold15_diff", "15분 골드 차이", True, (-12000, 12000)),
    ("level15_diff", "15분 레벨 차이", True, (-8, 8)),
    ("vision_per_min", "시야점수/분", True, (0, 6)),
    ("control_wards_bought", "컨트롤 와드 구매", True, (0, 30)),
    ("first_control_ward_min", "첫 컨트롤 와드(분)", False, (0, 60)),
    ("obj_participation", "오브젝트 참여율", True, (0, 1)),
    ("deaths_warded", "와드 있던 데스", False, (0, 40)),
    ("deaths_unwarded", "와드 없던 데스", False, (0, 40)),
    ("first_core_item_min", "첫 코어템(분)", True, (0, 60)),
    ("back_count", "상점 방문(귀환)", True, (0, 30)),
    ("first_full_clear_min", "첫 풀캠프(분)", False, (1, 15)),
    ("first_gank_min", "첫 갱(분)", False, (3, 60)),
    ("first_counter_jungled_min", "첫 카정 피해(분)", False, (0, 60)),
    ("enemy_jungle_minutes", "적정글 체류(분)", False, (0, 60)),
]

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def _num(rows: list, key: str) -> list[float]:
    return [r[key] for r in rows if r[key] is not None]


def _stats(vals: list[float]) -> str:
    if not vals:
        return "—"
    vals = sorted(vals)
    mid = vals[len(vals) // 2]
    return f"최소 {vals[0]:.6g} / 중앙 {mid:.6g} / 최대 {vals[-1]:.6g}"


def overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    print("[1] 전체 현황")
    c = counts(conn)
    print("    " + ", ".join(f"{k}={v}" for k, v in c.items()))
    if c["matches"] and c["participants"] != c["matches"] * 10:
        warn(f"participants({c['participants']}) 가 matches×10({c['matches'] * 10}) 이 아닙니다 — 파싱 누락 가능성")
    if c["matches"] and not c["frames"]:
        warn("frames 가 비었습니다 — 타임라인이 저장되지 않았습니다")

    rows = conn.execute("""
        SELECT m.*, COUNT(p.puuid) n_part FROM matches m
        LEFT JOIN participants p ON p.match_id = m.match_id GROUP BY m.match_id
    """).fetchall()
    if not rows:
        print("    수집된 매치가 없습니다.")
        return []
    starts = [r["game_start"] for r in rows if r["game_start"]]
    if starts:
        fmt = lambda ms: datetime.fromtimestamp(ms / 1000, config.KST).strftime("%Y-%m-%d %H:%M")
        print(f"    기간   : {fmt(min(starts))} ~ {fmt(max(starts))} KST")
    queues = sorted({r["queue_id"] for r in rows})
    patches = sorted({r["patch"] for r in rows if r["patch"]})
    durs = sorted(r["duration_s"] for r in rows if r["duration_s"])
    print(f"    큐     : {queues}" + ("  ← 420(솔랭) 외가 섞였습니다" if queues != [420] else ""))
    print(f"    패치   : {', '.join(patches)}")
    if durs:
        med = durs[len(durs) // 2]
        print(f"    게임시간: {durs[0] // 60}분 ~ {durs[-1] // 60}분 (중앙 {med // 60}분)")
        remakes = [r for r in rows if (r["duration_s"] or 0) < config.REMAKE_MAX_S]
        if remakes:
            print(f"    리메이크: {len(remakes)}판 (5분 미만) — 집계에서 제외됩니다: "
                  + ", ".join(f"{r['match_id']}({r['duration_s']}s)" for r in remakes[:3]))
        # 단위 오류라면 중앙값까지 이상해진다. 짧은 판 한둘은 리메이크지 버그가 아니다.
        if med > 7200 or med < 600:
            warn(f"게임시간 중앙값이 {med}s 입니다 — 초/밀리초 단위 오류 의심")
        if durs[-1] > 7200:
            warn(f"게임시간 최대값이 {durs[-1]}s({durs[-1] // 60}분) 입니다 — 확인 필요")
    if queues != [420] and queues:
        warn(f"솔랭(420) 외의 큐가 섞여 있습니다: {queues}")
    bad = [r["match_id"] for r in rows if r["n_part"] != 10]
    if bad:
        warn(f"참가자가 10명이 아닌 매치 {len(bad)}건: {bad[:5]}")
    return rows


def my_metrics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    me = conn.execute("SELECT * FROM players WHERE is_me = 1").fetchone()
    if not me:
        print("\n[2] 내 계정이 players 에 없습니다.")
        return []
    rows = conn.execute(
        "SELECT * FROM participant_metrics WHERE puuid = ? ORDER BY game_start", (me["puuid"],)
    ).fetchall()
    print(f"\n[2] 내 지표 ({me['game_name']}#{me['tag_line']}, {me['tier']} {me['rank']}) — {len(rows)}판")
    if not rows:
        warn("내 participant_metrics 가 비었습니다 — 지표 계산이 안 됐습니다")
        return []

    pos = {}
    for r in rows:
        pos[r["team_position"] or "(없음)"] = pos.get(r["team_position"] or "(없음)", 0) + 1
    print(f"    라인   : {pos}")
    if pos.get("(없음)"):
        warn(f"team_position 이 비어 있는 판 {pos['(없음)']}건 — 라인별 집계에서 빠집니다")
    wins = sum(r["win"] for r in rows)
    print(f"    승패   : {wins}승 {len(rows) - wins}패 ({wins / len(rows) * 100:.1f}%)")
    print(f"    KDA    : K {sum(r['kills'] for r in rows) / len(rows):.1f} / "
          f"D {sum(r['deaths'] for r in rows) / len(rows):.1f} / "
          f"A {sum(r['assists'] for r in rows) / len(rows):.1f}")

    print("\n[3] 지표별 채움 정도와 값 범위")
    print(f"    {'지표':<22} {'채움':>9}   값 범위")
    print("    " + "-" * 62)
    for col, label, required, (lo, hi) in WATCH:
        vals = _num(rows, col)
        cover = len(vals) / len(rows)
        flag = ""
        if required and not vals:
            flag = "  ← 전부 비었습니다 ★"
            warn(f"{label}({col}) 이 전부 비었습니다 — 계산이 실패했습니다")
        elif required and cover < 0.5:
            flag = "  ← 절반 넘게 비었습니다 ?"
            warn(f"{label}({col}) 채움률이 {cover:.0%} 뿐입니다")
        out = [v for v in vals if not (lo <= v <= hi)]
        if out:
            flag += f"  ← 범위 밖 {len(out)}건 {sorted(out)[:3]} ★"
            warn(f"{label}({col}) 에 범위({lo}~{hi}) 밖 값이 {len(out)}건 있습니다")
        print(f"    {label:<22} {len(vals):>4}/{len(rows):<4} {_stats(vals)}{flag}")
    return rows


def death_check(conn: sqlite3.Connection, me_rows: list) -> None:
    me = conn.execute("SELECT puuid FROM players WHERE is_me = 1").fetchone()
    if not me or not me_rows:
        return
    rows = conn.execute(
        "SELECT zone, warded, minute, x, y FROM deaths_detail WHERE puuid = ?", (me["puuid"],)
    ).fetchall()
    print(f"\n[4] 데스 상세 {len(rows)}건")
    if not rows:
        warn("deaths_detail 이 비었습니다")
        return
    declared = sum(r["deaths"] for r in me_rows)
    print(f"    타임라인 데스 {len(rows)}건 vs 매치 스탯 합계 {declared}건", end="")
    if declared and abs(len(rows) - declared) / declared > 0.05:
        print("  ← 5% 넘게 어긋납니다 ★")
        warn(f"데스 수가 어긋납니다: 타임라인 {len(rows)} vs 스탯 {declared}")
    else:
        print("  (일치)")

    zones: dict[str, int] = {}
    for r in rows:
        zones[r["zone"]] = zones.get(r["zone"], 0) + 1
    print("    구역   : " + ", ".join(
        f"{geo.zone_label(z)} {n}({n / len(rows):.0%})"
        for z, n in sorted(zones.items(), key=lambda kv: -kv[1])))
    if zones.get("UNKNOWN"):
        warn(f"좌표가 없는 데스 {zones['UNKNOWN']}건 — 구역 분류 불가")
    warded = sum(1 for r in rows if r["warded"] == 1)
    judged = sum(1 for r in rows if r["warded"] is not None)
    if judged:
        print(f"    와드   : 판정 {judged}건 중 와드 있던 데스 {warded}건 ({warded / judged:.0%}) _(추정)_")
        if warded == 0:
            warn("와드 있던 데스가 0건입니다 — 와드 위치 근사가 동작하지 않을 수 있습니다")


def jungle_calibration(conn: sqlite3.Connection) -> None:
    """정글 CS 곡선을 실제로 보여준다. '첫 풀캠프' 임계값이 맞는지 눈으로 확인하려는 것."""
    me = conn.execute("SELECT puuid FROM players WHERE is_me = 1").fetchone()
    if not me:
        return
    rows = conn.execute("""
        SELECT f.minute, f.jungle_cs FROM frames f
        JOIN participant_metrics pm ON pm.match_id = f.match_id AND pm.puuid = f.puuid
        WHERE f.puuid = ? AND pm.is_jungle = 1 AND f.minute BETWEEN 1 AND 6
    """, (me["puuid"],)).fetchall()
    if not rows:
        return
    by_min: dict[int, list[int]] = {}
    for r in rows:
        by_min.setdefault(r["minute"], []).append(r["jungle_cs"] or 0)
    print(f"\n[6] 정글 CS 곡선 (정글 판 실측, '첫 풀캠프' 임계값 {metrics.FULL_CLEAR_JUNGLE_CS} 검증용)")
    print("    분    " + "  ".join(f"{m:>4}" for m in sorted(by_min)))
    print("    중앙  " + "  ".join(
        f"{sorted(v)[len(v) // 2]:>4}" for _, v in sorted(by_min.items())))
    hit = next((m for m in sorted(by_min)
                if sorted(by_min[m])[len(by_min[m]) // 2] >= metrics.FULL_CLEAR_JUNGLE_CS), None)
    print(f"    → 중앙값이 {metrics.FULL_CLEAR_JUNGLE_CS} 를 넘는 시점: {hit}분")
    if hit is not None and hit <= 2:
        warn(f"정글 CS 가 {hit}분에 벌써 임계값 {metrics.FULL_CLEAR_JUNGLE_CS} 를 넘습니다 — "
             f"첫 풀캠프(6캠프)는 보통 3분 15초쯤이므로 임계값이 너무 낮습니다. "
             f"metrics.FULL_CLEAR_JUNGLE_CS 를 올려야 합니다.")


def event_types(conn: sqlite3.Connection) -> None:
    print("\n[7] 이벤트 종류 (지표가 의존하는 것들)")
    rows = conn.execute(
        "SELECT type, COUNT(*) n FROM events GROUP BY type ORDER BY n DESC"
    ).fetchall()
    seen = {r["type"]: r["n"] for r in rows}
    print("    " + ", ".join(f"{r['type']}={r['n']}" for r in rows))
    for need in ("CHAMPION_KILL", "WARD_PLACED", "ITEM_PURCHASED", "ELITE_MONSTER_KILL"):
        if not seen.get(need):
            warn(f"{need} 이벤트가 하나도 없습니다 — 관련 지표가 전부 빕니다")
    with_pos = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE type='CHAMPION_KILL' AND x IS NOT NULL"
    ).fetchone()["n"]
    total_kill = seen.get("CHAMPION_KILL", 0)
    if total_kill:
        print(f"    CHAMPION_KILL 중 좌표 있는 것: {with_pos}/{total_kill}")
        if with_pos < total_kill:
            warn(f"좌표 없는 킬 이벤트 {total_kill - with_pos}건")


def one_match(conn: sqlite3.Connection, match_id: str, static: Static) -> None:
    print(f"\n[상세] {match_id}")
    m = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if not m:
        print("    그런 매치가 없습니다.")
        return
    print(f"    패치 {m['patch']} · 큐 {m['queue_id']} · {m['duration_s'] // 60}분")
    for r in conn.execute(
        "SELECT * FROM participants WHERE match_id = ? ORDER BY team_id, team_position", (match_id,)
    ):
        print(f"    {r['team_id']} {r['team_position'] or '?':<8} "
              f"{static.champion(r['champion_id'], r['champion_name']):<10} "
              f"{r['kills']}/{r['deaths']}/{r['assists']}  CS {r['cs']:>3}  "
              f"골드 {r['gold']:>5}  시야 {r['vision_score']:>3}  {'승' if r['win'] else '패'}")
    row = conn.execute(
        "SELECT pm.* FROM participant_metrics pm JOIN players p ON p.puuid = pm.puuid "
        "WHERE pm.match_id = ? AND p.is_me = 1", (match_id,)
    ).fetchone()
    if row:
        print("\n    내 지표:")
        for k in row.keys():
            if k not in ("match_id", "puuid", "opp_puuid", "computed_at") and row[k] is not None:
                print(f"      {k:<26} = {row[k]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="수집 데이터 점검")
    ap.add_argument("--match", help="이 매치 하나를 자세히 본다")
    ap.add_argument("--sample", action="store_true", help="가장 최근 매치를 자세히 본다")
    args = ap.parse_args(argv)

    st = config.load_settings(require_key=False)
    with session(st.db_path) as conn:
        static = Static(conn)
        print("=" * 68)
        print(f"sim-diamond 데이터 점검  ·  {st.db_path}")
        print("=" * 68)
        rows = overview(conn)
        me_rows = my_metrics(conn)
        death_check(conn, me_rows)
        jungle_calibration(conn)
        event_types(conn)

        target = args.match
        if args.sample and rows:
            target = max(rows, key=lambda r: r["game_start"] or 0)["match_id"]
        if target:
            one_match(conn, target, static)

        print("\n" + "=" * 68)
        if WARNINGS:
            print(f"이상 징후 {len(WARNINGS)}건:")
            for w in WARNINGS:
                print(f"  ! {w}")
            print("\n위 내용을 그대로 공유해 주세요.")
        else:
            print("이상 없습니다. 전체 수집으로 넘어가도 됩니다.")
        print("=" * 68)
    return 1 if WARNINGS else 0


if __name__ == "__main__":
    sys.exit(main())
