"""성과 인사이트 평가 — 합성 시나리오로 '이상 탐지 → 원인 진단 → 개선 액션 → 보고서 숫자 충실도' 를 잰다.

    python -m evals.insights_eval [--split dev|holdout|all] [--gate] [--regression] [--out path.json]

모델을 부르지 않는다(결정론). 시나리오(evals/datasets/perf_scenarios.jsonl)는 이 프로젝트가 만든 합성 데이터이고 정답도 같은 사람이 정했다.

지표 (모두 이 코드가 계산한다)
- 이상 탐지 재현율/정밀도: 엔진이 찾은 구간과 정답 구간이 하루라도 겹치면 맞은 것. (탐지를 기대할 수 없게 만든 표본 부족 시나리오는 재현율 분모에서 뺀다)
- 대조 오경보율: 아무 일도 없거나 요일 효과뿐인 시나리오에서 이상을 하나라도 낸 비율.
- 원인 top-1/top-3 정확도: 원인을 가릴 수 있는 시나리오에서 1순위가 정답인가 / 정답 원인이 모두 3순위 안에 있는가(동시 사건은 모든 주 원인이 포함돼야 한다).
  엔진이 '원인을 정하기 어렵다' 고 답한 시나리오는 틀린 것으로 센다(보류를 공짜로 만들지 않는다) — 보류 비율은 따로 적는다.
- undetermined 적절성: 데이터만으로 원인을 가릴 수 없게 설계한 시나리오에서 원인을 단정('원인은 …')하지 않은 비율, 그리고 '정하기 어렵다' 로 답한 비율.
- 금지 액션 제안 수 (게이트 0): 엔진이 낸 액션 중 그 시나리오 정답이 금지한 것.
- 보고서 숫자 충실도 (게이트 0): 템플릿 보고서 문장 속 근거 없는 숫자 수 + 문장 검증기가 놓친 주입 수(아래 주입 시험).
- 주입 시험: 모델이 틀린 문장을 냈다고 가정하고(숫자 바꾸기·없는 숫자 끼워 넣기·없는 근거 id·카탈로그 밖 액션·금지 액션·과한 원인 단정·방향 뒤집기·링크 등) 검증기가 거르는지 센다.
- 확신 보정: 엔진이 낸 확신 구간별로 그 원인이 실제 정답이었던 비율.

이 숫자가 의미하지 않는 것
- 실제 광고 데이터의 정확도가 아니다. 시나리오는 합성이고, 생성기와 진단 규칙을 같은 사람이 설계했다(순환 위험). 생성기가 남기는 흔적을 진단 규칙이 알고 있다.
  holdout 은 엔진을 동결한 뒤 다른 시드·다른 크기 범위·혼동하기 쉬운 조합으로 만들어 이를 줄이려는 시도일 뿐이다.
- 모델이 쓴 문장의 품질은 측정하지 않았다(대역·템플릿·주입 시험만). 주입 시험은 우리가 떠올린 실패 유형만 센다.
- 확신(confidence)은 확률이 아니다. 구간별 정답률은 이 합성 시나리오 수가 적어(구간당 수 건) 불확실하다.
- 'allowed_action_hit' 은 우리가 정한 허용 목록과 겹치는지일 뿐 실제 운영 성과 개선을 뜻하지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from agents import insights as ins
from core import insight_engine as ie
from core.performance import ScenarioSpec, generate

DATASETS = Path(__file__).with_name("datasets")
REPORTS = Path(__file__).with_name("reports")
DATASET_PATH = DATASETS / "perf_scenarios.jsonl"
REPORT_PATH = REPORTS / "insights_controlled.json"
PRIOR_REGRESSION_PATH = REPORTS / "insights_regression.json"
FIRST_FOLLOWUP_PATH = REPORTS / "insights_followup_regression.json"
FIRST_HOLDOUT_PATH = REPORTS / "insights_holdout_20261020.json"
ROOT = Path(__file__).resolve().parents[1]

# 회귀 감시 허용 폭: 저장된 보고서 대비 이만큼 이상 나빠지면 게이트 실패 (비율 지표, 절대 차이)
REGRESSION_TOLERANCE = 0.05
HIGHER_BETTER = ("detection_recall", "detection_precision", "cause_top1", "cause_top3", "ambiguous_not_asserted", "allowed_action_hit")
LOWER_BETTER = ("control_false_alarm_rate", "overabstain_rate")
ZERO_GATES = ("forbidden_proposals", "unsupported_numbers", "audit_problems", "causal_overclaims", "perturbation_escaped")


@dataclass
class Scenario:
    id: str
    split: str
    family: str
    spec: ScenarioSpec
    truth: dict
    frozen_engine: str = ""


def load_scenarios(split: Optional[str] = None, path: Path = DATASET_PATH) -> list[Scenario]:
    out: list[Scenario] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if "_meta" in d:
            continue
        sc = Scenario(id=d["scenario_id"], split=d["split"], family=d["family"], spec=ScenarioSpec.from_dict(d["spec"]), truth=d["truth"],
                      frozen_engine=d.get("frozen_engine_fingerprint", ""))
        if split in (None, "all") or sc.split == split:
            out.append(sc)
    return out


def dataset_meta(path: Path = DATASET_PATH) -> dict:
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    return first.get("_meta", {})


def engine_fingerprint() -> str:
    """엔진(탐지·진단·액션)과 데이터 생성기 소스의 해시. 줄바꿈 방식에 영향받지 않게 정규화한다."""
    h = hashlib.sha256()
    for rel in ("core/insight_engine.py", "core/performance.py"):
        h.update((ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    return h.hexdigest()[:16]


def dataset_fingerprint(path: Path = DATASET_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ───────────────────────────── 시나리오 하나 ─────────────────────────────
def _overlap(a: tuple, b: tuple) -> bool:
    return not (a[1] < b[0] or b[1] < a[0])


def _iso(d: str) -> dt.date:
    return dt.date.fromisoformat(d)


def run_scenario(sc: Scenario) -> dict:
    rows = generate(sc.spec)
    a = ie.analyze(rows)
    truth = sc.truth
    t_inc = [(_iso(i["start"]), _iso(i["end"]), i["kind"]) for i in truth["incidents"]]
    e_inc = [(i.start, i.end) for i in a.incidents]
    matched = [any(_overlap((s, e), (ts, te)) for s, e in e_inc) for ts, te, _ in t_inc]
    tp_engine = [any(_overlap((s, e), (ts, te)) for ts, te, _ in t_inc) for s, e in e_inc]
    ranked = a.ranked_causes()
    answers = [c for c, conf in ranked if conf >= ie.CANDIDATE_CONFIDENCE]
    top1 = ranked[0][0] if ranked and ranked[0][1] >= ie.LIKELY_CONFIDENCE and a.diagnoses else None
    top3 = answers[:3]
    causes = list(truth["main_causes"])
    states = [dg.cause_state for dg in a.diagnoses]
    actions = [p.action_id for p in a.actions]
    forbidden = sorted(set(actions) & set(truth["forbidden_actions"]))
    res = {
        "id": sc.id, "split": sc.split, "family": sc.family, "ambiguous": truth["ambiguous"], "detectable": truth["detectable"], "is_control": truth["is_control"],
        "status": a.status, "truth_incidents": [{"kind": k, "start": s.isoformat(), "end": e.isoformat()} for s, e, k in t_inc],
        "comparison_window": [d.isoformat() for d in a.window], "reference_window": [d.isoformat() for d in a.baseline],
        "reference_candidates": a.reference_windows, "used_earlier_reference": bool(a.reference_evidence),
        "engine_incidents": [{"start": s.isoformat(), "end": e.isoformat(), "severity": i.severity, "polarity": i.polarity, "cause_state": dg.cause_state}
                             for (s, e), i, dg in zip(e_inc, a.incidents, a.diagnoses)],
        "truth_detected": matched, "engine_incident_tp": tp_engine,
        "main_causes": causes, "ranked": [(c, round(conf, 3)) for c, conf in ranked[:4]], "top1": top1, "top3": top3, "cause_states": states,
        "actions": actions, "forbidden_hits": forbidden, "allowed_hit": bool(set(actions) & set(truth["allowed_actions"])),
        "avoid_rules": sorted({v.rule for v in a.avoid}),
    }
    if causes and not truth["ambiguous"]:
        res["top1_correct"] = top1 in causes
        res["top3_correct"] = set(causes) <= set(top3)
        res["any_in_top3"] = bool(set(causes) & set(top3))
        res["abstained"] = top1 is None
    # 보고서 숫자 충실도 (템플릿 문장 — 모델 없음)
    rep = ins.build_report(rows, router=None, settings=None, data_origin="synthetic", analysis=a)
    problems = ins.audit_report(rep)
    texts = [rep.headline, rep.summary] + [f.text for f in rep.findings] + [a.why for a in rep.actions]
    causal_overclaims = sum(bool(ins.causal_issue(text, set(), a.cause_conf())) for text in texts)
    res["report"] = {"unsupported_numbers": rep.unsupported_numbers, "audit_problems": len(problems), "problem_reasons": sorted({p["reason"] for p in problems}),
                     "causal_overclaims": causal_overclaims}
    res["perturbation"] = perturbation_test(a, rep)
    return res


# ───────────────────────────── 주입 시험 ─────────────────────────────
def _tweak_number(text: str) -> Optional[str]:
    import re

    m = re.search(r"(\d+)(\.\d)?%", text)
    if not m:
        return None
    new = f"{int(m.group(1)) + 7}{m.group(2) or ''}%"
    return text[:m.start()] + new + text[m.end():]


def _flip_direction(text: str) -> Optional[str]:
    pairs = (("낮았", "높았"), ("높았", "낮았"), ("적었", "많았"), ("많았", "적었"))
    for a, b in pairs:
        if a in text:
            return text.replace(a, b, 1)
    return None


def perturbation_test(a: ie.Analysis, rep: ins.InsightReport) -> dict:
    """모델이 틀린 문장을 냈다고 가정하고, 정상 템플릿 문장에 틀린 문장 하나를 더해 검증기가 그 문장을 거르는지 센다.
    정상 문장이 잘못 버려지는 일(과잉 차단)도 센다."""
    ctx = a.to_context()
    tpl = ie.template_narrative(ctx)
    base_f = [SimpleNamespace(text=f["text"], evidence_ids=list(f["evidence_ids"])) for f in tpl["findings"]]
    base_a = [SimpleNamespace(action_id=x["action_id"], why=x["why"], evidence_ids=list(x["evidence_ids"])) for x in tpl["actions"]]
    first_ev = tpl["findings"][0]["evidence_ids"] if tpl["findings"] else []
    all_causes = ie.CAUSES
    low_cause = min(all_causes, key=lambda c: a.cause_conf().get(c, 0.0))
    low_conf = a.cause_conf().get(low_cause, 0.0)
    overview = ctx["overview_evidence"] or (first_ev[0] if first_ev else "")
    cases: list[tuple[str, str, SimpleNamespace, str]] = []   # (유형, 기대 이유 접두, 틀린 문장, 위치)
    num_src = next((f for f in tpl["findings"] if "%" in f["text"]), None)
    if num_src and _tweak_number(num_src["text"]):
        cases.append(("number_tweak", "number_unsupported", SimpleNamespace(text=_tweak_number(num_src["text"]), evidence_ids=list(num_src["evidence_ids"])), "finding"))
    if first_ev:
        cases.append(("number_inject", "number_unsupported", SimpleNamespace(text=f"{tpl['findings'][0]['text']} 이 변화로 약 37% 손실이 났습니다.", evidence_ids=list(first_ev)), "finding"))
        cases.append(("unknown_evidence", "unknown_evidence", SimpleNamespace(text="기간 동안 지표 변화가 있었습니다.", evidence_ids=["E99"]), "finding"))
        cases.append(("no_evidence", "no_evidence", SimpleNamespace(text="기간 동안 지표 변화가 있었습니다.", evidence_ids=[]), "finding"))
        cases.append(("unsafe_link", "unsafe_text", SimpleNamespace(text=f"{tpl['findings'][0]['text']} 자세히: http://evil.example/x", evidence_ids=list(first_ev)), "finding"))
        cases.append(("cause_overclaim", "cause_overclaim", SimpleNamespace(text=f"원인은 {ie.CAUSE_LABEL[low_cause]}입니다.", evidence_ids=[overview]), "finding")
                     if low_conf < ie.ASSERT_CONFIDENCE else ("cause_overclaim", "skip", None, "finding"))
        strongest = max(all_causes, key=lambda c: a.cause_conf().get(c, 0.0))
        cases.append(("high_support_overclaim", "cause_overclaim", SimpleNamespace(text=f"원인은 {ie.CAUSE_LABEL[strongest]}입니다.", evidence_ids=[overview]), "finding"))
        cases.append(("disclaimer_then_overclaim", "cause_overclaim", SimpleNamespace(text=f"원인을 확정할 수 없습니다. 하지만 원인은 {ie.CAUSE_LABEL[strongest]}입니다.", evidence_ids=[overview]), "finding"))
    flip_src = next((f for f in tpl["findings"] if _flip_direction(f["text"])), None)
    if flip_src:
        cases.append(("direction_flip", "direction_conflict", SimpleNamespace(text=_flip_direction(flip_src["text"]), evidence_ids=list(flip_src["evidence_ids"])), "finding"))
    if a.incidents and overview:
        cases.append(("all_clear", "all_clear_conflict", SimpleNamespace(text="눈에 띄는 이상은 없었고 모두 정상 범위입니다.", evidence_ids=[overview]), "finding"))
    cases.append(("unknown_action", "unknown_action", SimpleNamespace(action_id="delete_campaign", why="캠페인을 지우세요.", evidence_ids=list(first_ev)), "action"))
    blocked = next((aid for aid in ie.CATALOG if not ie.evaluate_action(aid, a.state).allowed and ie.evaluate_action(aid, a.state).hard and aid not in [x.action_id for x in base_a]), None)
    if blocked and first_ev:
        cases.append(("forbidden_action", "guard", SimpleNamespace(action_id=blocked, why=ie.ACTION_INSTRUCTION[blocked], evidence_ids=list(first_ev)), "action"))
    change = next((x for x in base_a if ie.CATALOG[x.action_id].kind == "change"), None)
    if change:
        unchecked = SimpleNamespace(action_id=change.action_id, why="설정을 지금 바꾸세요.", evidence_ids=list(change.evidence_ids))
        cases.append(("change_without_check", "verification_required", unchecked, "replacement_action"))
    injected = caught = escaped = 0
    false_drops = 0
    escaped_kinds: list[str] = []
    for kind, expect, bad, where in cases:
        if bad is None:
            continue
        findings = base_f + [bad] if where == "finding" else base_f
        actions = base_a + [bad] if where == "action" else base_a
        if where == "replacement_action":
            actions = [bad if x.action_id == bad.action_id else x for x in base_a]
        parsed = SimpleNamespace(headline=tpl["headline"], summary=tpl["summary"], findings=findings, actions=actions)
        _, _, kept_f, kept_a, dropped = ins.validate_model_output(parsed, a)
        injected += 1
        bad_text = bad.text if where == "finding" else bad.why
        survived = any(f.text == bad_text for f in kept_f) or any(x.why == bad_text for x in kept_a)
        hit = [d for d in dropped if d.text == bad_text and d.reason.startswith(expect)]
        if survived or not hit:
            escaped += 1
            escaped_kinds.append(kind)
        else:
            caught += 1
        false_drops += sum(1 for d in dropped if d.text != bad_text)
    return {"injected": injected, "caught": caught, "escaped": escaped, "false_drops": false_drops, "escaped_kinds": escaped_kinds}


# ───────────────────────────── 집계 ─────────────────────────────
def _rate(k: int, n: int) -> Optional[float]:
    return round(k / n, 4) if n else None


def aggregate(results: list[dict]) -> dict:
    ev = [r for r in results if r["truth_incidents"]]                          # 사건이 있는 시나리오
    det = [r for r in ev if r["detectable"]]
    controls = [r for r in results if r["is_control"]]
    resolvable = [r for r in ev if "top1_correct" in r]                          # 원인을 가릴 수 있는 시나리오
    amb = [r for r in ev if r["ambiguous"]]
    tp_inc = sum(sum(r["truth_detected"]) for r in det)
    n_inc = sum(len(r["truth_detected"]) for r in det)
    e_inc = sum(len(r["engine_incident_tp"]) for r in results)
    e_tp = sum(sum(r["engine_incident_tp"]) for r in results)
    ctrl_fa = sum(1 for r in controls if r["engine_incidents"])
    detected_resolvable = [r for r in resolvable if any(r["truth_detected"])]
    out = {
        "n_scenarios": len(results), "n_events": len(ev), "n_controls": len(controls), "n_resolvable": len(resolvable), "n_ambiguous": len(amb),
        "detection_recall": _rate(tp_inc, n_inc), "detection_recall_n": f"{tp_inc}/{n_inc}",
        "detection_precision": _rate(e_tp, e_inc), "detection_precision_n": f"{e_tp}/{e_inc}",
        "control_false_alarm_rate": _rate(ctrl_fa, len(controls)), "control_false_alarm_n": f"{ctrl_fa}/{len(controls)}",
        "cause_top1": _rate(sum(r["top1_correct"] for r in resolvable), len(resolvable)), "cause_top1_n": f"{sum(r['top1_correct'] for r in resolvable)}/{len(resolvable)}",
        "cause_top3": _rate(sum(r["top3_correct"] for r in resolvable), len(resolvable)), "cause_top3_n": f"{sum(r['top3_correct'] for r in resolvable)}/{len(resolvable)}",
        "cause_any_in_top3": _rate(sum(r["any_in_top3"] for r in resolvable), len(resolvable)),
        "abstained_n": sum(1 for r in resolvable if r["abstained"]),
        "overabstain_rate": _rate(sum(1 for r in detected_resolvable if r["abstained"]), len(detected_resolvable)),
        # 새 상태 supported는 원인 단정이 아니다. 상태 이름만으로 통과시키지 않고 실제 문장을 점검한다.
        # causal_overclaims가 없는 과거 레코드는 당시 상태 검사를 유지한다.
        "ambiguous_not_asserted": _rate(sum(1 for r in amb if not r["report"].get("causal_overclaims", "asserted" in r["cause_states"])), len(amb)),
        "ambiguous_undetermined": _rate(sum(1 for r in amb if r["cause_states"] and all(s == "undetermined" for s in r["cause_states"])), len(amb)),
        "ambiguous_supported_n": sum(1 for r in amb if "supported" in r["cause_states"]),
        "ambiguous_detected_n": sum(1 for r in amb if r["engine_incidents"]),
        "forbidden_proposals": sum(len(r["forbidden_hits"]) for r in results),
        "scenarios_with_forbidden": [r["id"] for r in results if r["forbidden_hits"]],
        "allowed_action_hit": _rate(sum(r["allowed_hit"] for r in results if not r["ambiguous"] or r["truth_incidents"]), len([r for r in results if not r["ambiguous"] or r["truth_incidents"]])),
        "unsupported_numbers": sum(r["report"]["unsupported_numbers"] for r in results),
        "audit_problems": sum(r["report"]["audit_problems"] for r in results),
        "causal_overclaims": sum(r["report"].get("causal_overclaims", 0) for r in results),
        "perturbation_injected": sum(r["perturbation"]["injected"] for r in results),
        "perturbation_caught": sum(r["perturbation"]["caught"] for r in results),
        "perturbation_escaped": sum(r["perturbation"]["escaped"] for r in results),
        "perturbation_false_drops": sum(r["perturbation"]["false_drops"] for r in results),
        "calibration": calibration(results),
    }
    return out


BINS = ((ie.CANDIDATE_CONFIDENCE, ie.LIKELY_CONFIDENCE, "후보(0.25~0.45)"), (ie.LIKELY_CONFIDENCE, ie.SUPPORTED_CONFIDENCE, "우선 확인(0.45~0.75)"),
        (ie.SUPPORTED_CONFIDENCE, 1.01, "근거 많음(0.75 이상)"))


def calibration(results: list[dict]) -> dict:
    """엔진이 낸 (원인, 확신) 쌍마다 그 원인이 정답 원인에 들어 있었는지 — 확신 구간별 정답률. 원인을 가릴 수 있는 시나리오만."""
    pairs = []
    for r in results:
        if "top1_correct" not in r:
            continue
        for c, conf in r["ranked"]:
            if conf >= ie.CANDIDATE_CONFIDENCE:
                pairs.append((conf, c in r["main_causes"]))
    bins = []
    for lo, hi, name in BINS:
        sel = [ok for conf, ok in pairs if lo <= conf < hi]
        bins.append({"bin": name, "n": len(sel), "accuracy": _rate(sum(sel), len(sel))})
    return {"bins": bins, "pairs": len(pairs), "note": "구간당 건수가 적어 불확실하다. 확신은 확률이 아니다."}


# holdout 결과를 본 뒤에 적은 관찰이다. 엔진(core/insight_engine.py · core/performance.py)은 이 결과를 보고 고치지 않았다 — 나쁜 것은 나쁜 대로 남긴다.
HOLDOUT_OBSERVATIONS = [
    "대조 4개 중 2개에서 이상을 냈다. holdout-no_event-1(잡음 1.6배 + 하루 -0.3% 추세)은 5일짜리 '원인을 정하기 어렵다' 구간을, holdout-seasonal_dip-1(요일 효과 3.5배)은 13일짜리 '좋은 방향' 변화를 냈다. 요일 보정과 추세 보정은 이 강도에서는 부족하다.",
    "holdout-landing_issue-1(전환율 약 19% 하락)에서 '일시 중지 검토'를 제안했다. 한 기기 묶음의 짧은 구간 변화율(-52%)이 '랜딩 문제가 심각하다' 판정에 쓰였기 때문이다. 금지 액션은 아니지만 약한 문제에는 과한 제안이다.",
    "같은 지표를 건드리는 동시 사건 3개는 어렵다: holdout-multi-1 은 소재 피로만 '가능성 높음'(도달 포화는 0.29 후보), holdout-multi-2 는 입찰 경쟁만 단정하고 예산 소진을 놓쳤고, holdout-multi-3 은 '원인을 정하기 어렵다' 로 답했다.",
    "문장 검증기(agents/insights.py)는 holdout 주입 시험에서 구멍 1건(±0.5% 미만 변화의 방향 비교가 비어 있던 것)이 드러나 고쳤다. 검증기는 탐지·진단 엔진이 아니며, 엔진 지문(engine_fingerprint)은 holdout 시나리오를 만들 때와 같다.",
]


def evaluate(split: str = "all", *, dataset_path: Optional[Path] = None) -> dict:
    path = Path(dataset_path) if dataset_path is not None else DATASET_PATH
    scenarios = load_scenarios(split, path) if dataset_path is not None else load_scenarios(split)
    meta = dataset_meta(path) if dataset_path is not None else dataset_meta()
    data_fp = dataset_fingerprint(path) if dataset_path is not None else dataset_fingerprint()
    known_regression_data = data_fp == dataset_fingerprint(DATASET_PATH)
    results = [run_scenario(sc) for sc in scenarios]
    by_split: dict[str, dict] = {}
    for s in ("dev", "holdout"):
        sub = [r for r in results if r["split"] == s]
        if sub:
            by_split[s] = aggregate(sub)
    fp = engine_fingerprint()
    held = [sc for sc in scenarios if sc.split == "holdout"]
    frozen = sorted({sc.frozen_engine for sc in held})
    provenance_issues = []
    if held:
        if "" in frozen:
            provenance_issues.append("holdout 시나리오에 동결 엔진 지문이 빠져 있습니다.")
        if len(frozen) != 1:
            provenance_issues.append("holdout 시나리오의 동결 엔진 지문이 서로 다릅니다.")
        meta_fp = meta.get("holdout_frozen_engine_fingerprint", "")
        if not meta_fp or frozen != [meta_fp]:
            provenance_issues.append("holdout 시나리오와 데이터셋 메타데이터의 동결 엔진 지문이 일치하지 않습니다.")
    engine_changed = bool(held) and any(value != fp for value in frozen)
    return {
        "dataset": path.stem, "dataset_path": str(path), "data": "synthetic",
        "command": f"python -m evals.insights_eval --split {split}" + (f" --dataset {shlex.quote(str(path))}" if dataset_path is not None else "")
                   + (" --regression" if engine_changed else "") + " --out evals/reports/insights_followup_regression_v2_20261020.json",
        "engine_fingerprint": fp, "dataset_fingerprint": data_fp, "holdout_frozen_engine": frozen[0] if len(frozen) == 1 else "",
        "engine_changed_after_holdout": engine_changed, "holdout_provenance_issues": provenance_issues,
        "holdout_role": ("regression" if engine_changed else "held_out") if held else "",
        "engine_params": {"recent_days": ie.RECENT_DAYS, "baseline_days": ie.BASELINE_DAYS, "h_score": ie.H_SCORE, "h_single": ie.H_SINGLE, "min_effect": ie.MIN_EFFECT,
                          "sigma_inflate": ie.SIGMA_INFLATE, "seg_h_score": ie.SEG_H_SCORE, "supported_confidence": ie.SUPPORTED_CONFIDENCE, "likely_confidence": ie.LIKELY_CONFIDENCE,
                          "candidate_confidence": ie.CANDIDATE_CONFIDENCE, "baseline_estimation_factor": ie.BASELINE_ESTIMATION_FACTOR,
                          "comparison_adjustment": "sqrt(h^2 + 2*log(M)): M=2*series for the preceding reference; M=2*earlier_candidates*series for earlier references; series only when no earlier reference exists", "segment_metrics": ie.SEGMENT_METRICS,
                          "reference_search": "prefixes of the preceding baseline; minimum 14 observed days and two observations per weekday; unchanged reporting window",
                          "older_reference_uncertainty": "includes slope uncertainty even when the selected trend is zero"},
        "splits": by_split, "holdout_observations": HOLDOUT_OBSERVATIONS if held and known_regression_data else [], "scenarios": results,
        "holdout_observations_note": "관찰은 원래 고정 엔진으로 처음 평가했을 때의 기록입니다. 엔진 변경 후 재실행은 이미 확인한 시나리오의 회귀 검사입니다." if held and known_regression_data else "",
        "limitations": [
            "시나리오는 우리가 만든 합성 데이터이고 생성기와 진단 규칙을 같은 사람이 설계했다(순환 위험). holdout 은 이를 줄이려는 시도일 뿐 실제 광고 데이터의 정확도가 아니다.",
            "모델 문장화의 품질은 측정하지 않았다(대역·템플릿·주입 시험만).",
            "근거 일치 점수는 원인이 맞을 확률이 아니다. 집계 관측 자료로는 원인을 단정할 수 없다. 구간별 정답률은 건수가 적어 불확실하다.",
            "탐지 점수는 기준선과 추세의 추정 오차 및 비교 수를 보정한 근사값이다. 자기상관·급격한 분산 변화·외부 충격 아래의 오류 확률을 보장하지 않는다.",
            "이전 기준 기간 탐색은 표시한 최근 기간과 과거의 차이를 찾는다. 선택한 기준 기간의 끝이나 표시 구간의 시작이 실제 사건 시작일이라는 뜻은 아니다.",
            "ambiguous_not_asserted는 실제 보고서 문장의 원인 과장 검사를 뜻한다. 모호한 원인을 잘 가렸다는 지표가 아니며 supported 건수와 탐지 여부를 함께 봐야 한다.",
            "주입 시험은 우리가 떠올린 실패 유형만 센다.",
        ],
    }


def check_gates(report: dict, stored: Optional[dict], *, regression: bool = False) -> list[str]:
    fails: list[str] = list(report.get("holdout_provenance_issues", []))
    for split, agg in report["splits"].items():
        for g in ZERO_GATES:
            if agg.get(g):
                fails.append(f"{split}: {g} = {agg[g]} (0 이어야 함)")
        if stored and report.get("dataset_fingerprint") == stored.get("dataset_fingerprint") and split in stored.get("splits", {}):
            old = stored["splits"][split]
            for k in HIGHER_BETTER:
                if agg.get(k) is not None and old.get(k) is not None and agg[k] < old[k] - REGRESSION_TOLERANCE:
                    fails.append(f"{split}: {k} {old[k]} → {agg[k]} (허용 폭 {REGRESSION_TOLERANCE} 초과 하락)")
            for k in LOWER_BETTER:
                if agg.get(k) is not None and old.get(k) is not None and agg[k] > old[k] + REGRESSION_TOLERANCE:
                    fails.append(f"{split}: {k} {old[k]} → {agg[k]} (허용 폭 {REGRESSION_TOLERANCE} 초과 상승)")
    if report.get("engine_changed_after_holdout") and not regression:
        fails.append("엔진이 holdout 시나리오를 만든 뒤 바뀌었습니다 — holdout 결과는 '엔진을 고정한 뒤 한 번 본 값' 이 아닙니다.")
    return fails


def load_report(path: Path = REPORT_PATH) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def format_summary(report: dict) -> str:
    lines = []
    if report.get("holdout_role") == "regression":
        lines.append("이미 확인한 최종 확인용 시나리오의 회귀 검사입니다. 새 최종 확인용 평가 결과로 해석할 수 없습니다.")
    for split, a in report["splits"].items():
        lines.append(f"[{split}] 시나리오 {a['n_scenarios']}개 (사건 {a['n_events']} · 대조 {a['n_controls']} · 원인 가릴 수 있음 {a['n_resolvable']} · 가릴 수 없게 설계 {a['n_ambiguous']})")
        lines.append(f"  이상 탐지 재현율 {a['detection_recall']} ({a['detection_recall_n']}) · 정밀도 {a['detection_precision']} ({a['detection_precision_n']}) · 정상인데 이상으로 알린 비율 {a['control_false_alarm_rate']} ({a['control_false_alarm_n']})")
        lines.append(f"  원인 top-1 {a['cause_top1']} ({a['cause_top1_n']}) · top-3 {a['cause_top3']} ({a['cause_top3_n']}) · 보류 {a['abstained_n']}건 · 과잉 보류율 {a['overabstain_rate']}")
        lines.append(f"  원인이 모호한 사례: 보고서 원인 과장 없음 {a['ambiguous_not_asserted']} · '정하기 어렵다' {a['ambiguous_undetermined']} · 근거 많은 가설 {a.get('ambiguous_supported_n', '미집계')}건")
        lines.append(f"  금지 액션 제안 {a['forbidden_proposals']}건 {a['scenarios_with_forbidden']} · 허용 액션 적중 {a['allowed_action_hit']}")
        lines.append(f"  보고서 근거 없는 숫자 {a['unsupported_numbers']} · 점검 문제 {a['audit_problems']} · 주입 {a['perturbation_injected']}건 중 차단 {a['perturbation_caught']} / 놓침 {a['perturbation_escaped']} (정상 문장 잘못 버림 {a['perturbation_false_drops']})")
        lines.append("  근거 일치 점수 구간별 결과: " + " · ".join(f"{b['bin']} n={b['n']} 정답률 {b['accuracy']}" for b in a["calibration"]["bins"]))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="성과 인사이트 평가 (합성 시나리오, 모델 호출 없음)")
    ap.add_argument("--split", default="all", choices=["dev", "holdout", "all"])
    ap.add_argument("--dataset", default="", help="별도 동결 시나리오 JSONL 경로 (기본은 이미 확인한 기존 데이터)")
    ap.add_argument("--gate", action="store_true", help="게이트(0 이어야 하는 항목 + 저장된 보고서 대비 회귀)를 검사하고 실패하면 종료 코드 1")
    ap.add_argument("--regression", action="store_true", help="이미 확인한 holdout 을 회귀 검사로 사용 (동결 지문·메타데이터 및 수치 게이트는 계속 검사)")
    ap.add_argument("--out", default="", help="결과 JSON 을 쓸 별도 경로 (예: evals/reports/insights_followup_regression_v2_20261020.json)")
    args = ap.parse_args(argv)
    historical_output = bool(args.out) and Path(args.out).resolve() in {REPORT_PATH.resolve(), PRIOR_REGRESSION_PATH.resolve(), FIRST_FOLLOWUP_PATH.resolve(), FIRST_HOLDOUT_PATH.resolve()}
    if historical_output:
        ap.error("과거 평가 기록은 덮어쓸 수 없습니다. 회귀 결과의 --out 에 별도 경로를 지정하세요.")
    stored = load_report() if args.gate else None
    report = dict(evaluate(args.split, dataset_path=Path(args.dataset)) if args.dataset else evaluate(args.split))
    if args.regression and "holdout" in report["splits"]:
        report["holdout_role"] = "regression"
    command = ["python", "-m", "evals.insights_eval", "--split", args.split]
    if args.dataset:
        command.extend(["--dataset", args.dataset])
    if args.gate:
        command.append("--gate")
    if args.regression:
        command.append("--regression")
    if args.out:
        command.extend(["--out", args.out])
    report["command"] = shlex.join(command)
    print(format_summary(report))
    if report["engine_changed_after_holdout"] and not args.regression:
        print("경고: 엔진이 holdout 시나리오를 만든 뒤 바뀌었습니다 — holdout 결과를 '고정 후 한 번 본 값' 으로 쓸 수 없습니다.")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        report = dict(report)
        report["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"저장: {out}")
    if args.gate:
        fails = check_gates(report, stored, regression=args.regression)
        if fails:
            print("게이트 실패:\n  " + "\n  ".join(fails))
            return 1
        print("게이트 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
