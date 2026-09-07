"""설정 진단: .env 를 어떻게 읽었고 Riot 이 실제로 뭐라고 답하는지 그대로 보여준다.

  python -m sim_diamond.doctor

키 전체는 절대 출력하지 않는다(앞 9자 / 뒤 4자만).
"""
from __future__ import annotations

import os
import sys

import requests

from . import config

EXPECTED_LEN = 42  # "RGAPI-" + UUID(36)
MASK_HEAD, MASK_TAIL = 9, 4


def _mask(key: str) -> str:
    if not key:
        return "(빈 값)"
    if len(key) <= MASK_HEAD + MASK_TAIL:
        return key[0] + "*" * (len(key) - 1)
    return f"{key[:MASK_HEAD]}…{'*' * 6}…{key[-MASK_TAIL:]}"


PREFIX = "RGAPI-"


def _describe_chars(key: str) -> list[str]:
    """키 문자열 자체의 문제를 찾는다. 붙여넣기 사고가 대부분이다."""
    problems = []
    if key.startswith(PREFIX * 2):
        problems.append(
            f"'{PREFIX}' 가 두 번 들어가 있습니다 — .env 에 이미 있던 {PREFIX} 뒤에 "
            f"{PREFIX} 로 시작하는 키를 그대로 붙여넣으면 이렇게 됩니다. "
            f"앞쪽 {PREFIX} 하나를 지우세요. ★"
        )
    elif key.count(PREFIX) > 1:
        problems.append(f"'{PREFIX}' 가 {key.count(PREFIX)}번 나옵니다 — 키가 두 번 붙여넣어졌을 수 있습니다. ★")
    if key != key.strip():
        problems.append("앞뒤에 공백/개행이 붙어 있습니다")
    if any(c in key for c in "\"'"):
        problems.append("따옴표가 섞여 있습니다 (.env 에는 따옴표 없이 써야 합니다)")
    if " " in key.strip():
        problems.append("중간에 공백이 있습니다")
    if key.startswith(PREFIX) and len(key) != EXPECTED_LEN and PREFIX * 2 not in key:
        diff = len(key) - EXPECTED_LEN
        problems.append(
            f"길이가 {EXPECTED_LEN}자가 아닙니다 ({'+' if diff > 0 else ''}{diff}자) — "
            + ("뒤에 뭔가 더 붙었거나 두 번 붙여넣었는지 확인하세요."
               if diff > 0 else "복사할 때 일부가 잘렸는지 확인하세요.")
        )
    weird = sorted({c for c in key if not (c.isalnum() or c in "-_")})
    if weird:
        problems.append(f"예상 밖 문자: {weird!r}")
    return problems


def suggest_fix(key: str) -> str | None:
    """고칠 수 있는 형태면 고친 값을 (마스킹해서) 제안한다."""
    fixed = key.strip().strip("\"'").replace(" ", "")
    while fixed.startswith(PREFIX * 2):
        fixed = fixed[len(PREFIX):]
    if fixed != key and fixed.startswith(PREFIX):
        return fixed
    return None


def check_example_file() -> None:
    """`.env.example` 은 git 이 관리하는 템플릿이다. 여기에 진짜 키를 넣으면
    (1) pull 이 막히고 (2) 실수로 커밋하면 키가 공개된다."""
    path = config.ROOT / ".env.example"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    real = [line.split("=", 1)[0].strip() for line in text.splitlines()
            if "=" in line and not line.strip().startswith("#")
            and line.split("=", 1)[1].strip() not in ("", config.PLACEHOLDER_KEY)
            and not line.split("=", 1)[1].strip().startswith(("RGAPI-xxx", "sk-ant-xxx"))
            and line.split("=", 1)[0].strip().endswith(("API_KEY", "KEY"))]
    if real:
        print("\n  ★ .env.example 에 실제 값이 들어 있습니다: " + ", ".join(real))
        print("    .env.example 은 git 이 관리하는 '빈 템플릿'입니다. 진짜 키는 .env 에만 넣으세요.")
        print("    이대로 두면 git pull 이 막히고, 실수로 커밋하면 키가 공개됩니다.")
        print(f"    되돌리기:  copy \"{path}\" \"{path}.bak\"  후  git checkout -- .env.example"
              if os.name == "nt" else
              f"    되돌리기:  cp {path} {path}.bak && git checkout -- .env.example")


def check_env_file() -> str | None:
    path = config.ROOT / ".env"
    print("[1] .env 파일")
    print(f"    경로 : {path}")
    if not path.exists():
        print("    존재 : 아니오  ← 이것부터 만들어야 합니다")
        strays = sorted(p.name for p in config.ROOT.glob(".env*")
                        if p.name not in (".env", ".env.example") and p.is_file())
        if strays:
            print(f"    ! 비슷한 파일: {', '.join(strays)}  (메모장이 .txt 를 붙였을 수 있음)")
        return None

    data = path.read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    print(f"    존재 : 예 ({len(data)} bytes){'  ! UTF-8 BOM 있음' if bom else ''}")

    text = data.decode("utf-8-sig", errors="replace")
    raw_line = None
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("RIOT_API_KEY"):
            raw_line = line
            head = line.split("=", 1)[1] if "=" in line else ""
            print(f"    RIOT_API_KEY 줄 : {i}번째 줄, '=' 뒤 길이 {len(head)}자")
            break
    if raw_line is None:
        print("    RIOT_API_KEY 줄 : 없음  ← 파일 안에 이 줄이 있어야 합니다")
    return raw_line


