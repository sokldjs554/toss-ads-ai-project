# 최신 상태 안내

아래는 모델 키 등록을 확인하기 전 보존한 과거 상태다. 이후 실제 호출은 이뤄졌으며 최신 결과는 [구조 보완 장부](live-pilot-structured-20261023.md)를 우선한다. v3 실제 실행·최종 배포 검증은 진행 중이다.

# 2026-10-23 14:02 UTC 당시 기술 상태 — 과거 기록

배포 main: `da250e7769379ee3840678af6ca39f7bbf1d6eab`. 기준: `7767dcbe288fd9ceaf6b9fdc021f7ac4450ad388`. 후속 공개 검증 근거는 `feat/live-rag-tools-20261023`에 보존한다.

- 최종 main [CI 39966038678](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39966038678): **4,487 통과 / 실패 0 / skip 0 / error 0**, 244.97초. 별도 DB 54개·LIVE 26개 게이트는 전체와 중복되며 합산하지 않는다. 모든 평가 게이트 통과. 제공자 호출 없음.
- [Pages 배포](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39966038780) 성공. Render `dep-dbdmh7lg1s2s73ds3oh0`은 14:02:10 UTC에 같은 커밋으로 live.
- [최종 공개 Chrome 실행](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39966158608): [무료 회귀 51/51 통과](evidence/live-pilot-20261023/public-39966158608/free-current/result.json). 두 사이트 버전·Pages 번들 16개 관련 파일 일치·쿠폰 과탐 제거·다른 위반 유지·직접 수정·금지 업종 우회 표기 반려·기존 평가 한계·재생 구분 확인.
- [LIVE 확인](evidence/live-pilot-20261023/public-39966158608/result.json)은 버전/구분 3항목 확인 후 모델 설정 부족으로 중단. 전체 공개 작업은 **실패**다. 무료 회귀 성공을 실제 모델 성공으로 대체하지 않는다.
- [최종 서버 실제 캡처](evidence/live-pilot-20261023/public-39966158608/live-configuration-missing.png)에 `빌드 da250e7`과 `미등록: 모델 연결`이 함께 보인다. [Pages 캡처](evidence/live-pilot-20261023/public-39966158608/pages-live-server-link.png)도 같은 빌드다. Chrome 155.0.8059.39, 한글 폰트 설치 후 촬영 원본.

현재 새 파일럿 **요청 0 / 제공자 호출 0 / 비용 예약 $0 / 실제 사람 0명·0건**. 실제 모델 ID·토큰·비용 측정 결과는 아직 없다. `gpt-4o-mini-2024-07-18`은 예정 모델이다. 저장·권한·중복·상한은 격리 PostgreSQL과 모의 모델 테스트에서 확인했고, 배포 UI의 실제 기록 저장·재접속·재배포 보존은 미완료다. 기존 공동 검수 데이터는 변경하지 않았다.

사용자가 Render 등록 완료를 알렸지만 최종 프로세스에는 `OPENAI_API_KEY`가 없거나 공백이다. 저장소 설정은 존재한다. my2 workspace의 정확한 `preflight-toss-ads` 서비스 Environment에서 저장 상태 확인을 요청했다. 키 값이나 유료 호출 재승인을 요청한 것은 아니다. 설정 복구 후 이미 승인된 범위에서 고정 5개 입력을 실행하고, 마지막 재배포 후에는 조회 전용 모드로 기록 보존을 확인해야 한다.

RAG/실제 function-tool 기반 수정 워크플로의 구조·한계·중간 실패는 [기술 장부](live-pilot-20261023.md), 사람 수행 절차는 [런북](live-pilot-human-runbook.md), ChatGPT Work 사실 표와 기여 구분은 [주장 검증표](live-pilot-claims.md)에 있다. 자기소개서·이력서·포트폴리오는 수정하지 않았다. 기존 지속 하락 미탐 7/12·13/24, 정책 위험한 통과 4/16의 AI 초안 정답 조건, 56응답=28쌍×2명·21/28 일치(공동 보류 포함)는 유지한다. 배포·자동 실행을 실사용 운영 또는 사람 시간 절감으로 표현하지 않는다.
