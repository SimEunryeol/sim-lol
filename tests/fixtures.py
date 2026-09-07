"""Riot 응답 모양을 흉내낸 합성 match / timeline JSON 생성기.

네트워크 없이 파싱 → 지표 → 리포트 전체를 돌려보기 위한 것이다.
실제 API 응답의 필드 이름/중첩 구조를 그대로 따른다.
"""
from __future__ import annotations

import random
from typing import Any

POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
CHAMPS = {
    "TOP": (86, "Garen"), "JUNGLE": (64, "LeeSin"), "MIDDLE": (103, "Ahri"),
    "BOTTOM": (222, "Jinx"), "UTILITY": (412, "Thresh"),
}
# 구역별 대표 좌표 (geo.zone_raw 검증용으로도 쓰인다)
SPOT = {
    "TOP": (1400, 7000), "MID": (7400, 7400), "BOT": (7000, 1400),
    "BLUE_JUNGLE": (3600, 6200), "RED_JUNGLE": (11000, 8800),
    "DRAGON_PIT": (9866, 4414), "BARON_PIT": (5007, 10471), "RIVER": (11500, 4200),
    "BLUE_BASE": (1150, 1150), "RED_BASE": (13700, 13700),
}
HOME = {
    (100, "TOP"): SPOT["TOP"], (100, "MIDDLE"): SPOT["MID"], (100, "BOTTOM"): SPOT["BOT"],
    (100, "UTILITY"): SPOT["BOT"], (100, "JUNGLE"): SPOT["BLUE_JUNGLE"],
    (200, "TOP"): SPOT["TOP"], (200, "MIDDLE"): SPOT["MID"], (200, "BOTTOM"): SPOT["BOT"],
    (200, "UTILITY"): SPOT["BOT"], (200, "JUNGLE"): SPOT["RED_JUNGLE"],
}

# ddragon 대체용 최소 정적 데이터
DDRAGON_CHAMPIONS = {
    "data": {
        "Garen": {"key": "86", "id": "Garen", "name": "가렌"},
        "LeeSin": {"key": "64", "id": "LeeSin", "name": "리 신"},
        "Ahri": {"key": "103", "id": "Ahri", "name": "아리"},
        "Jinx": {"key": "222", "id": "Jinx", "name": "징크스"},
        "Thresh": {"key": "412", "id": "Thresh", "name": "쓰레쉬"},
    }
}
DDRAGON_ITEMS = {
    "data": {
        "1039": {"name": "사냥꾼의 부적", "gold": {"total": 450}},
        "2055": {"name": "제어 와드", "gold": {"total": 75}},
        "3067": {"name": "낫갈퀴", "gold": {"total": 1100}},
        "3153": {"name": "몰락한 왕의 검", "gold": {"total": 3200}},
        "6692": {"name": "월식", "gold": {"total": 2900}},
    }
}


def _pos(x: int, y: int) -> dict:
    return {"x": int(x), "y": int(y)}


def make_players(prefix: str = "P") -> list[dict]:
    """10명. index 0..4 = 팀100, 5..9 = 팀200. 같은 position 끼리 라인 상대."""
    out = []
    for team_idx, team in enumerate((100, 200)):
        for i, pos in enumerate(POSITIONS):
            cid, cname = CHAMPS[pos]
            out.append(
                {
                    "puuid": f"{prefix}_{team}_{pos}",
                    "participantId": team_idx * 5 + i + 1,
                    "teamId": team,
                    "teamPosition": pos,
                    "championId": cid,
                    "championName": cname,
                }
            )
    return out


