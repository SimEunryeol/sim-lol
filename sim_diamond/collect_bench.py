"""벤치마크 플레이어 샘플 수집.

League-V4 entries 로 KR 서버의 지정 티어(기본 SILVER, GOLD) 각 디비전에서 플레이어를 뽑아
티어당 N명(기본 15명, 최근 활동 우선) → 각자 최근 솔랭 M판(기본 10판)을 raw + 파싱 저장한다.

  python -m sim_diamond.collect_bench --tiers SILVER GOLD --per-tier 15 --per-player 10
"""
from __future__ import annotations

import argparse
import sys
import time

from . import config, ddragon, metrics
from .collect_me import fetch_matches
from .db import counts, now_iso, session, set_state, upsert_many
from .riot_client import RiotClient, RiotError

DIVISIONS = ["I", "II", "III", "IV"]


def sample_players(client: RiotClient, tier: str, per_tier: int, divisions: list[str],
                   pages: int = 2) -> list[dict]:
    """디비전을 고르게 섞어 per_tier 명을 뽑는다. 최근 활동(비활동/신규 제외) 우선."""
    pool: dict[str, list[dict]] = {}
    for div in divisions:
        entries: list[dict] = []
        for page in range(1, pages + 1):
            try:
                got = client.league_entries(tier, div, page=page)
            except RiotError as exc:
                print(f"  ! {tier} {div} p{page} 조회 실패({exc.status})")
                break
            if not got:
                break
            entries.extend(got)
        # 최근 활동 우선: 비활동 계정 제외 → 총 판수 많은 순
        active = [e for e in entries if not e.get("inactive")]
        active.sort(key=lambda e: (e.get("wins", 0) + e.get("losses", 0)), reverse=True)
        pool[div] = active
        print(f"  {tier} {div}: 후보 {len(active)}명")

    picked: list[dict] = []
    idx = 0
    while len(picked) < per_tier and any(len(v) > idx for v in pool.values()):
        for div in divisions:
            if len(picked) >= per_tier:
                break
            if len(pool.get(div, [])) > idx:
                e = pool[div][idx]
                picked.append(
                    {
                        "puuid": e.get("puuid"),
                        "game_name": e.get("gameName") or e.get("summonerName"),
                        "tag_line": e.get("tagLine"),
                        "tier": e.get("tier", tier),
                        "rank": e.get("rank", div),
                        "lp": e.get("leaguePoints"),
                        "is_me": 0,
                        "is_bench": 1,
                        "fetched_at": now_iso(),
                    }
                )
        idx += 1
    return [p for p in picked if p["puuid"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="벤치마크 플레이어 수집")
    ap.add_argument("--tiers", nargs="+", default=["SILVER", "GOLD"])
    ap.add_argument("--divisions", nargs="+", default=DIVISIONS)
    ap.add_argument("--per-tier", type=int, default=15)
    ap.add_argument("--per-player", type=int, default=10)
    ap.add_argument("--no-timeline", action="store_true")
    ap.add_argument("--no-metrics", action="store_true")
    args = ap.parse_args(argv)

    st = config.load_settings()
    t0 = time.time()
    with session(st.db_path) as conn:
        client = RiotClient(st.api_key, conn)
        ddragon.ensure(conn)

        all_players: list[dict] = []
        for tier in args.tiers:
            print(f"[티어 {tier}] 플레이어 샘플링 (목표 {args.per_tier}명)")
            players = sample_players(client, tier, args.per_tier, args.divisions)
            print(f"  선정 {len(players)}명")
            all_players.extend(players)
        if not all_players:
            print("벤치 플레이어를 한 명도 뽑지 못했습니다. API 키/티어 인자를 확인하세요.")
            return 1

        upsert_many(conn, "players", all_players, ["puuid"])
        conn.commit()
        set_state(conn, "bench_puuids", [p["puuid"] for p in all_players])

        print(f"\n[매치 ID 수집] 플레이어 {len(all_players)}명 × 최근 {args.per_player}판")
        wanted: list[str] = []
        for i, p in enumerate(all_players, 1):
            try:
                ids = client.match_ids(p["puuid"], start=0, count=args.per_player,
                                       queue=config.SOLO_QUEUE_ID)
            except RiotError as exc:
                print(f"  ! {p['tier']} {p['rank']} 매치 ID 실패({exc.status})")
                continue
            wanted.extend(ids)
            print(f"  {i}/{len(all_players)} {p['tier']} {p['rank']}: {len(ids)}판", flush=True)
        uniq = list(dict.fromkeys(wanted))
        print(f"  중복 제거 후 {len(uniq)}판")

        print(f"\n[매치 + 타임라인 수집]")
        fstats = fetch_matches(client, conn, uniq, with_timeline=not args.no_timeline,
                               label="벤치 매치")

        if not args.no_metrics:
            print(f"\n[지표 계산]")
            mres = metrics.compute_all(conn)
            print(f"  {mres['matches']}경기 / {mres['rows']}행")

        c = counts(conn)
        print("\n" + "=" * 60)
        print(f"완료: {time.time() - t0:.0f}초")
        print(f"  벤치 {len(all_players)}명 / 매치 {len(uniq)}판 "
              f"(신규 {fstats['new_match']}, 캐시 {fstats['cached']}, 실패 {fstats['failed']})")
        print(f"  DB: " + ", ".join(f"{k}={v}" for k, v in c.items()))
        print(f"  {client.stats.summary()}")
        print("=" * 60)
    return 0


def cli(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except RiotError as exc:
        print(exc.report())
        return 1
    except KeyboardInterrupt:
        print("\n중단했습니다. 같은 명령을 다시 실행하면 받은 데이터부터 이어서 진행합니다.")
        return 130


if __name__ == "__main__":
    sys.exit(cli())
