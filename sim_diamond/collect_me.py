"""내 계정 수집: Riot ID → PUUID → 티어 → 시즌 솔랭 매치 전부 → raw 저장 → 파싱.

중간에 끊겨도 다시 실행하면 이미 받은 매치는 캐시에서 건너뛰고 이어서 진행한다.
  python -m sim_diamond.collect_me
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from . import config, ddragon, metrics, parse
from .db import (
    has_raw, load_raw, now_iso, save_raw, session, set_state, upsert, upsert_many, counts,
)
from .riot_client import RiotClient, RiotError

# /replays 는 공개 문서에 없는 엔드포인트라 후보 경로를 순서대로 찔러보고
# 응답(에러 포함)을 raw 로 남긴다. 용도 파악용.
REPLAY_CANDIDATES = [
    "/lol/match/v5/replays/by-puuid/{puuid}",
    "/lol/match/v5/matches/by-puuid/{puuid}/replays",
    "/lol/match/v5/replays/{puuid}",
]


def fetch_me(client: RiotClient, conn, game_name: str, tag_line: str) -> dict:
    acct = client.account_by_riot_id(game_name, tag_line)
    puuid = acct["puuid"]
    tier = rank = None
    lp = None
    try:
        entries = client.league_entries_by_puuid(puuid)
        solo = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
        if solo:
            tier, rank, lp = solo.get("tier"), solo.get("rank"), solo.get("leaguePoints")
    except RiotError as exc:
        print(f"  ! 티어 조회 실패({exc.status}) — 계속 진행합니다.")
    upsert(
        conn, "players",
        {
            "puuid": puuid, "game_name": acct.get("gameName", game_name),
            "tag_line": acct.get("tagLine", tag_line), "tier": tier, "rank": rank,
            "lp": lp, "is_me": 1, "is_bench": 0, "fetched_at": now_iso(),
        },
        ["puuid"],
    )
    conn.commit()
    return {"puuid": puuid, "tier": tier, "rank": rank, "lp": lp,
            "game_name": acct.get("gameName", game_name), "tag_line": acct.get("tagLine", tag_line)}


def all_match_ids(client: RiotClient, puuid: str, start_time_s: int,
                  queues: list[int] | None, page_size: int = 100,
                  max_matches: int | None = None) -> list[str]:
    """Match-V5 의 queue 파라미터는 값을 하나만 받으므로 큐별로 나눠 부르고 합친다."""
    ids: list[str] = []
    for queue in (queues or [None]):
        start = 0
        while True:
            want = page_size if not max_matches else min(page_size, max_matches - len(ids))
            if want <= 0:
                break
            page = client.match_ids(puuid, start=start, count=want, queue=queue,
                                    start_time=start_time_s)
            if not page:
                break
            ids.extend(page)
            print(f"  매치 ID {len(ids)}개 수집… (큐 {queue})", flush=True)
            if len(page) < want:
                break
            start += want
            if max_matches and len(ids) >= max_matches:
                break
    # 중복 제거(순서 유지)
    seen, out = set(), []
    for m in ids:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out[:max_matches] if max_matches else out


def fetch_matches(client: RiotClient, conn, match_ids: list[str], with_timeline: bool = True,
                  label: str = "매치") -> dict[str, int]:
    total = len(match_ids)
    stats = {"new_match": 0, "new_timeline": 0, "cached": 0, "failed": 0, "empty": 0}
    t0 = time.time()
    for i, mid in enumerate(match_ids, 1):
        try:
            if has_raw(conn, "matches_raw", mid):
                raw = load_raw(conn, "matches_raw", mid)
                stats["cached"] += 1
            else:
                raw = client.match(mid)
                save_raw(conn, "matches_raw", mid, raw)
                stats["new_match"] += 1
            if parse.store_match(conn, raw) is None:
                stats["empty"] = stats.get("empty", 0) + 1
                print(f"  · {mid} 는 참가자가 없는 껍데기 응답입니다 (시작되지 않은 게임). 건너뜁니다.")
                continue

            if with_timeline:
                if has_raw(conn, "timelines_raw", mid):
                    tl = load_raw(conn, "timelines_raw", mid)
                else:
                    tl = client.timeline(mid)
                    save_raw(conn, "timelines_raw", mid, tl)
                    stats["new_timeline"] += 1
                parse.store_timeline(conn, tl, mid)
        except RiotError as exc:
            stats["failed"] += 1
            print(f"  ! {mid} 실패: {exc}")
        if i % 10 == 0 or i == total:
            conn.commit()
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (total - i) / rate if rate else 0
            print(
                f"  {label} {i}/{total} (신규 {stats['new_match']}, 캐시 {stats['cached']}, "
                f"실패 {stats['failed']}) {elapsed:.0f}s 경과, 남은 예상 {eta:.0f}s",
                flush=True,
            )
    conn.commit()
    return stats


def fetch_mastery(client: RiotClient, conn, puuid: str, count: int = 20) -> int:
    try:
        top = client.mastery_top(puuid, count=count)
    except RiotError as exc:
        print(f"  ! 숙련도 조회 실패({exc.status})")
        return 0
    rows = [
        {
            "puuid": puuid,
            "champion_id": m.get("championId"),
            "champion_level": m.get("championLevel"),
            "champion_points": m.get("championPoints"),
            "last_play_time": m.get("lastPlayTime"),
            "fetched_at": now_iso(),
        }
        for m in top
    ]
    upsert_many(conn, "champion_mastery", rows, ["puuid", "champion_id"])
    conn.commit()
    return len(rows)


def probe_replays(client: RiotClient, conn, puuid: str) -> dict:
    """Match-V5 /replays 후보 경로를 1회씩 호출해 응답 구조를 raw 로 저장하고 요약 출력."""
    results = []
    for template in REPLAY_CANDIDATES:
        path = template.format(puuid=puuid)
        payload = client.get(path, host=config.REGIONAL, ttl=config.TTL_MEDIUM, allow_error=True)
        status = payload.get("_status", 200) if isinstance(payload, dict) else 200
        results.append({"path": path, "status": status, "payload": payload})
        if status == 200:
            break
    record = {"probed_at": now_iso(), "puuid": puuid, "results": results}
    # matches_raw 에 넣으면 reparse_all 이 이걸 매치로 착각한다. 별도 보관.
    conn.execute(
        "INSERT INTO static_data (key, json, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
        (f"replays_probe:{puuid[:12]}", json.dumps(record, ensure_ascii=False), now_iso()),
    )
    conn.commit()

    print("\n[replays 엔드포인트 탐색 결과]")
    for r in results:
        p = r["payload"]
        if r["status"] == 200:
            shape = _describe(p)
            print(f"  {r['path']} → 200 OK")
            print(f"    구조: {shape}")
            print(f"    앞부분: {json.dumps(p, ensure_ascii=False)[:400]}")
        else:
            body = p.get("_body") if isinstance(p, dict) else p
            print(f"  {r['path']} → {r['status']} :: {json.dumps(body, ensure_ascii=False)[:200]}")
    if not any(r["status"] == 200 for r in results):
        print("  ⇒ 200 을 주는 후보가 없습니다. 이 키/지역에서는 replays 엔드포인트를 쓸 수 없습니다.")
    return record


def _describe(obj, depth: int = 0) -> str:
    if depth > 2:
        return "…"
    if isinstance(obj, dict):
        return "{" + ", ".join(f"{k}: {_describe(v, depth + 1)}" for k, v in list(obj.items())[:12]) + "}"
    if isinstance(obj, list):
        return f"[{len(obj)}개 × {_describe(obj[0], depth + 1) if obj else '?'}]"
    return type(obj).__name__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="내 솔랭 데이터 수집")
    ap.add_argument("--limit", type=int, default=None, help="매치 수 제한 (테스트용)")
    ap.add_argument("--no-timeline", action="store_true", help="타임라인 생략")
    ap.add_argument("--no-metrics", action="store_true", help="지표 계산 생략")
    ap.add_argument("--no-replays-probe", action="store_true")
    ap.add_argument("--recompute", action="store_true", help="지표 전체 재계산")
    ap.add_argument("--queue", type=int, default=config.SOLO_QUEUE_ID, help="기본 솔랭(420)")
    ap.add_argument("--include-normals", action="store_true",
                    help=f"일반 게임도 수집 {config.NORMAL_QUEUE_IDS}")
    args = ap.parse_args(argv)

    st = config.load_settings()
    t_start = time.time()
    with session(st.db_path) as conn:
        client = RiotClient(st.api_key, conn)
        print(f"[1/6] {st.riot_id}#{st.riot_tag} 계정 조회")
        me = fetch_me(client, conn, st.riot_id, st.riot_tag)
        print(f"  puuid={me['puuid'][:16]}…  티어={me['tier']} {me['rank']} {me['lp']}LP")

        print(f"[2/6] 정적 데이터(Data Dragon)")
        ok = ddragon.ensure(conn)
        stat = ddragon.Static(conn)
        print(f"  {'버전 ' + str(stat.version) if ok and stat.available else '사용 불가 — 한글 이름/코어템 판정은 생략됩니다'}")

        since = st.season_start_kst
        queues = [args.queue] + (list(config.NORMAL_QUEUE_IDS) if args.include_normals else [])
        print(f"[3/6] {since:%Y-%m-%d %H:%M} KST 이후 큐 {queues} 매치 ID 조회")
        ids = all_match_ids(client, me["puuid"], st.season_start_epoch_s, queues,
                            max_matches=args.limit)
        print(f"  총 {len(ids)}판")
        set_state(conn, "me_match_ids", ids)
        conn.commit()

        print(f"[4/6] 매치 + 타임라인 수집")
        fstats = fetch_matches(client, conn, ids, with_timeline=not args.no_timeline, label="내 매치")

        print(f"[5/6] 챔피언 숙련도 top 20")
        n_mast = fetch_mastery(client, conn, me["puuid"])
        print(f"  {n_mast}개 저장")

        if not args.no_replays_probe:
            probe_replays(client, conn, me["puuid"])

        if not args.no_metrics:
            print(f"\n[6/6] 지표 계산")
            mres = metrics.compute_all(conn, recompute=args.recompute)
            print(f"  {mres['matches']}경기 / {mres['rows']}행")

        c = counts(conn)
        elapsed = time.time() - t_start
        print("\n" + "=" * 60)
        print(f"완료: {elapsed:.0f}초")
        print(f"  매치 ID {len(ids)} / 신규 매치 {fstats['new_match']} / 신규 타임라인 {fstats['new_timeline']}"
              f" / 캐시 {fstats['cached']} / 실패 {fstats['failed']}")
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
