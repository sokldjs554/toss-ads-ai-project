"""감사 기록: 해시 체인의 변조 탐지, 마스킹 규칙 고정, 크기·개수 정책, 광고 작업 어댑터.

탐지하지 못하는 것(끝 잘림·전체 재계산)도 '못 한다' 는 사실 그대로 테스트로 고정한다 — 한계를 문서에만 두지 않는다.
"""
from __future__ import annotations

import dataclasses
import json
import threading
from datetime import datetime, timezone
from enum import Enum

import pytest

from core.config import Settings
from core.schemas import AdCopy, Decision, PipelineRun
from ops import audit
from ops.audit import (
    AuditEntry,
    AuditFormatError,
    AuditFull,
    AuditLog,
    AuditTampered,
    EntryTooLarge,
    GENESIS_HASH,
    audit_edit,
    audit_from_run,
    audit_human_review,
    audit_label,
    audit_recheck,
    redact,
    redact_text,
    redact_with_report,
)

FIXED = datetime(2026, 10, 19, 9, 0, 0, tzinfo=timezone.utc)


def _log(n: int = 8, **kw) -> AuditLog:
    log = AuditLog(clock=lambda: FIXED, **kw)
    for i in range(n):
        log.append("ad.judged", f"user{i % 3}", {"variant_id": f"var_{i}", "final_decision": "APPROVE", "n": i})
    return log


def _lines(log: AuditLog) -> list[str]:
    return log.export_jsonl().splitlines()


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


# --- 해시 체인: 변조 탐지 ----------------------------------------------------------------
def test_chain_verifies_and_links_each_entry_to_the_previous_hash():
    log = _log(6)
    res = log.verify()
    assert res.ok and res.first_bad_index is None and res.checked == 6 and res.head_hash == log.head_hash
    es = log.entries
    assert es[0].prev_hash == GENESIS_HASH
    for a, b in zip(es, es[1:]):
        assert b.prev_hash == a.hash and b.index == a.index + 1
    assert len({e.hash for e in es}) == 6


def test_hash_is_sha256_of_canonical_json_so_it_can_be_reproduced_outside_this_code():
    import hashlib

    e = _log(1).entries[0]
    body = {"v": 1, "alg": "sha256", "index": 0, "ts": e.ts, "type": e.event_type, "actor": e.actor, "payload": e.payload, "redacted": e.redacted, "prev": GENESIS_HASH}
    canon = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert e.hash == hashlib.sha256(canon).hexdigest()


def test_modifying_a_middle_entry_is_detected_at_that_entry():
    log = _log(8)
    lines = _lines(log)
    d = json.loads(lines[4])
    d["payload"]["n"] = 999                      # 내용만 고치고 해시는 그대로
    lines[4] = json.dumps(d, ensure_ascii=False, sort_keys=True)
    res = AuditLog.verify_jsonl(_text(lines))
    assert not res.ok and res.first_bad_index == 4 and res.code == "hash"


def test_modifying_in_memory_entries_is_detected_too():
    log = _log(5)
    log._entries[2] = dataclasses.replace(log._entries[2], actor="attacker")
    res = log.verify()
    assert not res.ok and res.first_bad_index == 2


def test_recomputing_only_the_modified_entrys_hash_breaks_the_next_link():
    log = _log(6)
    e = log._entries[3]
    forged_body = dataclasses.replace(e, payload={**e.payload, "n": 777})
    forged = dataclasses.replace(forged_body, hash=audit._digest(forged_body.body(), None))     # 공격자가 이 항목의 해시만 다시 계산
    log._entries[3] = forged
    res = log.verify()
    assert not res.ok and res.first_bad_index == 4 and res.code == "chain"   # 다음 항목의 prev 와 안 맞는다


def test_deleting_a_middle_entry_is_detected():
    lines = _lines(_log(8))
    del lines[3]
    res = AuditLog.verify_jsonl(_text(lines))
    assert not res.ok and res.first_bad_index == 3 and res.code == "index"


def test_reordering_two_entries_is_detected():
    lines = _lines(_log(8))
    lines[2], lines[3] = lines[3], lines[2]
    res = AuditLog.verify_jsonl(_text(lines))
    assert not res.ok and res.first_bad_index == 2


def test_deleting_the_first_entry_is_detected_because_the_chain_must_start_at_genesis():
    lines = _lines(_log(5))
    res = AuditLog.verify_jsonl(_text(lines[1:]))
    assert not res.ok and res.first_bad_index == 0


def test_inserting_a_copied_entry_is_detected():
    lines = _lines(_log(5))
    lines.insert(3, lines[1])
    assert not AuditLog.verify_jsonl(_text(lines)).ok


