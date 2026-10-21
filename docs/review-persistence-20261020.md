# 검수 대기열 자동 저장 점검 — 2026-10-20

## 결론과 저장 범위

검수 대기열을 방문자의 브라우저 IndexedDB에 자동 저장하고, 새 세션에서 자동 복원하도록 바꿨다. 서버 디스크에 큐를 쓰지 않으므로 Render의 프로세스 종료·재시작이나 컨테이너 교체가 브라우저 저장본을 지우지 않는다. GitHub Pages 브라우저 실행판도 같은 저장 형식을 사용한다.

저장 단위는 **같은 사이트 주소의 같은 브라우저 프로필**이다. 방문자 전체가 하나의 익명 서버 큐를 공유하지 않는다. GitHub Pages와 Render는 사이트 주소가 달라 각각 별도 저장소를 갖는다. 기기·브라우저·프로필을 옮기려면 전체 JSONL 백업을 내려받아 가져온다.

이 기능은 현재 공개 데모의 개인별 작업 보존을 위한 것이다. 계정 인증, 직원별 접근 권한, 여러 기기의 실시간 협업 큐를 제공하는 것은 아니다. 라벨러 이름은 기록에 쓰는 이름이며 로그인 자격 증명이 아니다. 같은 공용 브라우저 프로필을 사용하는 사람끼리는 저장본을 볼 수 있다.

## 원인과 변경

기존 `ReviewQueue`는 메모리 객체이고, 화면은 `st.session_state["rq_queue"]`만 유지했다. `to_jsonl/from_jsonl`은 직렬화·검증 기능이었으며 자동 저장 경로가 없었다. 따라서 세션이 사라지면 사용자가 직접 내보낸 파일 외에는 복구할 수 없었다.

| 파일 | 역할 |
|---|---|
| `ops/review_persistence.py` | 복원·저장 확인 상태, 요청 식별, 오류·충돌 처리, 엄격한 JSONL 재검증 |
| `ui/review_queue_storage.py` | Streamlit 화면과 브라우저 연결, 복원 전 편집 차단, 한국어 상태·복구 안내 |
| `ui/review_storage_frontend/storage.js` | IndexedDB 읽기·쓰기와 버전 확인을 하나의 트랜잭션으로 실행 |
| `ui/review_storage_frontend/index.html` | Streamlit 컴포넌트 프로토콜, 전체 JSONL Blob 다운로드 |
| `ui/review_queue_view.py` | 추가·점유·반납·라벨·조정·내보냄 표시·설정·가져오기를 자동 저장 경로에 연결 |
| `scripts/build_site.py` | stlite 컴포넌트 HTML에 동일한 저장 JS를 포함해 상대 자산 경로 없이 실행 |

처음에는 저장본 읽기 완료를 기다리고, 검증된 큐를 복원한 후 편집 화면을 연다. 큐 내용이 바뀌면 저장을 시작하고 완료 응답을 받을 때까지 다음 편집을 막는다. `put()` 요청의 성공만으로 저장 완료를 표시하지 않고 `transaction.oncomplete`를 기다린다. 완료 후에만 **“이 브라우저에 저장됨”**과 해당 작업의 성공 안내를 보여 준다.

저장을 도입하기 전부터 열려 있던 세션의 메모리 큐도 빈 브라우저 저장소로 자동 이관한다. 기존 브라우저 저장본과 메모리 큐가 다르면 둘 중 하나를 조용히 선택하지 않고 충돌로 처리한다.

## 동시 수정과 실패 처리

각 저장본은 전체 JSONL, 증가하는 `revision`, 마지막 `write_id`를 함께 가진다. 저장 요청에는 화면이 읽었던 `expected_revision`을 넣는다. 현재 버전 읽기, 예상 버전 비교, 새 버전 기록을 **하나의 IndexedDB `readwrite` 트랜잭션**에서 처리한다. 두 탭이 같은 이전 버전을 가지고 저장해도 먼저 완료된 한 탭만 반영되고 다른 탭에는 충돌이 반환된다.

응답이 유실돼 재시도하더라도 `write_id`와 내용이 같은 이미 완료된 저장은 중복 반영하지 않는다. 재시도용 요청 번호와 쓰기 식별자는 별도로 관리한다. 오래된 응답은 현재 요청 번호와 맞지 않으면 무시한다.

| 상황 | 실제 동작 |
|---|---|
| 아직 초기 복원이 끝나지 않음 | 추가·라벨 등 편집 화면을 열지 않음 |
| 저장 중 | 다음 편집과 성공 안내를 미루고 저장 중임을 표시 |
| 다른 탭이 먼저 저장함 | 최신 저장본을 덮어쓰지 않고 현재 변경을 유지. 전체 백업 후 “최신 저장본 불러오기 (현재 변경 버림)”를 사용자가 선택 |
| 공간 부족·권한 거부·쓰기 중단 | 저장 성공을 주장하지 않고 다음 편집을 막음. 현재 작업은 세션에 남기고 전체 백업·재시도 제공 |
| 저장된 JSONL 형식·상태·인용이 잘못됨 | `ReviewQueue.from_jsonl` 검증에서 거부. 빈 큐로 덮어쓰지 않음 |
| 손상된 저장 레코드 | 저장을 중단. 잘못된 버전 값을 임의로 0으로 취급하지 않음 |

JSONL의 기존 30 MB 상한도 저장과 복원에 적용한다. 브라우저 자체의 저장 공간 제한이 더 작거나 해당 사이트의 다른 데이터가 공간을 차지하면 그보다 먼저 실패할 수 있다.

