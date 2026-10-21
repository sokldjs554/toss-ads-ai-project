# 남은 검증 공백 보완 계획

> **For agentic workers:** Use superpowers:subagent-driven-development to implement each bounded task. Keep completed evidence and failed attempts distinguishable.

**Goal:** 기존 공동 검수·문구 비교 계약을 더 정확히 검증하고 공개 배포의 화면 검사를 재현 가능하게 실행한다.

**Architecture:** 제품의 판단 기준과 동결 평가 자료는 유지한다. DB 운영 검증은 최초 설치 계약과 맞추고, 모델이 생성하는 비교 표현은 합법 상태만 나타내도록 제한한다. 공개 화면 검사는 비밀값 없이 GitHub Actions의 격리된 브라우저에서 실행한다.

**Tech Stack:** Python 3.11, Pydantic, psycopg, PostgreSQL 16, Streamlit/stlite, Playwright, GitHub Actions.

**Spec:** `docs/TEAM_REVIEW.md`, `docs/deployment/team-review-neon-activation.md`, `docs/review-quality-remaining-20261021.md`, `scripts/verify_remaining_ui.py`의 기존 계약.

## Global Constraints

- 시작 소스 `7f50da2d634f5f7d31bd321354e72db0252f0dc2`. 기존 작업 폴더와 원본 평가 파일을 보존한다.
- 실제 Neon 계정 생성·비밀번호 전달·Render DB 환경 등록은 구체적인 사용자 승인이 필요한 자동 검토 거절 항목이므로 실행하지 않는다.
- 모델 API 호출은 일반 CI·화면 검사에서 실행하지 않는다. 새 실제 측정은 별도 요청·예산 기록을 먼저 검증한다.
- 기존 라벨·평가 분모·알림 임계값·정책 판정·사람 평가 공란을 바꾸지 않는다.
- 공개 QA는 HTTPS 검증을 유지하고 접근키 입력·공동 검수 쓰기를 하지 않는다.
- 최초 설치 함수 본문과 권한을 새로 설계하지 않는다. 운영 verifier가 이미 선언된 설치 계약을 확인하도록 한다.

## Review Focus

- 정상 설치가 새 verifier에서 그대로 유효한지, 실제 PostgreSQL에서 설정 변조를 잡는지.
- 과거 v2 응답·지표가 v3 계약으로 재해석되거나 원본 응답이 사라지지 않는지.
- 문구 순서 교환·minor-only 승자·UNKNOWN·인용 오류의 기존 보류가 유지되는지.
- 공개 검사에서 오래된 배포·제공자 호출·TLS 우회·숨은 로그인 입력을 성공으로 세지 않는지.
- 공개 화면 검사 횟수와 합성/실제/미실행의 분모를 실제 결과로 기록하는지.

### Task 1: 공동 DB 운영 검증

**Files:** `ops/team_review_db_api.py`, `tests/test_team_review_db_api*.py` 또는 좁은 새 테스트, `docs/deployment/team-review-neon-template.json`, 관련 짧은 검토 기록.

**Interfaces:** `verify_api(conn, *, schema="preflight_team", runtime_role)`의 결과 계약을 확장하되 기존 키를 유지한다. 함수 SQL 생성은 그대로다.

- [x] TimeZone 미설정/변경, volatility·parallel 변경, owner CREATE, 추가 함수/오버로드를 잡지 못하는 회귀를 먼저 실행한다.
- [x] 고정 설정·함수 집합·권한을 실제 카탈로그로 비교하고 불일치면 `boundary_valid=False`를 반환한다.
- [x] 대역 검사와 실제 PostgreSQL 변조 검사를 추가하고 기존 정상 설치·runtime 동작을 검증한다.
- [x] 공개 템플릿의 고정 소스 해시를 맞추고 함수·placeholder 정합성 검사를 둔다. 실제 DB에는 적용하지 않는다.

### Task 2: 문구 비교 출력 계약

**Files:** `evals/quality_judge.py`, 해당 `tests/test_quality_*.py`, 필요한 대역 fixture, 짧은 변경 기록.

**Interfaces:** `compare()`의 기존 정규화 응답과 집계 의미를 유지한다. 제공자 요청의 새 wire schema는 합법적인 preference/impact 조합만 표현한다. 새 원본 응답과 계약 버전을 보존한다.

- [x] 현 wire schema가 TIE+minor 등 불가능한 조합을 표현하는 재현과 새 스키마 기대를 먼저 실행한다.
- [x] 단일 enum 또는 명시적 합집합으로 합법 조합만 요청한다. 기존 정규화 도메인·과거 응답 재생과 분리한다.
- [x] 원문 인용·결정 근거·순서 교환·보류 기준을 유지하고 SDK strict-schema 호환성을 검증한다.
- [x] 새 실측 전 모델 성능이나 채택률 향상을 주장하지 않는다. 기존 보고서 해시를 보존한다.

### Task 3: 공개 화면 CI

**Files:** `.github/workflows/public-ui.yml`, 필요한 최소 `scripts/verify_remaining_ui.py`/전용 실행 도우미, 실제 행동을 검증하는 좁은 테스트.

**Interfaces:** 기존 public URL 옵션과 실제 관측 result.json을 사용한다. 공개 main SHA를 시작 시 고정하고 필요시 입력된 정확한 SHA로 검사한다.

