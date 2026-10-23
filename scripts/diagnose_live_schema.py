"""One bounded provider diagnostic on a frozen synthetic input, not server usage."""
import json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from openai import OpenAI
from agents.live_pilot import MODEL, run_workflow
from core.schemas import AdCopy, ProductFacts, ProductCategory, Fact
from observability.pricing import estimate_cost_usd

out=Path('live-schema-diagnostic');out.mkdir(exist_ok=True)
case=json.load(open('evals/live_pilot_passage_plan.json'))['cases'][1]
copy=AdCopy(variant_id='live-input',headline=case['headline'],body=case['body'],cta=case['cta'])
product=ProductFacts(name='합성 파일럿',category=ProductCategory.ECOMMERCE,facts=[Fact(id=f'F{i+1}',text=t) for i,t in enumerate(case['facts'])])
record={'mode':'provider-schema-diagnostic-on-Actions','synthetic':True,'human_participants':0,'requested_model':MODEL,'started_at':datetime.now(timezone.utc).isoformat(),'maximum_calls':1,'calls':0,'status':'not_started','cost_usd':None}
key=os.environ['OPENAI_API_KEY']
client=OpenAI(api_key=key,timeout=30,max_retries=0)
def call(*,stage,**kwargs):
    assert record['calls']==0
    assert len(json.dumps(kwargs,ensure_ascii=False).encode())<=90000
    record['calls']+=1
    started=time.perf_counter()
    try:
        response=client.chat.completions.create(model=MODEL,temperature=0,max_completion_tokens=1200,**kwargs)
        record.update(status='success',model=response.model,input_tokens=response.usage.prompt_tokens,output_tokens=response.usage.completion_tokens,cost_usd=estimate_cost_usd(MODEL,response.usage.prompt_tokens,response.usage.completion_tokens))
        return response.choices[0].message.model_dump(include={'role','content','tool_calls'},exclude_none=True)
    except Exception as exc:
        record.update(status='failed',exception_type=type(exc).__name__,http_status=getattr(exc,'status_code',None))
        body=getattr(exc,'body',None)
        if isinstance(body,dict):
            for field in ('code','param','type'):
                value=body.get(field)
                if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',value):record['provider_'+field]=value
            message=body.get('message','')
            # Only a schema-validation message from this public synthetic input.
            # Authentication messages, headers and request objects are never logged.
            if record['http_status']==400 and isinstance(message,str) and message.startswith('Invalid schema for response_format'):
                record['schema_diagnostic']=re.sub(r'sk-[A-Za-z0-9_-]+','[redacted]',message.replace(key,'[redacted]'))[:2000]
        raise
    finally: record['latency_ms']=round((time.perf_counter()-started)*1000,1)
result=run_workflow('rag',copy,product,call)
record.update(workflow_status=result['status'],workflow_reason=result['reason'],finished_at=datetime.now(timezone.utc).isoformat())
(out/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(record,ensure_ascii=False,indent=2))
