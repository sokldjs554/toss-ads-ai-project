"""README 맨 위에 쓰는 짧은 움직이는 화면(docs/screenshots/demo.gif)을 만든다.

    pip install playwright pillow && python scripts/make_demo_gif.py [--out docs/screenshots/demo.gif]
실제 앱을 예시 실행(API 키 없음, 실제 AI 호출 없음)으로 띄워 e2e_flow.py 와 같은 길을 한 번 눌러 보고, 세 화면을 찍어 이어 붙인다:
예시 선택 → 결과(걸린 구절이 원문에 표시되고 이유가 붙음) → 문구를 고쳐 다시 검사. 화면 위 설명 띠는 캡처 직전에 페이지에 넣은 것이다.
기대한 글자가 보이지 않으면 실패(종료 코드 1)한다 — 화면이 바뀌었는데 오래된 모습의 GIF 를 조용히 내보내지 않기 위해서다.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("PREFLIGHT_GIF_PORT", "8633"))
EXAMPLE = "커머스 쿠폰 — 랜딩 페이지와 조건이 다름"
FIXED_TITLE, FIXED_BODY = "가을 신상품 입고", "5만원 이상 구매 시 전 상품 무료 배송"
SIZE = (1280, 780)
OUT_WIDTH = 900
SECONDS = (2.6, 3.4, 3.8)

BANNER_JS = """(text) => {
  let b = document.getElementById('demo-gif-banner');
  if (!b) { b = document.createElement('div'); b.id = 'demo-gif-banner';
    b.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;padding:12px 24px;background:#1b3a8a;color:#fff;font:600 20px/1.35 sans-serif;text-align:center';
    document.body.appendChild(b); }
  b.textContent = text; }"""


def capture(page, caption: str) -> "Image.Image":
    from PIL import Image

    page.evaluate(BANNER_JS, caption)
    time.sleep(0.4)
    return Image.open(io.BytesIO(page.screenshot(full_page=False))).convert("RGB")


def need(page, text: str, timeout: int = 180_000) -> None:
    page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)


def main() -> int:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "docs" / "screenshots" / "demo.gif"
    env = {**os.environ, "PREFLIGHT_FORCE_DEMO": "1"}
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", str(PORT),
                             "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    frames = []
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/_stcore/health", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        exe = os.environ.get("PREFLIGHT_CHROMIUM")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"], **({"executable_path": exe} if exe else {}))
            page = browser.new_context(viewport={"width": SIZE[0], "height": SIZE[1]}).new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load", timeout=60_000)
            page.get_by_role("button", name="예시로 바로 실행").wait_for(timeout=60_000)

            box = page.locator('[data-testid="stSelectbox"]').filter(has_text="예시 선택").first
            box.get_by_role("combobox").click()
            page.get_by_role("option").filter(has_text=EXAMPLE).first.click(timeout=15_000)
            time.sleep(1.5)
            page.evaluate("() => { const m = document.querySelector('[data-testid=\"stMain\"]'); if (m) m.scrollTo(0, 0); }")
            frames.append(capture(page, "① 예시를 고르고 ‘예시로 바로 실행’ — API 키 없이 돕니다"))

            page.get_by_role("button", name="예시로 바로 실행").click()
            need(page, "수정할 문구")
            time.sleep(1.5)
            page.get_by_text("광고안 1", exact=False).first.scroll_into_view_if_needed()
            page.evaluate("() => { const m = document.querySelector('[data-testid=\"stMain\"]'); if (m) m.scrollBy(0, -110); }")
            time.sleep(0.8)
            frames.append(capture(page, "② 확인이 필요한 구절을 원문에서 짚고, 이유를 붙입니다"))

            title = page.get_by_label("제목", exact=True).first
            title.scroll_into_view_if_needed()
            title.fill(FIXED_TITLE)
            title.press("Enter")
            time.sleep(1.2)
            body = page.get_by_label("본문", exact=True).first
            body.fill(FIXED_BODY)
            body.press("Control+Enter")
            time.sleep(1.2)
            page.get_by_role("button", name="수정한 문구 다시 검사").first.click()
            need(page, "재검사 결과", 120_000)
            time.sleep(1.5)
            if FIXED_BODY not in page.inner_text("body") or page.locator('[data-testid="stException"]').count():
                raise SystemExit("재검사 화면이 기대와 다릅니다 (고친 문구가 보이지 않거나 예외 표시)")
            # 이미 화면 안에 있어도 확실히 같은 자리에 오도록 직접 스크롤한다 (재검사 결과 제목이 위쪽, 지적 목록이 아래에 보이게).
            # Streamlit 은 창이 아니라 본문 영역(stMain)이 스크롤되므로 window.scrollBy 는 효과가 없다.
            page.get_by_text("재검사 결과", exact=False).first.evaluate(
                "e => { e.scrollIntoView({block: 'start'}); const m = document.querySelector('[data-testid=\"stMain\"]'); if (m) m.scrollBy(0, -130); }")
            time.sleep(0.8)
            frames.append(capture(page, "③ 고친 문구도 같은 기준으로 다시 검사 — 남은 문제와 이유를 보여 줍니다"))
            browser.close()
    finally:
        proc.terminate()

    h = round(SIZE[1] * OUT_WIDTH / SIZE[0])
    small = [f.resize((OUT_WIDTH, h), Image.LANCZOS).quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    out.parent.mkdir(parents=True, exist_ok=True)
    small[0].save(out, save_all=True, append_images=small[1:], duration=[int(s * 1000) for s in SECONDS], loop=0, optimize=True, disposal=1)
    print(f"{out} — {out.stat().st_size / 1024:.0f} KB, {len(small)} frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
