"""방식 비교 — 코드 검사만 · 검토 모델만 · 합친 방식. 저장된 결과만 보여 주며 보는 것만으로는 모델을 부르지 않는다."""
from __future__ import annotations

import streamlit as st

from evals.compare import CONTROLLED_NAME, INCOMPLETE_NAME, LIVE_NAME, SYSTEM_LABEL, compare_allowed, estimate
from evals.runner import load_report
from ui.labels import RUN_MODE_NAME, pct

SPLIT_LABEL = {"dev": "개발용 사례 (규칙·프롬프트를 고치는 데 쓴 사례)", "holdout": "최종 확인용 사례 (규칙을 고친 뒤 처음 쓴 사례)"}
# 완료되지 않은 실행의 사유 코드(evals.compare.incomplete_reasons) → 화면 문구
REASON_LABEL = {
    "cost_cap_reached": "비용 상한에 닿아 멈춤", "cost_over_cap": "모든 사례를 마쳤지만 비용이 상한을 넘음", "consecutive_failures": "모델 호출 실패가 연속되어 멈춤",
    "consecutive_fallback": "다른 제공자가 연속으로 판정해 멈춤", "failed_rows": "모델 호출이 실패한 행이 있음", "mixed_provider": "기준 제공자가 아닌 제공자가 판정한 행이 있음",
    "partial_scope": "승인된 전체 범위를 실행하지 않음", "cases_not_run": "실행하지 못한 사례가 남음", "aborted": "중간에 멈춤",
}


def _row(name: str, m: dict) -> dict:
    cost = f"${m['cost_usd']}" + (" 이상" if m.get("cost_is_lower_bound") else "")
    return {"방식": name, "잘못 통과한 광고": pct(m["unsafe_pass"]["k"], m["unsafe_pass"]["n"]), "올바른 통과": pct(m["correct_pass"]["k"], m["correct_pass"]["n"]),
            "불필요한 수정 요청": pct(m["unnecessary_revision"]["k"], m["unnecessary_revision"]["n"]), "담당자 확인": f"{m['human_review']['k']}/{m['human_review']['n']}",
            "모델 호출": m["calls"], "시간": f"{m['latency_ms_total'] / 1000:.1f}초", "비용": cost, **({"실패한 호출": m["failed"]} if m.get("failed") else {})}


def _table(rep: dict, split: str) -> None:
    o = rep["overall_by_split"].get(split)
    if not o:
        return
    st.markdown(f"**{SPLIT_LABEL.get(split, split)}** — {', '.join(o['datasets'])}")
    rows = [_row(SYSTEM_LABEL[s], o[s]) for s in rep["systems"]]
    if o.get("llm_only_with_tau"):
        rows.insert(2, _row("검토 모델만 + 확신 기준(τ)", o["llm_only_with_tau"]))
    t = o["_trivial"]
    for name, b in t.items():
        rows.append({"방식": f"(참고) {name}", "잘못 통과한 광고": pct(b["unsafe_pass"]["k"], b["unsafe_pass"]["n"]), "올바른 통과": pct(b["correct_pass"]["k"], b["correct_pass"]["n"]),
                     "불필요한 수정 요청": pct(b["unnecessary_revision"]["k"], b["unnecessary_revision"]["n"]), "담당자 확인": f"{b['human_review']['k']}/{b['human_review']['n']}", "모델 호출": 0, "시간": "—", "비용": "—"})
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("‘담당자 확인’ 은 ‘올바른 통과’ 에 들어가지 않습니다. 전부 담당자 확인으로 넘기면 잘못 통과한 광고는 0 이어도 올바른 통과도 0 입니다. "
               "‘코드 검사만’ 행은 코드 규칙이 하나도 걸리지 않으면 통과로 센 값이고, 실제 서비스는 코드 혼자 통과를 확정하지 않습니다(검사 사례의 ‘코드 검사만’ 표는 확정하지 않는 운영 방식).")


