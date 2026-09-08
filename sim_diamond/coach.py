"""코치 메모 생성 (Claude API).

  python -m sim_diamond.coach                # 생성해서 DB 에 저장하고 리포트를 다시 만든다
  python -m sim_diamond.coach --dry-run      # API 호출 없이 프롬프트만 출력 (토큰/비용 확인용)
  python -m sim_diamond.coach --show         # 저장된 최신 메모 보기

.env 에 ANTHROPIC_API_KEY 가 필요하다.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from . import config, explore, report
from .db import now_iso, session, upsert

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 16000

# 라인전 진단이 한 절 늘어서 900 → 1100. SPEC.md 의 분량 문구도 같이 고쳤다.
MAX_CHARS = 1100

# 이번 주 과제는 사용자가 SPEC.md 에서 직접 정한다. 코치가 매주 데이터를 보고 새로
# 고르게 두면 주마다 과제가 바뀌어 "한 번에 하나만 고친다"는 원칙이 깨진다. 실제로
# 첫 실행에서 코치가 SPEC 과 다른 목표치(판당 2개 → 산 판 비율 87%)를 만들어냈다.
# 과제를 바꿀 때는 SPEC.md 와 이 상수를 함께 고쳐라.
WEEKLY_TASK = "제어 와드를 매 귀환마다 1개씩 사서, 판당 2개 이상 유지하세요."

# 이번 주 과제를 "무슨 숫자로 확인하는가". SPEC.md 가 요구하는 세 요소 중 뒤 둘이다.
# 코치 메모 본문에서 숫자를 긁어오지 않는다 — 문장이 조금만 바뀌어도 조용히 깨진다.
# 과제를 바꾸면 coach.WEEKLY_TASK 와 여기를 같이 고쳐라.
WEEKLY_TASK_METRICS = [
    {"label": "제어 와드 산 판 비율", "col": "control_ward_rate", "pct": True,
     "target": 0.70, "bench_tier": "GOLD"},
    {"label": "판당 제어 와드", "col": "control_wards", "pct": False,
     "target": 2.0, "bench_tier": "GOLD"},
]


# 판 하나가 과제를 지켰는지 판정하는 기준. 과제를 바꾸면 여기도 같이 고쳐라.
WEEKLY_TASK_CHECK = {"column": "control_wards_bought", "label": "제어 와드",
                     "unit": "개", "min": 2}


def task_check(conn, match_ids: list[str], puuid: str) -> dict[str, dict]:
    """경기별로 이번 주 과제를 지켰는지. 30초 루틴에서 바로 보이라고 만든다."""
    if not match_ids:
        return {}
    col = WEEKLY_TASK_CHECK["column"]
    q = ",".join("?" * len(match_ids))
    rows = conn.execute(
        f"SELECT match_id, {col} AS v FROM participant_metrics "
        f"WHERE puuid = ? AND match_id IN ({q})", [puuid, *match_ids]).fetchall()
    out = {}
    for r in rows:
        v = r["v"]
        out[r["match_id"]] = {"value": v,
                              "ok": (v is not None and v >= WEEKLY_TASK_CHECK["min"])}
    return out


def task_targets(conn, position: str | None) -> list[dict]:
    """이번 주 과제의 '지금 값 → 목표'. 지금 탐색 중인 라인 기준으로 센다."""
    metrics, _ = report.load(conn)
    metrics = metrics[metrics["duration_s"].fillna(0) >= config.REMAKE_MAX_S]
    me = metrics[metrics["p_is_me"] == 1]
    if position:
        me = me[me["team_position"] == position]
    if me.empty:
        return []
    mine = report.agg(me)
    out = []
    for m in WEEKLY_TASK_METRICS:
        bench = report.agg(report.bench_slice(metrics, m["bench_tier"], position))
        out.append({**m, "now": mine.get(m["col"]), "bench": bench.get(m["col"]),
                    "games": int(len(me)), "position": position})
    return out


SYSTEM_PROMPT = """당신은 한국 리그 오브 레전드 코치입니다. 상대는 KR 서버 BRONZE III 의
심은렬이고, 목표는 다이아몬드입니다. 아래 진단 리포트는 그의 솔로랭크 전적과 같은 서버
BRONZE/SILVER/GOLD 표본을 같은 방식으로 계산해 나란히 놓은 것입니다.

