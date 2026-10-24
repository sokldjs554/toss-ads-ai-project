"""Real public Chromium regression: coupon example, editing, prohibited terms.

Only the explicitly free example button and its free rechecks are used. No
shared-review login, record writes, provider keys, or paid runs are used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = "업계 최저 OO몰 가을 쿠폰! 지금 아니면 못 받는 조건"
PAGES = "https://sokldjs554.github.io/toss-ads-ai-project/app/"
NATIVE = "https://preflight-toss-ads.onrender.com/"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha):
        parser.error("A full commit SHA is required")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"expected_sha": args.expected_sha, "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "running", "checks": [], "screens": [], "errors": [],
              "shared_review_actions": "not attempted", "execution_constraint": "free example and its DEMO rechecks only",
              "server_call_and_database_counters": "not observed; no credentials or server telemetry used",
              "tls_verification": True}

    def save():
        (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def check(scope, label, okay, detail=None):
        report["checks"].append({"scope": scope, "check": label, "ok": bool(okay), "detail": detail})
        save()
        print(f"{'PASS' if okay else 'FAIL'} {scope}: {label}", flush=True)
        if not okay:
            raise AssertionError(f"{scope}: {label}")

    def shot(page, scope, step):
        name = f"{scope}-{step}.png"
        page.screenshot(path=str(args.out / name), full_page=False)
        report["screens"].append(name)
        (args.out / f"{scope}-{step}.txt").write_text(page.inner_text("body"))
        save()

    try:
        # Check actual published source, not just a version label in HTML.
        req = urllib.request.Request(PAGES + "src.zip?verify=" + args.expected_sha,
                                     headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=90) as response:
            payload = response.read(20_000_001)
        check("pages", "published source archive within limit", len(payload) <= 20_000_000)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            check("pages", "archive build matches commit", archive.read("build_info.txt").decode().strip() == args.expected_sha[:7])
            for name in ("guardrails/normalize.py", "guardrails/lexical.py", "guardrails/policy.yaml",
                         "agents/adjudicator.py", "agents/recheck.py", "agents/rewrite_check.py", "core/insight_engine.py",
                         "llm/router.py", "llm/spending.py", "core/live_access.py", "observability/tracer.py", "ui/new_evals.py",
                         "agents/live_pilot.py", "llm/live_pilot_gateway.py", "ops/live_pilot_store.py", "ui/live_pilot_view.py"):
                deployed = archive.read(name)
                check("pages", "published source matches checkout: " + name,
                      deployed == (ROOT / name).read_bytes(), hashlib.sha256(deployed).hexdigest())

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **({"channel":"chrome"} if os.environ.get("PREFLIGHT_BROWSER_CHANNEL")=="chrome" else {}))
            report["browser"] = browser.version
            for scope, url in (("pages", PAGES), ("server", NATIVE)):
                ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
                page = ctx.new_page()
                page.set_default_timeout(60000)
                blocked = []

                def block(route):
                    blocked.append(route.request.url)
                    route.abort()

                page.route(re.compile(r"https://api\.(?:openai|anthropic)\.com/.*"), block)
                try:
                    deadline = time.monotonic() + 600
                    while True:
                        page.goto(url + "?verify=" + args.expected_sha, wait_until="domcontentloaded", timeout=90000)
                        page.get_by_role("button", name="예시로 바로 실행", exact=True).wait_for(timeout=180000)
                        observed = re.findall(r"빌드 ([0-9a-f]{7,40})", page.inner_text("body"))
                        if args.expected_sha[:7] in observed or time.monotonic() >= deadline:
                            break
                        print(f"WAIT {scope} build: {observed}", flush=True)
                        page.wait_for_timeout(15000)
                    check(scope, "screen build matches deployed commit", args.expected_sha[:7] in observed, observed)
                    shot(page, scope, "version")
                    box = page.locator('[data-testid="stSelectbox"]').filter(has_text="예시 선택").first
                    box.get_by_role("combobox").click()
                    page.get_by_role("option").filter(has_text="커머스 쿠폰").first.click()
                    page.get_by_role("button", name="예시로 바로 실행", exact=True).click()
                    page.get_by_role("heading", name="광고안 3", exact=True).wait_for(timeout=180000)
                    editor = page.locator("details").filter(has=page.locator("summary").filter(has_text="문구 고쳐서 다시 검사")).nth(2)
                    editor.get_by_role("button", name="수정한 문구 다시 검사", exact=True).wait_for()
                    ad = page.locator(".pf-ad").filter(has=page.locator(".h", has_text=ORIGINAL)).first
                    check(scope, "commerce coupon original rendered", ad.count() == 1)
                    marks = ad.locator("mark").all_text_contents()
                    reasons = "\n".join(page.locator(".pf-reason").all_text_contents())
                    check(scope, "false restricted quote removed", "폰! 지" not in marks and "광고 불가 업종·내용" not in reasons, marks)
                    check(scope, "other policy findings retained", "업계 최저" in reasons and "지금 아니면" in reasons, reasons)
                    card = page.locator('[data-testid="stVerticalBlock"]').filter(has=page.get_by_role("heading", name="광고안 3", exact=True)).filter(has_text="문구 고쳐서 다시 검사").last
                    check(scope, "original remains revise, not approve", card.locator(".pf-badge.big").first.inner_text() == "수정 필요")
                    check(scope, "free demo mode visible", "예시 실행 · 실제 AI 호출 없음" in page.inner_text("body"))
                    ad.scroll_into_view_if_needed()
                    shot(page, scope, "coupon")

                    def edit(text):
                        previous = editor.get_by_text("검사한 문구", exact=True).count() > 0
                        for label, value, multiline in (("제목", text, False), ("본문", "", True),
                                                        ("버튼 문구", "", False), ("안내 문구 (한 줄에 하나)", "", True)):
                            field = editor.get_by_label(label, exact=True)
                            field.fill(value)
                            field.press("Control+Enter" if multiline else "Enter")
                            if label == "제목" and previous:
                                editor.locator(".pf-ad .t").filter(has_text="이전에 검사한 문구").wait_for(timeout=90000)
                            page.wait_for_timeout(1000)
                        editor.get_by_role("button", name="수정한 문구 다시 검사", exact=True).click()
                        editor.get_by_text("검사한 문구", exact=True).wait_for(timeout=120000)
                        editor.locator(".pf-ad .h").filter(has_text=text).wait_for(timeout=120000)

                    edit("쿠폰! 지금 확인하세요")
                    check(scope, "manual coupon recheck passes", "이 문구는 검사 기준을 통과했습니다" in editor.inner_text())
                    check(scope, "manual recheck has no restricted finding", "광고 불가 업종·내용" not in editor.inner_text())
                    editor.get_by_text("검사한 문구", exact=True).scroll_into_view_if_needed()
                    shot(page, scope, "coupon-recheck")
                    edit("다! 단 계 영업")
                    check(scope, "real evasion remains rejected", "게재 불가" in editor.inner_text() and "광고 불가 업종·내용" in editor.inner_text())
                    check(scope, "real evasion quote preserved", "다! 단 계" in editor.locator("mark").all_text_contents())
                    editor.get_by_text("검사한 문구", exact=True).scroll_into_view_if_needed()
                    shot(page, scope, "restricted-recheck")
                    page.get_by_text("품질 확인", exact=True).first.click()
                    heading = page.get_by_role("heading", name="2026-10-23 검증 보강 · 채택하지 않은 탐지기 후보")
                    heading.wait_for(timeout=180000)
                    heading.scroll_into_view_if_needed()
                    body = page.inner_text("body")
                    check(scope, "rejected candidate and remaining misses disclosed", "배포하지 않았습니다" in body and "기존 지속 하락 12건 중 7건을 놓치는 문제는 해결되지 않았습니다." in body)
                    check(scope, "frozen before and candidate detection displayed", "11/24" in body and "10/24" in body)
                    check(scope, "synthetic policy limits and human denominator disclosed", "16건 중 4건" in body and "21/28" in body and "AI 정확도가 아닙니다" in body)
                    shot(page, scope, "evidence-strengthening")
                    if scope == "server":
                        # The evidence heading streams before the later trace panel.
                        # Wait for the actual control and inspect its downloaded data.
                        export = page.get_by_role("button", name="운영 기록 JSON 내보내기 (원문 제외)", exact=True)
                        export.wait_for(timeout=120000)
                        with page.expect_download(timeout=60000) as received:
                            export.click()
                        downloaded = args.out / "server-operational-record.json"
                        received.value.save_as(str(downloaded))
                        record = json.loads(downloaded.read_text())
                        check(scope, "operational export excludes raw text by default",
                              record.get("schema_version") == 1 and record.get("privacy") == "allowlisted operational metrics; raw content omitted"
                              and bool(record.get("trace_id")) and bool(record.get("spans"))
                              and all(key not in downloaded.read_text() for key in ('"prompt_user"', '"response_text"', '"product"', '"message"')))
                    check(scope, "no application exception", page.locator('[data-testid="stException"]').count() == 0)
                    check(scope, "no browser provider request", not blocked)
                except Exception as exc:
                    report["errors"].append({"scope": scope, "type": type(exc).__name__, "message": str(exc)[:1000]})
                    print(f"ERROR {scope}: {type(exc).__name__}: {str(exc)[:1000]}", flush=True)
                    shot(page, scope, "failure")
                finally:
                    ctx.close()
            ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = ctx.new_page()
            page.goto(PAGES.replace("/app/", "/replay/") + "?verify=" + args.expected_sha,
                      wait_until="networkidle", timeout=90000)
            notice = page.locator(".notice").inner_text()
            check("replay", "historical execution clearly labeled without new model calls",
                  "이 페이지는 재생입니다" in notice and "새 모델 호출은 하지 않습니다" in notice)
            recorded = json.loads(page.locator("#data").text_content())
            check("replay", "recorded and current policy versions remain separate",
                  bool(recorded["meta"]["recorded_policy_version"]) and bool(recorded["meta"]["policy_version"])
                  and all(s.get("recorded", {}).get("policy_version") for s in recorded["scenarios"]))
            shot(page, "replay", "historical-mode")
            ctx.close()
            browser.close()
        report["status"] = "passed" if not report["errors"] and all(c["ok"] for c in report["checks"]) else "failed"
    except Exception as exc:
        report["errors"].append({"type": type(exc).__name__, "message": str(exc)[:1000]})
        report["status"] = "failed"
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