def _report(rep: dict) -> None:
    s = rep["settings"]
    st.caption(f"실행 {str(rep['generated_at'])[:10]} · 실행 방식 {RUN_MODE_NAME.get(s['mode'], s['mode'])} · 모델 {rep.get('model') or s['openai_model']} · 검토 프롬프트 {s['reviewer_prompt_version']} · "
               f"비교용 프롬프트 {s['llm_only_prompt_version']} · 검사 기준 {s['policy_version']} · 자동 판정 확신 기준 {s['threshold']}")
    _provider_line(rep)
    if rep["run_kind"] == "controlled":
        st.warning(rep["run_kind_note"], icon="⚠️")
        for k, sys in rep["systems"].items():
            if sys.get("controlled_note"):
                st.caption(f"{sys['label']}: {sys['controlled_note']}")
    else:
        st.info(rep["run_kind_note"], icon="ℹ️")
    for split in ("dev", "holdout"):
        _table(rep, split)
    if rep.get("caveats"):
        with st.expander("이 비교를 읽을 때 알아야 할 것"):
            for c in rep["caveats"]:
                st.markdown(f"- {c}")
    f = rep["failures"]
    if f["n"]:
        st.warning(f"모델 호출 실패 {f['n']}건이 섞여 있습니다 ({f['by_error']}). 실패는 통과·담당자 확인으로 세지 않고 따로 셉니다.", icon="⚠️")
    with st.expander("사례 종류별 (평균에 가려진 차이)"):
        for split, o in rep["overall_by_split"].items():
            rows = []
            for d, by in o.get("by_difficulty", {}).items():
                for sname, m in by.items():
                    rows.append({"구분": split, "사례 종류": d, **_row(SYSTEM_LABEL.get(sname, sname), m)})
            if rows:
                st.dataframe(rows, width="stretch", hide_index=True)
    with st.expander("방식이 갈린 대표 사례 (성공·실패)"):
        for title, items in rep.get("examples", {}).items():
            st.markdown(f"**{title}**" + ("" if items else " — 없음"))
            for it in items:
                st.markdown(f"- `{it['dataset']}:{it['case_id']}` 정답 {it['label']} — " + " / ".join(f"{SYSTEM_LABEL[k]} {v['predicted']}" for k, v in it.items() if k in SYSTEM_LABEL))


def _provider_line(rep: dict) -> None:
    """누가 판정했는가 — 기준 제공자가 아닌 쪽이 판정한 행이 있으면 숨기지 않고 적는다."""
    served = rep.get("served_by")
    if not served or rep["run_kind"] == "controlled":
        return
    st.caption("판정한 제공자: " + " · ".join(f"{p} {k}회" for p, k in served.items()) + f" (기준 제공자 {rep.get('provider', '?')})")
    if rep.get("repaired_by"):
        st.caption("형식만 다른 제공자가 고친 응답(판단은 위 제공자): " + " · ".join(f"{p} {k}회" for p, k in rep["repaired_by"].items()))
    mixed = (rep.get("mixed_provider") or {}).get("n", 0)
    if mixed:
        st.warning(f"기준 제공자가 아닌 제공자가 판정한 행이 {mixed}건 있습니다 — 그 행은 이 모델의 측정이 아닙니다.", icon="⚠️")


