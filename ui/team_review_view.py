"""Native shared review UI; identity, authorization and receipts live on the server.

Credentials and in-flight commands stay in this Streamlit session. A transport
failure keeps that command's UUID and payload for replay. Explicit
atomic rejections release the form for correction. No queue snapshot is accepted
from the client, and reviewer screens use only the server's blinded view.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from core.config import IN_BROWSER
from core.schemas import AdCopy, Fact, ProductCategory
from guardrails.policy import get_catalog
from ops import team_review as team
from ops.dataset_tools import rows_to_jsonl
from ops.labeling import (
    AdjudicationRecord, Annotation, AnnotationRejected, LabelDecision,
    MAX_RATIONALE_LEN, ensure_adjudication, ensure_annotation, validate_annotator_name,
)
from ops.review_queue import CATEGORY_NAME_KO, QueueError
from ops.team_review_store import PostgresTeamStore
from ui.components import download_or_show, render_ad
from ui.review_queue_view import _entries, decision_name, md_escape, rule_titles, span_candidates, validation_messages

NATIVE_URL = "https://preflight-toss-ads.onrender.com/?view=운영"
ROLE_NAMES = {"owner": "관리자", "reviewer": "검수자", "adjudicator": "조정자"}
DECISIONS = [d.value for d in LabelDecision]
IDENTITY_NOTE = "서로 다른 계정의 기록입니다. 실제 참여자와 독립 검수 여부는 별도 확인이 필요합니다."
UNCERTAIN = "공동 저장 응답을 확인하지 못했습니다. 저장됐을 수도 있으므로 같은 요청으로 다시 확인해 주세요."
ATOMIC_ERRORS = (team.TeamReviewError, ValidationError, QueueError, AnnotationRejected)
ACTION_NAMES = {
    "claim": "광고 배정", "submit": "검토 저장", "release": "배정 반납",
    "adjudicate": "조정 저장", "create_account": "담당자 계정 발급",
    "replace_key": "접근키 재발급", "set_active": "계정 상태 변경",
    "enqueue_run": "광고 작업 결과 추가", "enqueue_ad": "직접 입력한 광고 추가",
}


def _messages(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return validation_messages(error)
    if isinstance(error, AnnotationRejected):
        return error.issues
    return [str(error)]


def _flash(kind: str, message: str) -> None:
    st.session_state["team_flash"] = (kind, message)


def _clear_pending() -> None:
    for key in ("team_pending", "team_pending_issued", "team_pending_error"):
        st.session_state.pop(key, None)


def _forget_auth() -> None:
    _logout()


def _logout() -> None:
    for key in list(st.session_state):
        if key.startswith("team_"):
            del st.session_state[key]


def _read(store, action: str, payload: dict | None = None) -> dict | None:
    try:
        return store.execute(st.session_state["team_access_key"], action, payload or {}, request_id=str(uuid4()))
    except team.AuthenticationError:
        _forget_auth()
        _flash("error", "이 접근키로 계속할 수 없습니다. 관리자에게 키를 확인한 뒤 다시 접속해 주세요.")
        st.rerun()
    except ATOMIC_ERRORS as exc:
        st.error(md_escape(" / ".join(_messages(exc))))
    except Exception:  # Transport/driver errors can contain a database URL or credential.
        st.error("공동 저장 정보를 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.")
    return None


def _success(action: str, result: dict) -> None:
    issued = st.session_state.pop("team_pending_issued", None)
    _clear_pending()
    st.session_state.pop("team_export_result", None)
    if issued:
        st.session_state["team_issued"] = issued
    message = "작업을 저장했습니다."
    if action == "claim":
        item = result.get("item")
        if item:
            st.session_state["team_selected_item"] = item["item_id"]
        message = "광고를 배정했습니다." if item else "지금 배정할 광고가 없습니다."
    elif action in {"submit", "release"}:
        st.session_state.pop("team_selected_item", None)
        message = "검토를 저장했습니다." if action == "submit" else "광고 배정을 반납했습니다."
        # The command result contains only this account's validation warnings.
        warnings = result.get("warnings", [])
        if warnings:
            message += " " + " / ".join(str(w) for w in warnings)
    elif action == "adjudicate":
        st.session_state.pop("team_selected_disagreement", None)
        message = "조정 결과를 저장했습니다."
    elif action == "create_account":
        message = "담당자 계정을 발급했습니다."
    elif action == "replace_key":
        message = "새 접근키를 발급했습니다. 기존 키는 사용할 수 없습니다."
    elif action == "set_active":
        message = "담당자 계정을 활성화했습니다." if result.get("active") else "담당자 계정을 비활성화했습니다."
    elif action == "enqueue_ad":
        message = "광고를 공동 검수 대기열에 담았습니다."
    elif action == "enqueue_run":
        message = (f"광고 {len(result.get('added', []))}건을 공동 검수 대기열에 담았습니다. "
                   f"이미 있는 광고 {len(result.get('duplicates', []))}건, 담당자 확인 대상이 아닌 광고 {result.get('not_eligible', 0)}건은 추가하지 않았습니다.")
    _flash("success", message)


def _attempt_pending(store) -> None:
    pending = st.session_state["team_pending"]
    try:
        result = store.execute(
            st.session_state["team_access_key"], pending["action"], deepcopy(pending["payload"]),
            request_id=pending["request_id"],
        )
    except team.AuthenticationError:
        # A revoked key must not be allowed to replay a saved receipt. A newly
        # issued key can recover the same account's held work through overview.
        _forget_auth()
        _flash("error", "이 접근키로 요청을 확인할 수 없습니다. 새 키로 다시 접속하면 본인에게 배정된 광고를 확인할 수 있습니다.")
    except ATOMIC_ERRORS as exc:
        # These are known atomic rejections, unlike a lost commit response.
        _clear_pending()
        _flash("error", " / ".join(_messages(exc)))
    except Exception:
        # StorageUnavailable, StorageBusy and unknown transport failures all
        # preserve the exact original request. Never include raw driver errors.
        st.session_state["team_pending_error"] = UNCERTAIN
    else:
        _success(pending["action"], result)


def _mutate(store, action: str, payload: dict, *, issued: dict | None = None) -> None:
    if "team_pending" in st.session_state:
        return
    st.session_state["team_pending"] = {
        "action": action, "payload": deepcopy(payload), "request_id": str(uuid4()),
    }
    if issued:
        # Raw keys are never part of a command payload, trace, export or receipt.
        st.session_state["team_pending_issued"] = issued
    _attempt_pending(store)
    st.rerun()


def _pending_panel(store) -> None:
    pending = st.session_state["team_pending"]
    st.warning(st.session_state.get("team_pending_error", UNCERTAIN))
    st.caption(f"확인할 작업: {ACTION_NAMES[pending['action']]}. 확인이 끝나면 다음 작업을 할 수 있습니다.")
    if st.button("같은 요청으로 저장 결과 확인", key="team_retry", type="primary"):
        _attempt_pending(store)
        st.rerun()
    st.caption("로그아웃하면 이 세션의 미확인 요청과 입력도 지워집니다. 저장된 작업은 로그인 후 다시 확인할 수 있습니다.")


def _login(store) -> None:
    st.info("관리자가 발급한 접근키로 접속하세요. 접근키는 현재 세션에만 보관하며 로그아웃하면 지웁니다.")
    with st.form("team_login", clear_on_submit=True):
        key = st.text_input("접근키", type="password", key="team_login_input")
        submitted = st.form_submit_button("공동 검수 접속", key="team_login_submit", type="primary")
    if not submitted:
        return
    try:
        result = store.execute(key.strip(), "overview", {}, request_id=str(uuid4()))
    except team.AuthenticationError:
        st.error("접근키가 올바르지 않거나 만료·비활성화됐습니다. 관리자에게 확인해 주세요.")
    except ATOMIC_ERRORS:
        st.error("접속 정보를 확인할 수 없습니다. 관리자에게 문의해 주세요.")
    except Exception:
        st.error("공동 저장에 연결하지 못했습니다. 잠시 후 다시 접속해 주세요.")
    else:
        st.session_state["team_access_key"] = key.strip()
        st.session_state["team_account"] = result["account"]
        st.rerun()


def _show_notices() -> None:
    notice = st.session_state.pop("team_flash", None)
    if notice:
        kind, message = notice
        getattr(st, kind)(md_escape(message))
    issued = st.session_state.pop("team_issued", None)
    if issued:
        st.warning(f"{md_escape(issued['name'])} 님의 새 접근키입니다. 이 화면에서 한 번만 표시합니다. 지금 복사해 담당자에게 전달하세요.")
        st.code(issued["key"], language=None)


def _ad_details(item: dict) -> AdCopy:
    copy = AdCopy.model_validate(item["ad_copy"])
    name = item.get("product_name", "")
    category = CATEGORY_NAME_KO.get(item.get("category", ""), "")
    if name or category:
        st.caption(md_escape(" · ".join(x for x in (name, category) if x)))
    render_ad(copy, catalog=get_catalog())
    facts = item.get("facts", [])
    if facts:
        st.markdown("**제공된 상품 근거**")
        for fact in facts:
            st.markdown("- " + md_escape(fact["text"]))
    else:
        st.caption("제공된 상품 근거가 없습니다.")
    if item.get("landing_text"):
        with st.expander("랜딩 페이지 내용"):
            st.text(item["landing_text"])
    return copy


def _label_form(store, account: dict, item: dict, copy: AdCopy, *, adjudicate: bool = False) -> None:
    iid = item["item_id"]
    prefix = f"team_{'adj' if adjudicate else 'review'}_{iid}"
    catalog = get_catalog()
    with st.form(prefix + "_form"):
        decision = st.radio("최종 판정" if adjudicate else "판정", DECISIONS, index=None,
                            format_func=decision_name, key=prefix + "_decision", horizontal=True)
        rules = st.multiselect("판정에 적용한 기준", catalog.rule_ids(),
                               format_func=lambda r: catalog.get(r).title, key=prefix + "_rules")
        spans = st.multiselect("광고에서 근거 구절 선택", span_candidates(copy), key=prefix + "_spans")
        extra = st.text_input("근거 구절 직접 입력 (선택)", key=prefix + "_span_free", max_chars=200)
        reason = st.text_area("조정 이유 (10자 이상)" if adjudicate else "검토 이유", key=prefix + "_rationale", max_chars=MAX_RATIONALE_LEN)
        st.caption("수정 필요·게재 불가에는 적용 기준과 근거가 필요합니다. 필수 내용 누락은 빠진 내용을 이유에 적어 주세요.")
        submitted = st.form_submit_button("조정 결과 저장" if adjudicate else "검토 저장", key=prefix + "_submit", type="primary")
    if not submitted:
        return
    if decision is None:
        st.error("판정을 선택해 주세요.")
        return
    cited = [*spans, *([extra.strip()] if extra.strip() else [])]
    payload = {"item_id": iid, "decision": decision, "rule_ids": rules, "cited_spans": cited, "rationale": reason}
    try:
        if adjudicate:
            record = AdjudicationRecord(item_id=iid, adjudicator=account["id"], decision=decision,
                                        rule_ids=rules, cited_spans=cited, reason=reason)
            ensure_adjudication(record, copy)
        else:
            record = Annotation(**payload, annotator=account["id"], prelabel_mode="hidden")
            ensure_annotation(record, copy)
    except (ValidationError, AnnotationRejected) as exc:
        for message in _messages(exc):
            st.error(md_escape(message))
        return
    # Neither role nor identity is submitted; the server binds both to the key.
    _mutate(store, "adjudicate" if adjudicate else "submit", payload)


def _reviewer(store, overview: dict) -> None:
    st.markdown("### 내 검수")
    st.caption("배정받은 광고의 문구와 근거를 읽고 판정을 남겨 주세요. 작성자는 현재 로그인한 계정으로 기록됩니다.")
    held = overview.get("held_items", [])
    if not held:
        st.info("현재 배정된 광고가 없습니다.")
        if st.button("다음 광고 배정받기", key="team_claim", type="primary"):
            _mutate(store, "claim", {})
        return
    if st.session_state.get("team_selected_item") not in held:
        st.session_state["team_selected_item"] = held[0]
    iid = st.selectbox("본인에게 배정된 광고", held, format_func=lambda value: f"배정 광고 {held.index(value) + 1}", key="team_selected_item")
    result = _read(store, "view", {"item_id": iid})
    if result is None:
        return
    item = result["item"]
    copy = _ad_details(item)
    _label_form(store, overview["account"], item, copy)
    if st.button("배정 반납", key="team_release"):
        _mutate(store, "release", {"item_id": iid})


def _accounts(store) -> None:
    result = _read(store, "accounts")
    if result is None:
        return
    accounts = result["accounts"]
    st.markdown("### 담당자 계정")
    st.dataframe([{"이름": a["name"], "역할": ROLE_NAMES[a["role"]], "상태": "활성" if a["active"] else "비활성"} for a in accounts], hide_index=True, use_container_width=True)
    with st.form("team_create_account"):
        name = st.text_input("새 담당자 이름", key="team_new_name", max_chars=60)
        role = st.selectbox("담당 역할", ["reviewer", "adjudicator"], format_func=ROLE_NAMES.get, key="team_new_role")
        submitted = st.form_submit_button("담당자 계정·접근키 발급", key="team_create_submit")
    if submitted:
        try:
            name = validate_annotator_name(name)
            if any(a["name"].casefold() == name.casefold() for a in accounts):
                raise ValueError("이미 있는 담당자 이름입니다. 아래에서 기존 계정의 키를 재발급할 수 있습니다.")
        except ValueError as exc:
            st.error(md_escape(str(exc)))
        else:
            key = team.new_access_key()
            _mutate(store, "create_account", {"account_id": "acct_" + uuid4().hex, "name": name,
                    "role": role, "key_digest": team.key_digest(key)}, issued={"key": key, "name": name})
    targets = {a["id"]: a for a in accounts if a["role"] != "owner"}
    if not targets:
        st.caption("검수자 또는 조정자를 발급하면 키 재발급과 계정 비활성화를 할 수 있습니다.")
        return
    st.markdown("**기존 담당자 관리**")
    target = st.selectbox("관리할 담당자", list(targets), format_func=lambda aid: targets[aid]["name"], key="team_target_account")
    account = targets[target]
    st.caption("키 재발급은 같은 계정과 기존 검토 기록을 유지합니다. 담당자의 활성·비활성 상태는 별도로 관리합니다.")
    if st.button("선택한 담당자의 접근키 재발급", key="team_replace_key"):
        key = team.new_access_key()
        _mutate(store, "replace_key", {"account_id": target, "key_digest": team.key_digest(key)}, issued={"key": key, "name": account["name"]})
    if st.button("계정 비활성화" if account["active"] else "계정 다시 활성화", key="team_toggle_active"):
        _mutate(store, "set_active", {"account_id": target, "active": not account["active"]})


def _enqueue(store, source: Any) -> None:
    st.markdown("### 공동 검수에 광고 추가")
    st.caption("각 광고는 서로 다른 검수 계정 두 곳의 검토를 받습니다. 판정이 갈리면 조정자가 확인합니다.")
    with st.expander("이 세션의 광고 작업 결과에서 추가"):
        entries = _entries(source)
        if entries:
            index = st.selectbox("광고 작업", list(range(len(entries))),
                                 format_func=lambda i: f"{i + 1}. {entries[i][0].brief.product.name}", key="team_run_selection")
            synthetic = st.checkbox("합성 예시로 표시", value=True, key="team_run_synthetic")
            st.caption("직접 수집한 실제 광고인 경우에만 합성 예시 표시를 해제하세요. 담당자 확인 대상만 추가합니다.")
            if st.button("선택한 작업의 광고 추가", key="team_enqueue_run"):
                run, landing = entries[index]
                _mutate(store, "enqueue_run", {"run": run.model_dump(mode="json"), "landing_text": landing, "synthetic": synthetic})
        else:
            st.info("아직 이 세션의 광고 작업 결과가 없습니다. 광고 작업을 실행하거나 아래에서 직접 입력하세요.")
    with st.expander("광고 직접 입력"):
        with st.form("team_manual_ad"):
            dataset_id = st.text_input("출처 ID (광고마다 다른 값)", key="team_ad_dataset_id", max_chars=100)
            product = st.text_input("상품 이름", key="team_ad_product", max_chars=200)
            category = st.selectbox("업종", [c.value for c in ProductCategory], index=list(ProductCategory).index(ProductCategory.OTHER),
                                    format_func=lambda c: CATEGORY_NAME_KO[c], key="team_ad_category")
            headline = st.text_input("광고 제목", key="team_ad_headline", max_chars=1000)
            body = st.text_area("광고 본문", key="team_ad_body", max_chars=10000)
            cta = st.text_input("버튼 문구", value="자세히 보기", key="team_ad_cta", max_chars=200)
            disclosures = st.text_area("안내·고지 문구 (한 줄에 하나)", key="team_ad_disclosures", max_chars=10000)
            facts = st.text_area("제공할 상품 근거 (한 줄에 하나)", key="team_ad_facts", max_chars=10000)
            landing = st.text_area("랜딩 페이지 내용 (선택)", key="team_ad_landing", max_chars=30000)
            synthetic = st.checkbox("합성 예시로 표시", value=True, key="team_ad_synthetic")
            st.caption("직접 수집한 실제 광고인 경우에만 합성 예시 표시를 해제하세요.")
            submitted = st.form_submit_button("입력한 광고 추가", key="team_enqueue_ad")
        if submitted:
            if not dataset_id.strip() or not (headline.strip() or body.strip()):
                st.error("출처 ID와 광고 제목 또는 본문을 입력해 주세요.")
                return
            copy = AdCopy(headline=headline, body=body, cta=cta, disclosures=[x.strip() for x in disclosures.splitlines() if x.strip()])
            evidence = [Fact(id=f"f{i + 1}", text=text).model_dump(mode="json") for i, text in enumerate(x.strip() for x in facts.splitlines() if x.strip())]
            _mutate(store, "enqueue_ad", {"dataset_id": dataset_id.strip(), "product_name": product, "category": category,
                    "copy": copy.model_dump(mode="json"), "facts": evidence, "landing_text": landing, "synthetic": synthetic})


def _export(store) -> None:
    st.markdown("### 검수 결과 내보내기")
    st.caption("검수 계정 두 곳의 판정이 일치했거나 조정이 완료된 광고를 평가셋 후보로 내보냅니다.")
    include_synthetic = st.checkbox("합성 예시도 포함", value=False, key="team_include_synthetic")
    if st.button("내보낼 결과 확인", key="team_export"):
        result = _read(store, "export", {"include_synthetic": include_synthetic})
        if result is not None:
            st.session_state["team_export_result"] = result
    result = st.session_state.get("team_export_result")
    if not result or result.get("synthetic_included") != include_synthetic:
        return
    st.info(IDENTITY_NOTE)
    rows = result["rows"]
    if not rows:
        st.caption("선택한 범위에서 내보낼 완료 결과가 없습니다.")
        return
    st.caption(f"평가셋 후보 {len(rows)}건 · 합성 예시 {'포함' if include_synthetic else '제외'}")
    header = ("공동 검수 평가셋 후보\n" + IDENTITY_NOTE
              + f"\nidentity_verified=false; independence_verified=false; synthetic_included={str(include_synthetic).lower()}")
    download_or_show("검수 결과 JSONL 내려받기", rows_to_jsonl(rows, header=header),
                     "team_review_candidates_with_synthetic.jsonl" if include_synthetic else "team_review_candidates.jsonl",
                     "application/x-ndjson", "team_download")


def _adjudicator(store, overview: dict) -> None:
    st.markdown("### 판정이 갈린 광고 조정")
    result = _read(store, "disagreements")
    if result is None:
        return
    items = {it["item_id"]: it for it in result["items"]}
    if items:
        if st.session_state.get("team_selected_disagreement") not in items:
            st.session_state["team_selected_disagreement"] = next(iter(items))
        iid = st.selectbox("조정할 광고", list(items), format_func=lambda value: items[value]["ad_copy"]["headline"], key="team_selected_disagreement")
        item = items[iid]
        copy = _ad_details(item)
        st.markdown("**담당자별 검토**")
        catalog = get_catalog()
        for i, annotation in enumerate(item["annotations"], 1):
            st.markdown(f"**검토 {i} · {decision_name(annotation['decision'])}**")
            st.caption(md_escape("적용 기준: " + rule_titles(catalog, annotation.get("rule_ids", []))))
            for quote in annotation.get("cited_spans", []):
                st.markdown("> " + md_escape(quote))
            st.markdown(md_escape(annotation.get("rationale", "") or "별도 이유 없음"))
        _label_form(store, overview["account"], item, copy, adjudicate=True)
    else:
        st.info("현재 조정할 광고가 없습니다.")
    _export(store)


def render(source: Any = None, *, store=None) -> None:
    st.subheader("공동 검수")
    st.caption("개별 접근키로 접속한 담당자가 같은 대기열에서 검토합니다. " + IDENTITY_NOTE)
    if IN_BROWSER:
        st.info("공동 검수는 서버가 연결된 운영 화면에서 사용할 수 있습니다.")
        st.link_button("서버의 운영 화면 열기", NATIVE_URL)
        return
    if store is None:
        try:
            store = PostgresTeamStore.from_env()
        except Exception:
            st.error("공동 저장 연결 설정을 확인하지 못했습니다. 관리자에게 문의해 주세요.")
            return
    if store is None:
        st.info("공동 저장 연결이 아직 설정되지 않았습니다. 관리자가 서버 연결과 최초 계정을 준비하면 접속할 수 있습니다.")
        return
    st.caption("공동 저장 연결 설정 있음 · 접속과 저장은 서버에서 확인합니다.")
    _show_notices()
    if not st.session_state.get("team_access_key"):
        _login(store)
        return
    st.button("로그아웃", key="team_logout", on_click=_logout)
    if "team_pending" in st.session_state:
        _pending_panel(store)
        return
    overview = _read(store, "overview")
    if overview is None:
        if st.button("연결 다시 확인", key="team_refresh"):
            st.rerun()
        return
    account = overview["account"]
    st.session_state["team_account"] = account
    st.caption(md_escape(f"{account['name']} · {ROLE_NAMES[account['role']]}"))
    if account["role"] == "reviewer":
        _reviewer(store, overview)
    elif account["role"] == "adjudicator":
        _adjudicator(store, overview)
    elif account["role"] == "owner":
        stats = overview["stats"]
        st.caption(f"전체 {stats['total']}건 · 검수 대기 {stats['waiting']}건 · 조정 대기 {stats['awaiting_adjudication']}건")
        _accounts(store)
        _enqueue(store, source)
        _export(store)
