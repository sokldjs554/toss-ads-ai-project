# stlite 1.11 / 1.12 저장·복원 비교

**각 버전 12개 기능 확인 통과, 종료 코드 0**이다. 브라우저에 가상 광고를 저장한 뒤 본 앱의 운영 화면에서 복원·배정하고, 다시 새로고침해 같은 담당자와 광고를 이어서 보는 경로를 확인했다. 모든 데이터는 테스트용이며 모델 API를 호출하지 않았다.

## 대상과 방법

| 항목 | 값 |
| --- | --- |
| checkout | `d9633d6efcf29ea99cdfb8a4eee2990400105244` |
| 공통 앱 묶음 SHA-256 | `d89e45ce3ede57323243010183dcb9cc4e24eaa1d028162957294f0ff711ddb9` |
| 브라우저 / 검사 도구 | Chromium 141.0.7390.37 / Playwright 1.56.0 |
| 1.11.0 앱 런타임 | Python 3.13.2 · Streamlit 1.65.0 · pydantic 2.12.5 |
| 1.12.0 앱 런타임 | Python 3.14.2 · Streamlit 1.65.0 · pydantic 2.12.5 |
| 저장 위치 | 동일 브라우저 origin의 IndexedDB `preflight.review_queue` |

같은 소스로 앱 묶음을 한 번 만든 뒤 임시 HTML 두 개에서 stlite 버전만 바꿨다. 각 버전은 새 브라우저 context로 시작해 서로의 저장 내용을 공유하지 않는다. 테스트용 `persistence_check.py` 진입점은 기존 `ReviewQueue`와 저장 컴포넌트를 그대로 사용해 가상 광고 한 건을 넣는다. 이어지는 전체 앱은 원래 `app.py` 진입점이다. 가상 광고를 넣기 위해 제품 코드나 실제 저장 snapshot을 직접 수정하지 않았다.

다음 경로를 두 버전에서 동일하게 실행했다.

1. 최소 화면에서 가상 광고를 추가하고 저장 완료 응답, IndexedDB revision 1, 제목을 확인한다.
2. 최소 화면을 새로고침해 광고가 복원되는지 확인한다.
3. 본 앱의 운영 화면에서 대기 **1건**과 광고 제목을 확인한다. 표의 접근성 셀은 DOM에 존재하는지 확인하고, 가시성은 대기 수와 다음 단계의 실제 광고 카드로 확인한다.
4. `호환성 검증자`를 입력하고 `다음 광고 받기`를 누른다. 실제 라벨링 폼에 `런타임 호환성 확인 광고`가 보이는지 확인한다.
5. 배정 결과가 IndexedDB revision 2와 담당자 이름으로 저장되는지 확인한다.
6. 본 앱을 새로고침하고 같은 담당자를 입력한다. 새 광고를 배정하지 않아도 동일한 광고가 복원되는지 확인한다.
7. 저장 제목·담당자·revision이 유지되는지, Streamlit/브라우저 예외나 모델 API 요청이 없는지 확인한다.

각 버전의 결과에 세 snapshot 단계와 해시가 있다. 두 버전 각각 배정 직후와 최종 새로고침 후 JSONL SHA-256이 일치했다. 버전 사이에는 독립 실행의 시각·식별자가 다르므로 서로의 snapshot 해시가 같아야 한다는 기준을 적용하지 않는다.

## 관측한 콘솔 오류

| 관측 | 1.11.0 | 1.12.0 |
| --- | --- | --- |
| 기능 확인 | 12/12 | 12/12 |
| `stlite.invalid` 요청 실패 | 8 | 8 |
| 콘솔 오류 메시지 | 24 | 24 |
| 브라우저 page error | 0 | 0 |
| 모델 API 요청 | 0 | 0 |

모든 요청 실패·콘솔 메시지·CDP initiator 스택은 각 버전의 `result.json`에 보존했다. 실패한 요청의 호출자는 `checkSourceUrlResponse`다. [고정된 공식 CDN 코드 대조](../../stlite-runtime/component-paths.json)에서도 두 버전의 이 사전 검사 함수가 동일하며, 컴포넌트 본문 로드는 별도 `InnerIFrame → kernel.sendHttpRequest → iframe.srcDoc` 경로라는 점을 확인했다.

실제 iframe 관측값은 두 버전 모두 `src: null`, `srcdocLength: 6715`, 저장 코드 존재, 실행 함수 존재였다. 이 경로에서 저장 완료 응답과 revision 증가, 복원이 실제로 작동했다. 따라서 이 실행의 근거는 **기존 프레임워크의 외부 URL 사전 확인 실패가 로그에 남지만, 실제 인라인 컴포넌트 저장·복원은 작동한다**는 결론을 지지한다. 1.12에서 새로 생긴 저장 회귀라는 증거는 없으며, 콘솔이 깨끗하다는 주장도 하지 않는다. 오류를 숨기거나 fetch 결과를 성공으로 조작하지 않았다.

이 확인은 한 로컬 Chromium 환경과 동일 브라우저 프로필 안의 저장·복원 범위다. 공개 배포 전체 검증, 브라우저 간 데이터 공유, 실제 운영 데이터 내구성, 장시간 부하 검사를 대신하지 않는다.

## 증거

- [요약 결과](result.json), [종료 코드](exit-code.txt), [실행 로그](execution.log)
- [1.11 상세 결과](1.11.0/result.json), [1.12 상세 결과](1.12.0/result.json)
- [1.12 대기 1건](1.12.0/full-queue-count.png)
- [1.12 실제 광고 배정](1.12.0/full-queue-claimed.png)
- [1.12 새로고침 후 담당자·광고 복원](1.12.0/full-queue-resumed.png)
- [실행에 사용한 검사 스크립트](verify_storage.py)

## 재현

저장소 루트에서 Playwright 1.56.0 및 해당 Chromium을 설치한 Python으로 실행한다. CDN 다운로드를 허용할 네트워크와 한글 폰트가 필요하다. `--out`, `--build`는 새 임시 경로를 사용한다.

```bash
python docs/evidence/final-2026-10-21/stlite-upgrade/storage-compatibility/verify_storage.py \
  --repo . \
  --out /tmp/preflight-storage-comparison-new \
  --build /tmp/preflight-storage-build-new \
  --port 8635 \
  --versions 1.11.0 1.12.0
```

실행은 저장소의 HTML을 수정하지 않고 임시 빌드의 두 버전만 생성한다. 각 버전의 결과를 즉시 저장하며 최종 요약과 종료 코드도 파일로 남긴다. 처음 실행의 부분 실패 자료는 상위 `compatibility/` 폴더에 별도로 보존했다.
