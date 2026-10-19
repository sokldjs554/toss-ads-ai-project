"""실패 모드 카탈로그가 기능별로 이어서 번호가 매겨졌고, 문서·화면·코드가 같은 정식 코드만 쓰는지 점검한다.

새 기능의 실패 모드를 등록할 때 이 파일의 점검이 어긋나지 않게 한다 — 번호 연속·중복 없음, 기능 묶음 순서, 가리키는 테스트의 존재,
임시 코드(자리표시자)가 어디에도 남지 않음, docs/FAILURE_MODES.md 가 카탈로그에서 다시 만든 것과 같음, 런북 행이 모든 코드에 있음.
"""
from __future__ import annotations

import re
from pathlib import Path

from guardrails.failure_modes import BY_CODE, FAILURE_MODES, FEATURES, by_feature

ROOT = Path(__file__).resolve().parents[1]
OPS_DOC = ROOT / "docs" / "OPERATIONS.md"
PLACEHOLDER = "FM-" + "NEW-"           # 이 파일 자신에 자리표시자 문자열이 남지 않게 둘로 나눠 적는다
CODE_RE = re.compile(r"^FM-(\d{2,3})$")
FEATURE_ORDER = ("core", "conversation", "url", "image", "quality", "insights", "labeling", "ops")


def _num(code: str) -> int:
    m = CODE_RE.match(code)
    assert m, f"코드 형식이 FM-숫자 가 아니다: {code}"
    return int(m.group(1))


def test_codes_are_unique_and_numbered_without_gaps_from_one():
    codes = [fm.code for fm in FAILURE_MODES]
    assert len(codes) == len(set(codes)), sorted(c for c in set(codes) if codes.count(c) > 1)
    nums = sorted(_num(c) for c in codes)
    assert nums == list(range(1, len(nums) + 1)), "번호가 FM-01 부터 빠짐없이 이어져야 한다"


def test_the_original_24_stay_core_and_new_codes_follow_in_file_order():
    first24 = FAILURE_MODES[:24]
    assert sorted(_num(fm.code) for fm in first24) == list(range(1, 25)) and all(fm.feature == "core" for fm in first24)
    rest = FAILURE_MODES[24:]
    assert [_num(fm.code) for fm in rest] == list(range(25, 25 + len(rest))), "FM-25 부터 파일 순서대로 번호가 이어져야 한다"
    assert all(fm.feature != "core" for fm in rest)


def test_features_are_valid_named_in_plain_korean_and_every_feature_has_entries():
    assert tuple(FEATURES) == FEATURE_ORDER
    assert tuple(FEATURES.values()) == ("핵심 심사", "대화형 셋업", "상품 페이지 가져오기", "이미지", "소재 퀄리티", "성과 리포트", "라벨링·검수", "운영")
    assert {fm.feature for fm in FAILURE_MODES} <= set(FEATURES), {fm.code for fm in FAILURE_MODES if fm.feature not in FEATURES}
    groups = by_feature()
    assert list(groups) == list(FEATURES) and sum(len(v) for v in groups.values()) == len(FAILURE_MODES)
    assert all(groups[k] for k in FEATURES), [k for k in FEATURES if not groups[k]]


def test_each_new_feature_is_one_contiguous_block_in_the_agreed_order():
    """A(대화) → B1(URL) → B2(이미지) → B3(품질) → C(성과) → D1(라벨링) → D2(운영) 순으로 한 덩어리씩 놓인다."""
    seq = [fm.feature for fm in FAILURE_MODES[24:]]
    order: list[str] = []
    for f in seq:
        if not order or order[-1] != f:
            order.append(f)
    assert order == list(FEATURE_ORDER[1:]), f"기능 묶음이 한 덩어리씩 순서대로 놓이지 않았다: {order}"
    for key, modes in by_feature().items():
        nums = sorted(_num(fm.code) for fm in modes)      # 핵심 심사는 파일 안에서 FM-23 이 FM-22 앞에 있다 (기존 순서 그대로)
        assert nums == list(range(nums[0], nums[0] + len(nums))), f"{key} 의 번호가 이어지지 않는다"


def test_every_entry_is_filled_in_and_safe_to_put_in_a_markdown_table():
    for fm in FAILURE_MODES:
        for name in ("code", "name", "stage", "symptom", "detection", "defense", "test"):
            v = getattr(fm, name)
            assert isinstance(v, str) and v.strip() and v == v.strip(), (fm.code, name)
            assert "|" not in v and "\n" not in v, f"{fm.code}.{name} 에 표를 깨는 글자(| 또는 줄바꿈)가 있다"
        assert isinstance(fm.fail_closed, bool)


def test_every_failure_mode_points_at_an_existing_test_function():
    missing = []
    for fm in FAILURE_MODES:
        path, sep, func = fm.test.partition("::")
        if fm.feature != "core":      # 기존 24개 중 일부는 파일 전체를 가리킨다. 새 항목은 반드시 파일::함수
            assert sep and func and "::" not in func, f"{fm.code}: tests/파일.py::함수 형식이 아니다 ({fm.test})"
        p = ROOT / path
        if not p.is_file() or (func and not re.search(rf"def {re.escape(func)}\b", p.read_text(encoding="utf-8"))):
            missing.append((fm.code, fm.test))
    assert not missing, missing


def test_entries_with_a_known_limit_are_not_marked_fail_closed_and_say_so():
    """알려진 한계가 남은 항목은 fail_closed=False 로 정직하게 표시하고, 방어 칸에 그 한계를 적는다 (거꾸로, 한계를 적었으면 닫혔다고 하지 않는다)."""
    for fm in FAILURE_MODES:
        if not fm.fail_closed:
            assert "한계" in fm.defense, f"{fm.code}: fail_closed=False 인데 방어 칸에 '한계' 를 적지 않았다"
        if "알려진 한계" in fm.defense:
            assert not fm.fail_closed, f"{fm.code}: 방어 칸에 알려진 한계를 적었는데 fail_closed=True 이다"