def check_key() -> tuple[str, list[str]]:
    print("\n[2] 실제로 읽어들인 값")
    st = config.load_settings(require_key=False)
    key = os.getenv("RIOT_API_KEY", "").strip().strip("\"'")
    print(f"    RIOT_ID  : {st.riot_id!r}")
    print(f"    RIOT_TAG : {st.riot_tag!r}")
    print(f"    키       : {_mask(key)}  (길이 {len(key)}자)")

    if not key:
        print("    진단     : 키를 못 읽었습니다.")
        return key, ["키 없음"]
    if key == config.PLACEHOLDER_KEY:
        print("    진단     : .env.example 의 예시 키 그대로입니다. 실제 키로 바꾸지 않았습니다. ★")
        return key, ["예시 키"]

    ok = []
    if key.startswith(PREFIX):
        ok.append(f"{PREFIX} 로 시작 OK")
    else:
        ok.append(f"! '{PREFIX}' 로 시작하지 않음 (앞 6자 {key[:6]!r})")
    ok.append(f"길이 {EXPECTED_LEN} OK" if len(key) == EXPECTED_LEN else f"! 길이 {len(key)}")
    print(f"    형식     : {' / '.join(ok)}")

    problems = _describe_chars(key)
    if not key.startswith(PREFIX):
        problems.append(f"'{PREFIX}' 로 시작하지 않습니다")
    for item in problems:
        print(f"    !        : {item}")
    fix = suggest_fix(key)
    if fix:
        print(f"    고치면   : {_mask(fix)}  (길이 {len(fix)}자)"
              f"{'  ← 이 형태가 맞습니다' if len(fix) == EXPECTED_LEN else ''}")
    return key, problems


DIAGNOSIS = {
    401: "키가 전송되지 않았거나 라이엇이 거부했습니다. 개발용 키는 24시간마다 만료됩니다 — "
         "developer.riotgames.com 에서 REGENERATE API KEY 를 눌러 새 키를 받아 .env 에 다시 넣으세요. ★",
    403: "키가 만료되었거나 잘못되었습니다. developer.riotgames.com 에서 새 키를 받으세요. ★",
    404: "키는 정상입니다. 다만 이 Riot ID 를 찾을 수 없습니다 — .env 의 RIOT_ID / RIOT_TAG 를 확인하세요.",
    429: "키는 정상입니다. 요청이 너무 잦아 잠시 막힌 것뿐이니 조금 뒤 다시 시도하세요.",
    503: "키는 정상입니다. 라이엇 서버가 일시적으로 응답하지 않습니다.",
}


def live_call(key: str, problems: list[str] | None = None) -> int:
    url = (f"{config.REGIONAL}/riot/account/v1/accounts/by-riot-id/"
           f"{os.getenv('RIOT_ID', 'F360')}/{os.getenv('RIOT_TAG', 'KR1')}")
    print("\n[3] 실제 호출 (캐시 안 씀)")
    print(f"    GET {url}")
    print(f"    헤더 X-Riot-Token: {_mask(key)}")
    try:
        resp = requests.get(url, headers={"X-Riot-Token": key}, timeout=20)
    except requests.RequestException as exc:
        print(f"    → 네트워크 오류: {exc.__class__.__name__}: {exc}")
        print("    진단: 인터넷/방화벽/프록시 문제입니다. 키 문제가 아닙니다.")
        return 1

    print(f"    → HTTP {resp.status_code}")
    print(f"    응답 본문: {resp.text[:300]}")
    interesting = {k: v for k, v in resp.headers.items()
                   if k.lower().startswith(("x-app-rate", "x-method-rate", "retry-after", "x-rate"))}
    if interesting:
        print(f"    응답 헤더: {interesting}")

    if resp.status_code == 200:
        body = resp.json()
        print(f"\n    ✅ 정상입니다. gameName={body.get('gameName')} tagLine={body.get('tagLine')} "
              f"puuid={str(body.get('puuid'))[:16]}…")
        print("    이제 python -m sim_diamond.collect_me --limit 20 을 실행하세요.")
        return 0
    if problems and resp.status_code in (401, 403):
        # 형식이 깨져 있으면 만료가 아니라 그것부터가 원인이다.
        print("\n    진단: 키 형식이 잘못됐습니다. 만료 문제가 아니라 아래부터 고치세요 —")
        for item in problems:
            print(f"           · {item}")
        path = config.ROOT / ".env"
        print(f"\n           {'notepad ' if os.name == 'nt' else '${EDITOR:-vi} '}{path}")
        return 1
    print(f"\n    진단: {DIAGNOSIS.get(resp.status_code, '예상 밖 응답입니다. 위 본문을 그대로 공유해 주세요.')}")
    return 1


def main() -> int:
    print("=" * 66)
    print("sim-diamond 설정 진단")
    print(f"  파이썬 {sys.version.split()[0]} · 작업 폴더 {config.ROOT}")
    print("=" * 66)
    check_env_file()
    check_example_file()
    key, problems = check_key()
    if not key:
        print("\n키가 없어 호출 테스트를 건너뜁니다.")
        return 1
    if key == config.PLACEHOLDER_KEY:
        print("\n예시 키라 호출 테스트를 건너뜁니다. .env 를 열어 실제 키로 바꾸세요:")
        print(f"    notepad {config.ROOT / '.env'}" if __import__('os').name == "nt"
              else f"    ${{EDITOR:-vi}} {config.ROOT / '.env'}")
        return 1
    return live_call(key, problems)


if __name__ == "__main__":
    sys.exit(main())