def test_truncating_the_tail_is_NOT_detected_without_a_separately_kept_hash__documented_limit():
    log = _log(8)
    lines = _lines(log)
    assert AuditLog.verify_jsonl(_text(lines[:5])).ok       # 한계: 끝을 잘라 낸 기록은 그 자체로 멀쩡한 체인이다
    assert "따로 보관" in AuditLog.verify_jsonl(_text(lines[:5])).reason   # 그리고 화면·결과가 그 사실을 알려 준다


def test_truncating_the_tail_is_detected_when_the_last_hash_or_count_was_kept_separately():
    log = _log(8)
    kept_hash, kept_n = log.head_hash, len(log)
    cut = _text(_lines(log)[:5])
    r1 = AuditLog.verify_jsonl(cut, expected_last_hash=kept_hash)
    assert not r1.ok and r1.code == "truncated" and r1.first_bad_index == 5
    r2 = AuditLog.verify_jsonl(cut, expected_count=kept_n)
    assert not r2.ok and r2.code == "truncated"
    assert AuditLog.verify_jsonl(_text(_lines(log)), expected_last_hash=kept_hash, expected_count=kept_n).ok
    assert log.verify(expected_last_hash=kept_hash, expected_count=kept_n).ok


def test_rewriting_the_whole_chain_is_NOT_detected_without_a_key__but_a_keyed_chain_resists_it():
    original = _log(5)
    forged = AuditLog(clock=lambda: FIXED)                      # 공격자가 내용을 바꿔 처음부터 다시 쓴 체인
    for e in original.entries:
        forged.append(e.event_type, e.actor, {**e.payload, "final_decision": "REJECT"}, ts=e.ts)
    assert forged.verify().ok and AuditLog.verify_jsonl(forged.export_jsonl()).ok          # 한계: 키 없는 체인은 통째로 다시 계산하면 속는다
    assert not AuditLog.verify_jsonl(forged.export_jsonl(), expected_last_hash=original.head_hash).ok   # 마지막 해시를 따로 보관했다면 잡힌다
    secret = b"0123456789abcdef-test-key"
    keyed = AuditLog(key=secret, clock=lambda: FIXED)
    for e in original.entries:
        keyed.append(e.event_type, e.actor, e.payload, ts=e.ts)
    assert AuditLog.verify_jsonl(keyed.export_jsonl(), key=secret).ok
    assert not AuditLog.verify_jsonl(forged.export_jsonl(), key=secret).ok                 # 키 없이 만든 체인은 키 검증을 통과하지 못한다
    assert AuditLog.verify_jsonl(keyed.export_jsonl()).code == "key"                       # 키 없이는 검증을 요구한다
    assert not AuditLog.verify_jsonl(keyed.export_jsonl(), key=b"wrong-key-wrong-key-xx").ok


def test_unknown_fields_and_malformed_lines_are_rejected_without_exceptions():
    lines = _lines(_log(3))
    d = json.loads(lines[1])
    d["note"] = "숨은 필드 — 해시가 지켜 주지 않는다"
    bad = _text([lines[0], json.dumps(d), lines[2]])
    res = AuditLog.verify_jsonl(bad)
    assert not res.ok and res.code == "format" and res.first_bad_index == 1
    for garbage in ("not json at all", "[1,2,3]", '{"index": "x"}', "{", "\x00\x01", '{"v":1}', "null"):
        r = AuditLog.verify_jsonl(garbage)
        assert not r.ok and r.code == "format"
    assert AuditLog.verify_jsonl(None).ok is False                      # type: ignore[arg-type]
    with pytest.raises(AuditFormatError):
        AuditLog.import_jsonl("not json")


def test_oversized_upload_is_refused_not_parsed(monkeypatch):
    monkeypatch.setattr(audit, "MAX_VERIFY_BYTES", 100)
    r = AuditLog.verify_jsonl(_log(3).export_jsonl())
    assert not r.ok and "큽니다" in r.reason


def test_unicode_line_separators_in_content_do_not_break_export_roundtrip():
    log = AuditLog(clock=lambda: FIXED)
    log.append("ad.judged", "u", {"note": "줄 나눔 문자 \u2028 \u2029 \x85 \x0b \x0c \x1c 와 \r\n 포함", "n": 1})
    log.append("ad.judged", "u", {"n": 2})
    text = log.export_jsonl()
    again = AuditLog.import_jsonl(text)
    assert len(again) == 2 and again.head_hash == log.head_hash and again.verify().ok
    assert AuditLog.verify_jsonl(text.replace("\n", "\r\n")).ok                          # 윈도 줄바꿈으로 저장돼도 검증된다


