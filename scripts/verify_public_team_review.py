"""Exercise an explicitly configured public shared-review workspace.

Creates one clearly synthetic item and two temporary reviewer accounts, uses
isolated browser contexts, and disables those accounts afterwards. No model
calls, actual human labels, or real advertiser data are produced. Credentials
are read/written only in a private local directory, never in public evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
HOST = "preflight-toss-ads.onrender.com"
KEY = re.compile(r"pfk_[A-Za-z0-9_-]{43}\Z")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".team-browser-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-operation", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--exercise-team-workspace", action="store_true")
    args = parser.parse_args()
    if not args.exercise_team_workspace:
        parser.error("The exercise flag is required before creating synthetic records.")
    operation = args.owner_operation.absolute()
    info = operation.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1:
        parser.error("Owner input must be an owned regular 0600 file.")
    owner_key = json.loads(operation.read_text(encoding="utf-8")).get("access_key", "")
    if not KEY.fullmatch(owner_key):
        parser.error("Owner input is not a valid operation file.")
    evidence = args.out.absolute()
    if evidence.exists() and any(evidence.iterdir()):
        parser.error("Use a new evidence directory.")
    evidence.mkdir(parents=True, exist_ok=True)
    suffix = uuid4().hex[:8]
    private_path = operation.parent / f"team-browser-accounts-{suffix}.json"
    credentials = {"kind": "synthetic-browser-check", "created": [], "disabled": []}
    private_json(private_path, credentials)
    report = {
        "kind": "public-shared-review-browser", "url": f"https://{HOST}/",
        "started_at": utc(), "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "synthetic": True, "identity_verified": False, "independence_verified": False,
        "actual_human_labels": 0, "checks": [], "page_error_types": [],
        "blocked_model_requests": 0, "created_accounts": 0, "disabled_accounts": 0,
        "status": "running", "tls_errors_ignored": False,
    }
    step = "open public service"

    def save():
        (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def check(label, okay):
        report["checks"].append({"check": label, "ok": bool(okay)})
        save()
        print(f"{'PASS' if okay else 'FAIL'} {label}", flush=True)
        if not okay:
            raise AssertionError(label)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        contexts = []

        def new_page():
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            contexts.append(context)
            page = context.new_page()
            page.set_default_timeout(60000)
            page.on("pageerror", lambda _: report["page_error_types"].append("browser_error"))

            def block(route):
                report["blocked_model_requests"] += 1
                route.abort()

            page.route(re.compile(r"https://api\.(?:openai|anthropic)\.com/.*"), block)
            return page

        def open_team(page):
            page.goto(report["url"], wait_until="domcontentloaded", timeout=90000)
            page.get_by_role("radio", name="운영", exact=True).wait_for(timeout=180000)
            page.get_by_role("radio", name="운영", exact=True).click()
            page.get_by_role("tab", name="공동 검수", exact=True).click()
            page.get_by_label("접근키", exact=True).wait_for()

        def area(page):
            return page.get_by_role("tabpanel", name="공동 검수", exact=True)

        def login(page, key, role_heading):
            address = urlsplit(page.url)
            check("credential destination remains the requested HTTPS service", address.scheme == "https" and address.hostname == HOST)
            area(page).get_by_label("접근키", exact=True).fill(key)
            area(page).get_by_role("button", name="공동 검수 접속", exact=True).click()
            area(page).get_by_role("heading", name=role_heading, exact=True).wait_for()

        def select_cleanup_account(page, name):
            # The heading is rendered after the account list. A failed account
            # read never produces the account heading required by login().
            area(page).get_by_role("heading", name="검수 결과 내보내기", exact=True).wait_for()
            page.wait_for_function("""() => !document.querySelector(
                '[data-stale="true"], [data-testid="stStatusWidgetRunningIcon"]')""")
            selector = area(page).get_by_role("combobox", name="관리할 담당자", exact=True)
            if selector.count() == 0:
                area(page).get_by_text(
                    "검수자 또는 조정자를 발급하면 키 재발급과 계정 비활성화를 할 수 있습니다.", exact=True,
                ).wait_for()
                return False
            previous = selector.input_value()
            selector.click()
            selector.fill(name)
            option = page.get_by_role("option", name=name, exact=True)
            empty = page.get_by_text("No results", exact=True)
            option.or_(empty).first.wait_for()
            if option.count() == 0:
                empty.wait_for()
                selector.press("Escape")
                return False
            if previous != name:
                # Selecting a different account changes the client input before
                # Streamlit rerenders its action button. Observe that rerender
                # so the previous account's inactive button cannot count as
                # confirmation for the newly selected account. Missing render
                # evidence times out and leaves cleanup explicitly incomplete.
                page.evaluate("""() => {
                    const state = {seen: false};
                    const busy = '[data-stale="true"], [data-testid="stStatusWidgetRunningIcon"]';
                    state.observer = new MutationObserver(records => {
                        if (document.querySelector(busy) || records.some(record =>
                            record.attributeName === 'data-stale' && record.oldValue === 'true')) {
                            state.seen = true;
                        }
                    });
                    state.observer.observe(document.body, {
                        subtree: true, childList: true, attributes: true,
                        attributeFilter: ['data-stale'], attributeOldValue: true,
                    });
                    window.__teamCleanupRender = state;
                }""")
            try:
                option.click()
                if previous != name:
                    page.wait_for_function("""() => window.__teamCleanupRender.seen &&
                        !document.querySelector('[data-stale="true"], [data-testid="stStatusWidgetRunningIcon"]')""")
            finally:
                if previous != name:
                    page.evaluate("""() => {
                        window.__teamCleanupRender?.observer.disconnect();
                        delete window.__teamCleanupRender;
                    }""")
            if selector.input_value() != name:
                raise AssertionError("cleanup account selection was not confirmed")
            return True

        owner = new_page()
        try:
            open_team(owner)
            check("access key is masked", owner.get_by_label("접근키", exact=True).get_attribute("type") == "password")
            owner.screenshot(path=str(evidence / "shared-review-login.png"))
            login(owner, owner_key, "담당자 계정")
            for number in (1, 2):
                step = f"create temporary reviewer {number}"
                name = f"기능 확인 {number} {suffix}"
                entry = {"name": name, "key": None, "creation_confirmed": False}
                credentials["created"].append(entry)
                private_json(private_path, credentials)
                area(owner).get_by_label("새 담당자 이름", exact=True).fill(name)
                area(owner).get_by_role("button", name="담당자 계정·접근키 발급", exact=True).click()
                area(owner).get_by_text(f"{name} 님의 새 접근키입니다.", exact=False).wait_for()
                entry["creation_confirmed"] = True
                report["created_accounts"] += 1
                raw_keys = [text.strip() for text in area(owner).get_by_test_id("stCode").all_text_contents() if KEY.fullmatch(text.strip())]
                check("one newly issued key is shown once", len(raw_keys) == 1)
                entry["key"] = raw_keys[0]
                private_json(private_path, credentials)
            step = "enqueue explicitly synthetic example"
            area(owner).get_by_text("광고 직접 입력", exact=True).click()
            area(owner).get_by_label("출처 ID (광고마다 다른 값)", exact=True).fill(f"public-browser-check-{suffix}")
            area(owner).get_by_label("상품 이름", exact=True).fill("자동화 기능 확인용 가상 노트")
            headline = f"공동 저장 기능 확인 {suffix}"
            area(owner).get_by_label("광고 제목", exact=True).fill(headline)
            area(owner).get_by_label("광고 본문", exact=True).fill("실제 판매나 사람 검수가 아닌 자동화된 합성 예시입니다.")
            check("manual example is explicitly synthetic", area(owner).get_by_label("합성 예시로 표시", exact=True).last.is_checked())
            area(owner).get_by_role("button", name="입력한 광고 추가", exact=True).click()
            area(owner).get_by_text("광고를 공동 검수 대기열에 담았습니다.", exact=False).wait_for()
            for number, entry in enumerate(credentials["created"], 1):
                step = f"reviewer {number} claim and submit"
                page = new_page()
                open_team(page)
                login(page, entry["key"], "내 검수")
                check("reviewer cannot open account administration", page.get_by_role("heading", name="담당자 계정", exact=True).count() == 0)
                area(page).get_by_role("button", name="다음 광고 배정받기", exact=True).click()
                area(page).get_by_text(headline, exact=True).wait_for()
                if number == 1:
                    step = "fresh page restores held work through server login"
                    page.close()
                    page = new_page()
                    open_team(page)
                    login(page, entry["key"], "내 검수")
                    area(page).get_by_text(headline, exact=True).wait_for()
                    check("held item survives a new browser context", True)
                reason = f"자동화 기능 확인 계정 {number}의 합성 제출이며 실제 사람 검수 결과가 아닙니다."
                other = f"자동화 기능 확인 계정 {3-number}의 합성 제출"
                check("another account's rationale is not shown", other not in page.inner_text("body"))
                area(page).get_by_role("radio", name="점검 통과", exact=True).check()
                area(page).get_by_label("검토 이유", exact=True).fill(reason)
                area(page).get_by_role("button", name="검토 저장", exact=True).click()
                area(page).get_by_text("검토를 저장했습니다.", exact=False).wait_for()
                check("review submission was confirmed", True)
                check("no other account statistics after submission", "전체 1건" not in page.inner_text("body"))
                page.close()
            step = "synthetic export exclusion and explicit inclusion"
            area(owner).get_by_role("button", name="내보낼 결과 확인", exact=True).click()
            area(owner).get_by_text("선택한 범위에서 내보낼 완료 결과가 없습니다.", exact=True).wait_for()
            check("synthetic records are excluded by default", True)
            area(owner).get_by_label("합성 예시도 포함", exact=True).check()
            area(owner).get_by_role("button", name="내보낼 결과 확인", exact=True).click()
            area(owner).get_by_text("평가셋 후보 1건 · 합성 예시 포함", exact=True).wait_for()
            check("explicit synthetic export contains the completed item", True)
            with owner.expect_download() as received:
                area(owner).get_by_role("button", name="검수 결과 JSONL 내려받기", exact=True).click()
            content = Path(received.value.path()).read_text(encoding="utf-8")
            check("export identifies unverified identity and independence", "identity_verified=false; independence_verified=false; synthetic_included=true" in content)
            check("export contains no access key or database URL", not re.search(r"pfk_[A-Za-z0-9_-]{43}|postgres(?:ql)?://", content))
            report["export_sha256"] = hashlib.sha256(content.encode()).hexdigest()
            check("no browser page error or model request", not report["page_error_types"] and report["blocked_model_requests"] == 0)
            report["status"] = "completed"
        except Exception as exc:
            # Playwright exception messages can echo fill() credentials. Store
            # only the step and exception class, never the message or trace.
            report["status"] = "interrupted"
            report["error"] = {"step": step, "type": type(exc).__name__}
            print(f"INTERRUPTED {step}: {type(exc).__name__}", flush=True)
        finally:
            for number, entry in enumerate(credentials["created"], 1):
                if entry["name"] in credentials["disabled"]:
                    continue
                try:
                    # The original session may be waiting on an uncertain
                    # creation/export result and cannot render administration.
                    # Each cleanup gets a fresh authenticated owner session.
                    cleanup = new_page()
                    open_team(cleanup)
                    login(cleanup, owner_key, "담당자 계정")
                    if not select_cleanup_account(cleanup, entry["name"]):
                        if entry["creation_confirmed"]:
                            raise AssertionError("previously confirmed account is missing")
                        entry["cleanup_status"] = "not_created"
                        private_json(private_path, credentials)
                        report["not_created_accounts"] = report.get("not_created_accounts", 0) + 1
                        report.setdefault("cleanup_results", []).append({"account_number": number, "status": "not_created"})
                        continue
                    if not entry["creation_confirmed"]:
                        entry["creation_confirmed"] = True
                        report["created_accounts"] += 1
                        private_json(private_path, credentials)
                    inactive = area(cleanup).get_by_role("button", name="계정 다시 활성화", exact=True)
                    active = area(cleanup).get_by_role("button", name="계정 비활성화", exact=True)
                    inactive.or_(active).first.wait_for()
                    if inactive.count() == 0:
                        active.click()
                    # The selected account's current state, not a generic
                    # success notice, is the cleanup completion condition.
                    inactive.wait_for()
                    if area(cleanup).get_by_role("combobox", name="관리할 담당자", exact=True).input_value() != entry["name"]:
                        raise AssertionError("cleanup account selection changed")
                    entry["cleanup_status"] = "disabled"
                    credentials["disabled"].append(entry["name"])
                    private_json(private_path, credentials)
                    report["disabled_accounts"] += 1
                    report.setdefault("cleanup_results", []).append({"account_number": number, "status": "disabled"})
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append({"account_number": number, "type": type(exc).__name__})
            if report["created_accounts"] != report["disabled_accounts"] or report.get("cleanup_errors"):
                report["status"] = "cleanup_incomplete"
            for context in contexts:
                context.close()
            browser.close()
            report["finished_at"] = utc()
            save()
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
