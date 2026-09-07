"""소환사의 협곡 좌표 → 구역 분류.

타임라인 좌표계는 대략 x,y ∈ [0, 14870]. 블루팀(100)은 좌하단, 레드팀(200)은 우상단.
강(river)은 반대각선 x + y ≈ 14870 을 따라 흐르고, 미드는 주대각선 x ≈ y 를 따라 간다.
"""
from __future__ import annotations

import math

MAP_MAX = 14870.0
DIAG = MAP_MAX  # x + y == DIAG 가 강 중심선

BLUE_SPAWN = (1150.0, 1150.0)
RED_SPAWN = (13700.0, 13700.0)
DRAGON_PIT = (9866.0, 4414.0)
BARON_PIT = (5007.0, 10471.0)

BASE_R = 2200.0
PIT_R = 1600.0
LANE_HALF = 2400.0   # 탑/봇 라인 통로 폭(가장자리로부터)
MID_HALF = 1700.0    # 미드 통로 반폭 (|x-y|)
RIVER_HALF = 1300.0  # 강 반폭 (|x+y-DIAG|)

ZONE_KO = {
    "TOP": "탑",
    "MID": "미드",
    "BOT": "봇",
    "OWN_JUNGLE": "내정글",
    "ENEMY_JUNGLE": "적정글",
    "DRAGON_PIT": "용둥지",
    "BARON_PIT": "바론/전령둥지",
    "RIVER": "강",
    "OWN_BASE": "아군기지",
    "ENEMY_BASE": "적기지",
    "UNKNOWN": "불명",
}

# 리포트 5번(데스 구역 분포)에서 쓰는 표시 순서
ZONE_ORDER = [
    "TOP", "MID", "BOT", "OWN_JUNGLE", "ENEMY_JUNGLE",
    "RIVER", "DRAGON_PIT", "BARON_PIT", "OWN_BASE", "ENEMY_BASE", "UNKNOWN",
]


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def zone_raw(x: float | None, y: float | None) -> str:
    """팀 중립 구역. BLUE_JUNGLE / RED_JUNGLE / BLUE_BASE / RED_BASE 를 쓴다."""
    if x is None or y is None:
        return "UNKNOWN"
    if _dist(x, y, *BLUE_SPAWN) < BASE_R:
        return "BLUE_BASE"
    if _dist(x, y, *RED_SPAWN) < BASE_R:
        return "RED_BASE"
    if _dist(x, y, *DRAGON_PIT) < PIT_R:
        return "DRAGON_PIT"
    if _dist(x, y, *BARON_PIT) < PIT_R:
        return "BARON_PIT"

    # 미드: 주대각선 통로
    if abs(x - y) < MID_HALF:
        return "MID"

    above_diag = y > x  # 탑 쪽 절반
    edge_top = (x < LANE_HALF) or (y > MAP_MAX - LANE_HALF)
    edge_bot = (y < LANE_HALF) or (x > MAP_MAX - LANE_HALF)
    if above_diag and edge_top:
        return "TOP"
    if (not above_diag) and edge_bot:
        return "BOT"

    if abs(x + y - DIAG) < RIVER_HALF:
        return "RIVER"

    return "BLUE_JUNGLE" if x + y < DIAG else "RED_JUNGLE"


def zone(x: float | None, y: float | None, team_id: int | None) -> str:
    """팀 기준 구역. team_id 는 100(블루) / 200(레드)."""
    raw = zone_raw(x, y)
    if team_id not in (100, 200):
        return raw.replace("BLUE_", "").replace("RED_", "") if "JUNGLE" in raw or "BASE" in raw else raw
    is_blue = team_id == 100
    mapping = {
        "BLUE_JUNGLE": "OWN_JUNGLE" if is_blue else "ENEMY_JUNGLE",
        "RED_JUNGLE": "ENEMY_JUNGLE" if is_blue else "OWN_JUNGLE",
        "BLUE_BASE": "OWN_BASE" if is_blue else "ENEMY_BASE",
        "RED_BASE": "ENEMY_BASE" if is_blue else "OWN_BASE",
    }
    return mapping.get(raw, raw)


def is_base(x: float | None, y: float | None, team_id: int | None) -> bool:
    return zone(x, y, team_id) == "OWN_BASE"


def zone_label(key: str) -> str:
    return ZONE_KO.get(key, key)
