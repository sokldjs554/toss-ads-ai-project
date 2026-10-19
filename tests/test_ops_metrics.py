"""운영 지표: 판정 분포·담당자 확인율·지연 분위수·비용·제공자·실패 모드·재검사율, 그리고 Prometheus 텍스트 형식."""
from __future__ import annotations

import json
import math
import re

import pytest

from core.config import Settings
from core.schemas import AdCopy, CampaignBrief, Decision, FailureEvent, Finding, Layer, PipelineRun, PreflightReport, ProductFacts, ReviewVerdict, RewriteStatus, Severity
from observability.tracer import Span, Trace, Tracer
from ops import metrics
from ops.metrics import Metrics, Quantiles, compute_metrics, escape_label_value, metrics_from_tracer, percentile


def _report(decision: Decision, findings: list[Finding] | None = None, variant: str = "v") -> PreflightReport:
    copy = AdCopy(variant_id=variant, headline="제목", body="본문", cta="신청")
    return PreflightReport(variant_id=variant, ad_copy=copy, verdict=ReviewVerdict(variant_id=variant, decision=decision, confidence=0.9, findings=findings or []), final_decision=decision)


def _run(decisions: list[Decision], mode: str = "demo", latency: float = 100.0, cost: float = 0.0, failures: list[str] | None = None, findings=None, trace_id: str = "") -> PipelineRun:
    brief = CampaignBrief(advertiser_input="x", product=ProductFacts(name="p"))
    return PipelineRun(mode=mode, brief=brief, reports=[_report(d, findings if i == 0 else None, f"v{i}") for i, d in enumerate(decisions)], total_latency_ms=latency, total_cost_usd=cost,
                       failures=[FailureEvent(code=c, stage="s", message="m", fallback="f") for c in (failures or [])], trace_id=trace_id)


def _span(name: str, kind: str, start: float, dur: float, parent: str | None = "root", status: str = "ok", **attrs) -> Span:
    s = Span(name=name, kind=kind, trace_id="t", parent_id=parent, start_ms=start, end_ms=start + dur, status=status)
    s.attributes.update(attrs)
    return s


def _trace(name: str, spans: list[Span], mode: str = "live", tid: str = "t") -> Trace:
    root = Span(name=name, kind="stage", trace_id=tid, span_id="root", start_ms=0.0, end_ms=1000.0)
    root.children = spans
    return Trace(name=name, trace_id=tid, root=root, metadata={"mode": mode})


# --- 분위수 ---------------------------------------------------------------------------------
def test_percentile_matches_linear_interpolation():
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(v, 0.5) == 3.0 and percentile(v, 0.0) == 1.0 and percentile(v, 1.0) == 5.0
    assert percentile(v, 0.95) == pytest.approx(4.8) and percentile(v, 0.99) == pytest.approx(4.96)
    assert percentile([7.0], 0.99) == 7.0 and math.isnan(percentile([], 0.5))


def test_quantiles_of_values_and_empty():
    q = Quantiles.of(range(1, 101))
    assert q.n == 100 and q.p50 == pytest.approx(50.5) and q.p95 == pytest.approx(95.05) and q.p99 == pytest.approx(99.01) and q.max == 100 and q.mean == pytest.approx(50.5)
    e = Quantiles.of([])
    assert e.n == 0 and math.isnan(e.p50) and e.to_dict()["p50"] is None


# --- 판정 분포·담당자 확인율·비용·실행 지연 -----------------------------------------------------
def test_verdict_distribution_and_human_review_rate():
    runs = [_run([Decision.APPROVE, Decision.REVISE, Decision.HUMAN_REVIEW], cost=0.3, latency=1000), _run([Decision.REJECT, Decision.HUMAN_REVIEW, Decision.HUMAN_REVIEW], cost=0.6, latency=3000)]
    m = compute_metrics(runs)
    assert m.n_runs == 2 and m.n_ads == 6
    assert m.verdicts == {"APPROVE": 1, "REVISE": 1, "REJECT": 1, "HUMAN_REVIEW": 3}
    assert m.human_review_rate == pytest.approx(0.5) and m.verdict_rates["HUMAN_REVIEW"] == pytest.approx(0.5)
    assert m.run_latency_ms.p50 == pytest.approx(2000) and m.run_latency_ms.n == 2
    assert m.run_cost_usd.total == pytest.approx(0.9) and m.cost_per_ad_usd == pytest.approx(0.15)


