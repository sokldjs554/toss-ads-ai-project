"""One-shot, separately reserved wire/API smoke. Default CLI only validates; no Router or retries.

Freeze after peer changes settle: python -m evals.quality_wire_smoke --freeze
The workflow reserves and pushes the full cap before its only secret-bearing step.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

from core.schemas import AdCopy, Fact
from evals.live_guard import APPROVED_MODELS, BoundedProvider, LiveLedger, request_reservation
from evals.quality_judge import WIRE_CONTRACT_VERSION, compare
from scripts.claim_remaining_evaluation import reserve
from llm.openai_provider import OpenAIProvider

ROOT = Path(__file__).resolve().parents[1]
BRANCH = 'codex/resume-ads-20261021'
RUN_ID = 'quality-wire-20261021-smoke1'
MODEL = 'gpt-5.4-mini-2026-03-17'
CASES = ['QP-H01', 'QP-H05', 'QP-H13', 'QP-H25']
MANIFEST = 'evals/quality_wire_smoke_20261021.json'
DATASET = 'evals/datasets/quality_pairs.jsonl'
LEDGER = 'evals/reports/live_features/reservations_remaining_20261021.json'
OUTPUT = f'evals/reports/quality_wire_smoke/{RUN_ID}.json'
OLD_REPORTS = tuple(f'evals/reports/live_features/{name}.json' for name in (
    'features-20261020-run1', 'features-20261020-run2', 'features-20261020-run3',
    'features-20261021-run1', 'reservations'))


class SmokeOpenAIProvider(OpenAIProvider):
    """Observe allowlisted successful HTTP completions before SDK parsing can fail.

    Inherited complete() keeps the normal strict parse/error behavior. The hook
    reads the same HTTP response; it cannot create another API request.
    """
    def __init__(self, api_key: str, *, http_client=None):
        super().__init__(api_key, MODEL, max_retries=0, reasoning_effort='none')
        self._http_client = http_client
        self.observe_completion = None

    def _observe_http(self, response):
        if not 200 <= response.status_code < 300 or self.observe_completion is None:
            return  # Error bodies, headers and requests are never recorded.
        response.read()
        try:
            body = response.json()
        except ValueError:
            return
        if not isinstance(body, dict) or not isinstance(body.get('choices'), list):
            return
        completion = {'choices': []}
        if isinstance(body.get('model'), str):
            completion['model'] = body['model']
        usage = body.get('usage')
        if isinstance(usage, dict):
            completion['usage'] = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                   if type(usage.get(k)) is int and usage[k] >= 0}
        for choice in body['choices']:
            if not isinstance(choice, dict):
                continue
            kept = {}
            if isinstance(choice.get('finish_reason'), str):
                kept['finish_reason'] = choice['finish_reason']
            message = choice.get('message')
            if isinstance(message, dict):
                kept['message'] = {k: message[k] for k in ('content', 'refusal')
                                   if isinstance(message.get(k), str) or k in message and message[k] is None}
            completion['choices'].append(kept)
        self.observe_completion(completion)

    def _get_client(self):
        if self._client is None:
            from openai import DefaultHttpxClient, OpenAI
            client = self._http_client if self._http_client is not None else DefaultHttpxClient()
            client.event_hooks['response'].append(self._observe_http)
            self._client = OpenAI(api_key=self._api_key, timeout=self._timeout,
                                  max_retries=0, http_client=client)
        return self._client


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def frozen_paths(root: Path) -> list[str]:
    paths = {'requirements.txt', '.github/workflows/quality-wire-smoke.yml',
             'evals/__init__.py', 'evals/live_guard.py', 'evals/quality_judge.py',
             'evals/quality_wire_smoke.py', 'scripts/claim_remaining_evaluation.py',
             'tests/test_quality_wire_smoke.py'}
    for directory in ('core', 'llm', 'guardrails', 'observability'):
        paths.update(str(p.relative_to(root)) for p in (root / directory).rglob('*')
                     if p.is_file() and p.suffix in {'.py', '.yaml'})
    return sorted(paths)


def _fixed() -> dict:
    return dict(version=1, run_id=RUN_ID, branch=BRANCH, provider='openai', model=MODEL,
                reasoning_effort='none', sdk_retries=0, fallback=False, repair=False,
                max_calls=8, cost_cap_usd=.5, input_price_per_million_usd=.75,
                output_price_per_million_usd=4.5, case_ids=CASES, orders=['ab', 'ba'],
                wire_contract_version=WIRE_CONTRACT_VERSION, budget_scope='remaining-20261021',
                output=OUTPUT, dataset=DATASET)


def make_manifest(root: Path = ROOT) -> dict:
    baseline = json.loads((root / LEDGER).read_text())
    if any(r['run_id'] == RUN_ID for r in baseline['reservations']):
        raise ValueError('cannot freeze an already reserved run')
    return {**_fixed(), 'source_sha256': {p: _hash((root / p).read_bytes()) for p in frozen_paths(root)},
            'input_sha256': {DATASET: _hash((root / DATASET).read_bytes())},
            'existing_report_sha256': {p: _hash((root / p).read_bytes()) for p in OLD_REPORTS},
            'reservation_baseline': baseline,
            'reservation_baseline_sha256': _hash((root / LEDGER).read_bytes()),
            'prior_reservation_ledger_sha256': _hash((root / OLD_REPORTS[-1]).read_bytes()),
            'purpose': 'Bounded SDK/API boundary smoke, not a new full evaluation or independent quality measurement.'}


def _pairs(root: Path) -> list[dict]:
    rows = [json.loads(line) for line in (root / DATASET).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    found = []
    for case in CASES:
        selected = [r for r in rows if r['id'] == case]
        if len(selected) != 1 or selected[0]['split'] != 'holdout':
            raise ValueError('fixed smoke case missing or duplicated')
        found.append(selected[0])
    return found


def _inputs(pair):
    return AdCopy(**pair['a']), AdCopy(**pair['b']), [Fact(**f) for f in pair['facts']]


def _check_reservation(manifest: dict, current: dict, workflow_run_id: str | None = None) -> dict:
    base = manifest['reservation_baseline']
    if current.get('reservations', [])[:-1] != base['reservations']:
        raise ValueError('reservation history changed')
    if any(current.get(k) != base.get(k) for k in ('version', 'scope', 'total_cap_usd', 'prior_ledger_sha256')):
        raise ValueError('reservation scope changed')
    rows = current.get('reservations', [])
    row = rows[-1] if rows else {}
    expected_total = sum(float(r['reserved_cap_usd']) for r in rows)
    if (len(rows) != len(base['reservations']) + 1 or row.get('run_id') != RUN_ID
            or row.get('manifest_sha256') != _hash(_json_bytes(manifest))
            or row.get('reserved_cap_usd') != '0.5'
            or float(current.get('reserved_total_usd', -1)) != expected_total
            or expected_total > 4 or (workflow_run_id is not None and row.get('workflow_run_id') != workflow_run_id)):
        raise ValueError('reservation does not match this frozen run')
    return row


def validate_manifest(manifest: dict, root: Path = ROOT, *, allow_reserved: bool = False) -> dict:
    for key, value in _fixed().items():
        if manifest.get(key) != value or type(manifest.get(key)) is not type(value):
            raise ValueError('frozen smoke contract differs: ' + key)
    if APPROVED_MODELS.get(MODEL) != ('openai', .75, 4.5):
        raise ValueError('live ledger pricing changed')
    for key, paths in (('source_sha256', frozen_paths(root)), ('input_sha256', [DATASET]),
                       ('existing_report_sha256', list(OLD_REPORTS))):
        expected = {p: _hash((root / p).read_bytes()) for p in paths}
        if manifest.get(key) != expected:
            raise ValueError('frozen hashes differ: ' + key)
    baseline = manifest['reservation_baseline']
    if _hash(_json_bytes(baseline)) != manifest['reservation_baseline_sha256']:
        raise ValueError('reservation baseline hash differs')
    # Reuse the production cumulative-cap validator without writing or calling a provider.
    reserve(manifest, copy.deepcopy(baseline), workflow_run_id='validation-only',
            request_sha256=_hash(_json_bytes(manifest)),
            prior_ledger_sha256=manifest['prior_reservation_ledger_sha256'])
    current_bytes = (root / LEDGER).read_bytes()
    current = json.loads(current_bytes)
    if current != baseline or _hash(current_bytes) != manifest['reservation_baseline_sha256']:
        if not allow_reserved:
            raise ValueError('reservation ledger changed or this run already reserved')
        _check_reservation(manifest, current)
    requests = []
    class Capture:
        def complete(self, request):
            requests.append(request)
            raise RuntimeError('validation-only request capture')
    for pair in _pairs(root):
        compare(*_inputs(pair), Capture(), label='quality-wire-smoke')
    amounts = [request_reservation(req, MODEL)[0] for req in requests]
    if len(requests) != 8 or sum(amounts) > .5:
        raise ValueError('planned requests exceed the bounded smoke budget')
    return {'planned_calls': len(requests), 'worst_case_reserved_usd': sum(amounts)}


def _initial_report(manifest, workflow_run_id, source_sha=''):
    return dict(schema_version=1, kind='real-api-wire-smoke', run_id=RUN_ID, status='reserved',
                manifest=copy.deepcopy(manifest), manifest_sha256=_hash(_json_bytes(manifest)),
                source_sha=source_sha, workflow_run_id=workflow_run_id, reservation_cap_usd=.5,
                started_at=datetime.now(timezone.utc).isoformat(), results=[], call_responses=[], raw_completions=[],
                limitations=['Four previously seen pairs; API/wire boundary smoke only. No independent human or advertising-performance validation.',
                             'Schema/evidence checks and acceptance are separate; failed orders are failures.'])


def reserve_budget(manifest: dict, root: Path = ROOT, *, workflow_run_id: str) -> dict:
    validate_manifest(manifest, root)
    if (root / OUTPUT).exists():
        raise ValueError('smoke output already exists; automatic rerun forbidden')
    current = json.loads((root / LEDGER).read_text())
    result = reserve(manifest, current, workflow_run_id=workflow_run_id,
                     request_sha256=_hash(_json_bytes(manifest)),
                     prior_ledger_sha256=manifest['prior_reservation_ledger_sha256'])
    _write(root / LEDGER, result)
    _write(root / OUTPUT, _initial_report(manifest, workflow_run_id, os.environ.get('GITHUB_SHA', '')))
    return result


def _git(root: Path, *args) -> str:
    return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()


def _workflow_guard() -> None:
    if (os.environ.get('GITHUB_REF_NAME') != BRANCH or os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'):
        raise ValueError('only the first push workflow on the authorized branch may reserve or spend')


def verify_durable_reservation(manifest: dict, root: Path = ROOT, *, workflow_run_id: str, commit: str) -> None:
    validate_manifest(manifest, root, allow_reserved=True)
    current = json.loads((root / LEDGER).read_text())
    _check_reservation(manifest, current, workflow_run_id)
    try:
        if _git(root, 'rev-parse', 'HEAD') != commit:
            raise ValueError('reservation commit is not HEAD')
        committed = json.loads(_git(root, 'show', f'{commit}:{LEDGER}'))
        committed_manifest = json.loads(_git(root, 'show', f'{commit}:{MANIFEST}'))
        remote = _git(root, 'ls-remote', 'origin', f'refs/heads/{BRANCH}').split()
        if committed != current or committed_manifest != manifest or not remote or remote[0] != commit:
            raise ValueError('reservation commit is not durably stored on its branch')
        if _git(root, 'status', '--porcelain', '--', *frozen_paths(root), DATASET, LEDGER, MANIFEST):
            raise ValueError('reservation/source/input working tree changed')
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError('durable reservation cannot be verified') from exc


def execute_smoke(manifest, provider, output: Path, *, workflow_run_id: str, source_sha: str,
                  reservation_commit: str, root: Path = ROOT) -> dict:
    """Run exactly the frozen compare calls. Injected providers are only used by offline tests."""
    report = _initial_report(manifest, workflow_run_id, source_sha)
    report.update(status='running', reservation_commit=reservation_commit)
    def checkpoint(summary):
        report['ledger'] = summary
        _write(output, report)
    ledger = LiveLedger(.5, 8, checkpoint=checkpoint)
    class Record:
        name, model = provider.name, provider.model
        available = provider.available
        def complete(self, request):
            entry = {'ordinal': len(ledger.calls), 'order': request.label.rsplit(':', 1)[-1]}
            if isinstance(provider, SmokeOpenAIProvider):
                def observe(completion):
                    report['raw_completions'].append({**entry, 'completion': completion})
                    checkpoint(ledger.summary())
                provider.observe_completion = observe
            try:
                response = provider.complete(request)
                entry.update(wire_text=response.text,
                             wire_response=response.parsed.model_dump(mode='json') if hasattr(response.parsed, 'model_dump') else response.parsed,
                             provider=response.provider, model=response.model,
                             input_tokens=response.input_tokens, output_tokens=response.output_tokens)
                return response
            except Exception as exc:
                entry['error_type'] = type(exc).__name__
                raise
            finally:
                report['call_responses'].append(entry)
                checkpoint(ledger.summary())
    bounded = BoundedProvider(Record(), ledger)
    checkpoint(ledger.summary())
    try:
        for pair in _pairs(root):
            start = len(ledger.calls)
            result = compare(*_inputs(pair), bounded, label='quality-wire-smoke')
            failed = any(o.get('error_type') for o in result['orders'])
            result.update(case_id=pair['id'], execution='failed' if failed else 'completed',
                          schema_valid_orders=sum(o.get('response') is not None for o in result['orders']),
                          contract_valid_orders=sum(o.get('evidence_valid') is True for o in result['orders']),
                          accepted_winner=result['winner'] if result['status'] == 'consensus' else None,
                          call_ordinals=list(range(start + 1, len(ledger.calls) + 1)))
            report['results'].append(result)
            checkpoint(ledger.summary())
            if result.get('budget_stopped'):
                break
        complete = len(report['results']) == 4 and all(r['execution'] == 'completed' for r in report['results'])
        report['status'] = 'completed' if complete else 'completed_with_errors'
    except BaseException as exc:
        report.update(status='interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed',
                      error_type=type(exc).__name__)
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        checkpoint(ledger.summary())
    return report


def finalize(output: Path) -> None:
    if output.exists():
        report = json.loads(output.read_text())
        if report.get('status') in {'reserved', 'running'}:
            report.update(status='interrupted', finished_at=datetime.now(timezone.utc).isoformat())
            _write(output, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    for mode in ('freeze', 'validate-only', 'reserve', 'run', 'finalize'):
        group.add_argument('--' + mode, action='store_true')
    parser.add_argument('--reservation-commit', default='')
    args = parser.parse_args()
    try:
        if args.freeze:
            manifest = make_manifest()
            validate_manifest(manifest)
            _write(ROOT / MANIFEST, manifest)
            print('Frozen smoke manifest written; no reservation or provider call.')
            return 0
        if args.finalize:
            finalize(ROOT / OUTPUT)
            return 0
        raw = (ROOT / MANIFEST).read_bytes()
        manifest = json.loads(raw)
        if raw != _json_bytes(manifest):
            raise ValueError('manifest must use canonical freeze serialization')
        if not args.reserve and not args.run:
            print(json.dumps(validate_manifest(manifest)))
            return 0
        _workflow_guard()
        run_id = os.environ['GITHUB_RUN_ID']
        if args.reserve:
            if _git(ROOT, 'rev-parse', 'HEAD') != os.environ['GITHUB_SHA']:
                raise ValueError('branch HEAD lease changed before reservation')
            remote = _git(ROOT, 'ls-remote', 'origin', f'refs/heads/{BRANCH}').split()
            if not remote or remote[0] != os.environ['GITHUB_SHA']:
                raise ValueError('remote branch moved before reservation')
            reserve_budget(manifest, workflow_run_id=run_id)
            return 0
        verify_durable_reservation(manifest, workflow_run_id=run_id, commit=args.reservation_commit)
        report = json.loads((ROOT / OUTPUT).read_text())
        if report.get('status') != 'reserved' or report.get('workflow_run_id') != run_id:
            raise ValueError('smoke already started or belongs to another workflow')
        # The only key read and provider construction happen after durable verification.
        key = os.environ.get('OPENAI_API_KEY') or os.environ.get('OPEN_API_KEY')
        if not key:
            raise ValueError('provider key missing')
        provider = SmokeOpenAIProvider(key)
        report = execute_smoke(manifest, provider, ROOT / OUTPUT, workflow_run_id=run_id,
                               source_sha=os.environ['GITHUB_SHA'], reservation_commit=args.reservation_commit)
        return 0 if report['status'] == 'completed' else 1
    except Exception as exc:
        # Exceptions may carry credentials or request text. Persist/print only the class.
        print('Smoke failed: ' + type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
