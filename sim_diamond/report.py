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

from . import config, geo
from .db import session
from .ddragon import Static

POSITION_KO = {
    "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜",
    "UTILITY": "서폿", "": "미상", None: "미상",
}
POSITION_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
MIN_CHAMP_GAMES = 5

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
    """리포트 2번에서 쓰는 라인 단위 집계."""
    if df.empty:
        return {}
    return {
        "games": len(df),
        "winrate": df["win"].mean(),
        "deaths": df["deaths"].mean(),
        "cs15_diff": df["cs15_diff"].mean(),
        "gold15_diff": df["gold15_diff"].mean(),
        "cs_per_min": df["cs_per_min"].mean(),
        "vision_per_min": df["vision_per_min"].mean(),
        "obj_participation": df["obj_participation"].mean(),
        "control_wards": df["control_wards_bought"].mean(),
        "first_control_ward_min": df["first_control_ward_min"].mean(),
    }


def cmp_rows(me: dict, benches: dict[str, dict], spec: list[tuple[str, str, callable]]) -> list[list[str]]:
    """항목 | 나 | <티어1> 벤치 | <티어2> 벤치 | … — 벤치 열은 수집된 티어만큼 생긴다."""
    rows = []
    for key, label, render in spec:
        rows.append([label, render(me.get(key))]
                    + [render(benches[t].get(key)) for t in benches])
    return rows


def cmp_headers(tiers: list[str]) -> list[str]:
    return ["항목", "나"] + [f"{t} 벤치" for t in tiers]


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
    if me.empty:
        return "_데이터 없음_\n"
    out = ["**라인별 판수/승률**\n"]
    rows = []
    for pos in POSITION_ORDER:
        sub = me[me["team_position"] == pos]
        if sub.empty:
            continue
        rows.append([POSITION_KO[pos], len(sub), pct(sub["win"].mean()),
                     fmt(sub["deaths"].mean(), 1)])
    out.append(table(["라인", "판수", "승률", "판당 데스"], rows))

    spec = [
        ("games", "판수", lambda v: fmt(int(v), 0) if v else "—"),
        ("winrate", "승률", pct),
        ("deaths", "판당 데스", lambda v: fmt(v, 2)),
        ("cs_per_min", "분당 CS", lambda v: fmt(v, 2)),
        ("cs15_diff", "15분 CS 차이", lambda v: fmt(v, 1, plus=True)),
        ("gold15_diff", "15분 골드 차이", lambda v: fmt(v, 0, plus=True)),
        ("vision_per_min", "시야점수/분", lambda v: fmt(v, 3)),
        ("control_wards", "컨트롤 와드 구매", lambda v: fmt(v, 1)),
        ("first_control_ward_min", "첫 컨트롤 와드(분)", lambda v: fmt(v, 1)),
        ("obj_participation", "오브젝트 참여율 _(추정)_", pct),
    ]
    for pos in POSITION_ORDER:
        sub = me[me["team_position"] == pos]
        if sub.empty:
            continue
        out.append(f"\n**{POSITION_KO[pos]} ({len(sub)}판)**\n")
        benches = {t: agg(bench_slice(df, t, pos)) for t in tiers}
        out.append(table(cmp_headers(tiers), cmp_rows(agg(sub), benches, spec)))
    return "\n".join(out)


def s_champions(me: pd.DataFrame, static: Static) -> str:
    if me.empty:
        return "_데이터 없음_\n"
    rows = []
    for cid, sub in me.groupby("champion_id"):
        if len(sub) < MIN_CHAMP_GAMES:
            continue
        name = static.champion(cid, sub["champion_name"].iloc[0])
        kda = ((sub["kills"] + sub["assists"]) / sub["deaths"].clip(lower=1)).mean()
        rows.append(
            [name, len(sub), pct(sub["win"].mean()), fmt(kda), fmt(sub["deaths"].mean(), 1),
             fmt(sub["gold15_diff"].mean(), 0, plus=True)]
        )
    rows.sort(key=lambda r: -r[1])
    if not rows:
        return f"_{MIN_CHAMP_GAMES}판 이상 플레이한 챔피언이 없습니다._\n"
    return table(["챔피언", "판수", "승률", "KDA", "판당 데스", "15분 골드 차이"], rows)