def test_halted_runs_with_no_reports_are_counted_but_do_not_break_rates():
    m = compute_metrics([_run([], latency=50), _run([Decision.APPROVE])])
    h = _run([])
    h.halted = True
    m2 = compute_metrics([h])
    assert m.n_runs == 2 and m.n_ads == 1 and m.human_review_rate == 0.0
    assert m2.n_halted == 1 and m2.n_ads == 0 and m2.human_review_rate is None and m2.cost_per_ad_usd is None


def test_empty_input_gives_empty_metrics_and_valid_prometheus_text():
    m = compute_metrics()
    assert m.n_runs == 0 and any("처리 기록이 없다" in c for c in m.caveats)
    _assert_valid_exposition(m.to_prometheus_text())
    json.dumps(m.to_dict(), allow_nan=False)


def test_demo_runs_are_marked_synthetic_and_live_runs_are_not():
    assert compute_metrics([_run([Decision.APPROVE], mode="demo")]).synthetic_only is True
    assert compute_metrics([_run([Decision.APPROVE], mode="partial", cost=0.1)]).synthetic_only is False
    m = compute_metrics([_run([Decision.APPROVE], mode="demo"), _run([Decision.APPROVE], mode="partial")])
    assert m.synthetic_only is False and m.modes == {"demo": 1, "partial": 1}
    assert any("대역" in c for c in compute_metrics([_run([Decision.APPROVE], mode="demo")]).caveats)


def test_small_samples_carry_a_caveat():
    assert any("크게 흔들린다" in c for c in compute_metrics([_run([Decision.APPROVE])]).caveats)


# --- 실패 모드·인용 환각 ------------------------------------------------------------------
def test_failure_mode_frequency_counts_events_and_affected_runs():
    runs = [_run([Decision.APPROVE], failures=["FM-04", "FM-04", "FM-19"]), _run([Decision.APPROVE], failures=["FM-04"]), _run([Decision.APPROVE])]
    m = compute_metrics(runs)
    assert m.failure_modes["FM-04"]["count"] == 3 and m.failure_modes["FM-04"]["runs"] == 2
    assert m.failure_modes["FM-19"]["count"] == 1 and "FM-01" not in m.failure_modes
    assert m.failure_modes["FM-04"]["name"] != "(카탈로그에 없음)"
    assert compute_metrics([_run([Decision.APPROVE], failures=["FM-9999"])]).failure_modes["FM-9999"]["name"] == "(카탈로그에 없음)"


def test_unverified_quote_rate_counts_only_model_findings():
    sem_ok = Finding(rule_id="R", severity=Severity.WARN, layer=Layer.SEMANTIC, quote="q", verified_quote=True)
    sem_bad = Finding(rule_id="R", severity=Severity.WARN, layer=Layer.SEMANTIC, quote="없는 말", verified_quote=False)
    det = Finding(rule_id="R2", severity=Severity.FIX, layer=Layer.LEXICAL, quote="x", verified_quote=True)
    m = compute_metrics([_run([Decision.REVISE, Decision.APPROVE], findings=[sem_ok, sem_bad, sem_bad, det])])
    assert m.semantic_findings == 3 and m.unverified_quotes == 2 and m.unverified_quote_rate == pytest.approx(2 / 3)
    assert compute_metrics([_run([Decision.APPROVE])]).unverified_quote_rate is None


# --- 트레이스: 단계 지연·제공자·재검사 -------------------------------------------------------
def test_stage_latency_uses_span_durations_and_skips_containers_and_llm_spans():
    spans = [_span("reviewer", "stage", 0, 100), _span("reviewer", "stage", 0, 300), _span("variant:var_1", "stage", 0, 999), _span("input_guard:intercept", "guardrail", 0, 10),
             _span("review:var_1", "llm", 0, 50, provider="openai", latency_ms=48.0, cost_usd=0.01)]
    m = compute_metrics([], [_trace("preflight", spans)])
    assert set(m.stage_latency_ms) == {"reviewer", "input_guard"}
    assert m.stage_latency_ms["reviewer"].n == 2 and m.stage_latency_ms["reviewer"].p50 == pytest.approx(200)
    assert m.llm_latency_ms["openai"].p50 == 48.0


