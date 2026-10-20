"""새 자료 재실행이 과거 평가의 출처나 공격 분모를 빌려 쓰지 않는지 확인한다."""
import pytest

from evals import dialogs as E


def test_custom_replay_names_its_own_frozen_data_and_separates_actual_attacks():
    path = E.ROOT / "evals/datasets/dialogs_holdout_20261020.jsonl"
    output = E.ROOT / "evals/reports/dialogs_holdout_regression_20261020.json"
    report = E.build_report("all", path=path, output_path=output)
    assert report["dataset"]["sha256"] == E.sha256_of(path)
    assert report["dataset"]["provenance"].endswith("dialogs_holdout_20261020.provenance.json")
    assert report["holdout"]["clean"] is False
    assert report["holdout"]["first_evaluation_claimed"] is False
    assert str(output) in report["repro"]
    assert "DLG-035" not in " ".join(report["caveats"])
    assert report["splits"]["all"]["injection_dialogs"] == 7  # 일반 금지 값 검사 포함
    assert report["splits"]["all"]["attack_dialogs"] == 4


@pytest.mark.parametrize("name", ["dialogs_controlled.json", "dialogs_holdout_20261020.json"])
def test_first_results_cannot_be_overwritten_by_the_cli(name):
    path = E.REPORTS / name
    original = E.sha256_of(path)
    with pytest.raises(SystemExit):
        E.main(["--out", str(path), "--quiet"])
    assert E.sha256_of(path) == original
