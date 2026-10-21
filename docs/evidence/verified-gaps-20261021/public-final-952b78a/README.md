# 병합 후 공개 화면 검증 — 952b78a

[public-ui 실행 39582480518](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39582480518), job `119163193927`에서 실제 공개 Pages·Render를 검사했다. 실행은 2026-10-21 10:12:50 UTC에 성공했다. [PR #4](https://github.com/sokldjs554/toss-ads-ai-project/pull/4)의 병합 SHA `952b78a27ddec6dab39c76498b9c61aadfd559a6`를 수동 입력으로 고정했으며, `pin.json`·체크아웃·화면 빌드·Pages 원본 ZIP이 같은 버전을 가리킨다. 이후 이 결과를 보존하는 문서 커밋과 검사 대상 소스는 구분한다.

## 실제 결과

`result.json`의 실제 값은 **242 checks / 0 failed / status passed**다. 각 assertion의 `ok` 값과 네 observation의 완료 상태를 다시 대조했다. Chromium 141.0.7390.37, Playwright 1.56.0, Python 3.11.17, 기본 TLS 검증을 사용했다. 모델·DB 비밀값을 주입하지 않았다.

| 공개 조합 | 최초 저장소 load 응답 | 새로고침 후 load 응답 | 확인한 저장 컴포넌트 |
| --- | --- | --- | --- |
| Render desktop 1280×900 | 수락 | 수락 | 서버의 component iframe |
| Render mobile 390×844 | 수락 | 수락 | 서버의 component iframe |
| Pages desktop 1280×900 | 수락 | 수락 | 인라인 srcdoc, storage code·execute 존재 |
| Pages mobile 390×844 | 수락 | 수락 | 인라인 srcdoc, storage code·execute 존재 |

load 응답의 UI 근거는 `review_queue_storage.ready()` 이후에만 렌더링되는 빈 **라벨러 이름** 입력이다. 각 조합에 `initial-load`와 `reload` 두 관측이 있으며, `load_acknowledged`는 모두 true다. Pages의 두 phase에서 srcdoc 길이는 6,715자이고 SHA-256은 `f13c26d8a701f16a99ea3dc324bc4ba2db9d4ec5939dbd63042b556e806357c4`다. 새로고침 뒤에도 빌드가 일치했다.

이는 **새 브라우저 context의 빈 개인 저장소를 열고 load 응답을 받는 검사**다. 큐 추가·배정·라벨·snapshot 저장 명령을 보내지 않았다. 빈 IndexedDB를 열 때 DB/object store가 초기화될 수 있다. 비어 있지 않은 큐의 저장 내구성, 실제 사람 검수, 운영 공동 DB 연결, 다운로드 파일의 내용 검증은 이 실행의 측정 범위에 포함되지 않는다. 비어 있지 않은 저장 흐름은 별도의 기존 Chromium 회귀 근거를 참고한다.

기존 전체 모델 결과 `features-20261021-run1`을 표시했으며, 159회 원본 보고서 SHA-256 `7c7c1947986ab168d09b4913b951a8c1529b25c7c26f6d454a461b1901b75c7f`는 유지됐다. 별도 8회 wire smoke를 이 전체 평가의 분모에 합치지 않았다. 새 모델 요청은 네 조합 모두 0개였다.

## 보존한 진단 로그와 해석 한계

| 조합 | page exception | console 오류 | 실패/중단 요청 |
| --- | ---: | ---: | --- |
| Render desktop | 0 | 0 | 0 |
| Render mobile | 0 | 1 | `storage.js`와 `/media/*.json` 두 요청의 `ERR_ABORTED` |
| Pages desktop | 0 | 8 | `stlite.invalid` 사전 URL 요청 4개 DNS 실패 |
| Pages mobile | 0 | 8 | `stlite.invalid` 사전 URL 요청 4개 DNS 실패 |

Pages 진단은 [이전 저장 성공 실행의 framework 경로 분석](../../final-2026-10-21/stlite-upgrade/storage-compatibility/README.md)에 기록된 `checkSourceUrlResponse` 사전 검사와 같은 유형이다. 이번에는 실제 별도 kernel/iframe srcdoc 로드 및 초기·새로고침 load 응답을 함께 확인했다. 오류 로그는 삭제하거나 성공 항목으로 바꾸지 않았다.

Render mobile의 console에는 `Client Error: Download Button fetch error - Failed to fetch`가 한 건 있다. 이를 stlite 진단으로 분류하지 않는다. 원시 기록에는 요청 중단의 시각·직접 원인이 없어, 이 실행만으로 원인을 단정하지 않는다. 242개 화면/저장소 조건의 성공과 다운로드 결과 검증은 서로 다른 범위다.

## 파일과 출처

- `pin.json`: 시작 시 main 고정과 TLS 검증.
- `result.json`: 242개 조건, 네 관측의 원시 console/network 로그, 저장 bridge metadata, 코드·보고서·공개 ZIP 해시.
- `python-packages.txt`, `workflow.txt`: 실행 패키지와 workflow 식별자.
- `deployment.json`: 최종 PR CI, Pages·Render 배포의 실제 SHA와 완료 근거.
- `*-storage-initial-load.png`, `*-storage-reload.png`: 네 조합의 load/reload 캡처 8장.
- `pages-review-queue-1792577457406.jpg`: 같은 배포를 Cloud Browser에서 직접 연 추가 관측. 수정된 독립 사람 검수 0건 안내를 표시한다. 자동 검사 242개의 일부로 세지 않는다.
- `public-ui-success-1792577728825.jpg`: GitHub 실행의 실제 성공 화면과 242/0 요약.

전체 artifact `12338196422`는 3,346,579 bytes이며 ZIP SHA-256은 `9627ce5737ead9e35d9ea22129694655550733590473f25588db8c3aa4de4a6d`다. 다운로드 후 digest를 검증하고 위 파일을 추출했다. 전체 archive에는 기존 품질·성과·대화 화면 캡처도 있으며, 이 폴더는 새 저장소 확인에 필요한 캡처를 보존한다.

공개 Pages `src.zip`은 286개 파일, SHA-256 `cd9e2662604aef4fedd7ae2842c226a39345557387d550ffe2cb0d13c3bf96da`, 빌드 `952b78a`다. 운영 DB 환경 설정은 바꾸지 않았고 실제 공동 공간과 독립 사람 검수 제출은 모두 0이다.
