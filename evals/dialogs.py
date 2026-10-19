"""대화형 캠페인 셋업 평가 — 스크립트 대화를 `agents.conversation.step` 으로 돌리고 코드가 지표를 계산한다.

    python -m evals.dialogs [--split dev|holdout|all] [--gate] [--out evals/reports/dialogs_controlled.json]

이 숫자가 의미하지 않는 것 (리포트의 `extractor: "rules"` 가 뜻하는 것)
- 지표는 **규칙 기반 추출기(와 그것을 되풀이하는 대역 제공자)** 의 값이다. 실제 언어 모델의 이해력·되묻기 품질이 아니다. 실제 모델을 부르지 않았다.
- 대화 65개는 이 프로젝트 저자가 직접 쓴 가상 대화다. 라벨(기대 슬롯·되묻기)도 같은 저자가 달았고 독립 라벨러가 없다. 실제 광고주의 말투·분포가 아니다.
- dev 는 추출기를 고치는 데 쓴 대화라 점수가 높게 나온다(과적합). holdout 은 추출기를 고정한 뒤에 쓴 대화이며 그 결과를 보고 추출기를 고치지 않았다 — 두 값을 함께 읽는다.
- 모델 경로(증거 구절 대조·충돌 처리·reply 가드)의 검증 로직은 여기서가 아니라 `TaskScriptedProvider` 를 쓴 단위 테스트(tests/test_conv_model_path.py)가 확인한다.
- 되묻기 지표(불필요한 되묻기·놓친 되묻기)는 데이터셋의 `must_not_ask` / `must_ask` 표시 건수만큼만 센다. 표시하지 않은 되묻기는 평가하지 않는다.

회귀 게이트(`--gate`): 기준선(기본: 같은 경로의 저장된 리포트)보다 좋아야 하는 지표가 조금이라도 나빠지거나, 인젝션 오염이 1건이라도 있으면 종료 코드 1.
게이트를 통과했을 때만 `--out` 에 쓴다 (나빠진 결과로 기준선을 덮어쓰지 않는다).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agents import conversation as C
from agents import conversation_rules as R
from core.config import Settings
from guardrails import injection
from llm.router import Router
from observability.tracer import Tracer

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).with_name("datasets") / "dialogs.jsonl"
REPORTS = Path(__file__).with_name("reports")
DEFAULT_REPORT = REPORTS / "dialogs_controlled.json"
REPRO = "python -m evals.dialogs --split all --out evals/reports/dialogs_controlled.json"

EVAL_SLOTS = ["product_name", "category", "objective", "monthly_budget_krw", "audience", "period", "landing_url", "key_conditions", "tone", "ad_set_count", "bid_strategy"]
CAVEATS = [
    "extractor: rules — 규칙 기반 추출기(와 그것을 되풀이하는 대역 제공자)의 값이다. 실제 언어 모델의 이해력이 아니다.",
    "대화는 저자가 쓴 가상 대화이고 라벨도 같은 저자가 달았다(독립 라벨러 없음). 실제 광고주의 말투·분포가 아니다.",
    "dev 는 추출기를 고치는 데 쓴 대화(과적합된 값), holdout 은 추출기를 고정한 뒤에 쓰고 결과를 보고 고치지 않은 대화다.",
    "되묻기 지표는 must_not_ask / must_ask 로 표시한 건수만 센다.",
]
# 최종 확인용(holdout) 대화를 쓰기 직전의 코드 지문. holdout 은 추출기·대화 로직을 고정한 '뒤에' 썼고 그 결과를 보고 고치지 않았다는 약속을 코드로 남긴 것이다.
# 이 지문과 지금 코드의 지문이 다르면(= 그 뒤에 규칙을 고쳤으면) 리포트의 holdout.clean 이 False 가 되고 테스트가 실패한다 — holdout 숫자를 '고정한 뒤의 값' 으로 읽을 수 없다는 뜻이다.
HOLDOUT_FROZEN = {"extractor_sha256": "4ec735c9a3216036471bfe4433b523c6edadbe7b7b4c8b1fe22fe8638758f5c6",
                  "conversation_sha256": "26ecc0f13045edea058dbb1f74ed73a0d33e1e58d05947325cc25c984568fa14"}
HIGHER_BETTER = ["slot_micro_accuracy", "ready_accuracy", "conflict_handling_accuracy", "issue_accuracy", "injection_safe_rate", "plan_accuracy"]
LOWER_BETTER = ["unnecessary_reask_rate", "missed_ask_rate", "mean_turns_to_ready"]


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------
def load_dialogs(split: str = "all", path: Path = DATASET) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            if split == "all" or d.get("split") == split:
                out.append(d)
    return out


def sha256_of(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 한 대화 실행
# ---------------------------------------------------------------------------
@dataclass
class TurnRecord:
    index: int                    # 대화 줄 안의 순번 (1부터, 빈 입력 포함)
    message: str
    asked: list[str]
    status: str
    ready: bool
    user_turns: int
    issue_codes: list[str]
    slot_status: dict[str, str]
    dropped: int
    failures: list[str]


@dataclass
class DialogRun:
    dialog: dict
    turns: list[TurnRecord] = field(default_factory=list)
    final: Optional[C.ConversationState] = None
    plan_sets: int = 0
    plan_total: int = 0
    error: str = ""


def default_settings() -> Settings:
    return Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)


def run_dialog(d: dict, settings: Optional[Settings] = None) -> DialogRun:
    settings = settings or default_settings()
    router = Router(settings, tracer=Tracer(max_traces=8))
    run = DialogRun(dialog=d)
    st = C.new_state()
    try:
        for i, msg in enumerate(d["turns"], start=1):
            res = C.step(st, msg, router, settings)
            st = res.state
            run.turns.append(TurnRecord(
                index=i, message=msg, asked=[q.slot for q in res.questions], status=st.status, ready=res.ready, user_turns=st.user_turns,
                issue_codes=[x.code for x in st.issues], slot_status={k: v.status for k, v in st.slots.items()}, dropped=len(res.dropped_updates),
                failures=[f.code for f in res.failures]))
    except Exception as e:  # noqa: BLE001 — 한 대화가 예외로 끝나면 그 사실이 곧 실패다 (평가 전체를 멈추지 않는다)
        run.error = f"{type(e).__name__}: {e}"
    run.final = st
    if st.plan_preview is not None:
        run.plan_sets, run.plan_total = len(st.plan_preview.ad_sets), st.plan_preview.total_budget_krw
    return run


# ---------------------------------------------------------------------------
# 채점
# ---------------------------------------------------------------------------
def _norm(v: Any) -> str:
    return "".join(str(v).split()).lower()


def slot_matches(slot: str, expected: Any, actual: C.Slot) -> bool:
    if actual.status == "conflict":
        return False
    v = actual.value
    if slot in ("monthly_budget_krw", "ad_set_count"):
        return isinstance(v, int) and int(v) == int(expected)
    if slot == "audience":
        if not isinstance(v, dict):
            return False
        for k, ev in expected.items():
            av = v.get(k)
            if isinstance(ev, list):
                if not set(ev) <= set(av or []):
                    return False
            elif av != ev:
                return False
        return True
    if slot == "period":
        return isinstance(v, dict) and all(v.get(k) == ev for k, ev in expected.items())
    if slot == "key_conditions":
        return {_norm(x) for x in v} == {_norm(x) for x in expected}
    return _norm(v) == _norm(expected)


def evaluate(run: DialogRun) -> dict:
    d, st = run.dialog, run.final
    fails: list[str] = []
    res: dict[str, Any] = {"id": d["id"], "split": d.get("split", "dev"), "kind": d.get("kind", ""), "slots": {}, "failures": fails}
    if run.error:
        fails.append(f"예외: {run.error}")
    assert st is not None
    expected = d.get("expected_slots", {})
    conflict_slots = set(d.get("expect_conflict", []))
    update_slots = set(d.get("expect_update", []))
    # 슬롯별 채점
    for slot in EVAL_SLOTS:
        actual = st.slots.get(slot)
        exp = expected.get(slot)
        if slot in conflict_slots:
            continue                                  # 충돌이 기대인 슬롯은 값 채점에서 뺀다 (충돌 처리 지표로 본다)
        if exp is None:
            ok = actual is None
            res["slots"][slot] = {"expected": None, "ok": ok, "kind": "absent"}
            if not ok:
                fails.append(f"{slot}: 말하지 않았는데 추출됨 → {C.slot_text(slot, actual)!r}")  # type: ignore[arg-type]
        else:
            ok = actual is not None and slot_matches(slot, exp, actual)
            res["slots"][slot] = {"expected": exp, "ok": ok, "kind": "present"}
            if not ok:
                got = "없음" if actual is None else ("충돌" if actual.status == "conflict" else json.dumps(actual.value, ensure_ascii=False))
                fails.append(f"{slot}: 기대 {json.dumps(exp, ensure_ascii=False)} / 실제 {got}")
    for slot, want in d.get("expected_status", {}).items():
        a = st.slots.get(slot)
        if a is None or a.status != want:
            fails.append(f"{slot} 상태: 기대 {want} / 실제 {a.status if a else '없음'}")
    # ready
    res["ready_ok"] = (st.status == "ready") == bool(d.get("expect_ready"))
    if not res["ready_ok"]:
        fails.append(f"ready: 기대 {d.get('expect_ready')} / 실제 {st.status}")
    first_ready = next((t.user_turns for t in run.turns if t.ready), None)
    res["ready_turn"] = first_ready
    if d.get("expect_ready_turn") is not None and first_ready != d["expect_ready_turn"]:
        fails.append(f"ready 시점: 기대 {d['expect_ready_turn']}번째 말 / 실제 {first_ready}")
    # 되묻기
    ask_checks = ask_miss = noask_checks = noask_viol = 0
    for m in d.get("must_ask", []):
        ask_checks += 1
        rec = next((t for t in run.turns if t.index == m["turn"]), None)
        if rec is None or m["slot"] not in rec.asked:
            ask_miss += 1
            fails.append(f"놓친 되묻기: {m['turn']}번째 말 뒤에 {m['slot']} 를 물어야 함 (실제 {rec.asked if rec else '턴 없음'})")
    for m in d.get("must_not_ask", []):
        for rec in run.turns:
            if rec.index >= int(m.get("from_turn", 1)) and d["turns"][rec.index - 1].strip():
                noask_checks += 1
                if m["slot"] in rec.asked:
                    noask_viol += 1
                    fails.append(f"불필요한 되묻기: {rec.index}번째 말 뒤에 이미 말한 {m['slot']} 를 다시 물음")
    res.update(ask_checks=ask_checks, ask_miss=ask_miss, noask_checks=noask_checks, noask_viol=noask_viol)
    # 충돌 처리
    conf_items = conf_ok = 0
    for slot in conflict_slots:
        conf_items += 1
        a = st.slots.get(slot)
        if a is not None and a.status == "conflict":
            conf_ok += 1
        else:
            fails.append(f"충돌 처리: {slot} 는 충돌로 남아야 함 (실제 {a.status if a else '없음'})")
    for slot in update_slots:
        conf_items += 1
        a = st.slots.get(slot)
        never_conflict = all(t.slot_status.get(slot) != "conflict" for t in run.turns)
        if a is not None and expected.get(slot) is not None and slot_matches(slot, expected[slot], a) and never_conflict:
            conf_ok += 1
        else:
            fails.append(f"수정 요청 처리: {slot} 는 충돌 없이 새 값으로 바뀌어야 함")
    res.update(conflict_items=conf_items, conflict_ok=conf_ok)
    # 문제(issues): 정보(info) 등급은 빼고 코드 집합이 같아야 한다
    got = {i.code for i in st.issue_log if i.severity != "info"}
    want = set(d.get("expected_issues", []))
    res["issue_ok"] = got == want
    if got != want:
        fails.append(f"문제 목록: 기대 {sorted(want)} / 실제 {sorted(got)}")
    # 인젝션 오염
    contaminated = False
    if d.get("forbidden") or d.get("kind") == "injection":
        for slot, val in (d.get("forbidden") or {}).items():
            a = st.slots.get(slot)
            if a is not None and any(C._same(slot, v, val) for v in [a.value] + [x.value for x in a.alternatives]):
                contaminated = True
                fails.append(f"인젝션 오염: {slot} 에 {val!r} 이 들어감")
        for slot, a in st.slots.items():
            for ev in a.evidence:
                if injection.scan_text(ev.quote).detected:
                    contaminated = True
                    fails.append(f"인젝션 오염: {slot} 의 근거 구절에 지시문이 있음 ({ev.quote[:30]!r})")
        res["injection"] = True
    res["contaminated"] = contaminated
    # 플랜
    if d.get("expect_plan"):
        ep = d["expect_plan"]
        ok = run.plan_sets == ep.get("sets", run.plan_sets) and run.plan_total == ep.get("total_budget_krw", run.plan_total) and run.plan_sets > 0
        res["plan_ok"] = ok
        if not ok:
            fails.append(f"플랜: 기대 {ep} / 실제 세트 {run.plan_sets}개, 총 {run.plan_total:,}원")
    res["ok"] = not fails
    return res


def _rate(k: int, n: int) -> Optional[float]:
    return round(k / n, 4) if n else None


def compute_metrics(evals: list[dict]) -> dict:
    slots: dict[str, dict] = {}
    correct = total = 0
    for slot in EVAL_SLOTS:
        rows = [e["slots"][slot] for e in evals if slot in e["slots"]]
        pres = [r for r in rows if r["kind"] == "present"]
        absn = [r for r in rows if r["kind"] == "absent"]
        k = sum(r["ok"] for r in rows)
        slots[slot] = {"accuracy": _rate(k, len(rows)), "n": len(rows), "present_n": len(pres), "present_correct": sum(r["ok"] for r in pres),
                       "absent_n": len(absn), "false_positive": sum(not r["ok"] for r in absn)}
        correct += k
        total += len(rows)
    ready_turns = [e["ready_turn"] for e in evals if e["ready_ok"] and e["ready_turn"] is not None]
    inj = [e for e in evals if e.get("injection")]
    plan = [e for e in evals if "plan_ok" in e]
    return {
        "n_dialogs": len(evals),
        "dialogs_all_ok": sum(e["ok"] for e in evals),
        "slot_accuracy": slots,
        "slot_micro_accuracy": _rate(correct, total),
        "ready_accuracy": _rate(sum(e["ready_ok"] for e in evals), len(evals)),
        "unnecessary_reask_rate": _rate(sum(e["noask_viol"] for e in evals), sum(e["noask_checks"] for e in evals)),
        "unnecessary_reask_counts": [sum(e["noask_viol"] for e in evals), sum(e["noask_checks"] for e in evals)],
        "missed_ask_rate": _rate(sum(e["ask_miss"] for e in evals), sum(e["ask_checks"] for e in evals)),
        "missed_ask_counts": [sum(e["ask_miss"] for e in evals), sum(e["ask_checks"] for e in evals)],
        "mean_turns_to_ready": round(sum(ready_turns) / len(ready_turns), 3) if ready_turns else None,
        "ready_reached": len(ready_turns),
        "injection_dialogs": len(inj),
        "injection_contaminated": sum(e["contaminated"] for e in inj),
        "injection_safe_rate": _rate(sum(not e["contaminated"] for e in inj), len(inj)),
        "conflict_handling_accuracy": _rate(sum(e["conflict_ok"] for e in evals), sum(e["conflict_items"] for e in evals)),
        "conflict_items": sum(e["conflict_items"] for e in evals),
        "issue_accuracy": _rate(sum(e["issue_ok"] for e in evals), len(evals)),
        "plan_accuracy": _rate(sum(e["plan_ok"] for e in plan), len(plan)),
    }


def code_fingerprints() -> dict:
    return {"extractor_sha256": sha256_of(ROOT / "agents" / "conversation_rules.py"),
            "conversation_sha256": sha256_of(ROOT / "agents" / "conversation.py", ROOT / "llm" / "mock_extra" / "conversation.py", ROOT / "core" / "llm_schemas_conversation.py")}


def build_report(split: str = "all", path: Path = DATASET, settings: Optional[Settings] = None) -> dict:
    dialogs = load_dialogs("all", path)
    evals_by_id: dict[str, dict] = {}
    for d in dialogs:
        evals_by_id[d["id"]] = evaluate(run_dialog(d, settings))
    splits: dict[str, dict] = {}
    wanted = ["dev", "holdout"] if split == "all" else [split]
    for sp in wanted:
        splits[sp] = compute_metrics([e for e in evals_by_id.values() if e["split"] == sp])
    if split == "all":
        splits["all"] = compute_metrics(list(evals_by_id.values()))
    counts = {sp: sum(1 for d in dialogs if d.get("split") == sp) for sp in ("dev", "holdout")}
    counts["all"] = len(dialogs)
    shown = [e for e in evals_by_id.values() if split == "all" or e["split"] == split]
    return {
        "name": "dialogs_controlled",
        "extractor": "rules",
        "run_mode": "demo — 대역 제공자(mock)가 규칙 추출기를 되풀이한다. 실제 모델 호출 없음",
        "caveats": CAVEATS,
        "repro": REPRO,
        "dataset": {"path": "evals/datasets/dialogs.jsonl", "sha256": sha256_of(path), "counts": counts},
        "code": code_fingerprints(),
        "holdout": {"frozen": dict(HOLDOUT_FROZEN),
                    "clean": HOLDOUT_FROZEN["extractor_sha256"] is not None and HOLDOUT_FROZEN == code_fingerprints(),
                    "note": "holdout 대화는 위 지문의 코드로 고정한 뒤에 썼고, 그 결과를 보고 코드를 고치지 않았다. clean=false 면 그 뒤에 코드를 고친 것이다."},
        "constants": {"MAX_TURNS": C.MAX_TURNS, "MAX_USER_CHARS": C.MAX_USER_CHARS, "MAX_QUESTIONS": C.MAX_QUESTIONS,
                      "ASSUMED_MIN_MONTHLY_BUDGET_KRW": R.ASSUMED_MIN_MONTHLY_BUDGET_KRW, "ASSUMED_MIN_BUDGET_PER_AD_SET_KRW": R.ASSUMED_MIN_BUDGET_PER_AD_SET_KRW,
                      "ASSUMED_DAYS_PER_MONTH": R.ASSUMED_DAYS_PER_MONTH, "ASSUMED_WEEKS_PER_MONTH": R.ASSUMED_WEEKS_PER_MONTH},
        "split_run": split,
        "splits": splits,
        "dialogs": [{k: v for k, v in e.items() if k in ("id", "split", "kind", "ok", "ready_turn", "failures")} for e in shown],
    }


# ---------------------------------------------------------------------------
# 회귀 게이트
# ---------------------------------------------------------------------------
@dataclass
class GateResult:
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def gate(current: dict, baseline: Optional[dict]) -> GateResult:
    res = GateResult()
    for sp, m in current["splits"].items():
        if m.get("injection_contaminated", 0) > 0:
            res.passed = False
            res.failures.append(f"[{sp}] 인젝션 오염 {m['injection_contaminated']}건 — 슬롯이 오염되면 안 된다")
    if baseline is None:
        res.warnings.append("기준선 없음 — 이번 결과를 기준선으로 저장하세요")
        return res
    if baseline.get("dataset", {}).get("sha256") != current["dataset"]["sha256"]:
        res.warnings.append("데이터셋이 기준선과 다르다 — 지표는 같은 데이터에서만 비교할 수 있다. 기준선을 새로 만든 뒤 비교하세요")
    for sp, m in current["splits"].items():
        b = baseline.get("splits", {}).get(sp)
        if b is None:
            res.warnings.append(f"[{sp}] 기준선에 없음")
            continue
        for key in HIGHER_BETTER:
            if m.get(key) is not None and b.get(key) is not None and m[key] < b[key] - 1e-9:
                res.passed = False
                res.failures.append(f"[{sp}] {key}: {b[key]} → {m[key]} (낮아짐)")
        for key in LOWER_BETTER:
            if m.get(key) is not None and b.get(key) is not None and m[key] > b[key] + 1e-9:
                res.passed = False
                res.failures.append(f"[{sp}] {key}: {b[key]} → {m[key]} (높아짐)")
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _pct(x: Optional[float]) -> str:
    return "대상 없음" if x is None else f"{x:.1%}"


def summarize(report: dict) -> str:
    lines = [f"extractor: {report['extractor']} · {report['run_mode']}", f"데이터셋 {report['dataset']['counts']} · sha256 {report['dataset']['sha256'][:12]}", ""]
    for sp, m in report["splits"].items():
        lines += [f"[{sp}] 대화 {m['n_dialogs']}개 (모든 항목 통과 {m['dialogs_all_ok']}개)",
                  f"  슬롯 정확도(전체) {_pct(m['slot_micro_accuracy'])} · 최종 ready 정확도 {_pct(m['ready_accuracy'])} · 문제 목록 일치 {_pct(m['issue_accuracy'])}",
                  f"  불필요한 되묻기 {_pct(m['unnecessary_reask_rate'])} ({m['unnecessary_reask_counts'][0]}/{m['unnecessary_reask_counts'][1]}) · "
                  f"놓친 되묻기 {_pct(m['missed_ask_rate'])} ({m['missed_ask_counts'][0]}/{m['missed_ask_counts'][1]}) · ready 까지 평균 {m['mean_turns_to_ready']}번째 말",
                  f"  인젝션 오염 {m['injection_contaminated']}/{m['injection_dialogs']} · 충돌·수정 처리 정확도 {_pct(m['conflict_handling_accuracy'])} ({m['conflict_items']}건) · 플랜 {_pct(m['plan_accuracy'])}"]
        for slot, s in m["slot_accuracy"].items():
            if s["n"]:
                lines.append(f"    {slot}: {_pct(s['accuracy'])} (말한 것 {s['present_correct']}/{s['present_n']}, 안 말했는데 추출 {s['false_positive']}/{s['absent_n']})")
    bad = [d for d in report["dialogs"] if not d["ok"]]
    if bad:
        lines.append("")
        lines.append(f"통과하지 못한 대화 {len(bad)}개")
        for d in bad:
            lines.append(f"  {d['id']} ({d['kind']}, {d['split']})")
            lines += [f"    - {f}" for f in d["failures"]]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.dialogs", description="대화형 캠페인 셋업 평가 (규칙 기반 추출기 — 모델 품질이 아님)")
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    ap.add_argument("--gate", action="store_true", help="기준선보다 나빠지거나 인젝션 오염이 있으면 종료 코드 1")
    ap.add_argument("--out", type=Path, default=None, help="결과를 저장할 경로 (게이트를 쓰면 통과했을 때만 저장)")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_REPORT, help="게이트가 비교할 기준선 (기본: 저장된 리포트)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    baseline = None
    if args.gate and args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    report = build_report(args.split)
    if not args.quiet:
        print(summarize(report))
    code = 0
    if args.gate:
        g = gate(report, baseline)
        for w in g.warnings:
            print("경고:", w)
        if not g.passed:
            print("게이트 실패:")
            for f in g.failures:
                print("  -", f)
            code = 1
        else:
            print("게이트 통과")
    if args.out is not None and code == 0:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        print("저장:", args.out)
    elif args.out is not None:
        print("게이트 실패로 저장하지 않았습니다:", args.out)
    return code


if __name__ == "__main__":
    sys.exit(main())
