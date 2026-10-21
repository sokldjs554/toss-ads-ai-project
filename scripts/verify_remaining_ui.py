"""Observe current shared-review and evaluation UI in native and Pages builds.

Uses the actual build_site.py archive and app.py entrypoint, or explicitly given
public deployment URLs. It never fabricates an evaluation result, submits a
shared-review command, enters an access key, or calls a model provider. Outputs
must be new directories. Product and frozen evaluation files are read-only.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import io
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ESSENTIAL = (
    "ui/team_review_view.py", "ui/ops_hub.py", "ui/new_evals.py",
    "ops/team_review.py", "ops/team_review_store.py",
    "evals/reports/dialogs_regression_v2_20261021.json",
    "evals/reports/dialogs_original_after_fix_20261021.json",
    "evals/reports/remaining-20261021/insights-after-original.json",
    "evals/reports/remaining-20261021/insights-after-holdout.json",
    "evals/reports/remaining-20261021/insights-contrasts-after.json",
)
NATIVE_LINK = "https://preflight-toss-ads.onrender.com/?view=운영"
VIEWS = (("desktop", 1280, 900), ("mobile", 390, 844))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--modes", default="native,pages", choices=("native", "pages", "native,pages"))
    ap.add_argument("--app-python", default=sys.executable, help="Existing application environment, separate from an optional QA browser environment")
    ap.add_argument("--timeout-ms", type=int, default=180000)
    ap.add_argument("--ignore-https-errors", action="store_true", help="Test proxy only; recorded in evidence, never applied to product HTML")
    ap.add_argument("--expected-run-id", help="Fail if the newest saved report is not this exact run")
    ap.add_argument("--require-remaining-evaluation", action="store_true", help="Require the quoted quality audit, four rewrite cases and one separate conversation follow-up")
    ap.add_argument("--native-url", help="Read-only QA of the public Render application instead of starting a local server")
    ap.add_argument("--pages-url", help="Read-only QA of the public Pages application instead of serving a local build")
    ap.add_argument("--expected-build-sha", help="Required for public URLs; compare the displayed build and public Pages archive with this revision")
    ap.add_argument("--native-shared-state", choices=("unconfigured", "login"), default="unconfigured", help="Expected public server state; login checks the empty masked input without authenticating")
    args = ap.parse_args()
    public_urls = {mode: value for mode, value in (("native", args.native_url), ("pages", args.pages_url)) if value}
    for mode, value in public_urls.items():
        parsed = urlsplit(value)
        host = "preflight-toss-ads.onrender.com" if mode == "native" else "sokldjs554.github.io"
        expected_path = "/" if mode == "native" else "/toss-ads-ai-project/app/"
        if (mode not in args.modes.split(",") or parsed.scheme != "https" or parsed.hostname != host
                or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443)
                or parsed.path.rstrip("/") != expected_path.rstrip("/")):
            ap.error(f"--{mode}-url must target the requested HTTPS application and an enabled mode")
    if public_urls and not (args.expected_build_sha and re.fullmatch(r"[0-9a-fA-F]{7,40}", args.expected_build_sha)):
        ap.error("Public URL verification requires --expected-build-sha with 7 to 40 hexadecimal characters")
    if args.native_shared_state == "login" and "native" not in public_urls:
        ap.error("The login state is only available for an explicit public native URL; local native QA removes DB settings")
    expected_build = args.expected_build_sha[:7].lower() if args.expected_build_sha else None
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error("Choose an empty evidence directory; prior evidence is preserved.")
    out.mkdir(parents=True, exist_ok=True)
    # Keep a virtual environment's executable path: resolving its interpreter
    # symlink would discard its site-packages.
    app_python = os.path.abspath(args.app_python)
    saved = []
    for path in (ROOT / "evals/reports/live_features").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("schema_version") == 1 and isinstance(record.get("results"), dict):
            saved.append((record, str(path.relative_to(ROOT))))
    if not saved:
        ap.error("No saved live evaluation report is available; the probe never creates one.")
    saved.sort(key=lambda item: (str(item[0].get("finished_at") or item[0].get("started_at") or ""), str(item[0].get("run_id", ""))), reverse=True)
    latest, latest_path = saved[0]
    essential = (*ESSENTIAL, latest_path)
    report = {
        "kind": "remaining-shared-review-and-evaluation-ui",
        "started_at": now(), "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "modes": args.modes.split(","), "viewports": VIEWS,
        "playwright": version("playwright"), "ignore_https_errors": args.ignore_https_errors,
        "expected_run_id": args.expected_run_id, "require_remaining_evaluation": args.require_remaining_evaluation,
        "saved_report": {"run_id": latest.get("run_id"), "path": latest_path, "sha256": sha((ROOT / latest_path).read_bytes()),
                         "status": latest.get("status"), "source_sha": latest.get("source_sha")},
        "scope": "read-only public deployment screens; no access-key entry or shared-review commands" if public_urls else "native database unconfigured; Pages browser mode; saved evaluation records only",
        "public_urls": public_urls, "expected_build": expected_build,
        "native_shared_state": args.native_shared_state,
        "probe_sha256": sha(Path(__file__).read_bytes()),
        "checks": [], "observations": [], "screenshots": [],
        "source_hashes": {name: sha((ROOT / name).read_bytes()) for name in essential},
        "frozen_report_hashes_before": {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in (ROOT / "evals/reports/live_features").glob("*.json")},
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def check(scope: str, label: str, okay: bool, detail=None):
        report["checks"].append({"scope": scope, "check": label, "ok": bool(okay), "detail": detail})
        save()
        print(f"{'PASS' if okay else 'FAIL'} {scope}: {label}", flush=True)

    def picture(page, name, anchor=None):
        if anchor:
            anchor.evaluate("e => {e.scrollIntoView({block:'start'}); document.querySelector('[data-testid=\"stMain\"]')?.scrollBy(0,-20)}")
            page.wait_for_timeout(350)
        page.screenshot(path=str(out / name), full_page=False)
        report["screenshots"].append(name)
        save()

    def inspect_page(page, scope):
        check(scope, "no Streamlit exception", page.get_by_test_id("stException").count() == 0)
        geometry = page.evaluate("({scroll:document.documentElement.scrollWidth, viewport:innerWidth})")
        check(scope, "no page horizontal overflow", geometry["scroll"] <= geometry["viewport"] + 1, geometry)

    def exact_row(page, container, scope, label, expected):
        rows = container.locator("tbody tr").filter(has=page.get_by_role("cell", name=label, exact=True))
        actual = [rows.nth(i).locator("td").all_text_contents() for i in range(rows.count())]
        check(scope, label + " matches saved values", actual == [expected], {"actual": actual, "expected": [expected]})

    def fraction_percent(value):
        numerator, denominator = (int(part) for part in value.split("/"))
        return f"{numerator}/{denominator} ({numerator / denominator:.1%})"

    def observe(browser, mode, url):
        for viewport, width, height in VIEWS:
            scope = ("public-" if mode in public_urls else "") + mode + "-" + viewport
            observed = {"scope": scope, "started_at": now(), "page_errors": [], "console_errors": [], "network_errors": [], "blocked_model_requests": []}
            report["observations"].append(observed)
            context = browser.new_context(viewport={"width": width, "height": height}, ignore_https_errors=args.ignore_https_errors)
            page = context.new_page()
            page.on("pageerror", lambda error: observed["page_errors"].append(str(error)))
            page.on("console", lambda msg: observed["console_errors"].append(msg.text[:2000]) if msg.type == "error" else None)
            page.on("requestfailed", lambda req: observed["network_errors"].append({"url": req.url, "failure": req.failure}))

            def block_provider(route):
                observed["blocked_model_requests"].append(route.request.url)
                route.abort()

            page.route(re.compile(r"https://api\.(?:openai|anthropic)\.com/.*"), block_provider)
            try:
                print(f"OPEN {scope}", flush=True)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                observed["webassembly"] = page.evaluate("({Suspending:typeof WebAssembly.Suspending,promising:typeof WebAssembly.promising})")
                page.get_by_role("radio", name="운영", exact=True).wait_for(timeout=args.timeout_ms)
                if mode in public_urls:
                    observed["url"] = page.url
                    check(scope, "page build matches expected deployment", f"빌드 {expected_build}" in page.inner_text("body"), expected_build)
                page.get_by_role("radio", name="운영", exact=True).click()
                page.get_by_role("tab", name="공동 검수", exact=True).wait_for(timeout=60000)
                tabs = page.get_by_role("tablist").first.get_by_role("tab").all_text_contents()
                check(scope, "operations has all three tabs in order", tabs == ["검수 대기열", "운영 지표·모니터", "공동 검수"], tabs)
                page.get_by_role("tab", name="공동 검수", exact=True).click()
                login_state = mode == "native" and args.native_shared_state == "login"
                expected = "공동 저장 연결이 아직 설정되지 않았습니다" if mode == "native" else "공동 검수는 서버가 연결된 운영 화면에서"
                if login_state:
                    page.get_by_label("접근키", exact=True).wait_for(timeout=60000)
                else:
                    page.get_by_text(expected, exact=False).first.wait_for(timeout=60000)
                visible = page.inner_text("body")
                if login_state:
                    login = page.get_by_label("접근키", exact=True)
                    check(scope, "server login is present without entering credentials", login.count() == 1
                          and login.get_attribute("type") == "password" and login.input_value() == ""
                          and page.get_by_role("button", name="공동 검수 접속", exact=True).count() == 1)
                else:
                    check(scope, "correct shared review availability message", expected in visible)
                    check(scope, "no access key input before server setup", page.get_by_label("접근키", exact=True).count() == 0)
                forbidden = re.findall(r"pfk_[A-Za-z0-9_-]{43}|postgres(?:ql)?://|PREFLIGHT_TEAM_DATABASE_URL|key_digest|request_id|acct_owner|psycopg|오탐|미탐|맞습니다", visible)
                check(scope, "no secret or unnecessary internal control names", not forbidden, forbidden)
                check(scope, "accounts do not claim independent people", "실제 참여자와 독립 검수 여부는 별도 확인" in visible)
                if mode == "pages":
                    link = page.get_by_role("link", name="서버의 운영 화면 열기", exact=True)
                    check(scope, "server link is correct", link.count() == 1 and unquote(link.get_attribute("href") or "") == NATIVE_LINK)
                    check(scope, "browser mode is identified", "브라우저 실행판 (서버 없음, 예시 실행만)" in visible)
                inspect_page(page, scope + ":shared-review")
                state_file = "-team-login-no-credentials.png" if login_state else ("-team-database-unconfigured.png" if mode == "native" else "-team-server-link.png")
                picture(page, scope + state_file, page.get_by_role("heading", name="공동 검수", exact=True))

                page.get_by_role("radio", name="품질 확인", exact=True).click()
                heading = page.get_by_role("heading", name="새 기능 평가 (합성 데이터)", exact=True)
                heading.wait_for(timeout=60000)
                page.get_by_role("heading", name="실제 모델 실행 기록", exact=True).wait_for(timeout=60000)
                page.get_by_text("실제 응답에서 확인한 모델:", exact=False).first.wait_for(timeout=60000)
                visible = page.inner_text("body")
                check(scope, "evaluation limits are displayed", "독립 검증은 없으며" in visible and "실제 광고 데이터의 정확도가 아닙니다" in visible)
                check(scope, "stored calls are not advertised as new calls", "여기서 열람하는 동안 새 호출이나 비용은 발생하지 않습니다" in visible)
                observed["latest_saved_report"] = latest["run_id"]
                detail = page.locator("details").filter(has_text=latest["run_id"]).first
                check(scope, "latest stored report is expanded", detail.get_attribute("open") is not None)
                first_table = detail.locator("table").first
                for group, label in (("vision", "이미지 속 글자·검사"), ("conversation", "여러 턴 캠페인 설정"),
                                     ("insights", "성과 보고서 문장화"), ("quality", "광고 문구 비교"),
                                     ("pipeline", "캠페인 구성·문구 생성·검사")):
                    summary = latest["summaries"][group]
                    attempted = summary["attempted_cases"]
                    final_cell = ("아래 채택·보류 표 확인" if group == "quality" and latest.get("quality_audit_summary")
                                  else f"{summary['all_checks_passed']}/{attempted}")
                    exact_row(page, first_table, scope + ":live-summary", label,
                              [label, f"{attempted}/{latest['manifest']['planned_cases'][group]}",
                               f"{summary['cases_with_real_calls']}/{attempted}", final_cell])
                observed["quality_audit_available_in_saved_report"] = bool(latest.get("quality_audit_summary"))
                observed["rewrite_supplement_available_in_saved_report"] = bool(latest.get("supplemental_summaries", {}).get("rewrite"))
                observed["conversation_followup_available_in_saved_report"] = bool(latest.get("supplemental_summaries", {}).get("conversation_followup"))
                if latest.get("quality_audit_summary"):
                    quality = latest["quality_audit_summary"]
                    count = quality["attempted_cases"]
                    for label, key, meaning in (
                        ("근거 검사 후 채택한 비교", "accepted_cases", "두 순서와 중요한 근거가 같은 비교"),
                        ("판단 보류", "abstained_cases", "불일치·인용 문제·응답 실패는 무승부로 세지 않음"),
                        ("채택했고 작성자 선호와도 일치", "accepted_author_matches", "전체 시도 쌍을 분모로 사용"),
                        ("기존 비교 지표", "raw_stable_author_matches", "원래 두 선택이 같고 작성자 선호와 일치; 근거 검사 전 수치"),
                    ):
                        exact_row(page, detail, scope + ":quality-audit", label, [label, f"{quality[key]}/{count}", meaning])
                    status_names = {"consensus": "채택", "order_disagreement": "순서에 따라 선택이 달라짐",
                                    "rubric_disagreement": "중요한 근거가 달라짐", "invalid_evidence": "인용·근거 검사 실패",
                                    "judge_abstained": "모델이 판단 보류", "incomplete": "응답을 모두 얻지 못함"}
                    status_text = "결과별 수: " + ", ".join(f"{status_names.get(key, key)} {value}쌍" for key, value in quality["by_status"].items())
                    check(scope, "quality status counts match the stored summary", detail.get_by_text(status_text, exact=True).count() == 1, status_text)
                    picture(page, scope + "-quality-accepted-and-abstained.png", detail.get_by_text("문구 비교 · 채택과 보류", exact=True))
                for group, heading_text, label, filename in (
                    ("conversation_followup", "추가 검사 · 연령 확인 뒤 캠페인 완성", "최소 연령 답변 → 미리보기 → 광고 작업 전달", "age-followup-separate-denominator"),
                    ("rewrite", "추가 검사 · 위반 원문의 수정과 재검사", "원문 → 모델 수정안 → 동일 정책 재검사", "rewrite-supplement"),
                ):
                    summary = latest.get("supplemental_summaries", {}).get(group)
                    planned = latest.get("manifest", {}).get("planned_supplemental_cases", {}).get(group)
                    if summary is None and not planned:
                        continue
                    summary = summary or {}
                    attempted = summary.get("attempted_cases", 0)
                    exact_row(page, detail, scope + ":" + group, label,
                              [label, f"{attempted}/{planned or '—'}", f"{summary.get('cases_with_real_calls', 0)}/{attempted}",
                               f"{summary.get('all_checks_passed', 0)}/{attempted}"])
                    if group == "conversation_followup":
                        check(scope, "one age follow-up is separate from the original twenty", planned == 1
                              and latest["manifest"]["planned_cases"]["conversation"] == 20
                              and "최초 대화 20개의 점수와 별도입니다" in detail.inner_text(),
                              {"followup_planned": planned, "followup_attempted": attempted,
                               "original_attempted": latest["summaries"]["conversation"]["attempted_cases"]})
                    picture(page, scope + "-" + filename + ".png", detail.get_by_text(heading_text, exact=True))
                ledger = latest.get("ledger", {})
                usage = ledger.get("usage_cost_usd")
                cost = f"${usage:.4f}" if isinstance(usage, (int, float)) else "확인되지 않음"
                ledger_text = f"호출 {ledger.get('attempts', 0)}회 · 응답 토큰으로 계산한 비용 {cost} · 사용량 미확인 {ledger.get('unknown_cost_calls', 0)}회"
                check(scope, "saved call count and usage cost match", ledger_text in detail.inner_text(), ledger_text)
                check(scope, "evaluation source revision is shown", f"실행 소스 {str(latest.get('source_sha', ''))[:12]}" in detail.inner_text())
                inspect_page(page, scope + ":evaluations")
                picture(page, scope + "-saved-live-evaluations.png", page.get_by_role("heading", name="실제 모델 실행 기록", exact=True))
                dialog = page.locator("details").filter(has=page.locator("summary").filter(has_text="대화형 캠페인 설정")).last
                dialog.locator(":scope > summary").click()
                page.get_by_text("알려진 실패를 수정한 뒤 다시 실행한 66개 대화", exact=False).first.wait_for(timeout=30000)
                check(scope, "versioned conversation report is available", "알려진 실패를 수정한 뒤 다시 실행한 66개 대화" in dialog.inner_text() and "별도 작성한 20개 대화의 첫 결과" in dialog.inner_text())
                inspect_page(page, scope + ":conversation-evaluation")
                picture(page, scope + "-conversation-regression-and-original.png", dialog.locator(":scope > summary"))

                insights = page.locator("details").filter(has=page.locator("summary").filter(has_text="성과 리포트 인사이트")).last
                insights.locator(":scope > summary").click()
                latest_table = insights.locator("table").filter(has=page.locator("th").filter(has_text=re.compile(r"^같은 80개 합성 사례 재검사$")))
                latest_table.wait_for(timeout=30000)
                current = json.loads((ROOT / "evals/reports/remaining-20261021/insights-after-holdout.json").read_text())["splits"]["holdout"]
                for label, key in (("이상을 찾아낸 비율(재현율)", "detection_recall_n"),
                                   ("찾은 것 중 실제 이상인 비율(정밀도)", "detection_precision_n"),
                                   ("이상이 없는데 이상이라고 한 시나리오", "control_false_alarm_n"),
                                   ("원인 가설 1순위가 합성 정답과 맞은 비율", "cause_top1_n")):
                    exact_row(page, latest_table, scope + ":current-insights", label, [label, "—", fraction_percent(current[key])])
                contrasts = json.loads((ROOT / "evals/reports/remaining-20261021/insights-contrasts-after.json").read_text())["summary"]
                for key, label in (("persistent_local_drop", "한 광고세트의 작은 하락"), ("normal", "변화 없는 구간"),
                                   ("continuing_trend", "이어지는 추세"), ("recovered_before_window", "표시 기간 전에 회복한 변화")):
                    item = contrasts[key]
                    meaning = "작은 하락을 찾은 수" if key == "persistent_local_drop" else "점검 알림을 낸 수"
                    exact_row(page, insights, scope + ":insight-contrasts", label,
                              [label, fraction_percent(f"{item['alerted']}/{item['n']}"), meaning])
                small = contrasts["persistent_local_drop"]
                check(scope, "remaining undetected contrasts are explicit", f"작은 하락 {small['n']}개 중 {small['n'] - small['alerted']}개를 찾지 못해" in insights.inner_text())
                prior = insights.locator("details").filter(has=page.get_by_text("2026-10-20의 같은 80개 재검사 기록", exact=True))
                prior.locator(":scope > summary").click()
                historical = json.loads((ROOT / "evals/reports/insights_holdout_regression_20261020.json").read_text())["splits"]["holdout"]
                label = "이상을 찾아낸 비율(재현율)"
                exact_row(page, prior, scope + ":historical-insights", label, [label, "—", fraction_percent(historical["detection_recall_n"])])
                inspect_page(page, scope + ":insight-evaluation")
                picture(page, scope + "-current-insight-evaluation.png", insights.get_by_text("처음 평가에서 드러난 한계를 보완한 뒤 재검사 · 최신 기록", exact=True))
                picture(page, scope + "-insight-contrasts-and-previous-result.png", insights.get_by_text("작은 변화와 정상 흐름을 구분하는 추가 대조", exact=True))
                observed["status"] = "completed"
            except Exception as exc:
                observed["status"] = "interrupted"
                observed["error"] = {"type": type(exc).__name__, "message": str(exc)}
                check(scope, "requested screens complete", False, type(exc).__name__)
                try:
                    (out / (scope + "-interrupted-visible-text.txt")).write_text(page.inner_text("body"), encoding="utf-8")
                    picture(page, scope + "-interrupted.png")
                except Exception as capture:
                    observed["capture_error"] = str(capture)
            finally:
                check(scope, "no browser page exception", not observed["page_errors"], observed["page_errors"])
                check(scope, "no model provider request", not observed["blocked_model_requests"], observed["blocked_model_requests"])
                observed["finished_at"] = now()
                save()
                context.close()

    native = None
    static = None
    log = (out / "native-server.log").open("w", encoding="utf-8")
    try:
        if args.expected_run_id:
            correct_run = latest.get("run_id") == args.expected_run_id
            check("saved-report", "newest report is the requested run", correct_run, latest.get("run_id"))
            if not correct_run:
                raise ValueError("Requested saved report is not the latest available record")
        if args.require_remaining_evaluation:
            planned = latest.get("manifest", {}).get("planned_supplemental_cases", {})
            supplemental = latest.get("supplemental_summaries", {})
            quality = latest.get("quality_audit_summary")
            ready = isinstance(quality, dict) and bool(quality) and planned.get("rewrite") == 4 and planned.get("conversation_followup") == 1
            ready = ready and all(isinstance(supplemental.get(group), dict) for group in ("rewrite", "conversation_followup"))
            check("saved-report", "remaining evaluation has quality audit and separate supplemental records", ready,
                  {"supplemental_planned": planned, "quality_audit": quality})
            if not ready:
                raise ValueError("Remaining evaluation has not been saved with the required separate summaries")
        with tempfile.TemporaryDirectory(prefix="preflight-remaining-site-") as temporary:
            site = Path(temporary) / "site"
            build = subprocess.run([app_python, "scripts/build_site.py", "--out", str(site)], cwd=ROOT, capture_output=True, text=True, check=True)
            (out / "pages-build.log").write_text(build.stdout, encoding="utf-8")
            archive = site / "app/src.zip"
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                report["bundle"] = {"sha256": sha(archive.read_bytes()), "files": len(names), "essential_hashes": {name: sha(zf.read(name)) for name in essential}, "stlite_html_sha256": sha((site / "app/index.html").read_bytes())}
                check("pages-build", "all new UI and reports bundled unchanged", report["bundle"]["essential_hashes"] == report["source_hashes"])
                leaked_names = [n for n in names if n == ".streamlit/secrets.toml" or Path(n).name.startswith(".env") or "secret" in Path(n).name.lower()]
                check("pages-build", "no secret configuration files bundled", not leaked_names, leaked_names)
            urls = dict(public_urls)
            if "pages" in public_urls:
                public_zip_url = urljoin(public_urls["pages"].split("?", 1)[0].rstrip("/") + "/", "src.zip")
                tls_context = ssl._create_unverified_context() if args.ignore_https_errors else ssl.create_default_context()
                request = urllib.request.Request(public_zip_url, headers={"Cache-Control": "no-cache"})
                with urllib.request.urlopen(request, timeout=60, context=tls_context) as response:
                    if urlsplit(response.url).hostname != "sokldjs554.github.io":
                        raise ValueError("Public Pages archive redirected outside the requested host")
                    body = response.read(20_000_001)
                if len(body) > 20_000_000:
                    raise ValueError("Public Pages archive exceeded the QA download limit")
                with zipfile.ZipFile(io.BytesIO(body)) as public_zip:
                    names = public_zip.namelist()
                    report["public_bundle"] = {
                        "url": public_zip_url, "sha256": sha(body), "files": len(names),
                        "build": public_zip.read("build_info.txt").decode("utf-8").strip(),
                        "essential_hashes": {name: sha(public_zip.read(name)) for name in essential},
                    }
                    check("public-pages-bundle", "archive build matches expected deployment", report["public_bundle"]["build"] == expected_build)
                    check("public-pages-bundle", "public UI and saved reports match the checked local files", report["public_bundle"]["essential_hashes"] == report["source_hashes"])
                    leaked_names = [n for n in names if n == ".streamlit/secrets.toml" or Path(n).name.startswith(".env") or "secret" in Path(n).name.lower()]
                    check("public-pages-bundle", "no secret configuration files bundled", not leaked_names, leaked_names)
            if "pages" in report["modes"] and "pages" not in public_urls:
                static = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(site)))
                threading.Thread(target=static.serve_forever, daemon=True).start()
                urls["pages"] = f"http://127.0.0.1:{static.server_port}/app/"
            if "native" in report["modes"] and "native" not in public_urls:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                env = {**os.environ, "PREFLIGHT_FORCE_DEMO": "1"}
                for key in ("OPENAI_API_KEY", "OPEN_API_KEY", "ANTHROPIC_API_KEY", "PREFLIGHT_TEAM_DATABASE_URL"):
                    env.pop(key, None)
                native = subprocess.Popen([app_python, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", str(port), "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                urls["native"] = f"http://127.0.0.1:{port}/"
                for _ in range(60):
                    if native.poll() is not None:
                        raise RuntimeError("Native application exited before ready; see native-server.log")
                    try:
                        urllib.request.urlopen(urls["native"] + "_stcore/health", timeout=2)
                        break
                    except OSError:
                        time.sleep(1)
                else:
                    raise RuntimeError("Native application did not become ready")
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                executable = os.environ.get("PREFLIGHT_CHROMIUM")
                with pw.chromium.launch(headless=True, **({"executable_path": executable} if executable else {})) as browser:
                    report["browser"] = browser.version
                    for mode in report["modes"]:
                        observe(browser, mode, urls[mode])
    except Exception as exc:
        report["setup_error"] = {"type": type(exc).__name__, "message": str(exc)}
        check("setup", "verification starts", False, type(exc).__name__)
    finally:
        if native is not None:
            native.terminate()
            try:
                native.wait(timeout=10)
            except subprocess.TimeoutExpired:
                native.kill()
                native.wait()
        if static is not None:
            static.shutdown()
            static.server_close()
        log.close()
        unchanged = all((ROOT / name).exists() and sha((ROOT / name).read_bytes()) == expected for name, expected in report["frozen_report_hashes_before"].items())
        check("provenance", "pre-existing live reports unchanged", unchanged)
        report["finished_at"] = now()
        report["failed_checks"] = sum(not c["ok"] for c in report["checks"])
        report["status"] = "passed" if not report["failed_checks"] else "failed"
        save()
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]), "failed": report["failed_checks"]}), flush=True)
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