def _text_files():
    skip_dirs = {".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv"}
    for p in ROOT.rglob("*"):
        if not p.is_file() or any(part in skip_dirs for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() in {".py", ".md", ".json", ".jsonl", ".yml", ".yaml", ".html", ".txt", ".toml", ".cfg", ".svg"}:
            yield p


def test_no_placeholder_failure_mode_code_is_left_anywhere_in_the_repo():
    hits = []
    for p in _text_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if PLACEHOLDER in text:
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, f"자리표시자 코드가 남았다: {hits}"


def test_failure_event_codes_in_the_runtime_code_are_all_in_the_catalog():
    """런타임이 FailureEvent 로 남기는 코드 문자열은 모두 정식 카탈로그 코드여야 한다 (대화 담당의 자리표시자를 정식 코드로 바꾼 뒤의 회귀 방지)."""
    bad = []
    for d in ("agents", "core", "guardrails", "llm", "ops", "ui", "evals", "observability"):
        for p in (ROOT / d).rglob("*.py"):
            for m in re.finditer(r"""code\s*=\s*["'](FM-[^"']*)["']""", p.read_text(encoding="utf-8")):
                if m.group(1) not in BY_CODE:
                    bad.append((str(p.relative_to(ROOT)), m.group(1)))
    assert not bad, bad


def test_conversation_failure_codes_used_by_runtime_and_tests_match_the_catalog():
    src = (ROOT / "agents" / "conversation.py").read_text(encoding="utf-8")
    used = set(re.findall(r'FailureEvent\(code="(FM-\d+)"', src))
    assert {"FM-25", "FM-26", "FM-27", "FM-28"} <= used
    for code in ("FM-25", "FM-26", "FM-27", "FM-28"):
        assert BY_CODE[code].feature == "conversation" and BY_CODE[code].stage.startswith("conversation"), code
    for p in sorted((ROOT / "tests").glob("test_conv_*.py")):
        for code in re.findall(r'"(FM-\d+)"', p.read_text(encoding="utf-8")):
            assert code in BY_CODE, (p.name, code)


def test_failure_modes_doc_equals_what_the_generator_makes_from_the_catalog():
    from scripts.gen_docs import failure_modes_md

    stored = (ROOT / "docs" / "FAILURE_MODES.md").read_text(encoding="utf-8")
    assert stored == failure_modes_md(), "docs/FAILURE_MODES.md 가 카탈로그와 다르다 — python scripts/gen_docs.py 로 다시 만든다"
    for fm in FAILURE_MODES:
        assert f"| {fm.code} |" in stored
    for label in FEATURES.values():
        assert f"## {label} (FM-" in stored


def _runbook_rows() -> dict[str, list[str]]:
    table = OPS_DOC.read_text(encoding="utf-8").split("### 4.2", 1)[1].split("## 5.", 1)[0]
    rows = {}
    for ln in table.splitlines():
        m = re.match(r"^\| (FM-\d+) \|", ln)
        if m:
            rows[m.group(1)] = [c.strip() for c in ln.strip().strip("|").split("|")]
    return rows


def test_operations_runbook_has_one_complete_row_per_catalog_code_and_names_match():
    rows = _runbook_rows()
    assert set(rows) == {fm.code for fm in FAILURE_MODES}, sorted(set(rows) ^ {fm.code for fm in FAILURE_MODES})
    for fm in FAILURE_MODES:
        cells = rows[fm.code]
        assert len(cells) == 5 and all(cells), (fm.code, cells)
        if int(fm.code.split("-")[1]) >= 25:
            assert cells[1] == fm.name, f"{fm.code}: 런북의 실패 이름이 카탈로그와 다르다"


def test_operations_doc_lists_exactly_the_codes_that_are_not_fail_closed():
    t = OPS_DOC.read_text(encoding="utf-8")
    m = re.search(r"`fail_closed` 가 거짓인 코드\((.*?)\)", t)
    assert m, "docs/OPERATIONS.md §4.2 에 fail_closed 가 거짓인 코드 목록이 없다"
    listed = {int(n) for n in re.findall(r"\d+", m.group(1))}
    assert listed == {_num(fm.code) for fm in FAILURE_MODES if not fm.fail_closed}


def test_catalog_screen_groups_entries_by_feature_without_errors(tmp_path):
    from streamlit.testing.v1 import AppTest

    script = tmp_path / "probe.py"
    script.write_text("import sys\nsys.path.insert(0, %r)\nfrom ui import catalog_view\ncatalog_view.render_failure_modes()\n" % str(ROOT), encoding="utf-8")
    at = AppTest.from_file(str(script), default_timeout=30)
    at.run()
    assert not at.exception
    labels = [e.label for e in at.expander]
    assert len(labels) == len(FEATURES)
    for (key, name), label in zip(FEATURES.items(), labels):
        assert label.startswith(name) and f"{len(by_feature()[key])}개" in label, label
    assert [len(d.value) for d in at.dataframe] == [len(by_feature()[k]) for k in FEATURES]
    shown = {row["코드"] for d in at.dataframe for _, row in d.value.iterrows()}
    assert shown == {fm.code for fm in FAILURE_MODES}
    first = at.dataframe[0].value
    assert list(first.columns) == ["코드", "이름", "단계", "증상", "탐지", "방어", "테스트", "실패 시 막힘"]
