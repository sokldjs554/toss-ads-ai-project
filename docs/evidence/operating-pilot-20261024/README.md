# 2026-10-24 반복 사용 준비와 실패 기록 수정 증거

- 서비스 기준117d996, 수정fe3e054. 신규 실제 모델 호출0, 새 사람 작업0.
- failure-red: 실제 격리 PostgreSQL + 모의 SDK 오류에서3실패. failure-green: 관련48통과.
- full-local: 4494통과·1실패·10오류·skip0. 11개 브라우저 테스트가 Chromium 실행 파일 부재로 실패했다. 실패를 숨기거나 CI 성공으로 덮어쓰지 않았다.
- measurement-smoke.py/.log: 기존 DEMO 생성→원문 직접 재검사 조작을 AppTest로 실행했다. 두 입력 원문·사실 유지와 APPROVE/REVISE, 비용0을 확인했다. 실제 공개 브라우저·사람 시간 측정과 다르다.
- local-results.json: 환경·로컬 결과 요약. readiness.json: 시험 계정 만료/예약 잔액의 읽기 전용 확인. persistence-final.json: 재배포 전후15행의 실행·호출·감사 내용 동일, 제공자 시도31회 유지.
- release.json과 별도 public Actions 원본은 최종 CI·배포·공개 브라우저를 구분한다. 같은 커밋의 여러 CI와 중복 선행 게이트를 합산하지 않는다.
- scoring-template.csv는 빈 양식이다. 정답 검수자/참여자/조건을 가린 채점이 필요하며 현재 측정 완료0행이다.

재현은 저장소 루트에서 `PREFLIGHT_FORCE_DEMO=1 OPENAI_API_KEY='' ANTHROPIC_API_KEY='' python docs/evidence/operating-pilot-20261024/measurement-smoke.py`. 본문 원문은 고정 합성 예시이며 실제 사람 검토 사유와 키는 포함하지 않는다.

공개 full-local.xml의 테스트 이름에 포함된 합성 API키 fixture3개는 REDACTED_SYNTHETIC_KEY_FIXTURE로 가렸다. 판정·개수·실패 내용은 변경하지 않았다.
