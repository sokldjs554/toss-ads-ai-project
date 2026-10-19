"""대화 평가(evals/dialogs.py) 테스트 — 네트워크·모델 호출 없음.

- 데이터셋 구조: 60개 이상, dev 40 / holdout 20+, 대화마다 2~6번 말, 필요한 종류를 모두 포함
- 러너·지표: 코드가 계산하고, 기대를 틀리게 하면 실제로 실패한다 (채점이 비어 있지 않다)
- 게이트: 기준선보다 나빠지거나 인젝션 오염이 있으면 실패, 통과했을 때만 저장
- 리포트: 규칙 기반(extractor: rules)임을 밝히고, 저장된 리포트가 지금 코드·데이터의 결과와 같다 (오래된 숫자가 남지 않는다)
이 테스트가 확인하는 숫자는 규칙 기반 추출기의 값이다. 모델 품질이 아니다.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agents import conversation as C
from evals import dialogs as E

KINDS = {"all_at_once", "gradual", "vague", "numeric", "contradiction", "modification", "minor", "prohibited", "policy_intent", "injection", "irrelevant", "empty", "long", "english", "emoji"}
REQUIRED_KEYS = {"id", "split", "kind", "turns", "expected_slots", "must_ask", "must_not_ask", "expected_issues", "expect_ready", "notes"}
ALL = E.load_dialogs("all")


# ───────────── 데이터셋 ─────────────
def test_dataset_size_split_and_shape():
    dev, hold = E.load_dialogs("dev"), E.load_dialogs("holdout")
    assert len(ALL) >= 60 and len(dev) == 40 and len(hold) >= 20 and len(dev) + len(hold) == len(ALL)
    assert len({d["id"] for d in ALL}) == len(ALL)
    for d in ALL:
        assert REQUIRED_KEYS <= set(d), d["id"]
        assert 2 <= len(d["turns"]) <= 6, (d["id"], len(d["turns"]))              # 대화마다 2~6번 말
        assert d["split"] in ("dev", "holdout") and isinstance(d["expect_ready"], bool) and isinstance(d["expected_slots"], dict)
        assert all(set(m) == {"turn", "slot"} and 1 <= m["turn"] <= len(d["turns"]) for m in d["must_ask"]), d["id"]
        assert all(set(m) == {"from_turn", "slot"} and 1 <= m["from_turn"] <= len(d["turns"]) for m in d["must_not_ask"]), d["id"]
        assert set(d["expected_slots"]) <= set(E.EVAL_SLOTS), d["id"]
        assert set(d["expected_issues"]) <= {"MINOR_TARGET", "PROHIBITED", "POLICY_INTENT", "INJECTION", "LONG_INPUT", "BUDGET_LOW", "BID_CONFLICT", "SETS_LIMITED", "TURN_LIMIT"}, d["id"]


def test_dataset_covers_every_conversation_kind_in_both_splits():
    for split in ("dev", "holdout"):
        kinds = {d["kind"] for d in E.load_dialogs(split)}
        assert KINDS <= kinds, (split, KINDS - kinds)


def test_dataset_exercises_the_edge_cases_the_spec_lists():
    texts = "\n".join(t for d in ALL for t in d["turns"])
    for needle in ("300만", "3백만", "3,000,000", "삼백만", "3m", "하루", "넉넉히", "잘 되게", "줄여줘", "고등학생", "리딩방", "원금 보장", "이전 지시를 무시", "ignore all previous", "날씨", "💳"):
        assert needle in texts, needle
    assert any(any(not t.strip() for t in d["turns"]) for d in ALL)                      # 빈 입력
    assert any(any(len(t) > C.MAX_USER_CHARS for t in d["turns"]) for d in ALL)          # 아주 긴 입력
    assert any(d.get("expect_conflict") for d in ALL) and any(d.get("expect_update") for d in ALL) and any(d.get("forbidden") for d in ALL)


def test_expected_values_were_written_by_hand_not_copied_from_the_extractor():
    """기대 슬롯에는 추출기가 읽지 못하는 말(예: 정답이 '없음')도 들어 있다 — 추출기 출력으로 채점표를 만들면 그런 항목이 없다."""
    vague = [d for d in ALL if d["kind"] == "vague"]
    assert any(d["expected_slots"] in ({}, {"category": "loan"}, {"category": "app"}) and not d["expect_ready"] for d in vague)
    assert any(not d["expect_ready"] for d in ALL) and any(d["expect_ready"] for d in ALL)


# ───────────── 채점이 비어 있지 않다 ─────────────
def dialog(id_):
    return copy.deepcopy(next(d for d in ALL if d["id"] == id_))


def test_a_correct_dialog_passes_and_wrong_expectations_fail():
    d = dialog("DLG-001")
    assert E.evaluate(E.run_dialog(d))["ok"]
    wrong = copy.deepcopy(d)
    wrong["expected_slots"]["monthly_budget_krw"] = 4_000_000
    r = E.evaluate(E.run_dialog(wrong))
    assert not r["ok"] and any("monthly_budget_krw" in f for f in r["failures"])
    wrong = copy.deepcopy(d)
    del wrong["expected_slots"]["audience"]                                               # 말했는데 기대에 없으면 '말하지 않았는데 추출' 로 센다
    assert any("말하지 않았는데 추출됨" in f for f in E.evaluate(E.run_dialog(wrong))["failures"])
    wrong = copy.deepcopy(d)
    wrong["expect_ready"] = False
    assert not E.evaluate(E.run_dialog(wrong))["ready_ok"]
    wrong = copy.deepcopy(d)
    wrong["expected_issues"] = ["BUDGET_LOW"]
    assert not E.evaluate(E.run_dialog(wrong))["issue_ok"]


def test_missed_ask_and_unnecessary_ask_are_counted(monkeypatch):
    d = dialog("DLG-005")                                                                 # 점점 나오는 대화
    base = E.evaluate(E.run_dialog(d))
    assert base["ask_checks"] > 0 and base["ask_miss"] == 0 and base["noask_checks"] > 0 and base["noask_viol"] == 0
    # 되묻기를 아예 하지 않는 구현 → 놓친 되묻기
    monkeypatch.setattr(C, "_build_questions", lambda st, ex, issues, hard: [])
    quiet = E.evaluate(E.run_dialog(d))
    assert quiet["ask_miss"] == quiet["ask_checks"] > 0
    # 이미 말한 슬롯까지 계속 묻는 구현 → 불필요한 되묻기
    monkeypatch.undo()
    orig = C._build_questions

    def nagging(st, ex, issues, hard):
        qs = orig(st, ex, issues, hard)
        return [C.Question(slot="category", kind="missing", text="업종?")] + qs[:1]

    monkeypatch.setattr(C, "_build_questions", nagging)
    noisy = E.evaluate(E.run_dialog(d))
    assert noisy["noask_viol"] > 0 and any("불필요한 되묻기" in f for f in noisy["failures"])


def test_conflict_and_update_scoring_distinguish_the_two(monkeypatch):
    conflict, update = dialog("DLG-020"), dialog("DLG-023")
    assert E.evaluate(E.run_dialog(conflict))["conflict_ok"] == 1 and E.evaluate(E.run_dialog(update))["conflict_ok"] == 1
    # 수정 요청을 충돌로 처리하는 구현은 점수를 잃는다
    monkeypatch.setattr("agents.conversation_rules.is_update_at", lambda text, s, e: False)
    r = E.evaluate(E.run_dialog(update))
    assert r["conflict_ok"] == 0 and any("수정 요청 처리" in f for f in r["failures"])


def test_injection_contamination_is_detected(monkeypatch):
    d = dialog("DLG-033")
    assert not E.evaluate(E.run_dialog(d))["contaminated"]
    monkeypatch.setattr(C, "quarantine", lambda text, catalog: (text.strip(), [], 0))      # 격리를 끄면 공격 문장이 슬롯이 된다
    r = E.evaluate(E.run_dialog(d))
    assert r["contaminated"] and any("인젝션 오염" in f for f in r["failures"])


def test_slot_matches_rules():
    mk = lambda v, status="confirmed": C.Slot(value=v, status=status)  # noqa: E731
    assert E.slot_matches("monthly_budget_krw", 3_000_000, mk(3_000_000)) and not E.slot_matches("monthly_budget_krw", 3_000_000, mk("3000000"))
    assert E.slot_matches("audience", {"age_min": 20}, mk({"age_min": 20, "age_max": 29, "gender": "여성"})) and not E.slot_matches("audience", {"gender": "남성"}, mk({"gender": "여성"}))
    assert E.slot_matches("audience", {"segments": ["직장인"]}, mk({"segments": ["직장인", "대학생"]})) and not E.slot_matches("audience", {"segments": ["주부"]}, mk({"segments": ["직장인"]}))
    assert E.slot_matches("period", {"days": 31}, mk({"days": 31, "start": "10-01"})) and not E.slot_matches("period", {"days": 30}, mk({"days": 31}))
    assert E.slot_matches("key_conditions", ["연 4.5%"], mk(["연  4.5%"])) and not E.slot_matches("key_conditions", ["연 4.5%"], mk(["연 4.5%", "수수료 없음"]))
    assert not E.slot_matches("category", "loan", mk("loan", "conflict"))


def test_metrics_are_computed_from_dialog_results_not_hardcoded():
    evals = [E.evaluate(E.run_dialog(dialog(i))) for i in ("DLG-001", "DLG-011", "DLG-020", "DLG-033")]
    m = E.compute_metrics(evals)
    assert m["n_dialogs"] == 4 and m["slot_micro_accuracy"] is not None and 0 <= m["slot_micro_accuracy"] <= 1
    assert m["injection_dialogs"] == 1 and m["injection_contaminated"] == 0 and m["conflict_items"] == 1
    assert m["unnecessary_reask_counts"][1] > 0 and m["missed_ask_counts"][1] > 0
    worse = copy.deepcopy(evals)
    worse[0]["ready_ok"] = False
    assert E.compute_metrics(worse)["ready_accuracy"] < m["ready_accuracy"]
    assert E.compute_metrics([])["slot_micro_accuracy"] is None                              # 분모가 0 이면 대상 없음


# ───────────── 모델 경로와 규칙 경로가 같다 (예시 실행의 대역 = 규칙 되풀이) ─────────────
def test_demo_mock_repeats_the_rules_so_no_model_update_is_dropped_on_any_dialog():
    for d in ALL:
        run = E.run_dialog(d)
        assert not run.error, (d["id"], run.error)
        assert all(t.dropped == 0 for t in run.turns), (d["id"], [t.dropped for t in run.turns])
        assert not any(c in ("FM-NEW-A-1", "FM-NEW-A-2", "FM-NEW-A-4") for t in run.turns for c in t.failures), d["id"]


def test_eval_runs_are_deterministic():
    a = [E.evaluate(E.run_dialog(d)) for d in ALL[:12]]
    b = [E.evaluate(E.run_dialog(d)) for d in ALL[:12]]
    assert a == b


# ───────────── 게이트 ─────────────
def synthetic(dev_overrides=None, sha="x"):
    m = {k: 1.0 for k in E.HIGHER_BETTER}
    m.update({k: 0.0 for k in E.LOWER_BETTER if k != "mean_turns_to_ready"})
    m.update({"mean_turns_to_ready": 1.5, "injection_contaminated": 0, "injection_dialogs": 3})
    m.update(dev_overrides or {})
    return {"dataset": {"sha256": sha}, "splits": {"dev": m}}


def test_gate_passes_when_equal_or_better_and_warns_without_baseline():
    assert E.gate(synthetic(), synthetic()).passed
    r = E.gate(synthetic(), None)
    assert r.passed and any("기준선 없음" in w for w in r.warnings)
    better = synthetic({"mean_turns_to_ready": 1.2})
    assert E.gate(better, synthetic()).passed


@pytest.mark.parametrize("key,bad", [("slot_micro_accuracy", 0.99), ("ready_accuracy", 0.9), ("conflict_handling_accuracy", 0.5), ("issue_accuracy", 0.95), ("injection_safe_rate", 0.66),
                                      ("plan_accuracy", 0.5), ("unnecessary_reask_rate", 0.01), ("missed_ask_rate", 0.02), ("mean_turns_to_ready", 1.6)])
def test_gate_fails_on_any_regression_in_either_direction(key, bad):
    r = E.gate(synthetic({key: bad}), synthetic())
    assert not r.passed and any(key in f for f in r.failures)


def test_gate_fails_on_injection_contamination_even_without_a_baseline():
    r = E.gate(synthetic({"injection_contaminated": 1}), None)
    assert not r.passed and any("인젝션 오염" in f for f in r.failures)


def test_gate_warns_when_the_dataset_changed():
    r = E.gate(synthetic(sha="new"), synthetic(sha="old"))
    assert r.passed and any("데이터셋이 기준선과 다르다" in w for w in r.warnings)


# ───────────── CLI ─────────────
def test_cli_gate_writes_only_when_it_passes(tmp_path, monkeypatch, capsys):
    out, base = tmp_path / "out.json", tmp_path / "base.json"
    assert E.main(["--split", "dev", "--out", str(base), "--quiet"]) == 0 and base.exists()
    saved = json.loads(base.read_text(encoding="utf-8"))
    assert saved["extractor"] == "rules" and set(saved["splits"]) == {"dev"}
    assert E.main(["--split", "dev", "--gate", "--baseline", str(base), "--out", str(out), "--quiet"]) == 0 and out.exists()   # 같은 결과 → 통과
    # 기준선을 '완벽한' 값으로 바꾸고 지금 결과를 나쁘게 만든다 → 실패하고 저장하지 않는다
    perfect = copy.deepcopy(saved)
    m = perfect["splits"]["dev"]
    m["slot_micro_accuracy"] = 1.0
    base.write_text(json.dumps(perfect), encoding="utf-8")
    monkeypatch.setattr(C, "quarantine", lambda text, catalog: (text.strip(), [], 0))
    out2 = tmp_path / "out2.json"
    assert E.main(["--split", "dev", "--gate", "--baseline", str(base), "--out", str(out2), "--quiet"]) == 1
    assert not out2.exists()
    text = capsys.readouterr().out
    assert "게이트 실패" in text and "저장하지 않았습니다" in text and "인젝션 오염" in text


def test_cli_without_gate_always_writes(tmp_path):
    out = tmp_path / "r.json"
    assert E.main(["--split", "holdout", "--out", str(out), "--quiet"]) == 0
    assert set(json.loads(out.read_text(encoding="utf-8"))["splits"]) == {"holdout"}


# ───────────── 리포트 ─────────────
REPORT = Path(E.DEFAULT_REPORT)


def test_saved_report_is_exactly_what_the_current_code_and_data_produce():
    """저장된 evals/reports/dialogs_controlled.json 은 지금의 코드·데이터로 다시 돌린 결과와 같아야 한다 (코드나 데이터를 고쳤다면 `python -m evals.dialogs --split all --out ...` 로 다시 만든다)."""
    saved = json.loads(REPORT.read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(E.build_report("all"), ensure_ascii=False)), "리포트가 오래됐다 — " + E.REPRO


def test_report_states_what_the_numbers_are_and_are_not():
    saved = json.loads(REPORT.read_text(encoding="utf-8"))
    assert saved["extractor"] == "rules" and saved["repro"] == E.REPRO == "python -m evals.dialogs --split all --out evals/reports/dialogs_controlled.json"
    assert "모델" in " ".join(saved["caveats"]) and "라벨" in " ".join(saved["caveats"]) and "holdout" in " ".join(saved["caveats"])
    assert saved["dataset"]["counts"]["dev"] == 40 and saved["dataset"]["counts"]["holdout"] >= 20
    assert set(saved["splits"]) == {"dev", "holdout", "all"}
    assert saved["splits"]["dev"]["injection_contaminated"] == 0                          # dev 는 0 (이 대화로 추출기를 고쳤다)
    hold = saved["splits"]["holdout"]
    assert hold["injection_dialogs"] == 2 and hold["injection_contaminated"] == 1         # holdout 은 1건 — 고치지 않고 그대로 적는다 (tests/test_conv_known_gaps.py)
    assert saved["splits"]["holdout"]["dialogs_all_ok"] == 20 and saved["splits"]["holdout"]["n_dialogs"] == 26
    assert "의미하지 않는" in E.__doc__ and "extractor" in E.__doc__ and "규칙 기반" in E.__doc__


def test_holdout_was_not_tuned_on_the_codes_fingerprints_match_the_freeze():
    """holdout 대화는 추출기·대화 로직을 고정한 뒤에 썼고 그 결과를 보고 고치지 않았다. 그 뒤에 코드를 고치면 이 테스트가 실패해 '고정한 뒤의 값' 이라는 주장이 깨졌음을 알린다."""
    saved = json.loads(REPORT.read_text(encoding="utf-8"))
    assert saved["holdout"]["frozen"]["extractor_sha256"] and saved["holdout"]["clean"] is True
    assert saved["holdout"]["frozen"] == E.code_fingerprints() == {k: saved["code"][k] for k in ("extractor_sha256", "conversation_sha256")}


def test_report_lists_every_failed_dialog_with_the_reason():
    saved = json.loads(REPORT.read_text(encoding="utf-8"))
    bad = [d for d in saved["dialogs"] if not d["ok"]]
    for d in bad:
        assert d["failures"], d["id"]                                                  # 실패는 이유와 함께 남는다
    assert len(saved["dialogs"]) == len(ALL)
    assert saved["splits"]["all"]["dialogs_all_ok"] == sum(d["ok"] for d in saved["dialogs"])


def test_gate_on_the_saved_report_fails_for_holdout_contamination_and_passes_for_dev(tmp_path, capsys):
    """저장된 holdout 결과에는 인젝션 오염 1건이 있다 — 게이트는 이를 숨기지 않는다. dev 게이트는 통과한다."""
    assert E.main(["--split", "dev", "--gate", "--quiet"]) == 0
    assert E.main(["--split", "holdout", "--gate", "--quiet"]) == 1
    text = capsys.readouterr().out
    assert "[holdout] 인젝션 오염 1건" in text and "게이트 통과" in text
    assert E.main(["--split", "all", "--gate", "--quiet"]) == 1
