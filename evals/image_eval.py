"""이미지 소재 평가 — 코드가 정하는 부분(받아들이기·SVG 정화·글자 대비·배너 넘침·이미지 속 글 규칙·판정)을 합성 사례로 잰다.

    python -m evals.image_eval [--split dev|holdout|all] [--gate] [--json] [--save 경로]

사례는 evals/datasets/image_cases.jsonl (dev / holdout). 픽스처 이미지는 evals/datasets/images/make_fixtures.py 가 코드로 만든다.
'이미지 속 글' 사례는 모델이 읽은 결과를 통제된 가짜 제공자로 주입한다 — 코드가 그 결과를 어떻게 다루는지(대조·규칙 적용·판정)를 재는 것이다.

이 숫자가 의미하지 않는 것
  · **실제 비전 모델의 정확도는 측정하지 않았다.** 사례의 '모델이 읽은 글' 은 사례를 쓴 사람이 정한 값이다. 실제 모델이 글자를 빠뜨리거나 잘못 읽는 빈도는 모른다.
  · 합성 사례의 값이다. 실제 광고 이미지의 분포를 대표하지 않으며 사례 수가 적어 비율의 불확실성이 크다(윌슨 95% 구간을 함께 적는다).
  · 라벨(기대 판정·기대 규칙)은 이 프로젝트의 검사 기준에 비춰 사례 작성자(규칙을 쓴 같은 저자)가 달았다. 독립 라벨러도 실제 플랫폼 심사 결과도 없다.
  · '문제 있는 이미지 통과 0' 은 이 사례들에서 관측된 값이며 모든 이미지에 대한 보장이 아니다.

개발용(dev)과 최종 확인용(holdout) 분리
  · dev 사례는 구현과 함께 쓰였고 그 결과를 보며 코드를 고쳤다.
  · holdout 사례는 구현·dev 사례·이 평가 코드를 고정한 **뒤에** 썼고, holdout 결과를 보고 코드나 사례를 고치지 않았다. 틀린 것은 틀린 대로 보고한다.

지표 (코드가 계산한다)
  · rule_detection   기대한 규칙 ID 집합과 정확히 같은 규칙이 나온 사례 비율 (규칙을 기대한 사례만)
  · decision_accuracy 기대한 판정과 같은 판정이 나온 사례 비율
  · bad_pass         문제 있는 이미지(bad)가 점검 통과로 나온 수 — 게이트 0
  · unreadable_pass  읽지 못한(일부 포함) 이미지가 점검 통과로 나온 수 — 게이트 0
  · sanitize         원본에 위험 요소가 있던 SVG 중 거부되었거나 정화 뒤 남은 위험 요소가 0인 비율 — 게이트 100%
  · load_accuracy    받아들이기·거부 사유가 기대와 같은 비율
  · wcag             손으로 계산한 알려진 색 쌍과의 오차
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Optional

from agents.vision import review_image
from core.banner import AA_RATIO, Palette, compose, contrast_ratio
from core.config import Settings
from core.images import ImageRejection, load_image, svg_hazards
from core.schemas import AdCopy, Decision, Fact
from evals.metrics import wilson
from guardrails import image_checks as ic
from llm.base import LLMResponse
from llm.errors import ProviderError
from llm.router import Router
from observability.tracer import Tracer

HERE = Path(__file__).resolve().parent
DATASET = HERE / "datasets" / "image_cases.jsonl"
FIXTURE_DIR = HERE / "datasets" / "images"

PROVENANCE = {
    "author": "프로젝트 저자 (규칙을 쓴 같은 저자 — 독립 라벨러 아님)",
    "synthetic": True,
    "real_vision_model_measured": False,
    "dev": "구현과 함께 작성, 결과를 보며 코드를 고쳤다",
    "holdout": "구현·dev 사례·평가 코드를 고정한 뒤 작성, 결과를 보고 고치지 않았다",
    "label_basis": "이 프로젝트의 검사 기준(규칙은 모두 이 프로젝트가 정함)에 비춰 저자가 단 기대값. 실제 플랫폼 심사 결과 아님",
}


def _fixtures():
    spec = importlib.util.spec_from_file_location("image_make_fixtures", FIXTURE_DIR / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Scripted:
    """통제된 가짜 제공자: 정해 둔 VISION 응답(dict) 또는 오류를 낸다. 실제 모델이 아니다."""

    name = "openai"
    model = "scripted"
    available = True

    def __init__(self, payload: Any):
        self.payload = payload

    def complete(self, req) -> LLMResponse:
        if isinstance(self.payload, Exception):
            raise self.payload
        return LLMResponse(text="{}", parsed=req.schema.model_validate(self.payload), provider=self.name, model=self.model, input_tokens=10, output_tokens=5,
                           latency_ms=1.0, cost_usd=0.001, finish_reason="stop")


LIVE = Settings.from_env(openai_api_key="sk-eval-not-a-real-key", anthropic_api_key="", force_demo=False)
DEMO = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)


def load_cases(split: str = "all") -> list[dict]:
    rows = [json.loads(ln) for ln in DATASET.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return rows if split == "all" else [r for r in rows if r["split"] == split]


def dataset_sha256() -> str:
    return hashlib.sha256(DATASET.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- 사례 한 건
def _payload(spec: dict) -> Any:
    if "error" in spec:
        return ProviderError("simulated provider failure", provider="openai")
    if "read_n" in spec:
        segs = [f"서로 다른 문구 {i} 번째 줄" for i in range(int(spec["read_n"]))]
    else:
        segs = list(spec.get("read", []))
    return {"readable": bool(spec.get("readable", True)), "text_segments": [{"text": s, "region": "center"} for s in segs], "tags": list(spec.get("tags", [])),
            "notes": spec.get("notes", "")}


def _facts(row: dict) -> list[Fact]:
    return [Fact(id=f["id"], text=f["text"]) for f in row.get("facts", [])]


def _eval_wcag(row: dict, mf) -> dict:
    r = contrast_ratio(row["fg"], row["bg"])
    return {"predicted": round(r, 3), "checks": {"ratio": abs(r - row["ratio"]) <= 0.01, "aa": (r >= AA_RATIO) == row["aa"]}, "abs_error": abs(r - row["ratio"])}


def _eval_banner(row: dict, mf) -> dict:
    c = row["copy"]
    copy = AdCopy(headline=c.get("headline", ""), body=c.get("body", ""), cta=c.get("cta", ""), disclosures=list(c.get("disclosures", [])))
    if "headline_repeat" in c:
        copy = copy.model_copy(update={"headline": c["headline_repeat"][0] * c["headline_repeat"][1]})
    if "body_repeat" in c:
        copy = copy.model_copy(update={"body": c["body_repeat"][0] * c["body_repeat"][1]})
    pal: Any = row.get("palette", "paper")
    if "palette_custom" in row:
        p = row["palette_custom"]
        pal = Palette("custom", "시험용", p["bg"], p["title"], p["body"], p["accent"], p["button_text"], p.get("disclosure", p["body"]))
    b = compose(copy, size=row.get("size", "landscape"), template=row.get("template", "left"), palette=pal, include_disclosures=row.get("include_disclosures", True))
    rules = sorted({f.rule_id for f in ic.check_banner(b)})
    checks = {"fit": b.fit_violation == row["fit"], "rules": rules == sorted(row["rules"])}
    return {"predicted": {"fit": b.fit_violation, "rules": rules}, "checks": checks, "rules": rules}


def _asset(row: dict, mf, name: Optional[str] = None):
    fixture = row.get("fixture")
    data = mf.load(fixture) if fixture else row["svg"].encode("utf-8")
    return load_image(data, name or row.get("name") or fixture or "case.svg", claimed_mime=row.get("claimed_mime")), data


def _decision_checks(row: dict, rv, checks: dict) -> None:
    rules = sorted({f.rule_id for f in rv.findings})
    checks["decision"] = rv.decision.value == row["decision"]
    if "rules" in row:
        checks["rules"] = rules == sorted(row["rules"])
    if "source_rules" in row:
        got = {getattr(f, "source_rule_id", "") for f in rv.findings}
        checks["source_rules"] = set(row["source_rules"]) <= got


def _eval_image_text(row: dict, mf) -> dict:
    asset, _ = _asset(row, mf)
    if isinstance(asset, ImageRejection):
        return {"predicted": f"reject:{asset.code}", "checks": {"decision": False}, "error": "fixture rejected"}
    prov = row["provider"]
    if prov == "demo":
        router, settings = Router(DEMO, tracer=Tracer()), DEMO
    else:
        router, settings = Router(LIVE, providers={"openai": _Scripted(_payload(prov))}, tracer=Tracer()), LIVE
    rv = review_image(asset, router, settings, declared_text=row.get("declared"), facts=_facts(row), categories=row.get("categories", ["other"]))
    checks: dict = {}
    _decision_checks(row, rv, checks)
    return {"predicted": rv.decision.value, "rules": sorted({f.rule_id for f in rv.findings}), "checks": checks, "unreadable_flag": rv.unreadable, "review": rv}


def _eval_svg(row: dict, mf) -> dict:
    asset, raw = _asset(row, mf)
    raw_text = raw.decode("utf-8", errors="replace")
    hazards = svg_hazards(raw_text)
    if isinstance(asset, ImageRejection):
        checks = {"load": row["load"] == f"reject:{asset.code}"}
        return {"predicted": f"reject:{asset.code}", "checks": checks, "raw_hazards": bool(hazards), "sanitized_ok": True}
    rv = review_image(asset, None, None, declared_text=row.get("declared"), facts=_facts(row), categories=row.get("categories", ["other"]))
    left = svg_hazards(asset.data.decode("utf-8"))
    checks = {"load": row["load"] == "accept", "hazard_free": not left}
    _decision_checks(row, rv, checks)
    return {"predicted": rv.decision.value, "rules": sorted({f.rule_id for f in rv.findings}), "checks": checks, "raw_hazards": bool(hazards), "sanitized_ok": not left, "review": rv}


def _eval_load(row: dict, mf) -> dict:
    res, _ = _asset(row, mf)
    got = f"reject:{res.code}" if isinstance(res, ImageRejection) else f"accept:{res.fmt}:{res.width}x{res.height}"
    return {"predicted": got, "checks": {"load": got == row["expect"]}}


EVALUATORS = {"wcag": _eval_wcag, "banner": _eval_banner, "image_text": _eval_image_text, "svg": _eval_svg, "load": _eval_load}


def evaluate_case(row: dict, mf=None) -> dict:
    mf = mf or _fixtures()
    try:
        out = EVALUATORS[row["type"]](row, mf)
    except Exception as e:  # noqa: BLE001 — 평가 코드의 오류는 사례의 오류로 센다 (조용히 건너뛰지 않는다)
        return {"id": row["id"], "split": row["split"], "type": row["type"], "error": f"{type(e).__name__}: {e}", "checks": {}, "ok": False}
    ok = all(out["checks"].values())
    return {"id": row["id"], "split": row["split"], "type": row["type"], "ok": ok, **{k: v for k, v in out.items() if k != "review"}, "_row": row}


# --------------------------------------------------------------------------- 집계
def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n) if n else (0.0, 0.0)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": [round(lo, 3), round(hi, 3)] if n else None}


def summarize(results: list[dict]) -> dict:
    def has(r, key):
        return key in r.get("checks", {})

    rule_rows = [r for r in results if has(r, "rules")]
    dec_rows = [r for r in results if has(r, "decision")]
    bad = [r for r in results if r.get("_row", {}).get("bad")]
    unreadable = [r for r in results if r.get("_row", {}).get("unreadable")]
    svg_haz = [r for r in results if r.get("raw_hazards")]
    loads = [r for r in results if r["type"] == "load"]
    wc = [r for r in results if r["type"] == "wcag"]
    clean = [r for r in results if r.get("_row", {}).get("clean")]
    return {
        "n": len(results),
        "by_type": {t: sum(1 for r in results if r["type"] == t) for t in EVALUATORS},
        "errors": sum(1 for r in results if r.get("error") and not r.get("checks")),            # 평가 코드가 예외를 낸 사례
        "all_expectations_met": _rate(sum(1 for r in results if r["ok"]), len(results)),
        "rule_detection": _rate(sum(1 for r in rule_rows if r["checks"]["rules"]), len(rule_rows)),
        "decision_accuracy": _rate(sum(1 for r in dec_rows if r["checks"]["decision"]), len(dec_rows)),
        "bad_pass": _rate(sum(1 for r in bad if r.get("predicted") == Decision.APPROVE.value), len(bad)),
        "unreadable_pass": _rate(sum(1 for r in unreadable if r.get("predicted") == Decision.APPROVE.value), len(unreadable)),
        "sanitize": _rate(sum(1 for r in svg_haz if r.get("sanitized_ok")), len(svg_haz)),
        "load_accuracy": _rate(sum(1 for r in loads if r["ok"]), len(loads)),
        "wcag": {**_rate(sum(1 for r in wc if r["ok"]), len(wc)), "max_abs_error": round(max((r.get("abs_error") or 0.0 for r in wc), default=0.0), 4)},
        "clean_not_approved": _rate(sum(1 for r in clean if r.get("predicted") != Decision.APPROVE.value), len(clean)),
    }


def run_eval(split: str = "all") -> dict:
    mf = _fixtures()
    rows = load_cases(split)
    results = [evaluate_case(r, mf) for r in rows]
    report = {
        "dataset": "image_cases.jsonl", "dataset_sha256": dataset_sha256(), "split": split, "provenance": PROVENANCE,
        "real_vision_model_measured": False, "summary": summarize(results),
        "by_split": {s: summarize([r for r in results if r["split"] == s]) for s in sorted({r["split"] for r in results})},
        "cases": [{k: v for k, v in r.items() if k != "_row"} for r in results],
        "mismatches": [{k: v for k, v in r.items() if k != "_row"} for r in results if not r["ok"]],
        "repro": f"python -m evals.image_eval --split {split}",
    }
    return report


def gate(report: dict) -> list[str]:
    """게이트: 문제 있는 이미지 통과 0 · 읽지 못한 이미지 통과 0 · 정화 100% · 평가 오류 0. (정확도 지표는 게이트가 아니다 — 보고한다.)"""
    out: list[str] = []
    for name, s in [("전체", report["summary"]), *report["by_split"].items()]:
        if s["bad_pass"]["k"]:
            out.append(f"{name}: 문제 있는 이미지가 점검 통과로 나옴 {s['bad_pass']['k']}/{s['bad_pass']['n']}")
        if s["unreadable_pass"]["k"]:
            out.append(f"{name}: 읽지 못한 이미지가 점검 통과로 나옴 {s['unreadable_pass']['k']}/{s['unreadable_pass']['n']}")
        if s["sanitize"]["n"] and s["sanitize"]["k"] != s["sanitize"]["n"]:
            out.append(f"{name}: SVG 정화 실패 {s['sanitize']['n'] - s['sanitize']['k']}건")
        if s["errors"]:
            out.append(f"{name}: 평가 중 오류 {s['errors']}건")
    seen = []
    for m in out:
        if m not in seen:
            seen.append(m)
    return seen


def _fmt(m: dict) -> str:
    return f"{m['k']}/{m['n']}" + (f" = {m['rate']:.3f}  (윌슨 95% {m['wilson95'][0]}–{m['wilson95'][1]})" if m.get("rate") is not None else " (대상 없음)")


def print_report(report: dict) -> None:
    print(f"image_eval | 데이터 {report['dataset']} sha256 {report['dataset_sha256'][:12]} | 실제 비전 모델 측정: 아니오 (합성 사례)")
    for name, s in [("전체", report["summary"]), *report["by_split"].items()]:
        print(f"\n[{name}] 사례 {s['n']}건 {s['by_type']}")
        for key, label in (("all_expectations_met", "모든 기대 충족"), ("rule_detection", "규칙 탐지 정확도(결정론)"), ("decision_accuracy", "판정 정확도"),
                           ("bad_pass", "문제 있는 이미지 통과(게이트 0)"), ("unreadable_pass", "읽지 못함 → 통과(게이트 0)"), ("sanitize", "SVG 정화 성공률"),
                           ("load_accuracy", "받아들이기·거부 정확도"), ("clean_not_approved", "문제없는 사례를 통과시키지 못한 수")):
            print(f"  {label:32s} {_fmt(s[key])}")
        w = s["wcag"]
        print(f"  {'WCAG 알려진 쌍':32s} {w['k']}/{w['n']} 최대 오차 {w['max_abs_error']}")
    if report["mismatches"]:
        print(f"\n기대와 다른 사례 {len(report['mismatches'])}건:")
        for m in report["mismatches"]:
            print(f"  {m['id']:8s} ({m['split']}) 예측 {m.get('predicted')} 규칙 {m.get('rules')} 어긋난 항목 {[k for k, v in m.get('checks', {}).items() if not v]} {m.get('error') or ''}")
    print("\n주의: 이 값은 합성 사례의 것이며 실제 비전 모델의 정확도가 아니다. 라벨은 규칙을 쓴 저자가 달았다.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="이미지 소재 평가")
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    ap.add_argument("--gate", action="store_true", help="문제 있는 이미지 통과·읽지 못함 통과·정화 실패·평가 오류가 하나라도 있으면 종료 코드 1")
    ap.add_argument("--json", action="store_true", help="보고서를 JSON 으로 출력")
    ap.add_argument("--save", default=None, help="보고서 JSON 을 저장할 경로 (기본: 저장하지 않음)")
    args = ap.parse_args(argv)
    report = run_eval(args.split)
    if args.save:
        Path(args.save).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(report)
    if args.gate:
        fails = gate(report)
        for f in fails:
            print("GATE FAIL:", f, file=sys.stderr)
        print("gate:", "FAIL" if fails else "PASS")
        return 1 if fails else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
