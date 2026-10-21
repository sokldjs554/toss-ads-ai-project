# 새 실제 평가 기록의 화면 검증 — 2026-10-21

저장된 `features-20261021-run1`을 대상으로 native Streamlit과 실제 Pages/stlite 빌드를 데스크톱·모바일에서 확인했다. **185개 검사, 실패 0개**다. 실행 시간은 2026-10-21 06:59:08~07:00:27 UTC이며, 화면 소스는 `1671b294a8a7e93b5caa429bef60e2a87d739d0b`와 당시 작업 트리의 변경분이다. 각 파일 지문은 [결과 JSON](result.json)에 기록했다. 실제 모델 평가의 실행 소스 `3887041588690f7fb3259d66b60f25f92373a126`와 화면 검증 소스를 구분한다.

## 실제 확인한 값

검증은 [실제 평가 원본](../../../../evals/reports/live_features/features-20261021-run1.json)을 읽어 표의 각 칸과 비교했다. 응답을 만들거나 점수를 대체하지 않았다. 평가 원본의 SHA-256은 `7c7c1947986ab168d09b4913b951a8c1529b25c7c26f6d454a461b1901b75c7f`이다.

| 화면 항목 | 네 환경에서 확인한 표시 |
|---|---|
| 이미지 속 글자·검사 | 12/12 시도·응답 사례·모든 검사 통과 |
| 원래 대화 평가 | 20/20 시도, 19/20 실제 응답 사례, 17/20 모든 검사 통과 |
| 성과 보고서 문장화 | 12/12 시도·응답 사례·모든 검사 통과 |
| 캠페인 구성·문구 생성·검사 | 4/4 시도·응답 사례·모든 검사 통과 |
| 문구 비교 | 채택 16/28, 보류 12/28, 채택하며 작성자 선호와도 일치 13/28, 근거 검사 전 지표 18/28 |
| 문구 비교 결과별 수 | 채택 16, 순서에 따른 선택 변경 2, 인용·근거 검사 실패 7, 중요한 기준의 불일치 3 |
| 대화 후속 확인 | 1/1 시도·응답 사례·전 단계 확인. 원래 대화 20개 점수와 별도라는 안내 |
| 수정안 보조평가 | 4/4 시도·응답 사례·전 단계 확인. 원문 위반·모델 수정안·실제 의미 검사·같은 정책 재검사의 범위 안내 |
| 호출 장부 | 159회, 화면 반올림 비용 $0.5304, 사용량 미확인 0회. 원본 계산값은 $0.530376 |
| 출처 | 응답 모델 `gpt-5.4-mini-2026-03-17`, 실행 소스 `388704158869` |
| 대화 회귀와 원본 | 새 버전 66개 회귀 기록, 별도 원래 20개 평가의 표시 |
| 현재 성과 회귀 | 같은 80개 사례에서 변화 탐지 34/35, 알림의 정밀도 35/35, 정상 사례 알림 0/40, 원인 1순위 30/32 |
| 추가 성과 대조 | 작은 하락 5/12, 정상·추세·회복 대조 각각 0/12. 놓친 하락 7/12 안내 |
| 이전 성과 결과 | 별도 이전 실행 설명을 열어 33/35 기록이 유지되는지 확인 |

문구 비교의 채택은 사람 선호나 실제 광고 성과의 검증이 아니다. 새 프롬프트·근거 검사 계약이 적용됐으므로 이전 실행의 21/28과 단순 정확도 향상으로 비교하지 않는다. 실제 독립 사람의 제출은 0건이다.

## 실행 범위와 결과

| 실행 환경 | 크기 | 검사 수 | 실패 |
|---|---:|---:|---:|
| Native 데스크톱 | 1280×900 | 44 | 0 |
| Native 모바일 | 390×844 | 44 | 0 |
| Pages 데스크톱 | 1280×900 | 46 | 0 |
| Pages 모바일 | 390×844 | 46 | 0 |
| 원본 선택·번들·보고서 보존 | — | 5 | 0 |
| 합계 | — | 185 | 0 |

운영 화면의 검수 대기열·운영 지표/모니터·공동 검수 세 탭과 순서를 확인했다. Native는 공동 DB 설정을 제거해 미설정 안내를 확인했고, Pages는 `https://preflight-toss-ads.onrender.com/?view=운영`으로 가는 서버 링크를 확인했다. 둘 다 접근키 입력·DB 연결 문자열·환경변수 이름·내부 역할 제어 항목을 노출하지 않았다. 계정 기록과 실제 독립 참여자가 다르다는 안내는 유지됐다.

화면 전체의 가로 넘침과 Streamlit 예외는 없었다. 대표 캡처 8개를 직접 확인해 모바일 표의 줄바꿈, 정확한 분수, 대화 분모 구분, 서버 링크와 미설정 안내를 점검했다. 기존 브랜드 이모지의 대체 글리프는 이 테스트 환경에서도 보이며 새 기능의 통과 근거에 포함하지 않았다.

### 주요 화면

