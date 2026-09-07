"""로컬 대시보드 (FastAPI).

  python -m sim_diamond.web              # http://127.0.0.1:8765
  scripts\\web.bat                        # 더블클릭용

화면은 4개(오늘/탐색/진단/코치)이고 한 페이지에서 탭으로 넘긴다. 데이터는 아래 5개
엔드포인트로만 가져온다.

  GET  /today     오늘 할 것 — 지금 탐색 라인·기준 챔프·이번 주 과제·평점 안 남긴 경기
  GET  /explore   탐색 진행 현황 (explore.summarize 그대로)
  GET  /report    진단 리포트 마크다운 전문
  GET  /coach     최신 코치 메모
  POST /rate      경기 재미 점수(1~5) 기록

개인용 로컬 도구다. 인증이 없으므로 **127.0.0.1 에만 바인딩한다.**
"""
from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import coach, config, explore, rate, report
from .db import now_iso, session, upsert
from .ddragon import Static

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765
# 인증이 없는 개인용 도구다. 루프백 밖으로 열면 같은 네트워크의 누구나 볼 수 있다.
DEFAULT_HOST = "127.0.0.1"

app = FastAPI(title="sim-diamond 대시보드", docs_url=None, redoc_url=None)


def _db() -> str:
    return config.load_settings(require_key=False).db_path


def _clean(v):
    """pandas/numpy 값을 JSON 으로 낼 수 있게 바꾼다.

    지표에는 표본이 없어 NaN 인 칸이 많다. NaN 은 JSON 표준이 아니라 그대로 내보내면
    브라우저가 파싱에 실패한다. null 로 바꿔서 화면에서 "—" 로 그리게 한다.
    """
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_clean(x) for x in v]
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    return v


def _ko_map(static: Static) -> dict[str, str]:
    """영문 championName → 한글 이름. 정적 데이터가 없으면 빈 dict."""
    return {en: static.champ_ko.get(cid, en)
            for cid, en in static.champ_en.items() if en}


def _me(conn):
    row = conn.execute("SELECT * FROM players WHERE is_me = 1").fetchone()
    if not row:
        raise HTTPException(404, "내 계정이 없습니다. python -m sim_diamond.collect_me 를 먼저 실행하세요.")
    return row


def _recent(conn, puuid: str, static: Static, limit: int = 10) -> list[dict]:
    out = []
    for m in rate.recent_matches(conn, puuid, limit):
        start = m["game_start"] or 0
        out.append({
            "match_id": m["match_id"],
            "when": datetime.fromtimestamp(start / 1000, config.KST).strftime("%m-%d %H:%M"),
            "game_start": start,
            "champion": m["champion_name"],
            "champion_ko": static.champion(m["champion_id"], m["champion_name"]),
            "role": m["team_position"] or "?",
            "win": bool(m["win"]),
            "kda": f"{m['kills']}/{m['deaths']}/{m['assists']}",
            "score": m["score"],
            "memo": m["memo"],
        })
    return out


@app.get("/today")
def today():
    with session(_db()) as conn:
        static = Static(conn)
        me = _me(conn)
        summary = explore.summarize(conn)
        recent = _recent(conn, me["puuid"], static, 10)
        memo = coach.latest_memo(conn)

        cur = None
        if summary:
            cur = next((r for r in summary["reports"] if r["is_current"]), None)
        start_ts = cur["start_ts"] if cur else 0
        # 단계 시작 이후 경기 중 아직 재미 점수가 없는 것 — 30초 루틴이 밀린 판이다.
        unrated = [m for m in recent if m["game_start"] >= start_ts and m["score"] is None]

        return _clean({
            "riot_id": f"{me['game_name']}#{me['tag_line']}",
            "phase": {
                "name": summary["phase_name"] if summary else None,
                "done": summary["done"] if summary else 0,
                "target": summary["target"] if summary else 0,
                "order": summary["order"] if summary else [],
                "current_role": summary["current_role"] if summary else None,
                "current_games": cur["games"] if cur else 0,
                "current_left": cur["left"] if cur else 0,
                "current_target": cur["target"] if cur else 0,
                "current_champs": cur["champs"] if cur else [],
                "complete": summary["complete"] if summary else False,
            } if summary else None,
            "weekly_task": coach.WEEKLY_TASK,
            "memo_headline": (memo["memo"].strip().splitlines()[0] if memo else None),
            "recent": recent,
            "unrated": unrated,
            "champ_ko": _ko_map(static),
            # 나중에 Data Dragon CDN 주소를 만들 때 쓴다. 없으면 화면은 플레이스홀더로 둔다.
            "ddragon_version": static.version,
        })


@app.get("/explore")
def explore_status():
    with session(_db()) as conn:
        summary = explore.summarize(conn)
        if not summary:
            raise HTTPException(404, "등록된 탐색 단계가 없습니다. phase init-explore 를 먼저 실행하세요.")
        summary["champ_ko"] = _ko_map(Static(conn))
        return _clean(summary)


@app.get("/report")
def report_markdown():
    with session(_db()) as conn:
        return {"markdown": report.build(conn), "generated_at": now_iso()}


@app.get("/coach")
def coach_memo():
    with session(_db()) as conn:
        memo = coach.latest_memo(conn)
        if not memo:
            raise HTTPException(404, "저장된 코치 메모가 없습니다. python -m sim_diamond.coach 를 실행하세요.")
        return _clean({**dict(memo), "weekly_task": coach.WEEKLY_TASK})


class RateIn(BaseModel):
    score: int = Field(ge=1, le=5)
    memo: str | None = None
    match_id: str | None = None


@app.post("/rate")
def rate_match(body: RateIn):
    with session(_db()) as conn:
        me = _me(conn)
        recents = rate.recent_matches(conn, me["puuid"], 20)
        if not recents:
            raise HTTPException(404, "수집된 경기가 없습니다.")
        target = (next((m for m in recents if m["match_id"] == body.match_id), None)
                  if body.match_id else recents[0])
        if target is None:
            raise HTTPException(404, f"{body.match_id} 을(를) 최근 경기에서 찾지 못했습니다.")
        upsert(conn, "self_rating", {
            "match_id": target["match_id"], "puuid": me["puuid"],
            "score": body.score, "memo": body.memo, "created_at": now_iso(),
        }, ["match_id", "puuid"])
        conn.commit()
        return {"match_id": target["match_id"], "score": body.score,
                "memo": body.memo, "was": target["score"]}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="로컬 대시보드")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST, help="인증이 없으므로 기본값을 바꾸지 마세요")
    args = ap.parse_args(argv)

    import uvicorn
    print(f"대시보드: http://{args.host}:{args.port}  (끄려면 Ctrl+C)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
