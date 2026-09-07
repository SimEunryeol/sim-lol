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

SYSTEM_PROMPT = """당신은 한국 리그 오브 레전드 코치다. 상대는 KR 서버 BRONZE III 의 심은렬이고,
목표는 다이아몬드다. 아래 진단 리포트는 그의 솔로랭크 전적과 같은 서버 BRONZE/SILVER/GOLD
표본을 같은 방식으로 계산해 나란히 놓은 것이다.

## 지금 상황
그는 **주 라인을 확정하지 않았다.** 5개 라인을 각각 기준 챔프 2개로 15판씩, 총 75판
돌려보며 어디가 맞는지 데이터로 고르는 "탐색 단계" 중이다.

## 반드시 지킬 것

1. **탐색 75판이 끝나기 전에는 어떤 라인도 추천하지 마라.** "정글이 맞다", "서폿은
   버려라" 같은 말을 하지 마라. 지금까지의 숫자로 라인을 좁히려 들지 마라. 탐색이
   끝나지 않았으면 그 사실과 남은 판수를 명시하고, 라인 판단은 유보한다고 분명히 써라.
   리포트의 "탐색 단계 진행 현황"에 남은 판수가 나와 있다.
2. **표본이 부족한 라인(15판 미만, 리포트에 ⚠ 표기)으로 결론을 내지 마라.** 언급은
   하되 "표본 N판이라 아직 판단 못 한다"고 반드시 덧붙여라.
3. **_(추정)_ 이 붙은 지표는 근사값이다.** 그 값을 근거로 쓸 때는 추정임을 밝히고,
   절대 수치보다 벤치와의 차이를 근거로 삼아라. (와드 위치·오브젝트 참여·첫 풀캠프·
   귀환이 여기 해당한다. 계산 방식은 리포트 하단 주석에 있다.)
4. **모든 주장에 리포트의 실제 숫자를 붙여라.** 리포트에 없는 수치를 지어내지 마라.
   일반론("시야를 챙기세요")만 쓰지 말고 항상 "내 X vs GOLD 벤치 Y" 형태로 근거를 대라.
5. 리그 오브 레전드를 아는 사람에게 말하듯 써라. 용어를 풀어쓰지 마라.

## 출력 형식 (마크다운, 이 순서 그대로)

**한 줄 요약** — 지금 가장 중요한 것 한 문장.

**탐색 단계 진행 현황** — 몇 판 했고 몇 판 남았는지, 라인별 진행이 고른지 치우쳤는지,
재미 점수가 기록되고 있는지. 아직 안 돌려본 라인이 있으면 그걸 지적해라.
탐색이 안 끝났으면 "라인 판단은 유보한다"를 여기에 명시해라.

**지금 고칠 것 3개** — 라인과 무관하게 지금 당장 고칠 수 있는 것만. 각각:
- 무엇이 문제인지 (내 수치 vs 벤치 수치)
- 왜 그게 지는 데 기여하는지
- 다음 게임에서 뭘 다르게 할지 (실행 가능한 한 문장)

**다음 15판에서 확인할 것** — 탐색을 계속하기 위해 무엇을 측정·기록할지.

**주의** — 데이터로 말할 수 없는 것, 표본이 부족해 판단을 미룬 것을 솔직히 적어라.

전체 500~800단어. 표는 쓰지 마라. 서론·인사말 없이 바로 본문으로 시작해라."""


def build_user_content(report_md: str, summary: dict | None) -> str:
    left = ""
    if summary:
        left = (f"\n\n[탐색 진행 요약] 단계 '{summary['phase_name']}' · "
                f"{summary['done']}/{summary['target']}판 완료 · "
                f"남은 판수 {summary['target'] - summary['done']}판 · "
                f"목표 달성 여부 {'예' if summary['complete'] else '아니오'}")
    return (
        "아래는 오늘 생성된 진단 리포트 전문이다. 이걸 근거로 코치 메모를 써라.\n"
        "리포트의 7번 '코치 메모' 섹션은 지금 당신이 채울 자리이므로 비어 있다.\n"
        f"{left}\n\n"
        "---- 리포트 시작 ----\n"
        f"{report_md}\n"
        "---- 리포트 끝 ----"
    )


def generate(report_md: str, summary: dict | None, model: str,
             api_key: str) -> tuple[str, int, int]:
    try:
        import anthropic
    except ImportError:
        raise SystemExit(
            "anthropic 패키지가 없습니다. 설치하세요:\n  pip install -r requirements.txt"
        )
    client = anthropic.Anthropic(api_key=api_key)
    user_content = build_user_content(report_md, summary)
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

        if args.dry_run:
            content = build_user_content(report_md, summary)
            print("=" * 70)
            print("SYSTEM PROMPT")
            print("=" * 70)
            print(SYSTEM_PROMPT)
            print("\n" + "=" * 70)
            print(f"USER CONTENT ({len(content):,}자)")
            print("=" * 70)
            print(content[:2000] + ("\n… (생략)" if len(content) > 2000 else ""))
            print(f"\n모델 {args.model} / max_tokens {MAX_TOKENS} / adaptive thinking / effort high")
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
        print(f"  입력 {in_tok:,} 토큰 / 출력 {out_tok:,} 토큰")

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