def make_match(
    match_id: str,
    start_ms: int,
    duration_s: int = 1800,
    hero_index: int = 1,          # 기본: 팀100 정글
    hero_wins: bool = True,
    seed: int = 0,
    puuid_prefix: str = "P",
    hero_puuid: str | None = None,
) -> tuple[dict, dict]:
    rng = random.Random(seed)
    players = make_players(puuid_prefix)
    if hero_puuid:
        players[hero_index]["puuid"] = hero_puuid
    hero = players[hero_index]
    win_team = hero["teamId"] if hero_wins else (200 if hero["teamId"] == 100 else 100)
    dur_min = max(1, duration_s // 60)

    # ---------------------------------------------------------- match ----
    mp: list[dict] = []
    for idx, p in enumerate(players):
        is_hero = idx == hero_index
        deaths = rng.randint(3, 9) if is_hero else rng.randint(2, 8)
        kills = rng.randint(2, 12)
        assists = rng.randint(3, 15)
        cs = int(dur_min * (3.0 if p["teamPosition"] in ("JUNGLE", "UTILITY") else 7.2)) + rng.randint(-15, 15)
        mp.append(
            {
                "puuid": p["puuid"],
                "participantId": p["participantId"],
                "teamId": p["teamId"],
                "championId": p["championId"],
                "championName": p["championName"],
                "teamPosition": p["teamPosition"],
                "individualPosition": p["teamPosition"],
                "win": p["teamId"] == win_team,
                "kills": kills, "deaths": deaths, "assists": assists,
                "totalMinionsKilled": int(cs * 0.8), "neutralMinionsKilled": int(cs * 0.2),
                "goldEarned": 8000 + rng.randint(0, 6000),
                "visionScore": rng.randint(10, 60),
                "wardsPlaced": rng.randint(4, 25), "wardsKilled": rng.randint(0, 8),
                "visionWardsBoughtInGame": rng.randint(0, 6),
                "totalDamageDealtToChampions": rng.randint(8000, 35000),
                "perks": {"styles": [
                    {"description": "primaryStyle", "style": 8000},
                    {"description": "subStyle", "style": 8100},
                ]},
                **{f"item{i}": rng.choice([3153, 6692, 3067, 2055, 0]) for i in range(7)},
                "summoner1Id": 4, "summoner2Id": 11 if p["teamPosition"] == "JUNGLE" else 14,
            }
        )
    match = {
        "metadata": {"matchId": match_id, "participants": [p["puuid"] for p in players]},
        "info": {
            "gameStartTimestamp": start_ms,
            "gameEndTimestamp": start_ms + duration_s * 1000,
            "gameCreation": start_ms - 60_000,
            "gameDuration": duration_s,
            "gameVersion": "15.18.593.1234",
            "queueId": 420,
            "participants": mp,
            "teams": [{"teamId": 100, "win": win_team == 100},
                      {"teamId": 200, "win": win_team == 200}],
        },
    }

    # -------------------------------------------------------- timeline ----
    frames = []
    hero_pid = hero["participantId"]
    for minute in range(dur_min + 1):
        pframes: dict[str, Any] = {}
        for p in players:
            hx, hy = HOME[(p["teamId"], p["teamPosition"])]
            if p["participantId"] == hero_pid and p["teamPosition"] == "JUNGLE":
                # 12,13분엔 적정글로 카정을 간다
                if minute in (12, 13):
                    hx, hy = HOME[(200 if p["teamId"] == 100 else 100, "JUNGLE")]
            jitter = rng.randint(-350, 350)
            is_jg = p["teamPosition"] == "JUNGLE"
            # 라인전 우위/열위를 만든다: 팀100 이 조금 앞선다
            edge = 1.06 if p["teamId"] == 100 else 1.0
            pframes[str(p["participantId"])] = {
                "participantId": p["participantId"],
                "position": _pos(hx + jitter, hy + jitter),
                "minionsKilled": 0 if is_jg else int(minute * 7.0 * edge),
                "jungleMinionsKilled": int(minute * 4.2) if is_jg else 0,
                "totalGold": int(500 + minute * 300 * edge),
                "currentGold": rng.randint(0, 900),
                "level": min(18, 1 + int(minute * 0.55)),
                "xp": int(minute * 380 * edge),
            }
        frames.append({"timestamp": minute * 60_000, "participantFrames": pframes, "events": []})

    def add(minute: float, ev: dict) -> None:
        idx = min(len(frames) - 1, int(minute))
        ev.setdefault("timestamp", int(minute * 60_000))
        frames[idx]["events"].append(ev)

    ally_ids = [p["participantId"] for p in players if p["teamId"] == hero["teamId"]]
    enemy_ids = [p["participantId"] for p in players if p["teamId"] != hero["teamId"]]

    # 시작 아이템 + 상점 방문(귀환) 3회 + 코어템 + 제어와드 + 되돌리기
    add(0.02, {"type": "ITEM_PURCHASED", "participantId": hero_pid, "itemId": 1039})
    add(5.5, {"type": "ITEM_PURCHASED", "participantId": hero_pid, "itemId": 3067})
    add(5.6, {"type": "ITEM_PURCHASED", "participantId": hero_pid, "itemId": 2055})
    add(11.0, {"type": "ITEM_PURCHASED", "participantId": hero_pid, "itemId": 6692})
    add(11.1, {"type": "ITEM_PURCHASED", "participantId": hero_pid, "itemId": 2055})
    add(17.0, {"type": "ITEM_PURCHASED", "participantId": hero_pid, "itemId": 3153})
    add(17.05, {"type": "ITEM_UNDO", "participantId": hero_pid, "beforeId": 3153, "afterId": 0})
    add(17.2, {"type": "ITEM_PURCHASED", "participantId": hero_pid, "itemId": 3153})

    # 아군 와드: 8.5분 미드 근처(8.9분 데스와 가까움), 14분 봇(멀리)
    add(8.5, {"type": "WARD_PLACED", "creatorId": ally_ids[2], "wardType": "CONTROL_WARD"})
    add(14.0, {"type": "WARD_PLACED", "creatorId": ally_ids[3], "wardType": "YELLOW_TRINKET"})
    add(20.0, {"type": "WARD_PLACED", "creatorId": ally_ids[0], "wardType": "UNDEFINED"})

    # 내 데스: 3.4분 내정글 / 8.9분 미드(와드 있음) / 16.5분 적정글 / 24.0분 바론둥지
    deaths_plan = [
        (3.4, SPOT["BLUE_JUNGLE"]),
        (8.9, SPOT["MID"]),
        (16.5, SPOT["RED_JUNGLE"]),
        (24.0, SPOT["BARON_PIT"]),
    ]
    for i, (minute, (x, y)) in enumerate(deaths_plan):
        if minute > dur_min:
            continue
        add(minute, {
            "type": "CHAMPION_KILL", "killerId": enemy_ids[i % 5], "victimId": hero_pid,
            "position": _pos(x, y), "assistingParticipantIds": [enemy_ids[(i + 1) % 5]],
        })
    # 내가 딴 킬 (첫 갱: 4.2분 탑)
    add(4.2, {"type": "CHAMPION_KILL", "killerId": hero_pid, "victimId": enemy_ids[0],
              "position": _pos(*SPOT["TOP"]), "assistingParticipantIds": [ally_ids[0]]})
    add(2.0, {"type": "CHAMPION_KILL", "killerId": ally_ids[3], "victimId": enemy_ids[3],
              "position": _pos(*SPOT["BOT"]), "assistingParticipantIds": []})

    # 오브젝트: 유충(6분) / 드래곤(8분, 내가 처치) / 전령(14분) / 바론(22분, 나는 멀리)
    add(6.0, {"type": "ELITE_MONSTER_KILL", "killerId": ally_ids[1], "monsterType": "HORDE",
              "monsterSubType": "HORDE", "position": _pos(*SPOT["BARON_PIT"]),
              "killerTeamId": hero["teamId"]})
    add(8.0, {"type": "ELITE_MONSTER_KILL", "killerId": hero_pid, "monsterType": "DRAGON",
              "monsterSubType": "FIRE_DRAGON", "position": _pos(*SPOT["DRAGON_PIT"]),
              "killerTeamId": hero["teamId"]})
    add(14.0, {"type": "ELITE_MONSTER_KILL", "killerId": ally_ids[0], "monsterType": "RIFTHERALD",
               "position": _pos(*SPOT["BARON_PIT"]), "killerTeamId": hero["teamId"]})
    if dur_min >= 22:
        add(22.0, {"type": "ELITE_MONSTER_KILL", "killerId": ally_ids[4], "monsterType": "BARON_NASHOR",
                   "position": _pos(*SPOT["BARON_PIT"]), "killerTeamId": hero["teamId"]})
    # 상대 팀 오브젝트(참여율 계산에서 제외돼야 한다)
    add(18.0, {"type": "ELITE_MONSTER_KILL", "killerId": enemy_ids[1], "monsterType": "DRAGON",
               "position": _pos(*SPOT["DRAGON_PIT"]),
               "killerTeamId": 200 if hero["teamId"] == 100 else 100})

    timeline = {
        "metadata": {"matchId": match_id, "participants": [p["puuid"] for p in players]},
        "info": {"frameInterval": 60000, "gameId": 1, "frames": frames,
                 "participants": [{"participantId": p["participantId"], "puuid": p["puuid"]}
                                  for p in players]},
    }
    return match, timeline
