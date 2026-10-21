# Neon 공동 검수 DB 설치 증거 — 2026-10-22 KST

사용자가 직접 생성한 제한 `preflight_runtime`을 재사용하는 50문장 설치안을 검토한 뒤, 사용자 명시적 승인으로 실행했다. [activation.json](activation.json)은 실제 도구 응답 중 비밀이 없는 카탈로그·권한·상태 요약만 보존한 증거다. 원문 키, 키 해시, DB 비밀번호, DSN, 전체 workspace는 포함하지 않는다.

## 확인한 결과

- 단일 설치 트랜잭션 50문장 성공: `restricted_api_bootstrap_verified`, revision 1, schema version 1, API 함수 7개.
- 별도 읽기 전용 트랜잭션에서 고정된 최종 권한 guard와 설치에 사용한 초기 상태 전체의 정확한 일치를 다시 확인.
- workspace 1행, 초기 앱 owner·키·감사 영수증 각 1개, owner 활성.
- runtime은 스키마 USAGE 및 `lock_workspace`, `save_workspace` EXECUTE만 보유. 내부 helper 5개 실행, 직접 테이블·열 접근, 스키마 CREATE는 허용되지 않음.
- 일곱 함수의 정확한 집합·본문 SHA-256·소유자·언어·SECURITY DEFINER·UTC·search_path·VOLATILE·PARALLEL UNSAFE·PUBLIC ACL 검사 통과.
- 임시 설치 권한 회수 확인. 실제 `preflight_runtime` 역할의 별도 연결에서도 같은 권한 요약 확인. 비밀번호는 연결 도구 내부에서 처리됐고 assistant에 반환되지 않음.

## 범위와 남은 조건

이 증거는 Neon DB 설치와 권한 경계에 대한 것이다. Render 환경 변수 등록이나 공개 앱에서의 저장·복원 시험을 완료했다는 증거가 아니다. 이번 설치 중 모델 API 호출·합성 검토 제출은 없었고 독립 사람 검수 결과도 0건이다.

`log_parameter_max_length=-1`이므로 기존 verifier 기준 로깅 검토 필요 표시는 유지한다. 관측한 statement/duration/sampling 로깅은 비활성이고 오류 파라미터 길이는 0이지만, 공급자 proxy/audit 로그는 검사하지 않았다.

[실행한 고정 템플릿과 설치 절차](../../deployment/team-review-neon-existing-runtime-activation.md). 템플릿의 준비 메타데이터는 실행 전 기록으로 보존하고 실제 결과는 이 증거를 기준으로 한다.
