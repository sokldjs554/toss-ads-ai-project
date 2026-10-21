# 검증 공백 추가 보완 — 2026-10-21

기준은 공개 main `7f50da2d634f5f7d31bd321354e72db0252f0dc2`다. 이전 대화의 일부 기록보다 실제 저장소와 배포가 앞서 있어, 완료된 159회 평가를 반복하지 않고 현재 코드의 검증 공백을 보완했다. 과거 평가 원문·작성자 라벨·사람 검수 공란은 보존한다.

## 바꾼 동작

**공동 DB 운영 검증.** 기존 설치 계약에 있던 UTC 고정, `VOLATILE`/`PARALLEL UNSAFE`, 함수 소유자의 schema CREATE 회수, 정확히 일곱 함수만 존재하는 조건을 실제 PostgreSQL 카탈로그와 대조한다. 추가 함수와 동일 이름 오버로드도 거절한다. 비밀값 없는 진단 코드를 반환한다. 템플릿의 51개 SQL 문장과 일곱 함수 본문은 바꾸지 않았으며 실제 운영 DB에도 적용하지 않았다.

**문구 비교 출력.** 제공자가 각 기준을 `A_material`, `A_minor`, `B_material`, `B_minor`, `TIE`, `UNKNOWN` 중 하나로 표현하도록 했다. `TIE+minor` 같은 모순을 출력 형식에서 표현할 수 없다. 각 순서의 wire 원문, 구조화 원본, 기존 의미 계약으로 옮긴 응답을 함께 보존한다. 판단 기준 버전은 그대로이며 wire 버전만 구분한다. 사소한 표현 차이만으로 승자를 선택하면 기존처럼 보류하며, 과거 응답을 새 형식으로 재해석하지 않는다.

**검수 안내.** 고정 문장의 사람 라벨러 1명 주장을 제거하고 독립 사람 검수 결과가 아직 없다는 사실과 합성 예시 검증 범위를 표시했다. 실제 세션의 라벨러 수로 계산하는 일치도 안내는 유지한다.

## 코드와 PostgreSQL 검증

| 검증 소스와 실행 | 실제 관측 |
| --- | --- |
| `2fcd81c`, [CI 39578788780](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39578788780) | PostgreSQL 선행 검사 54개 / 6.31초, 전체 4,323개 / 403.88초 통과 |
| `f55cc89`, [CI 39579660871](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39579660871) | PostgreSQL 선행 검사 54개 / 11.36초, 전체 4,352개 / 316.29초 통과 |

두 실행 모두 회귀 평가·실제 Chromium 저장 테스트·앱 시작 검사를 통과했다. PostgreSQL은 GitHub Actions의 격리된 테스트 서비스이며 운영 Neon DB가 아니다. 실제 예약 장부와 결과를 내려받은 뒤에도 wire smoke 관련 56개 로컬 회귀가 통과했다.

독립 검토에서 smoke 테스트가 예약 뒤의 장부를 잘못 재사용하는 문제와 SDK parse 실패 때 이미 받은 원문을 잃는 문제를 찾아 실행 전에 수정했다. 테스트 장부는 동결된 예약 전 baseline으로 독립 구성하고, smoke 전용 HTTP 응답 관측에서 허용된 completion 필드만 SDK parsing 전에 보존한다. 잘림·스키마 오류·거절은 계속 실패로 기록하며 재시도·추가 모델 호출을 하지 않는다. 원래 발견과 수정 확인을 구분해 검토했다.

## 별도 실제 API 확인

[Actions 39579660982](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39579660982), 소스 `f55cc89`, 모델 `gpt-5.4-mini-2026-03-17`로 이미 본 네 쌍을 두 순서씩 확인했다. **8회 응답·8개 schema 통과·8개 인용/근거 계약 통과**, 네 쌍의 채택 결과는 `a / tie / tie / tie`다. 원본 completion도 8개 남아 있다.

사용량 기준 계산액은 **$0.0285105**다. 호출 전 예약한 $0.50은 반환하지 않으며, 기존 $2 예약을 보존한 누적 예약은 $2.50/$4다. 이는 공급자 청구서가 아니다. [별도 실측 기록과 원문](../evals/reports/quality_wire_smoke/README.md)에 소스·예약·결과 커밋과 해석 한계가 있다. 전체 159회 평가, 작성자 라벨 대비 정확도, 독립 사람 검수, 실제 광고 성과를 새로 측정하지 않았다.

## 공개 화면 검증

[공개 baseline 실행 39578788768](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39578788768)은 실제 main `7f50da2`를 고정하고 Pages·Render의 desktop/mobile에서 **194개 assertion 통과, 실패 0개**를 기록했다. Chromium 141.0.7390.37 / Playwright 1.56.0, HTTPS 검증 유지, 모델·DB 비밀값 미주입이다. [원본 JSON·캡처·로그 해석](evidence/verified-gaps-20261021/public-baseline-7f50da2/README.md)을 보존했다.

Pages의 `stlite.invalid` 요청 실패는 이전 저장 성공과 함께 관측된 `checkSourceUrlResponse` 사전 검사와 일치했다. 실제 컴포넌트는 별도 kernel/iframe srcdoc 경로로 로드된다. 이 로그를 숨기거나 성공으로 바꾸지 않았고 제품 코드도 바꾸지 않았다. 최종 공개 검사에는 빈 브라우저 context에서 저장 load 응답 수락 뒤에만 표시되는 입력과 iframe 경로를 확인하고, 새로고침 후 load 응답도 재확인하도록 추가했다. 큐 추가·배정·라벨 저장은 하지 않는다. 빈 IndexedDB를 열 때 DB/object store가 초기화될 수 있으며, 비어 있지 않은 큐의 저장 내구성 검사로 확대하지 않는다.

배포 후 실행의 정확한 SHA·검사 수·실패 여부는 후속 결과를 확인한 뒤 이 문서에 추가한다.

## 그대로 남아 있는 외부 조건

독립 사람 검수 제출은 **0건**이다. 검수 패킷은 준비됐지만 AI가 사람 대신 제출하지 않는다. 성과 탐지의 개발용 지속 하락 12개 중 7개 미탐도 기존 기록대로 유지한다.

운영 공동 공간도 **0개**이며 `PREFLIGHT_TEAM_DATABASE_URL`을 등록하지 않았다. 새 프로젝트 전용 비밀번호를 Neon에 전달해 지속적인 제한 runtime 계정을 만드는 요청은 앞서 자동 승인 검토에서 거절됐다. [51문장 템플릿과 구체적인 활성화 계획](deployment/team-review-neon-activation.md)은 준비되어 있고, 구체적 승인을 받기 전에는 다른 경로로 재시도하지 않는다.
