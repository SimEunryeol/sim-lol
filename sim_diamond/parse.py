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


def puuid_map(timeline: dict) -> dict[int, str]:
    """participantId(1..10) → puuid"""
    meta = timeline.get("metadata") or {}
    plist = meta.get("participants") or []
    out = {i + 1: p for i, p in enumerate(plist)}
    if not out:  # 구형/변형 응답 대비
        for p in (timeline.get("info") or {}).get("participants") or []:
            if "participantId" in p and "puuid" in p:
                out[int(p["participantId"])] = p["puuid"]
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
                "puuid": p.get("puuid"),
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
def store_match(conn: sqlite3.Connection, raw: dict) -> str:
    match_row, participants = parse_match(raw)
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
    n_m = n_t = 0
    for mid in match_ids:
        raw = load_raw(conn, "matches_raw", mid)
        if isinstance(raw, dict) and "info" in raw and "participants" in raw["info"]:
            store_match(conn, raw)
            n_m += 1
        elif raw is not None:
            print(f"  ! {mid} 는 매치 JSON 이 아니라 건너뜁니다")
            continue
        tl = load_raw(conn, "timelines_raw", mid)
        if tl:
            store_timeline(conn, tl, mid)
            n_t += 1
    conn.commit()
    return {"matches": n_m, "timelines": n_t}
