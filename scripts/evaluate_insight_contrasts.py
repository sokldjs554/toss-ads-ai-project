"""Run frozen contrast inputs without provider calls; preserve every denominator."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path

from agents import insights
from core import insight_engine as ie
from core.performance import ScenarioSpec, generate
from evals.insights_eval import engine_fingerprint


def evaluate(path: Path) -> dict:
    source = json.loads(path.read_text())
    cases, summary = [], {}
    for case in source["cases"]:
        rows = generate(ScenarioSpec.from_dict(case["spec"]))
        analysis = ie.analyze(rows)
        alerted = any(i.start <= analysis.window[1] and i.end >= analysis.window[0] for i in analysis.incidents)
        expected = case["expect_recent_event"]
        outcome = ("tp" if alerted else "fn") if expected else ("fp" if alerted else "tn")
        group = summary.setdefault(case["family"], dict(n=0, tp=0, fn=0, fp=0, tn=0, withheld=0, audit_problems=0))
        group["n"] += 1
        group[outcome] += 1
        group["withheld"] += analysis.status != "ok"
        report = insights.build_report(rows, router=None, analysis=analysis, data_origin="synthetic")
        problems = insights.audit_report(report)
        group["audit_problems"] += len(problems)
        cases.append({"id": case["id"], "family": case["family"], "expected": expected, "outcome": outcome,
                      "status": analysis.status, "reference_window": [d.isoformat() for d in analysis.baseline],
                      "incidents": [{"start": i.start.isoformat(), "end": i.end.isoformat(), "polarity": i.polarity,
                                     "findings": [{"metric": m, "direction": s.direction, "pct": s.pct, "score": s.score, "segment": s.segment, "method": getattr(s, "method", "reference_forecast")} for m, s in i.findings]} for i in analysis.incidents],
                      "actions": [a.action_id for a in analysis.actions], "audit_problems": problems})
    return {"input": str(path), "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "engine_fingerprint": engine_fingerprint(), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "python": platform.python_version(), "execution": "deterministic synthetic; no provider calls; not human-reviewed truth",
            "summary": summary, "cases": cases}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    result = evaluate(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"], ensure_ascii=False))
