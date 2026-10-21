"""운영 문서·산출물의 정직성 점검 — '운영 경험이 있다' 로 읽힐 문장이 없고, 측정/가정/제안/설계만 표시가 있으며, 문서가 가리키는 코드·런북 절이 실제로 있다."""
from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS_DOC = ROOT / "docs" / "OPERATIONS.md"
DESIGN_DOC = ROOT / "docs" / "SYSTEM_DESIGN.md"

# 이 프로젝트가 가진 적 없는 것을 가졌다고 읽힐 수 있는 표현. 문서·코드·결과 파일 어디에도 없어야 한다.
FORBIDDEN = [
    r"운영\s*경험", r"운영해\s*(본|봤|왔|보았)", r"운영한\s*(경험|적|사례|바)", r"운영\s*중인\s*(서비스|시스템)", r"프로덕션\s*(에서|환경에서)\s*(돌|운영|검증|사용|쓰)",
    r"실제\s*(서비스|트래픽)에서\s*(검증|운영)\s*(했|하였|된)", r"(실제|라이브)\s*서비스\s*(를|을)\s*운영", r"production\s+experience", r"in\s+production\s+for",
    r"운영\s*(성과|실적)",
]
FILES = [DESIGN_DOC, OPS_DOC, ROOT / "ops" / "audit.py", ROOT / "ops" / "metrics.py", ROOT / "ops" / "monitors.py", ROOT / "ops" / "impact.py", ROOT / "ui" / "ops_view.py",
         ROOT / "scripts" / "loadtest.py", ROOT / "evals" / "reports" / "loadtest.json"]
FM_CODES_AT_WRITING = [f"FM-{i:02d}" for i in range(1, 25)]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_sentence_reads_as_operating_experience(path):
    text = _read(path)
    for pat in FORBIDDEN:
        m = re.search(pat, text, re.I)
        assert m is None, f"{path.name}: 운영 경험처럼 읽힐 표현 '{m.group(0)}' (…{text[max(0, m.start() - 20): m.end() + 20]!r}…)"


def test_the_forbidden_patterns_actually_catch_the_sentences_they_are_meant_to():
    for bad in ("프로덕션에서 운영해 본 경험이 있다", "운영 경험이 있습니다", "실제 서비스를 운영한 적이 있다", "운영 중인 서비스", "in production for two years", "운영 성과: 승인율 개선"):
        assert any(re.search(p, bad, re.I) for p in FORBIDDEN), bad
    for ok in ("이 프로젝트에는 실제 서비스의 운영 데이터가 없다", "운영을 시작한다면 쓸 시작값", "운영 화면"):
        assert not any(re.search(p, ok, re.I) for p in FORBIDDEN), ok


def test_docs_open_with_the_no_operating_data_statement():
    for doc in (OPS_DOC, DESIGN_DOC):
        head = _read(doc)[:1600]
        assert "실제 서비스의 운영 데이터가 없" in head, doc.name


def test_operations_doc_marks_status_and_separates_measured_from_assumed():
    t = _read(OPS_DOC)
    for tag in ("[구현]", "[제안]", "[설계만]", "[부분]"):
        assert tag in t, tag
    assert "측정" in t and "가정" in t and "제안" in t
    sec2 = t.split("## 2.", 1)[1].split("## 3.", 1)[0]
    assert "[제안]" in t.split("## 2.", 1)[1][:80] and "시작값" in sec2                         # SLO 는 제안으로 표시
    assert "근거" in sec2


def test_operations_doc_has_the_required_sections():
    t = _read(OPS_DOC)
    for title in ("## 2. 서비스 수준 목표(SLO) 제안", "## 4. 알림과 런북", "## 5. 롤아웃 계획", "## 6. 개인정보·보존·감사", "## 7. 비용 통제", "## 8. 장애 대응 절차", "## 9. 사람 검수 용량 계획"):
        assert title in t, title
    for step in ("오프라인 평가", "섀도 모드", "카나리", "확대"):
        assert step in t
    for word in ("통과 기준", "롤백 조건"):
        assert t.count(word) >= 3, word


def test_every_failure_mode_has_a_runbook_row():
    t = _read(OPS_DOC)
    table = t.split("### 4.2", 1)[1].split("## 5.", 1)[0]
    for code in FM_CODES_AT_WRITING:
        assert re.search(rf"^\| {code} \|", table, re.M), f"{code} 런북 행이 없다"
    try:
        from guardrails.failure_modes import FAILURE_MODES

        missing = [fm.code for fm in FAILURE_MODES if not re.search(rf"^\| {re.escape(fm.code)} \|", table, re.M)]
        if missing:
            warnings.warn(f"실패 모드 카탈로그에 새로 생긴 코드의 런북 행이 docs/OPERATIONS.md 에 없다: {missing}")
    except Exception:  # noqa: BLE001 — 카탈로그를 못 읽어도 이 문서 점검은 계속한다
        pass


