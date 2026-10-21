"""UI가 자동 저장 완료 전에 성공을 알리거나 다음 변경을 허용하지 않는지 검증한다."""
from __future__ import annotations

import pytest

from tests.review_storage_harness import BrowserStorageHarness
from tests.test_labeling_ui import _run, app, claim_as, enqueue, ok, queue_of, submit, texts


@pytest.fixture
def storage(monkeypatch):
    from ui import review_queue_storage

    browser = BrowserStorageHarness()
    monkeypatch.setattr(review_queue_storage, "_component", browser)
    return browser


def test_queue_and_label_restore_automatically_in_a_fresh_session(storage):
    first = app([_run()])
    enqueue(first)
    claim_as(first, "새로고침 검토자")
    submit(first, "점검 통과")
    original = queue_of(first).to_jsonl()
    second = app()
    ok(second)
    assert queue_of(second).to_jsonl() == original
    assert len(queue_of(second).annotations()) == 1
    assert "이 브라우저에 저장됨" in texts(second)


def test_restore_before_editing_is_mandatory(monkeypatch):
    from ui import review_queue_storage

    monkeypatch.setattr(review_queue_storage, "_component", lambda **kw: None)
    at = app([_run()])
    ok(at)
    assert "저장한 대기열을 불러오는 중" in texts(at)
    assert not any(b.key in {"rq_enqueue", "rq_claim"} for b in at.button)


def test_failed_save_keeps_queue_and_success_message_until_confirmed(storage):
    at = app([_run()])
    storage.failure = "quota"
    enqueue(at)
    assert "저장 공간" in texts(at)
    assert storage.jsonl is None and len(queue_of(at)) > 0
    assert not any("담았습니다" in s.value for s in at.success)
    assert not any(b.key == "rq_claim" for b in at.button)
    storage.failure = None
    at.button(key="rq_storage_retry").click().run()
    ok(at)
    assert storage.jsonl == queue_of(at).to_jsonl()
    assert "이 브라우저에 저장됨" in texts(at)
    assert any("담았습니다" in s.value for s in at.success)


def test_old_tab_conflict_preserves_latest_save_and_offers_explicit_reload(storage):
    first, old = app([_run()]), app([_run()])
    enqueue(first)
    latest = storage.jsonl
    enqueue(old)
    assert "다른 탭에서 먼저 저장" in texts(old)
    assert storage.jsonl == latest
    assert not any("담았습니다" in s.value for s in old.success)
    assert not any(b.key == "rq_claim" for b in old.button)
    old.button(key="rq_storage_reload").click().run()
    ok(old)
    assert queue_of(old).to_jsonl() == latest
    assert "담았습니다" not in texts(old)


def test_corrupt_storage_cannot_be_silently_replaced(storage):
    storage.jsonl, storage.revision = "corrupt backup", 2
    at = app([_run()])
    ok(at)
    assert "저장본을 읽지 못했습니다" in texts(at)
    assert not any(b.key == "rq_enqueue" for b in at.button)
    assert storage.jsonl == "corrupt backup"


def test_required_label_setting_is_saved_without_an_extra_manual_save(storage):
    at = app()
    at.radio(key="rq_required").set_value(1).run()
    assert storage.jsonl is not None
    restored = app()
    assert queue_of(restored).required_labels == 1
    assert restored.radio(key="rq_required").value == 1


def test_existing_session_queue_is_migrated_when_no_browser_snapshot_exists(storage):
    from streamlit.testing.v1 import AppTest
    from tests.test_labeling_common import add_item, make_queue
    from tests.test_labeling_ui import SCRIPT

    queue, _ = make_queue()
    add_item(queue, 99)
    at = AppTest.from_string(SCRIPT, default_timeout=90)
    at.session_state["rq_queue"] = queue
    at.run()
    ok(at)
    assert len(queue_of(at)) == 1
    assert storage.jsonl == queue_of(at).to_jsonl()


def test_valid_import_with_more_labelers_does_not_break_restore(storage):
    from ops.review_queue import ReviewQueue

    storage.jsonl, storage.revision = ReviewQueue(required_labels=3).to_jsonl(), 1
    at = app()
    ok(at)
    assert queue_of(at).required_labels == 3 and at.radio(key="rq_required").value == 3
