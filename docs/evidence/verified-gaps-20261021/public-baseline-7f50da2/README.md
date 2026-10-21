# 수정 전 공개 배포 확인 — 7f50da2

- 공개 소스: `7f50da2d634f5f7d31bd321354e72db0252f0dc2`
- 검사 도구 소스: `2fcd81cee57253915cbbc0abf2a5aefa60dc3659`
- [GitHub Actions 39578788768](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39578788768), 2026-10-21 09:41:06–09:42:29 UTC
- Chromium `141.0.7390.37`, Playwright `1.56.0`, `ignore_https_errors=false`
- 실제 Render·GitHub Pages의 desktop 1280×900 / mobile 390×844를 관측했다. 운영 DB 비밀값·모델 키를 주입하지 않았고, 접근키 입력·공동 검수 쓰기·새 모델 호출을 하지 않았다.

`result.json`의 명시적 assertion은 **194개 통과, 실패 0개**다. 빌드 표식·공개 ZIP 원본 지문·저장된 평가의 실제 수치·후속 평가의 별도 분모·공동 DB 미설정 안내와 화면 배치를 확인했다. 이 결과를 앱의 모든 기능이 정상이라는 뜻으로 확대하지 않는다.

## 로그에서 추가로 발견한 검사 공백

Pages desktop/mobile 각각에서 개인 검수 저장 컴포넌트의 `stlite.invalid/.../component/...` 요청이 DNS 오류로 실패했고 console에 custom component fetch 오류가 남았다. 이 실행은 개인 검수 대기열의 복원·저장 완료를 assertion으로 확인하지 않았으므로 해당 오류가 위 194개 검사 실패에 포함되지 않았다. Root는 이 원본을 보존하고 별도 재현·수정 대상으로 지정했다. Native desktop의 컴포넌트 요청 중단 `ERR_ABORTED`도 원본에 그대로 남아 있다.

후속 소스·기존 증거 대조에서 이 오류는 [stlite 1.11/1.12 저장 호환성 확인](../../final-2026-10-21/stlite-upgrade/storage-compatibility/README.md)에 보존된 비차단 사전 URL 검사와 일치했다. 당시 두 버전 모두 이 로그와 함께 실제 저장·배정·새로고침 복원 12개 검사를 통과했고, 본문은 `kernel.sendHttpRequest`와 `iframe.srcDoc`의 별도 경로로 로드됐다. 따라서 로그만으로 새 저장 결함을 단정하지 않으며, 제품 코드 변경이나 오류 숨김은 하지 않는다. 다음 공개 검사는 빈 브라우저 context에서 실제 load 응답 수락 뒤에만 나오는 입력을 확인하고, iframe 경로와 새로고침 뒤 load 응답을 직접 관측하도록 보강했다. 그 후속 결과는 이 원본과 별도로 기록한다.

## 보존 자료

- 원본 `pin.json`, `result.json`, `workflow.txt`, `python-packages.txt`
- 네 화면의 문구 비교 채택·보류 캡처. 나머지 화면과 로그는 실행 artifact에 있다.
- 원본 ZIP artifact ID `12334868448`, SHA-256 `c9774a9ad0466d65565018a3c58e2b6e74485c98c7397f8ab013419c5d655563`, 2,760,932 bytes. 다운로드 후 SHA-256을 대조했다.
- 이 기록의 저장된 전체 API 결과는 기존 `features-20261021-run1`, SHA-256 `7c7c1947986ab168d09b4913b951a8c1529b25c7c26f6d454a461b1901b75c7f`다. 이번 확인을 새 모델 실행이나 사람 검수로 세지 않는다.
