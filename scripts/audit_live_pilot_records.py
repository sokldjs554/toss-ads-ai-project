"""Offline cross-check of explicitly exported synthetic pilot records; no network."""
import csv
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo))
from core.schemas import AdCopy,ProductFacts
from guardrails.output_guard import check
from guardrails.policy import get_catalog

source=Path(sys.argv[1]); out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
rows=json.loads(source.read_text())['rows']
cases=json.loads((repo/'evals/datasets/live_pilot_20261023.json').read_text())['cases']
reports=[]; human=[]
for row in rows:
    payload=row['payload']; result=row['result']; ad=AdCopy.model_validate(result['copy']);product=ProductFacts.model_validate(payload['product'])
    original=payload['copy']; catalog=get_catalog()
    code=check(ad,product,catalog.categories_for(product,ad.full_text()),catalog=catalog)
    docs={d['id']:d for d in result['sources']};review=result.get('review')
    citations_ok=None if review is None else all(c['source_id'] in docs and c['quote'] and c['quote'] in docs[c['source_id']]['text'] for c in review['citations'])
    spans_ok=None if review is None else all(ad.full_text()[f['start']:f['end']]==f['ad_quote'] for f in review['findings'])
    kinds=None if review is None else sorted({docs[c['source_id']]['kind'] for c in review['citations'] if c['source_id'] in docs})
    name=next((c['id'] for c in cases if all(original[k]==c[k] for k in ('headline','body','cta')) and payload['path']==c['path']),None)
    if name is None:
        minimum=json.loads((repo/'evals/datasets/live_pilot_structured_holdout_20261023.json').read_text())
        fresh=json.loads((repo/'evals/live_pilot_passage_plan.json').read_text())['cases'][1]
        if all(original[k]==minimum[k] for k in ('headline','body','cta')):
            name='structured-holdout-minimum-purchase'
        elif all(original[k]==fresh[k] for k in ('headline','body','cta')):
            name='passage-rag-new'
        else:
            assert payload.get('parent'), 'unclassified input'
            name='manual-edit'
    content={k:v for k,v in payload.items() if k!='parent'}
    report={'case_id':name,'request_id':row['request_id'],'account':row['account_id'],'build':row['build'],'prompt_version':result['prompt_version'],'policy_version':result['policy_version'],'created_at':row['created_at'],'finished_at':row['finished_at'],'path':result['path'],'status':result['status'],'decision':result['decision'],'reason':result['reason'],'original_code':result['original_code_decision'],'final_code':result['code_decision'],'offline_code_matches':code.decision.value==result['code_decision'],'source_hashes_valid':all(hashlib.sha256(d['text'].encode()).hexdigest()==d['sha256'] for d in docs.values()) if docs else None,'citation_membership':citations_ok,'citation_kinds':kinds,'finding_spans_match':spans_ok,'tool_calls':len(result['tool_checks']),'tool_call_ids':[t['tool_call_id'] for t in result['tool_checks']],'content_sha256_without_parent':hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'call_count':row['call_count'],'audit_kind':(row.get('review') or {}).get('actor_kind'),'audit_decision':(row.get('review') or {}).get('decision')}
    assert report['offline_code_matches']
    assert all(t['tool_call_id'] and t['decision'] in {'APPROVE','REVISE','REJECT'} for t in result['tool_checks'])
    if review:
        assert citations_ok and spans_ok and set(kinds)=={'policy','fact'}
    reports.append(report)
    human.append({'request_id':row['request_id'],'case':name,'prompt':result['prompt_version'],'original_ad':'\n'.join(original[k] for k in ('headline','body','cta')),'review_ad':ad.full_text(),'product_facts':'\n'.join(f.text for f in product.facts),'code_result':result['code_decision'],'model_result':review['recommendation'] if review else 'no validated review','model_reason':review['reason'] if review else result['reason'],'source_quotes':json.dumps(review['citations'],ensure_ascii=False) if review else '', 'retrieved_policy_texts':json.dumps([d for d in result['sources'] if d['kind']=='policy'],ensure_ascii=False),'criteria':'Judge policy applicability, factual entailment, missing conditions, preserved facts after rewrite, and valid ad spans separately. Exact quotation alone is insufficient. AI synthetic probe; no human gold yet.','human_judgment':'PENDING','human_reason':''})
calls=[c for r in rows for c in (r['calls'] or [])]
summary={'scope':'Automated synthetic API executions only; no independent human gold','requests':len(rows),'provider_calls':len(calls),'provider_success':sum(c['status']=='success' for c in calls),'provider_failure_or_unknown':sum(c['status']!='success' for c in calls),'input_tokens':sum(c.get('input_tokens',0) for c in calls),'output_tokens':sum(c.get('output_tokens',0) for c in calls),'estimated_cost_usd':str(sum((Decimal(str(c['cost_usd'])) for c in calls if c.get('cost_usd') is not None),Decimal(0))),'unknown_cost_calls':sum(c.get('cost_usd') is None for c in calls),'reserved_usd':str(sum((Decimal(str(r['reserved_usd'])) for r in rows),Decimal(0))),'actual_models':sorted({c['model'] for c in calls if c.get('model')}),'human_participants_verified':0,'runs':reports}
comparisons=[]
for c in cases:
    matched=[r for r in reports if r['case_id']==c['id']]
    if len(matched)==2:
        before=next(r for r in matched if r['prompt_version']=='live-rag-tools-1');after=next(r for r in matched if r['prompt_version']=='live-rag-tools-2')
        same=before['content_sha256_without_parent']==after['content_sha256_without_parent']
        assert same
        comparisons.append({'case_id':c['id'],'same_ad_product_payload':same,'before':before['status']+':'+before['reason'],'after':after['status']+':'+after['reason'],'before_decision':before['decision'],'after_decision':after['decision']})
minimum=[r for r in reports if r['case_id']=='structured-holdout-minimum-purchase']
if len(minimum)>=2:
    before=minimum[0]
    for after in minimum[1:]:
        assert before['content_sha256_without_parent']==after['content_sha256_without_parent']
        comparisons.append({'case_id':'structured-holdout-minimum-purchase','same_ad_product_payload':True,'before_prompt':before['prompt_version'],'after_prompt':after['prompt_version'],'before':before['status']+':'+before['reason'],'after':after['status']+':'+after['reason'],'before_decision':before['decision'],'after_decision':after['decision'],'split':'v2 heldout became later development comparison, not independent evaluation'})
summary['paired_development_comparisons']=comparisons
summary['by_prompt']=[]
for prompt in sorted({r['result']['prompt_version'] for r in rows}):
    group=[r for r in rows if r['result']['prompt_version']==prompt]
    gc=[c for r in group for c in (r['calls'] or [])]
    summary['by_prompt'].append({'prompt':prompt,'requests':len(group),'calls':len(gc),'input_tokens':sum(c.get('input_tokens',0) for c in gc),'output_tokens':sum(c.get('output_tokens',0) for c in gc),'estimated_cost_usd':str(sum((Decimal(str(c['cost_usd'])) for c in gc if c.get('cost_usd') is not None),Decimal(0))),'human_review':sum(r['result']['status']=='human_review' for r in group),'hold':sum(r['result']['status']=='hold' for r in group)})
(out/'offline-audit.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
with (out/'human-semantic-review.csv').open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(human[0]));w.writeheader();w.writerows(human)
print(json.dumps({k:v for k,v in summary.items() if k!='runs'},ensure_ascii=False,indent=2))
