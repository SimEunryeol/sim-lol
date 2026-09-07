"""네트워크 없이 파싱 → 지표 → 리포트 전 구간을 검증한다.

  python tests/test_pipeline.py [--keep] [--db PATH] [--report PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures  # noqa: E402
from sim_diamond import metrics, parse, report  # noqa: E402
from sim_diamond.db import (  # noqa: E402
    counts, now_iso, save_raw, session, upsert_many,
)
from sim_diamond.ddragon import _cache_put  # noqa: E402

ME = "ME_PUUID_0000"
JAN1_KST_MS = 1767193200000  # 2026-01-01 00:00 KST
DAY_MS = 86_400_000

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "OK  " if cond else "FAIL"
    if not cond:
        FAILURES.append(f"{name} {detail}")
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")


def seed_db(conn, n_me: int = 24, n_bench_players: int = 6, n_bench_matches: int = 4) -> None:
    _cache_put(conn, "ddragon_version", "15.18.1")
    _cache_put(conn, "champions", fixtures.DDRAGON_CHAMPIONS)
    _cache_put(conn, "items", fixtures.DDRAGON_ITEMS)

    players = [{
        "puuid": ME, "game_name": "F360", "tag_line": "KR1", "tier": "SILVER",
        "rank": "II", "lp": 42, "is_me": 1, "is_bench": 0, "fetched_at": now_iso(),
    }]

    # 내 매치: 정글 위주(12판) + 미드/탑 섞기, 4개월에 걸쳐 분산
    plan = [1] * 12 + [2] * 7 + [0] * 5
    for i in range(n_me):
        hero_index = plan[i % len(plan)]
        mid = f"KR_ME_{i:04d}"
        m, t = fixtures.make_match(
            mid, JAN1_KST_MS + i * 5 * DAY_MS, duration_s=1500 + (i % 5) * 240,
            hero_index=hero_index, hero_wins=(i % 5 != 0), seed=i, puuid_prefix="OPP",
            hero_puuid=ME,
        )
        save_raw(conn, "matches_raw", mid, m)
        save_raw(conn, "timelines_raw", mid, t)

    # 벤치: SILVER / GOLD 각 3명
    b = 0
    for tier in ("SILVER", "GOLD"):
        for k in range(n_bench_players // 2):
            hero_index = k % 5
            pos = fixtures.POSITIONS[hero_index]
            puuid = f"BENCH_{tier}_{k}"
            players.append({
                "puuid": puuid, "game_name": None, "tag_line": None, "tier": tier,
                "rank": "III", "lp": 30, "is_me": 0, "is_bench": 1, "fetched_at": now_iso(),
            })
            for j in range(n_bench_matches):
                mid = f"KR_B_{tier}_{k}_{j}"
                m, t = fixtures.make_match(
                    mid, JAN1_KST_MS + (b * 3 + j) * DAY_MS, duration_s=1620,
                    hero_index=hero_index, hero_wins=(j % 2 == 0), seed=1000 + b * 10 + j,
                    puuid_prefix=f"BOPP{tier}{k}", hero_puuid=puuid,
                )
                save_raw(conn, "matches_raw", mid, m)
                save_raw(conn, "timelines_raw", mid, t)
            b += 1
    upsert_many(conn, "players", players, ["puuid"])
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "test_sim.db"))
    ap.add_argument("--report", default=str(ROOT / "data" / "report_SAMPLE.md"))
    ap.add_argument("--keep", action="store_true", help="끝나도 테스트 DB 를 지우지 않는다")
    args = ap.parse_args()

    db = Path(args.db)
    if db.exists():
        db.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()

    with session(db) as conn:
        print("[1] 합성 raw 데이터 적재")
        seed_db(conn)
        c = counts(conn)
        check("raw 저장", c["matches_raw"] == 48, f"matches_raw={c['matches_raw']}")

        print("[2] raw → 파싱 (reparse_all)")
        res = parse.reparse_all(conn)
        c = counts(conn)
        print(f"  {res} / {c}")
        check("matches 파싱", c["matches"] == c["matches_raw"])
        check("participants 10명/판", c["participants"] == c["matches"] * 10)
        check("frames > 0", c["frames"] > 0, str(c["frames"]))
        check("events > 0", c["events"] > 0, str(c["events"]))

        row = conn.execute("SELECT * FROM matches LIMIT 1").fetchone()
        check("패치 파싱", row["patch"] == "15.18", str(row["patch"]))
        check("게임시간 초단위", 1000 < row["duration_s"] < 3000, str(row["duration_s"]))

        print("[3] 파싱 멱등성 (두 번 돌려도 행 수 동일)")
        before = counts(conn)
        parse.reparse_all(conn)
        after = counts(conn)
        check("멱등", before == after, f"{before} vs {after}")

        print("[4] 지표 계산")
        mres = metrics.compute_all(conn, verbose=False)
        print(f"  {mres}")
        c = counts(conn)
        check("participant_metrics 행", c["participant_metrics"] == 48, str(c["participant_metrics"]))
        check("deaths_detail 행 > 0", c["deaths_detail"] > 0, str(c["deaths_detail"]))

        m = dict(conn.execute(
            "SELECT * FROM participant_metrics WHERE puuid = ? AND match_id = 'KR_ME_0000'", (ME,)
        ).fetchone())
        print("  샘플(KR_ME_0000, 정글):")
        for k in ("team_position", "cs15", "cs15_diff", "gold15_diff", "level15_diff",
                  "vision_per_min", "first_control_ward_min", "deaths_before_15",
                  "deaths_warded", "deaths_unwarded", "obj_team_total", "obj_participated",
                  "obj_participation", "first_full_clear_min", "first_gank_min",
                  "first_counter_jungled_min", "jg_gold_diff_10", "enemy_jungle_minutes", "first_core_item_min",
                  "first_core_item_id", "back_count", "voluntary_back_count", "back_times_json"):
            print(f"    {k:24} = {m[k]}")

        check("정글로 인식", m["is_jungle"] == 1)
        check("첫 풀캠프 ~2.9분", 2.5 <= m["first_full_clear_min"] <= 3.2, str(m["first_full_clear_min"]))
        # 첫 갱 = 3분 이후 라인 구역에서 내가 딴 첫 킬/어시 → 4.2분 탑 킬.
        # 3.4분 내정글 데스는 첫 갱이 아니라 "첫 카정 피해"로 분리된다.
        check("첫 갱 4.2분(탑 킬)", abs(m["first_gank_min"] - 4.2) < 0.01, str(m["first_gank_min"]))
        check("첫 카정 피해 3.4분(내정글 데스)",
              abs(m["first_counter_jungled_min"] - 3.4) < 0.01, str(m["first_counter_jungled_min"]))
        check("적정글 체류 2분", m["enemy_jungle_minutes"] == 2, str(m["enemy_jungle_minutes"]))
        check("라인 상대 골드차 > 0", m["gold15_diff"] > 0, str(m["gold15_diff"]))
        check("코어템 = 월식(6692) 11.0분", m["first_core_item_id"] == 6692 and abs(m["first_core_item_min"] - 11.0) < 0.01,
              f"{m['first_core_item_id']} @ {m['first_core_item_min']}")
        check("첫 제어와드 5.6분", abs(m["first_control_ward_min"] - 5.6) < 0.01, str(m["first_control_ward_min"]))
        check("귀환(상점방문) 3회", m["back_count"] == 3, m["back_times_json"])
        objd = json.loads(m["obj_detail_json"])
        check("아군 오브젝트만 집계(4개)", m["obj_team_total"] == 4, json.dumps(objd, ensure_ascii=False))
        check("드래곤 처치자=나 → 참여", objd.get("DRAGON", [0, 0])[1] == 1, json.dumps(objd, ensure_ascii=False))
        check("바론 원거리 → 미참여", objd.get("BARON_NASHOR", [1, 1])[1] == 0, json.dumps(objd, ensure_ascii=False))

        dz = json.loads(m["deaths_zone_json"])
        check("데스 구역 분류", dz.get("OWN_JUNGLE") == 1 and dz.get("MID") == 1
              and dz.get("ENEMY_JUNGLE") == 1, json.dumps(dz, ensure_ascii=False))
        check("15분 이전 데스 2회", m["deaths_before_15"] == 2, str(m["deaths_before_15"]))
        warded = conn.execute(
            "SELECT seq, minute, zone, warded FROM deaths_detail "
            "WHERE match_id='KR_ME_0000' AND puuid=? ORDER BY seq", (ME,)
        ).fetchall()
        print("  데스 상세:", [dict(r) for r in warded])
        mid_death = [r for r in warded if r["zone"] == "MID"]
        check("미드 데스는 와드 있음(8.5분 설치 → 8.9분 사망)",
              bool(mid_death) and mid_death[0]["warded"] == 1,
              str([dict(r) for r in mid_death]))
        jg_death = [r for r in warded if r["zone"] == "ENEMY_JUNGLE"]
        check("적정글 데스는 와드 없음", bool(jg_death) and jg_death[0]["warded"] == 0,
              str([dict(r) for r in jg_death]))

        print("[5] 지표 재계산 멱등성")
        before = counts(conn)
        metrics.compute_all(conn, recompute=True, verbose=False)
        after = counts(conn)
        check("멱등", before == after, f"{before} vs {after}")

        print("[6] 리포트 생성")
        text = report.build(conn)
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text, encoding="utf-8")
        for heading in ("# 심은렬 다이아만들기 — 첫 진단 리포트", "## 1. 요약",
                        "## 2. 라인별 지표", "## 3. 챔피언별", "## 4. 데스 패턴",
                        "## 5. 정글 전용", "## 6. 시간 흐름", "## 7. 코치 메모"):
            check(f"섹션 {heading[:22]}", heading in text)
        check("벤치 SILVER 열", "SILVER 벤치" in text)
        check("벤치 GOLD 열", "GOLD 벤치" in text)
        check("한글 챔피언 이름", "리 신" in text)
        check("코치 메모 TODO", "TODO" in text)
        print(f"  리포트 {len(text.encode('utf-8'))} bytes → {args.report}")

    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()

    print()
    if FAILURES:
        print(f"실패 {len(FAILURES)}건:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
