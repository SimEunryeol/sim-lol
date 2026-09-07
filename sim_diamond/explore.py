"""탐색 단계 진행 현황.

  python -m sim_diamond.explore

라인별로 (1) 진행 판수 (2) 기준 챔프 외 플레이 = 위반 (3) GOLD 벤치 대비 지표 격차
(4) 평균 재미 점수 를 모으고, 적합도 = 지표 70% + 재미 30% 로 합친다.

지표 격차 정규화 방식:
  BRONZE 벤치 = 0, GOLD 벤치 = 1 로 두고 내 값이 그 사이 어디인지를 본다.
  지표마다 단위가 달라(분당 CS vs 골드 차이) 절대값을 그냥 못 더하기 때문이고,
  "브론즈에서 골드까지 얼마나 왔나"가 이 프로젝트의 질문 그 자체이기 때문이다.
  두 벤치가 사실상 같은 지표(변별력 없음)는 계산에서 뺀다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import pandas as pd

from . import config
from .db import session
from .phase import ROLE_KO, ROLES, active_phase

# (컬럼, 라벨, 소수 자릿수). 방향은 BRONZE/GOLD 벤치의 대소가 알아서 알려준다.
FIT_METRICS = [
    ("win", "승률", 3),
    ("deaths", "판당 데스", 2),
    ("kda", "KDA", 2),
    ("cs_per_min", "분당 CS", 2),
    ("gold15_diff", "15분 골드 차이", 0),
    ("vision_per_min", "시야점수/분", 3),
    ("obj_participation", "오브젝트 참여율", 3),
]
# 정규화 축의 양 끝. 앞에 있는 티어를 우선 쓰고, 없으면 다음으로 내려간다.
# (BRONZE 를 안 모았어도 SILVER↔GOLD 축으로 계산은 된다)
LOWER_ANCHORS = ("BRONZE", "IRON", "SILVER")
UPPER_ANCHORS = ("GOLD", "PLATINUM", "SILVER")
LOW_BENCH_GAMES = 15      # 벤치 표본이 이보다 적으면 신뢰도 낮음 표시
MIN_SPREAD_RATIO = 0.03   # BRONZE↔GOLD 차이가 이보다 작으면 변별력 없다고 본다
METRIC_WEIGHT, FUN_WEIGHT = 0.7, 0.3


def _kst_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=config.KST).timestamp() * 1000)


def phase_start_ms(phase: dict) -> int:
    return phase["start_ts"] if phase.get("start_ts") else _kst_ms(phase["start_date"])


def load(conn) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_sql_query("""
        SELECT pm.*, p.is_me AS p_is_me, p.is_bench AS p_is_bench, p.tier AS p_tier,
               sr.score AS fun_score, sr.memo AS fun_memo
        FROM participant_metrics pm
        JOIN players p ON p.puuid = pm.puuid
        LEFT JOIN self_rating sr ON sr.match_id = pm.match_id AND sr.puuid = pm.puuid
    """, conn)
    df = df[df["duration_s"].fillna(0) >= config.REMAKE_MAX_S]
    return df[df["p_is_me"] == 1].copy(), df[df["p_is_bench"] == 1].copy()


def _progress(mine: float, lo: float, hi: float) -> float | None:
    """BRONZE(lo)=0, GOLD(hi)=1 축에서 내 위치. 변별력 없는 지표는 None."""
    if lo is None or hi is None or pd.isna(lo) or pd.isna(hi) or pd.isna(mine):
        return None
    spread = hi - lo
    scale = max(abs(lo), abs(hi), 1e-9)
    if abs(spread) / scale < MIN_SPREAD_RATIO:
        return None
    return max(0.0, min(1.0, (mine - lo) / spread))


def role_report(me: pd.DataFrame, bench: pd.DataFrame, phase: dict) -> dict:
    role = phase["role"]
    champs = set(json.loads(phase["champs"]))
    since = phase_start_ms(phase)

    # 단계 시작 시각 이후 경기만 본다. 그 전 경기는 탐색이 아니다(진단 리포트에는 그대로 남는다).
    played = me[(me["team_position"] == role) & (me["game_start"] >= since)]
    on_champ = played[played["champion_name"].isin(champs)] if champs else played
    off_champ = played[~played["champion_name"].isin(champs)] if champs else played.iloc[0:0]

    def slice_of(tier: str) -> pd.DataFrame:
        return bench[(bench["p_tier"] == tier) & (bench["team_position"] == role)]

    def pick(anchors: tuple[str, ...], exclude: str | None = None) -> tuple[str | None, pd.DataFrame]:
        for t in anchors:
            if t == exclude:
                continue
            d = slice_of(t)
            if len(d):
                return t, d
        return None, bench.iloc[0:0]

    lo_tier, lo_df = pick(LOWER_ANCHORS)
    hi_tier, hi_df = pick(UPPER_ANCHORS, exclude=lo_tier)
    b = {"lower": lo_df, "upper": hi_df}
    rows, scores = [], []
    for col, label, digits in FIT_METRICS:
        mine = on_champ[col].mean() if len(on_champ) else float("nan")
        lo = lo_df[col].mean() if len(lo_df) else None
        hi = hi_df[col].mean() if len(hi_df) else None
        prog = _progress(mine, lo, hi)
        if prog is not None:
            scores.append(prog)
        rows.append({"label": label, "digits": digits, "mine": mine,
                     "lower": lo, "upper": hi, "progress": prog})

    fun_vals = on_champ["fun_score"].dropna()
    fun_avg = float(fun_vals.mean()) if len(fun_vals) else None
    fun_norm = (fun_avg - 1) / 4 if fun_avg is not None else None
    metric_score = sum(scores) / len(scores) if scores else None

    fit = None
    if metric_score is not None:
        fit = METRIC_WEIGHT * metric_score + FUN_WEIGHT * (fun_norm if fun_norm is not None else 0.5)

    return {
        "role": role, "champs": sorted(champs), "target": phase["target_games"],
        "start_date": phase["start_date"], "start_ts": since,
        # 진행 판수 = 기준 챔프로 한 경기만. 나머지는 위반으로 따로 센다.
        "games": len(on_champ), "played_total": len(played), "violations": len(off_champ),
        "violation_champs": sorted(set(off_champ["champion_name"].dropna())),
        "winrate": on_champ["win"].mean() if len(on_champ) else None,
        "rows": rows, "metric_score": metric_score,
        "fun_avg": fun_avg, "fun_rated": int(len(fun_vals)), "fit": fit,
        "lower_tier": lo_tier, "upper_tier": hi_tier,
        "bench_games": {(lo_tier or "?"): len(lo_df), (hi_tier or "?"): len(hi_df)},
        "low_bench": len(lo_df) < LOW_BENCH_GAMES or len(hi_df) < LOW_BENCH_GAMES,
    }


def summarize(conn, phase_name: str | None = None) -> dict | None:
    phases = active_phase(conn, phase_name)
    if not phases:
        return None
    me, bench = load(conn)
    order = {r: i for i, r in enumerate(ROLES)}
    reports = sorted((role_report(me, bench, p) for p in phases),
                     key=lambda r: order.get(r["role"], 99))
    done = sum(min(r["games"], r["target"]) for r in reports)
    target = sum(r["target"] for r in reports)
    return {
        "phase_name": phases[0]["phase_name"],
        "start_date": min(p["start_date"] for p in phases),
        "reports": reports, "done": done, "target": target,
        "complete": done >= target,
    }


# ------------------------------------------------------------------ 출력 --
def _f(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:.{digits}f}"


def _pctf(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{v * 100:.0f}%"


def render_markdown(summary: dict) -> str:
    if not summary:
        return ("_등록된 탐색 단계가 없습니다._ `python -m sim_diamond.phase init-explore` 로 "
                "5개 라인 탐색 단계를 만들 수 있다.\n")
    out = [f"단계 **{summary['phase_name']}** (시작 {summary['start_date']}) · "
           f"진행 **{summary['done']}/{summary['target']}판** "
           f"({summary['done'] / max(1, summary['target']):.0%})\n"]
    rows = []
    for r in summary["reports"]:
        rows.append([
            ROLE_KO.get(r["role"], r["role"]),
            f"{r['games']}/{r['target']}",
            str(r["violations"]) + (f" ({', '.join(r['violation_champs'][:3])})"
                                    if r["violation_champs"] else ""),
            _pctf(r["winrate"]),
            _f(r["fun_avg"], 1) + (f" ({r['fun_rated']}판)" if r["fun_rated"] else ""),
            _pctf(r["metric_score"]),
            _f(r["fit"], 2) if r["fit"] is not None else "—",
        ])
    anchors = next((f"{r['lower_tier']}→{r['upper_tier']}" for r in summary["reports"]
                    if r["lower_tier"] and r["upper_tier"]), "벤치 없음")
    out.append(f"| 라인 | 진행 | 기준 챔프 외 | 승률 | 재미 | 지표({anchors}) | 적합도 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    out += ["| " + " | ".join(r) + " |" for r in rows]
    out.append("")
    out.append("- 진행 판수는 **단계 시작 시각 이후 · 기준 챔프로 한 경기**만 센다. "
               "그 전 경기와 기준 챔프 밖 경기는 위 숫자에 안 들어간다(진단 리포트에는 그대로 남는다).")
    if summary["complete"]:
        out.append("- 목표 판수를 채웠다. 이제 라인별 적합도를 비교해 결정할 수 있다.")
    else:
        out.append(f"- **아직 {summary['target'] - summary['done']}판 남았다. "
                   f"목표 판수를 채우기 전에는 라인을 결정하지 않는다.**")
    out.append(f"- 지표({anchors}) = 낮은 티어 벤치 0%, 높은 티어 벤치 100% 축에서 내 위치. "
               "지표마다 단위가 달라(분당 CS vs 골드 차이) 절대값을 그냥 더할 수 없어 이렇게 정규화한다. "
               "두 벤치가 사실상 같은 지표는 변별력이 없어 계산에서 뺀다.")
    out.append(f"- 적합도 = 지표 {METRIC_WEIGHT:.0%} + 재미 {FUN_WEIGHT:.0%}. "
               "재미 기록이 없는 라인은 재미를 중립(0.5)으로 둔다.")
    return "\n".join(out) + "\n"


def print_console(summary: dict) -> None:
    if not summary:
        print("등록된 탐색 단계가 없습니다.")
        print("  python -m sim_diamond.phase init-explore --jungle Nocturne Rammus ...")
        return
    print("=" * 76)
    print(f"탐색 단계 '{summary['phase_name']}' · 시작 {summary['start_date']} · "
          f"진행 {summary['done']}/{summary['target']}판 "
          f"({summary['done'] / max(1, summary['target']):.0%})")
    print("진행 판수 = 단계 시작 시각 이후 · 기준 챔프로 한 경기만")
    print("=" * 76)
    print(f"{'라인':<6}{'진행':>8}{'위반':>6}{'승률':>7}{'재미':>7}{'지표':>7}{'적합도':>8}  기준 챔프")
    print("-" * 76)
    for r in summary["reports"]:
        champs = ", ".join(r["champs"]) or "미지정"
        print(f"{ROLE_KO.get(r['role'], r['role']):<6}"
              f"{r['games']:>4}/{r['target']:<3}"
              f"{r['violations']:>6}"
              f"{_pctf(r['winrate']):>7}"
              f"{_f(r['fun_avg'], 1):>7}"
              f"{_pctf(r['metric_score']):>7}"
              f"{(_f(r['fit'], 2) if r['fit'] is not None else '—'):>8}  {champs}")

    for r in summary["reports"]:
        if not r["games"]:
            continue
        print(f"\n[{ROLE_KO.get(r['role'], r['role'])}] 기준 챔프 {r['games']}판 "
              f"/ 그 외 {r['violations']}판 (단계 시작 후 총 {r['played_total']}판)"
              + (f"  ! 벤치 표본 부족 {r['bench_games']}" if r["low_bench"] else ""))
        if r["violation_champs"]:
            print(f"  기준 챔프 외: {', '.join(r['violation_champs'])}")
        print(f"  {'지표':<18}{'나':>10}{(r['lower_tier'] or '?'):>10}"
              f"{(r['upper_tier'] or '?'):>10}{'진행도':>8}")
        for row in r["rows"]:
            prog = _pctf(row["progress"]) if row["progress"] is not None else "변별력없음"
            print(f"  {row['label']:<18}{_f(row['mine'], row['digits']):>10}"
                  f"{_f(row['lower'], row['digits']):>10}{_f(row['upper'], row['digits']):>10}"
                  f"{prog:>8}")

    print()
    if summary["complete"]:
        print("목표 판수를 채웠습니다. 라인별 적합도를 비교해 결정할 수 있습니다.")
    else:
        left = summary["target"] - summary["done"]
        print(f"아직 {left}판 남았습니다. 목표 판수를 채우기 전에는 라인을 결정하지 않습니다.")
    unrated = sum(1 for r in summary["reports"] if r["games"] and not r["fun_rated"])
    if unrated:
        print(f"재미 점수가 하나도 없는 라인이 {unrated}개 있습니다 — "
              f"경기 끝나고 `python -m sim_diamond.rate <1-5>` 를 찍어주세요.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="탐색 단계 진행 현황")
    ap.add_argument("--phase", default=None, help="단계 이름 (기본: 가장 최근 단계)")
    args = ap.parse_args(argv)
    st = config.load_settings(require_key=False)
    with session(st.db_path) as conn:
        print_console(summarize(conn, args.phase))
    return 0


if __name__ == "__main__":
    sys.exit(main())