def test_append_rejects_non_string_timestamp():
    with pytest.raises(AuditFormatError):
        AuditLog().append("run.executed", "u", {}, ts=123)                                # type: ignore[arg-type]


def test_import_roundtrip_and_tampered_import_raises():
    log = _log(5)
    again = AuditLog.import_jsonl(log.export_jsonl())
    assert again.head_hash == log.head_hash and len(again) == 5 and again.verify().ok
    lines = _lines(log)
    d = json.loads(lines[2])
    d["actor"] = "evil"
    lines[2] = json.dumps(d)
    with pytest.raises(AuditTampered) as ei:
        AuditLog.import_jsonl(_text(lines))
    assert ei.value.result.first_bad_index == 2
    assert len(AuditLog.import_jsonl(_text(lines), verify=False)) == 5            # 검증을 끄면 읽기만 한다


def test_same_inputs_and_clock_give_the_same_head_hash():
    assert _log(6).head_hash == _log(6).head_hash
    assert _log(6).head_hash != _log(7).head_hash


def test_append_validates_event_type_and_actor():
    log = AuditLog()
    for bad in ("", "Run.Executed", "x" * 80, "has space", 5):
        with pytest.raises(AuditFormatError):
            log.append(bad, "u")                                             # type: ignore[arg-type]
    with pytest.raises(AuditFormatError):
        log.append("run.executed", "  ")
    assert len(log) == 0


def test_concurrent_appends_keep_the_chain_valid():
    log = AuditLog()

    def work(k: int) -> None:
        for i in range(40):
            log.append("ad.judged", f"t{k}", {"i": i})

    ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(log) == 320 and log.verify().ok


# --- 마스킹 -----------------------------------------------------------------------------
@pytest.mark.parametrize("raw,kind", [
    ("키는 sk-ant-api03-AbCdEf0123456789_xyz-AbCdEf0123 입니다", "api_key"),
    ("OPENAI sk-proj-AbCdEf0123456789AbCdEf0123456789", "api_key"),
    ("sk-AbCdEf0123456789AbCdEf0123", "api_key"), ("짧은 키 sk-ant-AbCd1234 도", "api_key"),
    ("Authorization: Bearer abcDEF123456.ghi789-jkl", "api_key"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "api_key"),
    ("AKIAABCDEFGHIJKLMNOP", "api_key"),
    ("긴 토큰 a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8 끝", "token"),
    ("문의: hong.gildong+ads@example.co.kr 로", "email"),
    ("전화 010-1234-5678 입니다", "phone"), ("01012345678", "phone"), ("010 1234 5678", "phone"), ("+82 10-1234-5678", "phone"), ("+821012345678", "phone"),
    ("02-123-4567", "phone"), ("031-1234-5678", "phone"), ("070-8888-9999", "phone"), ("대표번호 1588-0000", "phone"),
    ("주민번호 900101-1234567", "rrn"), ("9001011234567", "rrn"), ("외국인 000229-5123456", "rrn"),
    ("카드 4111 1111 1111 1111", "card"), ("4111-1111-1111-1111", "card"), ("5500000000000004", "card"),
])
def test_masking_rules_are_pinned(raw, kind):
    counts: dict[str, int] = {}
    out = redact_text(raw, counts)
    assert f"[REDACTED:{kind}]" in out, (raw, out)
    assert counts.get(kind, 0) >= 1
    for secret in ("sk-ant-api03", "sk-proj-", "hong.gildong", "1234-5678", "900101", "4111", "ghp_", "AKIA", "a1B2c3D4"):
        assert secret not in out or secret not in raw, (raw, out)


@pytest.mark.parametrize("raw", [
    "원금 보장이라고 쓰지 마세요", "연 4.5%~15.0% (심사 결과에 따라 다름)", "최대 한도 5,000만원", "주문번호 12345678", "2026-10-19 09:00:00",
    "variant_id var_ab12cd34ef", "test_ops_audit_chain_detects_modification_of_entries", "sk-short", "010-12", "4111 1111 1111 1112",       # 마지막은 루훈 검사 실패
    "a" * 31, "https://example.com/path/to/page", "문의 gmail 주소는 비공개",
])
def test_non_secrets_are_left_alone(raw):
    assert redact_text(raw) == raw


