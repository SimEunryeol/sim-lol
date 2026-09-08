"""raw JSON → 파싱 테이블. raw 만 보고 언제든 재생성 가능하다."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from .db import load_raw, upsert, upsert_many

# 이벤트에서 "행위자"를 담고 있는 필드들 (우선순위 순)
ACTOR_FIELDS = ("killerId", "creatorId", "participantId")
_SKIP_EXTRA = {
    "type", "timestamp", "position", "itemId", "wardType", "monsterType",
    "killerId", "victimId", "creatorId", "participantId",
}


# 라이엇이 계정의 puuid 를 한 번 바꿨다(2026-09 초). 새로 받는 JSON 은 새 puuid 로
# 오지만 `matches_raw` 에 이미 받아둔 옛 사본은 옛 puuid 그대로다. 원본은 버리지
# 않는 게 원칙이라(9장), 읽을 때 옛→새로 옮겨 준다. `puuid_alias` 표가 비어 있으면
# 아무 일도 하지 않는다.
_ALIAS: dict[str, str] | None = None


def load_alias(conn: sqlite3.Connection) -> dict[str, str]:
    global _ALIAS
    conn.execute("CREATE TABLE IF NOT EXISTS puuid_alias ("
                 "old_puuid TEXT PRIMARY KEY, new_puuid TEXT NOT NULL, note TEXT)")
    _ALIAS = {r[0]: r[1] for r in conn.execute(
        "SELECT old_puuid, new_puuid FROM puuid_alias")}
    return _ALIAS


def alias(puuid: str | None) -> str | None:
    if puuid is None or not _ALIAS:
        return puuid
    return _ALIAS.get(puuid, puuid)


def puuid_map(timeline: dict) -> dict[int, str]:
    """participantId(1..10) → puuid"""
    meta = timeline.get("metadata") or {}
    plist = meta.get("participants") or []
    out = {i + 1: p for i, p in enumerate(plist)}
    if not out:  # 구형/변형 응답 대비
        for p in (timeline.get("info") or {}).get("participants") or []:
            if "participantId" in p and "puuid" in p:
                out[int(p["participantId"])] = alias(p["puuid"])
    return out


def parse_match(raw: dict) -> tuple[dict, list[dict]]:
    info = raw["info"]
    match_id = (raw.get("metadata") or {}).get("matchId") or info.get("gameId")
    match_row = {
        "match_id": match_id,
        "game_start": info.get("gameStartTimestamp") or info.get("gameCreation"),
        "duration_s": _duration_s(info),
        "patch": ".".join(str(info.get("gameVersion", "")).split(".")[:2]) or None,
        "queue_id": info.get("queueId"),
    }

    participants: list[dict] = []
    for p in info.get("participants", []):
        perks = p.get("perks") or {}
        styles = perks.get("styles") or []
        primary = next((s.get("style") for s in styles if s.get("description") == "primaryStyle"), None)
        secondary = next((s.get("style") for s in styles if s.get("description") == "subStyle"), None)
        if primary is None and styles:
            primary = styles[0].get("style")
        if secondary is None and len(styles) > 1:
            secondary = styles[1].get("style")
        items = [p.get(f"item{i}") for i in range(7)]
        participants.append(
            {
                "match_id": match_id,
                "puuid": alias(p.get("puuid")),
                "participant_id": p.get("participantId"),
                "team_id": p.get("teamId"),
                "champion_id": p.get("championId"),
                "champion_name": p.get("championName"),
                "team_position": _position(p),
                "win": int(bool(p.get("win"))),
                "kills": p.get("kills"),
                "deaths": p.get("deaths"),
                "assists": p.get("assists"),
                "cs": (p.get("totalMinionsKilled") or 0) + (p.get("neutralMinionsKilled") or 0),
                "gold": p.get("goldEarned"),
                "vision_score": p.get("visionScore"),
                "wards_placed": p.get("wardsPlaced"),
                "wards_killed": p.get("wardsKilled"),
                "control_wards_bought": p.get("visionWardsBoughtInGame"),
                "damage_to_champs": p.get("totalDamageDealtToChampions"),
                "rune_primary": primary,
                "rune_secondary": secondary,
                "items_final": json.dumps(items),
                "summoner1": p.get("summoner1Id"),
                "summoner2": p.get("summoner2Id"),
            }
        )
    return match_row, participants


def _duration_s(info: dict) -> int | None:
    dur = info.get("gameDuration")
    if dur is None:
        return None
    # 2021 이후 패치부터 초 단위. 아주 오래된 응답은 ms 로 오는 경우가 있다.
    end, start = info.get("gameEndTimestamp"), info.get("gameStartTimestamp")
    if end and start:
        return int(round((end - start) / 1000))
    return int(dur // 1000) if dur > 100000 else int(dur)


def _position(p: dict) -> str | None:
    pos = (p.get("teamPosition") or p.get("individualPosition") or "").upper()
    return pos or None


def parse_timeline(timeline: dict, match_id: str) -> tuple[list[dict], list[dict]]:
    pmap = puuid_map(timeline)
    info = timeline.get("info") or {}
    frames: list[dict] = []
    events: list[dict] = []

    for frame in info.get("frames", []):
        minute = int(round((frame.get("timestamp") or 0) / 60000))
        for pid_str, pf in (frame.get("participantFrames") or {}).items():
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            puuid = pmap.get(pid)
            if not puuid:
                continue
            pos = pf.get("position") or {}
            frames.append(
                {
                    "match_id": match_id,
                    "puuid": puuid,
                    "minute": minute,
                    "x": pos.get("x"),
                    "y": pos.get("y"),
                    "cs": pf.get("minionsKilled"),
                    "jungle_cs": pf.get("jungleMinionsKilled"),
                    "gold": pf.get("totalGold"),
                    "level": pf.get("level"),
                    "xp": pf.get("xp"),
                }
            )

        for ev in frame.get("events", []):
            pos = ev.get("position") or {}
            actor_id = next((ev[f] for f in ACTOR_FIELDS if ev.get(f)), None)
            extra = {k: v for k, v in ev.items() if k not in _SKIP_EXTRA}
            if "assistingParticipantIds" in ev:
                extra["assisting_puuids"] = [
                    pmap.get(int(a)) for a in ev["assistingParticipantIds"] if pmap.get(int(a))
                ]
            events.append(
                {
                    "match_id": match_id,
                    "ts_ms": ev.get("timestamp"),
                    "type": ev.get("type"),
                    "killer_puuid": pmap.get(int(actor_id)) if actor_id else None,
                    "victim_puuid": pmap.get(int(ev["victimId"])) if ev.get("victimId") else None,
                    "x": pos.get("x"),
                    "y": pos.get("y"),
                    "item_id": ev.get("itemId"),
                    "ward_type": ev.get("wardType"),
                    "monster_type": ev.get("monsterType"),
                    "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
                }
            )
    return frames, events


# --------------------------------------------------------------- 저장 --
def store_match(conn: sqlite3.Connection, raw: dict) -> str | None:
    """참가자가 없는 껍데기 응답(시작되지 않은 게임: queueId 0, duration 0)은 저장하지 않는다.
    원본은 matches_raw 에 남아 있으므로 나중에 다시 볼 수 있다."""
    match_row, participants = parse_match(raw)
    if not participants:
        return None
    upsert(conn, "matches", match_row, ["match_id"])
    upsert_many(conn, "participants", participants, ["match_id", "puuid"])
    return match_row["match_id"]


def store_timeline(conn: sqlite3.Connection, timeline: dict, match_id: str) -> tuple[int, int]:
    frames, events = parse_timeline(timeline, match_id)
    upsert_many(conn, "frames", frames, ["match_id", "puuid", "minute"])
    conn.execute("DELETE FROM events WHERE match_id = ?", (match_id,))
    if events:
        cols = list(events[0])
        conn.executemany(
            f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [[e[c] for c in cols] for e in events],
        )
    return len(frames), len(events)


def reparse_all(conn: sqlite3.Connection, match_ids: Iterable[str] | None = None) -> dict[str, int]:
    """raw 테이블만 보고 matches/participants/frames/events 를 통째로 재생성."""
    if match_ids is None:
        match_ids = [r["match_id"] for r in conn.execute("SELECT match_id FROM matches_raw")]
    n_m = n_t = n_empty = 0
    for mid in match_ids:
        raw = load_raw(conn, "matches_raw", mid)
        if isinstance(raw, dict) and "info" in raw and "participants" in raw["info"]:
            if store_match(conn, raw) is None:
                n_empty += 1
                continue
            n_m += 1
        elif raw is not None:
            print(f"  ! {mid} 는 매치 JSON 이 아니라 건너뜁니다")
            continue
        tl = load_raw(conn, "timelines_raw", mid)
        if tl:
            store_timeline(conn, tl, mid)
            n_t += 1
    conn.commit()
    return {"matches": n_m, "timelines": n_t, "empty": n_empty}
