"""SQLite 스키마 생성과 upsert 헬퍼.

raw 테이블(matches_raw / timelines_raw / api_cache)은 원본 보존용이고,
나머지 테이블은 전부 raw 만 보고 재생성할 수 있는 파생 데이터다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS players (
    puuid       TEXT PRIMARY KEY,
    game_name   TEXT,
    tag_line    TEXT,
    tier        TEXT,
    rank        TEXT,
    lp          INTEGER,
    is_me       INTEGER NOT NULL DEFAULT 0,
    is_bench    INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT
);

-- 원본 보존 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS matches_raw (
    match_id    TEXT PRIMARY KEY,
    json        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timelines_raw (
    match_id    TEXT PRIMARY KEY,
    json        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

-- 그 외 모든 API 응답 캐시 (account / league / mastery / match-id 목록 등)
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key   TEXT PRIMARY KEY,
    status      INTEGER NOT NULL,
    json        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

-- 파싱 결과 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS matches (
    match_id    TEXT PRIMARY KEY,
    game_start  INTEGER,      -- epoch ms
    duration_s  INTEGER,
    patch       TEXT,
    queue_id    INTEGER
);

CREATE TABLE IF NOT EXISTS participants (
    match_id             TEXT NOT NULL,
    puuid                TEXT NOT NULL,
    participant_id       INTEGER,
    team_id              INTEGER,
    champion_id          INTEGER,
    champion_name        TEXT,
    team_position        TEXT,
    win                  INTEGER,
    kills                INTEGER,
    deaths               INTEGER,
    assists              INTEGER,
    cs                   INTEGER,
    gold                 INTEGER,
    vision_score         INTEGER,
    wards_placed         INTEGER,
    wards_killed         INTEGER,
    control_wards_bought INTEGER,
    damage_to_champs     INTEGER,
    rune_primary         INTEGER,
    rune_secondary       INTEGER,
    items_final          TEXT,   -- json 배열
    summoner1            INTEGER,
    summoner2            INTEGER,
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS idx_participants_puuid ON participants(puuid);

CREATE TABLE IF NOT EXISTS frames (
    match_id    TEXT NOT NULL,
    puuid       TEXT NOT NULL,
    minute      INTEGER NOT NULL,
    x           INTEGER,
    y           INTEGER,
    cs          INTEGER,
    jungle_cs   INTEGER,
    gold        INTEGER,
    level       INTEGER,
    xp          INTEGER,
    PRIMARY KEY (match_id, puuid, minute)
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      TEXT NOT NULL,
    ts_ms         INTEGER NOT NULL,
    type          TEXT NOT NULL,
    killer_puuid  TEXT,      -- 행위자 (killerId / creatorId / participantId)
    victim_puuid  TEXT,
    x             INTEGER,
    y             INTEGER,
    item_id       INTEGER,
    ward_type     TEXT,
    monster_type  TEXT,
    extra         TEXT       -- 나머지 필드 전부 json
);
CREATE INDEX IF NOT EXISTS idx_events_match_type ON events(match_id, type);
CREATE INDEX IF NOT EXISTS idx_events_victim ON events(match_id, victim_puuid);

CREATE TABLE IF NOT EXISTS champion_mastery (
    puuid           TEXT NOT NULL,
    champion_id     INTEGER NOT NULL,
    champion_level  INTEGER,
    champion_points INTEGER,
    last_play_time  INTEGER,
    fetched_at      TEXT,
    PRIMARY KEY (puuid, champion_id)
);

-- 데스 1건 단위 상세 (리포트의 시간대/구역 분포용)
CREATE TABLE IF NOT EXISTS deaths_detail (
    match_id   TEXT NOT NULL,
    puuid      TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts_ms      INTEGER,
    minute     REAL,
    x          INTEGER,
    y          INTEGER,
    zone       TEXT,
    warded     INTEGER,     -- 1=근처(1500) 아군 와드 있었음, 0=없음, NULL=판정불가
    PRIMARY KEY (match_id, puuid, seq)
);

CREATE TABLE IF NOT EXISTS collect_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS static_data (
    key        TEXT PRIMARY KEY,
    json       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

# participant_metrics 는 컬럼이 많아서 코드에서 생성한다.
METRIC_COLUMNS: list[tuple[str, str]] = [
    ("match_id", "TEXT NOT NULL"),
    ("puuid", "TEXT NOT NULL"),
    ("game_start", "INTEGER"),
    ("patch", "TEXT"),
    ("team_position", "TEXT"),
    ("champion_id", "INTEGER"),
    ("champion_name", "TEXT"),
    ("tier_at_collect", "TEXT"),
    ("is_me", "INTEGER"),
    ("is_bench", "INTEGER"),
    ("duration_s", "INTEGER"),
    # 기본
    ("win", "INTEGER"),
    ("kills", "INTEGER"),
    ("deaths", "INTEGER"),
    ("assists", "INTEGER"),
    ("kda", "REAL"),
    ("cs", "INTEGER"),
    ("cs_per_min", "REAL"),
    ("cs10", "INTEGER"),
    ("cs15", "INTEGER"),
    ("gold15", "INTEGER"),
    ("damage_to_champs", "INTEGER"),
    # 라인 상대 대비
    ("opp_puuid", "TEXT"),
    ("cs10_diff", "INTEGER"),
    ("cs15_diff", "INTEGER"),
    ("gold10_diff", "INTEGER"),
    ("gold15_diff", "INTEGER"),
    ("level10_diff", "INTEGER"),
    ("level15_diff", "INTEGER"),
    # 시야
    ("vision_score", "INTEGER"),
    ("vision_per_min", "REAL"),
    ("control_wards_bought", "INTEGER"),
    ("first_control_ward_min", "REAL"),
    ("wards_placed", "INTEGER"),
    ("wards_killed", "INTEGER"),
    # 데스
    ("deaths_before_15", "INTEGER"),
    ("deaths_unwarded", "INTEGER"),
    ("deaths_warded", "INTEGER"),
    ("deaths_zone_json", "TEXT"),
    ("deaths_bucket_json", "TEXT"),
    # 오브젝트
    ("obj_team_total", "INTEGER"),
    ("obj_participated", "INTEGER"),
    ("obj_participation", "REAL"),
    ("obj_detail_json", "TEXT"),
    # 정글 전용
    ("is_jungle", "INTEGER"),
    ("first_full_clear_min", "REAL"),
    ("first_gank_min", "REAL"),
    ("first_counter_jungled_min", "REAL"),
    ("jg_gold_diff_5", "INTEGER"),
    ("jg_gold_diff_10", "INTEGER"),
    ("jg_level_diff_5", "REAL"),
    ("jg_level_diff_10", "REAL"),
    ("enemy_jungle_minutes", "INTEGER"),
    # 아이템 / 귀환
    ("first_core_item_min", "REAL"),
    ("first_core_item_id", "INTEGER"),
    ("back_count", "INTEGER"),
    ("voluntary_back_count", "INTEGER"),
    ("back_times_json", "TEXT"),
    ("computed_at", "TEXT"),
]

METRICS_DDL = (
    "CREATE TABLE IF NOT EXISTS participant_metrics (\n  "
    + ",\n  ".join(f"{n} {t}" for n, t in METRIC_COLUMNS)
    + ",\n  PRIMARY KEY (match_id, puuid)\n)"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(METRICS_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metrics_pos ON participant_metrics(team_position)"
    )
    _migrate_metrics(conn)
    _cleanup_legacy(conn)
    conn.commit()


def _cleanup_legacy(conn: sqlite3.Connection) -> None:
    """예전 버전이 matches_raw 에 잘못 넣은 replays 탐색 결과를 치운다."""
    conn.execute("DELETE FROM matches_raw WHERE match_id LIKE '\_\_replays\_probe\_\_%' ESCAPE '\\'")


def _migrate_metrics(conn: sqlite3.Connection) -> list[str]:
    """예전 DB 에 METRIC_COLUMNS 가 늘어난 만큼 컬럼을 붙인다(데이터는 보존)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(participant_metrics)")}
    added = []
    for name, decl in METRIC_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE participant_metrics ADD COLUMN {name} {decl.replace('NOT NULL', '')}")
            added.append(name)
    return added


