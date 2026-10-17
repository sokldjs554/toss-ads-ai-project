"""저장된 트레이스(JSON)에서 '사람이 읽을 결과' 를 복원한다 — 모델을 다시 부르지 않는다.

검사 사례 화면과 공개 재생 페이지가 같은 함수를 쓴다. 트레이스에는 모델 응답 원문이 그대로 있으므로
소재·심사 결과·판정을 그대로 꺼내고, 결정론 검사만 현재 코드로 다시 돌려 '그때 기록' 과 '지금 코드' 가 다르면 표시한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from core.schemas import AdCopy, Claim, Decision, RewriteStatus
from guardrails import output_guard
from guardrails.policy import PolicyCatalog, get_catalog

TRACES_DIR = Path(__file__).resolve().parents[1] / "traces"


def _jl(s: str) -> Optional[dict]:
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None


def _child(span: dict, name: str) -> Optional[dict]:
    for c in span.get("children", []):
        if c["name"] == name:
            return c
    return None


def _llm_child(span: dict) -> Optional[dict]:
    for c in span.get("children", []):
        if c["kind"] == "llm":
            return c
    return None


def recorded_at(trace: dict) -> str:
    try:
        return datetime.fromtimestamp(trace["root"]["start_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def list_stored(mode: str = "partial") -> dict[str, Path]:
    return {p.stem.replace(f"sample_{mode}_", ""): p for p in sorted(TRACES_DIR.glob(f"sample_{mode}_*.json"))}


def summarize_trace(trace: dict, product, categories: set[str], landing_text: str = "", catalog: Optional[PolicyCatalog] = None) -> dict:
    """트레이스 → 화면용 요약. product 는 ProductFacts (결정론 재검사용)."""
    catalog = catalog or get_catalog()
    root = trace["root"]
    llm_spans = [s for s in _walk(root) if s["kind"] == "llm"]
    meta = trace.get("metadata", {})
    out: dict[str, Any] = {
        "trace_id": trace.get("trace_id"), "mode": meta.get("mode", ""), "recorded_at": recorded_at(trace),
        "prompt_version": meta.get("prompt_version"), "policy_version": meta.get("policy_version"),
        "llm_calls": len(llm_spans), "cost_usd": round(sum(float(s["attributes"].get("cost_usd", 0)) for s in llm_spans), 4),
        "cost_estimated": any(s["attributes"].get("cost_estimated") for s in llm_spans),
        "duration_ms": round((root.get("end_ms") or root["start_ms"]) - root["start_ms"], 1),
        "models": sorted({f"{s['attributes'].get('provider')}/{s['attributes'].get('model')}" for s in llm_spans if s["attributes"].get("provider")}),
        "halted": bool(meta.get("halted")), "final": meta.get("final_decisions", []),
    }
    ig = _child(root, "input_guard:intercept") or {"attributes": {}}
    ia = ig["attributes"]
    out["intercept"] = {"triggered": bool(ia.get("triggered")), "kind": ia.get("kind"), "hard_block": bool(ia.get("hard_block")),
                        "removed": ia.get("removed", []), "suggested": ia.get("suggested_input", ""),
                        "findings": ia.get("findings", [])}
    pl = _child(root, "planner")
    out["plan"] = None
    if pl:
        pllm = _llm_child(pl)
        out["plan"] = {"reasoning": pl["attributes"].get("reasoning", ""), "objective": pl["attributes"].get("objective"),
                       "raw": _jl(pllm["attributes"].get("response_text", "")) if pllm else None, "repaired": pl["attributes"].get("repaired")}
    cw = _child(root, "copywriter")
    cwr = _jl(_llm_child(cw)["attributes"].get("response_text", "")) if cw and _llm_child(cw) else None
    cw_variants = (cwr or {}).get("variants", [])
    variants = []
    for i, vs in enumerate([c for c in root.get("children", []) if c["name"].startswith("variant:")]):
        if i >= len(cw_variants):
            break
        cv = cw_variants[i]
        copy = AdCopy(variant_id=vs["name"].split(":", 1)[1], headline=cv["headline"].strip(), body=cv["body"].strip(),
                      cta=(cv.get("cta") or "자세히 보기").strip(), claims=[Claim(text=c["text"], fact_id=c.get("fact_id")) for c in cv.get("claims", [])],
                      disclosures=[d for d in cv.get("disclosures", []) if d.strip()], tone=cv.get("tone", ""))
        og = _child(vs, "output_guard") or {"attributes": {}}
        recorded_det = og["attributes"].get("findings", [])
        det_now = output_guard.check(copy, product, categories, landing_text, [], catalog)
        now_ids = sorted({f.rule_id for f in det_now.findings})
        then_ids = sorted({f["rule_id"] for f in recorded_det})
        rv = _child(vs, "reviewer") or {"attributes": {}}
        rraw = _jl(_llm_child(rv)["attributes"].get("response_text", "")) if _llm_child(rv) else None
        so = _child(vs, "second_opinion")
        rc = _child(vs, "rewrite_recheck")
        adj = _child(vs, "adjudicate") or {"attributes": {}}
        final = adj["attributes"].get("final")
        rewrite = (rraw or {}).get("rewrite")
        if rewrite:
            if rc is None:
                rw_status = RewriteStatus.UNCHECKED.value
            elif rc["attributes"].get("decision") == Decision.APPROVE.value:
                rw_status = RewriteStatus.VERIFIED.value
            else:
                rw_status = RewriteStatus.FAILED_RECHECK.value
        else:
            rw_status = RewriteStatus.NONE.value
        variants.append({
            "copy": copy, "final": final, "reason": adj["attributes"].get("reason", ""),
            "det_findings_recorded": recorded_det, "det_rule_ids_now": now_ids, "det_rule_ids_then": then_ids, "det_changed": now_ids != then_ids,
            "det_now": det_now,
            "review": {"decision": rv["attributes"].get("decision"), "confidence": rv["attributes"].get("confidence"),
                       "provider": rv["attributes"].get("provider"), "unverified_quotes": rv["attributes"].get("unverified_quotes", 0),
                       "findings": (rraw or {}).get("findings", []), "rationale": (rraw or {}).get("rationale", "")},
            "second": {"decision": so["attributes"].get("decision"), "disagreement": so["attributes"].get("disagreement"),
                       "independent": so["attributes"].get("independent")} if so else None,
            "rewrite": rewrite, "rewrite_status": rw_status,
            "recheck_findings": (rc or {"attributes": {}})["attributes"].get("findings", []),
        })
    out["variants"] = variants
    return out


def _walk(span: dict):
    yield span
    for c in span.get("children", []):
        yield from _walk(c)
