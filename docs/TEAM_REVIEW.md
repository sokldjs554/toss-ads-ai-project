# 서버 공동 검수와 접근키 운영

## 현재 운영 상태 — 2026-10-21

**실제로 생성된 운영 공동 검수 공간은 0건이며, 사용자 승인 전 운영 초기화는 미완료 상태다.** 제한 DB API의 PostgreSQL 회귀와 전체 CI가 통과한 사실은 아래에 따로 기록한다. 코드 검증과 실제 운영 공간 생성을 구분한다.

운영 초기화의 첫 MCP 트랜잭션은 자동 승인 검토를 통과했지만, Neon이 미리 해시한 비밀번호를 받지 않아 400 오류(`Neon only supports being given plaintext passwords`)로 원자적으로 실패했다. 이후 읽기 전용 조회에서 대상 스키마가 없고 두 예정 역할도 0개임을 확인했다. 이 시도를 성공한 bootstrap이나 생성된 공동 검수 공간으로 집계하지 않는다.

Neon 요구에 맞춰 원문 런타임 DB 비밀번호를 전달하는 별도 템플릿을 준비했지만, **자동 승인 검토가 새 비밀값의 Neon 전달과 운영 역할 생성을 위한 구체적인 승인이 추가로 필요하다는 이유로 실행을 거절했다.** 이전 해시 전달 방식의 승인이 원문 비밀번호 전달까지 포함하지 않으므로 이 변형의 DB 실행은 0건이다. 승인 전에는 다른 API나 간접 SQL로 재시도하지 않는다. 템플릿에는 실제 비밀번호나 앱 접근키를 넣지 않았으며, 준비된 템플릿을 운영 초기화 완료 증거로 취급하지 않는다.

승인할 변경 대상과 비밀값 없는 SQL 템플릿은 [공동 검수 DB 활성화 검토안](deployment/team-review-neon-activation.md)에 정리했다.

## 현재 지원 범위

서버의 공동 검수는 PostgreSQL의 `preflight_team.workspace` 한 행에 계정, 키의 해시, 대기열, 검토·조정 기록, 요청 영수증을 저장한다. `ops/team_review_store.py`는 기본적으로 두 개의 DB API 함수를 호출한다. DB가 행을 잠그고 원본 상태로 앱 키·역할·만료를 인증하며, Python이 대기열 전이를 계산한 뒤 DB가 허용된 인증 정보 변경과 영수증 계약을 재검사하고 저장한다. 브라우저가 전체 대기열을 업로드해 서버 상태를 덮어쓰는 경로는 없다. 브라우저 전용 로컬 대기열과 서버의 공동 검수는 별개다.

이 기능의 계정은 **접근키가 가리키는 계정**이다. SSO, 실명 확인, 서로 다른 실제 사람의 참여나 독립 검토를 증명하는 시스템은 아니다. 내보내기의 `identity_verified=false`, `independence_verified=false`를 유지한다. 코드와 통제된 테스트가 있다는 사실을 실제 광고주 운영, 실제 검수자의 참여, 독립 사람 라벨의 확보로 확대해서 설명하지 않는다.

관리 CLI는 `scripts/team_review_admin.py`다. 설치된 Python 환경에서 저장소 루트를 작업 디렉터리로 사용한다.

```bash
.venv311/bin/python -m scripts.team_review_admin --help
```

이 문서는 명령과 복구 계약을 설명한다. 문서의 예시를 작성하거나 테스트한 사실만으로 실제 운영 DB에서 CLI를 실행했다고 주장하지 않는다. 다른 승인된 DB 관리 도구로 초기화했다면 그 도구·SQL·확인 결과를 별도 운영 기록에 남긴다.

## 역할과 만료

| 역할 | 허용하는 일 | 현재 키 유효기간 |
|---|---|---:|
| owner | 검수자·조정자 계정 생성, 기존 계정 키 교체, 계정 활성 상태 관리, 광고 추가, 내보내기 | 발급/교체 시점부터 90일 |
| reviewer | 본인에게 배정된 광고 열람, 검토 제출, 배정 반납 | 발급/교체 시점부터 30일 |
| adjudicator | 본인이 라벨링하지 않은 이견의 조정, 내보내기 | 발급/교체 시점부터 30일 |

owner 계정 ID는 `acct_owner`로 유지된다. 키를 바꿔도 담당자의 계정 ID와 기존 검토 기록은 바뀌지 않는다. 교체는 대상 계정의 이전 키를 모두 회수하며, 회수·만료·비활성 상태는 다른 세션과 영수증 재시도에도 적용된다. 정상 UI는 owner의 자기 키 교체를 제공하지 않는다. owner는 만료 전에 아래 CLI로 자기 키를 회전한다.

## 비밀값 입력과 결과 파일

CLI에는 DSN이나 키 값을 받는 argv 옵션이 없다. 다음 입력 중 하나를 사용한다.

