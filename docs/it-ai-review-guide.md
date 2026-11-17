# Preflight: LLM 시스템 개발·운영 근거

상품 사실과 광고 문구의 차이를 찾아 근거를 제시하고, 모델이나 사람이 수정한 문구를 다시 검사하는 개인 프로젝트입니다. 광고 게시나 실제 매체 승인 기능은 없습니다.

## 3분 확인 순서

1. [무료 데모](https://sokldjs554.github.io/toss-ads-ai-project/app/)에서 예시를 실행합니다. 문제 구절·상품 조건·정책 근거를 읽습니다.
2. 문구를 직접 고쳐 다시 검사합니다. 이 공개 경로의 모델 단계는 대역이며 실제 유료 생성이 아닙니다.
3. [본인 사용·실제 API 기록](CURRENT_STATUS.md)을 열어 무료 화면과 LIVE 근거를 구분합니다.
4. [실제 사용 기록](https://github.com/sokldjs554/toss-ads-ai-project/blob/c51d574eaf7a0fc90c09f01ed8e996a57fce98a8/docs/final-handoff-20261024.md)에서 모델 의견, 코드 검사, 사람이 저장한 판단을 대조합니다.

[LIVE 서버](https://preflight-toss-ads.onrender.com/?view=LIVE%20검토)는 접근키와 운영자 한도 확인이 필요합니다. 2026-10-24 인계 당시 예약 한도는 소진된 상태였습니다. 새 호출 가능 여부나 현재 서버 상태를 과거 성공 기록으로 대신하지 않습니다.

## 기능 → 코드 → 증거

| 직무 연결 | 구현 | 증거·경계 |
|---|---|---|
| 생성형 AI 시스템 | [live_pilot.py](../agents/live_pilot.py)의 `retrieve`, `validate_review`와 실제 도구 수정 루프 | 정책·상품 검색, 출처·인용 검증, `check_policy` 호출. 문자 bigram 검색이며 임베딩 RAG로 소개하지 않음 |
| LLM 데이터 처리 | [grounding.py](../guardrails/grounding.py), [adjudicator.py](../agents/adjudicator.py) | 상품의 숫자·조건 대조와 모델 의견 결합. 일반 재무 분석 도구나 실고객 데이터 실증이 아님 |
| 호출 실패·비용 관리 | [live_pilot_gateway.py](../llm/live_pilot_gateway.py) | 모델·사용량 확인, timeout, 자동 재시도 0, 허용 오류 필드만 기록. 제공자 실패를 무료 DEMO 결과로 바꾸지 않음 |
| 계정·저장·동시성 | [live_pilot_store.py](../ops/live_pilot_store.py) | 토큰 해시, 계정별 조회, 매개변수 SQL, 트랜잭션·예산 예약. 격리 DB 검증과 실제 사용을 구분 |
| 테스트·배포 | [ci.yml](../.github/workflows/ci.yml), [pages.yml](../.github/workflows/pages.yml) | 코드 회귀·격리 PostgreSQL·Chromium·정적 배포. 테스트 개수는 AI 정확도 아님 |

## 실제 사용 기록의 정확한 범위

2026-10-24 기준 본인 **1명·합성 광고 LIVE 요청 6건·판단 저장 6건**, 실제 모델 호출 **10회 성공**입니다. 사용 모델은 `gpt-4o-mini-2024-07-18`, 프롬프트는 `live-rag-tools-4`입니다. 6요청과 10 API 호출은 서로 다른 분모입니다. 저장·로그아웃 후 재접속·재배포 보존의 기록이 있습니다.

RAG와 function calling은 한 서비스의 두 활용 방식입니다. 서로 다른 LLM 두 개를 장기 운영했다고 표현하지 않습니다. 과거 CI 4,505개 통과는 해당 커밋의 회귀 검사 수이며, 모델의 정답률이나 실제 고객 수가 아닙니다. [날짜·비용·실패 사례·원자료](CURRENT_STATUS.md).

## 재현

Python 3.11 이상에서 저장소 루트 기준:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
PREFLIGHT_FORCE_DEMO=1 streamlit run app.py
```

API 키와 DB 없이 핵심 로직을 확인하는 부분 검사:

```bash
python -m pytest -o addopts='' -q tests/test_live_pilot_engine.py tests/test_live_pilot_gateway.py tests/test_grounding.py tests/test_injection.py tests/test_output_guard.py
```

전체 검사는 CI 정의대로 **별도 테스트 PostgreSQL과 Playwright Chromium**을 준비하고 실행합니다.

```bash
python -m playwright install chromium
python -m pytest -o addopts='' -q -ra
```

부분 검사의 모델 경계는 모의 제공자입니다. 실제 DB·브라우저 검사를 건너뛴 실행은 그 범위를 검증하지 않습니다. [2026-11-17 재검증 기록](verification/deloitte-20261117/README.md)에 환경 차이와 실패를 함께 남겼습니다.

## 지원서·면접에서 지킬 경계

- 본인 역할로 기록된 것은 범위·우선순위 결정, 결과 검수, 선호 응답 수집, LIVE 입력·판단 저장·재접속, 경쟁 서비스 사용입니다. AI 개발 도구가 코드·테스트·배포·분석·문서 작성을 지원했습니다.
- 전체 구현을 독립 작성했다는 근거는 없습니다. 커밋 작성자 이름만으로 직접 구현 범위를 판단하지 않습니다.
- Render와 GitHub Pages 사용을 AWS·GCP·Azure 서비스 운영 경험으로 바꾸지 않습니다.
- 16개 위험 합성 사례 중 4건이 승인된 기록과 의미가 모호한 모델 사유가 남아 있습니다. 독립 정책 정확도·고객 업무 시간 절감·장기 SLA는 확인되지 않았습니다.

이번 변경은 실행 안내·증거 연결과 검증된 CI OS/Streamlit 버전 고정이며 모델·정책·판정 로직·기존 평가 결과를 변경하지 않았습니다.