def _incomplete(rep: dict) -> None:
    """끝까지 마치지 못한 실제 모델 실행 — 측정한 사례만 보여 주며 세 방식 비교의 결과로 읽히지 않게 따로 둔다."""
    st.markdown("### 완료되지 않은 실행 — 측정한 사례만의 수치 (세 방식 비교의 결과가 아님)")
    st.error(rep.get("incomplete_note") or "이 실행은 끝까지 마치지 못했습니다. 측정한 사례만의 수치이며 세 방식 비교의 결과가 아닙니다.")
    reasons = rep.get("incomplete_reason") or []
    if reasons:
        st.markdown("**완료되지 않은 이유**\n" + "\n".join(f"- {REASON_LABEL.get(c, c)}" for c in reasons))
    counts = rep.get("case_counts") or {}
    if counts:
        st.dataframe([{"데이터": ds, "마친 사례": c["completed"], "전체 사례": c["total"], "호출이 실패한 행": c.get("failed_rows", 0)} for ds, c in counts.items()], width="stretch", hide_index=True)
    cost = rep.get("cost") or {}
    if cost:
        cap = f" · 상한 ${cost['cap_usd']:.2f}" if cost.get("cap_usd") is not None else ""
        st.caption(f"실행 {str(rep['generated_at'])[:10]} · 쓴 비용 ${cost.get('spent_usd', 0):.2f}{cap} · 모델 {rep.get('model', '?')}")
    _provider_line(rep)
    with st.expander("측정한 사례만의 수치 — 세 방식 비교의 결과로 읽지 마세요"):
        st.caption("아래 표는 이 실행이 마친 사례만 센 값입니다. 사례 수가 데이터마다 다르고 일부 데이터는 아예 없을 수 있어 방식끼리 비교하는 근거가 되지 않습니다.")
        for split in ("dev", "holdout"):
            _table(rep, split)


def _provenance(rep: dict) -> None:
    st.markdown("**비교에 쓴 데이터의 출처**")
    rows = []
    for ds, info in rep["datasets"].items():
        rows.append({"데이터": ds, "건수": info["n"], "구분": "최종 확인용" if info.get("split") == "holdout" else "개발용", "작성자": info.get("author", "?"), "라벨 근거": info.get("label_basis", "?"),
                     "사람 확인": info.get("human_review", "?"), "규칙 수정에 사용": "예" if info.get("used_for_tuning") else "아니오", "독립된 실제 데이터": "아니오" if not info.get("independent_real_data") else "예",
                     "버전·해시": f"v{info.get('dataset_version') or '?'} · {str(info.get('dataset_sha256', ''))[:8]}"})
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("모든 라벨은 규칙을 쓴 같은 저자가 달았고 독립 검증자는 없습니다. 개발용 사례의 수치는 고친 뒤 다시 잰 값이라 최종 성능이 아닙니다. 다른 사례를 바꿔 쓴 데이터는 독립 데이터가 아닙니다.")


def _changes(rep: dict) -> None:
    with st.expander("라벨 변경 이력과 G-069 효과 분리"):
        # 라벨 값이 실제로 바뀐 사례만 (기대 규칙·분류만 바뀐 사례는 점수를 바꾸는 라벨 변경이 아니다)
        ch = [c for c in rep.get("label_changes", []) if c.get("label_changed")]
        if ch:
            st.markdown("**라벨이 바뀐 사례** — 라벨을 바꾸면 점수가 바뀌므로 변경 전 값과 이유를 남깁니다.")
            st.dataframe([{"데이터": c["dataset"], "사례": c["case_id"], "버전": c["label_version"], "그때 라벨": c["label_then"], "이유": c["reason_then"]} for c in ch], width="stretch", hide_index=True)
        eff = rep.get("g069_effects")
        if eff:
            st.markdown("**G-069 — 모델·코드가 바뀐 효과와 라벨이 바뀐 효과를 가름** (저장된 4차·5차 판정을 같은 라벨 두 벌로 다시 채점)")
            st.dataframe([{"회차": r["round"], "G-069 판정": r["g069_predicted"], "잡은 규칙": ", ".join(r["g069_rules"]), "문제없는 광고에 수정 요청 (옛 라벨 v1.1)": r["under_v1.1_label"]["unnecessary_revision"],
                           "문제없는 광고에 수정 요청 (새 라벨 v1.2)": r["under_v1.2_label"]["unnecessary_revision"]} for r in eff["rows"]], width="stretch", hide_index=True)
            st.caption(eff["reading"])