def test_provider_calls_failures_fallback_and_circuit_skips():
    ok = _span("a", "llm", 0, 10, provider="openai", model="m", cost_usd=0.01, latency_ms=10.0, chain=["openai", "anthropic"], fallback_chain=["openai"])
    fb = _span("b", "llm", 0, 10, status="fallback", provider="anthropic", model="m", cost_usd=0.01, latency_ms=20.0, chain=["openai", "anthropic"], fallback_chain=["openai", "anthropic"])
    fb.events = [{"name": "provider_error", "provider": "openai", "type": "Timeout"}, {"name": "skip_provider", "provider": "openai", "reason": "circuit_open"}]
    dead = _span("c", "llm", 0, 10, status="error")
    dead.events = [{"name": "refusal", "provider": "openai"}, {"name": "refusal", "provider": "anthropic"}]
    m = compute_metrics([], [_trace("preflight", [ok, fb, dead])])
    o, a = m.providers["openai"], m.providers["anthropic"]
    assert (o.answered, o.failures, o.circuit_skips, o.fallback_answers) == (1, 2, 1, 0)
    assert (a.answered, a.failures, a.fallback_answers) == (1, 1, 1)
    assert o.failure_rate == pytest.approx(2 / 3) and a.fallback_rate == 1.0
    assert m.llm_calls == 2 and m.llm_fallback_calls == 1 and m.fallback_rate == 0.5
    assert m.cost_is_lower_bound is True        # 응답을 받지 못한 요청이 있어 비용은 최소값


def test_mock_provider_is_flagged_synthetic_and_latency_not_counted():
    s = _span("a", "llm", 0, 1, provider="mock", model="mock", cost_usd=0.0, latency_ms=35.1, chain=["mock"], fallback_chain=["mock"])
    m = compute_metrics([], [_trace("preflight", [s], mode="demo")])
    assert m.providers["mock"].synthetic is True and m.llm_latency_ms == {} and m.cost_is_lower_bound is False


def test_recheck_rate_counts_recheck_traces_per_ad():
    runs = [_run([Decision.APPROVE, Decision.REVISE]), _run([Decision.APPROVE, Decision.REVISE])]
    traces = [_trace("preflight", [], tid="a"), _trace("recheck", [], tid="b"), _trace("recheck", [], tid="c")]
    m = compute_metrics(runs, traces)
    assert m.n_rechecks == 2 and m.recheck_rate == pytest.approx(0.5)


def test_rewrite_checked_ads_are_counted():
    r = _run([Decision.REVISE, Decision.REVISE])
    r.reports[0].rewrite_status = RewriteStatus.VERIFIED
    assert compute_metrics([r]).rewrite_checked_ads == 1


def test_trace_only_input_still_gives_verdicts_and_run_latency():
    adj = [_span("adjudicate", "guardrail", 0, 1, final="APPROVE"), _span("adjudicate", "guardrail", 0, 1, final="HUMAN_REVIEW"), _span("adjudicate", "guardrail", 0, 1, final="HUMAN_REVIEW")]
    m = compute_metrics([], [_trace("preflight", adj, mode="partial")])
    assert m.n_runs == 1 and m.n_ads == 3 and m.human_review_rate == pytest.approx(2 / 3) and m.run_latency_ms.n == 1
    assert any("트레이스만" in c for c in m.caveats)


# --- 실제 파이프라인(예시 실행) 위에서 -------------------------------------------------------------
def test_metrics_from_a_real_demo_pipeline_run():
    from agents.pipeline import run_pipeline
    from ui.presets import PRESETS, build_brief

    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    t = Tracer()
    runs = [run_pipeline(build_brief(PRESETS[k]), s, tracer=t) for k in ("loan_ok", "commerce_landing")]
    m = metrics_from_tracer(t, runs)
    assert m.n_runs == 2 and m.n_ads == sum(len(r.reports) for r in runs) and m.synthetic_only
    assert {"reviewer", "output_guard", "adjudicate"} <= set(m.stage_latency_ms)
    assert m.providers["mock"].answered > 0 and m.providers["mock"].failures == 0
    assert sum(m.verdicts.values()) == m.n_ads
    json.dumps(m.to_dict(), allow_nan=False, ensure_ascii=False)
    _assert_valid_exposition(m.to_prometheus_text())


