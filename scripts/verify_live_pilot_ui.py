"""Actual deployed server API executions via authenticated Chromium.

Only frozen synthetic inputs and isolated pilot accounts. Provider credentials
stay in the Actions environment and Render environment, never browser inputs.
Authentication uses a domain-separated opaque token. Existing records are reused
by the server; verification-only reconnect/redeploy mode never presses Run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]
URL = "https://preflight-toss-ads.onrender.com/?view=LIVE%20검토"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha): raise ValueError("full expected commit required")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key: raise ValueError("configured key required for app authentication derivation")
    token = hmac.new(key.encode(), b"preflight-live-pilot-20261023-automation-v1", hashlib.sha256).hexdigest()
    other = hmac.new(key.encode(), b"preflight-live-pilot-20261023-secondary-v1", hashlib.sha256).hexdigest()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"kind":"authenticated-deployed-live-pilot", "expected_sha":args.expected_sha,
              "started_at":datetime.now(timezone.utc).isoformat(), "mode":"verify_only" if args.verify_only else "execute_frozen_inputs",
              "human_participants":0, "synthetic":True, "checks":[], "runs":[], "screens":[], "status":"running"}

    def save(): (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    def check(label, okay):
        report["checks"].append({"check":label, "ok":bool(okay)})
        save()
        print(("PASS " if okay else "FAIL ") + label, flush=True)
        if not okay: raise AssertionError(label)

    def shot(page, name):
        # Never capture a password input, even masked.
        if page.get_by_label("LIVE 검토 접근키", exact=True).count(): return
        text = page.inner_text("body")
        if any(s in text for s in (key, token, other)): raise AssertionError("secret visible")
        page.screenshot(path=str(args.out / (name+".png")), full_page=False)
        (args.out / (name+".txt")).write_text(text)
        report["screens"].append(name+".png")
        save()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        report["browser"] = browser.version

        def login(credential):
            context = browser.new_context(viewport={"width":1440,"height":1100})
            page = context.new_page()
            page.set_default_timeout(90000)
            # A browser-side provider call is forbidden; LIVE calls must be on Render.
            page.route(re.compile(r"https://api\.(openai|anthropic)\.com/.*"), lambda route: route.abort())
            deadline = time.monotonic()+600
            while True:
                page.goto(URL, wait_until="domcontentloaded", timeout=90000)
                page.get_by_role("heading", name="LIVE 검토", exact=True).wait_for(timeout=180000)
                if "빌드 "+args.expected_sha[:7] in page.inner_text("body"): break
                if time.monotonic()>deadline: raise AssertionError("deployed build mismatch")
                page.wait_for_timeout(15000)
            check("server screen build matches requested commit", "빌드 "+args.expected_sha[:7] in page.inner_text("body"))
            page.get_by_label("LIVE 검토 접근키", exact=True).fill(credential)
            page.get_by_role("button", name="LIVE 검토 접속", exact=True).click()
            page.get_by_role("button", name="LIVE 로그아웃", exact=True).wait_for()
            check("authenticated account explicitly marked automation", "계정 유형 automation" in page.inner_text("body"))
            return context, page

        def select(page, label, option):
            box=page.get_by_role("combobox",name=label,exact=True)
            box.click()
            page.get_by_role("option",name=option,exact=True).click()

        def download(page):
            button=page.get_by_role("button",name="LIVE 운영 기록 다운로드",exact=True)
            button.wait_for()
            with page.expect_download() as info: button.click()
            data=json.loads(Path(info.value.path()).read_text())
            check("operational export contains no original ad, prompts or credentials",
                  not any(x in json.dumps(data) for x in (key,token,other,'"payload"','"sources"','"note"','"messages"')))
            (args.out / (data["request_id"]+".json")).write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
            return data

        try:
            public = browser.new_context(viewport={"width":1440,"height":1100})
            demo = public.new_page()
            deadline = time.monotonic()+600
            while True:
                demo.goto("https://sokldjs554.github.io/toss-ads-ai-project/app/?view=LIVE%20검토&verify="+args.expected_sha, wait_until="domcontentloaded", timeout=90000)
                demo.get_by_role("heading", name="LIVE 검토", exact=True).wait_for(timeout=240000)
                report["pages_observed_builds"] = re.findall(r"빌드 ([0-9a-f]{7,40})", demo.inner_text("body"))
                if args.expected_sha[:7] in report["pages_observed_builds"] or time.monotonic()>deadline: break
                demo.wait_for_timeout(15000)
            shot(demo,"pages-version-observed")
            check("Pages build matches requested commit", "빌드 "+args.expected_sha[:7] in demo.inner_text("body"))
            from urllib.parse import unquote
            check("Pages explicitly separates DEMO from authenticated LIVE server", "DEMO 브라우저 실행판" in demo.inner_text("body") and unquote(demo.get_by_role("link", name="인증된 LIVE 검토 열기", exact=True).get_attribute("href"))==unquote(URL))
            shot(demo,"pages-live-server-link")
            public.close()
            ctx,page=login(token)
            shot(page,"authenticated-home")
            cases=json.loads((ROOT/"evals/datasets/live_pilot_20261023.json").read_text())["cases"]
            if not args.verify_only:
                for i,case in enumerate(cases):
                    if i:
                        page.get_by_role("button",name="다른 광고 검토",exact=True).click()
                        page.get_by_role("button",name="LIVE 예시 불러오기",exact=True).wait_for()
                    if i==3:
                        # Server's 3 new requests/minute limit; no network retry or new payload.
                        page.wait_for_timeout(60000)
                    select(page,"합성 입력 예시",case["id"])
                    page.get_by_role("button",name="LIVE 예시 불러오기",exact=True).click()
                    page.get_by_label("LIVE 제목",exact=True).wait_for()
                    check(case["id"]+": frozen headline loaded",page.get_by_label("LIVE 제목",exact=True).input_value()==case["headline"])
                    page.get_by_role("button",name="실제 LLM 실행",exact=True).click()
                    page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for(timeout=240000)
                    data=download(page)
                    check(case["id"]+": actual server calls recorded",data["call_count"]>0 and data["mode"]=="LIVE")
                    check(case["id"]+": original code outcome matches frozen expectation",data["original_code_decision"]==case["expected_code"])
                    check(case["id"]+": bounded calls",data["call_count"]<=4 and data["revisions"]<=2)
                    check(case["id"]+": actual model id and usage ledger",
                          all(c["model"]=="gpt-4o-mini-2024-07-18" and "cost_usd" in c for c in data["calls"]))
                    shot(page,case["id"]+"-result")
                    # Automatic audit exercise; never count as human participation.
                    if not page.get_by_text("검토 저장됨",exact=False).count():
                        review="승인" if data["decision"]=="APPROVE" and data["status"]=="human_review" else "반려"
                        select(page,"사람 검토 결과",review)
                        page.get_by_label("검토 사유",exact=True).fill("합성 광고 자동 브라우저 검증. 실제 사람 판단이 아닙니다.")
                        page.get_by_role("button",name="검토 결과 저장",exact=True).click()
                        page.get_by_text("검토 저장됨",exact=False).wait_for()
                    data=download(page)
                    check(case["id"]+": automated audit saved once",len(data["audit"])==1 and data["audit"][0]["actor_kind"]=="automation")
                    report["runs"].append({"case_id":case["id"],**data})
                    shot(page,case["id"]+"-saved")
                    save()
                check("RAG path actually completed with verified source membership",any(r["path"]=="rag" and r["status"]=="human_review" for r in report["runs"]))
                check("tool path actually revised, rechecked and reached human handoff",any(r["path"]=="tools" and r["status"]=="human_review" and r["decision"]=="APPROVE" and r["revisions"]>=1 for r in report["runs"]))
                manual=json.loads((ROOT/"evals/datasets/live_pilot_manual_20261023.json").read_text())
                parent=next(r for r in report["runs"] if r["case_id"]==manual["parent_case"])
                select(page,"내 저장 기록",parent["request_id"])
                page.get_by_role("button",name="선택한 기록 열기",exact=True).click()
                page.get_by_role("button",name="이 문구를 직접 수정하고 재검사",exact=True).click()
                page.get_by_label("LIVE 제목",exact=True).wait_for()
                page.get_by_label("LIVE 제목",exact=True).fill(manual["headline"])
                page.get_by_role("radio",name="RAG 정책 검토",exact=True).check()
                page.get_by_role("button",name="실제 LLM 실행",exact=True).click()
                page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for(timeout=240000)
                data=download(page)
                check("direct edit creates checked result",data["original_code_decision"]==manual["expected_code"] and data["path"]=="rag")
                if not page.get_by_text("검토 저장됨",exact=False).count():
                    select(page,"사람 검토 결과","승인" if data["decision"]=="APPROVE" and data["status"]=="human_review" else "수정 필요")
                    page.get_by_label("검토 사유",exact=True).fill("합성 직접 수정·재검사 자동 확인. 실제 사람 검토가 아닙니다.")
                    page.get_by_role("button",name="검토 결과 저장",exact=True).click()
                    page.get_by_text("검토 저장됨",exact=False).wait_for()
                data=download(page)
                report["runs"].append({"case_id":manual["id"],**data})
                shot(page,"manual-edit-rechecked-saved")
            else:
                # Read every existing receipt after a deployment; never press Run.
                options=page.get_by_role("combobox",name="내 저장 기록",exact=True)
                options.click()
                ids=page.get_by_role("option").all_text_contents()
                page.keyboard.press("Escape")
                check("existing persisted runs available",len(ids)>=5)
                for rid in ids:
                    select(page,"내 저장 기록",rid)
                    page.get_by_role("button",name="선택한 기록 열기",exact=True).click()
                    page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for()
                    data=download(page)
                    check("persisted audit available after redeploy",bool(data["audit"]))
                    report["runs"].append(data)
                shot(page,"after-redeploy-persisted")
            ctx.close()
            # A fresh browser session must load receipts without charging again.
            ctx,page=login(token)
            page.get_by_role("button",name="선택한 기록 열기",exact=True).click()
            page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for()
            data=download(page)
            previous=next(r for r in report["runs"] if r["request_id"]==data["request_id"])
            check("reconnect preserves call count and review",data["call_count"]==previous["call_count"] and data["audit"]==previous["audit"])
            shot(page,"reconnected-receipt")
            ctx.close()
            ctx,page=login(other)
            check("second authenticated account cannot list first account records",page.get_by_role("combobox",name="내 저장 기록",exact=True).count()==0)
            shot(page,"isolated-second-account")
            ctx.close()
            report["status"]="passed"
        except Exception as exc:
            report["status"]="failed"
            report["error_type"]=type(exc).__name__
            # Error messages can contain page input or credentials; retain type only.
            save()
            raise RuntimeError("browser_verification_failed; see redacted result.json") from None
        finally:
            report["finished_at"]=datetime.now(timezone.utc).isoformat()
            save()
            browser.close()
    return 0


if __name__=="__main__": raise SystemExit(main())
