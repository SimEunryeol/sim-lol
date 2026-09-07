"""진단 결과를 파일 하나로 모은다 (GitHub 에 올려 공유하기 위한 것).

  python -m sim_diamond.status

doctor / check / explore / 환경정보를 실행해 logs/status.txt 로 저장한다.
API 키처럼 보이는 문자열은 저장 전에 전부 가린다.
"""
from __future__ import annotations

import io
import os
import platform
import re
import subprocess
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

from . import config

# 저장 전에 가릴 패턴. 실수로도 키가 파일에 남지 않게 한다.
SECRET_PATTERNS = [
    re.compile(r"RGAPI-[0-9a-fA-F-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
]


def scrub(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0)[:6] + "…가림…" + m.group(0)[-4:], text)
    return text


def run_module(name: str, argv: list[str] | None = None) -> str:
    """모듈을 같은 프로세스에서 돌리고 출력을 문자열로 받는다."""
    buf = io.StringIO()
    try:
        mod = __import__(f"sim_diamond.{name}", fromlist=["main"])
        with redirect_stdout(buf), redirect_stderr(buf):
            mod.main(argv or [])
    except SystemExit as exc:
        buf.write(f"\n(종료코드 {exc.code})\n")
    except Exception as exc:  # 하나가 죽어도 나머지는 모은다
        buf.write(f"\n!! {name} 실행 중 오류: {exc.__class__.__name__}: {exc}\n")
    return buf.getvalue()


def shell(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           cwd=str(config.ROOT))
        return (r.stdout + r.stderr).strip() or "(출력 없음)"
    except Exception as exc:
        return f"(실행 실패: {exc})"


def collect() -> str:
    parts = [
        "=" * 70,
        f"sim-diamond 상태  ·  {datetime.now():%Y-%m-%d %H:%M:%S}",
        "=" * 70,
        f"파이썬 {sys.version.split()[0]} · {platform.system()} {platform.release()}",
        f"작업 폴더 {config.ROOT}",
        f"DB 존재 여부 {(config.DATA_DIR / 'sim.db').exists()}",
        "",
        "--- git ---",
        "브랜치: " + shell(["git", "branch", "--show-current"]),
        "최근 커밋: " + shell(["git", "log", "--oneline", "-1"]),
        "변경된 파일:\n" + shell(["git", "status", "--short"]),
        "",
        "--- 설치된 모듈 ---",
        "coach.py 있음: " + str((config.ROOT / "sim_diamond" / "coach.py").exists()),
        "explore.py 있음: " + str((config.ROOT / "sim_diamond" / "explore.py").exists()),
        "",
        "=" * 70,
        "doctor",
        "=" * 70,
        run_module("doctor"),
        "=" * 70,
        "check",
        "=" * 70,
        run_module("check"),
        "=" * 70,
        "explore",
        "=" * 70,
        run_module("explore"),
    ]
    return scrub("\n".join(parts))


def main(argv: list[str] | None = None) -> int:
    out_dir = config.ROOT / "logs"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "status.txt"
    text = collect()
    path.write_text(text, encoding="utf-8")
    print(text)
    print("\n" + "=" * 70)
    print(f"저장: {path}")
    print("API 키로 보이는 문자열은 전부 가려서 저장했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