## 지금 상황
주 라인을 확정하지 않았습니다. 5개 라인을 정해진 순서로 하나씩, 각 라인마다 기준 챔프
2개로 15판씩, 총 75판을 돌려보며 어디가 맞는지 데이터로 고르는 "탐색 단계"입니다.
지금 어느 라인을 하고 있고 몇 판 남았는지는 리포트의 "탐색 단계 진행 현황"에 있습니다.

솔로랭크는 라인을 두 개 걸어야 해서 원하는 라인만 골라 할 수 없습니다. **부라인이
걸리면 그 라인의 기준 챔프로 하면 그 라인 진행 판수에 쌓입니다**(순서 위반으로
표시되지만 집계에는 들어갑니다). "탑에서만 하세요"라고 하지 말고, 부라인이 걸렸을
때 무엇을 고르라고 알려 주세요.

## 반드시 지킬 것

1. **존댓말로 씁니다.** 반말·명령조를 쓰지 마세요.
   좋은 예: "매 귀환마다 제어 와드를 1개 사세요." / "25분 이후에는 혼자 움직이지 마세요."
   나쁜 예: "매 귀환마다 제어 와드를 사라." / "혼자 움직이지 마라."
2. **탐색 75판이 끝나기 전에는 어떤 라인도 추천하지 마세요.** "정글이 맞다", "서폿은
   버려라" 같은 말을 하지 마세요. 지금까지의 숫자로 라인을 좁히려 들지 마세요.
   탐색이 안 끝났으면 남은 판수를 명시하고 라인 판단은 유보한다고 분명히 쓰세요.
3. **지금 탐색 중인 라인과 그 라인에 남은 판수를 반드시 명시하세요.**
4. **표본이 부족한 라인(15판 미만, 리포트에 ⚠ 표기)으로 결론을 내지 마세요.** 언급은
   하되 "표본 N판이라 아직 판단할 수 없습니다"를 반드시 덧붙이세요.
5. **_(추정)_ 이 붙은 지표는 근사값입니다.** 근거로 쓸 때는 추정임을 밝히고, 절대
   수치보다 벤치와의 차이를 근거로 삼으세요. (와드 위치·오브젝트 참여·첫 풀캠프·
   귀환이 여기 해당합니다. 계산 방식은 리포트 하단 주석에 있습니다.)
6. **모든 주장에 리포트의 실제 숫자를 붙이세요.** 리포트에 없는 수치를 지어내지 마세요.
   일반론("시야를 챙기세요")만 쓰지 말고 항상 "내 X vs GOLD 벤치 Y" 형태로 근거를 대세요.
7. 리그 오브 레전드를 아는 사람에게 말하듯 쓰세요. 용어를 풀어쓰지 마세요.
8. **이번 주 과제는 사용자가 이미 정해 놓았습니다.** 사용자 입력의 "[이번 주 과제]"
   블록에 있는 것을 그대로 쓰세요. 데이터상 다른 게 더 급해 보여도 과제를 바꾸지
   마세요. 정말 더 급한 것이 있다면 "주의"에 한 줄로만 적고, 과제 자체는 정해진 것을
   유지하세요.

## 출력 형식 (마크다운, 이 순서 그대로)

**한 줄 요약** — 지금 가장 중요한 것 한 문장.

**탐색 단계 진행 현황** — 전체 몇 판 중 몇 판을 했고, 지금 탐색 중인 라인이 어디이며
그 라인에 몇 판이 남았는지. 순서 위반이나 기준 챔프 위반이 있으면 지적하세요.
재미 점수가 기록되고 있는지도 봐 주세요. 탐색이 안 끝났으면 "라인 판단은 유보합니다"를
여기에 명시하세요.

**이번 주 과제** — 사용자 입력의 "[이번 주 과제]" 블록에 있는 과제를 씁니다.
**새로 고르거나 다른 것으로 바꾸지 마세요.** **딱 하나만** 씁니다. 반드시 포함할 것:
- 무엇을 할지 (정해진 과제를 한 문장, 존댓말로)
- **측정 가능한 목표치** — 사용자 입력의 "[이번 주 과제]" 아래에 목표 수치가
  주어집니다. **그 숫자를 그대로 쓰세요.** 화면에도 같은 숫자가 떠 있어서 다른
  값을 쓰면 어긋납니다.
