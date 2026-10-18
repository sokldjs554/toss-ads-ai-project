"""저장소의 실제 트레이스·평가 리포트에서 재생 페이지(site/replay) 데이터를 만든다 (모델 호출 없음).

    python scripts/build_replay.py [--out _site/replay]
"""
import argparse, json, sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
HIST = REPO / "evals" / "reports" / "history"
sys.path.insert(0, str(REPO))

from agents.personas import PERSONAS
from agents.reviewer import DET_AUTHORITATIVE_LAYERS, _to_verdict
from core.schemas import AdCopy, Claim, Layer
from guardrails import output_guard
from guardrails.failure_modes import FAILURE_MODES
from guardrails.input_guard import intercept
from guardrails.normalize import loose_text
from guardrails.policy import get_catalog
from observability.replay import stored_rewrite_state
from ui.presets import PRESETS, build_brief

CAT = get_catalog()
PNAME = {p["persona_id"]: p["name"] for p in PERSONAS}
ORDER = ["loan_ok", "malicious", "injection", "prohibited", "commerce_landing"]
SHORT = {"loan_ok": ("정상 요청", "신용대출 · 조건과 고지를 갖춘 요청"),
         "malicious": ("악의적 요청", "원금 보장·무조건 승인을 써 달라는 요청"),
         "injection": ("프롬프트 인젝션", "상품 설명에 숨은 지시문과 보이지 않는 문자"),
         "prohibited": ("금지 업종", "리딩방 유입 광고"),
         "commerce_landing": ("랜딩 불일치", "쿠폰 조건이 랜딩 페이지와 다름")}


def jl(s):
    try:
        return json.loads(s)
    except Exception:
        return None


def flat(span, depth=0, out=None):
    out = [] if out is None else out
    out.append((depth, span))
    for c in span.get("children", []):
        flat(c, depth + 1, out)
    return out


def child(span, name, kind=None):
    for c in span.get("children", []):
        if c["name"] == name and (kind is None or c["kind"] == kind):
            return c
    return None


def llm_child(span):
    for c in span.get("children", []):
        if c["kind"] == "llm":
            return c
    return None


