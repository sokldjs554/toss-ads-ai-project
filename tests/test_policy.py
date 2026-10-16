import re
from pathlib import Path

from guardrails.failure_modes import FAILURE_MODES
from guardrails.policy import get_catalog

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_loads_and_ids_unique():
    cat = get_catalog()
    assert len(cat.rules) >= 40 and len(cat.rule_ids()) == len(set(cat.rule_ids()))
    assert all(r.section for r in cat.rules.values())


def test_every_failure_mode_test_exists():
    """실패 모드 카탈로그가 가리키는 테스트가 실제로 존재해야 한다 (문서와 코드가 어긋나지 않게)."""
    missing = []
    for fm in FAILURE_MODES:
        path, _, func = fm.test.partition("::")
        p = ROOT / path
        if not p.exists():
            missing.append(fm.test)
            continue
        if func and not re.search(rf"def {re.escape(func)}\b", p.read_text(encoding="utf-8")):
            missing.append(fm.test)
    assert not missing, missing


def test_disclosure_specs_reference_known_rules():
    cat = get_catalog()
    for spec in cat.disclosures.values():
        assert spec.rule_id in cat.rules
