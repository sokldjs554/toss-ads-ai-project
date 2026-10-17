"""개발용: 앱을 헤드리스로 띄워 주요 화면(데스크톱·모바일)을 docs/screenshots/ 에 저장하고, 가로 넘침·예외가 없는지 확인한다.
    pip install playwright pillow && python scripts/screenshot.py
크로미움 경로가 기본과 다르면 PREFLIGHT_CHROMIUM=/path/to/chrome 으로 지정한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
PORT = int(os.environ.get("PREFLIGHT_SHOT_PORT", "8611"))

PLAN = [
    {"name": "01_workbench_input", "path": "/", "wait": "광고 만들기", "viewport": (1400, 1000)},
    {"name": "02_result_malicious", "path": "/?preset=malicious&autorun=1", "wait": "검사 결과", "viewport": (1400, 1000), "tall": 5200, "crop": ("검사 결과", 1750)},
    {"name": "03_variant_reject", "path": "/?preset=malicious&autorun=1", "wait": "검사 결과", "viewport": (1400, 1000), "tall": 5200, "crop": ("광고안 3", 1500)},
    {"name": "04_recheck", "path": "/?preset=malicious&autorun=1", "wait": "검사 결과", "viewport": (1400, 1000), "tall": 5200,
     "steps": [("fill_last", "제목", "조건 확인 후 신청하는 신용대출"), ("click_last", "수정한 문구 다시 검사", "재검사 결과")], "crop": ("문구 고쳐서 다시 검사", 1150)},
    {"name": "05_cases", "path": "/?view=검사 사례", "wait": "검사 사례", "viewport": (1400, 1000), "tall": 3600, "crop": (None, 1500)},
    {"name": "06_quality", "path": "/?view=품질 확인", "wait": "품질 확인", "viewport": (1400, 1000), "tall": 3600, "crop": (None, 1650)},
    {"name": "07_mobile_result", "path": "/?preset=malicious&autorun=1", "wait": "검사 결과", "viewport": (390, 844), "tall": 6500, "crop": ("검사 결과", 2300)},
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PREFLIGHT_FORCE_DEMO": "1"}
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", str(PORT),
                             "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    report = []
    try:
        from PIL import Image
        from playwright.sync_api import sync_playwright

        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/_stcore/health", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        exe = os.environ.get("PREFLIGHT_CHROMIUM")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"], **({"executable_path": exe} if exe else {}))
            for item in PLAN:
                w, h = item["viewport"]
                ctx = browser.new_context(viewport={"width": w, "height": h})
                page = ctx.new_page()
                errs: list[str] = []
                page.on("pageerror", lambda e: errs.append(str(e)[:200]))
                page.goto(f"http://127.0.0.1:{PORT}{item['path']}", wait_until="load", timeout=60_000)
                page.get_by_text(item["wait"], exact=False).first.wait_for(timeout=180_000)
                for step in item.get("steps", []):
                    if step[0] == "fill_last":
                        loc = page.get_by_label(step[1]).last
                        loc.fill(step[2])
                        loc.press("Enter")
                        time.sleep(1.5)
                    elif step[0] == "click_last":
                        page.get_by_role("button", name=step[1]).last.click(timeout=15_000)
                        page.get_by_text(step[2], exact=False).first.wait_for(timeout=120_000)
                        time.sleep(2)
                time.sleep(2.5)
                if item.get("tall"):
                    page.set_viewport_size({"width": w, "height": item["tall"]})
                    time.sleep(2.5)
                tmp = OUT / f"_{item['name']}.png"
                page.screenshot(path=str(tmp), full_page=False)
                anchor, height = item.get("crop", (None, None))
                y0 = 0
                if anchor:
                    try:
                        box = page.get_by_text(anchor, exact=False).first.bounding_box(timeout=5_000)
                        y0 = max(0, int(box["y"]) - 16) if box else 0
                    except Exception:  # noqa: BLE001
                        y0 = 0
                im = Image.open(tmp)
                if height:
                    im = im.crop((0, y0, im.size[0], min(im.size[1], y0 + height)))
                im.save(OUT / f"{item['name']}.png")
                tmp.unlink(missing_ok=True)
                sw, iw = page.evaluate("document.documentElement.scrollWidth"), page.evaluate("window.innerWidth")
                report.append({"name": item["name"], "overflow": sw > iw, "exceptions": page.locator('[data-testid="stException"]').count(), "errors": errs})
                ctx.close()
            browser.close()
    finally:
        proc.terminate()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    bad = [r for r in report if r["overflow"] or r["exceptions"] or r["errors"]]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
