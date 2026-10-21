# 브라우저 실행 중단 재현 및 런타임 교체

측정일: 2026-10-21 UTC. 대상은 GitHub Pages의 브라우저 실행판입니다. 모델 API는 호출하지 않았습니다.

## 확인한 결함과 변경

`@stlite/browser@1.11.0`은 Pyodide `0.29.3`을 내려받았습니다. 같은 앱 ZIP을 JSPI 기능이 기본으로 켜진 Chromium 141에서 열어 두자, 앱 준비 완료 **144.44초 후** `Pyodide has suffered a fatal error` 및 `RuntimeError: table index is out of bounds`가 발생하고 페이지가 중단됐습니다. 브라우저가 중단되어 실패 시점 캡처는 만들지 못했고, 그 오류도 원본 JSON에 남겼습니다.

공식 배포된 `@stlite/browser@1.12.0`은 Pyodide `314.0.3`을 사용합니다. 동일 앱 ZIP·동일 Chromium 141·동일 기본 JSPI 설정에서 **360.33초 대기 후 예시 실행이 완료**됐습니다. 뒤이어 실행한 화면 전환 검사는 아래에 별도 구분했습니다. 배포 HTML의 JS와 CSS 버전을 함께 `1.12.0`으로 고정합니다. JSPI나 브라우저 보안 기능을 끄지 않았습니다.

HTML 변경 후 [최종 정적 빌드 확인](final-build.json)도 완료했습니다. 생성된 HTML의 두 런타임 고정값과 쉬운 부팅 안내, 재생 페이지 연결, run1·run2·run3 저장 보고서 포함을 확인했습니다. 이 빌드 확인을 새로운 브라우저 실행 측정으로 집계하지 않습니다.