@contextmanager
def session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- upsert --
def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], keys: Sequence[str]) -> None:
    cols = list(row)
    updates = [c for c in cols if c not in keys]
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in updates)
        if updates
        else f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    )
    conn.execute(sql, [row[c] for c in cols])


def upsert_many(
    conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]], keys: Sequence[str]
) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0])
    updates = [c for c in cols if c not in keys]
    placeholders = ", ".join("?" * len(cols))
    if updates:
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in updates)
        )
    else:
        sql = f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [[r[c] for c in cols] for r in rows])
    return len(rows)


def save_raw(conn: sqlite3.Connection, table: str, match_id: str, payload: Any) -> None:
    conn.execute(
        f"INSERT INTO {table} (match_id, json, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(match_id) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
        (match_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now_iso()),
    )


def load_raw(conn: sqlite3.Connection, table: str, match_id: str) -> Any | None:
    row = conn.execute(f"SELECT json FROM {table} WHERE match_id = ?", (match_id,)).fetchone()
    return json.loads(row["json"]) if row else None


def has_raw(conn: sqlite3.Connection, table: str, match_id: str) -> bool:
    return (
        conn.execute(f"SELECT 1 FROM {table} WHERE match_id = ?", (match_id,)).fetchone()
        is not None
    )


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO collect_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM collect_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "players", "matches_raw", "timelines_raw", "matches", "participants",
        "frames", "events", "participant_metrics", "deaths_detail", "champion_mastery",
    ]
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        except sqlite3.OperationalError:
            out[t] = 0
    return out
