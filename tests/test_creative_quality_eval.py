"""소재 퀄리티 신호 평가(evals/quality_signals.py)와 쌍대 선호 데이터 테스트 — 네트워크·모델 없음.

- 데이터: 80쌍 이상, dev 56 · holdout 24 이상, 이유 코드(8개 + 함정 4개 + 둘 다 좋음)가 두 split 에 모두 있고 정답이 a/b 에 고르게 나뉨
- 절차 기록: 신호 코드·가중치·무승부 기준·dev 쌍이 동결 시점과 같고 holdout 쌍이 처음 돌린 뒤 바뀌지 않았다 — 하나라도 바뀌면 이 테스트가 실패한다
- 지표 계산(무승부 처리·우연 수준·신호 하나만·ablation·게이트)을 작은 인공 데이터로 직접 검산
- 리포트에 '독립 라벨러 없음'·'클릭률 상관 미측정'이 들어가고, 저장된 리포트가 지금 코드로 다시 만든 것과 같다
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from core import creative_quality as cq
from core.schemas import ProductCategory
from evals import quality_signals as q

ROOT = Path(__file__).resolve().parents[1]
BASE_REASONS = {"specificity", "conditions", "cta", "readability", "redundancy", "hype", "tone", "length"}
TRAPS = {"trap_long", "trap_numbers", "trap_short_vague", "trap_hype_specific"}


@pytest.fixture(scope="module")
def pairs():
    return q.load_pairs()


@pytest.fixture(scope="module")
def report():
    return q.build_report()


# ───────────────────────── 데이터 ─────────────────────────
def test_dataset_size_and_split(pairs):
    dev, hold = [p for p in pairs if p.split == "dev"], [p for p in pairs if p.split == "holdout"]
    assert len(pairs) >= 80 and len(dev) >= 56 and len(hold) >= 24
    assert len({p.id for p in pairs}) == len(pairs)
    assert all(p.id.startswith("QP-D") for p in dev) and all(p.id.startswith("QP-H") for p in hold)


def test_every_reason_code_is_in_both_splits_and_traps_are_included(pairs):
    for split in ("dev", "holdout"):
        codes = {p.reason_code for p in pairs if p.split == split}
        assert BASE_REASONS | TRAPS | {"tie_both_good"} == codes, (split, codes ^ (BASE_REASONS | TRAPS | {"tie_both_good"}))
    dev_n = {c: sum(p.reason_code == c and p.split == "dev" for p in pairs) for c in BASE_REASONS | TRAPS}
    assert min(dev_n.values()) >= 4
    assert sum(p.reason_code == "tie_both_good" for p in pairs) == sum(p.better == "tie" for p in pairs)      # tie 라벨은 '둘 다 좋음' 쌍에만


def test_answers_are_balanced_between_a_and_b_so_position_is_not_a_shortcut(pairs):
    for split in ("dev", "holdout"):
        dec = [p for p in pairs if p.split == split and p.better != "tie"]
        share_a = sum(p.better == "a" for p in dec) / len(dec)
        assert 0.4 <= share_a <= 0.6, (split, share_a)


def test_pairs_are_well_formed_and_holdout_text_is_not_reused_from_dev(pairs):
    cats = {c.value for c in ProductCategory}
    triples = {"dev": set(), "holdout": set()}
    for p in pairs:
        assert p.facts and all(f.id and f.text for f in p.facts) and p.categories <= cats and p.better in q.LABELS
        for side in "ab":
            d = p.a if side == "a" else p.b
            assert d["headline"].strip() and d["body"].strip() and d["cta"].strip()
            triples[p.split].add((d["headline"], d["body"], d["cta"]))
    assert not (triples["dev"] & triples["holdout"])
    heads_dev = {t[0] for t in triples["dev"]}
    assert not ({t[0] for t in triples["holdout"]} & heads_dev)


def test_dataset_header_says_who_labeled_and_that_it_is_synthetic():
    head = "\n".join(l for l in (ROOT / "evals" / "datasets" / "quality_pairs.jsonl").read_text(encoding="utf-8").splitlines() if l.startswith("#"))
    for needle in ("작성자(AI)", "독립 라벨러", "가상", "절차(엄수)", "동결", "holdout"):
        assert needle in head, needle


def test_load_rejects_malformed_rows(tmp_path):
    good = json.loads(next(l for l in (ROOT / "evals" / "datasets" / "quality_pairs.jsonl").read_text(encoding="utf-8").splitlines() if l.startswith("{")))

    def write(row):
        p = tmp_path / "x.jsonl"
        p.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    assert len(q.load_pairs(write(good))) == 1
    for patch in ({"better": "c"}, {"split": "test"}, {"a": {"headline": "x"}}, {"facts": []}):
        with pytest.raises(ValueError):
            q.load_pairs(write({**good, **patch}))
    dup = tmp_path / "dup.jsonl"
    dup.write_text(json.dumps(good, ensure_ascii=False) + "\n" + json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        q.load_pairs(dup)
    with pytest.raises(ValueError):
        q.load_pairs(split="train")


# ───────────────────────── 절차 기록 (holdout 미조정) ─────────────────────────
def test_procedure_records_are_intact():
    chk = q.procedure_checks()
    assert chk["signal_code_unchanged_since_freeze"], "동결 뒤 core/creative_quality.py 가 바뀌었다 — 바꿨다면 PROCEDURE['amendments'] 에 이유를 적고 해시를 갱신해야 한다"
    assert chk["weights_unchanged_since_freeze"] and chk["tie_margin_unchanged_since_freeze"]
    assert chk["dev_pairs_unchanged_since_freeze"], "동결 시점의 dev 쌍과 달라졌다"
    assert chk["holdout_pairs_unchanged_since_first_run"], "holdout 쌍이 처음 돌린 뒤 바뀌었다"
    assert chk["amendments"] == len(q.PROCEDURE["amendments"]) == 0


def test_procedure_steps_put_holdout_after_the_freeze():
    steps = q.PROCEDURE["steps"]
    order = [next(i for i, s in enumerate(steps) if key in s) for key in ("dev 56쌍을 먼저", "동결했다", "동결한 뒤에 holdout", "holdout 결과를 보고")]
    assert order == sorted(order) and len(set(order)) == 4
    assert q.PROCEDURE["frozen"]["n_dev_pairs"] == sum(p.split == "dev" for p in q.load_pairs())
    assert re.fullmatch(r"[0-9a-f]{64}", q.PROCEDURE["frozen"]["core_creative_quality_sha256"]) and re.fullmatch(r"[0-9a-f]{64}", q.PROCEDURE["holdout_pairs_sha256"])


def test_first_holdout_run_is_what_the_current_code_gives_on_the_unchanged_holdout(pairs):
    """처음 돌린 숫자를 기록해 두었고, 코드와 쌍이 그대로이므로 지금 돌려도 같은 숫자여야 한다(나중에 몰래 고쳐 숫자를 바꾸지 못한다)."""
    first = q.PROCEDURE["first_holdout_run"]
    now = q.evaluate(q.score_pairs([p for p in pairs if p.split == "holdout"]))
    assert (now["decisive_agreement"]["k"], now["decisive_agreement"]["n"]) == (first["decisive_agreement"]["k"], first["decisive_agreement"]["n"])
    assert (now["three_way_accuracy"]["k"], now["three_way_accuracy"]["n"]) == (first["three_way_accuracy"]["k"], first["three_way_accuracy"]["n"])
    wrong = [f["id"] for f in q.failures([s for s in q.score_pairs(pairs) if s.pair.split == "holdout"])]
    assert wrong == first["wrong_pairs"]


def test_procedure_check_notices_every_kind_of_change(tmp_path, monkeypatch):
    """변이 확인: 신호 가중치·무승부 기준·dev 쌍·holdout 쌍 중 무엇을 바꿔도 점검이 잡는다."""
    monkeypatch.setattr(cq, "WEIGHTS", {**cq.WEIGHTS, "tone": 0.2, "length": 0.0})
    assert not q.procedure_checks()["weights_unchanged_since_freeze"]
    monkeypatch.undo()
    monkeypatch.setattr(cq, "TIE_MARGIN", 0.07)
    assert not q.procedure_checks()["tie_margin_unchanged_since_freeze"]
    monkeypatch.undo()
    text = (ROOT / "evals" / "datasets" / "quality_pairs.jsonl").read_text(encoding="utf-8")
    dev_changed = tmp_path / "dev.jsonl"
    dev_changed.write_text(text.replace('"better": "a"', '"better": "b"', 1), encoding="utf-8")                  # 첫 a 라벨(dev 쌍)을 뒤집는다
    chk = q.procedure_checks(dev_changed)
    assert not chk["dev_pairs_unchanged_since_freeze"] and chk["holdout_pairs_unchanged_since_first_run"]
    hold_changed = tmp_path / "hold.jsonl"
    lines = text.splitlines()
    i = next(i for i, l in enumerate(lines) if '"split": "holdout"' in l)
    lines[i] = lines[i].replace("계좌", "통장", 1) if "계좌" in lines[i] else lines[i].replace(" ", "  ", 1)
    hold_changed.write_text("\n".join(lines) + "\n", encoding="utf-8")
    chk = q.procedure_checks(hold_changed)
    assert not chk["holdout_pairs_unchanged_since_first_run"] and chk["dev_pairs_unchanged_since_freeze"]
    code_copy = tmp_path / "creative_quality.py"
    code_copy.write_text(Path(cq.__file__).read_text(encoding="utf-8") + "\n# 동결 뒤에 몰래 고침\n", encoding="utf-8")
    monkeypatch.setattr(cq, "__file__", str(code_copy))
    assert not q.procedure_checks()["signal_code_unchanged_since_freeze"]


# ───────────────────────── 지표 검산 (인공 데이터) ─────────────────────────
GOOD = {"headline": "전 상품 10% 할인", "body": "5만원 이상 구매 시 10월 31일까지 쓸 수 있어요.", "cta": "쿠폰 받기"}
BAD = {"headline": "특별한 혜택을 만나보세요", "body": "다양한 즐거움이 가득해요.", "cta": "자세히 보기"}
FACTS = [{"id": "F1", "text": "전 상품 10% 할인, 50,000원 이상 구매 시"}, {"id": "F2", "text": "10월 31일까지 진행"}]


def make(tmp_path, specs):
    rows = []
    for i, (a, b, better, reason) in enumerate(specs):
        rows.append({"id": f"QP-D{i + 1:02d}", "split": "dev", "facts": FACTS, "categories": ["ecommerce"], "a": a, "b": b, "better": better, "reason_code": reason, "notes": ""})
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return q.score_pairs(q.load_pairs(p))


def test_agreement_counts_ties_conservatively(tmp_path):
    sc = make(tmp_path, [(GOOD, BAD, "a", "specificity"), (BAD, GOOD, "b", "specificity"), (GOOD, GOOD, "tie", "tie_both_good"), (GOOD, GOOD, "a", "specificity"), (GOOD, BAD, "b", "specificity")])
    m = q.evaluate(sc)
    assert m["n_decisive_labels"] == 4 and m["n_tie_labels"] == 1
    assert (m["decisive_agreement"]["k"], m["decisive_agreement"]["n"]) == (2, 4)                    # 예측 tie(P4)와 틀린 쌍(P5)은 일치가 아니다
    assert (m["decisive_agreement_when_decided"]["k"], m["decisive_agreement_when_decided"]["n"]) == (2, 3) and m["decisive_agreement_when_decided"]["coverage"] == 0.75
    assert m["tie_pairs"] == {"n": 1, "predicted_tie": 1, "separated_anyway": 0}
    assert (m["three_way_accuracy"]["k"], m["three_way_accuracy"]["n"]) == (3, 5)
    assert m["predicted"] == {"a": 2, "b": 1, "tie": 2, "unknown": 0}
    assert m["chance"]["always_one_side"] == 0.5 and m["chance"]["level"] == 0.5


def test_chance_level_uses_the_majority_label(tmp_path):
    sc = make(tmp_path, [(GOOD, BAD, "a", "specificity"), (GOOD, BAD, "a", "specificity"), (GOOD, BAD, "a", "specificity"), (BAD, GOOD, "b", "specificity")])   # 3개가 a, 1개가 b
    m = q.evaluate(sc)
    assert m["chance"]["always_one_side"] == 0.75 and m["chance"]["level"] == 0.75 and m["decisive_agreement"]["rate"] == 1.0


def test_by_reason_and_failures_list_the_missed_pairs(tmp_path):
    sc = make(tmp_path, [(GOOD, BAD, "a", "specificity"), (GOOD, BAD, "b", "hype"), (GOOD, GOOD, "tie", "tie_both_good")])
    br = q.by_reason(sc)
    assert (br["specificity"]["k"], br["specificity"]["n"]) == (1, 1) and (br["hype"]["k"], br["hype"]["n"]) == (0, 1) and (br["tie_both_good"]["k"], br["tie_both_good"]["n"]) == (1, 1)
    f = q.failures(sc)
    assert [x["id"] for x in f] == ["QP-D02"] and f[0]["label"] == "b" and f[0]["predicted"] == "a" and f[0]["top_signals"] and f[0]["a"] == GOOD


def test_signal_alone_counts_abstentions_and_own_reason(tmp_path):
    no_cta = {**GOOD, "cta": ""}
    sc = make(tmp_path, [(GOOD, BAD, "a", "cta"), (no_cta, BAD, "a", "cta"), (GOOD, GOOD, "tie", "tie_both_good")])
    bs = q.by_signal(sc)
    cta = bs["cta"]
    assert cta["decisive_pairs"] == 2 and cta["decided"] == 1 and cta["abstained"] == 1                   # 버튼이 비면 그 신호는 의견이 없다
    assert (cta["agreement_when_decided"]["k"], cta["agreement_when_decided"]["n"]) == (1, 1)
    assert cta["on_own_reason"]["reason_code"] == "cta" and cta["on_own_reason"]["decided"] == 1
    assert cta["on_tie_pairs"] == {"n": 1, "called_tie": 1, "separated": 0, "abstained": 0}
    assert set(bs) == set(cq.SIGNAL_KEYS) and abs(sum(v["weight"] for v in bs.values()) - 1) < 1e-9


def test_ablation_changes_predictions_when_a_deciding_signal_is_removed(tmp_path):
    """변이 확인: 신호를 빼도 예측이 하나도 안 바뀐다면 ablation 이 신호를 무시하는 것이다."""
    no_min = {**GOOD, "body": "구매 시 10월 31일까지 쓸 수 있어요."}       # GOOD 과 '5만원 이상' 조건만 빠짐 — 이 쌍을 가르는 것은 조건 표시 신호 하나다
    sc = make(tmp_path, [(GOOD, no_min, "a", "conditions")] * 3)
    full = q.evaluate(sc)
    ab = q.ablation(sc)
    assert full["decisive_agreement"]["rate"] == 1.0
    assert ab["conditions"]["predictions_changed"] == 3 and ab["conditions"]["delta_decisive"] == -1.0 and ab["conditions"]["decisive_agreement"] == 0.0
    assert ab["tone"]["predictions_changed"] == 0 and ab["tone"]["delta_decisive"] == 0.0
    assert set(ab) == set(cq.SIGNAL_KEYS)


def test_ablation_on_the_real_data_moves_the_predictions(pairs):
    sc = q.score_pairs([p for p in pairs if p.split == "dev"])
    ab = q.ablation(sc)
    assert ab["specificity"]["delta_decisive"] < 0 and ab["specificity"]["predictions_changed"] > 0
    assert sum(v["predictions_changed"] for v in ab.values()) > 0


def test_baselines_are_simple_heuristics(tmp_path):
    longer = {"headline": "가나다라마바사아자차", "body": "가나다라마바사아자차" * 3, "cta": "받기"}
    shorter = {"headline": "가나", "body": "가나", "cta": "받기"}
    sc = make(tmp_path, [(longer, shorter, "a", "specificity"), (shorter, longer, "a", "specificity")])
    b = q.baselines(sc)
    assert b["always_a"]["rate"] == 1.0 and b["longer_text_wins"]["rate"] == 0.5 and set(b) == {"always_a", "longer_text_wins", "more_digits_wins"}
    assert q.baselines([]) == {}


# ───────────────────────── 게이트 ─────────────────────────
def _fake_report(rate, n=24, level=0.5):
    return {"splits": {"dev": {"decisive_agreement": {"rate": rate, "n": n}, "chance": {"level": level}}}}


def test_gate_fails_at_or_below_chance_and_passes_above():
    assert q.gate(_fake_report(0.5)) and q.gate(_fake_report(0.40))
    assert q.gate(_fake_report(0.51)) == []
    assert q.gate(_fake_report(0.70, level=0.75))                         # 한쪽으로 쏠린 데이터는 그 쏠림이 우연 수준이다
    assert q.gate(_fake_report(0.2, n=q.MIN_PAIRS_FOR_GATE - 1)) == []     # 쌍이 너무 적으면 판정하지 않는다


def test_gate_compares_with_a_baseline_report():
    base = _fake_report(0.90)
    assert q.gate(_fake_report(0.86), base) == []
    out = q.gate(_fake_report(0.80), base)
    assert out and "기준선" in out[0]


def test_gate_flips_to_failure_on_inverted_labels(tmp_path):
    rows = [json.loads(l) for l in (ROOT / "evals" / "datasets" / "quality_pairs.jsonl").read_text(encoding="utf-8").splitlines() if l.startswith("{")]
    flip = {"a": "b", "b": "a", "tie": "tie"}
    for r in rows:
        r["better"] = flip[r["better"]]
    p = tmp_path / "flipped.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    rep = q.build_report(p)
    assert rep["gate"]["failures"] and any("우연 수준" in f for f in rep["gate"]["failures"])
    assert q.main(["--pairs", str(p), "--gate"]) == 1
    assert q.main(["--pairs", str(p)]) == 0


# ───────────────────────── 리포트 ─────────────────────────
def test_report_states_the_limits_and_the_procedure(report):
    assert "독립 라벨러 없음" in report["no_independent_labeler"] and "가상" in report["no_independent_labeler"]
    assert "클릭률" in report["not_measured"] and "측정하지 않았다" in report["not_measured"]
    assert "tie" in report["tie_handling"] and "일치로 세지 않는다" in report["tie_handling"]
    joined = " ".join(report["limitations"])
    for needle in ("독립 라벨러", "가상", "클릭률", "dev 일치율은", "쉬운 편", "단어 목록"):
        assert needle in joined, needle
    assert report["procedure"]["checks"]["signal_code_unchanged_since_freeze"] and report["procedure"]["frozen"]["weights"] == report["signals"]["weights"]
    assert report["signals"]["assumed_limits_note"].startswith("이 프로젝트가 가정한 값") and "python -m evals.quality_signals" in report["reproduce"]
    assert "ablation_note" in report["splits"]["dev"] and "척도" in report["splits"]["dev"]["ablation_note"]
    assert set(report["splits"]) == {"dev", "holdout", "all"}


def test_report_numbers_are_internally_consistent(report):
    for sp in ("dev", "holdout", "all"):
        m = report["splits"][sp]
        assert m["n"] == report["dataset"]["counts"][sp]["pairs"] == m["n_decisive_labels"] + m["n_tie_labels"]
        assert sum(m["predicted"].values()) == m["n"]
        assert m["decisive_agreement"]["n"] == m["n_decisive_labels"]
        assert set(m["by_signal"]) == set(m["ablation"]) == set(cq.SIGNAL_KEYS)
        assert sum(v["n"] for v in m["by_reason"].values()) == m["n"]
        assert len(m["failures"]) == m["n"] - sum(v["k"] for v in m["by_reason"].values())
    a, d, h = (report["splits"][s]["decisive_agreement"] for s in ("all", "dev", "holdout"))
    assert a["k"] == d["k"] + h["k"] and a["n"] == d["n"] + h["n"]


def test_the_real_data_passes_the_loose_gate_and_beats_simple_baselines(report):
    assert report["gate"]["failures"] == []
    for sp in ("dev", "holdout"):
        m = report["splits"][sp]
        assert m["decisive_agreement"]["rate"] > max(m["chance"]["level"], m["baselines"]["longer_text_wins"]["rate"], m["baselines"]["more_digits_wins"]["rate"])


def test_report_is_deterministic():
    a = json.dumps(q.build_report(), ensure_ascii=False, sort_keys=True)
    b = json.dumps(q.build_report(), ensure_ascii=False, sort_keys=True)
    assert a == b


def test_stored_report_matches_a_fresh_run():
    """저장된 evals/reports/quality_signals.json 이 지금 코드·데이터로 다시 만든 것과 같다(쌍·신호가 바뀌었는데 리포트만 낡은 상태가 되지 않게)."""
    stored = json.loads((ROOT / "evals" / "reports" / "quality_signals.json").read_text(encoding="utf-8"))
    fresh = json.loads(json.dumps(q.build_report(), ensure_ascii=False, sort_keys=True))
    assert stored == fresh


def test_cli_writes_a_report_and_prints_the_disclaimers(tmp_path, capsys):
    out = tmp_path / "r" / "q.json"
    assert q.main(["--split", "dev", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    for needle in ("독립 라벨러 없음", "클릭률", "결정 쌍 일치율", "하나씩 뺐을 때", "게이트: 통과"):
        assert needle in text
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert list(saved["splits"]) == ["dev"] and saved["dataset"]["counts"]["all"]["pairs"] >= 80
    out2 = tmp_path / "q2.json"
    q.main(["--split", "dev", "--out", str(out2)])
    assert out.read_bytes() == out2.read_bytes()
    with pytest.raises(SystemExit):
        q.main(["--split", "train"])


def test_cli_baseline_option_gates_on_a_drop(tmp_path):
    base = q.build_report()
    base["splits"]["dev"]["decisive_agreement"]["rate"] = 1.0
    base["splits"]["holdout"]["decisive_agreement"]["rate"] = 1.0
    path = tmp_path / "base.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    assert q.main(["--baseline", str(path), "--gate"]) == 1          # 기준선이 100% 였다면 지금의 ~92% 는 5%p 넘게 떨어진 것


# ───────────────────────── 구조 ─────────────────────────
def test_eval_module_does_not_pull_in_judging_or_model_layers():
    tree = ast.parse((ROOT / "evals" / "quality_signals.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert mods <= {"__future__", "argparse", "hashlib", "json", "sys", "dataclasses", "pathlib", "typing", "core", "core.schemas", "evals.metrics"}, mods
    doc = q.__doc__
    assert "judge" in doc and "대역" in doc and "일치도는 재지 않는다" in doc and "이 숫자가 의미하지 않는 것" in doc


def test_module_import_has_no_side_effects(tmp_path):
    import subprocess
    import sys

    before = {p.name for p in (ROOT / "evals" / "reports").iterdir()}
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); import evals.quality_signals"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout == "" and out.stderr == ""
    assert {p.name for p in (ROOT / "evals" / "reports").iterdir()} == before