def build_scenario(key):
    t = json.load(open(REPO / "traces" / f"sample_partial_{key}.json", encoding="utf-8"))
    P = PRESETS[key]
    brief = build_brief(P)
    root = t["root"]
    t0 = root["start_ms"]
    spans = flat(root)
    llm_spans = [s for _, s in spans if s["kind"] == "llm"]
    sc = {"key": key, "label": SHORT[key][0], "blurb": SHORT[key][1],
          "input": {"advertiser_input": P["advertiser_input"], "product": P["product_name"], "category": P["category"],
                    "facts": P["facts"], "description": P.get("description", ""), "landing": P.get("landing_text", "")},
          "trace": {"id": t["trace_id"], "run_id": t["metadata"].get("run_id"), "duration_ms": round(root["end_ms"] - t0, 1),
                    "llm_calls": len(llm_spans), "cost_usd": round(sum(float(s["attributes"].get("cost_usd", 0)) for s in llm_spans), 4),
                    "tokens_in": sum(int(s["attributes"].get("input_tokens", 0)) for s in llm_spans),
                    "tokens_out": sum(int(s["attributes"].get("output_tokens", 0)) for s in llm_spans),
                    "halted": bool(t["metadata"].get("halted")), "final": t["metadata"].get("final_decisions", [])}}
    # ---- waterfall spans
    wf = []
    for depth, s in spans:
        a = s["attributes"]
        row = {"name": s["name"], "kind": s["kind"], "status": s["status"], "depth": depth, "off": round(s["start_ms"] - t0, 1),
               "dur": round((s["end_ms"] or s["start_ms"]) - s["start_ms"], 1), "events": [e["name"] for e in s["events"]]}
        if s["kind"] == "llm":
            row.update({"provider": a.get("provider"), "model": a.get("model"), "task": a.get("task"), "in": a.get("input_tokens"), "out": a.get("output_tokens"),
                        "cost": a.get("cost_usd"), "latency": a.get("latency_ms"), "constrained": a.get("constrained"), "chain": a.get("fallback_chain") or a.get("chain"),
                        "schema": a.get("schema"), "user": (a.get("prompt_user") or "")[:700], "resp": (a.get("response_text") or "")[:1100]})
        else:
            row["attrs"] = {k: v for k, v in a.items() if k not in ("findings", "checklist", "prompt_system", "prompt_user", "response_text") and not isinstance(v, (dict,))}
            for k, v in list(row["attrs"].items()):
                if isinstance(v, list):
                    row["attrs"][k] = json.dumps(v, ensure_ascii=False)[:220]
        wf.append(row)
    sc["spans"] = wf
    # ---- intercept
    ig = child(root, "input_guard:intercept")
    ia = ig["attributes"]
    redirect = llm_child(ig)
    red = jl(redirect["attributes"].get("response_text", "")) if redirect else None
    det_notice = intercept(brief, CAT, router=None)[0]
    msg = det_notice.message
    if red and red.get("suggested_input") == ia.get("suggested_input"):
        msg = red["message"]
    sc["intercept"] = {"triggered": bool(ia.get("triggered")), "kind": ia.get("kind"), "hard_block": bool(ia.get("hard_block")), "message": msg,
                       "removed": ia.get("removed", []), "suggested": ia.get("suggested_input", ""), "categories": ia.get("categories", []),
                       "message_by_llm": bool(red and red.get("suggested_input") == ia.get("suggested_input")),
                       "findings": [{"rule_id": f["rule_id"], "severity": f["severity"], "quote": f["quote"], "explanation": f["explanation"]} for f in ia.get("findings", [])]}
    # ---- planner
    pl = child(root, "planner", "stage")
    if pl:
        pllm = llm_child(pl)
        plan = jl(pllm["attributes"].get("response_text", "")) if pllm else None
        sc["planner"] = {"plan": plan, "repaired": pl["attributes"].get("repaired"), "issues": pl["attributes"].get("issues"), "provider": pl["attributes"].get("provider")}
    else:
        sc["planner"] = None
    # ---- copywriter + variants
    cw = child(root, "copywriter", "stage")
    cwr = jl(llm_child(cw)["attributes"].get("response_text", "")) if cw else None
    cw_variants = (cwr or {}).get("variants", [])
    vspans = [c for c in root["children"] if c["name"].startswith("variant:")]
    variants = []
    cats = set(ia.get("categories", [])) or {P["category"]}
    for i, vs in enumerate(vspans):
        cv = cw_variants[i]
        copy = AdCopy(variant_id=vs["name"].split(":", 1)[1], headline=cv["headline"].strip(), body=cv["body"].strip(), cta=(cv.get("cta") or "자세히 보기").strip(),
                      claims=[Claim(text=c["text"], fact_id=c.get("fact_id")) for c in cv.get("claims", [])], disclosures=[d for d in cv.get("disclosures", []) if d.strip()], tone=cv.get("tone", ""))
        det = output_guard.check(copy, brief.product, cats, P.get("landing_text", ""), [], CAT)
        og = child(vs, "output_guard")
        rec_ids = sorted({(f["rule_id"], f["severity"]) for f in og["attributes"].get("findings", [])})
        now_ids = sorted({(f.rule_id, f.severity.value) for f in det.findings})
        if rec_ids != now_ids:
            print("WARN det mismatch", key, i, rec_ids, now_ids)
        full = copy.full_text()
        # audience
        au = child(vs, "audience")
        aud = None
        if au:
            ar = jl(llm_child(au)["attributes"].get("response_text", "")) if llm_child(au) else None
            aa = au["attributes"]
            aud = {"mean_appeal": aa.get("mean_appeal"), "mean_trust": aa.get("mean_trust"), "click_rate": aa.get("click_rate"), "diversity": aa.get("diversity"),
                   "sycophancy": aa.get("sycophancy"), "summary": (ar or {}).get("summary", ""),
                   "feedbacks": [{"name": PNAME.get(f["persona_id"], f["persona_id"]), "appeal": f["appeal"], "trust": f["trust"], "clarity": f["clarity"], "click": f["would_click"],
                                  "concern": f["concern"], "quote": f["quote"]} for f in (ar or {}).get("feedbacks", []) if f["persona_id"] in PNAME]}
        # reviewer
        rv = child(vs, "reviewer")
        rraw = jl(llm_child(rv)["attributes"].get("response_text", "")) if rv and llm_child(rv) else None
        review = None
        if rraw:
            rw = rraw.get("rewrite")
            parsed = SimpleNamespace(decision=rraw["decision"], confidence=rraw["confidence"], rationale=rraw.get("rationale", ""),
                                     rewrite=SimpleNamespace(**rw) if rw else None,
                                     findings=[SimpleNamespace(**f) for f in rraw.get("findings", [])], model_dump=lambda: {})
            out = _to_verdict(copy, parsed, det, CAT, rv["attributes"].get("provider", ""), "gpt-4o")
            det_ids = {f.rule_id for f in det.findings}
            det_keys = {(f.rule_id, loose_text(f.quote)) for f in det.findings}
            raw_findings = []
            for f in rraw.get("findings", []):
                rule = CAT.get(f["rule_id"])
                if rule is None:
                    status = "unknown"
                elif (rule.id, loose_text(f["quote"])) in det_keys:
                    status = "duplicate"
                elif rule.layer in DET_AUTHORITATIVE_LAYERS and rule.id not in det_ids:
                    status = "overruled"
                elif not (loose_text(f["quote"]).strip() and loose_text(f["quote"]).strip() in loose_text(full)):
                    status = "unverified"
                else:
                    status = "kept"
                raw_findings.append({"rule_id": f["rule_id"], "severity": rule.severity.value if rule else "warn", "layer": rule.layer.value if rule else "",
                                     "quote": f["quote"], "explanation": f["explanation"], "status": status})
            review = {"model_decision": rraw["decision"], "model_confidence": rraw["confidence"], "decision": out.verdict.decision.value, "confidence": out.verdict.confidence,
                      "rationale": rraw.get("rationale", ""), "provider": rv["attributes"].get("provider"), "model": "gpt-4o", "unverified": out.unverified_quotes,
                      "overruled": out.overruled_by_det, "unsupported": out.verdict.unsupported_decision, "findings": raw_findings,
                      "rewrite": {"headline": rw["headline"], "body": rw["body"], "cta": rw["cta"], "disclosures": rw.get("disclosures", [])} if rw else None}
        so = child(vs, "second_opinion")
        second = None
        if so:
            sr = jl(llm_child(so)["attributes"].get("response_text", "")) if llm_child(so) else None
            second = {"decision": so["attributes"].get("decision"), "confidence": (sr or {}).get("confidence"), "disagreement": so["attributes"].get("disagreement"),
                      "independent": so["attributes"].get("independent"), "provider": so["attributes"].get("provider")}
        adj = child(vs, "adjudicate")
        final = adj["attributes"].get("final")
        # 수정안의 최종 상태는 저장된 기록 그대로 읽는다 (observability.replay.stored_rewrite_state — 검사 사례 화면과 같은 함수).
        # 코드 검사 결과(rewrite_recheck.decision)로 '통과' 를 다시 계산하지 않는다.
        rw_state = stored_rewrite_state(vs, bool(review and review["rewrite"]))
        variants.append({"id": copy.variant_id, "copy": {"headline": copy.headline, "body": copy.body, "cta": copy.cta, "claims": [c.model_dump() for c in copy.claims],
                                                         "disclosures": copy.disclosures, "tone": copy.tone},
                         "det": {"decision": det.decision.value, "findings": [{"rule_id": f.rule_id, "severity": f.severity.value, "layer": f.layer.value, "quote": f.quote, "explanation": f.explanation} for f in det.findings],
                                 "checklist": [{"name": c.name, "passed": c.passed, "detail": c.detail, "layer": c.layer.value} for c in det.checklist]},
                         "audience": aud, "review": review, "second": second,
                         "final": final, "reason": adj["attributes"].get("reason"),
                         "rewrite_status": rw_state["status"], "rewrite_source": rw_state["source"], "rewrite_note": rw_state["note"],
                         "rewrite_recheck": rw_state["recheck"]})
    sc["variants"] = variants
    return sc