| 입력 | 기본 환경 변수 | 파일 입력 옵션 |
|---|---|---|
| 정상 운영 DB 연결 | `PREFLIGHT_TEAM_DATABASE_URL` | `--database-file /secure/path/runtime-database.secret` |
| 명시적인 DB 관리자 연결 | `PREFLIGHT_TEAM_ADMIN_DATABASE_URL` | `--database-file /secure/path/admin-database.secret` |
| 현재 owner 키 | `PREFLIGHT_TEAM_OWNER_KEY` | `--owner-key-file /secure/path/owner.json` |

다른 환경 변수 **이름**은 `--database-env NAME`, `--owner-key-env NAME`으로 지정할 수 있다. 변수 값은 운영의 비밀 저장 방식으로 주입한다. 셸 명령줄, 명령 이력, CI 출력, 이슈, 채팅, 감사 사유에 DSN이나 키 값을 붙이지 않는다. DB 파일은 DSN 한 개를, 키 파일은 키 한 개 또는 이 CLI가 만든 JSON의 `access_key`를 담는다.

파일 입력은 현재 OS 사용자가 소유한 일반 파일이며 권한이 정확히 `0600`이어야 한다. 심볼릭 링크, 여러 하드 링크, FIFO, 디렉터리는 거절한다. 결과 디렉터리도 현재 사용자의 소유이며 다른 사용자가 쓸 수 없어야 한다. `0700` 디렉터리를 권장한다. 이 검사는 POSIX 파일 권한을 대상으로 하며 원격 공유 파일 시스템의 별도 ACL·백업·접근 정책까지 인증하지 않는다.

모든 키 생성 명령은 `--out`을 요구한다. 결과 파일에는 새 키, 요청 UUID, 대상 계정, 명령의 해시, DB 연결 대상의 해시, 진행 상태가 들어간다. **결과 파일 자체를 비밀로 보관한다.** 저장소에 커밋하거나 공개 아티팩트로 업로드하지 않는다. 백업과 전달도 조직의 비밀 전달 경로를 사용한다. 검토자에게 운영자 DB 연결이나 owner 키를 전달하지 않는다.

CLI는 새 키를 포함한 파일을 `0600`으로 쓰고 파일과 디렉터리를 `fsync`한 다음에만 DB 변경을 시작한다. 결과 파일 공개는 원자적으로 이루어지며, 새 명령은 기존 결과 파일을 덮어쓰지 않는다. 동일 경로의 동시 실행은 `.lock` 파일의 프로세스 잠금으로 거절한다. 이 잠금 파일은 inode 경합을 막기 위해 남겨 둔다. 임시 파일과 잠금 파일에도 비밀 디렉터리의 보관 정책을 적용한다.

표준 출력은 상태와 결과 경로만 포함한다. 정상 결과 예시는 다음 형태다.

```json
{"status":"confirmed","result_file":"/secure/preflight-team/owner-rotation.json"}
```

CLI는 드라이버·파서·파일 예외 메시지를 그대로 출력하지 않는다. `TypeError`를 포함한 예상 밖 오류도 비밀 문자열이나 traceback 대신 상태 코드로 처리한다. 셸 추적(`set -x`)이나 외부 런처의 환경 변수 덤프는 CLI 밖의 동작이므로 운영 명령에 사용하지 않는다.

## 1. 새 공간의 명시적인 bootstrap

초기화에는 **직접 DB 관리자 연결**을 사용한다. 이 명령은 앱 시작 시 실행되지 않으며, 일반 앱 환경 변수만으로 자동 복구되거나 초기화되지 않는다. `--database-admin`을 명시해야 한다.

```bash
.venv311/bin/python -m scripts.team_review_admin bootstrap \
  --database-admin \
  --database-file /secure/preflight-team/admin-database.secret \
  --out /secure/preflight-team/owner-initial.json
```

bootstrap은 기존 저장소 초기화와 같은 advisory lock을 사용한다. 스키마와 테이블을 준비하고, workspace 행이 없는 경우에만 새 상태와 `database_admin:<request_id>` 감사 영수증을 한 트랜잭션에 넣는다. **기존 행이 있으면 절대로 교체하지 않는다.** 그 행이 이 요청 자체의 성공한 bootstrap이라는 동일 UUID·해시 영수증을 가진 경우만 재시도로 인정한다.

`workspace_exists`이면 새 결과 파일의 키를 운영 키로 전달하지 않는다. 기존 owner가 정상 키를 가지고 있으면 정상 관리 경로를 사용하고, 키가 만료·유실됐다면 아래 DB 관리자 복구 절차를 사용한다. 환경 변수에 다른 owner 키를 넣거나 초기화를 다시 호출해서 기존 공간을 덮어쓰지 않는다.