- 다음 리포트에서 이 숫자로 확인하겠다는 명시

**라인전** — 리포트 4번(라인전) 표에서 **지금 탐색 중인 라인**만 봅니다.
15분 이전 데스와 10·15분 CS·골드 차이를 벤치와 비교해 한 문장으로 짚고,
그 숫자를 만드는 상황과 **구체적으로 무엇을 다르게 할지**를 한두 문장 씁니다.
죽은 위치가 리포트에 있으면 그것도 근거로 쓰세요.
여기서는 롤 지식으로 조언해도 됩니다 — 단, **진단의 근거 숫자는 반드시 리포트의
것**이어야 하고, 웨이브가 밀렸는지 당겨졌는지 같은 **측정하지 않은 것을 사실처럼
말하지 마세요**(리플레이가 없어 잴 수 없습니다).

**우선순위 (진단)** — 데이터로 본 문제 2개. 각각 두 문장 이내로, 내 수치 vs 벤치
수치를 붙여서. 1번은 위 "이번 주 과제"와 같은 항목입니다.
**2번에는 "다음 주 이후 후보"라고 명시하세요.** 지금 손대지 않습니다.

**주의** — 데이터로 말할 수 없는 것, 표본이 부족해 판단을 미룬 것을 솔직히 적으세요.

## 분량

