# 정규화 수정 작업 인계

이 파일은 검증 중의 재개 지점이다. **공개 화면 검증 완료 보고서가 아니다.** 최종 상태는 아래 Actions 실행의 실제 커밋과 artifact를 확인한다.

- 저장소: `sokldjs554/toss-ads-ai-project`
- 작업 브랜치: `fix/normalization-boundaries-20261023`
- 기준 main: `7b27a0e7689d4ee1319240f987d353cbac4a2d83`
- 보존한 첫 수정: `c7b86b692fe43b64ffeda66c3b567d6fd4512c0a`
- 정책 버전/리뷰 보완: `ed6e8b9b27cbef6ac217acfa5ef173b167decac0`
- 이 인계문을 포함하는 후속 커밋: 실제 UI 반려 fixture와 현재 대화 보고서의 정책 지문 보정. 수치·과거 보고서는 보존.
- 초기 로컬 소스: `/workspace/work/toss-ads-ai-project` (GitHub 연결 도구로 받은 소스; git checkout 아님). GitHub 접속 복구 뒤 `/workspace/work/verified-checkout`에 실제 clone을 받아 깨끗한 작업 트리와 커밋을 확인했다. 기존 `/workspace`의 다른 작업은 변경하지 않았다.
- 원본/로그: `/workspace/work/baseline-files.json`, `/workspace/work/evidence/`. 핵심 로그는 `docs/evidence/normalization-20261023/`에도 보존했다.

## 재현 및 검증 명령

새 환경에서는 해당 브랜치를 clone하고 `pip install -r requirements-dev.txt`, `python -m playwright install --with-deps chromium`을 실행한다. 모델 키를 넣지 말고 `PREFLIGHT_FORCE_DEMO=1`을 사용한다.

```bash
python -c 'from guardrails.lexical import scan; print([(f.rule_id, f.quote, f.start, f.end) for f in scan("쿠폰! 지금 확인하세요", {"ecommerce"})])'
python -m pytest -o addopts='' -q -ra tests/test_normalization_boundaries.py tests/test_conv_evals.py
python -m pytest -o addopts='' -q -ra
```

전체 CI는 PostgreSQL 격리 DB·Playwright 저장/권한 회귀와 평가 게이트를 포함한다. 로컬 DB가 없으면 skip을 통과로 합치지 않는다. 기존 `.github/workflows/ci.yml`과 같은 설정을 사용한다. 로컬에 Streamlit이 없어 전체 실행은 수집 오류 19개였고, 관련 묶음은 179 통과, 보고서+경계 묶음은 73 통과였다. 로컬 실행은 기존 설치된 `/workspace/.venvs/pickup-pact/bin/python`을 읽기 전용으로 사용했다.

진행 중 발견한 CI 실패도 남긴다:

- [c7b86b6 CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39908875444): 4,431 통과 / 1 실패 / 0 skip. 기존 UI 테스트가 정상 쿠폰 과탐 반려에 의존.
- [ed6e8b9 CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39909200224): 4,430 통과 / 4 실패 / 0 skip. 위 UI 1개 + 정책 버전 변경 후 보고서 지문 3개. 결과 수치 변화가 아님.
- 각 실행의 PostgreSQL 선행 게이트 54 통과는 전체 묶음과 중복이므로 총계에 더하지 않는다.
- 후속 [CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/workflows/ci.yml)의 전체 단계 성공 후에만 main을 fast-forward한다. 기존 작업을 보호하기 위해 예상 HEAD가 바뀌면 다시 확인한다.

## 배포 및 남은 확인

기존 Pages workflow와 Render main 자동 배포를 이용한다. Render는 사용자가 지정한 `my2 workspace` (`tea-db9hb2dg1s2s738nd1a0`), 서비스 `srv-db9hb6lg1s2s738ndklg`이다. 공동 검수 DB·사용자·광고 레코드에는 쓰지 않는다.

Pages 성공 뒤 `normalization-public-ui` workflow가 두 공개 URL을 실제 Chromium으로 조작하며 빌드/배포 소스·쿠폰·잔여 위반·직접 재검사·실제 금지 우회·화면 캡처를 검사한다. artifact의 `result.json`과 PNG를 확인한다. 인증 뒤 공동 검수 동작은 공개 브라우저 검사 범위 밖이며, 격리 DB 회귀와 구분해 보고한다.

```bash
python scripts/verify_normalization_ui.py --expected-sha FULL_DEPLOYED_COMMIT --out evidence
```

2026-10-23T06:07:34Z 로컬 `git ls-remote`는 `Failed to connect to proxy port 8080 after 0 ms: Could not connect to server`로 종료 128. 대상 도메인의 HTTP 차단 응답을 받은 것이 아니라 프록시 TCP 연결 단계에서 실패했다. 플랫폼 프록시를 제거하거나 우회하지 않았다. 오류 원문은 `docs/evidence/normalization-20261023/network-error.json`에 있다. GitHub/Render 연결 도구는 동작한다. 브라우저 검증이 실패/미실행이면 전체 수정 완료로 표시하지 않는다.

2026-10-23T06:21:06Z 네트워크 허용을 명시한 재확인에서는 GitHub HTTP 200과 git clone이 가능해졌다. 두 공개 사이트는 `Tunnel connection failed: 403 Forbidden`으로 거절됐다. 이는 앞선 TCP 연결 거부와 구분되는 프록시 터널 단계의 차단이다. `network-recheck-permitted.json`에 URL별 오류를 보존했다. 로컬 공개 접속 실패를 전체 인터넷 장애로 일반화하지 않는다. GitHub Actions의 공개 브라우저 검증 결과를 별도로 확인한다.

수정 원리·한계·동일 입력 전후·지원 문서 사실은 [기술 문서](normalization-boundaries-20261023.md)를 참조한다. 최종 CI 수·배포 SHA·실제 화면 검증 여부는 실행 후 결과로 채워야 하며 과거 4,396개 통과 기록으로 대체하지 않는다.
