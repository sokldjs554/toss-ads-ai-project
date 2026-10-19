"""소재 퀄리티 점수는 정책 판정과 완전히 분리된다 — 구조(import)로도, 동작으로도.

품질이 높다고 위반이 통과되지 않고, 낮다고 정책상 문제없는 광고가 막히지 않는다. 이 테스트들이 그 분리를 못 박는다:
- core/creative_quality.py·evals/quality_signals.py 는 판정 함수·심사·파이프라인·모델 계층을 import 하지 않는다 (소스 검사 + 새 인터프리터에서 import 한 뒤 로드된 모듈 검사)
- 판정 경로(agents/·guardrails/·llm/·core/ 의 다른 파일)는 이 모듈을 import 하지 않는다
- 실제 판정 함수(output_guard.check → adjudicator.decide)의 결과는 품질 카드와 무관하다
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import creative_quality as cq
from core.schemas import AdCopy, Decision, Fact, ProductCategory, ProductFacts

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PREFIXES = ("agents", "llm", "observability", "ui", "streamlit", "evals.runner", "evals.judge", "evals.compare", "openai", "anthropic",
                      "guardrails.output_guard", "guardrails.input_guard", "guardrails.failsafe", "guardrails.disclosure", "guardrails.injection")
ALLOWED_CORE_IMPORTS = {"__future__", "re", "dataclasses", "typing", "core.schemas", "guardrails.grounding", "guardrails.lexical"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def _is_forbidden(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def test_core_module_imports_only_the_allowed_modules():
    imports = _imports(ROOT / "core" / "creative_quality.py")
    assert imports <= ALLOWED_CORE_IMPORTS, imports - ALLOWED_CORE_IMPORTS
    assert not [i for i in imports if _is_forbidden(i)]


def test_importing_the_module_loads_no_judging_or_model_layer():
    """새 인터프리터에서 import 만 했을 때 판정·심사·모델·화면 계층이 함께 올라오지 않는다 (간접 import 까지 잡는다)."""
    code = ("import json, sys; sys.path.insert(0, %r); import core.creative_quality; "
            "print(json.dumps(sorted(m for m in sys.modules if any(m == p or m.startswith(p + '.') for p in %r))))") % (str(ROOT), list(FORBIDDEN_PREFIXES))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


# 실패 모드 카탈로그는 보장하는 테스트의 파일 이름(tests/test_creative_quality_*.py)을 글자로 적을 뿐 이 모듈을 부르지 않는다 — 글자 검색 대신 import 만 본다.
CATALOG_FILES = {"failure_modes.py"}


def _offenders(root: Path) -> list[str]:
    needles = ("creative_quality", "quality_signals", "QualityCard", "quality_card")
    out = []
    for folder in ("agents", "guardrails", "llm", "core"):
        for path in (root / folder).rglob("*.py"):
            if path.name == "creative_quality.py":
                continue
            if path.name in CATALOG_FILES:
                if any(n in imp for imp in _imports(path) for n in needles):
                    out.append(str(path.relative_to(root)))
                continue
            if any(n in path.read_text(encoding="utf-8") for n in needles):
                out.append(str(path.relative_to(root)))
    return sorted(out)


def test_the_decision_path_does_not_import_the_quality_module():
    assert _offenders(ROOT) == []


def test_the_offender_scan_really_detects_an_import(tmp_path):
    """변이 확인: 판정 경로 파일이 품질 모듈을 가져오면 위 검사가 그 파일을 집어낸다."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "adjudicator.py").write_text("from core.creative_quality import quality_card\n", encoding="utf-8")
    (tmp_path / "guardrails").mkdir()
    (tmp_path / "guardrails" / "ok.py").write_text("import re\n", encoding="utf-8")
    assert _offenders(tmp_path) == ["agents/adjudicator.py"]