def test_hash_columns_are_kept_but_secret_named_columns_are_always_masked():
    h = "a1" * 32
    out = redact({"ad_sha256": h, "content_hash": h, "fingerprint": h, "note": h, "api_key": "plain-secret", "password": "pw", "auth": "x", "input_tokens": 1234, "max_tokens": "2048"})
    assert out["ad_sha256"] == h and out["content_hash"] == h and out["fingerprint"] == h
    assert out["note"] == "[REDACTED:token]"                           # 해시 칸이 아니면 긴 토큰은 가린다
    assert out["api_key"] == out["password"] == out["auth"] == "[REDACTED:secret]"
    assert out["input_tokens"] == 1234 and out["max_tokens"] == "2048"         # 이름이 token 으로 '끝나지' 않는 칸은 건드리지 않는다


def test_masking_is_idempotent_deep_and_does_not_mutate_the_input():
    payload = {"a": ["mail x@y.com", {"b": "call 010-1111-2222"}], "x@y.com": "키가 이메일", "n": 3}
    snapshot = json.dumps(payload, sort_keys=True)
    out, counts = redact_with_report(payload)
    assert json.dumps(payload, sort_keys=True) == snapshot
    assert "x@y.com" not in json.dumps(out) and "010-1111-2222" not in json.dumps(out)
    assert counts == {"email": 2, "phone": 1}
    assert redact(out) == out


def test_secrets_never_reach_the_stored_entry_the_export_or_the_hash_input():
    secret = "sk-ant-api03-THIS-MUST-NEVER-BE-STORED-0123456789"
    log = AuditLog(clock=lambda: FIXED)
    e = log.append("run.executed", "ops@example.com", {"msg": f"실패: {secret}", "contact": "010-9999-8888", "rrn": "900101-1234567"})
    dump = log.export_jsonl()
    for s in (secret, "ops@example.com", "010-9999-8888", "900101-1234567"):
        assert s not in dump and s not in json.dumps(e.to_dict(), ensure_ascii=False)
    assert e.redacted == 4 and log.verify().ok


def test_non_json_values_are_normalized_without_leaking_bytes():
    class Color(Enum):
        RED = "red"

    out = redact({"when": FIXED, "color": Color.RED, "raw": b"\x00secret-bytes", "ids": {"b", "a"}, "nan": float("nan"), "inf": float("inf"), "deep": (1, (2, 3))})
    assert out["when"].startswith("2026-10-19") and out["color"] == "red" and out["ids"] == ["a", "b"]
    assert out["raw"].startswith("[bytes:") and "secret" not in out["raw"]
    assert out["nan"] == "nan" and out["inf"] == "inf" and out["deep"] == [1, [2, 3]]
    json.dumps(out, allow_nan=False)
    nested: dict = {}
    cur = nested
    for _ in range(30):
        cur["x"] = {}
        cur = cur["x"]
    with pytest.raises(AuditFormatError):
        redact(nested)


# --- 크기·개수 정책: 가득 차면 명시적 오류 ---------------------------------------------------
def test_full_log_raises_instead_of_evicting_old_entries():
    log = AuditLog(max_entries=5, clock=lambda: FIXED)
    for i in range(5):
        log.append("ad.judged", "u", {"i": i})
    first_hash = log.entries[0].hash
    with pytest.raises(AuditFull):
        log.append("ad.judged", "u", {"i": 5})
    assert len(log) == 5 and log.entries[0].hash == first_hash and log.verify().ok     # 아무것도 밀려나거나 바뀌지 않았다


def test_oversized_entry_is_rejected_not_truncated_and_leaves_the_log_untouched():
    log = AuditLog(max_entry_bytes=512, clock=lambda: FIXED)
    log.append("ad.judged", "u", {"ok": True})
    head = log.head_hash
    with pytest.raises(EntryTooLarge):
        log.append("ad.judged", "u", {"blob": "x " * 2000})
    assert len(log) == 1 and log.head_hash == head


def test_continuing_a_full_log_keeps_the_chain_and_needs_the_anchor_to_verify():
    old = AuditLog(max_entries=3, clock=lambda: FIXED)
    for i in range(3):
        old.append("ad.judged", "u", {"i": i})
    new = AuditLog.continue_from(old, max_entries=10)
    new.append("ad.judged", "u", {"i": 3})
    new.append("ad.judged", "u", {"i": 4})
    assert new.entries[0].prev_hash == old.head_hash and new.entries[0].index == 3 and new.verify().ok
    text = new.export_jsonl()
    assert not AuditLog.verify_jsonl(text).ok                                           # 앵커 없이는 처음부터가 아니라 검증 실패
    assert AuditLog.verify_jsonl(text, anchor=(3, old.head_hash)).ok
    assert not AuditLog.verify_jsonl(text, anchor=(3, "f" * 64)).ok
    assert len(AuditLog.import_jsonl(text, anchor=(3, old.head_hash))) == 2


