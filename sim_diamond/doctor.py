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


def _describe_chars(key: str) -> list[str]:
    problems = []
    if key != key.strip():
        problems.append("앞뒤에 공백/개행이 붙어 있습니다")
    if any(c in key for c in "\"'"):
        problems.append("따옴표가 섞여 있습니다 (.env 에는 따옴표 없이 써야 합니다)")
    if " " in key.strip():
        problems.append("중간에 공백이 있습니다")
    weird = sorted({c for c in key if not (c.isalnum() or c in "-_")})
    if weird:
        problems.append(f"예상 밖 문자: {weird!r}")
    return problems


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


def check_key() -> str:
    print("\n[2] 실제로 읽어들인 값")
    st = config.load_settings(require_key=False)
    key = os.getenv("RIOT_API_KEY", "").strip().strip("\"'")
    print(f"    RIOT_ID  : {st.riot_id!r}")
    print(f"    RIOT_TAG : {st.riot_tag!r}")
    print(f"    키       : {_mask(key)}  (길이 {len(key)}자)")

    if not key:
        print("    진단     : 키를 못 읽었습니다.")
        return key
    if key == config.PLACEHOLDER_KEY:
        print("    진단     : .env.example 의 예시 키 그대로입니다. 실제 키로 바꾸지 않았습니다. ★")
        return key
    ok = []
    if key.startswith("RGAPI-"):
        ok.append("RGAPI- 로 시작 OK")
    else:
        ok.append(f"! 'RGAPI-' 로 시작하지 않음 (앞 6자 {key[:6]!r})")
    if len(key) == EXPECTED_LEN:
        ok.append(f"길이 {EXPECTED_LEN} OK")
    else:
        ok.append(f"! 길이 {len(key)} (개발용 키는 보통 {EXPECTED_LEN}자)")
    print(f"    형식     : {' / '.join(ok)}")
    for p in _describe_chars(key):
        print(f"    !        : {p}")
    return key


DIAGNOSIS = {
    401: "키가 전송되지 않았거나 라이엇이 거부했습니다. 개발용 키는 24시간마다 만료됩니다 — "
         "developer.riotgames.com 에서 REGENERATE API KEY 를 눌러 새 키를 받아 .env 에 다시 넣으세요. ★",
    403: "키가 만료되었거나 잘못되었습니다. developer.riotgames.com 에서 새 키를 받으세요. ★",
    404: "키는 정상입니다. 다만 이 Riot ID 를 찾을 수 없습니다 — .env 의 RIOT_ID / RIOT_TAG 를 확인하세요.",
    429: "키는 정상입니다. 요청이 너무 잦아 잠시 막힌 것뿐이니 조금 뒤 다시 시도하세요.",
    503: "키는 정상입니다. 라이엇 서버가 일시적으로 응답하지 않습니다.",
}


def live_call(key: str) -> int:
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
    print(f"\n    진단: {DIAGNOSIS.get(resp.status_code, '예상 밖 응답입니다. 위 본문을 그대로 공유해 주세요.')}")
    return 1


def main() -> int:
    print("=" * 66)
    print("sim-diamond 설정 진단")
    print(f"  파이썬 {sys.version.split()[0]} · 작업 폴더 {config.ROOT}")
    print("=" * 66)
    check_env_file()
    key = check_key()
    if not key:
        print("\n키가 없어 호출 테스트를 건너뜁니다.")
        return 1
    if key == config.PLACEHOLDER_KEY:
        print("\n예시 키라 호출 테스트를 건너뜁니다. .env 를 열어 실제 키로 바꾸세요:")
        print(f"    notepad {config.ROOT / '.env'}" if __import__('os').name == "nt"
              else f"    ${{EDITOR:-vi}} {config.ROOT / '.env'}")
        return 1
    return live_call(key)


if __name__ == "__main__":
    sys.exit(main())