def _load_sources() -> tuple[dict | None, dict | None, dict | None, list[str]]:
    """저장된 세 종류의 결과를 이름과 내용이 맞는 것만 그 자리에 올린다 — 대역 결과·완료되지 않은 실행이 ‘실제 모델 결과’ 자리에 오르지 않게."""
    live, ctrl, inc = load_report(LIVE_NAME), load_report(CONTROLLED_NAME), load_report(INCOMPLETE_NAME)
    notes: list[str] = []
    if live and not (live.get("run_kind") == "live" and compare_allowed(live)[0]):
        notes.append(f"{LIVE_NAME} 의 내용이 ‘끝까지 마친 실제 모델 결과’ 가 아니라서(실행 종류 {live.get('run_kind')}) 실제 모델 결과로 보여 주지 않습니다.")
        live = None
    if ctrl and ctrl.get("run_kind") != "controlled":
        notes.append(f"{CONTROLLED_NAME} 의 내용이 대역 결과가 아니라서(실행 종류 {ctrl.get('run_kind')}) 보여 주지 않습니다.")
        ctrl = None
    if inc and inc.get("run_kind") != "live_incomplete":
        notes.append(f"{INCOMPLETE_NAME} 의 내용이 ‘완료되지 않은 실행’ 이 아니라서(실행 종류 {inc.get('run_kind')}) 보여 주지 않습니다.")
        inc = None
    return live, ctrl, inc, notes


def render() -> None:
    st.markdown("## 방식 비교")
    st.markdown("같은 고정 사례를 **코드 검사만**, **검토 모델만**, **둘을 합친 방식**(현재 서비스)으로 판정해 나란히 봅니다. 숫자는 이 데이터·이 설정에서 관측된 값이며, "
                "시간이나 심사 승인율이 얼마나 나아진다는 주장이 아닙니다.")
    live, ctrl, inc, notes = _load_sources()
    for n in notes:
        st.warning(n, icon="⚠️")
    any_rep = live or inc or ctrl
    if any_rep:
        with st.expander("각 방식이 받는 정보", expanded=False):
            names = list(any_rep["systems"].keys())
            keys = list(any_rep["systems"][names[0]]["info_given"].keys())
            st.dataframe([{"받는 정보": k, **{SYSTEM_LABEL[n]: any_rep["systems"][n]["info_given"][k] for n in names}} for k in keys], width="stretch", hide_index=True)
    if live:
        st.markdown("### 실제 모델 결과 (끝까지 마친 실행)")
        _report(live)
    else:
        e = estimate(["golden", "holdout", "contrast_g069", "conditions"], ("code_only", "llm_only", "combined"))
        wait = "끝까지 마친 실행이 없습니다 — 아래 ‘완료되지 않은 실행’ 은 이 비교의 결과가 아닙니다" if inc else "실행 승인 대기"
        cost = f"추정 비용 ${e['cost_range_usd']['low']:.2f} ~ ${e['cost_range_usd']['high']:.2f} (중간 ${e['cost_range_usd']['central']:.2f}, 상한 ${e['cap_usd']:.2f})" if e.get("cost_range_usd") else "추정 비용은 계산하지 못했습니다"
        st.info(f"**실제 모델로 돌린 비교 결과는 아직 없습니다** ({wait}). 실행하면 모델 호출 약 {e['total_calls']}회, {cost} 입니다 ({e['cases']}건 × 세 방식, 모두 추정). "
                "코드 검사만의 결과는 아래에서 이미 볼 수 있습니다.", icon="⏳")
        for w in e.get("warnings", []):
            st.warning(w, icon="⚠️")
    if inc:
        _incomplete(inc)
    if ctrl:
        with st.expander("하네스 검증용 — 대역(통제된 응답)으로 돌린 결과 · 모델 품질 아님", expanded=not live and not inc):
            _report(ctrl)
    if any_rep:
        _provenance(any_rep)
        _changes(any_rep)
    else:
        st.info("저장된 비교 결과가 없습니다. `python -m evals.compare` 로 만들 수 있습니다.")