def test_invalid_limits_are_refused():
    with pytest.raises(ValueError):
        AuditLog(max_entries=0)
    with pytest.raises(ValueError):
        AuditLog(max_entry_bytes=10)


# --- 어댑터 -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def demo_run() -> PipelineRun:
    from agents.pipeline import run_pipeline
    from observability.tracer import Tracer
    from ui.presets import PRESETS, build_brief

    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    return run_pipeline(build_brief(PRESETS["commerce_landing"]), s, tracer=Tracer())


def test_audit_from_run_records_run_and_each_judgement_without_raw_ad_text(demo_run):
    log = audit_from_run(demo_run, actor="tester")
    types = [e.event_type for e in log.entries]
    assert types[0] == "run.executed" and types.count("ad.judged") == len(demo_run.reports)
    assert log.entries[0].payload["run_id"] == demo_run.run_id and log.entries[0].payload["demo_mode"] is True
    dump = log.export_jsonl()
    for rep in demo_run.reports:
        assert rep.ad_copy.headline not in dump and rep.ad_copy.body not in dump         # 원문은 남기지 않는다 — 해시·길이만
        assert audit.text_digest(rep.ad_copy.full_text()) in dump
    assert log.verify().ok and all(e.actor == "tester" for e in log.entries)


def test_audit_from_run_records_human_review_requests(demo_run):
    run = demo_run.model_copy(deep=True)
    run.reports[0].final_decision = Decision.HUMAN_REVIEW
    run.reports[0].decision_reason = "연락처 010-5555-6666 로 문의"
    log = audit_from_run(run)
    hr = [e for e in log.entries if e.event_type == "human.review_requested"]
    assert len(hr) == 1 and hr[0].payload["variant_id"] == run.reports[0].variant_id
    assert "010-5555-6666" not in log.export_jsonl() and hr[0].redacted >= 1


def test_audit_from_run_appends_to_an_existing_log_and_keeps_it_verifiable(demo_run):
    log = AuditLog()
    audit_from_run(demo_run, log)
    n = len(log)
    audit_from_run(demo_run, log)
    assert len(log) == 2 * n and log.verify().ok


def test_other_event_helpers_record_hashes_not_text():
    log = AuditLog(clock=lambda: FIXED)
    before = AdCopy(variant_id="v1", headline="원금 보장", body="누구나 승인", cta="신청")
    after = AdCopy(variant_id="v1", headline="조건 확인 후 신청", body="심사 결과에 따라 다름", cta="신청")
    e1 = audit_edit(log, variant_id="v1", actor="a@b.com", before=before, after=after)
    assert e1.payload["changed"] is True and e1.payload["before_sha256"] != e1.payload["after_sha256"] and e1.actor == "[REDACTED:email]"
    assert "원금 보장" not in log.export_jsonl()
    e2 = audit_human_review(log, variant_id="v1", reviewer="reviewer-7", decision="REVISE", note="연락 010-2222-3333", run_id="run_x")
    e3 = audit_label(log, case_id="G-001", labeler="labeler-2", label="REJECT", rule_ids=["B", "A", "A"], note="메모")
    assert e2.event_type == "human.review_decided" and "010-2222-3333" not in log.export_jsonl()
    assert e3.event_type == "label.submitted" and e3.payload["rule_ids"] == ["A", "B"]
    assert [e.event_type for e in log.entries] == ["ad.edited", "human.review_decided", "label.submitted"] and log.verify().ok


def test_recheck_helper_records_the_report(demo_run):
    log = AuditLog()
    e = audit_recheck(log, variant_id=demo_run.reports[0].variant_id, actor="u", report=demo_run.reports[0], run_id=demo_run.run_id)
    assert e.event_type == "ad.rechecked" and e.payload["final_decision"] == demo_run.reports[0].final_decision.value


def test_event_types_constant_covers_the_required_events():
    for needed in ("run.executed", "ad.judged", "ad.edited", "ad.rechecked", "human.review_requested", "human.review_decided", "label.submitted"):
        assert needed in audit.EVENT_TYPES


def test_entry_from_dict_rejects_wrong_types():
    good = _log(1).entries[0].to_dict()
    assert AuditEntry.from_dict(good).hash == good["hash"]
    for key, bad in (("index", -1), ("index", True), ("index", "0"), ("payload", []), ("ts", 5), ("alg", "md5"), ("v", 2), ("redacted", -3)):
        with pytest.raises(AuditFormatError):
            AuditEntry.from_dict({**good, key: bad})
    with pytest.raises(AuditFormatError):
        AuditEntry.from_dict({k: v for k, v in good.items() if k != "prev"})
