"""data/report_YYYYMMDD.md 생성.

  python -m sim_diamond.report
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime

import pandas as pd

from . import config, explore, geo
from . import metrics as metrics_mod
from .db import session
from .ddragon import Static

POSITION_KO = {
    "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜",
    "UTILITY": "서폿", "": "미상", None: "미상",
}
POSITION_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
MIN_CHAMP_GAMES = 3       # 챔프 표에 올릴 최소 판수
MIN_POSITION_GAMES = 15   # 이 미만이면 "표본 부족" (탐색 단계 목표와 같은 값)
MIN_BENCH_GAMES = 20      # 벤치 열도 마찬가지
SMALL = " ⚠"

# 벤치 열 순서. 알파벳순으로 두면 BRONZE·GOLD·SILVER 가 되어 실력 순서가 뒤집힌다.
TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
              "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]


def sort_tiers(tiers) -> list[str]:
    return sorted(tiers, key=lambda t: (TIER_ORDER.index(t) if t in TIER_ORDER else 99, t))


# ------------------------------------------------------------------ 유틸 --
def fmt(value, digits: int = 2, suffix: str = "", plus: bool = False) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if isinstance(value, (int,)) and not plus:
        return f"{value}{suffix}"
    txt = f"{value:+.{digits}f}" if plus else f"{value:.{digits}f}"
    return f"{txt}{suffix}"


def pct(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value * 100:.1f}%"


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_데이터 없음_\n"
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------- 데이터 로드 --
def load(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame]:
    metrics = pd.read_sql_query(
        """
        SELECT pm.*, p.is_me AS p_is_me, p.is_bench AS p_is_bench, p.tier AS p_tier
        FROM participant_metrics pm
        JOIN players p ON p.puuid = pm.puuid
        """,
        conn,
    )
    deaths = pd.read_sql_query(
        """
        SELECT d.*, p.is_me AS p_is_me, p.is_bench AS p_is_bench, p.tier AS p_tier,
               pm.team_position
        FROM deaths_detail d
        JOIN players p ON p.puuid = d.puuid
        LEFT JOIN participant_metrics pm
               ON pm.match_id = d.match_id AND pm.puuid = d.puuid
        """,
        conn,
    )
    return metrics, deaths


def bench_slice(df: pd.DataFrame, tier: str, position: str | None = None) -> pd.DataFrame:
    out = df[(df["p_is_bench"] == 1) & (df["p_tier"] == tier)]
    if position is not None:
        out = out[out["team_position"] == position]
    return out


def agg(df: pd.DataFrame) -> dict:
    """라인 비교표가 쓰는 값 전부. 어떤 spec 이 와도 여기서 꺼내 쓴다."""
    if df.empty:
        return {}
    out = {"games": len(df), "winrate": df["win"].mean()}
    for col in ("deaths", "kda", "cs", "cs_per_min", "cs10", "cs15",
                "cs10_diff", "cs15_diff", "gold10_diff", "gold15_diff",
                "level10_diff", "level15_diff", "vision_per_min", "wards_placed",
                "wards_killed", "first_control_ward_min", "first_core_item_min",
                "voluntary_back_count", "deaths_before_15", "damage_to_champs",
                "obj_participation", "obj_participation_800", "obj_participation_1200",
                "obj_participation_2500", "first_full_clear_min", "first_gank_min",
                "first_counter_jungled_min", "jg_gold_diff_5", "jg_gold_diff_10",
                "jg_level_diff_10", "enemy_jungle_minutes"):
        if col in df.columns:
            out[col] = df[col].mean()
    out["control_wards"] = df["control_wards_bought"].mean()
    # 실제로 일어난 판만 평균 내는 지표는 "일어난 비율"을 같이 봐야 한다
    for col, key in (("first_gank_min", "gank_rate"),
                     ("first_counter_jungled_min", "counter_jungled_rate"),
                     ("first_control_ward_min", "control_ward_rate")):
        if col in df.columns:
            base = df[df["is_jungle"] == 1] if col != "first_control_ward_min" else df
            out[key] = base[col].notna().mean() if len(base) else None
    judged = (df["deaths_warded"].fillna(0) + df["deaths_unwarded"].fillna(0)).sum()
    out["unwarded_rate"] = (df["deaths_unwarded"].fillna(0).sum() / judged) if judged else None
    return out


# 모든 라인에 공통으로 보는 지표
COMMON_SPEC = [
    ("games", "판수", lambda v: fmt(int(v), 0) if v else "—"),
    ("winrate", "승률", pct),
    ("deaths", "판당 데스", lambda v: fmt(v, 2)),
    ("kda", "KDA", lambda v: fmt(v, 2)),
    ("cs_per_min", "분당 CS", lambda v: fmt(v, 2)),
    ("cs15_diff", "15분 CS 차이", lambda v: fmt(v, 1, plus=True)),
    ("gold15_diff", "15분 골드 차이", lambda v: fmt(v, 0, plus=True)),
    ("level15_diff", "15분 레벨 차이", lambda v: fmt(v, 2, plus=True)),
    ("vision_per_min", "시야점수/분", lambda v: fmt(v, 3)),
    ("control_wards", "컨트롤 와드 구매", lambda v: fmt(v, 1)),
    ("control_ward_rate", "컨트롤 와드 산 판 비율", pct),
    ("first_control_ward_min", "첫 컨트롤 와드(분)", lambda v: fmt(v, 1)),
    ("first_core_item_min", "첫 코어템(분)", lambda v: fmt(v, 1)),
    ("voluntary_back_count", "자발적 귀환", lambda v: fmt(v, 1)),
    ("obj_participation", "오브젝트 참여율 r1200 _(추정)_", pct),
]

LANE_SPEC = [
    ("cs10_diff", "10분 CS 차이", lambda v: fmt(v, 1, plus=True)),
    ("gold10_diff", "10분 골드 차이", lambda v: fmt(v, 0, plus=True)),
    ("level10_diff", "10분 레벨 차이", lambda v: fmt(v, 2, plus=True)),
    ("deaths_before_15", "15분 이전 데스", lambda v: fmt(v, 2)),
    ("unwarded_rate", "시야 없이 죽은 비율 _(추정)_", pct),
    ("obj_participation_800", "오브젝트 참여율 r800 _(추정)_", pct),
    ("obj_participation_2500", "오브젝트 참여율 r2500 _(추정)_", pct),
]

ROLE_SPEC = {
    "JUNGLE": [
        ("first_full_clear_min", "첫 풀캠프 완료(분) _(추정)_", lambda v: fmt(v, 2)),
        ("first_gank_min", "첫 갱(분)", lambda v: fmt(v, 2)),
        ("gank_rate", "갱 성사 판 비율", pct),
        ("first_counter_jungled_min", "첫 카정 피해(분)", lambda v: fmt(v, 2)),
        ("counter_jungled_rate", "카정 피해 판 비율", pct),
        ("jg_gold_diff_5", "5분 정글 골드 차이", lambda v: fmt(v, 0, plus=True)),
        ("jg_gold_diff_10", "10분 정글 골드 차이", lambda v: fmt(v, 0, plus=True)),
        ("jg_level_diff_10", "10분 정글 레벨 차이", lambda v: fmt(v, 2, plus=True)),
        ("enemy_jungle_minutes", "적정글 체류(분/판)", lambda v: fmt(v, 2)),
        ("deaths_before_15", "15분 이전 데스", lambda v: fmt(v, 2)),
        ("unwarded_rate", "시야 없이 죽은 비율 _(추정)_", pct),
        ("obj_participation_800", "오브젝트 참여율 r800 _(추정)_", pct),
        ("obj_participation_2500", "오브젝트 참여율 r2500 _(추정)_", pct),
    ],
    "UTILITY": [
        ("wards_placed", "와드 설치", lambda v: fmt(v, 1)),
        ("wards_killed", "와드 제거", lambda v: fmt(v, 1)),
        ("deaths_before_15", "15분 이전 데스", lambda v: fmt(v, 2)),
        ("unwarded_rate", "시야 없이 죽은 비율 _(추정)_", pct),
        ("cs10_diff", "10분 CS 차이", lambda v: fmt(v, 1, plus=True)),
        ("gold10_diff", "10분 골드 차이", lambda v: fmt(v, 0, plus=True)),
        ("obj_participation_800", "오브젝트 참여율 r800 _(추정)_", pct),
        ("obj_participation_2500", "오브젝트 참여율 r2500 _(추정)_", pct),
    ],
    "TOP": LANE_SPEC, "MIDDLE": LANE_SPEC, "BOTTOM": LANE_SPEC,
}


def cmp_rows(me: dict, benches: dict[str, dict], spec: list[tuple[str, str, callable]]) -> list[list[str]]:
    """항목 | 나 | <티어1> 벤치 | <티어2> 벤치 | … — 벤치 열은 수집된 티어만큼 생긴다."""
    rows = []
    for key, label, render in spec:
        rows.append([label, render(me.get(key))]
                    + [render(benches[t].get(key)) for t in benches])
    return rows


def cmp_headers(tiers: list[str], counts: dict[str, int] | None = None) -> list[str]:
    """벤치 열 제목에 표본 수를 같이 적는다. 8판짜리 평균과 50판짜리 평균은 다르게 읽어야 한다."""
    out = ["항목", "나"]
    for t in tiers:
        n = (counts or {}).get(t)
        mark = SMALL if n is not None and n < MIN_BENCH_GAMES else ""
        out.append(f"{t} 벤치" + (f" ({n}판{mark})" if n is not None else ""))
    return out


# ---------------------------------------------------------------- 섹션들 --
def s_summary(me: pd.DataFrame, conn: sqlite3.Connection, bench_tiers: list[str],
              n_remakes: int = 0) -> str:
    player = conn.execute("SELECT * FROM players WHERE is_me = 1").fetchone()
    tier = f"{player['tier']} {player['rank']} {player['lp']}LP" if player and player["tier"] else "미상"
    name = f"{player['game_name']}#{player['tag_line']}" if player else "?"
    if me.empty:
        return f"내 계정: **{name}** / 티어: **{tier}**\n\n_수집된 경기가 없습니다._\n"
    starts = pd.to_datetime(me["game_start"], unit="ms", utc=True).dt.tz_convert("Asia/Seoul")
    lines = [
        f"- 계정: **{name}**  ·  현재 티어: **{tier}**",
        f"- 수집 기간: **{starts.min():%Y-%m-%d} ~ {starts.max():%Y-%m-%d}** (KST)",
        f"- 총 **{len(me)}판** · 승률 **{pct(me['win'].mean())}** ({int(me['win'].sum())}승 {int(len(me) - me['win'].sum())}패)",
        f"- 평균 KDA **{fmt(((me['kills'] + me['assists']) / me['deaths'].clip(lower=1)).mean())}** "
        f"(K {fmt(me['kills'].mean(), 1)} / D {fmt(me['deaths'].mean(), 1)} / A {fmt(me['assists'].mean(), 1)})",
        f"- 평균 분당 CS **{fmt(me['cs_per_min'].mean())}** · 평균 시야점수/분 **{fmt(me['vision_per_min'].mean())}**",
        ("- 벤치마크: " + ", ".join(bench_tiers)) if bench_tiers else "- 벤치마크: _수집 안 됨_",
    ]
    if n_remakes:
        lines.append(f"- 리메이크/조기종료 **{n_remakes}판**은 모든 집계에서 제외했다 "
                     f"({config.REMAKE_MAX_S // 60}분 미만)")
    return "\n".join(lines) + "\n"


def s_positions(me: pd.DataFrame, df: pd.DataFrame, tiers: list[str]) -> str:
    """라인마다 판수·승률·공통 지표·라인 전용 지표를 벤치와 나란히 놓는다."""
    if me.empty:
        return "_데이터 없음_\n"
    out = ["**전체 라인 요약**\n"]
    rows = []
    for pos in POSITION_ORDER:
        sub = me[me["team_position"] == pos]
        short = len(sub) < MIN_POSITION_GAMES
        rows.append([
            POSITION_KO[pos] + (SMALL if short else ""),
            len(sub),
            pct(sub["win"].mean()) if len(sub) else "—",
            fmt(sub["deaths"].mean(), 1) if len(sub) else "—",
            "표본 부족" if short else "",
        ])
    out.append(table(["라인", "판수", "승률", "판당 데스", "비고"], rows))
    out.append(f"\n⚠ 표본 부족 = {MIN_POSITION_GAMES}판 미만. "
               "승률·평균이 크게 흔들리므로 판단 근거로 쓰지 않는다.\n")

    for pos in POSITION_ORDER:
        sub = me[me["team_position"] == pos]
        short = len(sub) < MIN_POSITION_GAMES
        head = f"\n### {POSITION_KO[pos]} — {len(sub)}판"
        if len(sub):
            head += f" · 승률 {pct(sub['win'].mean())}"
        if short:
            head += f"  ⚠ **표본 부족** ({len(sub)}/{MIN_POSITION_GAMES}판)"
        out.append(head + "\n")
        if sub.empty:
            out.append("_아직 이 라인 경기가 없다._\n")
            continue

        slices = {t: bench_slice(df, t, pos) for t in tiers}
        benches = {t: agg(d) for t, d in slices.items()}
        counts = {t: len(d) for t, d in slices.items()}
        mine = agg(sub)
        out.append("**공통 지표**\n")
        out.append(table(cmp_headers(tiers, counts), cmp_rows(mine, benches, COMMON_SPEC)))
        spec = ROLE_SPEC.get(pos)
        if spec:
            out.append(f"\n**{POSITION_KO[pos]} 전용 지표**\n")
            out.append(table(cmp_headers(tiers, counts), cmp_rows(mine, benches, spec)))
    return "\n".join(out)


def s_champions(me: pd.DataFrame, static: Static, mastery: dict[int, dict]) -> str:
    if me.empty:
        return "_데이터 없음_\n"
    rows = []
    for cid, sub in me.groupby("champion_id"):
        if len(sub) < MIN_CHAMP_GAMES:
            continue
        name = static.champion(cid, sub["champion_name"].iloc[0])
        kda = ((sub["kills"] + sub["assists"]) / sub["deaths"].clip(lower=1)).mean()
        top_pos = sub["team_position"].mode()
        pos = POSITION_KO.get(top_pos.iloc[0], "—") if len(top_pos) else "—"
        m = mastery.get(int(cid)) or {}
        pts = m.get("champion_points")
        rows.append([
            name, pos, len(sub), pct(sub["win"].mean()), fmt(kda),
            fmt(sub["deaths"].mean(), 1), fmt(sub["gold15_diff"].mean(), 0, plus=True),
            f"{pts:,}" if pts else "—",
            m.get("champion_level") or "—",
        ])
    rows.sort(key=lambda r: -r[2])
    if not rows:
        return f"_{MIN_CHAMP_GAMES}판 이상 플레이한 챔피언이 없습니다._\n"
    out = [table(["챔피언", "주 라인", "판수", "승률", "KDA", "판당 데스",
                  "15분 골드 차이", "숙련도 점수", "숙련도 레벨"], rows)]
    out.append(f"\n- {MIN_CHAMP_GAMES}판 이상 플레이한 챔피언 전부. 숙련도는 "
               "Champion-Mastery-V4 상위 20개 기준이라 그 밖의 챔프는 `—` 로 나온다.\n")
    return "\n".join(out)


def load_mastery(conn: sqlite3.Connection) -> dict[int, dict]:
    return {r["champion_id"]: dict(r) for r in conn.execute("""
        SELECT cm.* FROM champion_mastery cm
        JOIN players p ON p.puuid = cm.puuid WHERE p.is_me = 1
    """)}


DEATH_BUCKETS = [(0, 5, "0-5"), (5, 10, "5-10"), (10, 15, "10-15"),
                 (15, 20, "15-20"), (20, 25, "20-25"), (25, 10 ** 6, "25+")]


def _bucket(minute: float) -> str:
    for lo, hi, label in DEATH_BUCKETS:
        if lo <= minute < hi:
            return label
    return DEATH_BUCKETS[-1][2]


def s_deaths(deaths: pd.DataFrame, me_metrics: pd.DataFrame, metrics: pd.DataFrame,
             tiers: list[str]) -> str:
    mine = deaths[deaths["p_is_me"] == 1]
    if mine.empty:
        return "_데이터 없음_\n"
    out = []
    total = len(mine)
    n_games = max(1, len(me_metrics))

    # 티어별 데스와 판수. 데스 절대수는 표본이 달라 비교가 안 되므로 판당/비율로 본다.
    bench: dict[str, tuple[pd.DataFrame, int]] = {}
    for t in tiers:
        d = deaths[(deaths["p_is_bench"] == 1) & (deaths["p_tier"] == t)]
        g = int(((metrics["p_is_bench"] == 1) & (metrics["p_tier"] == t)).sum())
        bench[t] = (d, max(1, g))

    counts = Counter(mine["minute"].map(_bucket))
    out.append("**시간대별 데스 (판당)**\n")
    rows = []
    for _, _, b in DEATH_BUCKETS:
        n = counts.get(b, 0)
        row = [b, n, pct(n / total), fmt(n / n_games, 2)]
        for t in tiers:
            d, g = bench[t]
            row.append(fmt(sum(1 for m in d["minute"] if _bucket(m) == b) / g, 2))
        rows.append(row)
    out.append(table(["구간(분)", "데스", "비율", "나 판당"] + [f"{t} 판당" for t in tiers], rows))

    zone_counts = Counter(mine["zone"])
    bench_zone = {t: Counter(d["zone"]) for t, (d, _) in bench.items()}
    out.append("\n**구역별 데스 (비율)**\n")
    rows = []
    for z in geo.ZONE_ORDER:
        if not zone_counts.get(z):
            continue
        row = [geo.zone_label(z), zone_counts[z], pct(zone_counts[z] / total)]
        for t in tiers:
            d, _ = bench[t]
            row.append(pct(bench_zone[t][z] / len(d)) if len(d) else "—")
        rows.append(row)
    out.append(table(["구역", "데스", "비율"] + [f"{t}" for t in tiers], rows))

    judged = mine[mine["warded"].notna()]
    if len(judged):
        unwarded = int((judged["warded"] == 0).sum())
        rows = [
            ["시야 없이 죽은 비율 _(추정)_", pct(unwarded / len(judged))]
            + [_unwarded_rate(bench[t][0]) for t in tiers],
            ["판당 데스", fmt(total / n_games, 2)]
            + [fmt(len(bench[t][0]) / bench[t][1], 2) for t in tiers],
            ["15분 이전 데스(판당)", fmt((mine["minute"] < 15).sum() / n_games, 2)]
            + [fmt((bench[t][0]["minute"] < 15).sum() / bench[t][1], 2) for t in tiers],
        ]
        out.append("\n**벤치마크 비교**\n")
        out.append(table(["항목", "나"] + [f"{t} 벤치" for t in tiers], rows))
        out.append(
            f"\n- 총 데스 **{total}회** / {n_games}판. 죽기 직전 60초 안에 반경 1500 내 아군 와드가 "
            f"**없던** 데스 **{unwarded}회 ({pct(unwarded / len(judged))})** _(추정)_\n"
            f"- 와드 좌표는 라이엇이 주지 않아 근사값이다. 절대 수치보다 **벤치와의 차이**를 봐야 한다.\n"
        )
    return "\n".join(out)


def _unwarded_rate(d: pd.DataFrame) -> str:
    judged = d[d["warded"].notna()]
    return pct((judged["warded"] == 0).sum() / len(judged)) if len(judged) else "—"


def s_trend(me: pd.DataFrame) -> str:
    if me.empty or me["game_start"].isna().all():
        return "_데이터 없음_\n"
    d = me.copy()
    d["month"] = (
        pd.to_datetime(d["game_start"], unit="ms", utc=True)
        .dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m")
    )
    rows = []
    for month, sub in d.groupby("month"):
        row = [month + (SMALL if len(sub) < MIN_POSITION_GAMES else ""), len(sub),
               pct(sub["win"].mean()), fmt(sub["deaths"].mean(), 2),
               fmt(sub["cs_per_min"].mean(), 2), fmt(sub["vision_per_min"].mean(), 3),
               fmt(sub["control_wards_bought"].mean(), 1)]
        for pos in POSITION_ORDER:
            n = int((sub["team_position"] == pos).sum())
            row.append(f"{n / len(sub) * 100:.0f}%" if n else "—")
        rows.append(row)
    headers = (["월", "판수", "승률", "판당 데스", "분당 CS", "시야/분", "컨트롤 와드"]
               + [POSITION_KO[p] for p in POSITION_ORDER])
    out = [table(headers, rows)]
    out.append(f"\n- 오른쪽 5개 열은 그 달의 라인 비율(판수 기준). ⚠ = {MIN_POSITION_GAMES}판 미만.\n")
    return "\n".join(out)


def s_coach(conn: sqlite3.Connection) -> str:
    from .coach import latest_memo
    m = latest_memo(conn)
    if not m:
        return ("> 아직 생성되지 않았다. `python -m sim_diamond.coach` 로 만든다 "
                "(.env 에 ANTHROPIC_API_KEY 필요).\n")
    return (f"_{m['created_at'][:10]} · {m['model']} 생성_\n\n{m['memo']}\n")


def s_notes(static: Static) -> str:
    return (
        "- 와드 위치: 타임라인의 `WARD_PLACED` 이벤트에는 좌표가 없다. 설치자의 그 시각 위치"
        "(분 단위 프레임 + 킬 이벤트 좌표를 선형보간)로 **근사**한 값이다.\n"
        f"- 오브젝트 참여: 처치 시각 ±{metrics_mod.OBJECTIVE_WINDOW_MS // 1000}초 안에서 몬스터에 가장 "
f"가까웠던 거리로 판정한다. 기본 반경 {int(metrics_mod.OBJECTIVE_RADIUS)}, 함께 표시하는 r800/r2500 은 "
f"같은 계산의 반경만 바꾼 값이다. 본인 좌표도 근사값이며, 처치자 본인·어시스트 기록자는 무조건 참여로 본다.\n"
        f"- 첫 풀캠프: 정글 몬스터 처치 수가 {metrics_mod.FULL_CLEAR_JUNGLE_CS}마리"
        f"(6캠프 전체 분량)에 도달한 시각을 프레임 사이 선형보간으로 추정한다.\n"
        "- 귀환: 타임라인에 귀환 이벤트가 없어 상점 구매 묶음(간격 20초 초과 시 분리)을 기지 방문으로 본다. "
        "직전 방문 이후 죽은 적이 있으면 부활로 돌아온 것이므로 자발적 귀환에서 뺀다.\n"
        f"- 리메이크/조기종료({config.REMAKE_MAX_S // 60}분 미만)는 모든 집계에서 제외한다.\n"
        + ("- 코어템 판정: Data Dragon 캐시가 없어 이번 리포트에서는 비어 있다.\n" if not static.available else
           f"- 코어템 판정: Data Dragon {static.version} 기준 총 골드 2900 이상 아이템의 첫 구매.\n")
    )


# ------------------------------------------------------------------ 진입 --
def build(conn: sqlite3.Connection, include_memo: bool = True) -> str:
    metrics, deaths = load(conn)
    static = Static(conn)
    mastery = load_mastery(conn)

    # 리메이크/조기종료는 모든 집계에서 뺀다(승률·판당 데스를 왜곡한다).
    remakes = metrics[metrics["duration_s"].fillna(0) < config.REMAKE_MAX_S]
    n_my_remakes = int((remakes["p_is_me"] == 1).sum())
    metrics = metrics[metrics["duration_s"].fillna(0) >= config.REMAKE_MAX_S]
    deaths = deaths[deaths["match_id"].isin(set(metrics["match_id"]))]
    me = metrics[metrics["p_is_me"] == 1].copy()

    bench_tiers = sort_tiers(
        t for t in metrics[metrics["p_is_bench"] == 1]["p_tier"].dropna().unique()
    )
    bench_desc = [f"{t} {len(bench_slice(metrics, t))}판" for t in bench_tiers]
    explore_summary = explore.summarize(conn)

    today = datetime.now().strftime("%Y-%m-%d")
    parts = [
        "# 심은렬 다이아만들기 — 첫 진단 리포트",
        f"\n_생성일: {today}_\n",
        "\n## 1. 요약\n",
        s_summary(me, conn, bench_desc, n_my_remakes),
        "\n## 2. 라인별 지표 (벤치마크 비교)\n",
        "5개 라인을 모두 탐색하는 중이다. 아직 주 라인을 확정하지 않았다.\n",
        s_positions(me, metrics, bench_tiers),
        f"\n## 3. 챔피언별 ({MIN_CHAMP_GAMES}판 이상)\n",
        s_champions(me, static, mastery),
        "\n## 4. 데스 패턴\n",
        s_deaths(deaths, me, metrics, bench_tiers),
        "\n## 5. 탐색 단계 진행 현황\n",
        explore.render_markdown(explore_summary),
        "\n## 6. 시간 흐름 (월별)\n",
        s_trend(me),
        "\n## 7. 코치 메모\n",
        s_coach(conn) if include_memo else "> (생성 중)\n",
        "\n---\n\n### 계산 방식 주석\n",
        s_notes(static),
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="진단 리포트 생성")
    ap.add_argument("--out", default=None, help="출력 경로 (기본 data/report_YYYYMMDD.md)")
    args = ap.parse_args(argv)

    st = config.load_settings(require_key=False)
    with session(st.db_path) as conn:
        text = build(conn)
    out = args.out or str(config.DATA_DIR / f"report_{datetime.now():%Y%m%d}.md")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"리포트 작성: {out} ({len(text.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
