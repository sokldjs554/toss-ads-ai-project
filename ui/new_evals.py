"""새 기능 평가 — 대화형 설정·상품 페이지 이해·이미지 검사·소재 퀄리티·성과 리포트의 저장된 평가 결과를 개발용과 최종 확인용으로 나란히 보여 준다.

이 화면은 저장된 리포트(evals/reports/*.json)를 읽기만 한다. 숫자를 새로 계산하지 않고, 리포트에 없는 숫자를 쓰지 않는다.
모든 값은 합성 데이터·사례에서 잰 것이다. 규칙 검사와 실제 모델 호출 결과를 구분하며 실제 광고 데이터의 정확도는 아니다.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Callable, Optional

import streamlit as st

REPORTS = Path(__file__).resolve().parents[1] / "evals" / "reports"


def _load(name: str) -> Optional[dict]:
    try:
        return json.loads((REPORTS / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pct(x: Any) -> str:
    return f"{float(x) * 100:.1f}%" if isinstance(x, (int, float)) else "—"


def _frac(k: Any, n: Any) -> str:
    if not isinstance(k, (int, float)) or not isinstance(n, (int, float)) or not n:
        return "—"
    return f"{int(k)}/{int(n)} ({_pct(k / n)})"


def _kn(d: Any) -> str:
    """{'k': 4, 'n': 4} 꼴."""
    return _frac(d.get("k"), d.get("n")) if isinstance(d, dict) else "—"


def _str_frac(s: Any) -> str:
    """'24/25' 꼴 문자열."""
    try:
        k, n = str(s).split("/")
        return _frac(int(k), int(n))
    except (ValueError, TypeError):
        return "—"


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _two(split: dict[str, Optional[dict]], fn: Callable[[dict], str]) -> tuple[str, str]:
    def one(s: Optional[dict]) -> str:
        try:
            return fn(s) if isinstance(s, dict) else "—"
        except (KeyError, TypeError, ValueError, AttributeError):
            return "—"

    return one(split.get("dev")), one(split.get("holdout"))


def _table(rows: list[tuple[str, ...]], first: str = "개발용", second: str = "최종 확인용", third: Optional[str] = None) -> None:
    headers = ["지표", first, second] + ([third] if third is not None else [])
    head = "".join(f"<th style='text-align:left'>{html.escape(label)}</th>" for label in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>" for row in rows)
    st.markdown("<div style='overflow-x:auto'><table class='pf-tbl' style='width:100%;border-collapse:collapse;font-size:.92rem'>"
                f"<thead><tr>{head}</tr></thead>"
                f"<tbody>{body}</tbody></table></div>", unsafe_allow_html=True)


def _notes(lines: list[str], repro: str = "") -> None:
    for ln in lines:
        st.markdown(f"- {ln}")
    if repro:
        st.caption(f"다시 재는 방법: `{repro}`")


def _missing(name: str) -> None:
    st.info(f"저장된 평가 결과({name})를 찾지 못했습니다. 아래 ‘다시 재는 방법’으로 만들 수 있습니다.")


# --- 기능별 -------------------------------------------------------------------
def rows_dialogs(rep: dict) -> list[tuple[str, str, str]]:
    sp = rep.get("splits", {})
    return [
        ("모든 항목을 통과한 대화", *_two(sp, lambda s: _frac(s["dialogs_all_ok"], s["n_dialogs"]))),
        ("말에서 값을 읽은 정확도", *_two(sp, lambda s: _pct(s["slot_micro_accuracy"]))),
        ("준비가 됐는지 판단한 정확도", *_two(sp, lambda s: _pct(s["ready_accuracy"]))),
        ("이미 말한 것을 다시 물은 비율", *_two(sp, lambda s: _frac(*s["unnecessary_reask_counts"]))),
        ("꼭 물어야 할 것을 놓친 비율", *_two(sp, lambda s: _frac(*s["missed_ask_counts"]))),
        ("금지 값 검사에서 값이 섞인 대화", *_two(sp, lambda s: f"{s['injection_contaminated']}/{s['injection_dialogs']}")),
        ("숨은 지시문 공격에서 값이 섞인 대화", *_two(sp, lambda s: f"{s['attack_contaminated']}/{s['attack_dialogs']}")),
    ]


def _dialogs() -> None:
    regression = _load("dialogs_regression_v2_20261021.json")
    held = _load("dialogs_holdout_20261020.json")
    followup = _load("dialogs_holdout_regression_20261020.json")
    historical = _load("dialogs_controlled.json")
    if not any((regression, held, historical)):
        return _missing("dialogs_regression_v2_20261021.json")
    if regression:
        st.markdown("**알려진 실패를 수정한 뒤 다시 실행한 66개 대화**")
        _table(rows_dialogs(regression), first="기존 개발 사례", second="기존 최종 확인 사례 재검사")
    if held:
        st.markdown("**별도 작성한 20개 대화의 첫 결과**")
        _table(rows_dialogs(held), first="개발 사례", second="처음 평가한 결과")
        st.caption("세 사례는 정책 경고를 기대값에서 빠뜨리거나 연령 조건을 다르게 해석한 경우입니다. 원래 점수를 보존했습니다. 금지 값 검사에는 일반 수정 사례도 포함되므로 공격 사례 수와 다릅니다.")
    if followup:
        st.markdown("**새로 드러난 실패를 수정한 뒤 재검사**")
        _table(rows_dialogs(followup), first="개발 사례", second="같은 20개 대화 재검사")
    if historical:
        with st.expander("수정 전 대화 평가 기록"):
            _table(rows_dialogs(historical))
    _notes(["이 표는 **규칙 추출기와 대역 제공자**를 실행한 합성 평가입니다. 실제 모델 실행은 별도 기록에서 확인합니다.",
            "실패를 보고 수정한 사례는 회귀 검사입니다. 새 입력에서의 일반화 성능이나 독립적인 사람의 검증을 뜻하지 않습니다.",
            "지시문을 발견하면 같은 턴의 나머지를 격리하고, 정상 요청은 다음 턴에서 다시 받을 수 있습니다. 새로운 우회 표현까지 모두 막는다는 뜻은 아닙니다."],
           str((followup or regression or held or historical).get("repro") or ""))


def rows_urls(rep: dict) -> list[tuple[str, str, str]]:
    sp = rep.get("by_split", {})
    return [
        ("페이지 수", *_two(sp, lambda s: str(s["pages"]))),
        ("상품 이름 정확도", *_two(sp, lambda s: _pct(s["name_accuracy"]))),
        ("가격 정확도", *_two(sp, lambda s: _pct(s["price_accuracy"]))),
        ("사실 후보 재현율", *_two(sp, lambda s: _pct(s["fact_recall"]))),
        ("사실 후보 정밀도", *_two(sp, lambda s: _pct(s["fact_precision"]))),
        ("화면에 안 보이는 글이 사실로 새어 든 수", *_two(sp, lambda s: f"{s['leak_count']}/{s['forbidden_total']}")),
        ("바꿔 쓴 지시문이 새어 든 수(알려진 한계)", *_two(sp, lambda s: f"{s['known_gap']['leaked']}/{s['known_gap']['total']}")),
    ]


def _urls() -> None:
    current = _load("url_ingest_followup_20261020.json")
    historical = _load("url_ingest_eval.json")
    if not current and not historical:
        return _missing("url_ingest_eval.json")
    if current:
        st.markdown("**수정 후 같은 페이지 재검사**")
        _table(rows_urls(current), first="기존 개발 페이지", second="기존 최종 확인 페이지 재검사")
        st.caption("이미 확인한 페이지와 기대값으로 수정 후 다시 실행한 회귀 검사입니다. 새 독립 평가가 아닙니다.")
        if historical:
            with st.expander("처음 저장한 페이지 평가 기록"):
                _table(rows_urls(historical))
    elif historical:
        _table(rows_urls(historical))
    rep = current or historical
    _notes(["합성 페이지 위의 값이며 실제 쇼핑몰 페이지의 정확도가 아닙니다. 페이지와 기대값을 같은 사람이 만들었습니다.",
            "읽어 온 내용은 사용자가 직접 골라 확인하기 전에는 광고 사실이 되지 않습니다. 스크립트로만 생성되는 내용은 읽지 못합니다."],
           str(rep.get("reproduce") or "python -m evals.url_ingest_eval"))


def rows_images(rep: dict) -> list[tuple[str, str, str]]:
    sp = rep.get("by_split", {})
    return [
        ("사례 수", *_two(sp, lambda s: str(s["n"]))),
        ("기대한 결과를 모두 충족한 사례", *_two(sp, lambda s: _kn(s["all_expectations_met"]))),
        ("규칙을 제대로 찾아낸 사례", *_two(sp, lambda s: _kn(s["rule_detection"]))),
        ("문제 있는 이미지를 통과시킨 수", *_two(sp, lambda s: f"{s['bad_pass']['k']}/{s['bad_pass']['n']}")),
        ("읽지 못한 이미지를 통과시킨 수", *_two(sp, lambda s: f"{s['unreadable_pass']['k']}/{s['unreadable_pass']['n']}")),
    ]


def _images() -> None:
    rep = _load("image_eval.json")
    if not rep:
        return _missing("image_eval.json")
    _table(rows_images(rep))
    _notes(["이 표는 **합성 입력과 대역으로 검사 경로를 확인한 결과**입니다. 실제 모델의 이미지 읽기 결과는 별도 실행 기록에서 확인합니다. 사용자가 적은 글과 맞춰 보기 전에는 통과가 되지 않고, 읽지 못한 이미지는 통과가 아닙니다.",
            "100%에 가까운 값은 사례가 적고 규칙과 사례를 같은 사람이 만들었기 때문입니다 — 강한 증거가 아닙니다. 배너 크기·여백·글자 크기는 이 프로젝트가 가정한 값입니다."],
           str((rep.get("repro") or "python -m evals.image_eval")))


def rows_quality(rep: dict) -> list[tuple[str, str, str]]:
    sp = rep.get("splits", {})
    return [
        ("선호가 갈린 쌍에서 작성자 기준과 일치", *_two(sp, lambda s: _kn(s["decisive_agreement"]))),
        ("무승부까지 포함한 세 갈래 정확도", *_two(sp, lambda s: _kn(s["three_way_accuracy"]))),
        ("비교: 긴 쪽이 이긴다고 본 경우", *_two(sp, lambda s: _kn(s["baselines"]["longer_text_wins"]))),
        ("비교: 숫자가 많은 쪽이 이긴다고 본 경우", *_two(sp, lambda s: _kn(s["baselines"]["more_digits_wins"]))),
    ]


def _quality() -> None:
    rep = _load("quality_signals.json")
    if not rep:
        return _missing("quality_signals.json")
    _table(rows_quality(rep))
    _notes(["소재 퀄리티 신호는 **문구를 규칙으로 읽은 참고 점수**이며 심사 판정과 별개입니다. 품질이 높아도 위반은 통과되지 않습니다.",
            "실제 클릭률·전환과 얼마나 같이 움직이는지는 측정하지 않았습니다. 선호 라벨은 AI 작성자의 판단이고 독립적인 사람의 정답이 아닙니다.",
            "개발용은 신호를 만들 때 본 쌍이라 낙관적이고, 최종 확인용도 같은 작성자가 쓴 가상 문구입니다. 실제 모델의 선택과 일치해도 광고 성과를 증명하지는 않습니다."],
           str(rep.get("reproduce") or "python -m evals.quality_signals"))


def rows_insights(rep: dict) -> list[tuple[str, str, str]]:
    sp = rep.get("splits", {})
    return [
        ("시나리오 수", *_two(sp, lambda s: str(s["n_scenarios"]))),
        ("이상을 찾아낸 비율(재현율)", *_two(sp, lambda s: _str_frac(s["detection_recall_n"]))),
        ("찾은 것 중 실제 이상인 비율(정밀도)", *_two(sp, lambda s: _str_frac(s["detection_precision_n"]))),
        ("이상이 없는데 이상이라고 한 시나리오", *_two(sp, lambda s: _str_frac(s["control_false_alarm_n"]))),
        ("원인 가설 1순위가 합성 정답과 맞은 비율", *_two(sp, lambda s: _str_frac(s["cause_top1_n"]))),
        ("해서는 안 되는 액션을 제안한 수", *_two(sp, lambda s: str(s["forbidden_proposals"]))),
        ("보고서에 근거 없는 숫자가 든 수", *_two(sp, lambda s: str(s["unsupported_numbers"]))),
    ]


def _insights() -> None:
    historical = _load("insights_controlled.json")
    current = _load("insights_followup_regression_v2_20261020.json") or _load("insights_followup_regression.json")
    held = _load("insights_holdout_20261020.json")
    revised = _load("insights_holdout_regression_20261020.json")
    if not historical and not current and not held:
        return _missing("insights_controlled.json")
    if current:
        st.markdown("**후속 엔진 회귀 검사**")
        _table(rows_insights(current), first="기존 개발 사례", second="기존 최종 확인 사례 재검사")
        _notes(["이미 확인한 데이터를 수정 후 다시 실행한 결과이며 **새 독립 평가가 아닙니다**.",
                "기준선과 추세를 추정한 오차 및 비교 수를 반영했습니다. 점수가 높아도 원인을 단정하지 않고 확인할 가설과 점검 방법을 제시합니다."],
               str(current.get("command") or ""))
    if held:
        st.markdown("**엔진 동결 후 별도 작성한 합성 평가**")
        _table(rows_insights(held), first="개발 사례", second="별도 최종 확인 사례")
        _notes(["같은 생성기를 쓰는 합성 사례이므로 실제 광고 데이터의 정확도가 아닙니다. 원인 가설 순위와 실제 원인 증명은 다릅니다."],
               str(held.get("command") or ""))
        if held.get("engine_changed_after_holdout") or held.get("holdout_provenance_issues"):
            st.warning("저장된 동결 정보와 엔진이 일치하지 않아 새 최종 확인 결과로 해석할 수 없습니다.")
    if revised:
        st.markdown("**처음 평가에서 드러난 한계를 보완한 뒤 재검사**")
        _table(rows_insights(revised), first="개발 사례", second="같은 80개 합성 사례 재검사")
        st.caption("변화가 기준기간에 섞이는 경우를 보완한 뒤 같은 자료를 다시 실행했습니다. 새 독립 평가가 아닙니다.")
    if historical:
        with st.expander("처음 저장한 평가 기록"):
            _table(rows_insights(historical))
            ho = historical.get("splits", {}).get("holdout") or {}
            _notes([f"원래 엔진은 정상 시나리오 {_get(ho, 'control_false_alarm_n') or '—'}를 이상이 있다고 알렸습니다. 이 과거 기록은 보존했습니다.",
                    "동시에 일어난 사건은 원인을 하나만 맞히거나 정하지 못한 경우가 있었습니다. 이후 수정 결과와 구분해서 읽어 주세요."])
    _notes(["시나리오는 **합성 데이터**입니다. 생성기의 패턴을 진단 규칙이 공유하므로 실제 광고 데이터의 정확도가 아닙니다.",
            "이 표는 탐지·가설·보고서 근거 검사를 평가합니다. AI가 쓴 문장의 실무 유용성이나 운영 성과 개선을 측정하지 않았습니다."])


ROWS = {   # 문서 생성기(scripts/gen_feature_docs.py)가 같은 표를 쓴다
    "dialogs": ("dialogs_regression_v2_20261021.json", rows_dialogs),
    "urls": ("url_ingest_eval.json", rows_urls),
    "images": ("image_eval.json", rows_images),
    "quality": ("quality_signals.json", rows_quality),
    "insights": ("insights_followup_regression_v2_20261020.json", rows_insights),
}


def rows_for(key: str) -> Optional[list[tuple[str, str, str]]]:
    """저장된 리포트에서 만든 (지표, 개발용, 최종 확인용) 행. 리포트가 없으면 None."""
    name, fn = ROWS[key]
    rep = _load(name)
    return fn(rep) if rep else None


SECTIONS = [
    ("대화형 캠페인 설정", _dialogs),
    ("상품 페이지에서 정보 읽기", _urls),
    ("이미지 소재 검사", _images),
    ("소재 퀄리티 신호", _quality),
    ("성과 리포트 인사이트", _insights),
]


def _live_quality_summary(summary: dict) -> None:
    n = summary.get("attempted_cases", 0)
    st.markdown("**문구 비교 · 채택과 보류**")
    _table([
        ("근거 검사 후 채택한 비교", f"{summary.get('accepted_cases', 0)}/{n}", "두 순서와 중요한 근거가 같은 비교"),
        ("판단 보류", f"{summary.get('abstained_cases', 0)}/{n}", "불일치·인용 문제·응답 실패는 무승부로 세지 않음"),
        ("채택했고 작성자 선호와도 일치", f"{summary.get('accepted_author_matches', 0)}/{n}", "전체 시도 쌍을 분모로 사용"),
        ("기존 비교 지표", f"{summary.get('raw_stable_author_matches', 0)}/{n}", "원래 두 선택이 같고 작성자 선호와 일치; 근거 검사 전 수치"),
    ], first="쌍 / 시도", second="뜻")
    labels = {"consensus": "채택", "order_disagreement": "순서에 따라 선택이 달라짐",
              "rubric_disagreement": "중요한 근거가 달라짐", "invalid_evidence": "인용·근거 검사 실패",
              "judge_abstained": "모델이 판단 보류", "incomplete": "응답을 모두 얻지 못함"}
    st.caption("결과별 수: " + ", ".join(f"{labels.get(key, key)} {value}쌍" for key, value in summary.get("by_status", {}).items()))
    st.caption("기존 비교 지표가 높아도 근거가 모순되면 채택하지 않습니다. 채택은 사람의 선호나 실제 광고 성과를 검증했다는 뜻이 아닙니다.")


def _live() -> None:
    """저장된 실호출 기록만 읽는다. 화면을 보는 동안 API를 호출하지 않는다."""
    reports = []
    for path in sorted((REPORTS / "live_features").glob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and report.get("schema_version") == 1 and isinstance(report.get("results"), dict):
            reports.append(report)
    if not reports:
        return
    reports.sort(key=lambda report: (str(report.get("finished_at") or report.get("started_at") or ""),
                                     str(report.get("run_id", ""))), reverse=True)
    st.markdown("### 실제 모델 실행 기록")
    st.caption("GitHub Actions에서 API를 호출해 저장한 결과입니다. 여기서 열람하는 동안 새 호출이나 비용은 발생하지 않습니다.")
    names = {"vision": "이미지 속 글자·검사", "conversation": "여러 턴 캠페인 설정", "insights": "성과 보고서 문장화",
             "quality": "광고 문구 비교", "pipeline": "캠페인 구성·문구 생성·검사"}
    status_labels = {"completed": "완료", "incomplete_budget_or_provider_failure": "일부만 실행",
                     "incomplete_case_count": "일부만 실행",
                     "interrupted": "실행 중단", "running": "실행 중"}
    for rep in reports:
        planned = _get(rep, "manifest", "planned_cases") or {}
        summaries = rep.get("summaries", {})
        unattempted = any(isinstance(total, int) and total > summaries.get(key, {}).get("attempted_cases", 0)
                          for key, total in planned.items())
        supplemental_planned = _get(rep, "manifest", "planned_supplemental_cases") or {}
        supplemental_summaries = rep.get("supplemental_summaries", {})
        unattempted = unattempted or any(isinstance(total, int) and total > supplemental_summaries.get(key, {}).get("attempted_cases", 0)
                                        for key, total in supplemental_planned.items())
        status = rep.get("status")
        status_label = status_labels.get(status, "상태 확인 필요")
        if status == "completed" and unattempted:
            status_label = "일부만 실행"
        with st.expander(f"{rep.get('run_id', '실행')} · {status_label}", expanded=rep is reports[0]):
            if _get(rep, "manifest", "follows_run_id"):
                st.caption("이전 실행에서 확인한 같은 입력을 수정 후 다시 실행한 결과입니다. 처음 보는 입력의 성능과 구분합니다.")
            if status != "completed" or unattempted:
                st.warning("모든 계획 사례의 실행이 끝난 기록이 아닙니다. 시도하지 않은 사례를 성공으로 세지 않습니다.")
            rows = []
            for key, name in names.items():
                s = summaries.get(key, {})
                attempted = s.get("attempted_cases", 0)
                pass_cell = ("아래 채택·보류 표 확인" if key == "quality" and rep.get("quality_audit_summary")
                             else f"{s.get('all_checks_passed', 0)}/{attempted}")
                rows.append((name, f"{attempted}/{planned.get(key, '—')}", f"{s.get('cases_with_real_calls', 0)}/{attempted}",
                             pass_cell))
            _table(rows, first="시도 / 계획", second="실제 응답이 있는 사례 / 시도", third="모든 검사 통과 / 시도")
            if rep.get("quality_audit_summary"):
                _live_quality_summary(rep["quality_audit_summary"])
            followup_plan = _get(rep, "manifest", "planned_supplemental_cases", "conversation_followup")
            if followup_plan:
                followup = rep.get("supplemental_summaries", {}).get("conversation_followup", {})
                count = followup.get("attempted_cases", 0)
                st.markdown("**추가 검사 · 연령 확인 뒤 캠페인 완성**")
                _table([("최소 연령 답변 → 미리보기 → 광고 작업 전달", f"{count}/{followup_plan}",
                         f"{followup.get('cases_with_real_calls', 0)}/{count}", f"{followup.get('all_checks_passed', 0)}/{count}")],
                       first="시도 / 계획", second="실제 응답이 있는 사례 / 시도", third="전 단계 확인 / 시도")
                st.caption("최초 대화 20개의 점수와 별도입니다. 확인되지 않은 최소 연령을 물은 뒤 사용자가 명시적으로 답한 경우를 검사합니다.")
            supplemental = rep.get("supplemental_summaries", {}).get("rewrite")
            supplemental_plan = _get(rep, "manifest", "planned_supplemental_cases", "rewrite")
            if supplemental is not None or supplemental_plan:
                supplemental = supplemental or {}
                count = supplemental.get("attempted_cases", 0)
                st.markdown("**추가 검사 · 위반 원문의 수정과 재검사**")
                _table([("원문 → 모델 수정안 → 동일 정책 재검사", f"{count}/{supplemental_plan or '—'}",
                         f"{supplemental.get('cases_with_real_calls', 0)}/{count}", f"{supplemental.get('all_checks_passed', 0)}/{count}")],
                       first="시도 / 계획", second="실제 응답이 있는 사례 / 시도", third="전 단계 확인 / 시도")
                st.caption("기존 다섯 기능의 점수와 별도입니다. 원문의 위반, 모델 수정 제안, 수정안의 실제 의미 검사 호출, 같은 정책 재검사 통과를 모두 확인해야 성공입니다. 원문만 승인하거나 수정안을 검사하지 않은 경우는 성공으로 세지 않습니다.")
            ledger = rep.get("ledger", {})
            usage = ledger.get("usage_cost_usd")
            cost = f"${usage:.4f}" if isinstance(usage, (float, int)) else "확인되지 않음"
            st.markdown(f"호출 **{ledger.get('attempts', 0)}회** · 응답 토큰으로 계산한 비용 **{cost}** · 사용량 미확인 **{ledger.get('unknown_cost_calls', 0)}회**")
            if ledger.get("unknown_cost_calls", 0):
                accounted = ledger.get("accounted_usd")
                amount = f"${accounted:.4f}" if isinstance(accounted, (float, int)) else "확인되지 않음"
                st.markdown(f"사용량 미확인 호출의 예약액을 포함한 예산 반영액 **{amount}**")
            st.caption("공개 단가로 계산한 값이며 실제 청구서 금액은 아닙니다. 실패 요청은 예약 금액을 유지하며, 같은 실행을 다시 돌려 예산을 재사용하지 않습니다.")
            measured_models = sorted({c.get("returned_model") for c in ledger.get("calls", []) if c.get("status") == "success" and c.get("returned_model")})
            st.caption(f"실제 응답에서 확인한 모델: {', '.join(measured_models) or '없음'} · 실행 소스 {str(rep.get('source_sha', ''))[:12]}")
            _notes(rep.get("limitations", []))
            st.caption("대화의 세 원본 정답은 정책 경고/연령 조건 해석과 다릅니다. 원본 점수에 그대로 포함합니다. 문구 비교는 순서를 뒤집어 다시 묻고, 선택이 달라지면 일관되지 않은 판단으로 기록합니다.")


def render() -> None:
    st.markdown("## 새 기능 평가 (합성 데이터)")
    st.markdown("대화형 설정·상품 페이지 읽기·이미지 검사·소재 퀄리티·성과 리포트를 **이 프로젝트가 만든 합성 사례**로 재어 저장해 둔 결과입니다. "
                "**개발용**은 로직을 만들며 본 데이터라 점수가 높게 나오고, **최종 확인용**은 각 기능의 로직을 고정한 뒤 처음 평가한 기록입니다. 이후 수정과 재검사는 해당 기능의 설명에서 구분합니다. "
                "라벨은 작성자가 달았고 독립 검증은 없으며, 실제 광고 데이터의 정확도가 아닙니다. 규칙 검사와 실제 모델 호출 결과는 나눠 표시합니다.")
    _live()
    for title, fn in SECTIONS:
        with st.expander(title):
            fn()
