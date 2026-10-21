"""Bounded wire smoke infrastructure. Every provider response here is MOCK, not API evidence."""
import hashlib
import json
import shutil
import subprocess
import sys

import pytest

from evals import quality_wire_smoke as smoke
from llm.base import LLMResponse
from tests.fakes import FakeProvider


def prepare_frozen_checkout(tmp_path, checkout):
    for relative in smoke.frozen_paths(checkout):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / relative, target)
    for relative in (smoke.DATASET, smoke.MANIFEST, *smoke.OLD_REPORTS):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / relative, target)
    # Checkout's durable ledger evolves after the paid run; test setup always uses
    # the immutable pre-run baseline committed with its manifest instead.
    committed = json.loads((checkout / smoke.MANIFEST).read_text())
    baseline_bytes = (json.dumps(committed['reservation_baseline'], ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    assert hashlib.sha256(baseline_bytes).hexdigest() == committed['reservation_baseline_sha256']
    (tmp_path / smoke.LEDGER).write_bytes(baseline_bytes)
    manifest = smoke.make_manifest(tmp_path)
    return tmp_path, manifest


@pytest.fixture
def frozen(tmp_path):
    return prepare_frozen_checkout(tmp_path, smoke.ROOT)


def test_validate_only_has_no_provider_or_reservation_side_effect(frozen, monkeypatch):
    root, manifest = frozen
    before = (root / smoke.LEDGER).read_bytes()
    monkeypatch.setattr('llm.openai_provider.OpenAIProvider.__init__', lambda *a, **k: pytest.fail('validate exposed provider'))
    result = smoke.validate_manifest(manifest, root)
    assert result['planned_calls'] == 8
    assert 0 < result['worst_case_reserved_usd'] < .5
    assert (root / smoke.LEDGER).read_bytes() == before
    assert not (root / smoke.OUTPUT).exists()


@pytest.mark.parametrize('field,value', [('model', 'gpt-other'), ('reasoning_effort', 'low'),
    ('max_calls', 9), ('cost_cap_usd', .51), ('sdk_retries', 1), ('fallback', True),
    ('input_price_per_million_usd', 0), ('case_ids', ['QP-H02']), ('orders', ['ab'])])
def test_manifest_contract_tampering_is_rejected(frozen, field, value):
    root, manifest = frozen
    manifest[field] = value
    with pytest.raises(ValueError):
        smoke.validate_manifest(manifest, root)


@pytest.mark.parametrize('relative', ['evals/quality_judge.py', smoke.DATASET, smoke.OLD_REPORTS[0]])
def test_frozen_input_source_and_old_report_mutation_is_rejected(frozen, relative):
    root, manifest = frozen
    with (root / relative).open('ab') as stream:
        stream.write(b'\nchanged')
    with pytest.raises(ValueError):
        smoke.validate_manifest(manifest, root)


def test_missing_hash_cannot_remove_source_from_freeze(frozen):
    root, manifest = frozen
    manifest['source_sha256'].pop('evals/quality_judge.py')
    with pytest.raises(ValueError):
        smoke.validate_manifest(manifest, root)


def test_reservation_keeps_old_two_dollars_and_rejects_duplicates(frozen):
    root, manifest = frozen
    before = json.loads((root / smoke.LEDGER).read_text())
    result = smoke.reserve_budget(manifest, root, workflow_run_id='mock-workflow')
    assert result['reservations'][:-1] == before['reservations']
    assert result['reserved_total_usd'] == '2.5'
    assert result['reservations'][-1]['reserved_cap_usd'] == '0.5'
    assert json.loads((root / smoke.OUTPUT).read_text())['status'] == 'reserved'
    with pytest.raises(ValueError):
        smoke.reserve_budget(manifest, root, workflow_run_id='mock-workflow')


def test_rewritten_old_reservation_or_total_is_rejected(frozen):
    root, manifest = frozen
    ledger = json.loads((root / smoke.LEDGER).read_text())
    ledger['reservations'][0]['reserved_cap_usd'] = '1'
    ledger['reserved_total_usd'] = '1'
    (root / smoke.LEDGER).write_text(json.dumps(ledger))
    with pytest.raises(ValueError):
        smoke.reserve_budget(manifest, root, workflow_run_id='mock-workflow')


def mock_wire(req):
    """TIE fixture with real source quotations; explicitly a controlled MOCK."""
    data = req.context
    dims = []
    for criterion in ('information', 'conditions', 'action', 'expression'):
        field = 'cta' if criterion == 'action' else 'headline'
        dims.append({'criterion': criterion, 'judgment': 'TIE',
                     'evidence_a': [{'field': field, 'quote': data['copies']['A'][field]}],
                     'evidence_b': [{'field': field, 'quote': data['copies']['B'][field]}],
                     'fact_ids': [], 'reason': 'Controlled MOCK tie, not model quality evidence.'})
    raw = {'dimensions': dims, 'winner': 'TIE', 'decisive_criteria': [], 'reason': 'MOCK'}
    return LLMResponse(text=json.dumps(raw, ensure_ascii=False), parsed=req.schema.model_validate(raw),
                       provider='openai', model=smoke.MODEL, input_tokens=100, output_tokens=100)


def test_one_order_failure_is_retained_separately_from_contract_acceptance(frozen):
    root, manifest = frozen
    calls = []
    def behavior(req):
        calls.append(req)
        assert 'QP-H' not in req.user and 'QP-H' not in req.label
        assert 'better' not in req.user and 'reason_code' not in req.user
        if len(calls) == 2:
            raise RuntimeError('secret-must-not-appear')
        return mock_wire(req)
    provider = FakeProvider('openai', model=smoke.MODEL, behavior=behavior)
    report = smoke.execute_smoke(manifest, provider, root / smoke.OUTPUT,
                                 workflow_run_id='mock-workflow', source_sha='mock-source',
                                 reservation_commit='mock-reservation', root=root)
    stored = json.loads((root / smoke.OUTPUT).read_text())
    assert stored == report
    assert len(calls) == report['ledger']['attempts'] == 8
    assert report['status'] == 'completed_with_errors'
    first = report['results'][0]
    assert first['orders'][0]['wire_text'] and first['orders'][0]['response']
    assert first['orders'][1]['error_type'] == 'RuntimeError'
    assert first['execution'] == 'failed' and first['accepted_winner'] is None
    assert first['schema_valid_orders'] == 1
    assert report['ledger']['unknown_cost_calls'] == 1
    assert 'secret-must-not-appear' not in json.dumps(report)
    assert report['ledger']['accounted_usd'] > report['ledger']['usage_cost_usd']


def test_uncommitted_reservation_never_reaches_key_loading(frozen, monkeypatch):
    root, manifest = frozen
    smoke.reserve_budget(manifest, root, workflow_run_id='mock-workflow')
    with pytest.raises(ValueError, match='reservation'):
        smoke.verify_durable_reservation(manifest, root, workflow_run_id='mock-workflow', commit='not-a-commit')


def test_finalize_retains_interrupted_response_and_reservation(frozen):
    root, manifest = frozen
    smoke.reserve_budget(manifest, root, workflow_run_id='mock-workflow')
    report = json.loads((root / smoke.OUTPUT).read_text())
    report.update(status='running', call_responses=[{'wire_text': 'MOCK-first-response'}])
    (root / smoke.OUTPUT).write_text(json.dumps(report))
    smoke.finalize(root / smoke.OUTPUT)
    result = json.loads((root / smoke.OUTPUT).read_text())
    assert result['status'] == 'interrupted'
    assert result['call_responses'] == report['call_responses']
    assert result['reservation_cap_usd'] == .5


def test_usage_mismatch_preserves_completed_raw_response_and_stops(frozen):
    root, manifest = frozen
    def wrong_usage(req):
        response = mock_wire(req)
        response.cost_estimated = True
        return response
    provider = FakeProvider('openai', model=smoke.MODEL, behavior=wrong_usage)
    report = smoke.execute_smoke(manifest, provider, root / smoke.OUTPUT,
                                 workflow_run_id='mock', source_sha='mock',
                                 reservation_commit='mock', root=root)
    assert report['ledger']['attempts'] == 1
    assert report['ledger']['stopped'] is True
    assert report['call_responses'][0]['wire_text']
    assert report['ledger']['accounted_usd'] == report['ledger']['calls'][0]['reserved_usd']
    assert report['results'][0]['orders'][0]['error_type'] == 'BudgetExceeded'


def test_durable_reservation_checks_committed_manifest_and_remote_head(frozen, monkeypatch):
    root, manifest = frozen
    ledger = smoke.reserve_budget(manifest, root, workflow_run_id='mock-workflow')
    changed = None
    def git(_root, *args):
        if args[0] == 'rev-parse':
            return 'reservation-commit'
        if args[0] == 'show':
            if args[1].endswith(smoke.LEDGER):
                return json.dumps(ledger)
            return json.dumps({} if changed == 'manifest' else manifest)
        if args[0] == 'ls-remote':
            return ('changed-head' if changed == 'remote' else 'reservation-commit') + '\trefs/heads/' + smoke.BRANCH
        if args[0] == 'status':
            return ' M evals/quality_judge.py' if changed == 'dirty' else ''
        raise AssertionError('unexpected git operation')
    monkeypatch.setattr(smoke, '_git', git)
    smoke.verify_durable_reservation(manifest, root, workflow_run_id='mock-workflow', commit='reservation-commit')
    for changed in ('manifest', 'remote', 'dirty'):
        with pytest.raises(ValueError):
            smoke.verify_durable_reservation(manifest, root, workflow_run_id='mock-workflow', commit='reservation-commit')
    with pytest.raises(ValueError):
        smoke.verify_durable_reservation(manifest, root, workflow_run_id='different-workflow', commit='reservation-commit')


@pytest.mark.parametrize('name,value', [('GITHUB_REF_NAME', 'main'), ('GITHUB_EVENT_NAME', 'workflow_dispatch'),
                                      ('GITHUB_RUN_ATTEMPT', '2')])
def test_workflow_rerun_and_nonpush_entrypoints_are_rejected(monkeypatch, name, value):
    for key, item in {'GITHUB_REF_NAME': smoke.BRANCH, 'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_ATTEMPT': '1'}.items():
        monkeypatch.setenv(key, item)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        smoke._workflow_guard()


def test_fixture_rebuilds_pre_run_baseline_after_checkout_has_reserved_smoke(frozen, tmp_path):
    checkout, manifest = frozen
    # Model a committed post-run checkout using temporary files only.
    (checkout / smoke.MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    smoke.reserve_budget(manifest, checkout, workflow_run_id='mock-already-completed-workflow')
    reserved_bytes = (checkout / smoke.LEDGER).read_bytes()
    with pytest.raises(ValueError, match='already reserved'):
        smoke.validate_manifest(manifest, checkout)
    with pytest.raises(ValueError, match='already reserved'):
        smoke.make_manifest(checkout)
    cli = subprocess.run([sys.executable, '-m', 'evals.quality_wire_smoke', '--validate-only'],
                         cwd=checkout, capture_output=True, text=True)
    assert cli.returncode == 1 and cli.stdout.strip() == 'Smoke failed: ValueError'

    # The same helper used by every smoke test must still create a usable fixture.
    independent_root, independent_manifest = prepare_frozen_checkout(tmp_path / 'later-ci-fixture', checkout)
    assert smoke.validate_manifest(independent_manifest, independent_root)['planned_calls'] == 8
    assert json.loads((independent_root / smoke.LEDGER).read_text()) == manifest['reservation_baseline']
    assert independent_manifest['reservation_baseline_sha256'] == manifest['reservation_baseline_sha256']
    assert (checkout / smoke.LEDGER).read_bytes() == reserved_bytes
    with pytest.raises(ValueError, match='already reserved'):
        smoke.validate_manifest(manifest, checkout)


@pytest.mark.parametrize('mode,expected_error', [('truncation', 'TruncatedOutput'),
    ('schema', 'InvalidJudgeSchema'), ('refusal', 'ModelRefusal')])
def test_sdk_parse_failures_keep_allowlisted_raw_completion_and_usage(frozen, mode, expected_error):
    import importlib
    from openai import DefaultHttpxClient
    # Match the installed SDK's public client base (httpx or httpx2).
    httpx = importlib.import_module(DefaultHttpxClient.__mro__[1].__module__.split('.')[0])
    root, manifest = frozen
    requests = []
    sentinel = 'MOCK-RAW-' + mode
    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={'x-sensitive': 'never-store-header'}, json={
            'id': 'mock-completion', 'object': 'chat.completion', 'created': 1, 'model': smoke.MODEL,
            'usage': {'prompt_tokens': 111, 'completion_tokens': 222, 'total_tokens': 333,
                      'do_not_store': 'never-store-usage-extra'},
            'choices': [{'index': 0, 'finish_reason': 'length' if mode == 'truncation' else 'stop',
                         'message': {'role': 'assistant', 'content': json.dumps({'sentinel': sentinel}),
                                     'refusal': sentinel if mode == 'refusal' else None,
                                     'private_field': 'never-store-message-extra'}}],
            'private_top_level': 'never-store-body-extra'})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    provider = smoke.SmokeOpenAIProvider('mock-key-not-real', http_client=client)
    try:
        report = smoke.execute_smoke(manifest, provider, root / smoke.OUTPUT,
                                     workflow_run_id='mock-sdk', source_sha='mock-sdk',
                                     reservation_commit='mock-reservation', root=root)
    finally:
        client.close()
    assert len(requests) == report['ledger']['attempts'] == 8  # no retries or repair
    assert report['status'] == 'completed_with_errors'
    assert all(r['execution'] == 'failed' and r['accepted_winner'] is None for r in report['results'])
    assert report['results'][0]['orders'][0]['error_type'] == expected_error
    raw = report['raw_completions'][0]
    assert raw['ordinal'] == 1 and raw['order'] == 'ab'
    assert sentinel in raw['completion']['choices'][0]['message']['content']
    assert raw['completion']['model'] == smoke.MODEL
    assert raw['completion']['usage'] == {'prompt_tokens': 111, 'completion_tokens': 222, 'total_tokens': 333}
    assert raw['completion']['choices'][0]['finish_reason'] == ('length' if mode == 'truncation' else 'stop')
    assert report['ledger']['unknown_cost_calls'] == 8 and report['ledger']['usage_cost_usd'] == 0
    assert report['ledger']['accounted_usd'] == sum(c['reserved_usd'] for c in report['ledger']['calls'])
    stored = (root / smoke.OUTPUT).read_text()
    assert 'never-store-' not in stored and 'mock-key-not-real' not in stored