| 상태 | 데스크톱 | 모바일 |
|---|---|---|
| Native 공동 DB 미설정 | [화면](native-desktop-team-database-unconfigured.png) | [화면](native-mobile-team-database-unconfigured.png) |
| Pages 공동 검수 서버 링크 | [화면](pages-desktop-team-server-link.png) | [화면](pages-mobile-team-server-link.png) |
| 새 실제 실행의 원래 다섯 기능 | [화면](native-desktop-saved-live-evaluations.png) | [화면](pages-mobile-saved-live-evaluations.png) |
| 문구 비교 채택·보류 | [화면](pages-desktop-quality-accepted-and-abstained.png) | [화면](pages-mobile-quality-accepted-and-abstained.png) |
| 최소 연령 후속 확인과 별도 분모 | [화면](pages-desktop-age-followup-separate-denominator.png) | [화면](pages-mobile-age-followup-separate-denominator.png) |
| 수정안 보조평가 | [화면](pages-desktop-rewrite-supplement.png) | [화면](pages-mobile-rewrite-supplement.png) |
| 현재 성과 회귀 | [화면](pages-desktop-current-insight-evaluation.png) | [화면](pages-mobile-current-insight-evaluation.png) |
| 성과 대조와 이전 결과 구간 | [화면](pages-desktop-insight-contrasts-and-previous-result.png) | [화면](pages-mobile-insight-contrasts-and-previous-result.png) |

전체 32개 캡처의 파일명은 [결과 JSON](result.json)에 있다. 캡처는 당시 뷰포트의 보이는 구간이며 모바일에서는 한 표와 설명이 여러 화면 높이를 차지할 수 있다. 화면에 보이지 않는 뒷부분의 값은 DOM 검사 결과와 구분해 읽는다.

## 소스·도구·보존

- Python 3.11.17, Playwright 1.51.0, Chromium 134.0.6998.35, 기존 `ads-tools/fonts.conf`를 사용했다. Native Streamlit 1.66.0과 제품에 고정된 stlite 1.12.0을 유지했다.
- 실제 `scripts/build_site.py`가 만든 번들은 282개 파일, SHA-256 `7ba48cdd769aeab0ca12a451304a91fc7f7f8210645769817ca5f77109d1fd30`이다. [빌드 로그](pages-build.log).
- 공동 검수 UI·명령·저장 어댑터, 새 평가 UI, 대화 회귀 2개, 성과 결과 3개, 최신 실호출 결과 등 11개 필수 파일이 번들에도 원문 바이트로 포함됐는지 검사했다. 비밀 설정 파일은 포함되지 않았다.
- 검증 전후 기존 실호출 보고서와 예약 장부 6개의 지문이 같았다. frozen 입력·원본 보고서·이전 갤러리를 수정하지 않았다.
- [실행 당시 QA 코드](probe-source.py)의 SHA-256은 `a4629f20bf579eb218021dcf324ee42f2b745f463e17e789be0b266f90707b89`이다.

## 앞선 실패와 관찰한 한계

첫 실행은 [141개 중 선택자 실패 4개](../live-run1-native-pages/result.json)로 보존했다. 품질·대화 후속 확인·수정안·호출 장부의 값은 이미 일치했지만, 성과 표의 `columnheader` 역할 선택자가 각 환경에서 표를 찾지 못해 중단됐다. 당시 보이는 텍스트와 [원래 QA 코드](../live-run1-native-pages/probe-source.py)를 남겼다. 제품 코드는 바꾸지 않고 실제 `th` 요소를 선택하도록 QA 한 줄을 수정한 뒤, 이 새 경로에서 전부 재검사했다. 첫 결과를 성공으로 바꾸거나 캡처를 덮어쓰지 않았다.

Native의 페이지 예외·콘솔·네트워크 오류는 모두 0개였다. Pages는 각 크기에서 `stlite.invalid/.../component/...` 요청 실패 2개와 관련 콘솔 오류 6개를 남겼으며 페이지 예외는 0개였다. 해당 프레임워크 진단 로그는 삭제하지 않았다. 이번 검사는 공동 검수 안내와 저장된 평가의 표시를 확인한 것으로, 기존 개인 검수의 실제 저장 성공·실패를 이 로그만으로 판단하지 않는다.

모든 환경에서 모델 제공자 요청 시도는 0개였다. Native는 예시 모드이고 모델 키·공동 DB 설정을 전달하지 않았다. Pages의 모델 제공자 요청은 차단하고 시도를 기록했다. 실제 DB 접속, 로그인 후 역할별 변경, 공개 배포 주소의 상태는 이번 범위 밖이다.

인증서를 대체하는 테스트 프록시 때문에 `--ignore-https-errors`를 QA 브라우저에만 적용했다. 제품 HTML이나 Chromium 기능 플래그는 바꾸지 않았다. 이 Chromium 134의 `WebAssembly.Suspending`·`WebAssembly.promising`은 `undefined`였다. 이번 화면 검증을 기존 Chromium 141의 JSPI 대기·장시간 전환 검증으로 대신 읽지 않는다.

## 재현

저장소 루트에서 비어 있는 새 증거 경로를 사용한다. 공개 CDN과 localhost 통신이 가능한 환경이 필요하다.

```bash
env FONTCONFIG_FILE=/workspace/scratch/30d72ff77b94/ads-tools/fonts.conf \
  PLAYWRIGHT_BROWSERS_PATH=/workspace/scratch/30d72ff77b94/ads-tools/browsers \
  PYTHONUNBUFFERED=1 .venv311/bin/python scripts/verify_remaining_ui.py \
  --out /tmp/preflight-live-run1-new-evidence \
  --expected-run-id features-20261021-run1 \
  --require-remaining-evaluation
```

일반 환경에서는 TLS 검증을 유지한다. 이번처럼 인증서를 대체하는 검증 프록시가 있는 환경에서만 `--ignore-https-errors`를 명시한다.
