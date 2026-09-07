"""레이트리밋 / 재시도 / SQLite 캐시를 포함한 Riot API 클라이언트."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence
from urllib.parse import urlencode

import requests

from . import config
from .db import now_iso


STATUS_HINTS = {
    401: "API 키가 거부됐습니다. 개발용 키는 24시간마다 만료됩니다 — "
         "developer.riotgames.com 에서 새 키를 받아 .env 에 넣으세요.",
    403: "API 키가 만료됐거나 잘못됐습니다 — developer.riotgames.com 에서 새 키를 받으세요.",
    404: "찾을 수 없습니다. .env 의 RIOT_ID / RIOT_TAG 를 확인하세요.",
    429: "요청이 너무 잦습니다. 잠시 뒤 다시 실행하면 캐시된 데이터부터 이어서 진행합니다.",
    503: "라이엇 서버가 일시적으로 응답하지 않습니다. 잠시 뒤 다시 실행하세요.",
}


class RiotError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} {url} :: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body

    @property
    def hint(self) -> str:
        return STATUS_HINTS.get(self.status, "예상 밖 응답입니다. 위 본문을 그대로 공유해 주세요.")

    def report(self) -> str:
        return (
            f"\n{'=' * 66}\n"
            f"라이엇 API 요청이 실패했습니다.\n"
            f"  요청 : {self.url}\n"
            f"  응답 : HTTP {self.status}\n"
            f"  본문 : {self.body[:300]}\n\n"
            f"  {self.hint}\n\n"
            f"  자세히 보려면: python -m sim_diamond.doctor\n"
            f"{'=' * 66}"
        )


class NotFound(RiotError):
    pass


class RateLimiter:
    """여러 개의 (횟수, 초) 윈도우를 동시에 지키는 슬라이딩 윈도우 리미터."""

    def __init__(self, limits: Sequence[tuple[int, float]], safety: float = config.RATE_SAFETY):
        self._windows = [
            (max(1, int(math.floor(count * safety))), span, deque())
            for count, span in limits
        ]
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                wait = 0.0
                for count, span, hits in self._windows:
                    while hits and now - hits[0] >= span:
                        hits.popleft()
                    if len(hits) >= count:
                        wait = max(wait, span - (now - hits[0]) + 0.02)
                if wait <= 0:
                    for _, _, hits in self._windows:
                        hits.append(now)
                    return
            time.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """429 를 맞았을 때 모든 윈도우를 seconds 만큼 막는다."""
        with self._lock:
            now = time.monotonic()
            for count, span, hits in self._windows:
                hits.clear()
                # span 이 지나야 풀리도록 가짜 히트를 채운다
                fake = now - span + seconds
                for _ in range(count):
                    hits.append(fake)


@dataclass
class Stats:
    calls: int = 0
    cache_hits: int = 0
    retries_429: int = 0
    retries_5xx: int = 0
    errors: int = 0
    handled: int = 0   # allow_error=True 로 의도적으로 받아넘긴 4xx/5xx

    def summary(self) -> str:
        return (
            f"API 호출 {self.calls}회 / 캐시 히트 {self.cache_hits}회 / "
            f"429 재시도 {self.retries_429}회 / 5xx 재시도 {self.retries_5xx}회 / "
            f"실패 {self.errors}회" + (f" / 예상된 오류 {self.handled}회" if self.handled else "")
        )


class RiotClient:
    """모든 호출은 SQLite 캐시를 먼저 본다. 캐시에 있으면 네트워크를 타지 않는다."""

    def __init__(self, api_key: str, conn: sqlite3.Connection, verbose: bool = True):
        self.api_key = api_key
        self.conn = conn
        self.verbose = verbose
        self.limiter = RateLimiter(config.RATE_LIMITS)
        self.stats = Stats()
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Riot-Token": api_key, "Accept": "application/json",
             "User-Agent": "sim-diamond/0.1"}
        )

    # ------------------------------------------------------------ 캐시 --
    @staticmethod
    def cache_key(host: str, path: str, params: dict | None) -> str:
        q = urlencode(sorted((params or {}).items()))
        return f"{host}{path}" + (f"?{q}" if q else "")

    def _cache_get(self, key: str, ttl: int | None) -> Any | None:
        row = self.conn.execute(
            "SELECT status, json, fetched_at FROM api_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if ttl is not None:
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(row["fetched_at"])
            ).total_seconds()
            if age > ttl:
                return None
        return json.loads(row["json"])

    def _cache_put(self, key: str, status: int, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO api_cache (cache_key, status, json, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "status=excluded.status, json=excluded.json, fetched_at=excluded.fetched_at",
            (key, status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now_iso()),
        )
        self.conn.commit()

    # ------------------------------------------------------------- HTTP --
    def get(
        self,
        path: str,
        host: str = config.REGIONAL,
        params: dict | None = None,
        ttl: int | None = config.TTL_IMMUTABLE,
        use_cache: bool = True,
        allow_error: bool = False,
    ) -> Any:
        """GET 후 JSON 반환. allow_error=True 면 4xx/5xx 도 예외 대신 dict 로 돌려준다."""
        key = self.cache_key(host, path, params)
        if use_cache:
            hit = self._cache_get(key, ttl)
            if hit is not None:
                self.stats.cache_hits += 1
                return hit

        url = f"{host}{path}"
        backoff = 1.0
        n_429 = n_5xx = 0
        while True:
            self.limiter.acquire()
            self.stats.calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=20)
            except requests.RequestException as exc:
                n_5xx += 1
                self.stats.retries_5xx += 1
                if n_5xx > config.MAX_5XX_RETRIES:
                    self.stats.errors += 1
                    raise RiotError(0, url, f"네트워크 오류: {exc}") from exc
                self._log(f"  네트워크 오류({exc.__class__.__name__}) → {backoff:.0f}s 후 재시도")
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 200:
                payload = resp.json()
                if use_cache:
                    self._cache_put(key, 200, payload)
                return payload

            if resp.status_code == 429:
                n_429 += 1
                self.stats.retries_429 += 1
                retry_after = float(resp.headers.get("Retry-After", "1") or 1)
                limit_type = resp.headers.get("X-Rate-Limit-Type", "?")
                if n_429 > config.MAX_429_RETRIES:
                    self.stats.errors += 1
                    raise RiotError(429, url, "429 재시도 한도 초과")
                self._log(f"  429({limit_type}) → Retry-After {retry_after:.0f}s 대기")
                self.limiter.penalize(retry_after)
                time.sleep(retry_after + 0.2)
                continue

            if 500 <= resp.status_code < 600:
                n_5xx += 1
                self.stats.retries_5xx += 1
                if n_5xx > config.MAX_5XX_RETRIES:
                    if allow_error:
                        self.stats.handled += 1
                        return self._error_payload(resp)
                    self.stats.errors += 1
                    raise RiotError(resp.status_code, url, resp.text)
                self._log(f"  {resp.status_code} → {backoff:.0f}s 후 재시도 ({n_5xx}/{config.MAX_5XX_RETRIES})")
                time.sleep(backoff)
                backoff *= 2
                continue

            # 4xx
            if allow_error:
                self.stats.handled += 1
                payload = self._error_payload(resp)
                if use_cache:
                    self._cache_put(key, resp.status_code, payload)
                return payload
            self.stats.errors += 1
            if resp.status_code == 404:
                raise NotFound(404, url, resp.text)
            raise RiotError(resp.status_code, url, resp.text)

    @staticmethod
    def _error_payload(resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:2000]
        return {"_error": True, "_status": resp.status_code, "_body": body}

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # -------------------------------------------------------- 엔드포인트 --
    def account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        return self.get(
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}",
            host=config.REGIONAL,
            ttl=config.TTL_MEDIUM,
        )

    def league_entries_by_puuid(self, puuid: str) -> list[dict]:
        return self.get(
            f"/lol/league/v4/entries/by-puuid/{puuid}",
            host=config.PLATFORM,
            ttl=config.TTL_MEDIUM,
        )

    def league_entries(self, tier: str, division: str, page: int = 1, queue: str = "RANKED_SOLO_5x5") -> list[dict]:
        return self.get(
            f"/lol/league/v4/entries/{queue}/{tier}/{division}",
            host=config.PLATFORM,
            params={"page": page},
            ttl=config.TTL_MEDIUM,
        )

    def match_ids(
        self,
        puuid: str,
        start: int = 0,
        count: int = 100,
        queue: int | None = config.SOLO_QUEUE_ID,
        start_time: int | None = None,
    ) -> list[str]:
        params: dict[str, Any] = {"start": start, "count": count}
        if queue is not None:
            params["queue"] = queue
        if start_time is not None:
            params["startTime"] = start_time  # epoch seconds
        return self.get(
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
            host=config.REGIONAL,
            params=params,
            ttl=config.TTL_SHORT,
        )

    def match(self, match_id: str) -> dict:
        return self.get(f"/lol/match/v5/matches/{match_id}", host=config.REGIONAL)

    def timeline(self, match_id: str) -> dict:
        return self.get(f"/lol/match/v5/matches/{match_id}/timeline", host=config.REGIONAL)

    def mastery_top(self, puuid: str, count: int = 20) -> list[dict]:
        return self.get(
            f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top",
            host=config.PLATFORM,
            params={"count": count},
            ttl=config.TTL_MEDIUM,
        )
