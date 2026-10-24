# Preflight

**상품 조건과 다른 광고를 찾고, 수정한 문구까지 다시 검사하는 광고 사전 검사 도구입니다.**

토스 AI Engineer (Ads) 지원을 위해 진행한 개인 프로젝트입니다. 공개 자료로 재구성한 검사 기준을 사용하며 토스·토스애즈의 내부 심사나 실제 게재 승인을 대신하지 않습니다.

[![ci](https://github.com/sokldjs554/toss-ads-ai-project/actions/workflows/ci.yml/badge.svg)](https://github.com/sokldjs554/toss-ads-ai-project/actions/workflows/ci.yml)

[무료 데모](https://sokldjs554.github.io/toss-ads-ai-project/app/) · [서버 LIVE 검토](https://preflight-toss-ads.onrender.com/?view=LIVE%20검토) · [현재 확인된 범위](docs/CURRENT_STATUS.md) · [최종 사용·원자료](https://github.com/sokldjs554/toss-ads-ai-project/blob/c51d574eaf7a0fc90c09f01ed8e996a57fce98a8/docs/final-handoff-20261024.md)

![실제 앱의 예시 실행 화면](docs/screenshots/demo.gif)

위 영상은 무료 DEMO의 과거 실제 조작 화면입니다. 모델 단계는 대역이며 새 모델 호출을 보여 주는 영상이 아닙니다.

## 30초 요약

상품 사실과 광고를 입력하면 문제 구절·이유·근거를 보여 줍니다. 자동 수정안과 사람이 직접 고친 문구도 같은 정책으로 다시 검사합니다. 판단을 확정할 수 없으면 보류하고 사람이 근거를 읽고 판단을 저장합니다. 광고 게시 기능은 없습니다.

- **RAG 검토:** 관련 정책·상품 사실을 문자 bigram으로 검색하고, 모델의 출처·원문 인용과 광고 구절을 대조합니다. 인용이 존재한다는 사실만으로 결론이 옳다고 보장하지 않습니다.
- **도구 호출:** 실제 모델이 수정안을 만들어 `check_policy`를 호출하고 코드의 검사 결과를 받습니다. 최대 2회 수정·4회 API 호출 뒤 사람 검토로 이어집니다. 마지막 검토는 RAG를 재사용합니다.
- **판정 경계:** 숫자·일부 조건은 코드로 대조하며, 모델 의견이 코드의 반려를 덮어쓰지 못합니다. 제공자·인용·구조 검증 실패를 DEMO 결과로 바꾸지 않습니다.

## 실제 확인한 범위 — 2026-10-24

| 구분 | 확인 결과 | 해석 |
|---|---|---|
| 본인 사용 | 1명·LIVE 요청 6건·판단 저장 6건 | 승인 3·수정 필요 3. 합성 광고의 단기 파일럿 |
| 실제 모델 호출 | 본인 계정 10회 성공, 추정 $0.005459 | 전체 프로젝트 청구액과 구분 |
| 저장·재접속 | 판단 저장 후 로그아웃·재접속 확인 | 계정별 기록 접근 제한 |
| 재배포 보존 | 기존 18개 기록 보존, 이후 1개 추가 | 최종 인계 시 총 19개. 사람 사용 6건과 다른 분모 |
| 사용 근거 커밋 | `3f78335` | 이후 안내 변경 커밋과 구분 |
| 해당 커밋 CI | 4,505개 통과, 실패·skip·error 0 | 코드 회귀 검사이며 AI 정확도 아님 |
| 해당 공개 브라우저 | LIVE 33·무료 51항목 통과 | 화면·저장 확인 항목 수, 새 모델 호출 없음 |

[해당 CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40065093568) · [해당 브라우저 실행](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40065136970) · [현재 main CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/workflows/ci.yml) · [현재 Pages 배포](https://github.com/sokldjs554/toss-ads-ai-project/actions/workflows/pages.yml)

화면 상단 빌드는 현재 배포 코드입니다. 위 표는 실제 사용 당시의 근거이며, 이후 문서 배포마다 모델 실험을 새로 했다는 뜻은 아닙니다. 모델은 `gpt-4o-mini-2024-07-18`, 프롬프트는 `live-rag-tools-4`, 정책은 `2026.10.7`입니다.

## 직접 확인하는 순서

1. [무료 데모](https://sokldjs554.github.io/toss-ads-ai-project/app/)에서 예시를 실행합니다. 실제 모델 호출·로그인 없이 검사 흐름을 볼 수 있습니다.
2. 문제 구절·상품 근거를 비교하고 문구를 직접 편집해 재검사합니다.
3. 실제 모델 사용은 [서버 LIVE 검토](https://preflight-toss-ads.onrender.com/?view=LIVE%20검토)에서 운영자가 발급한 접근키로 진행합니다. 기존 기록 조회는 새 비용을 만들지 않습니다. 최종 인계 시 예약 한도는 소진됐으며 새 실행에는 운영자의 한도 확인이 필요합니다.
4. [근거 원본](https://github.com/sokldjs554/toss-ads-ai-project/blob/c51d574eaf7a0fc90c09f01ed8e996a57fce98a8/docs/final-handoff-20261024.md)에서 코드 검사·모델 의견·사람 판단을 나누어 확인합니다.

[과거 GPT-4o 실행 재생](https://sokldjs554.github.io/toss-ads-ai-project/replay/)은 이전 정책·프롬프트의 저장된 기록입니다. 현재 LIVE 모델의 새 실행 결과가 아닙니다.

## 기술적 판단과 남은 문제

| 문제 | 선택·검증 | 남은 범위 |
|---|---|---|
| 정상 쿠폰 문구 일부가 금지 표현으로 연결됨 | 정규화에서 단어 경계를 보존하고 기존 우회 검사와 회귀 확인 | 모든 형태소·의미를 해석하는 규칙은 아님. 금지어 삭제는 별도 요청 변경 |
| 모델의 근거 인용이 원문과 다름 | 검색 출처·연속 원문 구절·광고 위치 검증, 실패 시 보류 | 인용 존재와 의미의 타당성은 별개 |
| 코드 통과 뒤 모델이 조건 누락을 지적 | 최종 수정 필요 상태 유지, 본인이 판단·사유 저장 | 어느 조건이 부족한지 확실하지 않음. [실제 사례](https://github.com/sokldjs554/toss-ads-ai-project/blob/c51d574eaf7a0fc90c09f01ed8e996a57fce98a8/docs/operating-pilot-20261024/session-02-result.md) |
| 모든 광고 조건을 코드로 대조하기 어려움 | 여섯 종류 조건에 한정해 값·뜻 비교 | 신규 고객 한정·첫 달만 등은 모델·사람에게 의존 |
| 새로운 정책 사례에서 위반을 놓침 | 새 합성 평가의 위험 사례 16건 중 4건 승인 그대로 공개 | 규칙·DEMO, AI 초안 정답. 독립 검수·최신 LIVE 정확도 아님 |
| 성과의 지속 하락을 놓침 | 개발에서 좋아진 후보가 새 자료에서 악화돼 철회 | 개발 7/12·새 자료 13/24를 놓친 상태 유지 |

[상세 한계·원자료](docs/CURRENT_STATUS.md) · [숫자·조건 구현](guardrails/grounding.py) · [정규화 수정](docs/normalization-boundaries-20261023.md)

사람 선호 응답은 **28쌍×2명=56개**, 선택 일치 **21/28**에 공동 보류 1쌍을 포함합니다. AI 정확도로 사용하지 않습니다. 독립 정책 정답, 실제 광고주 업무 시간 절감, 상용·장기 운영 효과는 입증하지 못했습니다.

## 경쟁 서비스에서 확인한 것

Ahrefs 무료 광고 생성기를 본인이 3회 실행했습니다. 중복을 제외한 15문구 중 가격 사례 10문구는 모두 29,000원을 유지했습니다. AI 분석에서는 3문구에 제공되지 않은 구체 주장이 추가됐습니다. 독립 오류율이나 같은 조건의 Preflight LIVE 우열 비교가 아닙니다. [원본·검수표](https://github.com/sokldjs554/toss-ads-ai-project/blob/f55418d5f1d2a615a402458dc4bd4cf02f8e2df7/docs/competitor-comparison-20261024/RESULT.md)

## 공고와 연결되는 구현

| 업무·역량 | 구현과 근거 | 범위 |
|---|---|---|
| 광고 심사·반려 사유·수정 | `guardrails/`, `agents/live_pilot.py`, `agents/rewrite_check.py` | 주력 범위. 실제 RAG·도구 호출과 본인 검토 확인 |
| 평가·실패 대응 | `evals/`, `tests/`, `ops/live_pilot_store.py` | 회귀·실패 보류·비용 예약·감사 기록. 정확도와 테스트 수 구분 |
| 대화형 캠페인 설정 | `agents/conversation.py`, `agents/planner.py` | 기존 합성 입력 평가·실제 호출 기록. 현업 효과는 미측정 |
| 성과 인사이트 | `core/insight_engine.py`, `agents/insights.py` | 합성 시나리오, 지속 하락을 놓친 사례 유지 |
| 광고 문구·이미지 | `agents/copywriter.py`, `agents/vision.py`, `core/banner.py` | 문구 생성, 이미지 속 글자 검사, SVG 배치. 이미지 생성 모델·영상은 없음 |
| 두 LLM 활용 방식의 배포·사용 | RAG 및 실제 function call 수정 루프 | 배포 서버에서 본인 1명이 합성 광고 6건을 검토하고 판단·사유 저장 |

## 기여

본인은 문제 범위·보완 우선순위 결정, 결과 검수, 사람 선호 응답 수집, LIVE 직접 입력·판단 저장·재접속, 경쟁 서비스 실행을 맡았습니다. AI 개발 도구는 설계 후보·코드 구현·테스트·배포·분석·문서 작성을 지원했습니다. 확인하지 않은 협업·고객 성과나 직접 구현 범위를 주장하지 않습니다.

실패 유형 목록은 [131개 실패 모드(핵심 심사 24개 + 기능별 107개)](guardrails/failure_modes.py)로 관리합니다. 유형 수는 해결한 문제 수나 모델 정확도를 뜻하지 않습니다.

## 실행·코드 탐색

Python 3.11 이상에서 실행합니다. 실제 모델·DB 자격증명은 서버 환경에만 설정하며 공개 문서나 입력 예시에 넣지 않습니다.

```bash
pip install -r requirements.txt
PREFLIGHT_FORCE_DEMO=1 streamlit run app.py
python -m pytest -q
```

전체 테스트의 격리 PostgreSQL·Chromium 준비는 [.github/workflows/ci.yml](.github/workflows/ci.yml)을 따릅니다. 운영 DB를 테스트 대상으로 쓰지 않습니다.

- `agents/live_pilot.py`: 검색·인용 검증·도구 루프·최종 결합
- `guardrails/grounding.py`: 숫자·조건의 값과 뜻 대조
- `agents/adjudicator.py`, `agents/rewrite_check.py`: 원문·수정안의 판정 경계
- `llm/live_pilot_gateway.py`, `ops/live_pilot_store.py`: 호출·비용·저장·계정 경계
- `ui/`: Streamlit 화면, `site/`: 공개 랜딩·브라우저 실행판

[아키텍처](docs/ARCHITECTURE.md) · [평가 설명](docs/EVALS.md) · [운영 문서](docs/OPERATIONS.md) · [주장 검증표](docs/live-pilot-claims.md)

이전 단계별 실험과 상세 실패 기록은 [안내 갱신 전 README](https://github.com/sokldjs554/toss-ads-ai-project/blob/3f78335/README.md)에 보존했습니다. 날짜가 있는 문서는 당시 조건으로 읽고, 최신 합계는 [현재 상태](docs/CURRENT_STATUS.md)를 기준으로 확인해 주세요.
