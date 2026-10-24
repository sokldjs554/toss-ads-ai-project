# 승인된 LIVE 예약 1건 추가: 배포 결과

사용자는 전체 예약 한도 $1.50 → $1.52 조정을 명시적으로 승인했다. 기준 `fe3e054`에서 `3f7833509636fc3d0056905b290b4d687e7630c1`로 main·Pages·Render를 배포했다. 기존 전체18요청·예약$1.44에서 신규1요청($0.08)만 가능하며 다음 요청은 거부한다. 사용자당$0.60, 호출4회·수정2회 등은 유지한다. 예약액은 실제 청구액이 아니다.

- 실제 격리 PostgreSQL 경계 회귀: 수정 전1실패·16선택 제외, 수정 후 관련48통과·실패0·skip0·error0. [red.log](red.log), [green.log](green.log).
- 동일 배포 커밋 [전체 CI 40064638665](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40064638665): 4,505통과·실패0·skip0·error0, 242.97초. [원본 로그](ci-40064638665.log). 테스트 수는 정책 정확도가 아니다.
- [Pages 40065093581](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40065093581) 성공. Render `dep-dbe3qj6q1p3s73f8piq0` live, 2026-10-24 14:09:45 KST 완료.
- [공개 Chrome 검증 40065136970](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/40065136970): LIVE33/33·무료51/51. 두 사이트 `3f78335` 확인. 서버는 버전이 보이는 실제 PNG, Pages는 DOM 및 게시 소스 일치 확인. LIVE는 저장 기록을 읽는 verify_only 실행이며 신규 모델 호출·사람 사용이 아니다.
- 배포와 브라우저 검증 전후18개 기록(자동13·사람 계정5)의 실행·호출·감사 내용 비교값 동일. 제공자 시도34회 유지. 기존 데이터 변경·삭제0, 신규 유료 호출0. [이전 비교값](before-record-digests.json), [이후 비교값](after-record-digests.json). MD5는 내용 비교 수단이며 위변조 보증이 아니다.

[실제 화면·브라우저 원자료](https://github.com/sokldjs554/toss-ads-ai-project/tree/1e2a81e84b259d888c630972ca7c661cb5726f9c/docs/evidence/live-pilot-20261023/public-40065136970-attempt-1) · [버전 표시 서버 화면](https://github.com/sokldjs554/toss-ads-ai-project/blob/1e2a81e84b259d888c630972ca7c661cb5726f9c/docs/evidence/live-pilot-20261023/public-40065136970-attempt-1/control-tools-comparison-saved-version.png) · [구조화 결과](release.json)

남은 사람 작업은 [도구 경로 신규1건 실행 절차](../../operating-pilot-20261024/quota-extension.md)를 따른다. 직전 `3962f50a`는 RAG 실행이며 도구 경로 완료로 세지 않는다. 실제 LIVE 사람은 본인1명이며 기존 선호 평가2명과 별개다. 확인된 사용은 KST 같은 날짜이므로 장기 운영·상용 고객·시간 절감·채용팀 production 요건 충족으로 확대하지 않는다.

사용자는 한도 증액을 결정하고 기존 수동 검토를 수행했다. 이번 코드·테스트·문서 작성과 실행·배포·자동 검증은 AI 지원 작업이다. 커밋 작성자 표시만으로 지원자가 직접 코드를 작성했다고 판단하지 않는다. 이번 변경은 한도·표시·관련 테스트와 문서에 한정되며 프롬프트·정책·평가 입력·기존 한계 수치를 바꾸지 않았다. 네트워크에서 일반 git fetch가 인증 입력 부재로 실패했으나 GitHub 연결 도구와 명시적 브랜치 pull/push로 이어갔고 프록시를 변경하지 않았다.
