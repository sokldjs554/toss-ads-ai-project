# 최종 로컬 브라우저 확인 — 2026-10-21

이 폴더는 **로컬 Streamlit을 Chromium으로 확인한 기록**이다. 공개 GitHub Pages의 배포 확인과 구분한다. 모든 실행은 `PREFLIGHT_FORCE_DEMO=1`로 시작했고 API 키를 서버 환경에서 제외했다. 기록을 열람하고 예시를 실행하는 데 유료 API를 호출하지 않았다.

## 전체 화면 흐름

- [e2e_report.json](e2e_report.json): 총 122개 검사, 실패 0개. 데스크톱 1280×900에서 61개, 모바일 390×844에서 61개.
- [e2e.log](e2e.log): 실행 중 출력된 검사 목록. 프로세스 종료 코드 0.
- Python 3.11.17, Streamlit 1.66.0, Playwright 1.51.0, Chromium 134.0.6998.35.
- 실행 코드는 `80e88076dd829d1def8034b442363bb24331e720`의 수정본이다. 실행 중 두 번째 API 보고서가 저장된 커밋까지 체크아웃이 전진했으며, 보고서의 종료 시점 커밋은 `3e3f830`이다. 이 사이 애플리케이션 소스 변경은 없었다. 첫 번째·두 번째 API 보고서 화면이 실행 중 달라질 수 있으므로, 최신 보고서의 표시 값은 아래 별도 검사로 확인했다.

## 두 번째 실제 API 보고서 표시

- [live-records-browser.json](live-records-browser.json): 데스크톱·모바일 각 18개, 총 36개 검사. 실패 0개, 프로세스 종료 코드 0.
- [live-records-browser.log](live-records-browser.log): 각 검사 결과.
- `features-20261020-run2`가 먼저 펼쳐지고 이전 기록은 접힌 채 남는지 확인했다.
- 5개 기능의 시도/계획, 실제 응답 사례, 전체 검사 통과 수를 저장된 JSON과 직접 대조했다. 대화 17/20, 성과 보고서 12/12, 문구 비교 21/28 등 서로 다른 값을 구분해 표시한다.
- 호출 148회, 표시 비용 `$0.3204`, 실제 응답 모델 `gpt-5.4-mini-2026-03-17`, 실행 소스 `80e88076dd82`를 확인했다. 비용은 토큰과 공개 단가로 계산한 값이며 실제 청구 금액이 아니라는 설명도 확인했다.
- 예외 표시·브라우저 오류·페이지 가로 넘침이 없었다. 이 열람에서는 모델 제공자 요청이 없었다.
- 기록 JSON에는 원본 보고서·화면 코드·폰트 파일의 SHA-256을 함께 저장했다.

| 화면 | 캡처 |
|---|---|
| 데스크톱 결과·비용·한계 | [live-records-desktop.png](live-records-desktop.png) |
| 모바일 결과 표 | [live-records-mobile.png](live-records-mobile.png) |
| 모바일 비용·모델·한계 | [live-records-mobile-cost.png](live-records-mobile-cost.png) |

## 환경 복구 기록

[environment-first-attempt](environment-first-attempt/)에는 첫 실행의 검사 기록과 대표 캡처를 남겼다. DOM 검사는 122개 통과했지만, 컨테이너 복구 후 한글 폰트가 없어 캡처에 글자가 표시되지 않았다. 해당 캡처를 최종 화면 증거로 사용하지 않았다. 작업 디렉터리에 Noto Sans CJK KR을 설치하고 `FONTCONFIG_FILE`로 지정한 뒤 전체 122개 검사를 다시 실행했다. 애플리케이션 코드는 변경하지 않았다.

재현 명령은 `scripts/e2e_flow.py --out <새 경로>`이며 프로젝트의 `requirements-dev.txt`와 Chromium, 한글을 지원하는 시스템 폰트가 필요하다. 같은 결과 폴더를 덮어쓰지 않고 실행별로 보존한다.
