"""CLI: python -m evals.run [--dataset golden] [--no-llm] [--gate] [--save-baseline] [--limit N] [--redteam] [--harvest]"""
from __future__ import annotations

import argparse
import sys

from core.config import Settings

from .regression import gate
from .runner import load_report, run_eval, save_report


def _print_summary(report: dict) -> None:
    s = report["summary"]
    print(f"\n== {report['dataset']} | n={report['n']} | mode={report['mode']} | reviewer={report['reviewer']} | policy {report['policy_version']} / prompt {report['prompt_version']}")
    for key in ("coverage", "accuracy_decided", "catch_rate_decided", "unsafe_approve", "false_flag", "human_review"):
        m = s[key]
        print(f"  {key:22s} {m['k']:3d}/{m['n']:<3d} = {m['rate'] if m['rate'] is not None else '-'}  wilson95={m['wilson95']}")
    cal = s["calibration"]
    print(f"  ECE={cal['ece']}  Brier={cal['brier']}  high-confidence error={cal['high_confidence_error']['rate']} "
          f"({cal['high_confidence_error']['k']}/{cal['high_confidence_error']['n']})  confidently wrong: {cal['confidently_wrong']}")
    print(f"  recommended τ for 0% unsafe approve: {report['recommended_threshold_for_zero_unsafe']}")
    print("  by difficulty:", {k: (v['unsafe_approve']['rate'], v['false_flag']['rate'], v['human_review']['rate']) for k, v in s['by_difficulty'].items()})
    bad = [c for c in report["cases"] if c["predicted"] != c["label"]]
    if bad:
        print(f"  mismatches ({len(bad)}):")
        for c in bad[:40]:
            print(f"    {c['id']:14s} label={c['label']:8s} pred={c['predicted']:12s} conf={c['confidence']:.2f} rules={c['predicted_rules']} expected={c['label_rules']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="golden")
    ap.add_argument("--no-llm", action="store_true", help="결정론 가드레일만 (네트워크 없음)")
    ap.add_argument("--gate", action="store_true", help="기준선과 비교해 회귀 게이트 실행")
    ap.add_argument("--baseline", default=None, help="기준선 파일명 (기본: baseline_<mode>.json)")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--redteam", action="store_true")
    ap.add_argument("--harvest", action="store_true", help="레드팀 돌파 사례를 adversarial.jsonl 에 추가")
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    if args.redteam:
        from .redteam import harvest, run_redteam

        rep = run_redteam(settings, limit_seeds=args.limit)
        save_report(rep, "redteam_latest.json")
        print(f"redteam: mode={rep['mode']} seeds={rep['seeds']} attacks={len(rep['attacks'])} evasion_rate={rep['evasion_rate']} cost=${rep['cost_usd']}")
        for s, m in rep["by_strategy"].items():
            print(f"  {s:12s} n={m['n']} evaded={m['evaded']} human_review={m['human_review']} rate={m['evasion_rate']}")
        if args.harvest:
            print("harvested:", harvest(rep))
        return 0
    report = run_eval(args.dataset, settings, use_llm=not args.no_llm, limit=args.limit)
    mode_tag = report["mode"]
    save_report(report, "latest.json")
    _print_summary(report)
    if args.save_baseline:
        save_report(report, args.baseline or f"baseline_{mode_tag}.json")
        print("baseline saved")
    if args.gate:
        base = load_report(args.baseline or f"baseline_{mode_tag}.json")
        g = gate(report, base)
        print("\nGATE:", "PASS" if g.passed else "FAIL", g.deltas)
        for f in g.failures:
            print("  FAIL", f)
        for w in g.warnings:
            print("  warn", w)
        return 0 if g.passed else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