복구용 백업은 브라우저에서 Blob 파일을 내려받는다. 긴 큐를 화면에 일부만 잘라 보여 주는 방식에 의존하지 않으며, 저장 오류 메시지에 검증용 정답이 담긴 JSONL을 직접 펼치지 않는다. 라벨 입력은 기존 `AnnotatorView` 경로를 유지한다. 자동 저장이 일반 항목과 검증용 항목의 표시 차이를 새로 만들지 않는다.

## 재현과 검증

일반 Python·화면 검증:

```bash
python -m pytest tests/test_review_persistence.py tests/test_review_persistence_ui.py tests/test_labeling_ui.py
```

실제 브라우저 검증은 개발 도구 Playwright와 Chromium이 필요하다. 앱 실행에는 이 도구들이 필요하지 않다.

```bash
python -m pip install playwright
python -m playwright install chromium
python -m pytest tests/test_review_persistence_browser.py tests/test_review_persistence_end_to_end.py
```

직접 실행해 확인한 항목:

- Python 저장 상태 검증: 복원 전 대기, 엄격한 라벨 재검증, 응답 번호·버전 확인, 충돌과 오류 이후 미저장 작업 보존, 멱등 재시도.
- AppTest: 새 세션의 자동 복원, 라벨 보존, 저장 실패 때 성공 안내 금지, 초기 복원 전 편집 차단, 저장 실패 재시도, 오래된 탭의 명시적 재로드, 기존 세션 자동 이관.
- Chromium IndexedDB: 페이지 새로고침, 브라우저 프로세스 종료 후 동일 프로필 재실행, 두 탭 충돌, 동시 저장 8회 중 1회 성공·7회 충돌, 프로필 간 분리, 저장 공간 부족·권한 거부 시 기존 저장본 유지, 손상 레코드 거부, 20만 자를 넘는 전체 백업.
- 실제 Streamlit 화면: 광고 추가 → 라벨 제출 → 자동 저장 완료 → 새로고침 → 같은 라벨 복원 → 서버 프로세스 종료·재시작 → 같은 라벨 자동 복원. 저장 JSONL이 같음을 비교했다.
- 실제 stlite 1.11.0 번들: Chromium 134에서 저장 연결로 광고를 추가 → 자동 저장 완료 → 페이지 전체 새로고침 → 같은 광고 자동 복원. `page_errors: []`를 확인했다. [결과 JSON](evidence/persistence-2026-10-20/stlite-result.json), [복원 화면](evidence/persistence-2026-10-20/stlite-restored.png).

stlite 실험에서는 처음에 컴포넌트의 상대 모듈 경로가 앱 루트의 `/storage.js`로 풀려 404가 발생했다. 실제 iframe이 `about:srcdoc`인 것을 확인했고, `build_site.py`가 검증된 같은 JS 소스를 컴포넌트 HTML에 포함하도록 수정했다. 자산 포장 회귀 테스트를 실패부터 확인한 다음, 수정된 번들로 위 자동 복원을 다시 실행해 통과했다. 런타임/CDN 장애와 저장 자산 경로 결함을 구분한 결과다.

브라우저 실행판 확인 명령:

```bash
python scripts/verify_review_persistence_stlite.py --out /tmp/preflight-review-browser
```

이 명령은 배포와 같은 `build_zip` 함수를 사용하고 저장 연결만 여는 작은 진입점을 추가한다. 실제 stlite/Pyodide CDN을 읽으며 유료 모델 API는 호출하지 않는다. 성공·실패 결과와 화면 이미지를 지정한 디렉터리에 남긴다.

## 남는 보존 경계

브라우저 저장은 별도 서버 백업이 아니다. 사이트 데이터 삭제, 브라우저 프로필 삭제, 시크릿 세션 종료, 브라우저의 저장 공간 회수 정책, 기기 분실 시 저장본을 잃을 수 있다. 작업 화면에서도 이 범위를 안내하며 중요한 작업은 전체 JSONL 백업을 함께 보관해야 한다. 저장 중 화면을 닫으면 마지막 변경의 완료를 확인하지 못하므로 완료 안내를 기다려야 한다.

지원 브라우저에서는 `durability: "strict"`를 요청한다. 지원하지 않으면 표준 IndexedDB 트랜잭션을 사용한다. 운영체제·기기 자체의 고장을 포함한 무손실 보존을 보장하지 않는다.

저장소가 손상됐을 때는 먼저 제공되는 복구용 백업을 보관한다. 유효한 별도 백업이 있다면 브라우저의 해당 사이트 데이터를 지운 뒤 페이지를 다시 열고 고급 메뉴에서 백업을 가져올 수 있다. 자동 복원이 손상된 저장본을 조용히 삭제하거나 빈 큐로 대체하지 않는다.

## 기술 근거

- [MDN — IndexedDB 트랜잭션](https://developer.mozilla.org/en-US/docs/Web/API/IDBTransaction): 읽기·쓰기는 트랜잭션 안에서 수행하며 요청 성공과 트랜잭션 완료는 구분된다.
- [MDN — 브라우저 저장 공간과 데이터 회수](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria): 사이트별 저장 범위, 저장 공간 제한, 기본 보존 방식과 삭제 가능성.
- [Streamlit — custom component 선언](https://docs.streamlit.io/develop/api-reference/custom-components/st.components.v1.declare_component): 로컬 정적 자산을 가진 브라우저 컴포넌트 연결.

구현·검증 기록은 이 저장소의 위 파일과 테스트를 기준으로 한다. 현재 무료 Render 서비스에 영구 디스크나 새 외부 데이터베이스를 추가하지 않았다.
