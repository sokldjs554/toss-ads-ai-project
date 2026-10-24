from streamlit.testing.v1 import AppTest
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd()))
for facts,headline in [('', '가을 상품 안내'),('판매가 29000원','판매가 37000원')]:
 at=AppTest.from_file(str(Path.cwd() / 'app.py'),default_timeout=60).run()
 assert not at.exception
 at.text_input(key='form_name').set_value('합성 상품')
 at.selectbox(key='form_category').set_value('ecommerce')
 for k,v in [('form_desc',''),('form_landing',''),('form_facts',facts),('form_input','상품 정보를 알리는 짧은 광고 문구를 만들어 주세요.')]: at.text_area(key=k).set_value(v)
 at.run()
 next(b for b in at.button if '초안' in b.label).click().run()
 assert not at.exception, [e.value for e in at.exception]
 entry=at.session_state['runs'][-1]
 print('mode',entry['mode'],'categories',entry['run'].categories,'reports',len(entry['run'].reports),'rejected',at.session_state['rejected_history'])
 vid=entry['run'].reports[0].variant_id
 at.text_input(key='edit_h_'+vid).set_value(headline)
 at.text_area(key='edit_b_'+vid).set_value('')
 at.text_input(key='edit_c_'+vid).set_value('')
 at.text_area(key='edit_d_'+vid).set_value('')
 at.button(key='recheck_'+vid).click().run()
 assert not at.exception, [e.value for e in at.exception]
 actual = next(iter(at.session_state['rechecks'].values())).latest()
 assert actual.report.ad_copy.headline == headline
 assert actual.report.ad_copy.body == actual.report.ad_copy.cta == ''
 assert actual.report.ad_copy.disclosures == []
 assert [x.text for x in entry['brief'].product.facts] == ([facts] if facts else [])
 assert actual.mode == 'demo' and actual.cost_usd == 0
 print('observed', {'headline':headline,'facts':facts,'decision':actual.report.final_decision.value,'mode':actual.mode,'cost':actual.cost_usd,'exceptions':len(at.exception)})