DB 관리자 도구로 같은 작업을 수행할 때도 준비된 키 파일을 먼저 durable하게 보관한다. workspace 상태에는 원문 앱 접근키를 넣지 않고 `apply_admin(None, record)`가 만든 해시·상태·감사 영수증만 넣는다. `INSERT ... ON CONFLICT DO NOTHING`의 실행 성공만으로 초기화를 성공으로 판단하지 않는다. 조회한 스키마 버전, revision, 상태와 해당 UUID의 fingerprint가 준비한 결과와 일치하는지 확인한다. 다른 상태가 있으면 기존 데이터를 그대로 두고 실패로 기록한다. 같은 원칙은 MCP를 이용한 명시적 bootstrap에도 적용되며, 이를 CLI 실행 증거로 바꾸지 않는다.

## 1.1 런타임 DB API 설치와 권한 검증

`bootstrap`은 workspace와 최초 관리자 키를 준비하는 작업이다. 런타임용 제한 API는 별도로 설치한다. `ops/team_review_db_api.py`의 `install_api()`/`verify_api()`와 다음 CLI는 **명시적인 관리자 연결에서만** 실행한다. 앱 시작, 공개 UI, 일반 환경 변수에서 설치나 권한 복구를 자동 실행하지 않는다. 기존 테이블 권한으로 자동 되돌아가는 경로도 없다.

운영자가 먼저 전용 runtime LOGIN 역할을 준비한다. installer는 그 비밀번호를 생성하거나 argv로 받지 않는다. 역할은 superuser/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS가 없어야 하며 **다른 역할의 멤버십도 하나도 없어야 한다.** `NOINHERIT`만으로는 `SET ROLE`을 막지 못하므로 충분하지 않다. 역할 이름은 아래 예시의 비밀이 아닌 식별자이며, 실제 승인된 역할로 바꾼다.

