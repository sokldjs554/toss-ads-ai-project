from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

TITLE = '런타임 호환성 확인 광고'
ANNOTATOR = '호환성 검증자'
PROBE = '''import sys
import streamlit as st
import pydantic
from core.schemas import AdCopy
from ui import review_queue_storage as storage
st.title("브라우저 자동 저장 확인")
st.caption(f"Python {sys.version.split()[0]} · Streamlit {st.__version__} · pydantic {pydantic.__version__}")
if storage.ready():
    queue = st.session_state["rq_queue"]
    if st.button("검증 광고 추가", disabled=len(queue) > 0):
        queue.enqueue_ad(AdCopy(headline="런타임 호환성 확인 광고", body="복원 확인용 가상 문구입니다", cta="확인"), dataset_id="stlite-compatibility", category="other")
        st.rerun()
    for item in queue.items():
        st.write(item.ad_copy.headline)
    storage.save_changes()
'''


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def snapshot(page):
    value = page.evaluate('''() => new Promise((resolve,reject) => {
      const request=indexedDB.open('preflight.review_queue',1);
      request.onerror=()=>reject(String(request.error));
      request.onsuccess=()=>{ const db=request.result;
        const tx=db.transaction('snapshots','readonly');
        const get=tx.objectStore('snapshots').get('preflight-studio-review-v1');
        get.onsuccess=()=>resolve(get.result || null);
        tx.oncomplete=()=>db.close(); tx.onerror=()=>reject(String(tx.error));
      };
    })''')
    if value is None:
        return None
    raw=value.get('jsonl','')
    return dict(revision=value.get('revision'), jsonl_sha256=hashlib.sha256(raw.encode()).hexdigest(), has_title=TITLE in raw, has_claiming_annotator=ANNOTATOR in raw)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--repo',type=Path,default=Path.cwd())
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--build',type=Path,default=Path('/tmp/preflight-storage-runtime-build'))
    ap.add_argument('--port',type=int,default=8635)
    ap.add_argument('--versions',nargs='+',default=['1.11.0','1.12.0'])
    args=ap.parse_args()
    root=args.repo.resolve();out=args.out.resolve();build=args.build.resolve()
    out.mkdir(parents=True,exist_ok=True);build.mkdir(parents=True,exist_ok=True)
    assert not (out/'result.json').exists(), 'Use a fresh evidence folder'
    sys.path.insert(0,str(root))
    from scripts.build_site import build_zip
    build_zip(build/'source.zip')
    bundle_sha=hashlib.sha256((build/'source.zip').read_bytes()).hexdigest()
    source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    original=(root/'site/app/index.html').read_text()
    for ver in args.versions:
        assert ver in {'1.11.0','1.12.0'}
        html=original.replace('@stlite/browser@1.11.0/',f'@stlite/browser@{ver}/').replace('@stlite/browser@1.12.0/',f'@stlite/browser@{ver}/')
        for mode in ('full','storage'):
            folder=build/ver/mode;folder.mkdir(parents=True,exist_ok=True)
            (folder/'src.zip').write_bytes((build/'source.zip').read_bytes())
            (folder/'index.html').write_text(html if mode=='full' else html.replace('entrypoint: "app.py"','entrypoint: "persistence_check.py"'))
            if mode=='storage':
                with zipfile.ZipFile(folder/'src.zip','a') as z:z.writestr('persistence_check.py',PROBE)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',args.port),functools.partial(Handler,directory=str(build)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    all_results=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
            for ver in args.versions:
                dest=out/ver;dest.mkdir(exist_ok=True)
                data=dict(stlite=ver,browser=browser.version,playwright=version('playwright'),source_commit=source_commit,bundle_sha256=bundle_sha,checks=[],page_errors=[],console_errors=[],failed_requests=[],component_request_initiators=[],frames=[],snapshots=[],phase='startup',failure=None)
                context=browser.new_context(ignore_https_errors=True,viewport=dict(width=1280,height=900))
                page=context.new_page()
                page.on('pageerror',lambda e,d=data:d['page_errors'].append(str(e)))
                page.on('console',lambda m,d=data:d['console_errors'].append(m.text) if m.type=='error' else None)
                page.on('requestfailed',lambda r,d=data:d['failed_requests'].append(dict(url=r.url,failure=r.failure)))
                session=context.new_cdp_session(page);session.send('Network.enable')
                session.on('Network.requestWillBeSent',lambda event,d=data:d['component_request_initiators'].append(dict(url=event['request']['url'],initiator=event.get('initiator'))) if 'stlite.invalid' in event['request']['url'] else None)
                paid=[]
                page.route('https://api.openai.com/**',lambda route:(paid.append(route.request.url),route.abort()))
                page.route('https://api.anthropic.com/**',lambda route:(paid.append(route.request.url),route.abort()))
                def check(label,ok,detail=None):
                    data['checks'].append(dict(check=label,ok=bool(ok),detail=detail));print(ver, 'PASS' if ok else 'FAIL',label,flush=True)
                    if not ok:raise AssertionError(label)
                def frames():
                    data['frames'].append(dict(phase=data['phase'],frames=page.locator('iframe').evaluate_all("els => els.map(e=>({title:e.title,src:e.getAttribute('src'),srcdocLength:(e.getAttribute('srcdoc')||'').length,hasStorageCode:(e.getAttribute('srcdoc')||'').includes('preflight.review_queue'),hasInlineExecute:(e.getAttribute('srcdoc')||'').includes('async function execute'),sandbox:e.getAttribute('sandbox')}))")))
                def capture(name,anchor=None):
                    if anchor:page.locator(anchor).first.scroll_into_view_if_needed();time.sleep(.4)
                    page.screenshot(path=str(dest/name),full_page=False)
                base=f'http://127.0.0.1:{args.port}/{ver}'
                try:
                    data['phase']='minimal save'
                    page.goto(base+'/storage/',wait_until='load',timeout=60000)
                    page.get_by_role('button',name='검증 광고 추가',exact=True).click(timeout=180000)
                    page.get_by_text('이 브라우저에 저장됨',exact=False).first.wait_for(timeout=60000)
                    page.get_by_text(TITLE,exact=True).wait_for(timeout=30000)
                    data['runtime_text']=next(line for line in page.locator('body').inner_text().splitlines() if line.startswith('Python '))
                    snap=snapshot(page);data['snapshots'].append(dict(phase=data['phase'],**snap))
                    check('initial save acknowledged and durable',snap['revision']==1 and snap['has_title'],snap)
                    frames();capture('storage-saved.png')
                    data['phase']='minimal reload'
                    page.reload(wait_until='load',timeout=60000)
                    page.get_by_text(TITLE,exact=True).wait_for(timeout=180000)
                    page.get_by_text('이 브라우저에 저장됨',exact=False).first.wait_for(timeout=30000)
                    check('minimal app restores saved record',True)
                    frames();capture('storage-reloaded.png')
                    data['phase']='full app restore'
                    page.goto(base+'/full/?view='+quote('운영'),wait_until='load',timeout=60000)
                    page.get_by_text('이 브라우저에 저장됨',exact=False).first.wait_for(timeout=180000)
                    stat=page.locator('.pf-stat').filter(has_text='라벨을 기다리는 광고').first
                    stat.wait_for(timeout=30000)
                    check('full app visibly reports one waiting ad',stat.locator('.v').inner_text()=='1건',stat.inner_text())
                    cell=page.locator('[role="gridcell"]').filter(has_text=TITLE).first
                    cell.wait_for(state='attached',timeout=30000)
                    check('accessible grid contains the saved headline',cell.inner_text()==TITLE)
                    capture('full-queue-count.png','.pf-stat')
                    field=page.get_by_label('라벨러 이름',exact=True);field.fill(ANNOTATOR);field.press('Enter')
                    page.get_by_role('button',name='다음 광고 받기',exact=True).click(timeout=30000)
                    ad=page.locator('.pf-ad .h').filter(has_text=TITLE).first
                    ad.wait_for(timeout=30000)
                    check('restored ad opens visibly in the real labeling form',ad.is_visible() and ad.inner_text()==TITLE)
                    for _ in range(30):
                        snap=snapshot(page)
                        if snap and snap['revision']>=2 and snap['has_claiming_annotator']:break
                        time.sleep(.5)
                    data['snapshots'].append(dict(phase='claim saved',**snap))
                    check('claim is committed to IndexedDB',snap['revision']>=2 and snap['has_claiming_annotator'],snap)
                    page.get_by_text('이 브라우저에 저장됨',exact=False).first.wait_for(timeout=30000)
                    frames();capture('full-queue-claimed.png','.pf-ad')
                    data['phase']='full app reload claim'
                    page.reload(wait_until='load',timeout=60000)
                    page.get_by_text('이 브라우저에 저장됨',exact=False).first.wait_for(timeout=180000)
                    field=page.get_by_label('라벨러 이름',exact=True);field.fill(ANNOTATOR);field.press('Enter')
                    ad=page.locator('.pf-ad .h').filter(has_text=TITLE).first;ad.wait_for(timeout=30000)
                    check('same annotator resumes the visible ad after reload',ad.is_visible() and ad.inner_text()==TITLE)
                    check('existing claim resumes without claiming a different ad',page.get_by_role('button',name='다음 광고 받기',exact=True).count()==0)
                    snap=snapshot(page);data['snapshots'].append(dict(phase=data['phase'],**snap))
                    check('stored claim and headline remain after reload',snap['revision']>=2 and snap['has_title'] and snap['has_claiming_annotator'],snap)
                    frames();capture('full-queue-resumed.png','.pf-ad')
                    check('no Streamlit exception',page.get_by_test_id('stException').count()==0)
                    check('no fatal worker error or browser page exception',not data['page_errors'],data['page_errors'])
                    check('no provider calls attempted',not paid,paid)
                except Exception as e:
                    data['failure']=dict(phase=data['phase'],kind=type(e).__name__,message=str(e))
                    print(ver,json.dumps(data['failure'],ensure_ascii=False),flush=True)
                    try:capture('failure.png');(dest/'visible-text.txt').write_text(page.locator('body').inner_text())
                    except Exception:pass
                finally:
                    data['status']='failed' if data['failure'] else 'passed'
                    data['functional_checks']=len(data['checks']);data['failed_checks']=sum(not row['ok'] for row in data['checks'])
                    data['observed_component_fetch_failures']=sum('stlite.invalid' in row['url'] for row in data['failed_requests'])
                    data['console_error_count']=len(data['console_errors'])
                    data['run_at_utc']=datetime.now(timezone.utc).isoformat()
                    (dest/'result.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
                    all_results.append(data);context.close()
                    (out/'checkpoint.json').write_text(json.dumps({r['stlite']:dict(status=r['status'],checks=r['functional_checks'],errors=r['console_error_count']) for r in all_results},indent=2)+'\n')
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
    result=dict(versions={r['stlite']:dict(status=r['status'],functional_checks=r['functional_checks'],failed_checks=r['failed_checks'],observed_component_fetch_failures=r['observed_component_fetch_failures'],console_error_count=r['console_error_count'],runtime_text=r.get('runtime_text'),failure=r['failure']) for r in all_results},source_commit=source_commit,bundle_sha256=bundle_sha,paid_api_calls=0,public_deployment_check=False)
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    rc=int(any(r['failure'] for r in all_results));(out/'exit-code.txt').write_text(str(rc)+'\n')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return rc


if __name__=='__main__':raise SystemExit(main())
