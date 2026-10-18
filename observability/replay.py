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


_VALID_STATUS = {x.value for x in RewriteStatus}
LEGACY_NOTE = "이 기록에는 수정안의 최종 상태가 저장돼 있지 않아 확인하지 못했습니다 (기록 당시 코드 검사 결과만 남아 있어 통과 여부를 판단할 수 없음)."


def stored_rewrite_state(variant_span: dict, has_rewrite: bool) -> dict:
    """저장된 실행 기록에서 수정안의 **최종** 상태를 읽는다 — 모든 화면(검사 사례·정적 재생 페이지·내려받기)이 이 함수 하나를 쓴다.

    - 상태는 실행 당시 판정 단계(adjudicate)가 저장한 `rewrite_status` 그대로다. 코드 검사 결과(rewrite_recheck.decision 등)로
      '통과' 를 다시 계산하지 않는다 — 코드 검사는 통과했지만 의미 검사에서 걸린 수정안이 통과로 보이던 문제가 있었다.
    - 최종 상태가 저장돼 있지 않은 과거 기록은 '확인하지 못함'(unchecked, source=legacy_unknown) 으로 읽는다. 기록 원본은 고치지 않는다.
    - recheck 에는 기록에 남아 있는 근거(코드 검사·의미 검사 결과)만 담는다. 상태를 정하는 데 쓰지 않고 보여 주기만 한다.
    """
    adj = (_child(variant_span, "adjudicate") or {}).get("attributes", {})
    rc = (_child(variant_span, "rewrite_recheck") or {}).get("attributes", {})
    stored = adj.get("rewrite_status")
    recheck = None
    if rc:
        recheck = {"det_decision": rc.get("det_decision", rc.get("decision")),  # 과거 기록의 decision 은 코드 검사 결과였다
                   "shared_decision": rc.get("decision") if "det_decision" in rc else None,
                   "semantic_decision": rc.get("semantic_decision"), "semantic_confidence": rc.get("semantic_confidence"),
                   "unverified_quotes": rc.get("unverified_quotes"), "reason": rc.get("reason"), "threshold": rc.get("threshold"),
                   "findings": rc.get("findings", [])}
    if has_rewrite and stored in (RewriteStatus.VERIFIED.value, RewriteStatus.FAILED_RECHECK.value, RewriteStatus.UNCHECKED.value):
        return {"status": stored, "source": "stored", "note": adj.get("rewrite_note") or "", "recheck": recheck}
    if not has_rewrite and stored in (None, RewriteStatus.NONE.value):
        return {"status": RewriteStatus.NONE.value, "source": "stored", "note": "자동 수정안 없음", "recheck": recheck}
    # 수정안은 있는데 최종 상태가 없거나(과거 기록), 저장된 값이 수정안 유무와 맞지 않는다 → 확인하지 못함
    return {"status": RewriteStatus.UNCHECKED.value, "source": "legacy_unknown", "note": LEGACY_NOTE, "recheck": recheck}


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
        rw_state = stored_rewrite_state(vs, bool(rewrite))  # 저장된 최종 상태. 코드 검사 결과로 다시 계산하지 않는다
        variants.append({
            "copy": copy, "final": final, "reason": adj["attributes"].get("reason", ""),
            "det_findings_recorded": recorded_det, "det_rule_ids_now": now_ids, "det_rule_ids_then": then_ids, "det_changed": now_ids != then_ids,
            "det_now": det_now,
            "review": {"decision": rv["attributes"].get("decision"), "confidence": rv["attributes"].get("confidence"),
                       "provider": rv["attributes"].get("provider"), "unverified_quotes": rv["attributes"].get("unverified_quotes", 0),
                       "findings": (rraw or {}).get("findings", []), "rationale": (rraw or {}).get("rationale", "")},
            "second": {"decision": so["attributes"].get("decision"), "disagreement": so["attributes"].get("disagreement"),
                       "independent": so["attributes"].get("independent")} if so else None,
            "rewrite": rewrite, "rewrite_status": rw_state["status"], "rewrite_state": rw_state,
            "recheck_findings": (rc or {"attributes": {}})["attributes"].get("findings", []),
        })
    out["variants"] = variants
    return out


def _walk(span: dict):
    yield span
    for c in span.get("children", []):
        yield from _walk(c)