def eval_row(d, label, change):
    s = d["summary"]; c = s["calibration"]
    return {"label": label, "change": change, "prompt": d["prompt_version"], "n": d["n"], "cost": s["cost_usd"],
            "coverage": s["coverage"], "unsafe": s["unsafe_approve"], "false_flag": s["false_flag"], "human": s["human_review"],
            "ece": c["ece"], "brier": c["brier"], "hc": c["high_confidence_error"], "unverified": sum(x["unverified_quotes"] for x in d["cases"]),
            "tau": d.get("recommended_threshold_for_zero_unsafe_and_zero_false_flag")}


def red_row(t):
    a = t["attacks"]
    return {"n": len(a), "evaded": sum(1 for x in a if x["evaded"]), "human": sum(1 for x in a if x["predicted"] == "HUMAN_REVIEW"),
            "revise": sum(1 for x in a if x["predicted"] == "REVISE"), "cost": t["cost_usd"],
            "by_strategy": t["by_strategy"]}


r1 = json.load(open(HIST / "baseline_partial_run1.json", encoding="utf-8"))
r2 = json.load(open(HIST / "baseline_partial_run2.json", encoding="utf-8"))
r3 = json.load(open(HIST / "baseline_partial_run3.json", encoding="utf-8"))
r4 = json.load(open(HIST / "baseline_partial_run4_golden_v1.1_policy_2026.10.1.json", encoding="utf-8"))
r5 = json.load(open(REPO / "evals/reports/baseline_partial.json", encoding="utf-8"))
h5 = json.load(open(REPO / "evals/reports/holdout_partial.json", encoding="utf-8"))
c5 = json.load(open(REPO / "evals/reports/contrast_g069_partial.json", encoding="utf-8"))
t1 = json.load(open(HIST / "redteam_partial_run1.json", encoding="utf-8"))
t2 = json.load(open(HIST / "redteam_partial_run2.json", encoding="utf-8"))
t3 = json.load(open(HIST / "redteam_partial_run3.json", encoding="utf-8"))
t4 = json.load(open(HIST / "redteam_partial_run4_policy_2026.10.1.json", encoding="utf-8"))
t5 = json.load(open(REPO / "evals/reports/baseline_redteam_partial.json", encoding="utf-8"))
s5 = r5["summary"]
data = {
    "scenarios": [build_scenario(k) for k in ORDER],
    "evals": {"rounds": [eval_row(r1, "1차", "처음 실행. 프롬프트 v2026.10.1"),
                         eval_row(r2, "2차", "보이지 않는 문자 전부 제거 + 프롬프트 v2026.10.2"),
                         eval_row(r3, "3차", "코드가 확정한 고지·근거·구조를 모델이 뒤집지 못함(FM-23), 골든셋 55건"),
                         eval_row(r4, "4차", "근거 없는 수정·반려 제안은 사람 검토로"),
                         eval_row(r5, "5차", "조건 생략 규칙·수정안 의미 재검사·골든셋 v1.2(G-069 라벨 수정)·프롬프트 2026.10.3 — 분모가 바뀜(문제 있는 광고 42, 문제없는 광고 13)")],
              "extra": [dict(eval_row(h5, "holdout (5차)", "처음 보는 12건, 규칙을 고치는 데 쓰지 않음"), dataset="holdout"),
                        dict(eval_row(c5, "G-069 대조 (5차)", "'최초' 유무 × 결제 조건 표시 유무 4건"), dataset="contrast_g069")],
              "red": [dict(red_row(t), label=l) for t, l in ((t1, "1차"), (t2, "2차"), (t3, "3차"), (t4, "4차"), (t5, "5차 (검사 기준 2026.10.2)"))],
              "curve": s5["coverage_curve"], "curve_method": s5.get("curve_method"), "bins": [b for b in s5["calibration"]["bins"] if b["n"]],
              "by_diff": {k: {"n": v["n"], "unsafe": v["unsafe_approve"], "ff": v["false_flag"], "human": v["human_review"]} for k, v in s5["by_difficulty"].items()},
              "rules": sorted([{"id": k, **v} for k, v in s5["rules"].items()], key=lambda r: (r["precision"] if r["precision"] is not None else 2, -(r["tp"] + r["fp"])))[:14],
              "run_id": "39042649138", "generated_at": r5["generated_at"], "trace_run_id": "39044437805", "trace_date": "2026-10-17", "trace_round": "5차", "eval_round": "5차", "eval_date": "2026-10-17"},
    "fms": [{"code": f.code, "name": f.name, "stage": f.stage, "symptom": f.symptom, "detection": f.detection, "defense": f.defense, "test": f.test} for f in FAILURE_MODES],
    "rules": {r.id: {"title": r.title, "severity": r.severity.value, "layer": r.layer.value, "section": r.section} for r in CAT.rules.values()},
    "meta": {"policy_version": CAT.version, "rule_count": len(CAT.rules), "prompt_version": r5["prompt_version"]},
}
ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(REPO / "_site" / "replay"), help="index.html 을 쓸 디렉터리")
args = ap.parse_args()
out_dir = Path(args.out)
out_dir.mkdir(parents=True, exist_ok=True)
payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
template = (REPO / "site" / "replay" / "template.html").read_text(encoding="utf-8")
page = template.replace("__DATA__", payload)
head_part, body_part = page.split("</style>", 1)
html = ("<!doctype html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        "<meta name=\"description\" content=\"Preflight — 실제 GPT-4o 실행 트레이스와 1차→5차 평가 결과 재생\">\n"
        + head_part + "</style>\n</head>\n<body>" + body_part + "\n</body>\n</html>\n")
(out_dir / "index.html").write_text(html, encoding="utf-8")
print("wrote", out_dir / "index.html", len(html), "bytes")
for sc in data["scenarios"]:
    print(sc["key"], "spans", len(sc["spans"]), "variants", len(sc["variants"]), "final", [v["final"] for v in sc["variants"]], "intercept", sc["intercept"]["kind"],
          "| raw finding statuses", [[f["status"] for f in (v["review"] or {}).get("findings", [])] for v in sc["variants"]])
