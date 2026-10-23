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
    parser.add_argument("--secondary-account", action="store_true")
    parser.add_argument("--expected-prompt")
    parser.add_argument("--passage-probes", "--passage-v3", dest="passage_v3", action="store_true")
    parser.add_argument("--passage-plan", default="evals/live_pilot_passage_plan.json")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha): raise ValueError("full expected commit required")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key: raise ValueError("configured key required for app authentication derivation")
    token = hmac.new(key.encode(), b"preflight-live-pilot-20261023-automation-v1", hashlib.sha256).hexdigest()
    other = hmac.new(key.encode(), b"preflight-live-pilot-20261023-secondary-v1", hashlib.sha256).hexdigest()
    if args.secondary_account: token, other = other, token
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"kind":"authenticated-deployed-live-pilot", "expected_sha":args.expected_sha,
              "started_at":datetime.now(timezone.utc).isoformat(), "mode":"verify_only" if args.verify_only else "execute_frozen_inputs",
              "human_participants":0, "synthetic":True, "checks":[], "runs":[], "screens":[], "status":"running"}
    report["automation_account"] = "secondary" if args.secondary_account else "primary"

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
        # Streamlit scrolls an inner container, so full_page alone does not
        # include the header. Capture the actual header separately, unedited.
        page.locator(".pf-topbar").scroll_into_view_if_needed()
        page.screenshot(path=str(args.out / (name+"-version.png")))
        report["screens"].append(name+"-version.png")
        if page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).count():
            page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).scroll_into_view_if_needed()
        if name.endswith("-saved"):
            details=page.get_by_text("실제 모델 도구 호출과 재검사",exact=True).locator("xpath=ancestor::details")
            if details.count() and details.get_attribute("open") is None:
                details.locator("summary").click()
            if details.count():
                tool_text=details.get_by_text(re.compile(r"수정 \d+ · check_policy"))
                tool_text.first.wait_for(state="visible")
                tool_text.first.scroll_into_view_if_needed()
                page.screenshot(path=str(args.out / (name+"-tool.png")))
                report["screens"].append(name+"-tool.png")
        page.screenshot(path=str(args.out / (name+".png")), full_page=name.endswith("-saved"))
        (args.out / (name+".txt")).write_text(text)
        report["screens"].append(name+".png")
        save()

    from playwright.sync_api import TimeoutError as BrowserTimeout, expect, sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, **({"channel":"chrome"} if os.environ.get("PREFLIGHT_BROWSER_CHANNEL")=="chrome" else {}))
        report["browser"] = browser.version

        def login(credential):
            context = browser.new_context(viewport={"width":1440,"height":1100})
            page = context.new_page()
            page.set_default_timeout(90000)
            report["phase"] = "server_readiness"
            # A browser-side provider call is forbidden; LIVE calls must be on Render.
            page.route(re.compile(r"https://api\.(openai|anthropic)\.com/.*"), lambda route: route.abort())
            deadline = time.monotonic()+600
            load_attempts = 0
            while True:
                load_attempts += 1
                try:
                    response = page.goto(URL, wait_until="domcontentloaded", timeout=90000)
                    report.setdefault("server_loads", []).append({"http_status":response.status if response else None})
                    page.get_by_role("heading", name="LIVE 검토", exact=True).wait_for(timeout=60000)
                except BrowserTimeout:
                    report.setdefault("server_load_errors", []).append({"attempt":load_attempts,"type":"page_readiness_timeout"})
                    save()
                    if load_attempts >= 3:
                        shot(page,"server-readiness-failure")
                        raise
                    continue  # Read-only page load, never a provider request retry.
                try:
                    expect(page.locator(".pf-topbar")).to_contain_text(args.expected_sha[:7],timeout=20000)
                    break
                except AssertionError:
                    pass
                if time.monotonic()>deadline: raise AssertionError("deployed build mismatch")
                page.wait_for_timeout(15000)
            check("server screen build matches requested commit", args.expected_sha[:7] in page.locator(".pf-topbar").inner_text())
            try:
                report["login_phase"]="waiting_for_access_key_input"
                page.get_by_label("LIVE 검토 접근키", exact=True).or_(page.get_by_text("LIVE 설정 대기", exact=False)).first.wait_for()
                if page.get_by_text("LIVE 설정 대기", exact=False).count():
                    shot(page,"live-configuration-missing")
                    raise RuntimeError("live_configuration_missing")
                page.get_by_label("LIVE 검토 접근키", exact=True).fill(credential)
                report["login_phase"]="submitting_access_key"
                page.get_by_role("button", name="LIVE 검토 접속", exact=True).click()
                report["login_phase"]="waiting_for_authenticated_view"
                page.get_by_role("button", name="LIVE 로그아웃", exact=True).wait_for()
                # Account history is rendered after a DB read. The input form
                # follows it, so an early header cannot pass the isolation check.
                page.get_by_label("LIVE 본문", exact=True).wait_for()
            except Exception:
                alerts = page.locator('[data-testid="stAlert"]').all_text_contents()
                report["login_alerts"] = [a for a in alerts if not any(s in a for s in (key,token,other))]
                save()
                raise
            check("authenticated account explicitly marked automation", "계정 유형 automation" in page.inner_text("body"))
            return context, page

        def select(page, label, option):
            report["phase"] = "select:" + label
            page.wait_for_function("""() => !document.querySelector(
                '[data-stale="true"], [data-testid="stStatusWidgetRunningIcon"]')""")
            box=page.get_by_role("combobox",name=label,exact=True)
            # Streamlit's editable combobox need not open when its existing
            # selected value is clicked. Reuse the proven team-review selector.
            if box.input_value() == option: return
            box.press("ArrowDown")
            box.fill(option)
            page.get_by_role("option",name=option,exact=True).click()
            expect(box).to_have_value(option)

        def download(page):
            button=page.get_by_role("button",name="LIVE 운영 기록 다운로드",exact=True)
            button.wait_for()
            with page.expect_download() as info: button.click()
            data=json.loads(Path(info.value.path()).read_text())
            check("operational export contains no original ad, prompts or credentials",
                  not any(x in json.dumps(data) for x in (key,token,other,'"payload"','"sources"','"note"','"messages"')))
            (args.out / (data["request_id"]+".json")).write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
            return data

        def listed_ids(page):
            box=page.get_by_role("combobox",name="내 저장 기록",exact=True)
            if not box.count(): return []
            box.press("ArrowDown")
            box.fill("")
            page.get_by_role("option").first.wait_for()
            ids=page.get_by_role("option").all_text_contents()
            page.keyboard.press("Escape")
            return ids

        def open_record(page, rid):
            select(page,"내 저장 기록",rid)
            page.get_by_role("button",name="선택한 기록 열기",exact=True).click()
            page.get_by_text(re.compile("실행 빌드 .* 요청 " + re.escape(rid))).wait_for()

        try:
            public = browser.new_context(viewport={"width":1440,"height":1100})
            demo = public.new_page()
            deadline = time.monotonic()+600
            while True:
                demo.goto("https://sokldjs554.github.io/toss-ads-ai-project/app/?view=LIVE%20검토&verify="+args.expected_sha+"&navigation="+str(time.time_ns()), wait_until="domcontentloaded", timeout=90000)
                demo.get_by_role("heading", name="LIVE 검토", exact=True).wait_for(timeout=240000)
                report["pages_observed_builds"] = re.findall(r"빌드 ([0-9a-f]{7,40})", demo.inner_text("body"))
                report.setdefault("pages_build_history", []).append({"at":datetime.now(timezone.utc).isoformat(),"builds":report["pages_observed_builds"]})
                save()
                if args.expected_sha[:7] in report["pages_observed_builds"] or time.monotonic()>deadline: break
                demo.wait_for_timeout(15000)
            shot(demo,"pages-version-observed")
            check("Pages build matches requested commit", "빌드 "+args.expected_sha[:7] in demo.inner_text("body"))
            from urllib.parse import unquote
            check("Pages explicitly separates DEMO from authenticated LIVE server", "DEMO 브라우저 실행판" in demo.inner_text("body") and unquote(demo.get_by_role("link", name="인증된 LIVE 검토 열기", exact=True).get_attribute("href"))==unquote(URL))
            shot(demo,"pages-live-server-link")
            public.close()
            if args.passage_v3:
                plan=json.loads((ROOT/args.passage_plan).read_text())
                pending_checks=[]
                # Exactly the two precommitted probes. Repeated browser runs reuse
                # the durable same-input result; no revised retry payloads.
                for index, spec in enumerate(plan["cases"]):
                    credential=token if index==0 else other
                    ctx,page=login(credential)
                    case=json.loads((ROOT/spec["dataset"]).read_text()) if "dataset" in spec else spec
                    if args.verify_only:
                        receipts=[]
                        for rid in listed_ids(page):
                            open_record(page,rid)
                            receipt=download(page)
                            if receipt["prompt_version"]==plan["prompt"]: receipts.append(receipt)
                        check(spec["id"]+": exactly one persisted v3 receipt",len(receipts)==1)
                        data=receipts[0]
                        open_record(page,data["request_id"])
                    else:
                        if spec["parent"]:
                            open_record(page,spec["parent"])
                            page.get_by_role("button",name="이 문구를 직접 수정하고 재검사",exact=True).click()
                            page.get_by_label("LIVE 제목",exact=True).wait_for()
                            expect(page.get_by_label("상품 근거 (한 줄에 한 사실)",exact=True)).to_have_value("\n".join(case["facts"]))
                        for label,field in (("LIVE 제목","headline"),("LIVE 본문","body"),("LIVE 버튼 문구","cta")):
                            page.get_by_label(label,exact=True).fill(case[field])
                        page.get_by_label("LIVE 고지 (한 줄에 한 고지)",exact=True).fill("")
                        page.get_by_label("상품 근거 (한 줄에 한 사실)",exact=True).fill("\n".join(case["facts"]))
                        path_label="RAG 정책 검토" if case["path"]=="rag" else "도구 호출 수정·재검사"
                        page.get_by_text(path_label,exact=True).click()
                        expect(page.get_by_role("radio",name=path_label,exact=True)).to_be_checked()
                        for label,expected in (("LIVE 제목",case["headline"]),("LIVE 본문",case["body"]),("LIVE 버튼 문구",case["cta"]),("상품 근거 (한 줄에 한 사실)","\n".join(case["facts"]))):
                            expect(page.get_by_label(label,exact=True)).to_have_value(expected)
                        report["phase"]=spec["id"]+":execute"
                        page.get_by_role("button",name="실제 LLM 실행",exact=True).click()
                        page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for(timeout=240000)
                        data=download(page)
                        if not page.get_by_text("검토 저장됨",exact=False).count():
                            select(page,"사람 검토 결과","승인" if data["decision"]=="APPROVE" and data["status"]=="human_review" else "반려")
                            page.get_by_label("검토 사유",exact=True).fill("고정 합성 사례의 실제 API·자동 브라우저 검증. 사람 참여가 아닙니다.")
                            page.get_by_role("button",name="검토 결과 저장",exact=True).click()
                            page.get_by_text("검토 저장됨",exact=False).wait_for()
                        data=download(page)
                    report["runs"].append({"case_id":spec["id"],"evaluation_split":spec["split"],**data})
                    pending_checks.append((spec["id"]+": actual bounded model and prompt",0<data["call_count"]<=4 and data["revisions"]<=2 and data["prompt_version"]==plan["prompt"] and all(c["model"]==plan["model"] for c in data["calls"])))
                    pending_checks.append((spec["id"]+": frozen original code decision",data["original_code_decision"]==case["expected_code"]))
                    pending_checks.append((spec["id"]+": automation audit exactly once",len(data["audit"])==1 and data["audit"][0]["actor_kind"]=="automation"))
                    if not args.verify_only: pending_checks.append((spec["id"]+": execution build matches deployment",data["build"]==args.expected_sha))
                    shot(page,spec["id"]+"-saved")
                    ctx.close()
                    ctx,page=login(credential)
                    open_record(page,data["request_id"])
                    again=download(page)
                    check(spec["id"]+": fresh session preserves receipt without new call",again["calls"]==data["calls"] and again["audit"]==data["audit"])
                    shot(page,spec["id"]+"-reconnected")
                    ctx.close()
                    ctx,page=login(other if index==0 else token)
                    check(spec["id"]+": other account cannot list receipt",data["request_id"] not in listed_ids(page))
                    ctx.close()
                for label,okay in pending_checks:
                    report["checks"].append({"check":label,"ok":bool(okay)})
                    print(("PASS " if okay else "FAIL ")+label,flush=True)
                save()
                check("all fixed probe contracts",all(okay for _,okay in pending_checks))
                check("RAG source-verified human handoff",any(r["path"]=="rag" and r["status"]=="human_review" for r in report["runs"]))
                check("tools revised/rechecked APPROVE human handoff",any(r["path"]=="tools" and r["status"]=="human_review" and r["decision"]=="APPROVE" and r["revisions"]>=1 for r in report["runs"]))
                report["status"]="passed"
                return 0
            ctx,page=login(token)
            shot(page,"authenticated-home")
            cases=json.loads((ROOT/"evals/datasets/live_pilot_20261023.json").read_text())["cases"]
            if not args.verify_only:
                for i,case in enumerate(cases):
                    report["phase"] = case["id"] + ":load"
                    if i:
                        page.get_by_role("button",name="다른 광고 검토",exact=True).click()
                        page.get_by_role("button",name="LIVE 예시 불러오기",exact=True).wait_for()
                    if i==3:
                        # Server's 3 new requests/minute limit; no network retry or new payload.
                        page.wait_for_timeout(60000)
                    select(page,"합성 입력 예시",case["id"])
                    page.get_by_role("button",name="LIVE 예시 불러오기",exact=True).click()
                    # The input already exists before Streamlit processes the preset
                    # rerun. Visibility alone can read the previous (empty) value.
                    expect(page.get_by_label("LIVE 제목",exact=True)).to_have_value(case["headline"], timeout=90000)
                    for label,expected in (("LIVE 본문",case["body"]),("LIVE 버튼 문구",case["cta"]),("상품 근거 (한 줄에 한 사실)","\n".join(case["facts"]))):
                        expect(page.get_by_label(label,exact=True)).to_have_value(expected,timeout=90000)
                    path_label="RAG 정책 검토" if case["path"]=="rag" else "도구 호출 수정·재검사"
                    expect(page.get_by_role("radio",name=path_label,exact=True)).to_be_checked()
                    check(case["id"]+": frozen headline loaded",page.get_by_label("LIVE 제목",exact=True).input_value()==case["headline"])
                    check(case["id"]+": all frozen fields loaded before provider execution",True)
                    report["phase"] = case["id"] + ":execute"
                    page.get_by_role("button",name="실제 LLM 실행",exact=True).click()
                    page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for(timeout=240000)
                    report["phase"] = case["id"] + ":export"
                    data=download(page)
                    check(case["id"]+": actual server calls recorded",data["call_count"]>0 and data["mode"]=="LIVE")
                    if args.expected_prompt:
                        check(case["id"]+": requested prompt version executed",data["prompt_version"]==args.expected_prompt)
                    check(case["id"]+": original code outcome matches frozen expectation",data["original_code_decision"]==case["expected_code"])
                    check(case["id"]+": bounded calls",data["call_count"]<=4 and data["revisions"]<=2)
                    check(case["id"]+": actual model id and usage ledger",
                          all(c["model"]=="gpt-4o-mini-2024-07-18" and "cost_usd" in c for c in data["calls"]))
                    shot(page,case["id"]+"-result")
                    # Automatic audit exercise; never count as human participation.
                    if not page.get_by_text("검토 저장됨",exact=False).count():
                        review="승인" if data["decision"]=="APPROVE" and data["status"]=="human_review" else "반려"
                        select(page,"사람 검토 결과",review)
                        report["phase"] = case["id"] + ":save_review"
                        page.get_by_label("검토 사유",exact=True).fill("합성 광고 자동 브라우저 검증. 실제 사람 판단이 아닙니다.")
                        page.get_by_role("button",name="검토 결과 저장",exact=True).click()
                        page.get_by_text("검토 저장됨",exact=False).wait_for()
                    data=download(page)
                    check(case["id"]+": automated audit saved once",len(data["audit"])==1 and data["audit"][0]["actor_kind"]=="automation")
                    report["runs"].append({"case_id":case["id"],**data})
                    shot(page,case["id"]+"-saved")
                    save()
                manual=json.loads((ROOT/"evals/datasets/live_pilot_manual_20261023.json").read_text())
                parent=next(r for r in report["runs"] if r["case_id"]==manual["parent_case"])
                open_record(page,parent["request_id"])
                page.get_by_role("button",name="이 문구를 직접 수정하고 재검사",exact=True).click()
                page.get_by_label("LIVE 제목",exact=True).wait_for()
                parent_case=next(c for c in cases if c["id"]==manual["parent_case"])
                expect(page.get_by_label("상품 근거 (한 줄에 한 사실)",exact=True)).to_have_value("\n".join(parent_case["facts"]),timeout=90000)
                page.get_by_label("LIVE 제목",exact=True).fill(manual["headline"])
                page.get_by_text("RAG 정책 검토",exact=True).click()
                expect(page.get_by_role("radio",name="RAG 정책 검토",exact=True)).to_be_checked()
                page.get_by_role("button",name="실제 LLM 실행",exact=True).click()
                page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for(timeout=240000)
                data=download(page)
                check("direct edit creates checked result",data["original_code_decision"]==manual["expected_code"] and data["path"]=="rag")
                if not page.get_by_text("검토 저장됨",exact=False).count():
                    select(page,"사람 검토 결과","수정 필요")
                    page.get_by_label("검토 사유",exact=True).fill("합성 직접 수정·재검사 자동 확인. 실제 사람 검토가 아닙니다.")
                    page.get_by_role("button",name="검토 결과 저장",exact=True).click()
                    page.get_by_text("검토 저장됨",exact=False).wait_for()
                data=download(page)
                check("manual probe exercises edit audit without claiming human judgment",data["review_decision"]=="edit" and data["audit"][0]["actor_kind"]=="automation")
                report["runs"].append({"case_id":manual["id"],**data})
                shot(page,"manual-edit-rechecked-saved")
            else:
                # Read every existing receipt after a deployment; never press Run.
                ids=listed_ids(page)
                check("existing persisted runs available",len(ids)>=5)
                for rid in ids:
                    open_record(page,rid)
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
            check("second authenticated account cannot list first account records",not (set(listed_ids(page)) & {r["request_id"] for r in report["runs"]}))
            shot(page,"isolated-second-account")
            if args.secondary_account and not args.verify_only:
                # Separate, precommitted heldout input, within the other test
                # account's existing cap. Never change it after seeing output.
                fresh=json.loads((ROOT/"evals/datasets/live_pilot_structured_holdout_20261023.json").read_text())
                report["phase"]="heldout:execute"
                for label,field in (("LIVE 제목","headline"),("LIVE 본문","body"),("LIVE 버튼 문구","cta")):
                    page.get_by_label(label,exact=True).fill(fresh[field])
                page.get_by_label("상품 근거 (한 줄에 한 사실)",exact=True).fill("\n".join(fresh["facts"]))
                page.get_by_text("도구 호출 수정·재검사",exact=True).click()
                expect(page.get_by_role("radio",name="도구 호출 수정·재검사",exact=True)).to_be_checked()
                page.get_by_role("button",name="실제 LLM 실행",exact=True).click()
                page.get_by_role("heading",name="저장된 LIVE 검토",exact=True).wait_for(timeout=240000)
                data=download(page)
                check("heldout: fixed original code expectation",data["original_code_decision"]==fresh["expected_code"])
                check("heldout: bounded actual tools and requested prompt",data["path"]=="tools" and 0<data["call_count"]<=4 and data["revisions"]<=2 and data["prompt_version"]==args.expected_prompt)
                if not page.get_by_text("검토 저장됨",exact=False).count():
                    select(page,"사람 검토 결과","승인" if data["decision"]=="APPROVE" and data["status"]=="human_review" else "반려")
                    page.get_by_label("검토 사유",exact=True).fill("별도 고정 합성 사례의 자동 검증이며 사람 판단이 아닙니다.")
                    page.get_by_role("button",name="검토 결과 저장",exact=True).click()
                    page.get_by_text("검토 저장됨",exact=False).wait_for()
                data=download(page)
                report["runs"].append({"case_id":fresh["id"],"evaluation_split":"heldout",**data})
                shot(page,"heldout-minimum-purchase-saved")
            ctx.close()
            if not args.verify_only:
                # Collect all frozen cases and persistence checks even if a
                # workflow acceptance check fails. Keep the same expectations.
                check("RAG path actually completed with verified source membership",any(r["path"]=="rag" and r["status"]=="human_review" for r in report["runs"]))
                check("tool path actually revised, rechecked and reached human handoff",any(r["path"]=="tools" and r["status"]=="human_review" and r["decision"]=="APPROVE" and r["revisions"]>=1 for r in report["runs"]))
            report["status"]="passed"
        except Exception as exc:
            report["status"]="failed"
            report["error_type"]=type(exc).__name__
            if "page" in locals():
                alerts = page.locator('[data-testid="stAlert"]').all_text_contents()
                report["failure_alerts"] = [a for a in alerts if not any(s in a for s in (key,token,other))]
                try: shot(page,"failure-view")
                except Exception: pass  # Preserve the original failure; never expose credentials.
            # Error messages can contain page input or credentials; retain type only.
            save()
            raise RuntimeError("browser_verification_failed; see redacted result.json") from None
        finally:
            report["finished_at"]=datetime.now(timezone.utc).isoformat()
            save()
            browser.close()
    return 0


if __name__=="__main__": raise SystemExit(main())
