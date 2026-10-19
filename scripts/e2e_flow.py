"""실제 사용 흐름을 브라우저로 끝까지 누른다 (데스크톱 + 모바일): 예시 실행 → 결과 확인 → 문구 수정 → 재검사 →
(모델 호출이 실패하는 상황에서) 확인 요청 생성 → 같은 문구로 다시 시도. 각 단계의 화면 상태를 검증하고 스크린샷을 남긴다.

    pip install playwright pillow && python scripts/e2e_flow.py [--out docs/screenshots]
화면 캡처만으로 검증을 대신하지 않기 위한 스크립트다: 단계마다 기대한 글자가 보이는지, 예외 표시와 가로 넘침이 없는지, 스타일이 실제로 적용됐는지 확인하고 하나라도 어긋나면 실패(종료 코드 1)한다.
실행 결과(검사 목록과 통과 여부)는 <out>/e2e_report.json 에 남는다. flow_*.png 열 장(다섯 단계 × 데스크톱·모바일)이 이 스크립트가 찍은 화면이고, 01·02·03·04·05_cases·06_quality·07_mobile_result·08_compare 는 scripts/screenshot.py 가 찍는다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("PREFLIGHT_E2E_PORT", "8621"))
EXAMPLE = "커머스 쿠폰 — 랜딩 페이지와 조건이 다름"
VIEWPORTS = {"desktop": (1280, 900), "mobile": (390, 844)}


class Flow:
    def __init__(self, page, name: str, out: Path):
        self.page, self.name, self.out = page, name, out
        self.checks: list[dict] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"viewport": self.name, "check": label, "ok": bool(ok), "detail": detail})
        print(("  ok   " if ok else "  FAIL ") + f"[{self.name}] {label}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:   # 실패 시점의 화면 글자와 캡처를 남겨 원인을 볼 수 있게 한다
            self.out.mkdir(parents=True, exist_ok=True)
            (self.out / f"_fail_{self.name}.txt").write_text(self.text(), encoding="utf-8")
            self.page.screenshot(path=str(self.out / f"_fail_{self.name}.png"), full_page=True)

    def shot(self, step: str, anchor: str | None = None) -> None:
        """anchor 글자가 있는 곳으로 스크롤한 뒤 화면을 저장한다 (결과가 첫 화면 밖에 있어도 그 결과가 찍히게)."""
        self.out.mkdir(parents=True, exist_ok=True)
        if anchor:
            self.page.get_by_text(anchor, exact=False).first.scroll_into_view_if_needed()
            self.page.evaluate("window.scrollBy(0, -40)")
            time.sleep(0.8)
        self.page.screenshot(path=str(self.out / f"flow_{self.name}_{step}.png"), full_page=False)

    def text(self) -> str:
        return self.page.inner_text("body")

    def wait_text(self, needle: str, timeout: int = 180_000) -> None:
        self.page.get_by_text(needle, exact=False).first.wait_for(timeout=timeout)

    def no_exception(self, label: str) -> None:
        self.check(f"{label}: 예외 표시 없음", self.page.locator('[data-testid="stException"]').count() == 0)
        sw, iw = self.page.evaluate("document.documentElement.scrollWidth"), self.page.evaluate("window.innerWidth")
        self.check(f"{label}: 가로 넘침 없음", sw <= iw + 1, f"scrollWidth {sw} > innerWidth {iw}")

    def pick(self, label: str, option: str) -> None:
        """Streamlit 선택 상자에서 항목을 고른다 (라벨 글자로 상자를 찾고, 펼쳐진 목록에서 항목을 클릭)."""
        box = self.page.locator('[data-testid="stSelectbox"]').filter(has_text=label).first
        box.scroll_into_view_if_needed()
        box.get_by_role("combobox").click()
        self.page.get_by_role("option").filter(has_text=option).first.click(timeout=15_000)
        time.sleep(1.2)


def run_flow(browser, name: str, size: tuple[int, int], out: Path) -> list[dict]:
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))
    f = Flow(page, name, out)
    print(f"== {name} {size[0]}x{size[1]}")
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load", timeout=60_000)
    f.wait_text("세 가지 결과는 서로 다릅니다", 90_000)
    page.get_by_role("button", name="예시로 바로 실행").wait_for(timeout=30_000)   # 버튼까지 그려진 뒤에 글자를 읽는다
    t = f.text()
    f.check("첫 화면: 시작하기·예시 선택·바로 실행이 위에 있다", "시작하기" in t and "예시로 바로 실행" in t and "세 가지 결과는 서로 다릅니다" in t)
    f.no_exception("첫 화면")
    # 스타일이 실제로 적용되는지(본문 최대 너비·모바일 좌우 여백) — 선택자가 화면 구조와 맞지 않으면 규칙이 조용히 무시된다
    css = page.evaluate("""() => { const e = document.querySelector('[data-testid="stMainBlockContainer"]'); if (!e) return null;
        const c = getComputedStyle(e); return {maxWidth: c.maxWidth, padLeft: parseFloat(c.paddingLeft), padRight: parseFloat(c.paddingRight)}; }""")
    if name == "desktop":
        f.check("스타일 적용: 본문 최대 너비 1120px", bool(css) and css["maxWidth"] == "1120px", str(css))
    else:
        f.check("스타일 적용: 모바일 좌우 여백 16px", bool(css) and abs(css["padLeft"] - 16) < 0.5 and abs(css["padRight"] - 16) < 0.5, str(css))
    f.shot("1_start")

    # 1) 예시 선택 → 한 번에 실행
    f.pick("예시 선택", EXAMPLE)
    page.get_by_role("button", name="예시로 바로 실행").click()
    f.wait_text("광고안 1")
    f.wait_text("수정할 문구")
    time.sleep(1.5)
    t = f.text()
    f.check("예시 실행 표시(실제 AI 호출 없음)", "예시 실행 · 실제 AI 호출 없음" in t)
    order = [t.find(x) for x in ("수정할 문구", "수정 내역", "다음 작업")]
    f.check("광고안 1: 수정할 문구 → 수정 내역 → 다음 작업 순서", all(i >= 0 for i in order) and order == sorted(order), str(order))
    f.check("판정 이름이 읽히는 말이다", any(w in t for w in ("수정 필요", "게재 불가", "담당자 확인 필요", "점검 통과")))
    f.check("전문 용어 없음", not re.search(r"오탐|미탐|fact_id|\bfinding\b|HUMAN_REVIEW|GRND-", t.split("상세 기록")[0]) )
    f.no_exception("예시 실행 뒤")
    f.shot("2_result", "광고안 1")

    # 2) 문구 수정 → 재검사
    box = page.get_by_label("제목", exact=True).first
    box.scroll_into_view_if_needed()
    box.fill("조건을 확인하고 신청하세요")
    box.press("Enter")
    time.sleep(1.5)
    page.get_by_role("button", name="수정한 문구 다시 검사").first.click()
    f.wait_text("재검사 결과", 120_000)
    time.sleep(1.5)
    t = f.text()
    f.check("재검사: 편집한 문구가 검사 대상으로 표시됨", "검사한 문구" in t and "조건을 확인하고 신청하세요" in t)
    f.check("재검사: 이 문구 검사 1번 시도 표시", "이 문구 검사 1번 시도" in t)
    f.no_exception("재검사 뒤")
    f.shot("3_recheck", "재검사 결과")

    # 3) 같은 문구를 다시 누르면 재사용
    page.get_by_role("button", name="수정한 문구 다시 검사").first.click()
    f.wait_text("이미 검사했습니다", 60_000)
    t = f.text()
    f.check("성공한 같은 문구는 다시 부르지 않고 재사용 (시도 횟수가 늘지 않음)", "이 문구 검사 1번 시도" in t and "이 문구 검사 2번 시도" not in t)

    # 4) 모델 호출이 실패하는 상황: 장애 시뮬레이션 → 다른 문구 검사 → 실패 표시·확인 요청문
    page.locator("summary").filter(has_text="상세 옵션").first.click()   # 설명 글 속의 ‘상세 옵션’ 이 아니라 접힌 제목을 연다
    time.sleep(1.0)
    f.pick("장애 시뮬레이션", "mock: timeout")
    # 코드 검사에서 걸리는 것이 없는 문구로 고친다 → 모델 호출만 실패하면 ‘자동으로 확정하지 못함(담당자 확인 필요)’ 이 된다
    for label, value in (("제목", "가을 신상품을 만나 보세요"), ("본문", "자세한 조건은 상품 상세 페이지에서 확인하세요."), ("버튼 문구", "자세히 보기"), ("안내 문구", "상세 조건은 상품 페이지 참고")):
        fld = page.get_by_label(label, exact=False).first
        fld.scroll_into_view_if_needed()
        fld.fill(value)
        fld.press("Control+Enter" if label in ("본문", "안내 문구") else "Enter")
        time.sleep(0.8)
    page.get_by_role("button", name="수정한 문구 다시 검사").first.click()
    f.wait_text("끝까지 확인하지 못했습니다", 120_000)
    time.sleep(1.0)
    t = f.text()
    f.check("실패: 통과로 보지 않는다고 알린다", "통과로 보지 않습니다" in t and "이 문구는 검사 기준을 통과했습니다" not in t)
    f.check("실패: 버튼이 ‘같은 문구로 다시 시도’ 로 바뀜", page.get_by_role("button", name="같은 문구로 다시 시도").count() >= 1)
    f.check("확인 요청문 생성(수정본 재검사)", "이 문구에 대한 담당자 확인 요청문" in t)
    f.no_exception("호출 실패 뒤")
    page.locator("summary").filter(has_text="확인 요청 내용 (재검사한 문구").first.click()
    time.sleep(1.0)
    req = page.locator("pre").filter(has_text="수정본 재검사").first.inner_text()
    f.check("요청문에 지금 편집한 문구와 이 재검사의 처리 기록이 들어 있다", "가을 신상품을 만나 보세요" in req and "처리 기록(이 재검사)" in req and "검토 대기열" in req, req[:200])
    f.shot("4_failed_handoff", "이 문구에 대한 담당자 확인 요청문")

    # 5) 장애 해제 → 같은 문구로 다시 시도
    f.pick("장애 시뮬레이션", "없음")
    page.get_by_role("button", name="같은 문구로 다시 시도").first.click()
    f.wait_text("2번 시도 · 실패 1번", 120_000)
    f.check("재시도 성공 뒤에도 첫 실패가 기록에 남는다", "2번 시도 · 실패 1번" in f.text())
    f.no_exception("재시도 뒤")
    f.shot("5_retry_ok", "2번 시도 · 실패 1번")

    # 6) 문구를 바꾸면 이전 결과는 ‘이전에 검사한 문구’
    box = page.get_by_label("제목", exact=True).first
    box.fill("전혀 다른 제목")
    box.press("Enter")
    time.sleep(1.5)
    t = f.text()
    f.check("문구를 바꾸면 이전 결과가 현재 결과로 보이지 않는다", "이전에 검사한 문구" in t and "이 문구는 검사 기준을 통과했습니다" not in t)

    f.no_exception("문구 변경 뒤")
    f.check("브라우저 오류 없음", not errors, "; ".join(errors))
    ctx.close()
    return f.checks


def _go(page, f: "Flow", view: str, wait_for: str) -> None:
    """화면 주소(?view=)로 바로 열고 기대한 글자가 보일 때까지 기다린다."""
    page.goto(f"http://127.0.0.1:{PORT}/?view={view}", wait_until="load", timeout=60_000)
    f.wait_text(wait_for, 90_000)
    time.sleep(1.5)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def run_features_flow(browser, name: str, size: tuple[int, int], out: Path) -> list[dict]:
    """새 기능 흐름: 대화로 시작 → 상품 페이지(예시)에서 가져오기 → 소재 퀄리티·배너·이미지 검사 → 성과 리포트 → 운영 → 품질 확인의 새 기능 평가.
    예시 실행(AI 호출 없음)으로만 돈다. 단계마다 기대한 글자·예외 표시·가로 넘침을 확인한다."""
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))
    f = Flow(page, name, out)
    print(f"== {name} {size[0]}x{size[1]} (새 기능)")
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load", timeout=60_000)
    f.wait_text("세 가지 결과는 서로 다릅니다", 90_000)
    t = f.text()
    f.check("시작 방법 세 가지가 보인다", all(w in t for w in ("예시로 시작", "대화로 시작", "상품 페이지에서 가져오기")))

    # 1) 대화로 시작 → 입력 칸 채우기
    page.get_by_text("대화로 시작", exact=True).first.click()
    box = page.get_by_test_id("stChatInputTextArea")
    box.wait_for(timeout=30_000)
    box.fill("20대 직장인 대상 신용대출, 월 300만원, 신청 전환이에요")
    box.press("Enter")
    f.wait_text("이 내용으로 광고 작업 채우기", 90_000)
    time.sleep(1.0)
    t = f.text()
    f.check("대화: 말한 값이 ‘지금까지 이해한 내용’ 에 나온다 (업종·예산)", "신용대출" in t and "300만원" in t.replace(",", "").replace("3000000", "300만원") or "3,000,000" in t)
    f.check("대화: 예시 실행(규칙 기반) 표시", "규칙 기반" in t)
    f.no_exception("대화 한 번 뒤")
    f.shot("feat1_chat", "이 내용으로 광고 작업 채우기")
    page.get_by_role("button", name="이 내용으로 광고 작업 채우기").first.click()
    f.wait_text("대화에서 이해한 내용으로 입력 칸을 채웠습니다", 60_000)
    time.sleep(1.0)
    budget = page.get_by_label("월 예산", exact=False).first.input_value()
    f.check("대화: 월 예산이 입력 칸에 3,000,000원으로 들어갔다", _digits(budget) == "3000000", budget)
    f.check("대화: 시작 방법이 다시 ‘예시로 시작’ 이다", page.get_by_role("radio", name="예시로 시작", exact=True).is_checked())
    f.no_exception("입력 칸 채운 뒤")

    # 2) 상품 페이지(예시)에서 가져오기 — 사실은 사용자가 고른 것만
    page.get_by_text("상품 페이지에서 가져오기", exact=True).first.click()
    page.get_by_role("button", name="읽어 오기").first.wait_for(timeout=30_000)
    page.get_by_role("button", name="읽어 오기").first.click()
    time.sleep(3.0)
    checked = page.locator('[data-testid="stCheckbox"] input:checked').count()
    boxes = page.locator('[data-testid="stCheckbox"]').count()
    f.check("상품 페이지: 사실 후보가 보이고 기본은 아무것도 고르지 않았다", boxes >= 1 and checked == 0, f"boxes={boxes} checked={checked}")
    f.no_exception("예시 페이지 읽은 뒤")
    f.shot("feat2_url", "읽어 오기")
    page.locator('[data-testid="stCheckbox"]').first.click()
    time.sleep(1.0)
    page.get_by_role("button", name="선택한 사실로 광고 작업 채우기").first.click()
    f.wait_text("상품 페이지에서 확인한 내용으로", 60_000)
    f.check("상품 페이지: 고른 사실이 ‘반드시 포함해야 할 조건’ 칸에 들어갔다", "F1:" in page.get_by_label("사실 목록", exact=False).first.input_value())
    f.no_exception("상품 페이지 내용을 채운 뒤")

    # 3) 예시 실행 → 소재 퀄리티 · 배너 · 이미지 검사
    page.get_by_text("예시로 시작", exact=True).first.click()
    time.sleep(1.5)
    page.get_by_role("button", name="예시로 바로 실행").first.click()
    f.wait_text("광고안 1", 180_000)
    f.wait_text("소재 퀄리티 (참고)", 60_000)
    f.check("광고안마다 ‘소재 퀄리티 (참고)’ 가 붙는다", page.get_by_text("소재 퀄리티 (참고)", exact=False).count() >= 1)
    page.get_by_text("배너 시안과 이미지 검사 열기", exact=False).first.click()
    f.wait_text("광고 문구를 그대로 글로 배치한 배너 시안", 60_000)
    time.sleep(1.5)
    f.check("배너: 시안이 3개 보인다", page.get_by_role("button", name="SVG 내려받기", exact=False).count() >= 3 or page.get_by_text("SVG", exact=False).count() >= 3)
    f.no_exception("배너 시안을 연 뒤")
    f.shot("feat3_banner", "배너 시안")
    page.get_by_role("tab", name="이미지 검사").first.click()
    time.sleep(1.0)
    page.locator('input[type="file"]').first.set_input_files(str(ROOT / "evals" / "datasets" / "images" / "svg_guarantee.svg"))
    f.wait_text("원금 보장", 60_000)
    time.sleep(1.0)
    t = f.text()
    f.check("이미지 검사: 이미지 속 ‘원금 보장’ 이 문구 규칙으로 걸린다", "원금 보장" in t and "게재 불가" in t)
    f.no_exception("이미지 검사 뒤")
    f.shot("feat3_image", "이미지 검사")

    # 4) 성과 리포트
    _go(page, f, "성과 리포트", "성과 리포트")
    page.get_by_role("button", name="분석하기").first.click()
    f.wait_text("무슨 일이 있었나", 120_000)
    time.sleep(1.5)
    t = f.text()
    f.check("성과 리포트: 이상·원인 후보·다음 액션이 나온다", all(w in t for w in ("원인 후보", "다음에 할 일")))
    f.check("성과 리포트: 합성 데이터와 ‘원인을 확정하지 않는다’ 가 표시된다", "합성" in t and "확정하지 않습니다" in t)
    f.no_exception("성과 리포트 분석 뒤")
    f.shot("feat4_performance", "무슨 일이 있었나")

    # 5) 운영
    _go(page, f, "운영", "검수 대기열")
    page.get_by_role("tab", name="운영 지표·모니터").first.click()
    f.wait_text("효과 계산기", 60_000)
    time.sleep(1.0)
    t = f.text()
    f.check("운영: 효과 계산기에 ‘실제 운영 효과가 아닙니다’ 가 붙는다", "실제 운영 효과가 아닙니다" in t)
    f.no_exception("운영 화면")
    f.shot("feat5_ops", "효과 계산기")

    # 6) 품질 확인의 새 기능 평가
    _go(page, f, "품질 확인", "새 기능 평가 (합성 데이터)")
    page.get_by_text("성과 리포트 인사이트", exact=True).first.click()
    time.sleep(1.5)
    t = f.text()
    f.check("새 기능 평가: 합성 데이터 경고와 한계가 보인다", "합성" in t and "독립 검증은 없으며" in t)
    f.no_exception("새 기능 평가")
    f.shot("feat6_new_evals", "새 기능 평가 (합성 데이터)")
    f.check("브라우저 오류 없음 (새 기능)", not errors, "; ".join(errors))
    ctx.close()
    return f.checks


def main() -> int:
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "docs" / "screenshots"
    env = {**os.environ, "PREFLIGHT_FORCE_DEMO": "1"}
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", str(PORT),
                             "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    checks: list[dict] = []
    try:
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
            for name, size in VIEWPORTS.items():
                checks += run_flow(browser, name, size, out)
                checks += run_features_flow(browser, name, size, out)
            browser.close()
    finally:
        proc.terminate()
    bad = [c for c in checks if not c["ok"]]
    # 이 실행의 결과를 파일로 남긴다 — 문서가 '몇 개 검사를 통과했다' 고 말할 때 근거로 삼는다
    try:
        import streamlit

        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "app.py", "ui", "agents", "guardrails", "core", "llm"], cwd=ROOT, capture_output=True, text=True).stdout.strip())
        report = {"commit": sha + ("+uncommitted" if dirty else ""), "streamlit": streamlit.__version__, "python": sys.version.split()[0], "viewports": list(VIEWPORTS),
                  "checks": len(checks), "failed": len(bad), "per_viewport": {n: sum(1 for c in checks if c["viewport"] == n) for n in VIEWPORTS},
                  "results": [{"viewport": c["viewport"], "check": c["check"], "ok": c["ok"]} for c in checks]}
        out.mkdir(parents=True, exist_ok=True)
        (out / "e2e_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print("e2e_report.json 을 쓰지 못함:", e)
    print(json.dumps({"checks": len(checks), "failed": len(bad)}, ensure_ascii=False))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