def s_deaths(deaths: pd.DataFrame, me_metrics: pd.DataFrame) -> str:
    mine = deaths[deaths["p_is_me"] == 1]
    if mine.empty:
        return "_데이터 없음_\n"
    out = []
    total = len(mine)

    buckets = ["0-5", "5-10", "10-15", "15-20", "20-25", "25+"]
    def bucket(m):
        for lo, hi, label in zip([0, 5, 10, 15, 20, 25], [5, 10, 15, 20, 25, 10**6], buckets):
            if lo <= m < hi:
                return label
        return buckets[-1]
    counts = Counter(mine["minute"].map(bucket))
    out.append("**시간대별 데스 분포**\n")
    out.append(
        table(
            ["구간(분)", "데스", "비율", "판당"],
            [[b, counts.get(b, 0), pct(counts.get(b, 0) / total),
              fmt(counts.get(b, 0) / max(1, len(me_metrics)), 2)] for b in buckets],
        )
    )

    zone_counts = Counter(mine["zone"])
    out.append("\n**구역별 데스 분포**\n")
    out.append(
        table(
            ["구역", "데스", "비율"],
            [[geo.zone_label(z), zone_counts[z], pct(zone_counts[z] / total)]
             for z in geo.ZONE_ORDER if zone_counts.get(z)],
        )
    )

    judged = mine[mine["warded"].notna()]
    if len(judged):
        unwarded = int((judged["warded"] == 0).sum())
        out.append(
            f"\n- 총 데스 **{total}회**, 그중 판정 가능한 **{len(judged)}회** 기준\n"
            f"- 죽기 직전 60초 안에 반경 1500 내 아군 와드가 **없던** 데스 _(추정)_: "
            f"**{unwarded}회 ({pct(unwarded / len(judged))})**\n"
            f"- 15분 이전 데스: **{int((mine['minute'] < 15).sum())}회** "
            f"(판당 {fmt((mine['minute'] < 15).sum() / max(1, len(me_metrics)), 2)})\n"
        )
    return "\n".join(out)


def s_jungle(me: pd.DataFrame, df: pd.DataFrame, tiers: list[str]) -> str:
    jg = me[me["is_jungle"] == 1]
    if jg.empty:
        return ""
    spec = [
        ("first_full_clear_min", "첫 풀캠프 완료(분) _(추정)_", lambda v: fmt(v, 2)),
        ("first_gank_min", "첫 갱(분)", lambda v: fmt(v, 2)),
        ("gank_rate", "갱 성사 판 비율", pct),
        ("first_counter_jungled_min", "첫 카정 피해(분)", lambda v: fmt(v, 2)),
        ("counter_jungled_rate", "카정 피해 판 비율", pct),
        ("jg_gold_diff_5", "5분 정글 골드 차이", lambda v: fmt(v, 0, plus=True)),
        ("jg_gold_diff_10", "10분 정글 골드 차이", lambda v: fmt(v, 0, plus=True)),
        ("jg_level_diff_10", "10분 레벨 차이", lambda v: fmt(v, 2, plus=True)),
        ("enemy_jungle_minutes", "적정글 체류(분/판)", lambda v: fmt(v, 2)),
        ("obj_participation", "오브젝트 참여율 _(추정)_", pct),
    ]

    def jagg(d: pd.DataFrame) -> dict:
        d = d[d["is_jungle"] == 1]
        if d.empty:
            return {}
        # 첫 갱/첫 카정 평균은 "실제로 일어난 판"만 대상으로 한다(없는 판은 NaN).
        out = {k: d[k].mean() for k, _, _ in spec if k in d.columns}
        out["gank_rate"] = d["first_gank_min"].notna().mean()
        out["counter_jungled_rate"] = d["first_counter_jungled_min"].notna().mean()
        return out

    body = [f"정글 **{len(jg)}판** · 승률 **{pct(jg['win'].mean())}**\n"]
    body.append(table(cmp_headers(tiers),
                      cmp_rows(jagg(jg), {t: jagg(bench_slice(df, t)) for t in tiers}, spec)))
    body.append(
        "\n- 첫 갱 = 3분 이후 **라인(탑/미드/봇)** 에서 내가 딴 첫 킬/어시. "
        "정글·강에서 난 교전과 내 데스는 세지 않는다.\n"
        "- 첫 카정 피해 = **내 정글에서 내가 죽은** 첫 시각(시간 하한 없음, 초반 인베이드 포함).\n"
        "- 두 지표의 평균은 실제로 일어난 판만 대상이므로, 옆의 '판 비율'과 같이 봐야 한다.\n"
    )
    return "\n".join(body)


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
        rows.append(
            [month, len(sub), pct(sub["win"].mean()), fmt(sub["deaths"].mean(), 2),
             fmt(sub["cs_per_min"].mean(), 2), fmt(sub["vision_per_min"].mean(), 3)]
        )
    return table(["월", "판수", "승률", "판당 데스", "분당 CS", "시야/분"], rows)


