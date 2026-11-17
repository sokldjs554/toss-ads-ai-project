# 2026-11-17 재검증: 성공·실패·미검증 범위

조사 SHA `b5793cc83dfbb5272bd3245a6058b8985dc49865`, 실행 주체 Codex. Linux·Python 3.12.14의 별도 가상환경에 `requirements-dev.txt`를 설치했다. [설치 버전](preflight-versions.json). 기본 런타임 패키지를 공유한 환경이며 과거 CI와 같은 완전 고정 환경은 아니다.

## 전체 실행

```bash
python -m pytest -o addopts='' -q -ra
```

**4,425 통과·2 실패·68 건너뜀·10 오류, 555.99초. 전체 통과가 아니다.** [원본 로그](preflight-pytest.log).

| 분류 | 원인·관찰 | 이번 처리 |
|---|---|---|
| 브라우저 1실패·10오류 | Playwright 1.51.0이 요구하는 `chromium_headless_shell-1161` 미설치 | 현재 브라우저 저장/재시작 검증 미완료. 기존 CI 성공으로 이번 실패를 덮지 않음 |
| 68개 건너뜀 | 격리 PostgreSQL DSN 미설정 | 실제 운영 DB를 테스트 대상으로 연결하지 않음. 해당 통합 검증은 미완료 |
| 대화 UI 1실패 | `test_long_input_is_cut_and_reported`가 기대한 잘림 경고 문구 없음 | 같은 테스트의 재실행에서도 실패. 아래 별도 진단을 보존 |

별도 QuarterGuard 브라우저 검사에서도 시스템 Chromium이 `socket() ... Operation not permitted`로 시작하지 못했다. 현재 환경에서 브라우저 동작이 검증됐다고 주장하지 않는다.

## 핵심 로직 부분 검사와 재실행

```bash
python -m pytest -o addopts='' -q tests/test_live_pilot_engine.py tests/test_live_pilot_gateway.py tests/test_grounding.py tests/test_injection.py tests/test_output_guard.py
```

첫 [부분 실행](preflight-focused-tests.log)은 **161 통과·1 실패**였다. 100,000자 반복 입력의 3초 제한 검사에서 3.494초가 측정됐다. 다른 검사와 함께 실행되던 시점이며, 이것만으로 원인을 CPU 경쟁으로 확정하지 않았다.

전체 실행 종료 후 위 부분 검사와 `tests/test_conv_ui.py::test_long_input_is_cut_and_reported`를 함께 다시 실행했다. [재실행](preflight-focused-serial.log)은 **162 통과·1 실패, 18.52초**였다. 시간 제한 검사는 재통과했고 대화 UI 문구 검사는 계속 실패했다. 새로운 성능 보장이나 전체 회귀 통과로 확대하지 않는다.

## 대화 UI 진단

[진단 출력](preflight-input-diagnostic.log): Streamlit **1.67.0**, 대화 로직에 남은 사용자 입력 **1,000자**, `truncated=False`, 추가 경고 없음, 기본 1,000자 제한 안내는 표시, 예산 **2,000,000원** 유지.

화면은 `st.chat_input(max_chars=1000)`을 사용하고 대화 로직은 전달받은 원문이 1,000자를 초과할 때만 별도 잘림 경고를 만든다. 현재 AppTest 경로에서는 이미 1,000자로 제한된 입력이 전달되므로 테스트가 요구하는 별도 경고가 발생하지 않는다. 입력 뒤쪽의 999만원이 예산에 반영되는 문제는 관찰되지 않았다. 과거 배포 버전의 동일한 동작이나 실제 브라우저 입력을 재확인한 것은 아니다.

제품 코드나 테스트 기대값은 수정하지 않았다. 아래에서 이전 성공 환경과 대조한 의존성 고정 결과를 추가한다. 최초 실패 로그는 그대로 보존했다.

## 재현 환경 수정과 재검사

새 PR의 [첫 CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/43922762974)는 `ubuntu-latest`가 Ubuntu 26.04로 바뀌어 `Playwright does not support chromium on ubuntu26.04-x64`로 브라우저 설치 단계에서 중단됐다. 해당 실행에서 PostgreSQL restricted API와 LIVE isolated storage gate는 통과했지만 전체 테스트는 시작되지 않았다.

이전 성공 [CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40076009819)의 작업 로그는 Ubuntu 24.04, Streamlit 1.66.1, Playwright 1.51.0을 기록한다. 이번 보완은 `.github/workflows/ci.yml`의 실행 OS를 `ubuntu-24.04`, `requirements.txt`의 Streamlit을 `1.66.1`로 고정했다. 모델·정책·제품 로직·테스트 기대값을 변경하지 않았다. 이 고정은 새 환경으로의 자동 이동을 막으며, 다음 버전 갱신 때 동일 UI/브라우저 검사를 다시 실행해야 한다.

Streamlit 1.66.1로 재설치한 뒤 대화 UI 전체 **25개 통과, 10.35초**([로그](preflight-pinned-ui.log)). 핵심 로직 162개와 대화 UI 25개를 함께 실행해 **187개 통과, 25.08초**([로그](preflight-pinned-focused.log)). 새 코드 테스트가 아닌 기존 검사 그대로다.

```bash
python -m pytest -o addopts='' -q tests/test_live_pilot_engine.py tests/test_live_pilot_gateway.py tests/test_grounding.py tests/test_injection.py tests/test_output_guard.py tests/test_conv_ui.py
```

환경 고정 커밋 `25e29f00beb59a891e31b5ec634cdd583e7d048e`의 [전체 CI 재실행](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/43923706177)은 이 문서 작성 시 실행 중이었다. 전체 CI 성공 여부는 해당 실행의 최종 상태와 구분한다. 187개 부분 통과를 전체 통과로 바꾸지 않는다.

## 배포·LIVE 상태

- 무료 [Pages](https://sokldjs554.github.io/toss-ads-ai-project/app/) HTTP 200. 모델 단계는 대역이다.
- Render `/_stcore/health`는 25초 내 응답 없음. 현재 LIVE 로그인·저장·새 모델 호출은 검증하지 못했다. [HTTP 원본](public-http.json).
- 조사 SHA의 과거 [CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40076009819)와 [Pages 배포](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40076009803)는 GitHub 조회에서 성공 상태였다. 이번 로컬 전체 검사와 분리한다.
- 실제 모델·운영 DB·예약 한도를 변경하지 않았으며 추가 유료 호출 0회다. 6요청·10모델 호출의 사용자 기록은 기존 시점의 근거로 유지한다.

## 변경 범위

README의 검토 안내 링크, 개발 의존성·격리 DB·Chromium 실행 안내, [IT AI 검토 문서](../../it-ai-review-guide.md), 이 검증 기록을 추가/수정했다. 재현 문제를 해결하기 위해 CI OS와 Streamlit 버전을 고정했다. 제품 코드·정책·모델·기존 평가 자료는 동일하다. [증거 파일 해시](sha256.json).
