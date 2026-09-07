"""환경설정 로딩과 상수 정의."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

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


def load_settings(require_key: bool = True) -> Settings:
    load_dotenv(ROOT / ".env")
    key = os.getenv("RIOT_API_KEY", "").strip()
    if require_key and not key:
        raise SystemExit(
            "RIOT_API_KEY 가 없습니다. .env.example 을 .env 로 복사하고 키를 넣어주세요.\n"
            "  cp .env.example .env && ${EDITOR:-vi} .env"
        )
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