def test_the_catalog_exemption_skips_only_the_text_scan_not_an_import(tmp_path):
    """카탈로그가 테스트 파일 이름을 적는 것은 허용하지만, 카탈로그가 품질 모듈을 import 하면 여전히 집어낸다."""
    (tmp_path / "guardrails").mkdir()
    f = tmp_path / "guardrails" / "failure_modes.py"
    f.write_text('TEST = "tests/test_creative_quality_core.py::test_x"\n', encoding="utf-8")
    assert _offenders(tmp_path) == []
    f.write_text("from core.creative_quality import quality_card\n", encoding="utf-8")
    assert _offenders(tmp_path) == ["guardrails/failure_modes.py"]


def test_the_module_exposes_nothing_that_looks_like_a_verdict():
    assert not {"Decision", "decide", "adjudicate", "Finding", "Severity_BLOCK"} & set(vars(cq)) - {"Severity"}
    names = {f.name for cls in (cq.Signal, cq.QualityCard, cq.Comparison) for f in cls.__dataclass_fields__.values()}
    assert not {n for n in names if any(w in n for w in ("decision", "verdict", "approve", "reject", "block", "passed"))}
    assert cq.QualityCard.__dataclass_fields__["grade"].type in ("str", str)      # 등급은 상·중·하 띠일 뿐 합격·불합격이 아니다


def _violating_but_well_written() -> tuple[AdCopy, ProductFacts]:
    facts = [Fact(id="F1", text="소수점 1,000원부터 투자 가능"), Fact(id="F2", text="국내 ETF 매매 수수료 무료 (12월 31일까지)"), Fact(id="F3", text="투자 원금 손실 가능")]
    product = ProductFacts(name="소수점 ETF", category=ProductCategory.INVESTMENT, facts=facts)
    copy = AdCopy(headline="1,000원부터 시작하는 소수점 투자", body="12월 31일까지 국내 ETF 매매 수수료가 무료예요. 원금 보장이에요.", cta="계좌 개설하기")
    return copy, product


def test_high_quality_does_not_let_a_violation_through():
    """문구는 구체적이고 읽기 쉬워 품질 신호가 높지만 '원금 보장' 이라는 금지 표현이 있다 — 판정은 품질과 무관하게 반려다."""
    from agents.adjudicator import decide, inputs_from
    from guardrails import output_guard

    copy, product = _violating_but_well_written()
    c = cq.quality_card(copy, product.facts, {"investment"})
    assert c.overall >= 0.7 and c.grade == "상"
    det = output_guard.check(copy, product, {"investment"})
    assert det.decision == Decision.REJECT
    assert decide(inputs_from(det, None, None), 0.8).decision == Decision.REJECT


def test_low_quality_does_not_block_a_compliant_ad():
    """정책상 문제없지만 막연한 문구 — 품질 신호는 낮아도 코드 검사의 결과는 반려·수정 요청이 아니다."""
    from guardrails import output_guard

    facts = [Fact(id="F1", text="전월 실적 30만원 이상 시 월 최대 3만원 캐시백"), Fact(id="F2", text="연회비 없음")]
    product = ProductFacts(name="카드", category=ProductCategory.OTHER, facts=facts)
    copy = AdCopy(headline="혜택 가득한 상품을 만나보세요", body="다양한 즐거움을 누려보세요.", cta="자세히 보기")
    c = cq.quality_card(copy, facts, {"other"})
    assert c.grade in ("중", "하") and c.overall < 0.7
    det = output_guard.check(copy, product, {"other"})
    assert det.decision == Decision.APPROVE and not det.findings


def test_decision_inputs_have_no_quality_field_and_decide_ignores_the_card():
    import inspect

    from agents.adjudicator import DecisionInputs, decide

    assert not [f for f in DecisionInputs.__dataclass_fields__ if "quality" in f]
    assert list(inspect.signature(decide).parameters) == ["inp", "threshold"]
    inp = DecisionInputs(det_block=False, verdict_available=True, model_decision="APPROVE", confidence=0.95)
    before = decide(inp, 0.8)
    cq.quality_card(AdCopy(headline="a", body="b", cta="c"), [], set())          # 카드를 아무리 만들어도 판정 입력은 바뀌지 않는다
    assert decide(inp, 0.8) == before
