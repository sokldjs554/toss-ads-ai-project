# 기존 runtime 계정으로 공동 검수 DB 설치·검증

## 실행 결과 — 2026-10-22 KST

**사용자 승인 후 Neon DB 설치와 권한 검증을 완료했다. 운영 공동 검수 공간은 1개다.** 검토한 50문장을 단일 트랜잭션으로 실행했고, 별도의 읽기 전용 트랜잭션에서 같은 초기 상태와 최종 권한 검사를 다시 확인했다. 실제 `preflight_runtime` 역할로 별도 연결도 성공했다. 인증 정보는 연결 도구 내부에서 처리했으며 assistant는 runtime 비밀번호를 조회·변경하거나 반환받지 않았다.

| 확인 | 결과 |
|---|---|
| workspace / schema version / revision | 1행 / 1 / 1 |
| 초기 앱 관리자 / 키 / 감사 영수증 | 각 1개, 관리자 활성 |
| 함수 | 7개, 고정 본문 해시·소유자·설정 일치 |
| runtime 실행 권한 | `lock_workspace`, `save_workspace` 두 함수만 |
| runtime 직접 테이블·열 권한 / 스키마 CREATE | 없음 |
| 설치용 임시 권한 | 회수 확인 |
| Render 환경 변수 등록·공개 앱 저장 시험 | 미완료, 사용자 연결 정보 입력 대기 |
| 독립 사람 검수 결과 | 0건 |

[비밀값 없는 실제 관측 결과](../evidence/team-review-neon-20261022/activation.json)를 보존했다. runtime의 `log_parameter_max_length=-1`이므로 기존 verifier 기준 `logging_review_required=true`는 유지한다. 관측한 statement/duration/sampling 로깅은 비활성이었고 오류 파라미터 길이는 0이었다. 공급자의 proxy/audit 로그까지 확인하거나 무기록을 보장하지 않는다.

## 설치 직전 확인한 상태

사용자가 Neon SQL Editor에서 `preflight_runtime`을 직접 생성했다. 제공한 결과 화면과 인증된 Neon 관리 연결의 읽기 전용 조회에서 LOGIN 허용, 제한 역할 속성, 다른 역할 멤버십 0개를 확인했다. 이는 비밀번호 로그인 시험이나 공동 검수 활성화 완료를 의미하지 않는다.

설치 직전 조회에서 `preflight_team` 스키마와 workspace 테이블은 없고, `preflight_team_api_owner` 역할도 없었다. 당시 운영 공동 검수 공간은 0개였다. DB 관리자 세션, PostgreSQL 16.15, 관리자 CREATEROLE 및 데이터베이스 CREATE 권한, `log_statement=none`, `log_parameter_max_length_on_error=0`도 실행 직전에 다시 확인했다.

| 대상 | 값 |
|---|---|
| Neon 프로젝트 | `preflight-toss-ads-review` / `fancy-cell-02708790` |
| 브랜치 | `production` / `br-ancient-firefly-a1opl77x` |
| 데이터베이스 | `preflight` |
| 설치 관리자 | `preflight_owner` |
| 재사용할 runtime | `preflight_runtime` |
| Render 서비스 | `preflight-toss-ads` / `srv-db9hb6lg1s2s738ndklg` |

## 이번 실행안

[기존 계정 재사용 템플릿](team-review-neon-existing-runtime-template.json)을 검토 후 실행했다. 템플릿 파일의 준비 상태 메타데이터와 해시는 실행 전 고정본으로 보존하며, 실제 실행 결과는 위 증거 파일을 기준으로 한다. 기존 51문장 fresh-install 템플릿은 runtime이 없는 상태를 요구하므로 현재 DB에 사용하지 않는다. 이미 설치된 DB에 새 공간 생성 템플릿을 재실행하지 않는다.

새 템플릿은 다음 두 곳만 SQL을 변경한다.

1. 사전 검사에서 정확한 데이터베이스, 신규 스키마·함수 소유자, 이미 존재하는 제한 LOGIN runtime과 멤버십 0개를 요구한다. 관리자·버전·해시 함수·로그 설정 검사도 유지한다.
2. `CREATE ROLE preflight_runtime ... PASSWORD ...` 문장과 비밀번호 placeholder를 제거한다.

나머지 49개 문장, 함수 7개의 정의와 본문 해시, 권한 부여·회수, 초기 상태 확인, 최종 권한 검사는 원본과 동일하다. 새 50개 문장은 승인 후 하나의 트랜잭션으로 실행했다. runtime 비밀번호는 읽거나 전달하거나 변경하지 않았다.

독립 읽기 검토에서도 이 차이와 소스 해시 일치를 확인했다. 실행 전 새 사전 검사 DO 블록만 실제 대상의 `SET TRANSACTION READ ONLY` 트랜잭션에서 실행해 `existing_runtime_preconditions_passed`와 `transaction_read_only=on`을 확인했다. 이후 승인된 50문장을 실행해 `restricted_api_bootstrap_verified`, revision 1, schema version 1, 함수 7개를 반환받았다. 별도 사후 읽기 검증에서도 준비 상태 전체 일치와 최종 권한 검사를 통과했다.

