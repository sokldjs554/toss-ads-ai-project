"""개발용: 앱을 헤드리스로 띄워 탭별 스크린샷을 docs/screenshots/ 에 저장하고, 화면에 예외가 없는지 확인한다.
    pip install playwright && python scripts/screenshot.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
PORT = int(os.environ.get("PREFLIGHT_SHOT_PORT", "8611"))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PREFLIGHT_FORCE_DEMO": os.environ.get("PREFLIGHT_FORCE_DEMO", "1")}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", str(PORT),
                             "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        from playwright.sync_api import sync_playwright

        time.sleep(6)
        errors: list[str] = []
        with sync_playwright() as p:
            exe = None
            for cand in ("/opt/pw-browsers/chromium", None):
                exe = cand if cand and Path(cand).exists() else None
                if exe:
                    break
            browser = p.chromium.launch(headless=True, **({"executable_path": exe} if exe and Path(exe).is_file() else {}))
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(f"http://localhost:{PORT}", wait_until="networkidle", timeout=60_000)
            page.wait_for_selector("text=Preflight", timeout=60_000)
            time.sleep(2)
            page.screenshot(path=str(OUT / "01_studio_input.png"), full_page=False)

            # 악의적 요청 예시를 URL 로 열고 자동 실행
            page.goto(f"http://localhost:{PORT}/?preset=malicious&autorun=1", wait_until="networkidle", timeout=60_000)
            page.wait_for_selector("text=소재별 사전점검 결과", timeout=120_000)
            time.sleep(2)
            # Streamlit 본문은 내부 스크롤이라 full_page 가 안 먹는다 → 뷰포트를 길게 늘려 찍고 PIL 로 잘라 낸다
            page.set_viewport_size({"width": 1440, "height": 4200})
            time.sleep(2.5)
            page.screenshot(path=str(OUT / "_studio_tall.png"), full_page=False)
            anchors = {}
            for key, text in (("intercept", "Safety Intercept"), ("plan", "Planner 생각 과정"), ("variants", "소재별 사전점검 결과"), ("fm", "이번 실행에서 밟은 실패 모드")):
                loc = page.get_by_text(text, exact=False).first
                try:
                    box = loc.bounding_box(timeout=5_000)
                    anchors[key] = int(box["y"]) if box else None
                except Exception:  # noqa: BLE001
                    anchors[key] = None
            from PIL import Image

            im = Image.open(OUT / "_studio_tall.png")
            w, h = im.size
            y0 = max(0, (anchors.get("intercept") or 900) - 20)
            y1 = min(h, (anchors.get("variants") or y0 + 1200) + 10)
            im.crop((300, y0, w, y1)).save(OUT / "02_studio_intercept_plan.png")
            y2 = max(0, (anchors.get("variants") or 1800) - 10)
            y3 = min(h, (anchors.get("fm") or y2 + 1700))
            im.crop((300, y2, w, y3)).save(OUT / "02b_studio_variants.png")
            (OUT / "_studio_tall.png").unlink(missing_ok=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            time.sleep(1.5)

            def tab(name: str, file: str, settle: float = 2.5, full: bool = True):
                page.get_by_role("tab", name=name).click()
                time.sleep(settle)
                page.screenshot(path=str(OUT / file), full_page=full)

            tab("🔍 관측 (Trace)", "03_trace.png")
            page.get_by_text("저장된 기준선 (demo)").click()
            time.sleep(2.5)
            page.screenshot(path=str(OUT / "04_evals.png"), full_page=True)
            tab("🧨 레드팀", "05_redteam.png")
            tab("⚠️ 실패 모드", "06_failure_modes.png", full=False)
            tab("📜 정책 규칙", "07_policy.png", full=False)
            body = page.inner_text("body")
            for marker in ("Traceback", "Error:", "StreamlitAPIException", "AttributeError", "TypeError", "KeyError"):
                if marker in body:
                    errors.append(marker)
            browser.close()
        if errors:
            print("화면에 오류 표시:", errors)
            return 1
        print("screenshots:", sorted(p.name for p in OUT.glob("*.png")))
        return 0
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        bad = [l for l in (out or "").splitlines() if "Traceback" in l or "Error" in l]
        if bad:
            print("server log:", bad[:10])


if __name__ == "__main__":
    sys.exit(main())
