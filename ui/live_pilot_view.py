"""Authenticated, server-only LIVE pilot. No key entry, demo fallback, or publication."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import streamlit as st

from agents.live_pilot import MODEL, run_workflow
from core.config import IN_BROWSER
from core.schemas import AdCopy, ProductFacts, ProductCategory, Fact
from llm.live_pilot_gateway import LiveGateway
from ops.live_pilot_store import LiveStore, PilotDenied, operational_summary

URL = "https://preflight-toss-ads.onrender.com/?view=LIVE%20검토"
ROOT = Path(__file__).resolve().parents[1]


def _logout():
    for key in list(st.session_state):
        if key.startswith("pilot_"):
            del st.session_state[key]


def _display(store, token, request_id):
    row = store.get(token, request_id)
    st.subheader("저장된 LIVE 검토")
    st.caption(f"실행 빌드 {row['build'][:7]} · 요청 {row['request_id']} · {row['created_at']}")
    result = row.get("result")
    if not result:
        st.warning("실행 결과를 아직 확인할 수 없습니다. 중복 호출 없이 기록을 조회하세요. 중단된 요청은 보류합니다.")
        if st.button("저장 기록 다시 조회", key="pilot_refresh"): st.rerun()
        return
    st.write(f"경로: {result['path']} · 상태: {result['status']} · 최종 검사: {result['decision']}")
    st.caption("코드 검사는 반려 근거를 강제합니다. 모델 의견과 인용 존재 검증은 의미적 타당성의 증명이 아니며, 사람 검토가 필요합니다.")
    st.write(f"코드 검사 {result['code_decision']} · 수정 {result['revisions']}회 · 사유 {result['reason']}")
    for finding in result["code_findings"]:
        st.write(f"{finding['rule_id']} · {finding['quote']} · {finding['explanation']}")
    st.markdown("**검토할 문구**")
    ad = result["copy"]
    st.text("\n".join([ad["headline"], ad["body"], ad["cta"], *ad.get("disclosures", [])]))
    if result.get("sources"):
        with st.expander("검색한 정책·상품 근거", expanded=True):
            for source in result["sources"]:
                st.write(f"{source['id']} · {source['version']} · {source['kind']}")
                st.text(source["text"])
    if result.get("review"):
        review = result["review"]
        st.write(f"모델 의견: {review['recommendation']} · {review['reason']}")
        st.caption("인용의 검색 문서·원문 일치 확인됨 · 의미적 함의는 사람 확인 대상")
        for citation in review["citations"]:
            st.write(citation["source_id"])
            st.text(citation["quote"])
    if result["tool_checks"]:
        with st.expander("실제 모델 도구 호출과 재검사"):
            for index, check in enumerate(result["tool_checks"], 1):
                st.write(f"수정 {index} · check_policy · {check['decision']} · {check['tool_call_id']}")
                st.text(check["copy"]["headline"] + "\n" + check["copy"]["body"])
    summary = operational_summary(row)
    st.caption(f"실제 API 호출 {row['call_count']}회 · 예약 ${row['reserved_usd']:.2f} · 원문 없는 운영 요약")
    st.download_button("LIVE 운영 기록 다운로드", json.dumps(summary, ensure_ascii=False, indent=2),
                       file_name=f"live-{request_id}.json", mime="application/json", key="pilot_export")
    if row.get("review"):
        st.success(f"검토 저장됨 · {row['review']['decision']} · {row['review']['actor_kind']}")
        st.text(row["review"]["note"])
    else:
        with st.form("pilot_review_form"):
            decision = st.selectbox("사람 검토 결과", ["반려", "수정 필요", "승인"])
            note = st.text_area("검토 사유", max_chars=2000)
            submit = st.form_submit_button("검토 결과 저장")
        if submit:
            store.review(token, request_id, {"반려": "reject", "수정 필요": "edit", "승인": "approve"}[decision], note)
            st.rerun()
    if st.button("이 문구를 직접 수정하고 재검사", key="pilot_edit"):
        if not row.get("review"):
            store.review(token, request_id, "edit", "직접 수정 후 재검사 요청")
        st.session_state["pilot_loaded"] = {"headline": ad["headline"], "body": ad["body"], "cta": ad["cta"],
                                              "facts": [f["text"] for f in row["payload"]["product"]["facts"]]}
        st.session_state["pilot_parent"] = request_id
        st.session_state.pop("pilot_selected", None)
        st.rerun()


def render():
    st.header("LIVE 검토")
    st.write("실제 LLM으로 근거를 검토하고, 수정안을 도구로 재검사합니다. 게시 기능은 없습니다.")
    if IN_BROWSER:
        st.info("이 화면은 DEMO 브라우저 실행판입니다. LIVE 실행과 저장은 인증된 서버에서만 가능합니다.")
        st.link_button("인증된 LIVE 검토 열기", URL)
        return
    db = os.environ.get("PREFLIGHT_LIVE_DATABASE_URL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not db or not key:
        st.warning("LIVE 설정 대기 · 실제 모델이나 저장소를 사용할 수 없어 보류합니다. DEMO 결과로 대체하지 않습니다.")
        return
    store = LiveStore(db)
    try:
        if "pilot_token" not in st.session_state:
            with st.form("pilot_login", clear_on_submit=True):
                token = st.text_input("LIVE 검토 접근키", type="password", autocomplete="off")
                submitted = st.form_submit_button("LIVE 검토 접속")
            if submitted:
                store.account(token.strip())
                st.session_state["pilot_token"] = token.strip()
                st.rerun()
            return
        token = st.session_state["pilot_token"]
        actor = store.account(token)
        st.caption(f"LIVE · {MODEL} · 계정 유형 {actor['kind']} · 합성 광고 파일럿")
        st.info("API·자동 브라우저 실행은 사람 사용으로 집계하지 않습니다. 이 파일럿에는 개인정보·실제 광고주 비밀 자료를 입력하지 마세요.")
        if st.button("LIVE 로그아웃"):
            _logout()
            st.rerun()
        rows = store.list_runs(token)
        if rows:
            selected = st.selectbox("내 저장 기록", [r["request_id"] for r in rows], key="pilot_history")
            if st.button("선택한 기록 열기"):
                st.session_state["pilot_selected"] = selected
        if st.session_state.get("pilot_selected"):
            _display(store, token, st.session_state["pilot_selected"])
            if st.button("다른 광고 검토"):
                st.session_state.pop("pilot_selected", None)
                st.session_state.pop("pilot_parent", None)
                st.rerun()
            return
        cases = json.loads((ROOT / "evals/datasets/live_pilot_20261023.json").read_text())["cases"]
        case_id = st.selectbox("합성 입력 예시", [c["id"] for c in cases])
        if st.button("LIVE 예시 불러오기"):
            st.session_state["pilot_loaded"] = next(c for c in cases if c["id"] == case_id)
            st.rerun()
        loaded = st.session_state.pop("pilot_loaded", None)
        if loaded:
            for field in ("headline", "body", "cta"):
                st.session_state["pilot_" + field] = loaded[field]
            st.session_state["pilot_facts"] = "\n".join(loaded["facts"])
            if "path" in loaded: st.session_state["pilot_path"] = loaded["path"]
        with st.form("pilot_input"):
            path = st.radio("실제 LLM 활용 방식", ["rag", "tools"], format_func=lambda x: "RAG 정책 검토" if x == "rag" else "도구 호출 수정·재검사", key="pilot_path")
            headline = st.text_input("LIVE 제목", key="pilot_headline", max_chars=120)
            body = st.text_area("LIVE 본문", key="pilot_body", max_chars=600)
            cta = st.text_input("LIVE 버튼 문구", key="pilot_cta", max_chars=40)
            facts_text = st.text_area("상품 근거 (한 줄에 한 사실)", key="pilot_facts", max_chars=10000)
            submit = st.form_submit_button("실제 LLM 실행", type="primary")
        st.caption("요청당 최대 4회 호출·수정 2회·예약 $0.08, 사용자 누적 $0.50·전체 $1.00. 서버의 영속 장부로 중복·한도를 확인합니다.")
        if submit:
            if not all(x.strip() for x in (headline, body, cta, facts_text)):
                st.error("제목·본문·버튼·상품 근거를 모두 입력하세요.")
                return
            copy = AdCopy(variant_id="live-input", headline=headline.strip(), body=body.strip(), cta=cta.strip())
            product = ProductFacts(name="합성 파일럿", category=ProductCategory.ECOMMERCE,
                                   facts=[Fact(id=f"F{i+1}", text=t.strip()) for i,t in enumerate(facts_text.splitlines()) if t.strip()])
            payload = {"path": path, "copy": copy.model_dump(mode="json"), "product": product.model_dump(mode="json"), "parent": st.session_state.get("pilot_parent")}
            signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ids = st.session_state.setdefault("pilot_requests", {})
            request_id = ids.setdefault(signature, str(uuid4()))
            build = os.environ.get("RENDER_GIT_COMMIT", os.environ.get("PREFLIGHT_BUILD", "local"))
            row, created = store.begin(token, request_id, payload, build)
            if created:
                with st.spinner("실제 모델 검토·재검사 중 · 중복 호출하지 마세요"):
                    gateway = LiveGateway(key, store, token, request_id)
                    result = run_workflow(path, copy, product, gateway)
                    store.finish(token, request_id, result)
            st.session_state["pilot_selected"] = row["request_id"]
            st.rerun()
    except PilotDenied as exc:
        st.error(str(exc))
    except Exception:
        st.error("LIVE 처리를 완료하지 못했습니다. 원문·키를 출력하지 않습니다. 저장 기록을 먼저 확인하세요.")