설치 결과는 `preflight_team.workspace`, 초기 앱 owner 상태와 감사 영수증, NOLOGIN 함수 소유자, 함수 7개다. runtime에는 해당 스키마 USAGE와 `lock_workspace`·`save_workspace` EXECUTE만 허용하며 직접 테이블·열 권한은 주지 않는다. 함수 소유자에게 잠시 준 스키마 CREATE와 관리자의 임시 SET 멤버십은 회수한다. 검증에 실패하면 전체 설치 트랜잭션이 롤백된다.

## 실행과 검증 순서

아래 1~6은 완료했다. 기존 보호된 초기 앱 owner 기록의 키·요청 UUID·대상 바인딩을 그대로 보존했고, DB 사후 검증 후 결과 파일을 `confirmed`로 기록했다. 현재 남은 단계는 7~8이다.

1. 아래 운영 쓰기 범위에 대한 구체적 승인을 확인한다. 현재 조회 결과와 검토용 템플릿 생성 자체는 승인이나 실행 증거가 아니다.
2. 기존 초기 앱 owner 준비 파일이 있으면 그 상태와 UUID·fingerprint를 확인한다. DB에 이미 적용됐는지 모르면 먼저 읽기 조회한다. 키를 임의로 바꾸거나 파일을 덮어쓰지 않는다.
3. 처음 준비하는 경우 앱 접근키와 요청 기록을 보호된 파일에 먼저 내구성 있게 보관한다. 이는 DB 비밀번호와 별개의 앱 키다. 결과 파일 자체는 비밀이며 공개 저장소·증거 자료·채팅에 넣지 않는다.
4. `apply_admin(None, prepared_record, database_user='preflight_owner')`로 초기 상태를 만들고 SQL literal로 안전하게 인용한다. DB에는 원문 앱 키가 아닌 해시와 감사 영수증만 넣는다. 완성된 SQL·초기 상태는 공개 파일이나 로그에 남기지 않는다.
5. 대상과 사전 조건을 다시 확인하고 검토한 50문장을 하나의 관리 트랜잭션으로 실행한다. 성공 여부가 불명확하면 재실행 전에 읽기 조회한다.
6. 스키마 버전·revision·준비한 상태 및 영수증 일치, 제한 권한과 함수 정의를 확인한다. 전체 워크스페이스·키 해시를 공개 결과에 출력하지 않는다.
7. DB 설치가 확인된 후 사용자 본인이 runtime 연결 문자열을 Render의 `PREFLIGHT_TEAM_DATABASE_URL`에 입력한다. 기존 환경 변수는 보존한다. 관리자 연결 문자열을 앱에 배포하지 않는다.
8. 공개 서비스에서 합성 광고 1개와 임시 검수 계정 2개로 저장·새 세션 복원·담당자별 화면·합성 결과의 내보내기 제외를 검증하고 시험 계정을 비활성화한다. 실제 독립 사람 검수는 0건으로 유지한다.

기존 관리 CLI의 `bootstrap` → `install-api --runtime-role preflight_runtime` → `verify-api --runtime-role preflight_runtime`도 기존 runtime을 지원한다. CLI는 bootstrap과 API 설치를 별도 트랜잭션으로 수행한다. 위 템플릿 경로와 CLI 경로를 중복 실행하지 않는다. 상세 키 보관·복구 계약은 [운영 문서](../TEAM_REVIEW.md)를 따른다.

## 승인 기록과 남은 사용자 입력

이전 자동 승인 검토는 원문 비밀번호의 Neon 전달과 지속적인 운영 역할·권한 변경 요청을 거절했다. 사용자가 계정을 직접 만든 뒤 비밀번호 처리가 없는 축소된 설치안을 별도로 검토했다. 2026-10-22 사용자가 이 설치와 권한 검증을 명시적으로 승인했고, 그 범위로 실행했다.

승인받아 실행한 범위는 **기존 `preflight_runtime`의 비밀번호를 읽거나 바꾸지 않고, 지정한 Neon DB에 공동 검수 스키마·초기 상태·NOLOGIN 함수 소유자·함수와 제한 권한을 설치하고 검증하는 작업**이다. 이후 runtime 연결 문자열은 사용자가 Render에 직접 입력한다. 비밀번호나 연결 문자열을 채팅으로 전달할 필요가 없다.

Render 환경 변수 수정과 공개 서버에서의 합성 데이터 쓰기 시험은 위 DB 설치 승인에 자동 포함하지 않는다. 각각 기존 사용자 지시의 적용 범위나 후속 명시적 승인을 확인한다.

Neon DB 설치·권한 검증은 완료했다. Render 환경 변수 등록과 공개 서버 공동 검수 시험은 아직 완료되지 않았다.
