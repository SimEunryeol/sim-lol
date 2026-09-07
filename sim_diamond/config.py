"""환경설정 로딩과 상수 정의."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# 윈도우 콘솔(cp949)에서 표현 못 하는 글자가 나와도 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# 라우팅
REGIONAL = "https://asia.api.riotgames.com"      # Account-V1, Match-V5
PLATFORM = "https://kr.api.riotgames.com"        # Summoner-V4, League-V4, Champion-Mastery-V4

KST = timezone(timedelta(hours=9))

SOLO_QUEUE_ID = 420
DEFAULT_SEASON_START_KST = "2026-01-01T00:00:00"

# 레이트리밋: 20 req / 1s, 100 req / 120s (개발용 키 기준)
RATE_LIMITS = ((20, 1.0), (100, 120.0))
# 시계 오차/동시성 여유. 실제 사용 한도 = ceil(limit * SAFETY)
RATE_SAFETY = 0.9

# 재시도
MAX_5XX_RETRIES = 3
MAX_429_RETRIES = 5

# 캐시 TTL(초). None = 영구(불변 데이터)
TTL_IMMUTABLE = None
TTL_SHORT = 60 * 30          # 매치 ID 목록 등
TTL_MEDIUM = 60 * 60 * 6     # 티어, 숙련도 등


@dataclass(frozen=True)
class Settings:
    api_key: str
    riot_id: str
    riot_tag: str
    db_path: Path
    season_start_kst: datetime

    @property
    def season_start_ms(self) -> int:
        return int(self.season_start_kst.timestamp() * 1000)

    @property
    def season_start_epoch_s(self) -> int:
        return int(self.season_start_kst.timestamp())


PLACEHOLDER_KEY = "RGAPI-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


def _env_help() -> str:
    """OS 에 맞는 .env 만들기 안내. 윈도우의 '.env.txt' 함정까지 잡아준다."""
    env_path = ROOT / ".env"
    win = os.name == "nt"
    out = [f"RIOT_API_KEY 가 없습니다.  찾은 경로: {env_path}"]

    strays = sorted(
        p.name for p in ROOT.glob(".env*")
        if p.name not in (".env", ".env.example") and p.is_file()
    )
    if strays:
        out.append("")
        out.append(f"  ! 비슷한 이름의 파일이 있습니다: {', '.join(strays)}")
        out.append("    메모장이 확장자 .txt 를 몰래 붙였을 수 있습니다. 이름을 정확히 '.env' 로 바꾸세요:")
        out.append(f"      ren \"{ROOT / strays[0]}\" .env" if win else f"      mv {strays[0]} .env")

    if not env_path.exists():
        out.append("")
        out.append("  .env 파일이 아직 없습니다. 아래를 그대로 복사해 실행하세요:")
        if win:
            out.append("      copy .env.example .env")
            out.append("      notepad .env")
            out.append("    (PowerShell 이면 copy 대신: Copy-Item .env.example .env)")
        else:
            out.append("      cp .env.example .env")
            out.append("      ${EDITOR:-vi} .env")
    else:
        out.append("")
        out.append("  .env 는 있는데 RIOT_API_KEY 값이 비어 있거나 예시 그대로입니다.")
        out.append("  파일 안에 아래 한 줄이 있어야 합니다 (따옴표·공백 없이):")
        out.append("      RIOT_API_KEY=RGAPI-여기에-발급받은-키")

    out.append("")
    out.append("  키 발급: https://developer.riotgames.com 로그인 → DEVELOPMENT API KEY (24시간마다 만료)")
    return "\n".join(out)


def load_settings(require_key: bool = True) -> Settings:
    load_dotenv(ROOT / ".env")
    key = os.getenv("RIOT_API_KEY", "").strip().strip("\"'")
    if key == PLACEHOLDER_KEY:
        key = ""
    if require_key and not key:
        raise SystemExit(_env_help())
    if require_key and not key.startswith("RGAPI-"):
        print(f"  ! RIOT_API_KEY 가 'RGAPI-' 로 시작하지 않습니다 (앞 8자: {key[:8]}…). 잘못 붙여넣지 않았는지 확인하세요.")
    raw_start = os.getenv("SIM_SEASON_START_KST", DEFAULT_SEASON_START_KST)
    start = datetime.fromisoformat(raw_start).replace(tzinfo=KST)
    db_path = Path(os.getenv("SIM_DB_PATH", str(DATA_DIR / "sim.db")))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        api_key=key,
        riot_id=os.getenv("RIOT_ID", "F360").strip(),
        riot_tag=os.getenv("RIOT_TAG", "KR1").strip(),
        db_path=db_path,
        season_start_kst=start,
    )
