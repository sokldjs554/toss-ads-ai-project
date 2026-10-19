"""GitHub Pages 사이트 조립 — 랜딩 + 브라우저 실행판(stlite) + 실제 트레이스 재생.

    python scripts/build_site.py --out _site

산출물:
  _site/index.html          랜딩
  _site/app/index.html      stlite 로 앱을 브라우저에서 실행하는 페이지
  _site/app/src.zip         앱 소스(app.py, core/, llm/, guardrails/, agents/, observability/, evals/, ui/, traces/, .streamlit/)
  _site/replay/index.html   실제 GPT-4o 트레이스·평가 결과 재생 (scripts/build_replay.py)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_INCLUDE = ["app.py", "core", "llm", "guardrails", "agents", "observability", "ops", "evals", "ui", "traces", ".streamlit"]
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache"}
EXCLUDE_FILES = {"evals/reports/latest.json", "evals/reports/redteam_latest.json"}
ALLOW_STREAMLIT = {".streamlit/config.toml"}  # .streamlit/ 에서는 설정 파일만. secrets.toml 은 어떤 경우에도 묶지 않는다


def _want(rel: Path) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
        return False
    if rel.suffix in {".pyc", ".png"}:
        return False
    if rel.as_posix() in EXCLUDE_FILES:
        return False
    if rel.parts[0] == ".streamlit" and rel.as_posix() not in ALLOW_STREAMLIT:
        return False
    if rel.name.startswith(".env") or "secret" in rel.name.lower():
        return False
    if rel.parts[0] == "traces" and not rel.name.startswith("sample_"):
        return False
    return True


def build_zip(out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for top in APP_INCLUDE:
            p = REPO / top
            files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())
            for f in files:
                rel = f.relative_to(REPO)
                if not _want(rel):
                    continue
                zf.write(f, rel.as_posix())
                n += 1
        sha = _build_sha()
        if sha:  # 브라우저 실행판 상단에 '빌드 <커밋>' 을 보여 주기 위한 표식 (app.py 가 읽는다)
            zf.writestr("build_info.txt", sha + "\n")
            n += 1
    return n


def _build_sha() -> str:
    sha = os.environ.get("GITHUB_SHA") or os.environ.get("PREFLIGHT_BUILD") or ""
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout
        except Exception:  # noqa: BLE001 — git 이 없거나 저장소가 아니면 표식 없이 빌드
            sha = ""
    return sha.strip()[:7]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "_site"))
    args = ap.parse_args()
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy(REPO / "site" / "index.html", out / "index.html")
    (out / "app").mkdir()
    shutil.copy(REPO / "site" / "app" / "index.html", out / "app" / "index.html")
    n = build_zip(out / "app" / "src.zip")
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_replay.py"), "--out", str(out / "replay")], check=True)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    size = (out / "app" / "src.zip").stat().st_size
    print(f"site -> {out}  (src.zip: {n} files, {size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