Neon에서는 SQL 역할 생성 시 **원문 비밀번호를 공급자에게 전달해야 하며, Neon은 이를 저장할 때 암호화한다. 미리 해시한 비밀번호는 지원하지 않는다.** 함수 소유자의 `NOLOGIN` 역할은 지원한다. 또한 Neon Console·역할 관리 API·CLI로 생성한 역할에는 `neon_superuser` 멤버십이 부여되므로, 여기서 요구하는 멤버십 없는 runtime 역할은 SQL `CREATE ROLE`과 이후 실제 권한 검사를 기준으로 준비한다. [Neon의 SQL 역할 및 비밀번호 계약](https://neon.com/docs/manage/roles#manage-roles-with-sql).

준비된 Neon 전용 변형은 기존 템플릿의 SCRAM 형식 검사 SELECT 한 개를 제거하고 runtime `CREATE ROLE ... PASSWORD`의 전체 SQL literal placeholder 한 개만 원문 비밀번호용으로 바꾼 것이다. 함수·본문 해시·ACL·권한 검사는 그대로이고 문장 수는 52개에서 51개가 된다. 이 변경은 공급자가 받는 비밀번호 형태를 맞추는 작업이며 실제 운영 실행 승인은 별도다. 승인된 운영자가 비밀 저장 경로의 값을 `psycopg.sql.Literal`로 인용해 공급자 요청에 삽입해야 한다. SQL 인용은 구문을 보호할 뿐 비밀번호를 해시하거나 로그에서 숨기지 않으므로, 비밀번호가 채워진 SQL·요청 payload는 파일이나 저장소에 저장하지 않고 로그에도 남기지 않는다. 앱 접근키는 계속 workspace에 해시만 저장한다.

```bash
.venv311/bin/python -m scripts.team_review_admin install-api \
  --database-admin \
  --database-file /secure/preflight-team/admin-database.secret \
  --runtime-role preflight_team_runtime \
  --out /secure/preflight-team/api-installation.json

.venv311/bin/python -m scripts.team_review_admin verify-api \
  --database-admin \
  --database-file /secure/preflight-team/admin-database.secret \
  --runtime-role preflight_team_runtime \
  --out /secure/preflight-team/api-verification.json
```

설치에는 PostgreSQL 16 이상, 대상 스키마/테이블의 소유권 또는 충분한 GRANT 권한, CREATEROLE과 함수 소유 역할에 대한 ADMIN 권한이 필요하다. superuser를 필수로 요구하지 않는다. 함수 소유자는 스키마별 전용 **NOLOGIN** 역할이고, 다른 역할의 멤버가 될 수 없다. 관리자와 runtime 로그인은 이 소유자 역할과 분리한다.

PostgreSQL 16에서 비 superuser가 만든 역할은 생성자에게 ADMIN이 자동 부여되지만 SET/INHERIT는 기본적으로 부여되지 않는다. installer는 직접 관리자 세션에서만 실행되며, 필요한 경우 관리자 자신에게 일시적인 SET 멤버십을 부여하고 `SET LOCAL ROLE`로 함수 소유자가 되어 함수를 생성한다. 종료 전에 자신이 추가한 멤버십을 제거하거나 이전 옵션으로 복구한다. 기존 생성자 ADMIN 권한은 유지한다. 이 권한은 DB 관리자의 관리 능력이며 runtime에 넘기지 않는다. 모든 설치와 권한 변경은 호출자가 커밋하는 한 트랜잭션 안에 있고, 검증 실패는 롤백한다. [PostgreSQL 16 역할 생성 규칙](https://www.postgresql.org/docs/16/role-attributes.html), [SET ROLE](https://www.postgresql.org/docs/16/sql-set-role.html).

| 주체 | 대상 workspace에 대한 권한 |
|---|---|
| runtime LOGIN | 스키마 USAGE와 `lock_workspace`/`save_workspace` EXECUTE만 갖는다. 테이블 및 열의 SELECT/INSERT/UPDATE/DELETE 등 권한, 스키마 CREATE, 함수 소유자나 다른 역할의 멤버십은 허용하지 않는다. |
| API 함수 소유자 | NOLOGIN, 테이블 SELECT/UPDATE, 스키마 USAGE. 설치 중 필요한 CREATE는 설치 후 회수한다. |
| DB 관리자 | 명시적인 bootstrap/recovery/설치/점검에 필요한 별도 권한. 앱 런타임 DSN으로 배포하지 않는다. |

installer는 모든 API 함수의 PUBLIC EXECUTE를 회수하고 내부 검증 helper에는 runtime EXECUTE를 주지 않는다. 두 공개 함수만 SECURITY DEFINER이며 모든 함수의 `search_path`는 `pg_catalog, pg_temp`로 고정하고 UTC 시간을 사용한다. 설치 확인은 실제 역할 속성·멤버십, 테이블/열 권한, 스키마 권한, 함수 소유자·PUBLIC/런타임 EXECUTE·고정 경로·함수 본문과 언어까지 검사한다. 기존 workspace 데이터는 설치로 바꾸지 않는다. [SECURITY DEFINER 작성 규칙](https://www.postgresql.org/docs/16/sql-createfunction.html).

설치 결과를 잃었어도 새 키가 추가 생성되는 작업은 아니다. 새 결과 경로로 `verify-api`를 실행해 현재 정의와 권한을 확인한다. 설치 자체는 같은 소스로 재실행할 수 있으며 `resume`는 키 관리 작업 전용이다. 결과 파일의 `api_installed`/`api_verified`는 DB API 계약 검사 결과다. 별도의 운영 승인, 로그 정책, 실제 사람 검수 확인까지 완료했다는 뜻은 아니다.

### DB 함수가 보장하는 계약

`lock_workspace(access_key, request_id UUID, command_text)`는 workspace 행을 잠근 뒤 DB 원본의 키 SHA-256, 계정 active, 만료 시각, action에 허용된 역할을 검사한다. `pg_catalog.sha256(bytea)`를 사용하므로 pgcrypto 확장이 필요하지 않다. Python에는 revision, 전체 상태, DB 현재 시각을 반환한다. Python은 그 시각으로 상태 전이를 계산한다. [PostgreSQL 16 기본 해시 함수](https://www.postgresql.org/docs/16/functions-binarystring.html).

`save_workspace(access_key, request_id UUID, command_text, expected_revision, updated_document)`는 동일 트랜잭션의 행 잠금 아래 **DB 원본 자격을 다시 인증**한다. 만료 판단에는 실제 현재 시각인 `clock_timestamp()`를 사용한다. 클라이언트가 설정할 수 있는 사용자 GUC를 인증 근거로 사용하지 않는다. 다음 조건을 통과한 상태만 revision CAS로 저장한다.

- 명령은 UTF-8 크기 제한, action 목록, payload 필수/허용 필드와 타입을 만족해야 한다. request ID와 expected revision의 SQL NULL도 거절한다.
- 문서의 최상위 키는 `schema_version/revision/queue/accounts/keys/receipts`이며 버전과 DB revision을 일치시킨다. 알 수 없는 키를 암묵적으로 보존하거나 허용하지 않는다.
- 일반 action은 owner를 포함해 accounts/keys를 전혀 바꿀 수 없다. owner의 계정 생성·키 교체·활성 변경은 지정한 대상의 정확한 허용 delta만 인정한다. 계정 ID/역할/생성 시각을 임의 변경하거나 owner를 새로 만들거나 비활성화할 수 없다. 키 교체는 대상의 이전 키를 모두 회수하고 30/90일 만료의 새 해시 하나만 허용한다.
- 기존 영수증은 바뀌거나 없어질 수 없다. 변경 요청은 인증된 `actor_id:request_UUID` 영수증 하나만 추가할 수 있고, action, 원문 command_text의 SHA-256 fingerprint, 결과 revision을 일치시킨다. 시각은 해당 트랜잭션 시작 이후이면서 DB 현재 시각 이하이어야 한다. 키 만료도 이 검증된 시각으로 계산한다. runtime은 `database_admin:` 감사 영수증을 추가할 수 없다.
- 같은 actor/UUID를 다시 쓰면 기존 fingerprint가 같아야 하고 상태를 추가 변경할 수 없다. 인증이 재생보다 먼저이므로 회수·만료된 키로 과거 성공을 재생할 수 없다. 조회는 새 영수증을 추가하지 않으며, 만료 배정 정리로 변경된 queue의 저장은 기존 Python 전이대로 처리한다.

원문 키·명령·상태는 psycopg의 bound parameters로 전달한다. 드라이버 오류를 사용자 출력에 그대로 포함하지 않는다. `verify-api` 결과에는 **검사한 관리자 세션**의 `log_statement`, `log_parameter_max_length`, `log_parameter_max_length_on_error`와 `logging_review_required`가 들어간다. 이는 runtime 로그인에 적용된 별도 역할 설정이나 외부 프록시·추적 시스템까지 인증하지 않는다. 실제 runtime 세션의 설정도 운영자가 확인하고, 오류·statement·duration·audit 로그에 파라미터가 남지 않도록 검토한다. bound parameters 자체가 서버 로그에서 비밀을 숨겨준다는 뜻은 아니다. [PostgreSQL 로그 설정](https://www.postgresql.org/docs/16/runtime-config-logging.html).

계정 발급의 `payload.name`은 `ops.labeling.validate_annotator_name()`으로 정규화한 이름을 전달하는 계약이다. 공개 UI와 관리자 CLI는 요청을 만들기 전에 이 처리를 한다. 저장 어댑터나 SQL 함수를 직접 호출한다면 호출자가 같은 처리를 해야 한다. 예를 들어 앞뒤 공백이나 전각 영문자가 남은 원문을 직접 넘기면 Python의 표시명 정규화 결과와 DB의 명령 일치 검사가 달라 저장을 거절한다. 원문 요청·영수증 지문을 저장 중에 암묵적으로 바꾸거나 DB에서 Unicode 정규화를 별도로 구현하지 않는다.

### 이 경계의 명확한 한계

**runtime DSN만으로는 앱 키 인증 없이 workspace를 읽거나 계정·키를 조작할 수 없게 하는 경계다.** 유효한 앱 키와 runtime DSN이 함께 유출되면 `lock_workspace`가 반환하는 전체 queue, 계정 메타데이터와 기존 영수증을 볼 수 있다. 또한 해당 역할의 action을 사용해 Python의 전체 대기열 의미 검증을 우회한 queue 제안을 제출할 수 있다. SQL은 독립 검수, 본인 배정, 라벨의 의미, reviewer 간 블라인드 조회를 전부 재구현하지 않았다. 브라우저의 정상 앱 경로에서는 Python 검사가 계속 적용되지만, 이 DB API를 전체 블라인드 워크플로의 독립 보안 계층이라고 설명하면 안 된다.

DB 관리자는 테이블·함수·역할을 관리할 수 있으므로 앱 키에 대한 보안 경계 밖에 있다. 다른 DB 확장이나 별도로 설치한 SECURITY DEFINER 함수의 권한까지 이 verifier가 전수 감사하는 것도 아니다. 여기서 확인하는 것은 지정한 workspace와 이 모듈이 설치한 함수·역할 계약이다. 실제 운영 설치와 DB 권한 변경에는 별도의 명시적인 승인이 필요하다. 코드·CI 테스트가 있다는 사실은 운영 변경이 승인되거나 실행됐다는 증거가 아니다.

## 2. 검수자·조정자 발급과 기존 계정 키 교체

정상 owner 키로 검수자 계정을 발급한다.

```bash
.venv311/bin/python -m scripts.team_review_admin issue \
  --database-file /secure/preflight-team/runtime-database.secret \
  --owner-key-file /secure/preflight-team/owner-initial.json \
  --name '검수 담당 A' --role reviewer \
  --out /secure/preflight-team/reviewer-a-issued.json
```

조정자 발급은 `--role adjudicator`를 사용한다. 이름은 계정을 구분하는 표시이며 실명 인증을 뜻하지 않는다. 발급한 `account_id`는 결과 파일에 보관된다. 같은 사람에게 여러 계정을 발급한다고 독립 검수가 되는 것은 아니다.

기존 계정을 유지한 채 키만 교체하려면 그 계정 ID를 지정한다.

```bash
.venv311/bin/python -m scripts.team_review_admin replace \
  --database-file /secure/preflight-team/runtime-database.secret \
  --owner-key-file /secure/preflight-team/owner-initial.json \
  --account-id acct_0123456789abcdef0123456789abcdef \
  --out /secure/preflight-team/reviewer-a-replacement.json
```

예시 계정 ID는 실제 대상 ID로 바꾼다. `replace`는 owner를 대상으로 받을 수 없다. owner 자기 회전은 다음 전용 명령을 사용한다. 비활성 계정의 키 교체는 계정을 자동 활성화하지 않으며 `applied_inactive`로 표시한다. 활성화가 필요하면 기존 owner UI에서 명시적으로 처리한다.

정상 발급·교체·회전은 모두 `PostgresTeamStore.execute`의 owner 인증과 역할 검사를 사용한다. DB 관리자 자격으로 일반 계정 정책을 건너뛰는 명령이 아니다. 새 키가 실제 대상 계정으로 인증되는지도 확인한다. 예전에 성공했던 발급 영수증을 재생했지만 그 키가 이후 교체·만료됐다면 `superseded`를 반환하며 이전 키를 되살리지 않는다.

## 3. owner 자기 키 회전

현재 키가 유효할 때 실행한다.

```bash
.venv311/bin/python -m scripts.team_review_admin rotate-owner \
  --database-file /secure/preflight-team/runtime-database.secret \
  --owner-key-file /secure/preflight-team/owner-initial.json \
  --out /secure/preflight-team/owner-rotation.json
```

새 키를 디스크에 확실히 저장하기 전에는 기존 키를 회수하지 않는다. 성공 후 owner 계정은 그대로 유지되고 새 키의 만료가 90일로 갱신된다. 이후 명령은 새 결과 파일을 `--owner-key-file`로 사용한다.

커밋 직후 네트워크가 끊기면 기존 키는 이미 회수됐을 수 있다. CLI의 `resume`는 같은 요청을 기존 키로 확인하고, 인증이 거절되면 준비 파일의 새 키로 같은 요청을 확인한다. 저장소의 계정 ID는 같으므로 이미 저장된 영수증을 반환할 수 있다. 새 키를 다시 생성하거나 새 UUID로 교체를 반복하지 않는다.

## 4. 응답 유실과 재실행

`outcome_unknown`이나 중단 후에는 **같은 결과 파일**을 사용한다. 아래 예시에는 비밀값이 들어 있지 않다.

```bash
.venv311/bin/python -m scripts.team_review_admin resume \
  --database-file /secure/preflight-team/runtime-database.secret \
  --owner-key-file /secure/preflight-team/owner-initial.json \
  --out /secure/preflight-team/owner-rotation.json
```

owner 회전이 이미 커밋된 경우라면 기존 키 입력을 생략하고 파일에 저장된 새 키로 확인할 수도 있다. 회전이 아직 커밋되지 않았다면 유효한 기존 owner 키가 필요하다. 발급·다른 계정 교체의 재시도에는 정상 owner 키가 필요하다.

DB 관리자 bootstrap 또는 복구의 재시도에는 관리자 연결과 `--database-admin`을 다시 명시한다.

```bash
.venv311/bin/python -m scripts.team_review_admin resume \
  --database-admin \
  --database-file /secure/preflight-team/admin-database.secret \
  --out /secure/preflight-team/owner-recovery.json
```

재시도는 파일의 명령·키·UUID·hash를 검증하며 바꾸지 않는다. 같은 UUID인데 hash가 다르면 거절한다. 다른 연결 대상이나 스키마로 바뀌어도 거절한다. DB 비밀번호만 바뀐 경우는 대상 바인딩을 유지한다. 바인딩은 명시된 접속 대상 정보의 해시이며 DB의 영구적인 신원을 인증하는 서명은 아니다. 운영에서는 host·database·user가 명시된 DSN을 사용하고, service 설정이나 DNS의 대상을 다른 workspace로 바꾸면서 기존 파일을 재사용하지 않는다.

별개의 결과 파일을 지정한 명령은 별개의 관리 작업이다. 같은 계정에 두 번 교체하면 마지막으로 커밋한 키 하나만 남는다. `confirmed`는 확인 시점의 결과이며 영구적인 유효성 보장이 아니다. 예전 파일을 `resume`하면 현재 사용 가능한지 다시 확인하고, 나중에 회수된 키를 재설치하지 않는다. 한 관리 작업에는 한 결과 파일을 사용한다.

| 상태 | 해석과 다음 단계 |
|---|---|
| confirmed | 해당 요청의 커밋과 새 키 인증을 확인했다. 이후 관리에는 이 결과 파일을 사용한다. |
| applied_inactive | 키 교체 요청은 적용됐지만 대상 계정이 비활성이다. 로그인 가능한 키로 안내하지 않는다. |
| superseded | 이 파일의 키는 현재 사용할 수 없다. 과거 키를 재활성화하지 않았다. 최신 발급 또는 복구 기록을 확인한다. |
| outcome_unknown | 커밋 여부를 알 수 없다. 같은 파일로 `resume`한다. 새 키나 새 요청으로 대체하지 않는다. |
| revision_conflict | 관리자 복구 전에 다른 작업이 revision을 바꿨다. 현재 상태를 새로 확인하고 변경 내용을 검토한 뒤 새로운 복구 요청을 준비한다. 자동으로 expected revision을 바꾸지 않는다. |
| workspace_exists | 새 bootstrap 대상이 아니다. 기존 공간과 키를 보존하고 정상 관리 또는 명시적 복구를 선택한다. |
| request_conflict / invalid_operation_file | 같은 요청의 내용이 달라졌거나 파일이 손상됐다. 임의 수정으로 통과시키지 말고 준비 파일과 운영 기록을 확인한다. |
| result_file_exists / operation_busy | 기존 파일 또는 같은 경로의 진행 작업이 있다. 새 작업으로 덮어쓰지 말고 기존 작업을 확인한다. |
| authentication_failed / permission_denied | 현재 키로 수행할 수 없다. owner 상태를 확인하고 필요하면 명시적 복구를 사용한다. |

키 파일의 마지막 상태 갱신만 실패했을 수도 있다. `prepared`라고 적혀 있다는 사실은 DB가 변경되지 않았다는 증거가 아니다. DB의 영수증과 동일 요청을 확인해야 한다. DB 상태는 CLI 결과보다 먼저 커밋될 수 있으며, 이 때문에 키와 요청을 먼저 보관한다.

## 5. owner 키 만료·유실 시 DB 관리자 복구

이 경로는 정상 앱 인증을 사용하지 않고 **직접 DB 관리 권한을 명시적으로 사용**한다. 권한 있는 운영자가 만료·유실 사유와 대상 공간을 확인한 뒤 실행한다. 공개 UI, 일반 앱 환경 설정, 서버 재시작에서 자동 실행되지 않는다. `PREFLIGHT_TEAM_ADMIN_DATABASE_URL`을 앱 런타임에 둘 필요가 없다.

먼저 상태를 읽어 revision을 확인한다. 결과에는 키 값·키 해시·검토 원문이 없고 revision, 계정/영수증 개수, owner 활성 상태와 만료 시각이 들어간다. 이 조회도 결과 파일에 쓰며 stdout에는 경로와 상태만 표시한다.

```bash
.venv311/bin/python -m scripts.team_review_admin inspect \
  --database-admin \
  --database-file /secure/preflight-team/admin-database.secret \
  --out /secure/preflight-team/recovery-inspection.json
```

확인한 revision이 예를 들어 42라면 다음과 같이 명시한다. 감사 사유에 비밀값이나 개인정보를 쓰지 않는다.

```bash
.venv311/bin/python -m scripts.team_review_admin recover-owner \
  --database-admin \
  --database-file /secure/preflight-team/admin-database.secret \
  --expected-revision 42 \
  --reason '관리자 접근키 만료 확인 후 복구' \
  --out /secure/preflight-team/owner-recovery.json
```

복구 트랜잭션은 다음 조건을 모두 지킨다.

1. 기존 스키마 버전이 1이고 DB revision 컬럼과 JSON 문서의 revision이 같아야 한다. 형식 불일치를 자동으로 고치거나 빈 공간으로 대체하지 않는다.
2. 행 잠금 후 현재 revision이 명시한 `--expected-revision`과 같아야 한다. 같은 revision으로 동시 복구를 시작하면 한 요청만 커밋하고 다른 요청은 충돌한다.
3. 모든 이전 owner 역할의 키를 회수하고, 동일한 `acct_owner` 계정에 새 90일 키 하나를 설치한다. owner가 비활성이었다면 활성화한다.
4. 대기열, 광고, 라벨·조정 기록, 배정, 다른 계정과 키, 기존 영수증을 보존한다. owner 계정의 표시명과 생성 시각도 유지한다.
5. 기존 revision을 증가시키며 새 키 설치와 감사 영수증을 **한 번에 커밋**한다. 감사에는 UUID, 명령 hash, 시각, 실제 DB `current_user`, 명시적 DB 관리자 권한 사용, 사유, 이전 revision, 회수한 owner 키 수를 기록한다. 원문 키와 DSN은 기록하지 않는다.
6. 응답 유실 후에는 동일 UUID·hash 영수증만 재생한다. 더 나중의 회전이나 복구로 이 키가 회수됐다면 `superseded`로 알려 주고 되살리지 않는다.

일반 앱의 runtime DB 계정과 관리 계정은 분리한다. runtime에는 위의 두 API 함수 EXECUTE와 스키마 USAGE만 허용한다. workspace 테이블이나 열의 SELECT/UPDATE 권한을 runtime에 주지 않는다. bootstrap에는 별도 관리자의 DDL·INSERT 권한이 필요하고, 복구에는 해당 행의 SELECT/UPDATE 권한이 필요하다. 관리 CLI는 명시적인 직접 관리 경로를 사용하며 앱의 키 인증을 DB 관리자에 대한 보안 경계로 취급하지 않는다. `PostgresTeamStore(..., admin_mode=True)`는 기존 관리·트랜잭션 장애 테스트용 직접 경로이고, `from_env()`나 일반 앱 설정으로 켜지지 않는다.

## 용량 상한과 안전 관리 작업

현재 정상 작업의 상한은 계정 100개, 광고 2,000개, 요청 영수증 10,000개, 직렬화된 상태 30,000,000바이트, 한 명령 1,000,000바이트다. 계정 생성 등 정상 작업은 상한에서 거절될 수 있다. 비활성 계정도 기존 기록의 식별자이므로 자동 삭제하거나 이름을 재사용하지 않는다.

인증된 owner의 `replace_key`와 `set_active`는 영수증 개수·직렬화된 상태 크기의 정상 입장 상한에서 예외를 받는다. 작업 장부를 채워 버렸다는 이유로 접근권을 회수하거나 키를 바꾸지 못하게 해서는 안 되기 때문이다. 기존 계정의 키 교체에는 계정 추가 슬롯이 필요하지 않다. owner 자기 회전도 같은 `replace_key` 계약을 사용한다.

`overview/view/accounts/disagreements/export` 조회 역시 영수증·직렬화 크기 상한 때문에 거절하지 않는다. 기존 인증·역할·본인 배정 조건은 여전히 적용한다. 만료 키가 조회할 수 있다는 뜻은 아니다. 조회 중 만료된 배정 정리는 기존 저장소의 동작을 따른다. 명령 크기·상태 유효성·기타 별도 검사를 무시하는 포괄 예외도 아니다.

DB 관리자 복구는 기존 정상 작업 장부가 가득 차도 새 키와 감사 영수증을 저장할 수 있다. 기존 영수증을 지우거나 순환 덮어쓰지 않는다. 이 예외는 무제한 정상 운영 기능이 아니며, 상한에 도달하면 백업·보관·확장 계획을 별도로 검토해야 한다. 영수증을 무작정 삭제하면 중복 요청 방지와 감사 근거가 깨질 수 있으므로 이 CLI에는 장부 초기화 명령이 없다.

## 검증과 운영 한계

관련 검사:

```bash
.venv311/bin/python -m pytest -q -o addopts='' \
  tests/test_team_review_admin.py \
  tests/test_team_review.py \
  tests/test_team_review_adversarial.py \
  tests/test_team_review_postgres.py \
  tests/test_team_review_admin_review.py \
  tests/test_team_review_db_api.py \
  tests/test_team_review_db_boundary_review.py --tb=short
```

새 DB 경계와 CLI를 포함한 위 명령의 마지막 로컬 검증은 **86개 통과, PostgreSQL 통합 44개 미실행**이다. 로컬 실행에는 `PREFLIGHT_TEAM_TEST_DATABASE_URL`이 없었다. 통제된 메모리 저장소로 실제 순수 상태 전이 함수를 호출했고, private 파일 권한·원자성·잠금·파일/디렉터리 fsync, 저장 전 실패, 커밋 후 응답 유실, 결과 파일 상태 갱신 실패, 동일 요청 재시도, 이전 키 회수, 기존 검토/배정/계정 보존, 다른 hash 거절, 용량 상한, 민감 예외 출력 방지를 검증했다.

고정 소스 `fc8724dcdaaca6a995c2416f6f0076806a773e56`의 [PR CI 39570494115](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39570494115)는 PostgreSQL 사전 검사와 전체 검사를 모두 통과했다. 회귀 검사와 smoke도 성공했다. 아래는 서로 다른 CI 단계의 결과이며 통과 수를 합산하지 않는다.

| CI 단계 | 결과 | pytest 실행 시간 |
|---|---:|---:|
| PostgreSQL 사전 검사 | 38 passed | 4.10초 |
| 전체 pytest | 4,281 passed | 252.09초 |

PostgreSQL 검사는 별도의 무작위 테스트 스키마와 전용 runtime LOGIN으로 실행한다. 기존 bootstrap/replay·동시 복구·revision 불일치·커밋 장애 검사는 명시적 직접 관리자 경로로 유지했다. 새 API 검사는 실제 table 권한 거절, 올바른 앱 키 없는 조회 거절, 정상 계정/큐 전이, 연속된 정상 저장의 기존 영수증 보존, 키 회전과 동일 요청 재생, builtin SHA-256, 비 superuser CREATEROLE 설치 및 임시 멤버십 복구를 검사한다. 독립 공격 회귀는 SQL NULL, accounts/keys 변조, 잘못된 owner delta, 미래 시각/만료 동시 위조, 다른 actor 또는 기존 영수증 변조, load 후 키 만료, role membership과 열 권한 검증을 포함한다. CI의 격리된 `PREFLIGHT_TEAM_TEST_DATABASE_URL`로 실행해야 한다. 운영 DB를 테스트 대상으로 넣지 않는다. CI나 실제 운영에서 추가 검증한 결과는 실행 ID와 함께 별도로 기록하며 로컬에서 미실행한 검사를 성공으로 적지 않는다. 위 CI 성공은 운영 DB 초기화나 실제 사람의 검수 참여를 증명하지 않는다.

이 도구는 OS 계정, DB 운영 권한, 비밀 파일의 보관·전달, 실제 참여자 확인을 대신하지 않는다. 키 결과 파일과 백업이 모두 유실되면 DB 관리자 복구가 필요하다. DB 자체의 손실·손상은 별도의 백업 복원 문제이며 owner 복구 명령으로 대기열을 재생성하지 않는다.