- [x] GitHub Actions에서 현재 공개 Pages·Render의 desktop/mobile을 기본 TLS 검증으로 검사한다. 모델/DB 비밀값은 주입하지 않는다.
- [x] 브라우저·Python 종속성을 고정하고 성공·실패의 JSON/스크린샷을 항상 artifact로 보존한다.
- [x] branch push 또는 수동 실행에 한정한다. 정기 실행·외부 서비스 변경·유료 API 요청을 추가하지 않는다.
- [x] 기존 스크립트의 공개 빌드/원본 ZIP/화면 수치를 확인하고 실제 검사 수로 보고한다.

## 통합 완료 기준

- 세 변경의 diff 검토, 필요한 로컬 회귀, 격리 PostgreSQL CI, 공개 UI 실행 결과를 확인한다.
- 완료한 변경과 실제 사람 평가·운영 DB 활성화·통계 검정력 한계를 별도로 보고한다.
- 코드 검증이 끝나면 저장소에 변경과 근거를 반영한다. 승인이 필요한 DB 변경만 구체적인 최종 승인 대상으로 남긴다.

### Task 4: 새 출력 계약의 제한된 실제 API 확인

기존 사용자 API 승인에 따라 159회 전체 평가를 반복하지 않고, 변경한 provider wire 계약만 별도 실측한다. `QP-H01`, `QP-H05`, `QP-H13`, `QP-H25` 네 쌍을 원래·반대 순서로 한 번씩 호출한다. 모델은 `gpt-5.4-mini-2026-03-17`, 최대 8요청, 상한 $0.50, 재시도·대체 모델·추가 수리는 없다.

**Files:** `evals/quality_wire_smoke.py`, `tests/test_quality_wire_smoke.py`, `.github/workflows/quality-wire-smoke.yml`, `evals/quality_wire_smoke_20261021.json`.

- 기존 `remaining-20261021` 예산 장부의 $4 누적 상한과 반환 없는 예약을 유지한다. GitHub에 새 $0.50 예약이 기록된 뒤에만 기존 OpenAI 비밀값을 호출 단계에 제공한다.
- 소스·입력·wire 버전·모델·가격·호출 수·run ID를 사전에 고정한다. `codex/resume-ads-20261021`의 해당 manifest push만 실행하고, 재실행·중복 run ID는 거절한다.
- 원문·정규화 응답·실패·사용량·비용·호출 모델·실행 SHA를 별도 `quality_wire_smoke` 보고서에 보존한다. 기존 `live_features` 보고서와 화면의 최신 전체 평가는 바꾸지 않는다.
- 연결과 구조화 응답 확인을 채택률·정답률·사람 평가와 구분한다. 작성자 라벨·이유 코드·사례 ID를 모델 입력에 보내지 않는다.
- 유료 호출 전에 대역 회귀·validate-only·독립 검토를 통과시킨다. 전체 CI와 공개 화면 검사에는 계속 모델/DB 비밀값을 제공하지 않는다.

## Task 2 추가 범위 — 공개 검수 안내 문구

Root가 Cloud Browser로 공개 앱의 `운영 › 검수 대기열`을 직접 관측해 `ui/review_queue_view.py`의 고정 안내가 “사람 라벨러가 한 명”이라고 주장함을 확인했다. 저장된 독립 사람 제출은 0건이므로 인원·신원을 추정하는 고정 문장을 독립적인 사람 검수 결과가 아직 없다는 현재 상태로 수정한다. 여러 명의 라벨링은 합성 예시로 검증했다는 범위를 명시한다. 실제 세션의 라벨러 수로 계산하는 동적 일치도 경고는 보존한다. 새 문구 전용 테스트는 만들지 않고 기존 AppTest 기대와 결과를 확인한다. 관측 출처는 root의 2026-10-21 Cloud Browser 공개 화면 관측이며, 이번 담당자가 새 공개 브라우저 관측을 수행한 것으로 표현하지 않는다.

## 실행 완료 근거 — 2026-10-21

- Task 1·2: 최종 PR head `6e07cfb`의 CI 39581067361에서 PostgreSQL 선행 54개와 전체 4,352개 통과. 원래 SQL 51문장·함수 본문 7개와 과거 평가 해시는 유지했다.
- Task 3: 공개 baseline `7f50da2`에서 194/0, 병합 후 `952b78a`에서 242/0을 각각 기록했다. 최종 run 39582480518은 네 조합의 빈 저장소 초기·새로고침 load 응답을 확인했다. 원시 진단 및 측정 범위를 보존했다.
- Task 4: run 39579660982의 실제 8회 응답과 schema/근거 8/8, 원문 8개, 사용량 기준 $0.0285105를 별도로 기록했다. 예약 $0.50과 기존 $2를 유지한다. 재실행하지 않았다.
- PR #4 병합, Pages·Render 동일 SHA 배포 및 원본 JSON·캡처 보존 완료. 운영 DB credential/role 설정은 앞선 자동 승인 거절로 구체적 사용자 승인 대기이며 실행하지 않았다. 독립 사람 검수 제출도 0건이다.

상세 근거: [추가 보완 기록](../../release-verified-gaps-2026-10-21.md).
