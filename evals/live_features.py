"""새 기능의 실제 모델 평가. 키는 실행 환경에서만 읽고 공개 합성 입력만 보낸다.

실행 전 입력 파일과 소스 해시를 manifest로 고정한다. 대역 제공자는 구성하지
않으며 실패한 호출·템플릿 대체·위치에 따른 품질 판단 변화를 그대로 남긴다.
python -m evals.live_features --request evals/live_request_20261020.json --validate-only
python -m evals.live_features --request evals/live_request_20261020.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.config import Settings
from evals.live_guard import APPROVED_MODELS, BoundedProvider, LiveLedger
from llm.errors import BudgetExceeded
from llm.router import Router
from observability.tracer import Tracer

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "evals/reports/live_features"
OPENAI_MODEL = "gpt-5.4-mini-2026-03-17"
ANTHROPIC_MODEL = "claude-sonnet-5-5"
GROUPS = ("vision", "conversation", "insights", "quality", "pipeline")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_fingerprints() -> dict:
    paths = []
    for directory in ("agents", "core", "guardrails", "llm", "observability", "evals"):
        paths.extend((ROOT / directory).rglob("*.py"))
    paths.append(ROOT / "guardrails/policy.yaml")
    paths.extend([
        ROOT / "scripts/claim_live_evaluation.py", ROOT / ".github/workflows/live-features.yml",
        ROOT / "scripts/claim_remaining_evaluation.py", ROOT / ".github/workflows/live-remaining.yml",
    ])
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(paths)}


def safe_path(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("evaluation input must be an existing repository file")
    return path


def validate_request(request: dict) -> dict:
    if request.get("version") != 1 or not re.fullmatch(r"[a-z0-9_-]{1,64}", request.get("run_id", "")):
        raise ValueError("invalid evaluation manifest version/run_id")
    LiveLedger(float(request["cost_cap_usd"]), request["max_calls"])
    if request.get("groups") != list(GROUPS):
        raise ValueError("all frozen feature groups are required")
    if request.get("models") != {"openai": OPENAI_MODEL, "anthropic": ANTHROPIC_MODEL}:
        raise ValueError("model contract differs from approved manifest")
    sources = source_fingerprints()
    if request.get("source_sha256") != sources:
        raise ValueError("source differs from the frozen evaluation manifest")
    if request.get("budget_scope") == "remaining-20261021":
        if request.get("prior_reservation_ledger_sha256") != digest(ROOT / "evals/reports/live_features/reservations.json"):
            raise ValueError("prior reservation ledger differs from the frozen request")
    inputs = request.get("input_sha256", {})
    required = {request["dialogs_path"], request["vision_manifest"], "evals/datasets/quality_pairs.jsonl",
                request.get("insights_path", "evals/datasets/perf_scenarios.jsonl")}
    if request.get("rewrite_fixture_path"):
        required.add(request["rewrite_fixture_path"])
    if request.get("followup_dialogs_path"):
        required.add(request["followup_dialogs_path"])
    if not required <= set(inputs):
        raise ValueError("evaluation input manifest is incomplete")
    for path, expected in inputs.items():
        if digest(safe_path(path)) != expected:
            raise ValueError(f"evaluation input changed: {path}")
    vision = json.loads(safe_path(request["vision_manifest"]).read_text())
    for case in vision["cases"]:
        if case["image"] not in inputs or digest(safe_path(case["image"])) != case["sha256"]:
            raise ValueError("vision image differs from its pre-call label")
    from evals.dialogs import load_dialogs
    from evals.insights_eval import load_scenarios
    from evals.quality_signals import load_pairs
    dialogs = load_dialogs(path=safe_path(request["dialogs_path"]))
    selected = request.get("insight_ids")
    if (not isinstance(selected, list) or not selected or any(not isinstance(value, str) for value in selected)
            or len(selected) != len(set(selected))):
        raise ValueError("insight selection must contain unique scenario IDs")
    scenarios = load_scenarios(path=safe_path(request.get("insights_path", "evals/datasets/perf_scenarios.jsonl")))
    if not set(selected) <= {s.id for s in scenarios}:
        raise ValueError("insight selection contains missing scenarios")
    if (len({d["id"] for d in dialogs}) != len(dialogs)
            or len({v["id"] for v in vision["cases"]}) != len(vision["cases"])):
        raise ValueError("duplicate evaluation case IDs")
    actual = {"vision": len(vision["cases"]), "conversation": len(dialogs), "insights": len(selected),
              "quality": len(load_pairs(split="holdout")), "pipeline": 4}
    planned = request.get("planned_cases")
    if (not isinstance(planned, dict) or set(planned) != set(GROUPS)
            or any(type(n) is not int or n <= 0 for n in planned.values()) or planned != actual):
        raise ValueError("planned cases differ from the frozen evaluation inputs")
    supplemental = request.get("planned_supplemental_cases", {})
    enabled = {name for name, field in (("rewrite", "rewrite_fixture_path"), ("conversation_followup", "followup_dialogs_path"))
               if request.get(field)}
    if (not isinstance(supplemental, dict) or set(supplemental) != enabled
            or any(type(n) is not int or n <= 0 for n in supplemental.values())):
        raise ValueError("supplemental plans and input paths must agree")
    if request.get("rewrite_fixture_path"):
        from evals.live_rewrite import load_cases
        cases = load_cases(safe_path(request["rewrite_fixture_path"]))
        if request.get("planned_supplemental_cases", {}).get("rewrite") != len(cases):
            raise ValueError("supplemental rewrite planned count differs from frozen fixtures")
    if request.get("followup_dialogs_path"):
        cases = load_dialogs(path=safe_path(request["followup_dialogs_path"]))
        if (supplemental.get("conversation_followup") != len(cases)
                or len({c["id"] for c in cases}) != len(cases)):
            raise ValueError("supplemental conversation count differs from frozen fixtures")
    return request


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    pending.replace(path)


def case_summary(rows: list[dict]) -> dict:
    return {
        "attempted_cases": len(rows), "completed_cases": sum(r.get("execution") == "completed" for r in rows),
        "all_checks_passed": sum(r.get("execution") == "completed" and bool(r.get("checks")) and all(r["checks"].values()) for r in rows),
        "cases_with_real_calls": sum(r.get("successful_calls", 0) > 0 for r in rows),
        "failed_case_ids": [r["id"] for r in rows if r.get("execution") != "completed" or not all(r.get("checks", {}).values())],
    }


def quality_audit_summary(rows: list[dict]) -> dict:
    """기존 raw 지표와 근거 검사를 통과해 채택한 비교를 별도로 센다."""
    by_status = {}
    for row in rows:
        status = row.get("aggregation_status", "incomplete")
        by_status[status] = by_status.get(status, 0) + 1
    accepted = [r for r in rows if r.get("execution") == "completed" and r.get("aggregation_status") == "consensus"
                and r.get("accepted_winner") in {"a", "b", "tie"}]
    return {"attempted_cases": len(rows), "accepted_cases": len(accepted), "abstained_cases": len(rows) - len(accepted),
            "accepted_author_matches": sum(r["accepted_winner"] == r.get("expected") for r in accepted),
            "raw_stable_author_matches": sum(r.get("execution") == "completed" and bool(r.get("checks")) and all(r["checks"].values()) for r in rows),
            "by_status": by_status}


class FeatureRun:
    def __init__(self, request: dict, output: Path):
        self.request, self.output = request, output
        self.report = {
            "run_id": request["run_id"], "schema_version": 1, "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "source_sha": os.environ.get("GITHUB_SHA") or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "runtime": {"python": sys.version.split()[0], **{name: importlib.metadata.version(name) for name in ("openai", "anthropic", "pydantic")}},
            "manifest": request, "results": {group: [] for group in GROUPS}, "summaries": {},
            "limitations": [
                "입력과 정답은 AI 작성 합성 데이터다. 실제 광고주의 데이터/성과/승인율이 아니다.",
                "품질 선호 라벨은 기존 작성자 판단이며 독립적인 사람의 정답이 아니다. 모델과의 일치는 CTR/전환율 검증이 아니다.",
                "비전 평가는 고정한 글자 배너 12장 범위다. 실제 사진, 로고, 작은 글자 등 모든 광고 이미지의 정확도를 나타내지 않는다.",
                "안전하게 템플릿으로 바뀐 보고서도 모델 문장화 성공과 구분한다. 재시도와 제공자 변경을 호출 장부에 포함한다.",
            ],
        }
        self.ledger = LiveLedger(float(request["cost_cap_usd"]), request["max_calls"], checkpoint=self.checkpoint)
        # 환경값 전체나 Settings 객체를 기록/출력하지 않는다.
        self.settings = Settings.from_env(force_demo=False, openai_model=OPENAI_MODEL, anthropic_model=ANTHROPIC_MODEL,
                                          max_retries=0, request_timeout_s=60.0, run_budget_usd=float(request["cost_cap_usd"]))
        if not (self.settings.has_openai or self.settings.has_anthropic):
            raise ValueError("GitHub Secrets has no usable provider key")
        self.providers = {}
        if self.settings.has_openai:
            from llm.openai_provider import OpenAIProvider
            self.providers["openai"] = BoundedProvider(OpenAIProvider(self.settings.openai_api_key, OPENAI_MODEL, 60, 0, reasoning_effort="none"), self.ledger)
        if self.settings.has_anthropic:
            from llm.anthropic_provider import AnthropicProvider
            self.providers["anthropic"] = BoundedProvider(AnthropicProvider(self.settings.anthropic_api_key, ANTHROPIC_MODEL, 60, 0, effort="low"), self.ledger)
        self.primary = "openai" if "openai" in self.providers else "anthropic"
        self.report["configured_providers"] = list(self.providers)
        self.report["primary_provider"] = self.primary
        self.checkpoint(self.ledger.summary())

    def checkpoint(self, ledger: dict) -> None:
        self.report["ledger"] = ledger
        self.report["summaries"] = {group: case_summary(rows) for group, rows in self.report["results"].items()}
        quality_rows = self.report["results"].get("quality", [])
        if any("aggregation_status" in row for row in quality_rows):
            self.report["quality_audit_summary"] = quality_audit_summary(quality_rows)
        if "supplemental_results" in self.report:
            self.report["supplemental_summaries"] = {group: case_summary(rows) for group, rows in self.report["supplemental_results"].items()}
        write_json(self.output, self.report)

    def router(self, *, both: bool = False) -> Router:
        providers = self.providers if both else {self.primary: self.providers[self.primary]}
        r = Router(self.settings, providers=dict(providers), tracer=Tracer(max_traces=4))
        # 파이프라인의 new_run은 전체 ledger를 초기화하지 않는다. 제공자 wrapper가 장부를 공유한다.
        r.budget.max_calls = self.request["max_calls"]
        return r

    def case(self, group: str, case_id: str, fn: Callable[[], dict], *, supplemental: bool = False) -> None:
        start = len(self.ledger.calls)
        try:
            row = fn()
            row.setdefault("execution", "completed")
        except BudgetExceeded:
            row = {"execution": "budget_stopped", "checks": {}, "error_type": "BudgetExceeded"}
        except Exception as exc:
            # 메시지에는 SDK/서버가 반환한 민감 정보가 섞일 수 있다.
            row = {"execution": "failed", "checks": {}, "error_type": type(exc).__name__}
        calls = self.ledger.calls[start:]
        row.update(id=case_id, attempted_calls=len(calls), successful_calls=sum(c["status"] == "success" for c in calls),
                   call_ordinals=[c["ordinal"] for c in calls], models=sorted({c.get("returned_model", c["requested_model"]) for c in calls}))
        collection = self.report.setdefault("supplemental_results", {}).setdefault(group, []) if supplemental else self.report["results"][group]
        collection.append(row)
        if row.get("budget_stopped"):
            self.ledger.stopped = True
        self.checkpoint(self.ledger.summary())
        print(f"{group}/{case_id}: {row['execution']}; checks {sum(bool(v) for v in row.get('checks', {}).values())}/{len(row.get('checks', {}))}; calls {len(calls)}", flush=True)
        last = self.ledger.calls[-3:]
        if len(last) == 3 and all(c["status"] != "success" for c in last):
            self.ledger.stopped = True
        if self.ledger.stopped:
            raise BudgetExceeded("budget/call contract stopped the evaluation")

    def vision(self) -> None:
        from agents.vision import review_image
        from core.images import ImageRejection, load_image
        from core.schemas import Decision, Fact
        manifest = json.loads(safe_path(self.request["vision_manifest"]).read_text())
        for item in manifest["cases"]:
            def measure(item=item):
                asset = load_image(safe_path(item["image"]).read_bytes(), Path(item["image"]).name)
                if isinstance(asset, ImageRejection):
                    raise ValueError("frozen image could not be loaded")
                rv = review_image(asset, self.router(), self.settings, declared_text=item["declared_text"],
                                  facts=[Fact(**f) for f in item["facts"]], categories=item["categories"])
                norm = lambda value: "".join(ch for ch in value.casefold() if ch.isalnum())
                read = norm(" ".join(s.text for s in rv.segments))
                # 읽기 실패 시 확인용으로 돌려준 사용자 입력을 실제 이미지 읽기로 세지 않는다.
                from_pixels = rv.text_source == "model" and not rv.unreadable
                recovered = [from_pixels and norm(text) in read for text in item["visible_text"]]
                decision_ok = rv.decision != Decision.APPROVE if item["must_not_approve"] else rv.decision == Decision.APPROVE
                return {"checks": {"real_response": rv.provider in self.providers,
                                   "visible_text_recovered": all(recovered), "workflow_decision": decision_ok},
                        "kind": item["kind"], "recovered_lines": recovered, "decision": rv.decision.value,
                        "text_source": rv.text_source, "cross_check": rv.cross_check,
                        "unreadable": rv.unreadable, "read_segments": [asdict(s) for s in rv.segments],
                        "rules": [f.rule_id for f in rv.findings], "source_rules": [getattr(f, "source_rule_id", "") for f in rv.findings],
                        "provider": rv.provider, "model": rv.model, "error_type": rv.error}
            self.case("vision", item["id"], measure)

    def conversation(self, *, supplemental: bool = False) -> None:
        from evals.dialogs import evaluate, load_dialogs, run_dialog
        path_key = "followup_dialogs_path" if supplemental else "dialogs_path"
        for dialog in load_dialogs(path=safe_path(self.request[path_key])):
            def measure(dialog=dialog):
                run = run_dialog(dialog, settings=self.settings, router=self.router())
                # 원래 평가기의 예외 문자열에 SDK 상세가 들어갈 수 있어 타입만 보존한다.
                if run.error:
                    run.error = run.error.split(":", 1)[0]
                scored = evaluate(run)
                result = {"checks": {"scenario": scored["ok"], "no_contamination": not scored["contaminated"],
                                   "no_exception": not run.error,
                                   "no_model_call_failure": not any(code in {"FM-01", "FM-05", "FM-06", "FM-07", "FM-20"} for t in run.turns for code in t.failures)},
                        "score": scored, "turns": [asdict(t) for t in run.turns],
                        "model_path": {"rejected_proposals": sum(t.dropped for t in run.turns),
                                       "rule_model_disagreement_turns": sum("FM-26" in t.failures for t in run.turns),
                                       "reply_fallback_turns": sum("FM-28" in t.failures for t in run.turns)},
                        "slots": {key: slot.model_dump(mode="json") for key, slot in run.final.slots.items()},
                        "status": run.final.status, "plan_total": run.plan_total, "plan_sets": run.plan_sets,
                        "plan": run.final.plan_preview.model_dump(mode="json") if run.final.plan_preview else None,
                        "plan_notes": list(run.final.plan_notes)}
                if supplemental:
                    from agents import conversation as C, conversation_rules as R
                    expected = dialog["expected_slots"]["audience"]
                    plan = run.final.plan_preview
                    bounds = []
                    if plan:
                        for ad_set in plan.ad_sets:
                            ages = [v.value for v in R._extract_audience(ad_set.audience, [])
                                    if v.value.get("age_min") is not None or v.value.get("age_max") is not None]
                            bounds.append(bool(ages) and all(
                                a.get("age_min") is not None and a["age_min"] >= expected["age_min"]
                                and a.get("age_max") is not None and a["age_max"] <= expected["age_max"] for a in ages))
                    handoff = C.brief_for_workbench(run.final) if run.final.status == "ready" else None
                    result.update(original_case_id=dialog["original_case_id"], handoff=handoff)
                    result["checks"].update(preview_preserves_confirmed_age=bool(bounds) and all(bounds),
                                            handoff_ready=handoff is not None)
                return result
            self.case("conversation_followup" if supplemental else "conversation", dialog["id"], measure, supplemental=supplemental)

    def insights(self) -> None:
        from agents import insights
        from core.performance import generate
        from evals.insights_eval import load_scenarios
        selected = set(self.request["insight_ids"])
        path = safe_path(self.request.get("insights_path", "evals/datasets/perf_scenarios.jsonl"))
        scenarios = [s for s in load_scenarios(path=path) if s.id in selected]
        if len(scenarios) != len(selected):
            raise ValueError("frozen insight sample is incomplete")
        for scenario in scenarios:
            def measure(scenario=scenario):
                report = insights.build_report(generate(scenario.spec), router=self.router(), settings=self.settings, data_origin="synthetic")
                problems = insights.audit_report(report)
                return {"checks": {"real_model_narrative": report.narrative_source == "model" and not report.mock_used,
                                   "no_unsupported_numbers": report.unsupported_numbers == 0,
                                   "final_report_audit": not problems},
                        "narrative_source": report.narrative_source, "fallback_reason": report.fallback_reason,
                        "dropped_sentences": len(report.dropped_sentences), "audit_problems": problems, "report": report.to_dict()}
            self.case("insights", scenario.id, measure)

    def quality(self) -> None:
        from core.creative_quality import compare_cards, quality_card
        from evals.quality_judge import compare
        from evals.quality_signals import load_pairs
        for pair in load_pairs(split="holdout"):
            def measure(pair=pair):
                judged = compare(pair.copy("a"), pair.copy("b"), list(pair.facts), self.router(), label=f"quality:{pair.id}")
                consistent = judged["position_consistent"]
                # 과거 보고서와 비교 가능한 raw 선택 지표. 채택 여부와 분리한다.
                winner = (judged["orders"][0]["winner"] if consistent is True else "unstable" if consistent is False else "unavailable")
                cards = [quality_card(pair.copy(s), list(pair.facts), set(pair.categories)) for s in ("a", "b")]
                signal = compare_cards(*cards).winner
                return {"checks": {"position_consistent": consistent is True, "agrees_with_author_label": winner == pair.better},
                        "expected": pair.better, "model_winner": winner, "signal_winner": signal,
                        "signal_model_agreement": winner == signal, "orders": judged["orders"], "reason_code": pair.reason_code,
                        "accepted_winner": judged["winner"], "aggregation_status": judged["status"], "aggregation_reason": judged["reason"],
                        "rubric_version": judged["rubric_version"], "inconsistent_criteria": judged["inconsistent_criteria"],
                        "evidence_basis": judged["evidence_basis"], "budget_stopped": judged["budget_stopped"],
                        "quality_checks": {"evidence_valid": judged["evidence_valid"] is True,
                                           "dimension_consistent": judged["dimension_consistent"] is True,
                                           "consensus_accepted": judged["status"] == "consensus",
                                           "accepted_author_agreement": judged["winner"] == pair.better},
                        "execution": "budget_stopped" if judged["budget_stopped"] else "failed" if judged["status"] == "incomplete" else "completed"}
            self.case("quality", pair.id, measure)

    def pipeline(self) -> None:
        from agents.pipeline import RunOptions, run_pipeline
        from core.schemas import CampaignBrief, Decision, ProductFacts
        from guardrails import output_guard
        # 전체 경로가 연결되고 계산/근거/실패 상태가 보존되는지 본다. 생성 광고의 성공률/CTR 정답은 없다.
        cases = [
            ("P01", "카페 소담", "other", "카페 신메뉴를 알리고 싶어요. 월 120만원, 매장 방문이 목표입니다.", ["밤 라테 5,500원", "매일 오전 9시부터 오후 8시까지 영업"]),
            ("P02", "두빛 영어", "education", "직장인 영어 강의 수강 신청을 늘리고 싶어요. 월 200만원입니다.", ["총 8주 온라인 수업", "주 2회, 회당 30분", "샘플 강의 무료 공개"]),
            ("P03", "소담 머그", "ecommerce", "쇼핑몰 구매 전환, 월 150만원. 30대 대상입니다.", ["300ml 도자기 머그컵", "판매가 18,000원", "5만원 이상 구매 시 무료 배송"]),
            ("P04", "가상 예금", "savings", "30대 직장인 대상 예금 가입 신청 광고입니다. 월 300만원입니다.", ["예시 상품의 기본 연 금리 2.5%", "우대 조건 충족 시 최고 연 3.1%", "만기 12개월", "예시 상품이며 실제 가입을 받지 않습니다"]),
        ]
        for case_id, name, category, prompt, facts in cases:
            def measure(name=name, category=category, prompt=prompt, facts=facts):
                product = ProductFacts(name=name, category=category, facts=[{"id": f"f{i+1}", "text": f} for i, f in enumerate(facts)])
                brief = CampaignBrief(advertiser_input=prompt, product=product)
                router = self.router(both=True)
                run = run_pipeline(brief, self.settings, router=router, tracer=router.tracer,
                                   options=RunOptions(n_variants=2, enable_audience=False, enable_second_opinion=True))
                plan_ok = run.plan is not None and sum(s.budget_krw for s in run.plan.ad_sets) == run.plan.total_budget_krw
                unsafe_pass = [r.variant_id for r in run.reports if r.final_decision == Decision.APPROVE
                               and output_guard.check(r.ad_copy, product, set(run.categories)).decision in (Decision.REVISE, Decision.REJECT)]
                return {"checks": {"not_halted": not run.halted, "budget_sum": plan_ok,
                                   "two_variants_reviewed": len(run.reports) == 2, "no_code_block_approved": not unsafe_pass,
                                   "no_failed_stages": not any(f.code in {"FM-05", "FM-06", "FM-07", "FM-20"} for f in run.failures)},
                        "halted": run.halted, "failures": [f.code for f in run.failures],
                        "plan": run.plan.model_dump(mode="json") if run.plan else None,
                        "ads": [{"copy": r.ad_copy.model_dump(mode="json"), "decision": r.final_decision.value,
                                 "rewrite_status": r.rewrite_status.value,
                                 "verified_rewrite": r.verified_rewrite.model_dump(mode="json") if r.verified_rewrite else None,
                                 "rule_ids": [f.rule_id for f in r.verdict.findings]} for r in run.reports]}
            self.case("pipeline", case_id, measure)

    def run(self) -> dict:
        try:
            for group in GROUPS:
                getattr(self, group)()
            if self.request.get("followup_dialogs_path"):
                self.conversation(supplemental=True)
            if self.request.get("rewrite_fixture_path"):
                from evals.live_rewrite import run_supplement
                run_supplement(self)
            mismatches = []
            for planned_key, results_key in (("planned_cases", "results"), ("planned_supplemental_cases", "supplemental_results")):
                for group, count in self.request.get(planned_key, {}).items():
                    rows = self.report.get(results_key, {}).get(group, [])
                    if len(rows) != count or len({row["id"] for row in rows}) != len(rows):
                        mismatches.append({"group": group, "planned": count, "recorded": len(rows)})
            if mismatches:
                self.report.update(status="incomplete_case_count", case_count_mismatches=mismatches)
            else:
                self.report["status"] = "completed"
        except BudgetExceeded:
            self.report["status"] = "incomplete_budget_or_provider_failure"
        except BaseException as exc:
            self.report.update(status="interrupted", error_type=type(exc).__name__)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.checkpoint(self.ledger.summary())
        return self.report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    request = validate_request(json.loads(args.request.read_text(encoding="utf-8")))
    if args.validate_only:
        print(f"manifest verified: {request['run_id']}; cap ${request['cost_cap_usd']}; calls <= {request['max_calls']}; no API request")
        return 0
    output = REPORT_DIR / f"{request['run_id']}.json"
    if output.exists():
        raise ValueError("refusing to overwrite an existing live measurement; freeze a new run_id")
    report = FeatureRun(request, output).run()
    summary = {"status": report["status"], "summaries": report["summaries"],
               "usage_cost_usd": round(report["ledger"]["usage_cost_usd"], 6), "accounted_usd": round(report["ledger"]["accounted_usd"], 6)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # 품질 선호는 결과를 보고하는 지표다. 호출/그룹 누락을 성공으로 취급하지 않는다.
    summaries = [*report["summaries"].values(), *report.get("supplemental_summaries", {}).values()]
    return 0 if report["status"] == "completed" and all(s["completed_cases"] == s["attempted_cases"] for s in summaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