def s_notes(static: Static) -> str:
    return (
        "- 와드 위치: 타임라인의 `WARD_PLACED` 이벤트에는 좌표가 없다. 설치자의 그 시각 위치"
        "(분 단위 프레임 + 킬 이벤트 좌표를 선형보간)로 **근사**한 값이다.\n"
        "- 오브젝트 참여: 본인 좌표도 같은 방식의 근사값이며, 킬러 본인·어시스트 기록자는 무조건 참여로 본다.\n"
        "- 첫 풀캠프: 정글 CS 가 12에 도달한 시각을 프레임 사이 선형보간으로 추정한다.\n"
        "- 귀환: 타임라인에 귀환 이벤트가 없어 상점 구매 묶음(간격 20초 초과 시 분리)을 기지 방문으로 본다. "
        "직전 방문 이후 죽은 적이 있으면 부활로 돌아온 것이므로 자발적 귀환에서 뺀다.\n"
        f"- 리메이크/조기종료({config.REMAKE_MAX_S // 60}분 미만)는 모든 집계에서 제외한다.\n"
        + ("- 코어템 판정: Data Dragon 캐시가 없어 이번 리포트에서는 비어 있다.\n" if not static.available else
           f"- 코어템 판정: Data Dragon {static.version} 기준 총 골드 2900 이상 아이템의 첫 구매.\n")
    )


# ------------------------------------------------------------------ 진입 --
def build(conn: sqlite3.Connection) -> str:
    metrics, deaths = load(conn)
    static = Static(conn)
    # 리메이크/조기종료는 모든 집계에서 뺀다(승률·판당 데스를 왜곡한다).
    n_all = len(metrics)
    remakes = metrics[metrics["duration_s"].fillna(0) < config.REMAKE_MAX_S]
    n_my_remakes = int((remakes["p_is_me"] == 1).sum())
    metrics = metrics[metrics["duration_s"].fillna(0) >= config.REMAKE_MAX_S]
    me = metrics[metrics["p_is_me"] == 1].copy()
    bench_tiers = sort_tiers(
        t for t in metrics[metrics["p_is_bench"] == 1]["p_tier"].dropna().unique()
    )
    bench_desc = [f"{t} {len(bench_slice(metrics, t))}판" for t in bench_tiers]

    today = datetime.now().strftime("%Y-%m-%d")
    parts = [
        "# 심은렬 다이아만들기 — 첫 진단 리포트",
        f"\n_생성일: {today}_\n",
        "\n## 1. 요약\n",
        s_summary(me, conn, bench_desc, n_my_remakes),
        "\n## 2. 라인별 지표 (벤치마크 비교)\n",
        s_positions(me, metrics, bench_tiers),
        f"\n## 3. 챔피언별 ({MIN_CHAMP_GAMES}판 이상)\n",
        s_champions(me, static),
        "\n## 4. 데스 패턴\n",
        s_deaths(deaths, me),
    ]
    jungle = s_jungle(me, metrics, bench_tiers)
    if jungle:
        parts += ["\n## 5. 정글 전용\n", jungle]
    parts += [
        "\n## 6. 시간 흐름 (월별)\n",
        s_trend(me),
        "\n## 7. 코치 메모\n",
        "> TODO: 다음 단계에서 Claude API 로 생성 예정.\n",
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
