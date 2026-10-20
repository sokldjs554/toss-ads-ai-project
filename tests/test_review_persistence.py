"""검수 자동 저장: 복원·확인 응답·오래된 탭·실패 시 원본 보존."""
from __future__ import annotations

import json

import pytest

from ops.review_queue import ReviewQueue
from tests.test_labeling_common import add_item, label, make_queue


def session():
    from ops.review_persistence import QueuePersistence

    return QueuePersistence()


def loaded(store, text=None, revision=0):
    command = store.command()
    return store.accept({"id": command["id"], "status": "loaded", "revision": revision, "jsonl": text})


def test_waits_for_storage_before_allowing_queue_edits():
    store = session()
    assert store.ready is False
    command = store.command()
    assert command["op"] == "load"
    assert store.accept(None) is None
    assert store.command() == command and store.ready is False


def test_new_session_automatically_restores_labels_through_strict_jsonl_loader():
    q, _ = make_queue(required=1)
    item = add_item(q, 1)
    label(q, "검토자", item.id)
    text = q.to_jsonl()
    store = session()
    restored = loaded(store, text, 3)
    assert store.ready and store.revision == 3
    assert restored.to_jsonl() == text
    assert restored.get(item.id).annotations[0].annotator == "검토자"
    assert store.command(restored) is None


def test_only_matching_committed_acknowledgement_marks_a_change_as_saved():
    store = session()
    q = loaded(store)
    add_item(q, 1)
    command = store.command(q)
    assert command["op"] == "save" and command["expected_revision"] == 0
    assert not store.ready
    store.accept({"id": "outdated-response", "status": "saved", "revision": 1})
    assert not store.ready
    store.accept({"id": command["id"], "status": "saved", "revision": 1})
    assert store.ready and store.revision == 1
    assert store.command(q) is None


def test_conflict_keeps_unsaved_work_and_never_retries_over_newer_content():
    store = session()
    q = loaded(store)
    add_item(q, 7)
    command = store.command(q)
    store.accept({"id": command["id"], "status": "conflict", "revision": 2})
    assert store.error == "conflict" and not store.ready
    assert store.unsaved_jsonl == q.to_jsonl()
    assert store.command(q) is None
    with pytest.raises(ValueError, match="충돌"):
        store.retry()
    store.reload()
    other, _ = make_queue()
    add_item(other, 8)
    restored = loaded(store, other.to_jsonl(), 2)
    assert store.ready and restored.to_jsonl() == other.to_jsonl()


@pytest.mark.parametrize("kind", ["quota", "denied", "unavailable", "aborted"])
def test_storage_failure_keeps_pending_work_and_retry_is_idempotent(kind):
    store = session()
    q = loaded(store)
    add_item(q, 2)
    command = store.command(q)
    store.accept({"id": command["id"], "status": "error", "error": kind})
    assert store.error == kind and store.unsaved_jsonl == q.to_jsonl()
    assert not store.ready
    store.retry()
    retry = store.command(q)
    assert retry["id"] != command["id"]
    assert retry["write_id"] == command["write_id"]
    assert retry["expected_revision"] == 0 and retry["jsonl"] == command["jsonl"]
    store.accept({"id": retry["id"], "status": "saved", "revision": 1})
    assert store.ready


@pytest.mark.parametrize("bad", ["broken", '{"format":"preflight.review_queue","version":99}\n',
                                  '{"format":"preflight.review_queue","version":1,"config":[1]}\n'])
def test_corrupt_restore_is_not_replaced_with_an_empty_queue(bad):
    store = session()
    assert loaded(store, bad, 1) is None
    assert store.error == "invalid" and not store.ready
    assert store.command(ReviewQueue()) is None
    assert store.recovery_jsonl == bad


def test_loaded_annotations_are_revalidated_before_reopening():
    q, _ = make_queue(required=1)
    item = add_item(q, 1)
    label(q, "검토자", item.id)
    lines = [json.loads(line) for line in q.to_jsonl().splitlines()]
    lines[1]["data"]["annotations"][0]["cited_spans"] = ["광고에 없는 인용"]
    bad = "\n".join(json.dumps(line) for line in lines)
    store = session()
    assert loaded(store, bad, 1) is None and store.error == "invalid"


@pytest.mark.parametrize("revision", [True, -1, 1.5, "1"])
def test_bad_response_revision_cannot_enable_edits(revision):
    store = session()
    assert loaded(store, ReviewQueue().to_jsonl(), revision) is None
    assert store.error == "invalid" and not store.ready


def test_unexpected_saved_revision_does_not_claim_success():
    store = session()
    q = loaded(store)
    add_item(q, 1)
    command = store.command(q)
    store.accept({"id": command["id"], "status": "saved", "revision": 900})
    assert not store.ready and store.error == "invalid"
