"""최초 20개 결과를 그대로 두고 명시적인 후속 답변만 별도로 확인한다."""
import json

from core.config import Settings
from evals import live_features as live
from evals.live_guard import LiveLedger
from llm.router import Router


def test_followup_is_separate_and_preserves_explicit_age_in_preview_and_handoff(tmp_path):
    run = object.__new__(live.FeatureRun)
    run.output = tmp_path / "controlled-followup.json"
    run.request = {"followup_dialogs_path": "evals/datasets/dialogs_followup_20261021.jsonl"}
    run.settings = Settings.from_env(force_demo=True, openai_api_key="", anthropic_api_key="")
    run.report = {"results": {g: [] for g in live.GROUPS}}
    run.ledger = LiveLedger(1, 20)
    run.router = lambda: Router(run.settings)
    run.conversation(supplemental=True)
    result = json.loads(run.output.read_text())
    assert result["results"]["conversation"] == []
    row, = result["supplemental_results"]["conversation_followup"]
    assert all(row["checks"].values()), row
    assert row["original_case_id"] == "h26-d04" and len(row["turns"]) == 2
    assert "만 19~34세" in row["handoff"]["advertiser_input"]
    assert result["summaries"]["conversation"]["attempted_cases"] == 0
    assert result["supplemental_summaries"]["conversation_followup"]["all_checks_passed"] == 1