def test_provider_failure_from_a_real_router_is_counted():
    """제공자가 실패해 대체 경로를 탄 호출이 지표에 잡힌다 (가짜 제공자 — 네트워크 없음)."""
    from agents.pipeline import run_pipeline
    from llm.errors import Timeout
    from llm.router import Router
    from tests.fakes import FakeProvider, TaskScriptedProvider
    from ui.presets import PRESETS, build_brief

    settings = Settings.from_env(openai_api_key="k1", anthropic_api_key="k2", force_demo=False)
    t = Tracer()
    flaky = FakeProvider("openai", errors=[Timeout("t", provider="openai")] * 3)
    ok = TaskScriptedProvider("anthropic")
    router = Router(settings, providers={"openai": flaky, "anthropic": ok}, tracer=t)
    run_pipeline(build_brief(PRESETS["loan_ok"]), settings, router=router, tracer=t)
    m = metrics_from_tracer(t)
    assert m.providers["openai"].failures >= 1
    assert m.providers["anthropic"].answered >= 1 and m.providers["anthropic"].fallback_answers >= 1


# --- Prometheus 텍스트 ---------------------------------------------------------------------
_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_LABEL = r'[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\\\|\\"|\\n)*"'
_SAMPLE = re.compile(rf"^({_NAME})(\{{{_LABEL}(?:,{_LABEL})*\}})? (-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|NaN|\+Inf|-Inf)$")


def _assert_valid_exposition(text: str) -> None:
    assert text.endswith("\n") and not text.endswith("\n\n")
    declared: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("# HELP "):
            assert "\n" not in line
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            assert kind in {"counter", "gauge", "summary", "histogram", "untyped"} and name not in declared, line
            declared[name] = kind
            continue
        m = _SAMPLE.match(line)
        assert m, f"형식 오류: {line!r}"
        base = re.sub(r"_(sum|count)$", "", m.group(1))
        assert m.group(1) in declared or base in declared, f"TYPE 선언 없는 샘플: {line!r}"


def test_label_value_escaping_follows_the_exposition_format():
    assert escape_label_value('a"b') == 'a\\"b'
    assert escape_label_value("a\\b") == "a\\\\b"
    assert escape_label_value("a\nb") == "a\\nb"
    assert escape_label_value('\\"\n') == '\\\\\\"\\n'


def test_prometheus_text_escapes_hostile_label_values(monkeypatch):
    monkeypatch.setattr(metrics, "_fm_names", lambda: {"FM-04": 'bad "name"\nnewline \\ back'})
    m = compute_metrics([_run([Decision.APPROVE], failures=["FM-04"])])
    text = m.to_prometheus_text()
    _assert_valid_exposition(text)
    assert 'name="bad \\"name\\"\\nnewline \\\\ back"' in text and "\nnewline" not in text.split("preflight_failure_mode_total{", 1)[1].split("}", 1)[0]


def test_prometheus_text_for_a_realistic_snapshot_is_valid_and_has_the_main_families():
    sp = [_span("reviewer", "stage", 0, 120), _span("a", "llm", 0, 10, provider="openai", model="m", cost_usd=0.02, latency_ms=900.0, chain=["openai"], fallback_chain=["openai"])]
    m = compute_metrics([_run([Decision.APPROVE, Decision.HUMAN_REVIEW], mode="partial", cost=0.05, latency=2500, failures=["FM-19"])], [_trace("preflight", sp, mode="partial")])
    text = m.to_prometheus_text()
    _assert_valid_exposition(text)
    for fam in ("preflight_runs_total", "preflight_verdicts_total", "preflight_human_review_ratio", "preflight_stage_duration_seconds", "preflight_run_duration_seconds",
                "preflight_run_cost_usd_total", "preflight_provider_answers_total", "preflight_provider_failures_total", "preflight_failure_mode_total", "preflight_synthetic_only"):
        assert f"# TYPE {fam} " in text, fam
    assert 'preflight_verdicts_total{decision="HUMAN_REVIEW"} 1' in text
    assert 'preflight_stage_duration_seconds{stage="reviewer",quantile="0.5"} 0.12' in text
    assert 'preflight_synthetic_only 0' in text
    assert metrics_prefix_text(m, "x_y").startswith("# HELP x_y_runs_total")


def metrics_prefix_text(m: Metrics, prefix: str) -> str:
    return m.to_prometheus_text(prefix)


def test_to_dict_is_json_safe_with_nan_quantiles():
    m = compute_metrics([_run([], latency=10)])
    json.dumps(m.to_dict(), allow_nan=False)
