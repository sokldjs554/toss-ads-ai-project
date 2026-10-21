# 공동 검수·새 평가 UI 브라우저 QA — 2026-10-21

**새 실제 평가 기록 추가 검증:** `features-20261021-run1` 저장 후 native·Pages × 데스크톱·모바일에서 정확한 표 값과 보조평가의 별도 분모를 확인했다. **185개 검사, 실패 0개**와 새 캡처 32개는 [별도 최신 증거](live-run1-native-pages-recheck/README.md)에 있다. 아래 122개·79개 결과와 당시의 최신 기록 `features-20261020-run3`는 이전 검증 기록으로 그대로 보존한다.

## 결과

제품 코드 수정 없이 기존 E2E와 추가 화면 검증을 완료했다. 실행 대상은 `ba27f6d`와 당시 작업 트리의 변경분이다. 기존 평가 파일·manifest와 이전 갤러리는 수정하지 않았다.

| 검증 | 실제 실행 범위 | 결과 |
|---|---|---|
| 기존 `scripts/e2e_flow.py` | 데스크톱 1280×900, 모바일 390×844의 예시 실행·수정·재검사·실패/재시도·대화·상품 페이지·배너·이미지·성과·운영·평가 | **122개 검사, 실패 0개**. 화면별 61개. |
| 새 `scripts/verify_remaining_ui.py` | 같은 두 크기의 native 실행과 실제 Pages/stlite 빌드 | **79개 검사, 실패 0개**. 번들 확인과 원본 보고서 보존 검사 포함. |

- [기존 E2E 결과](native-existing-e2e/e2e_report.json)
- [추가 검증 결과·소스/번들 지문](shared-and-evals-verified/result.json)
- [Pages 빌드 로그](shared-and-evals-verified/pages-build.log)

Python 3.11.17, Playwright 1.51.0, Chromium 134.0.6998.35와 기존 `ads-tools/fonts.conf`를 재사용했다. Native Streamlit은 1.66.0이다. Pages는 제품 HTML에 고정된 stlite 1.12.0을 그대로 사용했다. 생성된 `src.zip`은 279개 파일이며 SHA-256은 `0266abc6e16cf7bc5ef41dcf1a2745149dff134623734d229de3bd207270d57c`이다.

## 확인한 화면

운영 화면에서 **검수 대기열 / 운영 지표·모니터 / 공동 검수**가 그 순서로 나타난다. Native에서 공동 DB 환경변수를 제외하면 연결 준비 안내가 나타나며 키 입력은 표시하지 않는다. Pages의 공동 검수 탭은 `https://preflight-toss-ads.onrender.com/?view=운영`으로 가는 링크를 제공한다.

공동 검수 화면에 원문 접근키, DB 연결 문자열, 환경변수 이름, 해시/요청 ID/내부 계정 ID 등이 표시되지 않는지 확인했다. 계정 기록과 실제 독립 사람 검수가 다르다는 안내는 보인다. 모바일에서 탭·본문·링크와 안내의 가로 넘침이 없고 한국어 글자가 표시되는 것을 캡처로도 확인했다. 상단 브랜드의 기존 체크 이모지는 이 테스트 환경에서 대체 글리프로 표시되며, 새 UI 기능의 판정에 포함하지 않았다.

새 평가 화면은 당시 최신 저장 기록인 `features-20261020-run3`를 읽고 대화의 `20/20` 시도, `19/20` 실제 응답 사례, `17/20` 모든 검사 통과를 원본 보고서와 동일하게 표시했다. 20261021 버전의 66개 대화 회귀 보고서와 별도 20개 대화의 첫 평가 기록이 함께 표시되는 것도 확인했다. 두 종류의 평가 수치를 합치지 않았다.

Pages 빌드의 `APP_INCLUDE`가 `ui`, `ops`, `evals`를 포함하므로 별도의 파일 목록 변경이 필요하지 않았다. 새 공동 검수 UI·서버 명령·저장 어댑터·새 평가 UI·새 대화 보고서 7개가 번들 안에서도 원문 바이트와 같은지 해시로 확인했다. `.env` 및 Streamlit 비밀 설정 파일은 포함되지 않았다.

### 주요 스크린샷

| 상태 | 데스크톱 | 모바일 |
|---|---|---|
| Native 공동 DB 미설정 | [화면](shared-and-evals-verified/native-desktop-team-database-unconfigured.png) | [화면](shared-and-evals-verified/native-mobile-team-database-unconfigured.png) |
| Pages 공동 검수 서버 링크 | [화면](shared-and-evals-verified/pages-desktop-team-server-link.png) | [화면](shared-and-evals-verified/pages-mobile-team-server-link.png) |
| Pages 저장된 실호출 평가 | [화면](shared-and-evals-verified/pages-desktop-saved-live-evaluations.png) | [화면](shared-and-evals-verified/pages-mobile-saved-live-evaluations.png) |
| Pages 대화 회귀·원본 기록 | [화면](shared-and-evals-verified/pages-desktop-conversation-regression-and-original.png) | [화면](shared-and-evals-verified/pages-mobile-conversation-regression-and-original.png) |

