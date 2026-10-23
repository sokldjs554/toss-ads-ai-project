"""Frozen synthetic policy check; DEMO and injected outages are NOT LLM quality."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import fields
from pathlib import Path

from agents.recheck import recheck
from core.config import Settings
from evals.compare import run_code_only
from evals.runner import EvalCase
from guardrails import output_guard
from guardrails.policy import get_catalog
from llm.router import Router
from llm.mock_provider import MockProvider
from observability.tracer import Tracer

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "evals/datasets/policy_final_20261023.json"
OUT = ROOT / "evals/reports/strengthening-20261023/policy-final.json"


def main():
    source = json.loads(SOURCE.read_text())
    catalog = get_catalog()
    settings = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    rows = []
    review = []
    for raw in source["cases"]:
        case = EvalCase(**{k:v for k,v in raw.items() if k in {f.name for f in fields(EvalCase)}})
        copy, product = case.to_copy(), case.to_product()
        tracer = Tracer()
        result = recheck(copy, product, set(), settings, tracer=tracer)
        det = output_guard.check(copy, product, catalog.categories_for(product, copy.full_text()), "", [], catalog)
        edit = copy.model_copy(update={"headline":raw["proposed_edit"], "body":"", "cta":"", "claims":[]})
        edited = recheck(edit, product, set(), settings, tracer=tracer)
        live = Settings.from_env(openai_api_key="offline-unavailable", anthropic_api_key="", force_demo=False)
        failure = recheck(copy, product, set(), live, router=Router(live, providers={"mock":MockProvider()}, tracer=tracer), tracer=tracer)
        quotes = [{"rule":f.rule_id, "quote":f.quote, "exists": bool(f.quote and f.quote in copy.full_text()), "verified":f.verified_quote} for f in det.findings]
        rows.append({"id":case.id, "family":raw["family"], "label":case.label, "rules_only":run_code_only(case,catalog).predicted,
                     "demo_combined":result.report.final_decision.value, "outage_final":failure.report.final_decision.value,
                     "outage_failed":failure.failed,"outage_reusable":failure.reusable,"outage_successful_calls":failure.calls,
                     "fixed_edit_final":edited.report.final_decision.value,"evidence":quotes})
        review.append({"id":case.id,"original":copy.full_text(),"facts":json.dumps(raw.get("facts",[]),ensure_ascii=False),
                       "policy_basis":raw["basis"],"draft_label":case.label,"reviewer_id":"","human_label":"","evidence_quote":"",
                       "applicable_rule":"","disagreement_reason":"","reviewed_at":""})
    def summarize(key):
        bad=[r for r in rows if r["label"] in ("REJECT","REVISE")]; good=[r for r in rows if r["label"]=="APPROVE"]
        return {"n":len(rows),"unsafe_pass":{"k":sum(r[key]=="APPROVE" for r in bad),"n":len(bad)},
                "unnecessary_reject":{"k":sum(r[key]=="REJECT" for r in good),"n":len(good)},
                "unnecessary_revision":{"k":sum(r[key]=="REVISE" for r in good),"n":len(good)},
                "hold":{"k":sum(r[key]=="HUMAN_REVIEW" for r in rows),"n":len(rows)},
                "exact_draft_label":{"k":sum(r[key]==r["label"] for r in rows),"n":len(rows)}}
    report={"input_sha256":hashlib.sha256(SOURCE.read_bytes()).hexdigest(),"policy":catalog.version,
            "execution":"No model calls; deterministic rules, DEMO reviewer, injected unavailable provider. Human gold validation pending.",
            "rules_only":summarize("rules_only"),"demo_combined":summarize("demo_combined"),
            "fixed_edit_violations":{"k":sum(r["fixed_edit_final"]!="APPROVE" for r in rows),"n":len(rows),"note":"Authored neutral edits, not model rewrites"},
            "outage":{"held":sum(r["outage_final"]=="HUMAN_REVIEW" for r in rows),"blocked":sum(r["outage_final"]=="REJECT" for r in rows),
                      "unsafe_approve":sum(r["outage_final"]=="APPROVE" for r in rows),"successful_calls":sum(r["outage_successful_calls"] for r in rows),"n":len(rows)},
            "evidence":{"quoted":sum(bool(e["quote"]) for r in rows for e in r["evidence"]),
                        "quote_missing_in_source":sum(bool(e["quote"]) and not e["exists"] for r in rows for e in r["evidence"]),
                        "semantic_validity":"pending human review; existence is not entailment"},"rows":rows}
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    with (OUT.parent/"policy-human-review.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(review[0]));writer.writeheader();writer.writerows(review)
    print(json.dumps({k:v for k,v in report.items() if k!="rows"},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
