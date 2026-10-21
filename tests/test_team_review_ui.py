"""Shared review UI with real command transitions, without a network or paid model.

The fake transport can commit and then lose a response. This tests request replay
at the UI boundary, not just whether a button calls an implementation helper.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from ops import team_review as team
from ops.labeling import Annotation, AnnotationRejected
from ops.review_queue import NotClaimed, ReviewQueue
from ops.team_review_store import StorageBusy, StorageUnavailable

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
SCRIPT = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
from ui import team_review_view
team_review_view.render(st.session_state.get("runs"), store=st.session_state.get("_test_store"))
'''


class MemoryStore:
    def __init__(self):
        self.owner = team.new_access_key()
        self.state = team.new_state(team.key_digest(self.owner), now=NOW)
        self.calls = []
        self.lose_after = None
        self.reject_before = None

    def execute(self, key, action, payload=None, *, request_id):
        payload = deepcopy(payload or {})
        self.calls.append((action, payload, request_id))
        if self.reject_before and self.reject_before[0] == action:
            _, error = self.reject_before
            self.reject_before = None
            raise error
        state, result = team.execute(self.state, key, action, payload, request_id=request_id, now=NOW)
        self.state = state
        if self.lose_after == action:
            self.lose_after = None
            raise StorageUnavailable("postgresql://user:DO_NOT_RENDER@host/database")
        return result

    def call(self, key, action, payload=None):
        return self.execute(key, action, payload, request_id=str(uuid4()))

    def account(self, name, role="reviewer"):
        key = team.new_access_key()
        result = self.call(self.owner, "create_account", {
            "name": name, "role": role, "account_id": "acct_" + uuid4().hex,
            "key_digest": team.key_digest(key),
        })
        return key, result["account_id"]

    def add(self, dataset_id="example", *, synthetic=True, headline="상품 구성 확인"):
        return self.call(self.owner, "enqueue_ad", {
            "dataset_id": dataset_id, "category": "other", "synthetic": synthetic,
            "copy": {"headline": headline, "body": "제품 구성과 사용 방법을 확인하세요.", "cta": "자세히 보기"},
            "facts": [{"id": "f1", "text": "제품 구성표가 제공됩니다."}],
        })["item_id"]

    def queue(self):
        return ReviewQueue.from_jsonl(self.state["queue"], clock=lambda: NOW)


@pytest.fixture
def store():
    return MemoryStore()


def app(store=None, source=None):
    at = AppTest.from_string(SCRIPT, default_timeout=30)
    if store is not None:
        at.session_state["_test_store"] = store
    if source is not None:
        at.session_state["runs"] = source
    at.run()
    ok(at)
    return at


def ok(at):
    assert not at.exception, [str(e.value) for e in at.exception]
    for phrase in ("오탐", "미탐", "맞습니다"):
        assert phrase not in texts(at)


def texts(at):
    return "\n".join(str(e.value) for kind in ("markdown", "caption", "info", "warning", "error", "success", "code") for e in getattr(at, kind))


def login(at, key):
    at.text_input(key="team_login_input").set_value(key)
    at.button(key="team_login_submit").click().run()
    ok(at)
    return at


def claim(at):
    at.button(key="team_claim").click().run()
    ok(at)
    return at


def submit(at, iid, *, decision="APPROVE", reason="광고 내용과 제공된 근거를 확인했습니다.", adjudicate=False):
    prefix = f"team_{'adj' if adjudicate else 'review'}_{iid}"
    at.radio(key=prefix + "_decision").set_value(decision)
    at.text_area(key=prefix + "_rationale").set_value(reason)
    at.button(key=prefix + "_submit").click().run()
    ok(at)
    return at


def test_unconfigured_and_browser_screens_do_not_offer_credentials(monkeypatch):
    from ui import team_review_view as view

    monkeypatch.setattr(view.PostgresTeamStore, "from_env", classmethod(lambda cls: None))
    native = app()
    assert "공동 저장 연결이 아직 설정되지 않았습니다" in texts(native)
    assert not native.text_input
    monkeypatch.setattr(view, "IN_BROWSER", True)
    def must_not_connect(cls):
        raise AssertionError("browser must not access database configuration")
    monkeypatch.setattr(view.PostgresTeamStore, "from_env", classmethod(must_not_connect))
    browser = app()
    assert not browser.text_input
    links = browser.get("link_button")
    assert len(links) == 1 and links[0].proto.url == "https://preflight-toss-ads.onrender.com/?view=운영"


def test_password_login_server_role_and_logout_clear_only_team_session(store):
    key, _ = store.account("검수 A")
    at = app(store)
    field = at.text_input(key="team_login_input").proto
    assert field.type == field.PASSWORD
    login(at, key)
    assert "검수 A" in texts(at) and "검수자" in texts(at)
    assert key not in texts(at)
    assert not any(b.key in {"team_create_submit", "team_export"} for b in at.button)
    at.session_state["unrelated"] = "keep"
    at.button(key="team_logout").click().run()
    ok(at)
    assert "team_access_key" not in at.session_state
    assert "team_account" not in at.session_state
    assert at.session_state["unrelated"] == "keep"


def test_reviewer_never_sees_other_labels_before_or_after_submission(store):
    first, _ = store.account("검수 A")
    second, sid = store.account("검수 B")
    iid = store.add()
    store.call(first, "claim")
    store.call(first, "submit", {"item_id": iid, "decision": "APPROVE", "rationale": "다른 검수자에게 노출되면 안 되는 이유"})
    at = claim(login(app(store), second))
    assert "상품 구성 확인" in texts(at)
    assert "다른 검수자에게 노출되면 안 되는 이유" not in texts(at)
    assert not any(c[0] in {"accounts", "disagreements", "export"} for c in store.calls[5:])
    submit(at, iid)
    assert "다른 검수자에게 노출되면 안 되는 이유" not in texts(at)
    assert "검토를 저장했습니다" in texts(at)
    assert store.queue().get(iid).annotations[-1].annotator == sid
    assert "team_pending" not in at.session_state


@pytest.mark.parametrize("action", ["claim", "submit", "release"])
def test_lost_response_retries_same_request_and_payload_once(store, action):
    key, aid = store.account("검수 A")
    iid = store.add("first")
    store.add("second")
    at = login(app(store), key)
    if action != "claim":
        claim(at)
        iid = at.session_state["team_selected_item"]
    store.lose_after = action
    if action == "claim":
        claim(at)
    elif action == "submit":
        submit(at, iid)
    else:
        at.button(key="team_release").click().run()
        ok(at)
    pending = deepcopy(at.session_state["team_pending"])
    assert pending["action"] == action
    assert "DO_NOT_RENDER" not in texts(at)
    assert "저장됐을 수도" in texts(at)
    assert not any(b.key in {"team_claim", f"team_review_{iid}_submit", "team_release"} for b in at.button)
    revision = store.state["revision"]
    at.button(key="team_retry").click().run()
    ok(at)
    repeated = [c for c in store.calls if c[0] == action][-2:]
    assert repeated[0] == repeated[1]
    assert repeated[1][1:] == (pending["payload"], pending["request_id"])
    assert store.state["revision"] == revision
    assert "team_pending" not in at.session_state
    if action == "claim":
        assert len(store.queue().held_by(aid)) == 1
    elif action == "submit":
        assert len(store.queue().get(iid).annotations) == 1
    else:
        assert store.queue().held_by(aid) == []


def _validation_error():
    try:
        Annotation(item_id="item", annotator="reviewer", decision="APPROVE", rationale="x" * 2001)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected schema rejection")


@pytest.mark.parametrize("error", [
    team.InvalidCommand("요청한 판정 내용을 다시 확인해 주세요."),
    team.PermissionDenied("이 작업을 할 권한이 없습니다."),
    NotClaimed("광고의 배정 상태를 다시 확인해 주세요."),
    AnnotationRejected(["광고 문구의 인용을 다시 확인해 주세요."]),
    _validation_error(),
])
def test_atomic_domain_rejection_clears_pending_and_allows_correction(store, error):
    key, _ = store.account("검수 A")
    iid = store.add()
    at = claim(login(app(store), key))
    store.reject_before = ("submit", error)
    submit(at, iid)
    assert "team_pending" not in at.session_state
    assert at.error
    assert not store.queue().get(iid).annotations
    submit(at, iid, reason="근거를 다시 읽고 판정 내용을 확인했습니다.")
    assert len(store.queue().get(iid).annotations) == 1


@pytest.mark.parametrize("error", [StorageBusy("DO_NOT_RENDER"), TimeoutError("DO_NOT_RENDER")])
def test_unconfirmed_transport_failure_keeps_request_and_logout_is_available(store, error):
    key, _ = store.account("검수 A")
    store.add()
    at = login(app(store), key)
    store.reject_before = ("claim", error)
    claim(at)
    pending = deepcopy(at.session_state["team_pending"])
    assert pending["action"] == "claim" and "DO_NOT_RENDER" not in texts(at)
    at.run()
    ok(at)
    assert at.session_state["team_pending"] == pending
    assert len([c for c in store.calls if c[0] == "claim"]) == 1
    at.button(key="team_logout").click().run()
    ok(at)
    assert "team_pending" not in at.session_state and "team_access_key" not in at.session_state
    assert at.text_input(key="team_login_input")


def test_local_invalid_label_can_be_corrected_without_creating_a_request(store):
    key, _ = store.account("검수 A")
    iid = store.add()
    at = claim(login(app(store), key))
    submit(at, iid, decision="REJECT")
    assert at.error and "team_pending" not in at.session_state
    assert not any(c[0] == "submit" for c in store.calls)
    submit(at, iid, decision="APPROVE")
    assert len(store.queue().get(iid).annotations) == 1


def test_authentication_failure_during_retry_allows_new_key_or_logout(store):
    key, aid = store.account("검수 A")
    store.add()
    at = login(app(store), key)
    store.lose_after = "claim"
    claim(at)
    store.call(store.owner, "set_active", {"account_id": aid, "active": False})
    at.button(key="team_retry").click().run()
    ok(at)
    assert "team_pending" not in at.session_state
    assert "team_access_key" not in at.session_state
    assert at.text_input(key="team_login_input")
    assert "접근키" in texts(at)


def test_owner_creation_lost_response_shows_key_once_without_sending_raw_key(store):
    at = login(app(store), store.owner)
    store.lose_after = "create_account"
    at.text_input(key="team_new_name").set_value("새 담당자")
    at.button(key="team_create_submit").click().run()
    ok(at)
    secret = at.session_state["team_pending_issued"]["key"]
    pending = deepcopy(at.session_state["team_pending"])
    assert secret not in json.dumps(pending)
    assert secret not in texts(at)
    at.button(key="team_retry").click().run()
    ok(at)
    assert secret in texts(at)
    assert "team_pending_issued" not in at.session_state and "team_issued" not in at.session_state
    assert len(store.state["accounts"]) == 2
    assert secret not in json.dumps(store.state)
    assert secret not in json.dumps(store.calls)
    at.run()
    ok(at)
    assert secret not in texts(at)


@pytest.mark.parametrize("lose_response", [False, True])
def test_owner_can_rotate_other_key_and_disable_account_but_not_self(store, lose_response):
    old, aid = store.account("검수 A")
    at = login(app(store), store.owner)
    assert "관리자" not in at.selectbox(key="team_target_account").options
    if lose_response:
        store.lose_after = "replace_key"
    at.button(key="team_replace_key").click().run()
    ok(at)
    if lose_response:
        pending = deepcopy(at.session_state["team_pending"])
        raw = at.session_state["team_pending_issued"]["key"]
        assert raw not in json.dumps(pending) and raw not in texts(at)
        at.button(key="team_retry").click().run()
        ok(at)
        repeated = [c for c in store.calls if c[0] == "replace_key"][-2:]
        assert repeated[0] == repeated[1]
    key = next(c.value for c in at.code if c.value.startswith("pfk_"))
    assert store.call(key, "overview")["account"]["id"] == aid
    with pytest.raises(team.AuthenticationError):
        store.call(old, "overview")
    at.button(key="team_toggle_active").click().run()
    ok(at)
    assert not store.state["accounts"][aid]["active"]
    assert key not in texts(at)


def test_owner_manual_ad_and_existing_run_enqueue_are_server_mutations(store):
    from tests.test_labeling_ui import _run

    at = login(app(store, [_run()]), store.owner)
    at.button(key="team_enqueue_run").click().run()
    ok(at)
    assert len(store.queue()) > 0
    before = len(store.queue())
    at.text_input(key="team_ad_dataset_id").set_value("manual-ui-source")
    at.text_input(key="team_ad_headline").set_value("직접 입력한 상품")
    at.text_area(key="team_ad_body").set_value("상품 구성과 사용 방법을 확인하세요.")
    at.button(key="team_enqueue_ad").click().run()
    ok(at)
    assert len(store.queue()) == before + 1
    assert {c[0] for c in store.calls} >= {"enqueue_run", "enqueue_ad"}
    assert all(it.synthetic for it in store.queue().items())


def test_adjudicator_resolves_actual_disagreement_and_exports_with_identity_caveat(store, monkeypatch):
    from ui import team_review_view as view

    downloads = []
    monkeypatch.setattr(view, "download_or_show", lambda *args, **kwargs: downloads.append(args))
    a, _ = store.account("검수 A")
    b, _ = store.account("검수 B")
    adj, _ = store.account("조정 담당", "adjudicator")
    iid = store.add(synthetic=False, headline="최고의 상품 구성")
    for key, payload in ((a, {"decision": "APPROVE"}), (b, {"decision": "REVISE", "rule_ids": ["TA-SUPERLATIVE-01"], "cited_spans": ["최고의 상품 구성"]})):
        store.call(key, "claim")
        store.call(key, "submit", {"item_id": iid, "rationale": "서로 다른 검수 이유를 확인합니다.", **payload})
    at = login(app(store), adj)
    assert "담당자별 검토" in texts(at)
    submit(at, iid, adjudicate=True, reason="제공된 상품 근거와 문구를 대조하여 통과로 정했습니다.")
    assert store.queue().get(iid).adjudication is not None
    at.button(key="team_export").click().run()
    ok(at)
    assert "실제 참여자와 독립 검수 여부는 별도 확인" in texts(at)
    exported = at.session_state["team_export_result"]
    assert len(exported["rows"]) == 1
    assert exported["identity_verified"] is False and exported["independence_verified"] is False
    assert "pfk_" not in json.dumps(exported)
    assert not any(b.key in {"team_create_submit", "team_claim"} for b in at.button)
    data = downloads[-1][1]
    assert "identity_verified=false; independence_verified=false" in data
    assert "실제 참여자와 독립 검수 여부는 별도 확인" in data
    assert "pfk_" not in data


def test_no_forbidden_claims_or_secret_exception_text_in_review_ui(store):
    key, _ = store.account("검수 A")
    at = app(store)
    store.reject_before = ("overview", RuntimeError("DO_NOT_RENDER credential pfk_hidden"))
    login(at, key)
    assert "DO_NOT_RENDER" not in texts(at)
    for phrase in ("오탐", "미탐", "맞습니다"):
        assert phrase not in texts(at)


def test_ops_hub_adds_shared_review_tab_and_passes_existing_runs(monkeypatch):
    from ui import ops_hub

    received = []
    monkeypatch.setattr(ops_hub.review_queue_view, "render", lambda source: None)
    monkeypatch.setattr(ops_hub.ops_view, "render", lambda tracer, status: None)
    monkeypatch.setattr(ops_hub.team_review_view, "render", lambda source: received.append(source))
    at = AppTest.from_string('''
import streamlit as st
from ui import ops_hub
ops_hub.render(None, {})
''')
    at.session_state["runs"] = ["source marker"]
    at.run()
    ok(at)
    assert [tab.label for tab in at.tabs] == ["검수 대기열", "운영 지표·모니터", "공동 검수"]
    assert received == [["source marker"]]
