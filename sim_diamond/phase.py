"""탐색/집중 단계 관리 (phases 테이블).

  python -m sim_diamond.phase init-explore            # 5개 라인 탐색 단계를 한 번에 만든다
  python -m sim_diamond.phase list
  python -m sim_diamond.phase add --name 탐색 --role JUNGLE --champs Nocturne Rammus --games 15
  python -m sim_diamond.phase set-champs --name 탐색 --role TOP --champs Garen Malphite
  python -m sim_diamond.phase end --name 탐색 --role TOP
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import config
from .db import now_iso, session, upsert
from .ddragon import Static

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_KO = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿"}
EXPLORE_NAME = "탐색"
EXPLORE_TARGET = 15          # 라인당 목표 판수
EXPLORE_CHAMPS_PER_ROLE = 2  # 라인당 기준 챔프 수


def today_kst() -> str:
    return datetime.now(config.KST).strftime("%Y-%m-%d")


def add_phase(conn, name: str, role: str, champs: list[str], target: int, start: str) -> None:
    upsert(conn, "phases", {
        "phase_name": name, "start_date": start, "role": role.upper(),
        "champs": json.dumps(champs, ensure_ascii=False), "target_games": target,
        "end_date": None, "created_at": now_iso(),
    }, ["phase_name", "role"])


def list_phases(conn, name: str | None = None) -> list[dict]:
    sql = "SELECT * FROM phases"
    args: tuple = ()
    if name:
        sql += " WHERE phase_name = ?"
        args = (name,)
    sql += " ORDER BY start_date, role"
    return [dict(r) for r in conn.execute(sql, args)]


def active_phase(conn, name: str | None = None) -> list[dict]:
    """종료되지 않은 단계. name 을 안 주면 가장 최근에 시작한 단계."""
    rows = [p for p in list_phases(conn, name) if not p["end_date"]]
    if not rows:
        return []
    if name:
        return rows
    latest = max(p["start_date"] for p in rows)
    newest = max(p["phase_name"] for p in rows if p["start_date"] == latest)
    return [p for p in rows if p["phase_name"] == newest]


def _print(conn, static: Static) -> None:
    rows = list_phases(conn)
    if not rows:
        print("등록된 단계가 없습니다. `python -m sim_diamond.phase init-explore` 로 시작하세요.")
        return
    print(f"{'단계':<8} {'라인':<6} {'시작일':<12} {'목표':>4}  기준 챔프")
    print("-" * 72)
    for p in rows:
        champs = ", ".join(json.loads(p["champs"]))
        end = f"  (종료 {p['end_date']})" if p["end_date"] else ""
        print(f"{p['phase_name']:<8} {ROLE_KO.get(p['role'], p['role']):<6} "
              f"{p['start_date']:<12} {p['target_games']:>4}  {champs}{end}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="탐색/집중 단계 관리")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-explore", help="5개 라인 탐색 단계를 한 번에 만든다")
    p_init.add_argument("--name", default=EXPLORE_NAME)
    p_init.add_argument("--start", default=None, help="YYYY-MM-DD (기본: 오늘 KST)")
    p_init.add_argument("--games", type=int, default=EXPLORE_TARGET)
    for role in ROLES:
        p_init.add_argument(f"--{role.lower()}", nargs="*", default=[],
                            help=f"{ROLE_KO[role]} 기준 챔프 (영문 championName)")

    sub.add_parser("list", help="등록된 단계 보기")

    p_add = sub.add_parser("add", help="단계 하나 추가/수정")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--role", required=True, choices=ROLES)
    p_add.add_argument("--champs", nargs="+", required=True)
    p_add.add_argument("--games", type=int, default=EXPLORE_TARGET)
    p_add.add_argument("--start", default=None)

    p_set = sub.add_parser("set-champs", help="기준 챔프만 교체")
    p_set.add_argument("--name", required=True)
    p_set.add_argument("--role", required=True, choices=ROLES)
    p_set.add_argument("--champs", nargs="+", required=True)

    p_end = sub.add_parser("end", help="단계 종료 표시")
    p_end.add_argument("--name", required=True)
    p_end.add_argument("--role", choices=ROLES, help="생략하면 그 단계 전체")

    args = ap.parse_args(argv)
    st = config.load_settings(require_key=False)
    with session(st.db_path) as conn:
        static = Static(conn)
        if args.cmd == "init-explore":
            start = args.start or today_kst()
            for role in ROLES:
                champs = getattr(args, role.lower()) or []
                add_phase(conn, args.name, role, champs, args.games, start)
            conn.commit()
            print(f"'{args.name}' 단계를 {start} 시작으로 5개 라인에 만들었습니다 "
                  f"(라인당 {args.games}판, 총 {args.games * len(ROLES)}판).")
            empty = [ROLE_KO[r] for r in ROLES if not getattr(args, r.lower())]
            if empty:
                print(f"  ! 기준 챔프를 아직 안 정한 라인: {', '.join(empty)}")
                print(f"    예) python -m sim_diamond.phase set-champs --name {args.name} "
                      f"--role JUNGLE --champs Nocturne Rammus")
                print("    챔프 이름은 영문 championName 을 씁니다 (리 신 → LeeSin).")
            _print(conn, static)
        elif args.cmd == "list":
            _print(conn, static)
        elif args.cmd == "add":
            add_phase(conn, args.name, args.role, args.champs, args.games,
                      args.start or today_kst())
            conn.commit()
            _print(conn, static)
        elif args.cmd == "set-champs":
            n = conn.execute(
                "UPDATE phases SET champs = ? WHERE phase_name = ? AND role = ?",
                (json.dumps(args.champs, ensure_ascii=False), args.name, args.role),
            ).rowcount
            conn.commit()
            print(f"{n}건 수정" if n else "해당 단계/라인이 없습니다.")
            _print(conn, static)
        elif args.cmd == "end":
            sql = "UPDATE phases SET end_date = ? WHERE phase_name = ? AND end_date IS NULL"
            params: list = [today_kst(), args.name]
            if args.role:
                sql += " AND role = ?"
                params.append(args.role)
            n = conn.execute(sql, params).rowcount
            conn.commit()
            print(f"{n}건 종료 처리")
            _print(conn, static)
    return 0


if __name__ == "__main__":
    sys.exit(main())