## 실패한 준비·선택자 시도 보존

1. [최초 native 준비 실패](native-sandbox-socket-blocked.json): 실행 샌드박스가 localhost socket 생성도 `EPERM`으로 차단했다. 화면 검사는 시작하지 못했다. 로컬 통신이 허용되는 실행으로 기존 E2E를 완료했다.
2. [추가 검사 첫 실행](shared-and-evals-chromium134/result.json): 화면은 표시됐지만 QA 코드의 `dialog.locator("summary")`가 대화 설명 안의 중첩 설명 제목까지 두 개 선택했다. 엄격한 선택자 검사가 네 환경에서 실패하여 **71개 중 4개 실패**로 그대로 남겼다. [당시 QA 코드](shared-and-evals-chromium134/probe-source.py)와 중단 시점 화면도 보존했다. 제품 코드를 바꾸지 않고 직접 자식 `:scope > summary`를 선택하도록 수정한 뒤 새 경로에서 79개 검사를 완료했다.

## 관찰한 경고와 범위 제한

- Native 두 화면 크기 모두 브라우저 페이지 예외·콘솔 오류·네트워크 오류가 0개였다.
- Pages 두 화면 크기 모두 브라우저 페이지 예외는 0개였다. 각 환경에서 `stlite.invalid/.../component/...` 진단 요청 실패 2개와 관련 콘솔 오류 6개가 관찰되어 결과 JSON에 보존했다. 요청한 공동 검수·평가 화면의 표시와 전환은 완료됐다. 이번 검증은 기존 로컬 검수 저장 기능의 재검증이 아니므로 이 로그로 저장 성공이나 실패를 단정하지 않는다.
- 모든 환경에서 모델 제공자 요청 시도는 0개였다. Native는 예시 모드로 실행하며 모델 키와 공동 DB 설정을 전달하지 않았다. Pages에서는 모델 제공자 요청 경로를 차단하고 시도를 기록했다.
- 테스트 프록시의 TLS 대체 환경 때문에 `--ignore-https-errors`를 검증 브라우저에만 사용했고 결과에 명시했다. 제품 HTML이나 Chromium 기능 플래그는 바꾸지 않았다.
- 이 Chromium 134 환경에서 `WebAssembly.Suspending`과 `WebAssembly.promising`은 `undefined`로 관찰됐다. 이번 화면 QA는 이전 Chromium 141의 JSPI 대기·장시간 전환 검증을 대체하지 않는다.
- 당시 저장된 최신 실호출 보고서에는 `quality_audit_summary`와 rewrite 추가 검사 결과가 없었다. 해당 코드가 번들에 포함되는 것은 확인했지만, 새로운 채택·보류/수정 추가 검사 카드의 실제 데이터 화면은 확인하지 않았다. 결과 JSON에 두 availability를 `false`로 기록했다. 실제 보고서에 해당 필드가 생기면 같은 스크립트가 카드의 분리 표시도 검사한다.
- 실제 DB 접속·로그인 후 역할별 작업·동시 저장·배포 상태는 이 실행의 범위가 아니다. 별도 서버 계약/AppTest와 배포 후 DB 검증을 구분해야 한다.

## 재현

저장소 루트에서 비어 있는 새 증거 경로를 지정한다. 실행 환경이 로컬 socket 또는 공개 CDN 연결을 제한한다면 승인된 네트워크 실행 환경이 필요하다.

```bash
env -u PREFLIGHT_TEAM_DATABASE_URL \
  FONTCONFIG_FILE=/workspace/scratch/30d72ff77b94/ads-tools/fonts.conf \
  PLAYWRIGHT_BROWSERS_PATH=/workspace/scratch/30d72ff77b94/ads-tools/browsers \
  .venv311/bin/python scripts/e2e_flow.py --out /tmp/preflight-native-new-evidence

env FONTCONFIG_FILE=/workspace/scratch/30d72ff77b94/ads-tools/fonts.conf \
  PLAYWRIGHT_BROWSERS_PATH=/workspace/scratch/30d72ff77b94/ads-tools/browsers \
  .venv311/bin/python scripts/verify_remaining_ui.py \
  --out /tmp/preflight-shared-ui-new-evidence
```

일반 환경에서는 TLS 검증을 유지한다. 인증서를 대체하는 테스트 프록시가 있는 환경에 한해서 `--ignore-https-errors`를 명시한다. 별도 QA용 Python을 사용한다면 `--app-python /실제/저장소/.venv311/bin/python`으로 기존 앱 환경을 지정할 수 있다.
