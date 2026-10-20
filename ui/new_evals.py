"""새 기능 평가 — 대화형 설정·상품 페이지 이해·이미지 검사·소재 퀄리티·성과 리포트의 저장된 평가 결과를 개발용과 최종 확인용으로 나란히 보여 준다.

이 화면은 저장된 리포트(evals/reports/*.json)를 읽기만 한다. 숫자를 새로 계산하지 않고, 리포트에 없는 숫자를 쓰지 않는다.
모든 값은 이 프로젝트가 만든 합성 데이터·사례에서 잰 것이다 — 실제 모델의 능력이나 실제 광고 데이터의 정확도가 아니다.
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


def _table(rows: list[tuple[str, str, str]], first: str = "개발용", second: str = "최종 확인용") -> None:
    body = "".join(f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td><td>{html.escape(c)}</td></tr>" for a, b, c in rows)
    st.markdown("<div style='overflow-x:auto'><table class='pf-tbl' style='width:100%;border-collapse:collapse;font-size:.92rem'>"
                f"<thead><tr><th style='text-align:left'>지표</th><th style='text-align:left'>{html.escape(first)}</th><th style='text-align:left'>{html.escape(second)}</th></tr></thead>"
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
        ("숨은 지시문에 값이 오염된 대화", *_two(sp, lambda s: f"{s['injection_contaminated']}/{s['injection_dialogs']}")),
    ]


def _dialogs() -> None:
    rep = _load("dialogs_controlled.json")
    if not rep:
        return _missing("dialogs_controlled.json")
    _table(rows_dialogs(rep))
    ho = (rep.get("splits") or {}).get("holdout") or {}
    _notes(["이 숫자는 **정해진 규칙이 말을 읽은 결과**입니다. 실제 AI 모델이 얼마나 잘 이해하는지는 측정하지 않았습니다.",
            "대화는 만든 사람이 직접 쓴 가상 대화이고 라벨도 같은 사람이 달았습니다. 개발용은 규칙을 고치며 본 대화라 점수가 높게 나옵니다.",
            f"최종 확인용에서 숨은 지시문에 값이 오염된 대화는 {ho.get('injection_contaminated', '—')}건이었습니다 — 바꿔 쓴 지시문을 못 잡는 알려진 한계입니다(확정된 값은 조용히 바뀌지 않고 되묻는 상태가 됩니다)."],
           str(rep.get("repro") or ""))


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
    rep = _load("url_ingest_eval.json")
    if not rep:
        return _missing("url_ingest_eval.json")
    _table(rows_urls(rep))
    _notes(["합성 페이지 위의 값이며 실제 쇼핑몰 페이지의 정확도가 아닙니다. 페이지와 기대값을 같은 사람이 만들었습니다.",
            "읽어 온 내용은 사용자가 직접 골라 확인하기 전에는 광고 사실이 되지 않습니다. 자바스크립트로 그려지는 페이지는 읽지 못합니다."],
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
    _notes(["**실제 AI 모델이 이미지 속 글자를 얼마나 정확히 읽는지는 측정하지 않았습니다.** 모델이 혼자 읽은 결과는 사용자가 적은 글과 맞춰 보기 전에는 통과가 되지 않고, 읽지 못한 이미지는 어떤 경우에도 통과가 아닙니다.",
            "100%에 가까운 값은 사례가 적고 규칙과 사례를 같은 사람이 만들었기 때문입니다 — 강한 증거가 아닙니다. 배너 크기·여백·글자 크기는 이 프로젝트가 가정한 값입니다."],
           str((rep.get("repro") or "python -m evals.image_eval")))


def rows_quality(rep: dict) -> list[tuple[str, str, str]]:
    sp = rep.get("splits", {})
    return [
        ("선호가 갈린 쌍에서 사람의 선택과 일치", *_two(sp, lambda s: _kn(s["decisive_agreement"]))),
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
            "실제 클릭률·전환과 얼마나 같이 움직이는지는 측정하지 않았습니다. 선호 라벨은 만든 사람 한 명의 판단이고 문구는 모두 가상입니다.",
            "개발용은 신호를 만들 때 본 쌍이라 낙관적이고, 최종 확인용은 신호를 고정한 뒤 쓴 쌍이지만 같은 사람이 쓴 쉬운 편의 쌍입니다."],
           str(rep.get("reproduce") or "python -m evals.quality_signals"))


def rows_insights(rep: dict) -> list[tuple[str, str, str]]:
    sp = rep.get("splits", {})
    return [
        ("시나리오 수", *_two(sp, lambda s: str(s["n_scenarios"]))),
        ("이상을 찾아낸 비율(재현율)", *_two(sp, lambda s: _str_frac(s["detection_recall_n"]))),
        ("찾은 것 중 실제 이상인 비율(정밀도)", *_two(sp, lambda s: _str_frac(s["detection_precision_n"]))),
        ("이상이 없는데 이상이라고 한 시나리오", *_two(sp, lambda s: _str_frac(s["control_false_alarm_n"]))),
        ("원인 1순위가 맞은 비율", *_two(sp, lambda s: _str_frac(s["cause_top1_n"]))),
        ("해서는 안 되는 액션을 제안한 수", *_two(sp, lambda s: str(s["forbidden_proposals"]))),
        ("보고서에 근거 없는 숫자가 든 수", *_two(sp, lambda s: str(s["unsupported_numbers"]))),
    ]


def _insights() -> None:
    rep = _load("insights_controlled.json")
    if not rep:
        return _missing("insights_controlled.json")
    _table(rows_insights(rep))
    sp = rep.get("splits", {})
    ho = sp.get("holdout") or {}
    _notes(["시나리오는 **합성 데이터**이고 생성기와 진단 규칙을 같은 사람이 설계했습니다 — 실제 광고 데이터의 정확도가 아닙니다.",
            f"저장 당시 평가에서는 이상이 없는 시나리오 {_get(ho, 'control_false_alarm_n') or '—'}를 이상이 있다고 잘못 알렸고, 동시에 일어난 사건은 원인을 하나만 맞히거나 정하지 못한 경우가 있었습니다.",
            "이 표는 처음 저장한 평가 기록입니다. 이후 전환이 없는 경우의 비용 표시와 원인별 기여도 계산을 수정했습니다. 같은 데이터로 다시 실행한 검사는 새 독립 평가가 아닙니다.",
            "AI가 쓴 보고서 문장의 품질은 측정하지 않았습니다. 코드가 확인하는 것은 문장 속 숫자·근거·액션이 계산 결과와 맞는지입니다."],
           str(rep.get("command") or "python -m evals.insights_eval"))


ROWS = {   # 문서 생성기(scripts/gen_feature_docs.py)가 같은 표를 쓴다
    "dialogs": ("dialogs_controlled.json", rows_dialogs),
    "urls": ("url_ingest_eval.json", rows_urls),
    "images": ("image_eval.json", rows_images),
    "quality": ("quality_signals.json", rows_quality),
    "insights": ("insights_controlled.json", rows_insights),
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


def render() -> None:
    st.markdown("## 새 기능 평가 (합성 데이터)")
    st.markdown("대화형 설정·상품 페이지 읽기·이미지 검사·소재 퀄리티·성과 리포트를 **이 프로젝트가 만든 합성 사례**로 재어 저장해 둔 결과입니다. "
                "**개발용**은 로직을 만들며 본 데이터라 점수가 높게 나오고, **최종 확인용**은 각 기능의 로직을 고정한 뒤 처음 평가한 기록입니다. 이후 수정과 재검사는 해당 기능의 설명에서 구분합니다. "
                "라벨은 만든 사람이 달았고 독립 검증은 없으며, 실제 모델의 능력이나 실제 광고 데이터의 정확도가 아닙니다.")
    for title, fn in SECTIONS:
        with st.expander(title):
            fn()
