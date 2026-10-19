"""운영 — 처리 지표, 모니터 상태, 효과 계산기, 감사 기록 검증, 처리량 측정.

판정·계산 로직은 ops/ 와 scripts/loadtest.py 의 결과를 부르기만 한다. 이 화면은 그리기만 한다.
세션 상태 키는 모두 `opsv_` 로 시작한다.

화면에 꼭 보여야 하는 말
- 효과 계산기의 모든 결과에는 '실제 운영 효과가 아님' 이 붙는다 (측정값은 평가 사례에서 잰 것, 나머지는 사용자의 가정).
- 모니터 시뮬레이션은 시뮬레이션이다 — 실제 서비스의 기록이 아니다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import streamlit as st

from core.schemas import Decision, PipelineRun
from observability.tracer import Tracer
from ops import impact, monitors
from ops.audit import AuditLog, audit_from_run
from ops.metrics import Metrics, compute_metrics
from ui import theme
from ui.labels import DECISION_LABEL

LOADTEST_FILE = Path(__file__).resolve().parents[1] / "evals" / "reports" / "loadtest.json"
STAGE_LABEL = {"input_guard": "요청 검사", "planner": "캠페인 구성", "copywriter": "광고 초안 작성", "output_guard": "코드 검사", "audience": "가상 사용자 반응",
               "reviewer": "검토 모델 검사", "second_opinion": "다른 모델 계열 교차 확인", "rewrite_recheck": "자동 수정안 재검사", "adjudicate": "최종 판정"}
PROVIDER_LABEL = {"mock": "예시 실행(대역)", "openai": "OpenAI", "anthropic": "Anthropic"}
STATUS_LABEL = {"ok": "정상", "warn": "주의", "critical": "심각", "insufficient": "판단 보류(표본 부족)"}
KIND_LABEL = {"측정": "측정", "가정": "가정", "계산": "계산"}
KIND_HELP = "측정 = 평가 사례에서 잰 값 · 가정 = 직접 넣은 값 · 계산 = 측정과 가정으로 계산한 값"
_MD_UNSAFE = re.compile(r"[`<>*_\[\]|\\]")


def _plain(text: Any, limit: int = 300) -> str:
    """업로드한 파일에서 온 글자가 마크다운·HTML 로 해석되지 않게 특수문자를 뺀다."""
    return _MD_UNSAFE.sub(" ", str(text))[:limit]


def _num(x: Optional[float], digits: int = 1, suffix: str = "") -> str:
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x:,.{digits}f}{suffix}"


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _runs_from_session() -> list[PipelineRun]:
    out: list[PipelineRun] = []
    for e in st.session_state.get("runs", []) or []:
        run = e.get("run") if isinstance(e, dict) else None
        if isinstance(run, PipelineRun):
            out.append(run)
    return out


# ---------------------------------------------------------------------------
# ① 처리 지표
# ---------------------------------------------------------------------------
def _metrics_section(tracer: Tracer, runs: list[PipelineRun]) -> None:
    st.markdown("### 1. 이 세션의 처리 지표")
    st.caption("이 세션에서 실행한 기록만 셉니다. 서비스 전체의 값이 아닙니다.")
    traces = list(getattr(tracer, "traces", []) or [])
    if not runs and not traces:
        st.info("아직 이 세션의 처리 기록이 없습니다. ‘광고 작업’ 에서 한 번 실행하면 여기에 지표가 나타납니다.")
        return
    m: Metrics = compute_metrics(runs, traces)
    cols = st.columns(4)
    cols[0].markdown(theme.stat("실행 / 광고안", f"{m.n_runs} / {m.n_ads}", f"중간에 멈춘 실행 {m.n_halted}건"), unsafe_allow_html=True)
    hr_sub = f"{m.verdicts.get(Decision.HUMAN_REVIEW.value, 0)}건 / {m.n_ads}건" if m.n_ads else "광고안 없음"
    cols[1].markdown(theme.stat("담당자 확인으로 넘긴 비율", _pct(m.human_review_rate), hr_sub), unsafe_allow_html=True)
    cols[2].markdown(theme.stat("실행당 처리 시간 (중앙값 / 95%)", f"{_num(m.run_latency_ms.p50 / 1000 if m.run_latency_ms.n else None, 2)}초 / {_num(m.run_latency_ms.p95 / 1000 if m.run_latency_ms.n else None, 2)}초",
                                f"실행 {m.run_latency_ms.n}건 기준"), unsafe_allow_html=True)
    cost_sub = "예시 실행은 모델을 부르지 않아 0원" if m.synthetic_only else ("응답을 받지 못한 요청이 있어 최소값" if m.cost_is_lower_bound else "")
    cols[3].markdown(theme.stat("광고안당 비용", f"${m.cost_per_ad_usd:.4f}" if m.cost_per_ad_usd is not None else "—", cost_sub), unsafe_allow_html=True)
    if m.synthetic_only:
        st.info("모든 실행이 예시 실행(실제 AI 호출 없음)입니다. 비용 0원과 처리 시간은 대역의 값이며 모델의 성능이나 비용이 아닙니다.")
    for c in m.caveats:
        if "예시 실행" not in c:
            st.caption(c)
    if m.n_ads:
        st.markdown("**판정 분포**")
        st.dataframe([{"판정": DECISION_LABEL[Decision(k)], "건수": v, "비율": _pct(v / m.n_ads)} for k, v in m.verdicts.items()], width="stretch", hide_index=True)
    if m.stage_latency_ms:
        st.markdown("**단계별 처리 시간 (ms)**")
        st.dataframe([{"단계": STAGE_LABEL.get(k, k), "횟수": q.n, "중앙값": _num(q.p50, 2), "95%": _num(q.p95, 2), "99%": _num(q.p99, 2), "최대": _num(q.max, 2)} for k, q in m.stage_latency_ms.items()],
                     width="stretch", hide_index=True)
        st.caption("횟수가 100 미만이면 99% 값은 사실상 최댓값입니다.")
    if m.providers:
        st.markdown("**모델 제공자별 호출**")
        st.dataframe([{"제공자": PROVIDER_LABEL.get(k, k), "응답": p.answered, "실패": p.failures, "실패율": _pct(p.failure_rate), "대신 응답한 호출": p.fallback_answers, "서킷 차단으로 건너뜀": p.circuit_skips}
                      for k, p in m.providers.items()], width="stretch", hide_index=True)
    if m.failure_modes:
        st.markdown("**발생한 실패 유형**")
        st.dataframe([{"유형": d["name"], "건수": d["count"], "영향받은 실행": d["runs"]} for d in m.failure_modes.values()], width="stretch", hide_index=True)
    st.caption(f"고친 문구를 다시 검사한 횟수 {m.n_rechecks}건 · 자동 수정안을 재검사한 광고안 {m.rewrite_checked_ads}건"
               + (f" · 검토 모델이 낸 근거 중 원문에서 찾지 못한 비율 {_pct(m.unverified_quote_rate)}" if m.unverified_quote_rate is not None else ""))
    with st.expander("상세 기록 — 수집기용 텍스트 형식으로 보기"):
        st.caption("Prometheus 텍스트 형식입니다. 이 프로젝트에는 수집기·저장소가 없어 형식만 맞춘 것입니다.")
        st.code(m.to_prometheus_text(), language="text")


# ---------------------------------------------------------------------------
# ② 모니터 상태
# ---------------------------------------------------------------------------
def _rule_rows(results: list[monitors.RuleResult]) -> list[dict]:
    rows = []
    for r in results:
        spec = monitors.RULE_SPECS[r.rule]
        rows.append({"점검": spec.title, "상태": STATUS_LABEL[r.status], "값": "—" if r.value is None else f"{r.value:g}", "표본": r.n_window, "설명": r.message,
                     "대응 절차": f"운영 문서 {spec.runbook.lstrip('§')}절"})
    return rows


def _monitor_section(runs: list[PipelineRun], tracer: Tracer) -> None:
    st.markdown("### 2. 모니터 상태")
    src = st.radio("보는 데이터", ["sim", "session"], format_func=lambda k: {"sim": "시뮬레이션 데이터 (합성)", "session": "이 세션의 기록"}[k], key="opsv_monitor_src", horizontal=True)
    if src == "sim":
        st.warning("시뮬레이션입니다. 이 프로젝트가 정한 예시 분포로 만든 가짜 기록이며 실제 서비스의 기록이 아닙니다.", icon="🧪")
        c1, c2 = st.columns(2)
        names = list(monitors.SCENARIOS)
        pick = c1.selectbox("상황", names, format_func=lambda k: monitors.SCENARIOS[k].title, key="opsv_sim_scenario")
        seed = int(c2.number_input("난수 시드", min_value=0, value=0, step=1, key="opsv_sim_seed"))
        n_runs, change_at, base_n = 300, 180, 100
        point = st.slider("어느 시점까지 본 상태인가 (실행 번호)", min_value=base_n + 10, max_value=n_runs, value=n_runs, key="opsv_sim_point",
                          help=f"기준 구간은 처음 {base_n}건, 상황의 변화는 {change_at}번째 실행부터 시작합니다.")
        recs = monitors.generate_stream(monitors.SCENARIOS[pick], n_runs, change_at, seed)
        results = monitors.evaluate(recs[base_n:point], recs[:base_n])
        st.caption(f"기준 구간 {base_n}건 · 현재까지 {point - base_n}건 · 변화 시작 {change_at}번째" + (" (이 상황은 변화가 없습니다)" if not monitors.SCENARIOS[pick].expected else ""))
    else:
        recs = monitors.records_from_runs(runs, list(getattr(tracer, "traces", []) or []))
        if len(recs) < 2:
            st.info("이 세션에 실행이 2건 이상 있어야 앞쪽을 기준 구간으로 삼아 견줄 수 있습니다. 지금은 판단할 수 없습니다.")
            return
        half = len(recs) // 2
        results = monitors.evaluate(recs[half:], recs[:half])
        st.caption(f"앞쪽 {half}건을 기준 구간, 나머지 {len(recs) - half}건을 현재로 봤습니다. 세션 기록은 표본이 작아 대부분 ‘판단 보류’ 로 나옵니다 — 보류는 정상이 아니라 아직 모른다는 뜻입니다.")
    fired = [r for r in results if r.fired]
    if fired:
        st.error("울린 점검: " + ", ".join(f"{monitors.RULE_SPECS[r.rule].title}({STATUS_LABEL[r.status]})" for r in fired), icon="🚨")
    elif all(r.status == "insufficient" for r in results):
        st.info("모든 점검이 표본 부족으로 판단 보류입니다.")
    else:
        st.success("울린 점검이 없습니다 (표본이 모자란 점검은 보류).")
    st.dataframe(_rule_rows(results), width="stretch", hide_index=True)
    with st.expander("점검 기준과 근거 — 모두 이 프로젝트가 가정한 값입니다"):
        st.dataframe([{"점검": s.title, "최대 심각도": STATUS_LABEL[s.severity], "최소 표본": s.min_samples, "기준(근거)": s.basis, "대응 절차": f"운영 문서 {s.runbook.lstrip('§')}절"}
                      for s in monitors.RULE_SPECS.values()], width="stretch", hide_index=True)
        st.caption("용어: PSI = 판정 구성이 기준 구간과 얼마나 달라졌는지 보는 지수 · 번 레이트 = 처리 시간 약속을 어긴 실행이 허용 한도(오류 예산)를 몇 배 빠르게 쓰는지 · "
                   "윌슨 하한 = 비율의 95% 신뢰구간 아래쪽 끝(표본이 작으면 낮게 잡혀 섣부른 경보를 막는다) · 로버스트 z = 중앙값 기준으로 얼마나 벗어났는지(튀는 값에 덜 흔들림)")
        st.caption(f"처리 시간 약속(SLO)은 실행 한 번 {monitors.ASSUMED_LATENCY_SLO_MS / 1000:.0f}초 이내를 {monitors.ASSUMED_SLO_TARGET:.0%} 이상으로 가정했습니다. "
                   "실제 서비스의 약속이 아니고, 실제 모델 호출 시간 분포를 충분히 재 본 값도 아닙니다.")
    _score_block()


def _score_block() -> None:
    with st.expander("점검이 얼마나 빨리, 얼마나 정확히 알아채는지 시뮬레이션으로 재기"):
        st.caption("합성 스트림에 점검을 돌려 ‘변화를 알아채기까지 걸린 실행 수’ 와 ‘변화가 없는데 울린 횟수’ 를 셉니다. 합성 분포에서 잰 값이라 실제 서비스의 탐지 성능이 아닙니다.")
        c1, c2 = st.columns(2)
        n = int(c1.number_input("스트림 수", min_value=5, max_value=100, value=20, step=5, key="opsv_score_streams"))
        seed = int(c2.number_input("시드", min_value=0, value=0, step=1, key="opsv_score_seed"))
        if st.button("시뮬레이션 평가 실행", key="opsv_score_run"):
            with st.spinner("합성 스트림에 점검을 돌리는 중…"):
                st.session_state["opsv_score"] = monitors.simulate_and_score(n_streams=n, seed=seed, eval_every=3).to_dict()
        score = st.session_state.get("opsv_score")
        if not score:
            return
        rows = []
        for key, d in score["scenarios"].items():
            if not d["expected_rules"]:
                continue
            rows.append({"상황": monitors.SCENARIOS[key].title, "알아챈 스트림": f"{d['detected_streams']}/{d['n_streams']}",
                         "알아채기까지 (중앙값, 변화 후 실행 수)": _num(d["delay_runs_median"], 0), "90번째 백분위": _num(d["delay_runs_p90"], 0)})
        st.dataframe(rows, width="stretch", hide_index=True)
        normal = score["scenarios"].get("normal")
        if normal:
            fa = normal["false_alarm_streams"]
            st.caption(f"변화가 없는 스트림 {normal['n_streams']}개 중 한 번이라도 잘못 울린 스트림: {sum(fa.values()) if fa else 0}개(점검별 합) · 시드 {score['seed']} · 평가 간격 {score['eval_every']}회마다. "
                       "표본이 작아 오경보율은 거칠게만 알 수 있습니다.")


# ---------------------------------------------------------------------------
# ③ 효과 계산기
# ---------------------------------------------------------------------------
def _measured_table(measured: impact.Measured) -> None:
    names = {"code_only": "코드 검사만", "llm_only": "검토 모델만", "combined": "합친 방식(현재 서비스)"}
    rows = []
    for k, s in measured.systems.items():
        rows.append({"방식": names.get(k, s.label), "사례": s.n, "담당자 확인으로 넘긴 비율": _pct(s.human_review.rate),
                     "문제 있는 광고를 통과시킨 비율": f"{s.unsafe_pass.k}/{s.unsafe_pass.n}", "문제없는 광고에 불필요한 수정 요청": f"{s.unnecessary_revision.k}/{s.unnecessary_revision.n}",
                     "사례당 비용(USD)": _num(s.cost_per_case_usd, 4), "처리 시간 중앙값(초)": _num(s.latency_p50_ms / 1000 if s.latency_p50_ms is not None else None, 2)})
    st.dataframe(rows, width="stretch", hide_index=True)


def _impact_section() -> None:
    st.markdown("### 3. 효과 계산기 (가정 기반 What-if)")
    st.warning(impact.NOT_REAL_EFFECT + " 이 프로젝트에는 실제 운영 데이터가 없습니다. ‘측정’ 은 평가 사례에서 잰 값, ‘가정’ 은 직접 넣은 값입니다.", icon="⚠️")
    split = st.selectbox("측정에 쓸 사례 구간", list(impact.SPLITS), format_func=lambda k: impact.SPLIT_LABEL[k], key="opsv_split")
    measured = impact.load_measured(split=split)
    if measured.available:
        st.caption(f"측정값 출처: {', '.join(measured.sources)} · 실제 모델 {measured.model or '?'} · {measured.generated_at[:10]} 실행 · 사례 {measured.n_cases}건 (저자가 쓴 가상 광고). "
                   "라벨과 규칙을 같은 저자가 만들었고, 개발용 사례는 규칙을 고치는 데 썼습니다.")
        _measured_table(measured)
        for n in measured.notes:
            st.caption(n)
    else:
        st.info("측정 없음 — " + (measured.missing.get("담당자 확인율") or "실제 모델로 끝까지 돈 평가 결과 파일이 없습니다.") + " 아래에서 담당자 확인율을 직접 가정하면 ‘가정’ 으로 표시해 계산합니다.")
    st.markdown("**가정 입력** — " + impact.EXAMPLE_NOTE)
    ex = impact.EXAMPLE_ASSUMPTIONS
    c1, c2 = st.columns(2)
    daily = c1.number_input("하루 검수 건수", min_value=0.0, value=float(ex.daily_reviews), step=10.0, key="opsv_daily")
    m_rev = c2.number_input("사람 1건당 검수 시간(분)", min_value=0.0, value=float(ex.minutes_per_review), step=0.5, key="opsv_min_review")
    wage = c1.number_input("사람 시급(원)", min_value=0.0, value=float(ex.hourly_wage_krw), step=500.0, key="opsv_wage")
    m_flag = c2.number_input("담당자 확인 건의 처리 시간(분)", min_value=0.0, value=float(ex.minutes_per_flagged), step=0.5, key="opsv_min_flagged")
    spot = c1.number_input("자동 판단 건을 사람이 다시 보는 비율", min_value=0.0, max_value=1.0, value=float(ex.spot_check_rate), step=0.01, key="opsv_spot")
    days = c2.number_input("한 달 근무일 수", min_value=0.0, value=float(ex.workdays_per_month), step=1.0, key="opsv_days")
    fx = c1.number_input("원/달러 환율", min_value=0.0, value=float(ex.usd_krw), step=10.0, key="opsv_fx")
    spread = c2.slider("가정의 ± 폭 (범위 계산용)", min_value=0, max_value=90, value=int(ex.spread * 100), step=5, key="opsv_spread", format="%d%%",
                       help="가정마다 입력값 ± 이 폭을 낙관·비관 범위로 씁니다. 30% 는 예시이며 근거가 없습니다.")
    use_share = st.checkbox("실제 광고 중 문제 있는 광고의 비율을 가정해 다시 계산", key="opsv_use_share",
                            help="평가 사례는 문제 있는 광고가 약 2/3 입니다. 실제 광고의 구성은 다를 수 있어, 측정한 층별 비율을 이 구성으로 다시 가중합니다.")
    share = st.slider("문제 있는 광고의 비율(가정)", min_value=1, max_value=95, value=20, step=1, key="opsv_share", format="%d%%", disabled=not use_share) / 100.0
    what_if = st.checkbox("담당자 확인율을 직접 가정", key="opsv_whatif_on", help="측정 대신 이 값을 씁니다. 결과는 모두 ‘가정’ 으로 표시되고 AI 비용·위험은 계산하지 않습니다.")
    what_rate = st.slider("담당자 확인율(가정)", min_value=0, max_value=100, value=10, step=1, key="opsv_whatif_rate", format="%d%%", disabled=not what_if) / 100.0
    a = impact.Assumptions(daily_reviews=daily, minutes_per_review=m_rev, hourly_wage_krw=wage, minutes_per_flagged=m_flag, spot_check_rate=spot, workdays_per_month=days, usd_krw=fx,
                           violation_share=share if use_share else None, what_if_human_review_rate=what_rate if what_if else None, spread=spread / 100.0)
    try:
        est = impact.estimate(a, measured)
    except impact.ImpactInputError as e:
        for msg in e.messages:
            st.error(msg)
        st.caption(impact.NOT_REAL_EFFECT)
        return
    if not est.computed:
        st.info(est.headline)
        for k, v in est.unavailable.items():
            st.caption(f"측정 없음 · {k}: {v}")
        return
    st.markdown(f"**{est.headline}**")
    if est.basis:
        st.caption("담당자 확인율의 근거: " + est.basis)
    rows = []
    for it in est.items:
        rows.append({"구분": KIND_LABEL[it.kind], "항목": it.name, "기준": f"{_num(it.base, 1)} {it.unit}", "비관": _num(it.pessimistic, 1), "낙관": _num(it.optimistic, 1), "비고": it.note})
    st.caption(KIND_HELP)
    st.dataframe(rows, width="stretch", hide_index=True)
    if est.risk:
        u = est.risk["unsafe_pass"]
        txt = f"위험도 함께 보기: 이 평가에서 문제 있는 광고 {u['n']}건 중 담당자 확인 없이 통과시킨 것은 {u['k']}건입니다. 0건이어도 95% 상한은 {_pct(u['wilson95'][1])} 입니다(모든 광고에 대한 보장이 아님)."
        if "expected_unsafe_passes_per_day_upper95" in est.risk:
            txt += f" 문제 있는 광고 비율 가정에서는 하루 최대 약 {est.risk['expected_unsafe_passes_per_day_upper95']:.1f}건이 통과할 수 있다는 상한입니다."
        st.markdown(txt)
    if est.comparison:
        c = est.comparison
        st.markdown(f"검토 모델만 쓸 때 문제없는 광고에 불필요한 수정을 요청한 비율은 {c['llm_only']['k']}/{c['llm_only']['n']}({_pct(c['llm_only']['rate'])}), 합친 방식은 "
                    f"{c['combined']['k']}/{c['combined']['n']}({_pct(c['combined']['rate'])})입니다 (측정). 문제없는 광고 1,000건으로 환산하면 약 {c['per_1000_benign_fewer_requests']:.0f}건 차이이며 예측이 아닙니다.")
    for k, v in est.unavailable.items():
        st.caption(f"측정 없음 · {k}: {v}")
    for n in est.notices[1:]:
        st.caption(n)
    st.warning(impact.NOT_REAL_EFFECT, icon="⚠️")


# ---------------------------------------------------------------------------
# ④ 감사 기록 검증
# ---------------------------------------------------------------------------
def _session_audit(runs: list[PipelineRun]) -> tuple[AuditLog, bool]:
    """(감사 기록, 이 세션 기록으로 만든 것인지). 세션에 실행이 없으면 합성한 예시 기록을 돌려준다."""
    if runs:
        log = AuditLog()
        for r in runs:
            audit_from_run(r, log, actor="session")
        return log, True
    log = AuditLog()
    for i, (et, actor) in enumerate([("run.executed", "example"), ("ad.judged", "example"), ("human.review_requested", "example"), ("human.review_decided", "reviewer-1")]):
        log.append(et, actor, {"example": True, "step": i})
    return log, False


def tamper(text: str, kind: str) -> str:
    """내보낸 JSONL 을 일부러 망가뜨린다 (탐지 데모용). none · modify · delete · reorder · truncate."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if kind == "modify" and lines:
        mid = len(lines) // 2
        d = json.loads(lines[mid])
        d["actor"] = str(d.get("actor", "")) + "x"                   # 해시는 그대로 두고 내용만 바꾼다
        lines[mid] = json.dumps(d, ensure_ascii=False, sort_keys=True)
    elif kind == "delete" and len(lines) > 2:
        del lines[len(lines) // 2]
    elif kind == "reorder" and len(lines) > 2:
        lines[1], lines[2] = lines[2], lines[1]
    elif kind == "truncate" and len(lines) > 1:
        lines = lines[:-1]
    return "\n".join(lines) + "\n"


def _show_verify(res: Any) -> None:
    if res.ok:
        st.success(_plain(res.reason))
    else:
        st.error(f"변조 또는 손상이 의심됩니다 — {_plain(res.reason)}" + (f" (처음 틀어진 위치: 항목 {res.first_bad_index})" if res.first_bad_index is not None else ""))


def _audit_section(runs: list[PipelineRun]) -> None:
    st.markdown("### 4. 감사 기록 검증")
    st.caption("감사 기록은 항목마다 앞 항목의 지문(해시)을 이어 붙여, 중간을 고치거나 지우면 처음 틀어진 곳을 알려 줍니다. 비밀·개인정보 패턴은 저장 전에 가립니다. "
               "단, 끝을 잘라 낸 것은 마지막 지문을 따로 보관해 두었을 때만 알 수 있고, 처음부터 다시 계산해 쓴 기록은 알아채지 못합니다.")
    log, from_session = _session_audit(runs)
    text = log.export_jsonl()
    st.caption(("이 세션의 실행으로 만든 감사 기록" if from_session else "세션에 실행이 없어 합성한 예시 기록") + f" · 항목 {len(log)}개 · 마지막 지문 {log.head_hash[:16]}…")
    st.download_button("감사 기록 내려받기 (.jsonl)", data=text, file_name="preflight_audit.jsonl", mime="application/json", key="opsv_audit_dl")
    st.markdown("**올려서 검증하기**")
    up = st.file_uploader("내려받은 감사 기록(JSONL)", type=["jsonl", "json", "txt"], key="opsv_upload")
    c1, c2 = st.columns(2)
    expected = c1.text_input("따로 보관한 마지막 지문(선택)", key="opsv_expected_hash", help="입력하면 끝이 잘렸는지도 확인합니다.")
    hkey = c2.text_input("키(선택, 키로 만든 기록일 때만)", type="password", key="opsv_hmac_key")
    if up is not None:
        raw = up.getvalue()
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            st.error("글자(UTF-8)로 읽을 수 없는 파일입니다.")
        else:
            _show_verify(AuditLog.verify_jsonl(body, key=hkey.encode() if hkey else None, expected_last_hash=expected.strip() or None))
    st.markdown("**변조 예시로 확인하기**")
    kind = st.selectbox("위 기록을 이렇게 망가뜨렸다고 가정", ["none", "modify", "delete", "reorder", "truncate"], key="opsv_tamper",
                        format_func=lambda k: {"none": "그대로", "modify": "중간 항목의 내용을 바꿈", "delete": "중간 항목을 지움", "reorder": "두 항목의 순서를 바꿈", "truncate": "마지막 항목을 잘라 냄"}[k])
    res = AuditLog.verify_jsonl(tamper(text, kind), expected_last_hash=log.head_hash if kind == "truncate" else None)
    if kind == "truncate":
        st.caption("마지막 항목을 잘라 낸 기록은 그 자체로는 멀쩡한 체인입니다. 마지막 지문을 따로 보관해 대조한 경우로 보여 드립니다.")
    _show_verify(res)


# ---------------------------------------------------------------------------
# ⑤ 처리량 측정
# ---------------------------------------------------------------------------
def load_loadtest(path: Optional[Path] = None) -> Optional[dict]:
    try:
        d = json.loads((path or LOADTEST_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("kind") == "loadtest" else None
    except (OSError, ValueError):
        return None


def _loadtest_section() -> None:
    st.markdown("### 5. 처리량 측정 결과")
    rep = load_loadtest()
    if rep is None:
        st.info("저장된 처리량 측정 결과가 없습니다. 터미널에서 `python scripts/loadtest.py` 를 실행하면 evals/reports/loadtest.json 이 만들어집니다.")
        return
    env = rep.get("environment", {})
    st.warning("모델 호출이 없는 대역 파이프라인과 코드 검사로 잰 값입니다. 네트워크·모델 지연이 빠져 있어 ‘우리 코드만의 처리량’ 이며, 실제 서비스의 처리량이 아닙니다.", icon="⚠️")
    st.caption(f"측정 환경: 파이썬 {env.get('python')} · CPU {env.get('cpu_model')} ({env.get('cpu_count')}개) · {str(rep.get('generated_at', ''))[:10]} · 반복 {rep['config']['repeats']}회, 반복당 {rep['config']['n_per_repeat']}건, 예열 {rep['config']['warmup_calls']}건")
    names = {"guardrails": "코드 검사", "pipeline_mock": "예시 실행 1회"}
    st.caption("코드 검사 = 표현·안내 문구·상품 정보 일치 등을 보는 검사 한 번. 예시 실행 1회 = 대역으로 돌리는 전체 처리 흐름 한 번(모델 호출 없음).")
    rows = []
    for key, w in rep.get("workloads", {}).items():
        for r in w["results"]:
            lat = r["latency_ms"]
            rows.append({"작업": names.get(key, key), "스레드 수": r["concurrency"], "초당 처리 건수(중앙값)": _num(r["throughput_per_s_median"], 1), "1건 처리 시간 중앙값(ms)": _num(lat["p50"], 2),
                         "95%(ms)": _num(lat["p95"], 2), "99%(ms)": _num(lat["p99"], 2), "오류": r["errors"]})
    st.dataframe(rows, width="stretch", hide_index=True)
    mem = []
    for key, w in rep.get("workloads", {}).items():
        m = w["memory"]
        mem.append({"작업": names.get(key, key), "구간별 남은 파이썬 객체(KB)": " → ".join(f"{x:,.0f}" for x in m["retained_after_each_third_kb"]),
                    "뒤 구간 건당 증가(바이트)": _num(m["growth_per_call_second_to_last_third_bytes"], 0), "최대 메모리 증가(KB)": _num(m.get("rss_peak_growth_kb"), 0)})
    st.dataframe(mem, width="stretch", hide_index=True)
    with st.expander("이 측정이 말하지 않는 것"):
        for t in rep.get("what_this_is_not", []):
            st.markdown(f"- {t}")


# ---------------------------------------------------------------------------
def render(tracer: Tracer, server_status: dict) -> None:
    """운영 화면. tracer 는 세션의 트레이서(처리 기록), server_status 는 서버 키 설정 여부 — 이 화면은 읽기만 하고 모델을 부르지 않는다."""
    st.markdown("## 운영")
    st.markdown("운영에 필요한 장치(지표·모니터·감사 기록·효과 계산)를 구현하고 시뮬레이션으로 확인한 화면입니다. "
                "**이 프로젝트에는 실제 서비스의 운영 데이터가 없습니다.** 여기 나오는 값은 이 세션의 기록, 합성 데이터, 저장된 평가 결과, 직접 넣은 가정입니다.")
    runs = _runs_from_session()
    _metrics_section(tracer, runs)
    _monitor_section(runs, tracer)
    _impact_section()
    _audit_section(runs)
    _loadtest_section()