**전체 1100자 이내(공백 포함).** 넘기지 마세요. 표를 쓰지 마세요. 서론·인사말 없이
바로 본문으로 시작하세요. 짧게 쓰기 위해 근거 수치를 빼지는 마세요 — 대신 설명을
줄이세요."""


def target_lines(conn, position: str | None) -> list[str]:
    """'지금 값 → 목표' 를 문장으로. 화면과 코치가 같은 숫자를 쓰게 하려는 것이다."""
    out = []
    for m in task_targets(conn, position):
        f = (lambda v: "—" if v is None else
             (f"{v * 100:.1f}%" if m["pct"] else f"{v:.2f}"))
        where = f"{position} {m['games']}판 기준" if position else "전체"
        out.append(f"{m['label']}: 지금 {f(m['now'])} → 목표 {f(m['target'])} "
                   f"({where}, {m['bench_tier']} 벤치 {f(m['bench'])})")
    return out


def build_user_content(report_md: str, summary: dict | None,
                       targets: list[str] | None = None) -> str:
    """리포트 전문 + 탐색 상태를 구조화해 넘긴다.

    탐색 상태는 리포트 안에도 있지만, 규칙(라인 추천 금지·현재 라인 명시)이 걸린
    값이라 표에서 읽어내게 두지 않고 따로 못박아 준다.
    """
    lines = []
    if summary:
        cur = summary.get("current_role")
        lines = [
            "[탐색 상태 — 아래 규칙 2·3번의 근거]",
            f"- 단계: {summary['phase_name']} (시작 {summary['start_date']})",
            f"- 전체 진행: {summary['done']}/{summary['target']}판, "
            f"남은 판수 {summary['target'] - summary['done']}판",
            f"- 75판 완료 여부: {'예' if summary['complete'] else '아니오 → 라인 추천 금지'}",
            f"- 탐색 순서: {' → '.join(summary['order'])}",
        ]
        if cur:
            lines += [
                f"- **지금 탐색 중인 라인: {cur}**",
                f"- 그 라인 진행: {summary['current_games']}판, "
                f"남은 판수 {summary['current_left']}판",
                f"- 그 라인 기준 챔프: {', '.join(summary['current_champs']) or '미지정'}",
            ]
        else:
            lines.append("- 지금 탐색 중인 라인: 없음(전 라인 목표 달성)")
        lines.append(f"- 순서 위반 누적: {summary.get('order_violations', 0)}판")
    head = ("\n".join(lines) + "\n\n") if lines else ""
    # 규칙 8번의 근거. 코치가 과제를 새로 고르지 못하게 값으로 못박는다.
    task = (
        "[이번 주 과제 — 사용자가 이미 정했습니다. 새로 고르지 마세요]\n"
        f"- {WEEKLY_TASK}\n"
        + "".join(f"- 목표: {m}\n" for m in (targets or []))
        + "\n"
    )
    return (
        "아래는 오늘 생성된 진단 리포트 전문입니다. 이걸 근거로 코치 메모를 써 주세요.\n"
        "리포트의 7번 '코치 메모' 섹션은 지금 채울 자리라 비어 있습니다.\n\n"
        f"{task}"
        f"{head}"
        "---- 리포트 시작 ----\n"
        f"{report_md}\n"
        "---- 리포트 끝 ----"
    )


def generate(report_md: str, summary: dict | None, model: str,
             api_key: str, targets: list[str] | None = None) -> tuple[str, int, int]:
    try:
        import anthropic
    except ImportError:
        raise SystemExit(
            "anthropic 패키지가 없습니다. 설치하세요:\n  pip install -r requirements.txt"
        )
    client = anthropic.Anthropic(api_key=api_key)
    user_content = build_user_content(report_md, summary, targets)
    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise SystemExit(f"모델이 응답을 거부했습니다: {message.stop_details}")
    text = "\n".join(b.text for b in message.content if b.type == "text").strip()
    if len(text) > MAX_CHARS * 1.2:
        print(f"  ! 메모가 {len(text)}자입니다 (상한 {MAX_CHARS}자). "
              f"프롬프트의 분량 지시를 조여야 할 수 있습니다.")
    return text, message.usage.input_tokens, message.usage.output_tokens


def save(conn, memo: str, model: str, in_tok: int, out_tok: int) -> None:
    upsert(conn, "coach_memo", {
        "created_at": now_iso(), "report_date": datetime.now().strftime("%Y-%m-%d"),
        "model": model, "memo": memo, "input_tokens": in_tok, "output_tokens": out_tok,
    }, ["created_at"])


def latest_memo(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM coach_memo ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="코치 메모 생성 (Claude API)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="API 호출 없이 프롬프트만 출력")
    ap.add_argument("--show", action="store_true", help="저장된 최신 메모 출력")
    ap.add_argument("--out", default=None, help="리포트 출력 경로")
    args = ap.parse_args(argv)

    st = config.load_settings(require_key=False)
    with session(st.db_path) as conn:
        if args.show:
            m = latest_memo(conn)
            if not m:
                print("저장된 코치 메모가 없습니다.")
                return 1
            print(f"[{m['created_at']} · {m['model']} · "
                  f"입력 {m['input_tokens']} / 출력 {m['output_tokens']} 토큰]\n")
            print(m["memo"])
            return 0

        # 메모 없는 상태의 리포트를 먼저 만들어 그걸 입력으로 준다
        report_md = report.build(conn, include_memo=False)
        summary = explore.summarize(conn)
        # 목표 수치는 화면과 같은 값을 써야 한다 — 코치가 새로 만들면 어긋난다
        targets = target_lines(conn, summary["current_role"] if summary else None)

        if args.dry_run:
            content = build_user_content(report_md, summary, targets)
            print("=" * 70)
            print("SYSTEM PROMPT")
            print("=" * 70)
            print(SYSTEM_PROMPT)
            print("\n" + "=" * 70)
            print(f"USER CONTENT ({len(content):,}자)")
            print("=" * 70)
            print(content[:2000] + ("\n… (생략)" if len(content) > 2000 else ""))
            print(f"\n모델 {args.model} / max_tokens {MAX_TOKENS} / adaptive thinking / "
              f"effort high / 분량 상한 {MAX_CHARS}자")
            print("--dry-run 이므로 API 를 호출하지 않았습니다.")
            return 0

        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            print("ANTHROPIC_API_KEY 가 없습니다. .env 에 아래 한 줄을 추가하세요:")
            print("    ANTHROPIC_API_KEY=sk-ant-...")
            print("  키 발급: https://console.anthropic.com/settings/keys")
            return 1

        print(f"코치 메모 생성 중… (모델 {args.model})")
        memo, in_tok, out_tok = generate(report_md, summary, args.model, api_key)
        save(conn, memo, args.model, in_tok, out_tok)
        conn.commit()
        print(f"  입력 {in_tok:,} 토큰 / 출력 {out_tok:,} 토큰 / 메모 {len(memo)}자")

        text = report.build(conn)
        out = args.out or str(config.DATA_DIR / f"report_{datetime.now():%Y%m%d}.md")
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"  리포트에 반영: {out}")
        print("\n" + "=" * 70)
        print(memo)
        print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
