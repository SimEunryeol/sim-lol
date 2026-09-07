"""Data Dragon 정적 데이터 (챔피언/아이템, 한글 이름 포함).

- 최신 버전 → champion.json / item.json (ko_KR) 을 받아 static_data 테이블에 캐시.
- 네트워크가 막혀 있으면 캐시만 쓰고, 캐시도 없으면 빈 값으로 우아하게 실패한다
  (한글 이름은 영문 championName 으로 대체, 코어템 판정은 None 이 된다).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import requests

from .db import now_iso

BASE = "https://ddragon.leagueoflegends.com"
CORE_ITEM_GOLD = 2900  # 이 값 이상이면 "코어템 완성"으로 본다
CONTROL_WARD_ID = 2055

_TIMEOUT = 20


class DDragonUnavailable(RuntimeError):
    pass


def _cache_get(conn: sqlite3.Connection, key: str) -> Any | None:
    row = conn.execute("SELECT json FROM static_data WHERE key = ?", (key,)).fetchone()
    return json.loads(row["json"]) if row else None


def _cache_put(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO static_data (key, json, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
        (key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now_iso()),
    )
    conn.commit()


def _fetch(url: str) -> Any:
    resp = requests.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def latest_version(conn: sqlite3.Connection, refresh: bool = False) -> str | None:
    if not refresh:
        cached = _cache_get(conn, "ddragon_version")
        if cached:
            return cached
    try:
        versions = _fetch(f"{BASE}/api/versions.json")
    except Exception:
        return _cache_get(conn, "ddragon_version")
    version = versions[0]
    _cache_put(conn, "ddragon_version", version)
    return version


def ensure(conn: sqlite3.Connection, locale: str = "ko_KR", refresh: bool = False) -> bool:
    """정적 데이터를 캐시에 채운다. 성공(또는 이미 캐시됨)하면 True."""
    have = _cache_get(conn, "champions") and _cache_get(conn, "items")
    if have and not refresh:
        return True
    version = latest_version(conn, refresh=refresh)
    if not version:
        return bool(have)
    try:
        champs = _fetch(f"{BASE}/cdn/{version}/data/{locale}/champion.json")
        items = _fetch(f"{BASE}/cdn/{version}/data/{locale}/item.json")
    except Exception as exc:  # 네트워크 차단 등
        print(f"  ! Data Dragon 을 받지 못했습니다 ({exc.__class__.__name__}): {exc}")
        return bool(have)
    _cache_put(conn, "champions", champs)
    _cache_put(conn, "items", items)
    return True


class Static:
    """캐시된 정적 데이터 조회 래퍼. 데이터가 없으면 전부 None/영문 폴백."""

    def __init__(self, conn: sqlite3.Connection):
        champs = _cache_get(conn, "champions") or {}
        items = _cache_get(conn, "items") or {}
        self.version = _cache_get(conn, "ddragon_version")
        self.available = bool(champs and items)

        self.champ_ko: dict[int, str] = {}
        self.champ_en: dict[int, str] = {}
        for entry in (champs.get("data") or {}).values():
            try:
                cid = int(entry["key"])
            except (KeyError, ValueError):
                continue
            self.champ_ko[cid] = entry.get("name") or entry.get("id")
            self.champ_en[cid] = entry.get("id")

        self.item_name: dict[int, str] = {}
        self.item_gold: dict[int, int] = {}
        for iid, entry in (items.get("data") or {}).items():
            try:
                key = int(iid)
            except ValueError:
                continue
            self.item_name[key] = entry.get("name", str(iid))
            self.item_gold[key] = int((entry.get("gold") or {}).get("total") or 0)

    def champion(self, champion_id: int | None, fallback: str | None = None) -> str:
        if champion_id is None:
            return fallback or "?"
        return self.champ_ko.get(int(champion_id)) or fallback or str(champion_id)

    def item(self, item_id: int | None) -> str:
        if item_id is None:
            return "?"
        return self.item_name.get(int(item_id), str(item_id))

    def item_cost(self, item_id: int | None) -> int | None:
        if item_id is None or not self.available:
            return None
        return self.item_gold.get(int(item_id))

    def is_core_item(self, item_id: int | None) -> bool | None:
        cost = self.item_cost(item_id)
        if cost is None:
            return None
        return cost >= CORE_ITEM_GOLD