공식 stlite 유지보수자의 [동일 계열 문제 분석](https://github.com/whitphx/stlite/issues/1493#issuecomment-6262498320)은 Pyodide 0.29.3과 314.0.3의 차이 및 [Pyodide 수정 #6260](https://github.com/pyodide/pyodide/pull/6260)을 설명합니다. 프로젝트에서 관찰한 실패는 이 보고와 일치합니다. 이 프로젝트의 관측만으로 WASM 내부 원인을 별도로 증명한 것은 아닙니다. 실제 내려받은 런타임 URL과 공식 CDN 파일 해시는 [upstream-versions.json](upstream-versions.json)에 있습니다.

## 원본 기록과 범위

| 기록 | 조건 | 결과 |
| --- | --- | --- |
| [기존 Chromium 134](../stlite-idle/result.json) | stlite 1.11.0, 360.14초 대기 후 실행 | 완료. 별도 동일 브라우저 검사에서 JSPI API가 없었음. 이 결과만으로 Chromium 141의 안정성을 판단하지 않음 |
| [Chromium 141 / 1.11.0](1.11.0/result.json) | 초기 준비 후 화면에 입력하지 않고 대기 | 준비 완료 144.44초 뒤 fatal 오류·페이지 중단 |
| [Chromium 141 / 1.12.0 첫 검사](1.12.0/result.json) | 같은 ZIP, 360.33초 대기 후 실행 | 대기·예시 실행 완료. 이후 검사 코드가 화면 전환을 잘못된 `button` 역할로 찾아 시간 초과. 원본 전체 상태는 `failed`로 보존 |
| [첫 화면 전환 기록](navigation/1.12.0/result.json) | 1.12.0, 올바른 화면 선택 | 14/60회 기록만 있고 완료 결과 없음. 통과 집계에 포함하지 않음 |
| [새 60회 화면 전환 기록](navigation-restart/1.12.0/result.json) | 1.12.0, Chromium 141, 5개 화면을 7초 간격으로 전환 | 60/60회 완료. 페이지·fatal 오류 0, 모델 요청 0. supervisor 종료 코드 0 |
| [저장·복원 비교](../stlite-upgrade/storage-compatibility/result.json) | Chromium 141, 1.11.0과 1.12.0 각각 실제 저장·배정·새로고침 복원 | 버전마다 12/12 기능 검사 완료 |

새 60회 검사는 **별도 브라우저 세션의 화면 전환 검사**입니다. 같은 세션에서 6분 대기를 다시 수행한 것으로 합치지 않습니다. 각 화면으로 전환한 후 해당 화면의 제목·탭·버튼을 확인하고 Streamlit 예외와 런타임 fatal 오류를 검사했습니다. 화면 요소의 실제 역할은 `radio`입니다.

## 검사한 소스와 앱 묶음

| 관측 | Git 빌드 표시 및 소스 | ZIP SHA-256 | 포함된 저장 평가 결과 |
| --- | --- | --- | --- |
| 최초 idle 및 1.11/1.12 직접 비교 | `80e88076dd829d1def8034b442363bb24331e720` | `9990d0fe90bfe52c0214d48b756699d072bbc9aa41fdba8d6208dcffb2bb857d` | run1 |
| 새 60회 화면 전환 | `000bc89ed096f8003d11f40a5c5c6f70e422955c` | `27a310c3ce36252f87e639f2f77e7f0d26d084a2233c37f8660338c8b7a6b7a5` | run1·run2 |
| 저장·복원 비교, 두 버전 공통 | `d9633d6efcf29ea99cdfb8a4eee2990400105244` | `d89e45ce3ede57323243010183dcb9cc4e24eaa1d028162957294f0ff711ddb9` | 해당 별도 검사 JSON 참조 |

처음 두 런타임을 비교할 때는 **ZIP이 완전히 같았습니다**. 이후 모델 처리 코드와 저장 보고서가 갱신되어 60회 전환 검사의 ZIP은 다릅니다. 실제 ZIP의 `build_info.txt`와 평가 관련 파일 해시를 [archive-provenance.json](archive-provenance.json)에 보관했습니다. 새 전환 검사에 run3 평가 결과가 포함됐다고 주장하지 않습니다. ZIP의 파일 시간 정보 때문에 다시 빌드한 ZIP 해시가 달라질 수 있으므로 위 값은 당시 관측한 바이트를 식별합니다.

## 컴포넌트 진단 오류와 실제 저장 동작

화면 전환 시 `https://stlite.invalid/.../component/ui.review_queue_storage...` 요청의 CORS 오류가 관찰됐습니다. 새 60회 전환 검사에서 해당 요청 실패 24건과 이에 따른 콘솔 오류 72건이 기록됐습니다. 다른 네트워크 실패 또는 콘솔 오류 유형은 없었습니다.

이를 숨기거나 모든 콘솔 오류가 없다고 처리하지 않았습니다. 공식 1.11/1.12 파일의 `checkSourceUrlResponse` 함수가 동일하고, 해당 함수가 가상 URL을 일반 브라우저 `fetch`로 검사하면서 오류를 남기는 것을 [component-paths.json](component-paths.json)에 기록했습니다. 실제 컴포넌트 HTML은 별도로 `kernel.sendHttpRequest`를 거쳐 `iframe.srcDoc`에 들어갑니다.

[두 버전 저장 비교](../stlite-upgrade/storage-compatibility/result.json)에서 다음을 직접 확인했습니다.

- 버전마다 같은 진단 요청 실패 8건과 콘솔 오류 24건을 보존했습니다. CDP 호출 스택은 모두 `checkSourceUrlResponse`를 가리킵니다.
- 합성 광고 1건의 저장 완료 응답 및 IndexedDB revision 1을 확인했습니다.
- 본 앱에서 대기 광고 1건을 확인하고, 실제 라벨 화면에서 광고를 받아 내용을 확인했습니다. 담당자 배정이 revision 2에 저장됐습니다.
- 본 앱을 새로 열고 같은 담당자 이름을 입력했을 때 같은 광고가 복원됐습니다. 저장 내용의 SHA-256이 유지됐습니다.
- 두 버전 모두 12개 기능 확인을 통과했고 페이지·fatal 오류 및 모델 API 요청이 없었습니다.

따라서 관찰된 진단 요청 실패를 저장 실패나 1.12의 기능 회귀로 해석할 근거는 없습니다. 이 진단 로그 자체는 남아 있습니다. 실제 저장 성공 여부는 완료 응답과 복원 결과로 확인합니다.

## 재현 방법

프로젝트 개발 환경의 Playwright 1.51 / Chromium 134로는 이 JSPI 조건을 재현할 수 없습니다. 아래는 **별도 가상 환경**에 Playwright **1.56.0 / Chromium 141.0.7390.37**을 설치하는 절차입니다. 프로젝트 의존성 파일은 바꾸지 않습니다. 먼저 저장소 루트로 이동합니다. Python과 Chromium이 요구하는 운영체제 라이브러리는 설치되어 있어야 합니다.

```bash
python3 -m venv /tmp/preflight-runtime-pw
/tmp/preflight-runtime-pw/bin/python -m pip install 'playwright==1.56.0'
export PLAYWRIGHT_BROWSERS_PATH=/tmp/preflight-runtime-browsers
/tmp/preflight-runtime-pw/bin/python -m playwright install chromium
export PREFLIGHT_RUNTIME_OUT="$(mktemp -d /tmp/preflight-runtime-result.XXXXXX)"
/tmp/preflight-runtime-pw/bin/python scripts/verify_stlite_runtime.py
```

[검사 스크립트](../../../../scripts/verify_stlite_runtime.py)는 현재 소스를 한 ZIP으로 묶고 두 stlite 버전에 같은 ZIP을 제공합니다. 두 브라우저는 동시에 시작합니다. 기본으로 각각 360초 대기, 예시 실행, 7초 간격 60회 화면 전환을 검사합니다. 임의 기능 해제 플래그는 사용하지 않습니다. OpenAI·Anthropic 모델 API 주소를 차단하고 시도된 요청을 결과에 기록합니다. 실제 모델 호출은 이 검사의 대상이 아닙니다.

기존 결과를 덮어쓰지 않도록 새 출력 디렉터리가 필요합니다. 프로세스의 종료 코드만으로 두 런타임이 모두 통과했다고 판단하지 말고 각 `result.json`의 `status`, `checks`, `console_errors`, `page_errors`, `network_errors`, 실제 브라우저 버전과 내려받은 Pyodide URL을 확인합니다. 과거 런타임의 실패를 비교 증거로 남기는 스크립트입니다.

이미 확보한 대기 증거와 별도로 1.12 화면 전환만 다시 검사하려면 다음과 같이 범위를 명시합니다.

```bash
export PREFLIGHT_RUNTIME_OUT="$(mktemp -d /tmp/preflight-runtime-navigation.XXXXXX)"
export PREFLIGHT_RUNTIME_VERSIONS=1.12.0
export PREFLIGHT_RUNTIME_IDLE_SECONDS=0
/tmp/preflight-runtime-pw/bin/python scripts/verify_stlite_runtime.py
```

이 기록의 환경에서는 TLS를 대체하는 테스트 프록시 때문에 `PREFLIGHT_TEST_IGNORE_HTTPS_ERRORS=1`을 사용했고 각 결과에 표시했습니다. 일반 실행의 기본값은 TLS 인증서 검증입니다. 이 옵션은 배포 HTML에 적용되지 않습니다. 한국어 캡처에는 환경에 설치한 한국어 글꼴을 사용했습니다.

## 해석의 한계

이 검증은 관찰된 Chromium 141의 대기 중단 결함과 해당 앱의 화면 전환·저장 복원을 다룹니다. 모든 브라우저 또는 장시간 연속 운영을 보장하는 측정은 아닙니다. 저장소의 모델 평가 수치는 별도 실제 API 실행 보고서를 따라야 합니다. 브라우저 예시 실행의 성공은 모델 품질 점수가 아닙니다.