def test_runbook_rows_name_a_signal_and_a_first_action():
    table = _read(OPS_DOC).split("### 4.2", 1)[1].split("## 5.", 1)[0]
    rows = [ln for ln in table.splitlines() if re.match(r"^\| FM-\d+ \|", ln)]
    assert len(rows) >= 24
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        assert len(cells) == 5 and all(cells), ln


def test_runbook_sections_for_every_monitor_rule_exist_and_have_the_standard_parts():
    from ops.monitors import RULE_SPECS

    t = _read(OPS_DOC)
    for s in RULE_SPECS.values():
        num = s.runbook.lstrip("§")
        m = re.search(rf"^#### {re.escape(num)} .*\(`?{s.name}`?\)\n(.*?)(?=^#### |^### |^## )", t, re.M | re.S)
        assert m, f"{num} 절({s.name})이 없다"
        body = m.group(1)
        for part in ("**뜻:**", "**먼저 볼 것:**", "**조치:**"):
            assert part in body, (num, part)


def test_simulation_table_numbers_in_the_doc_match_the_stored_command_semantics():
    t = _read(OPS_DOC)
    assert "python -m ops.monitors --streams 100 --runs 300 --change-at 180 --baseline 100 --seed 2026 --eval-every 1" in t
    assert "잘못된 알림 0건을 요구하지 않았고" in t and "합성" in t and "실제 서비스에서의 탐지 성능" in t
    assert "95% 구간" in t


def test_system_design_marks_each_section_and_lists_what_was_not_built():
    t = _read(DESIGN_DOC)
    for tag in ("[구현]", "[부분]", "[설계만]"):
        assert tag in t
    assert "## 12. 구현하지 않은 것" in t
    tail = t.split("## 12. 구현하지 않은 것", 1)[1].split("## 13.", 1)[0]
    for item in ("작업 큐", "영속 저장소", "인증", "다중 지역", "알림 전송"):
        assert item in tail, item
    for needle in ("recheck_key", "fail-closed", "라벨", "섀도", "속도 제한", "비동기"):
        assert needle in t, needle
    assert re.search(r"^## 3\. 구성도", t, re.M) and "```text" in t
    assert "가정" in t.split("## 2.", 1)[1].split("## 3.", 1)[0]                           # 규모는 가정이라고 밝힌다


def test_every_system_design_section_carries_a_status_tag():
    t = _read(DESIGN_DOC)
    for n in range(1, 12):
        m = re.search(rf"^## {n}\. .*$", t, re.M)
        assert m, n
        assert re.search(r"\[(구현|부분|설계만|가정|요구 정의|측정)", m.group(0)), f"{m.group(0)} 에 상태 표시가 없다"


def test_system_design_numbers_come_from_stored_reports():
    """설계 문서가 인용한 측정값이 저장된 리포트와 맞는지 대조 (평가 사례 110건의 확인율 8건, 사례당 비용 $0.0218)."""
    import json

    d = json.loads(_read(ROOT / "evals" / "reports" / "compare_live.json"))
    comb = [d["overall_by_split"][s]["combined"] for s in ("dev", "holdout")]
    assert sum(c["human_review"]["k"] for c in comb) == 8 and sum(c["human_review"]["n"] for c in comb) == 110
    assert round(sum(c["cost_usd"] for c in comb) / 110, 4) == 0.0218
    t = _read(DESIGN_DOC)
    assert "8/110" in t and "$0.0218" in t


def test_backticked_repository_paths_in_the_docs_exist():
    """문서가 가리키는 파일 경로가 실제로 있다. 아직 없는 것은 설계만으로 표시한 것뿐이어야 한다(허용 목록)."""
    planned: set[str] = set()
    pat = re.compile(r"`((?:agents|guardrails|core|llm|ops|ui|scripts|evals|docs|tests|observability|traces)/[A-Za-z0-9_./\-]+)`")
    for doc in (OPS_DOC, DESIGN_DOC):
        for path in sorted(set(pat.findall(_read(doc)))):
            if "*" in path or path in planned:
                continue
            assert (ROOT / path.rstrip("/")).exists(), f"{doc.name}: `{path}` 가 없다"


def test_effect_model_and_monitor_modules_say_what_their_numbers_do_not_mean():
    for p in (ROOT / "ops" / "impact.py", ROOT / "ops" / "monitors.py", ROOT / "ops" / "metrics.py", ROOT / "scripts" / "loadtest.py"):
        src = _read(p)
        assert "의미하지 않는 것" in src, p.name
    assert "마스킹의 한계" in _read(ROOT / "ops" / "audit.py") and "무결성의 한계" in _read(ROOT / "ops" / "audit.py")
