"""가장 최근 경기에 재미 점수(1~5)와 메모를 남긴다.

  python -m sim_diamond.rate 4
  python -m sim_diamond.rate 2 "정글 동선 꼬여서 답답했음"
  python -m sim_diamond.rate 5 "한타 재밌었다" --match KR_8369802379
  python -m sim_diamond.rate --list 10
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from . import config
from .db import now_iso, session, upsert
from .ddragon import Static

SCORE_KO = {1: "재미없음", 2: "별로", 3: "보통", 4: "재밌음", 5: "아주 재밌음"}


def recent_matches(conn, puuid: str, limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT pm.match_id, pm.game_start, pm.team_position, pm.champion_id,
               pm.champion_name, pm.win, pm.kills, pm.deaths, pm.assists,
               sr.score, sr.memo
        FROM participant_metrics pm
        LEFT JOIN self_rating sr ON sr.match_id = pm.match_id AND sr.puuid = pm.puuid
        WHERE pm.puuid = ? AND pm.duration_s >= ?
        ORDER BY pm.game_start DESC LIMIT ?
    """, (puuid, config.REMAKE_MAX_S, limit))]


def describe(m: dict, static: Static) -> str:
    when = datetime.fromtimestamp((m["game_start"] or 0) / 1000, config.KST).strftime("%m-%d %H:%M")
    champ = static.champion(m["champion_id"], m["champion_name"])
    mark = f"  ★{m['score']}" + (f" {m['memo']}" if m["memo"] else "") if m["score"] else ""
    return (f"{when}  {champ:<10} {m['team_position'] or '?':<8} "
            f"{'승' if m['win'] else '패'}  {m['kills']}/{m['deaths']}/{m['assists']}"
            f"  {m['match_id']}{mark}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="경기 재미 점수 기록")
    ap.add_argument("score", nargs="?", type=int, choices=[1, 2, 3, 4, 5])
    ap.add_argument("memo", nargs="?", default=None)
    ap.add_argument("--match", default=None, help="특정 경기에 기록 (기본: 가장 최근 경기)")
    ap.add_argument("--list", type=int, nargs="?", const=10, default=None,
                    help="최근 경기와 기록 상태 보기")
    args = ap.parse_args(argv)

    st = config.load_settings(require_key=False)
    with session(st.db_path) as conn:
        static = Static(conn)
        me = conn.execute("SELECT * FROM players WHERE is_me = 1").fetchone()
        if not me:
            print("내 계정이 없습니다. 먼저 python -m sim_diamond.collect_me 를 실행하세요.")
            return 1
        recents = recent_matches(conn, me["puuid"], max(args.list or 10, 10))
        if not recents:
            print("수집된 경기가 없습니다.")
            return 1

        if args.list is not None or args.score is None:
            print("최근 경기 (★ = 이미 기록됨)")
            for m in recents[:args.list or 10]:
                print("  " + describe(m, static))
            if args.score is None:
                print("\n점수를 남기려면: python -m sim_diamond.rate <1-5> \"메모\"")
                return 0

        target = next((m for m in recents if m["match_id"] == args.match), None) if args.match \
            else recents[0]
        if target is None:
            print(f"{args.match} 을(를) 최근 경기에서 찾지 못했습니다.")
            return 1
        prev = target["score"]
        upsert(conn, "self_rating", {
            "match_id": target["match_id"], "puuid": me["puuid"], "score": args.score,
            "memo": args.memo, "created_at": now_iso(),
        }, ["match_id", "puuid"])
        conn.commit()
        verb = f"수정 (★{prev} → ★{args.score})" if prev else f"기록 (★{args.score})"
        print(f"{verb}: {SCORE_KO[args.score]}")
        print("  " + describe({**target, "score": args.score, "memo": args.memo}, static))
        n = conn.execute("SELECT COUNT(*) c FROM self_rating WHERE puuid = ?",
                         (me["puuid"],)).fetchone()["c"]
        print(f"  지금까지 {n}판 기록됨")
    return 0


if __name__ == "__main__":
    sys.exit(main())
