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


def seed_db(conn, n_me: int = 24, n_bench_matches: int = 4) -> None:
    _cache_put(conn, "ddragon_version", "15.18.1")
    _cache_put(conn, "champions", fixtures.DDRAGON_CHAMPIONS)
    _cache_put(conn, "items", fixtures.DDRAGON_ITEMS)

    players = [{
        "puuid": ME, "game_name": "F360", "tag_line": "KR1", "tier": "SILVER",
        "rank": "II", "lp": 42, "is_me": 1, "is_bench": 0, "fetched_at": now_iso(),
    }]

    # 내 매치: 5개 라인 전부 (정글 11 / 미드 6 / 탑 3 / 원딜 2 / 서폿 2), 4개월에 분산.
    # ROLE_SPEC 이 라인마다 다르므로 전 라인을 태워야 리포트 코드가 다 검증된다.
    plan = [1] * 11 + [2] * 6 + [0] * 3 + [3] * 2 + [4] * 2
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

    # 벤치: BRONZE / SILVER / GOLD 각 5명(라인당 1명) — 라인별 벤치 슬라이스를 다 채운다
    b = 0
    for tier in ("BRONZE", "SILVER", "GOLD"):
        for k in range(5):
            hero_index = k
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
    upsert_many(conn, "champion_mastery", [
        {"puuid": ME, "champion_id": 64, "champion_level": 7, "champion_points": 123456,
         "last_play_time": 0, "fetched_at": now_iso()},
        {"puuid": ME, "champion_id": 103, "champion_level": 5, "champion_points": 45678,
         "last_play_time": 0, "fetched_at": now_iso()},
    ], ["puuid", "champion_id"])
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
        check("raw 저장", c["matches_raw"] == 24 + 15 * 4, f"matches_raw={c['matches_raw']}")

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
        check("participant_metrics 행", c["participant_metrics"] == 24 + 15 * 4,
              str(c["participant_metrics"]))
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
        # 정글 CS 곡선 3분=16, 4분=22 → 임계값 20 을 3.67분에 넘는다
        check("첫 풀캠프 3.67분(보간)", abs(m["first_full_clear_min"] - 3.67) < 0.02,
              str(m["first_full_clear_min"]))
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
        # 상점 방문 4회 중 3회는 데스 직후(부활), 20.5분 1회만 자발적 귀환
        check("상점 방문 4회", m["back_count"] == 4, str(m["back_count"]))
        check("자발적 귀환 1회 @20.5분",
              m["voluntary_back_count"] == 1 and json.loads(m["back_times_json"]) == [20.5],
              f"{m['voluntary_back_count']} {m['back_times_json']}")
        objd = json.loads(m["obj_detail_json"])
        check("아군 오브젝트만 집계(5개)", m["obj_team_total"] == 5, json.dumps(objd, ensure_ascii=False))
        check("드래곤 2개 중 내가 딴 1개만 참여", objd.get("DRAGON", [0, 0]) == [2, 1],
              json.dumps(objd, ensure_ascii=False))
        check("바론 원거리 → 미참여", objd.get("BARON_NASHOR", [1, 1])[1] == 0, json.dumps(objd, ensure_ascii=False))
        # 10분 드래곤은 약 2000 떨어져 있다 → 800/1200 에서는 빠지고 2500 에서만 잡힌다
        check("반경별 참여율이 갈린다",
              m["obj_participation_800"] == 0.4 and m["obj_participation_1200"] == 0.4
              and m["obj_participation_2500"] == 0.6,
              f"800={m['obj_participation_800']} 1200={m['obj_participation_1200']} "
              f"2500={m['obj_participation_2500']}")

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

        print("[6] 탐색 단계 + 재미 점수 + explore")
        from sim_diamond import explore, phase, rate  # noqa: E402
        from sim_diamond.db import upsert as _up  # noqa: E402
        champs_by_role = {"TOP": ["Garen"], "JUNGLE": ["LeeSin"], "MIDDLE": ["Ahri"],
                          "BOTTOM": ["Jinx"], "UTILITY": ["Thresh"]}
        for role, champs in champs_by_role.items():
            phase.add_phase(conn, "탐색", role, champs, 15, "2026-01-01")
        conn.commit()

        recents = rate.recent_matches(conn, ME, limit=3)
        check("최근 경기 조회", len(recents) == 3, str(len(recents)))
        # 정글 경기 3판에만 점수를 준다 → 정글 평균 4.0, 다른 라인은 미기록
        for i, score in enumerate((5, 4, 3)):
            _up(conn, "self_rating", {"match_id": f"KR_ME_{i:04d}", "puuid": ME,
                                      "score": score, "memo": f"메모{i}",
                                      "created_at": now_iso()}, ["match_id", "puuid"])
        conn.commit()

        summ = explore.summarize(conn)
        check("탐색 단계 인식", summ is not None and summ["phase_name"] == "탐색")
        by_role = {r["role"]: r for r in summ["reports"]}
        for role, r in by_role.items():
            print(f"  {role:<8} 진행 {r['games']:>2}/{r['target']} 위반 {r['violations']} "
                  f"재미 {r['fun_avg']} 지표 {r['metric_score']} 적합도 {r['fit']}")
        check("5개 라인 전부 집계", len(summ["reports"]) == 5, str(len(summ["reports"])))
        check("정글 11판", by_role["JUNGLE"]["games"] == 11, str(by_role["JUNGLE"]["games"]))
        check("서폿 2판", by_role["UTILITY"]["games"] == 2, str(by_role["UTILITY"]["games"]))
        check("기준 챔프 위반 0", all(r["violations"] == 0 for r in summ["reports"]),
              str({k: v["violation_champs"] for k, v in by_role.items()}))
        check("정글 재미 평균 4.0 (5,4,3)", by_role["JUNGLE"]["fun_avg"] == 4.0,
              str(by_role["JUNGLE"]["fun_avg"]))
        check("정글 재미 기록 3판", by_role["JUNGLE"]["fun_rated"] == 3,
              str(by_role["JUNGLE"]["fun_rated"]))
        check("미드는 재미 미기록", by_role["MIDDLE"]["fun_avg"] is None)
        check("적합도 0~1", all(r["fit"] is None or 0 <= r["fit"] <= 1 for r in summ["reports"]))
        check("목표 미달이면 complete=False", summ["complete"] is False,
              f"{summ['done']}/{summ['target']}")

        # 기준 챔프를 바꾸면 위반으로 잡히는지
        phase.add_phase(conn, "탐색", "JUNGLE", ["Rammus"], 15, "2026-01-01")
        conn.commit()
        viol = next(r for r in explore.summarize(conn)["reports"] if r["role"] == "JUNGLE")
        check("기준 챔프 외 플레이 = 위반으로 집계, 진행 판수에서 제외",
              viol["violations"] == 11 and viol["violation_champs"] == ["LeeSin"]
              and viol["games"] == 0 and viol["played_total"] == 11,
              f"위반 {viol['violations']} 진행 {viol['games']} 총 {viol['played_total']}")
        phase.add_phase(conn, "탐색", "JUNGLE", ["LeeSin"], 15, "2026-01-01")
        conn.commit()

        # 탐색 순서(탑→미드→원딜→서폿→정글): 탑이 15판을 못 채웠는데 정글을 하면 순서 위반
        by_r = {r["role"]: r for r in summ["reports"]}
        check("탐색 순서대로 정렬", [r["role"] for r in summ["reports"]] == phase.ROLE_ORDER,
              str([r["role"] for r in summ["reports"]]))
        check("현재 탐색 라인 = 탑(3/15)", summ["current_role"] == "TOP"
              and summ["current_left"] == 12, f"{summ['current_role']} {summ['current_left']}")
        check("정글 11판은 전부 순서 위반", by_r["JUNGLE"]["order_violations"] == 11,
              str(by_r["JUNGLE"]["order_violations"]))
        check("탑은 순서 위반 아님", by_r["TOP"]["order_violations"] == 0,
              str(by_r["TOP"]["order_violations"]))
        check("순서 위반도 진행 판수에는 포함", by_r["JUNGLE"]["games"] == 11,
              str(by_r["JUNGLE"]["games"]))

        # 단계 시작 시각을 뒤로 밀면 그 이전 경기는 탐색에서 빠져야 한다
        mid = conn.execute("SELECT game_start FROM participant_metrics WHERE puuid=? "
                           "AND team_position='JUNGLE' ORDER BY game_start", (ME,)).fetchall()
        cut = mid[5]["game_start"]
        phase.add_phase(conn, "탐색", "JUNGLE", ["LeeSin"], 15, "2026-01-01", start_ts=cut)
        conn.commit()
        after = next(r for r in explore.summarize(conn)["reports"] if r["role"] == "JUNGLE")
        check("시작 시각 이전 경기는 탐색에서 제외", after["games"] == 11 - 5,
              f"{after['games']} (전체 11판 중 6번째부터)")
        phase.add_phase(conn, "탐색", "JUNGLE", ["LeeSin"], 15, "2026-01-01")
        conn.commit()

        print("[7] 리포트 생성")
        text = report.build(conn)
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text, encoding="utf-8")
        for heading in ("# 심은렬 다이아만들기 — 첫 진단 리포트", "## 1. 요약",
                        "## 2. 라인별 지표", "## 3. 챔피언별", "## 4. 데스 패턴",
                        "## 5. 탐색 단계 진행 현황", "## 6. 시간 흐름", "## 7. 코치 메모"):
            check(f"섹션 {heading[:22]}", heading in text)
        for lane in ("### 탑", "### 정글", "### 미드", "### 원딜", "### 서폿"):
            check(f"라인 섹션 {lane}", lane in text)
        check("정글 전용 지표 표", "**정글 전용 지표**" in text)
        check("서폿 전용 지표 표", "**서폿 전용 지표**" in text)
        check("표본 부족 표기", "표본 부족" in text)
        check("숙련도 열", "숙련도 점수" in text)
        check("숙련도 점수 값 표시", "123,456" in text)
        check("탐색 진행률 표시", "진행 **" in text)
        check("월별 라인 비율", "| 컨트롤 와드 |" in text)
        check("벤치 SILVER 열", "SILVER 벤치" in text)
        check("벤치 GOLD 열", "GOLD 벤치" in text)
        check("한글 챔피언 이름", "리 신" in text)
        check("코치 메모 미생성 안내", "python -m sim_diamond.coach" in text)
        # 메모가 저장되면 리포트 7번에 실제로 실린다
        from sim_diamond import coach  # noqa: E402
        coach.save(conn, "테스트 메모 본문", "claude-opus-5", 100, 200)
        conn.commit()
        text2 = report.build(conn)
        check("저장된 코치 메모가 리포트에 실림", "테스트 메모 본문" in text2)
        check("메모 없는 리포트도 만들 수 있음(코치 입력용)",
              "테스트 메모 본문" not in report.build(conn, include_memo=False))
        check("코치 프롬프트에 75판 규칙 명시", "75판" in coach.SYSTEM_PROMPT)
        check("코치 프롬프트 존댓말 강제 + 예시",
              "존댓말" in coach.SYSTEM_PROMPT and "제어 와드를 1개 사세요" in coach.SYSTEM_PROMPT)
        check("코치 프롬프트 과제 1개 강제", "**딱 하나만**" in coach.SYSTEM_PROMPT)
        check("코치 프롬프트 우선순위 2·3은 다음 주 이후",
              "다음 주 이후 후보" in coach.SYSTEM_PROMPT)
        check("코치 프롬프트 분량 상한 900자",
              coach.MAX_CHARS == 900 and "900자 이내" in coach.SYSTEM_PROMPT)
        check("코치 프롬프트 현재 라인 명시 요구", "지금 탐색 중인 라인" in coach.SYSTEM_PROMPT)
        content = coach.build_user_content("(리포트)", explore.summarize(conn))
        check("사용자 입력에 현재 라인·남은 판수 주입",
              "지금 탐색 중인 라인" in content and "라인 추천 금지" in content,
              content.split("---- 리포트")[0][-200:])
        check("코치 프롬프트에 추정 지표 주의", "_(추정)_" in coach.SYSTEM_PROMPT)
        check("코치 프롬프트에 탐색 진행 현황 항목", "탐색 단계 진행 현황" in coach.SYSTEM_PROMPT)
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
