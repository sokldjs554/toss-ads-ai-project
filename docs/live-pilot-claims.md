# ChatGPT Work용 주장 검증표

지원서 문안이 아니라 사실 중심의 기술 인계다. 아래 실측은 `b0f110c`, 프롬프트 `live-rag-tools-4`, 정책 `2026.10.7`에 대한 기록이다. 문서 재배포의 최종 SHA/CI/조회 화면은 최종 인계문의 후속 증거와 대조한다. [상태·원자료](live-pilot-final-status-20261023.md), [선택 이유·실패·코드](live-pilot-decisions-20261023.md).

| 지원서에 쓸 수 있는 사실 | 정확한 수치·분모 | 검증 조건 | 근거 링크 | 쓰면 안 되는 확대 해석 |
|---|---|---|---|---|
| 배포 서버에서 검색 근거 검토와 실제 도구 호출 워크플로를 실행 | 두 경로 각 1요청, 합계 API 4회 | 합성 입력·자동 계정, gpt-4o-mini-2024-07-18, 사람 검토 대기까지 | [Chrome 원장](evidence/live-pilot-20261023/public-39999144241-attempt-1/result.json) | 상용 고객 production 경력 충족 / 실제 사람 승인 / 독립 서비스 두 개 |
| RAG는 정책·상품 근거를 검색하고 모델이 출처/구절과 검토 의견을 선택·생성 | 정책 최대6·상품 사실 최대4, 실제 v4 응답2건의 출처/구절 검사 통과 | 문자 bigram 검색·업종 필터·결정론 위반 우선, 원문 span 대조 | [구현](../agents/live_pilot.py), [오프라인 감사](evidence/live-pilot-20261023/final-audit/offline-audit.json) | 벡터 검색 / 검색 recall 검증 / 인용 의미·정책 정확도 입증 |
| 모델의 실제 check_policy 함수 호출로 수정·재검사를 수행 | 최종 도구 사례: 수정1회·API3회 | 5,000원→50,000원 조건 수정, 코드/모델 APPROVE 뒤 사람 검토 대기 | [실제 화면](evidence/live-pilot-20261023/public-39999144241-attempt-1/control-tools-comparison-reconnected.png), [원본](evidence/live-pilot-20261023/final-synthetic-records.json) | 무제한 자율 에이전트 / 광고 자동 집행 |
| 인용 실패를 실제 호출에서 발견하고 원문 구절을 보존하는 계약으로 보완 | 동일 도구 입력1건: v2 인용 불일치→v3 제공자 실패→v4 검토 대기 | 광고·상품 payload hash 동일. v2 holdout을 후속 개발 자료로 전환했다고 명시 | [같은 입력 대조](evidence/live-pilot-20261023/final-audit/offline-audit.json), [HTTP400 진단](evidence/live-pilot-20261023/schema-diagnostic-39996868624.json) | 일반 정확도 개선 / 독립 holdout 성능 / 실패 사례 삭제 |
| 누적 실제 호출과 실패·비용 미확인을 기록 | 서버13요청/27시도 + 별도 진단1시도; 성공26·실패2, 알려진 비용 추정 $0.006483 | 자동 계정2, 서로 다른 입력7개·반복 검증 포함; 실패2회 사용량/비용 미확인 | [원장](evidence/live-pilot-20261023/final-synthetic-records.json), [별도 진단](evidence/live-pilot-20261023/schema-diagnostic-39996868624.json) | 13개 독립 평가 사례 / 실제 청구액 확정 / 실패 비용0 |
| 배포 코드의 전체 회귀와 별도 실제 브라우저 조작 검증 | CI4496통과·실패/skip/error0; LIVE35/35, 무료 회귀51/51 | CI는 유료 호출0. 브라우저 수치는 체크 항목 수 | [CI](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39997476845), [LIVE](evidence/live-pilot-20261023/public-39999144241-attempt-1/result.json), [무료](evidence/live-pilot-20261023/public-39999144241-attempt-1/free-current/result.json) | 정책 정확도100% / 테스트4496개=실제 모델4496건 |
| 저장·새 세션 재접속·타 계정 목록 분리 확인, 이전 기록의 배포 후 보존 확인 | 최신 두 결과 재접속·감사 동일, 기존 자동 기록11/11 전체 export 필드 동일 | 실제 Chrome + 배포 전후 SQL 대조; 읽기/쓰기 권한 경계는 격리 DB 테스트 | [보존 대조](evidence/live-pilot-20261023/persistence-through-v4.json), [권한 테스트](../tests/test_live_pilot_store.py) | DB 관리자까지 격리 / 장기 보존 SLA / 실제 고객 다중 사용자 검증 |
| 중복·비용·실패 시 처리의 경계를 구현 | 요청 예약$0.08·사용자$0.60·전체$1.50, 최대4호출·2수정, 분당 신규3 | 서버 영속 예약, SDK retry0·30초 timeout, 불명 비용 환급 없음, 실패 시 보류 | [장부](../ops/live_pilot_store.py), [SDK 경계](../llm/live_pilot_gateway.py) | 제공자 계정 전체 청구 상한 / 모든 실패 유형의 실서비스 발생 검증 |
| 본인 1명의 두 경로 직접 사용과 승인 저장 확인 | 1명·합성 광고2건·API4회 성공, 추정$0.001855 | 안내된 예시, 직접 읽고 판단했다는 자기보고 + 서버의 승인2건·비어 있지 않은 사유 감사. 초기 export는 저장 전이었고 재안내 후 저장 확인 | [사람 기록](live-pilot-human-result-20261023.md), [최소 메타데이터](evidence/live-pilot-20261023/human-pilot-summary.json) | 독립 평가자 / 상용 고객·장기 production / 검수시간 절감·협업 효과 |
| 지속 하락 후보가 새 자료에서 악화해 철회 | 개발 미탐7/12·새 자료 미탐13/24 유지 | 이전 고정 자료, 이번 LIVE 연결과 별개 | [기존 평가](strengthening-20261023.md) | 이번 변경으로 지속 하락 미탐 해결 |
| 새 정책 평가의 위험한 통과 한계 유지 | 4/16 | 사람 검수 전 AI 초안 정답 | [기존 평가](strengthening-20261023.md) | 독립 사람 정답 기준 정확도 |
| 기존 사람 응답·선택 일치의 정확한 분모 유지 | 56응답=28쌍×2명, 21/28 일치에 공동 보류1쌍 | 기존 사람 비교·사후 대조이며 새 독립 실험 아님 | 기존 평가 및 [보존 조건](live-pilot-final-status-20261023.md) | AI 정확도75% / 신규 사람 실험56건 |

## 기여·판단의 출처

| 구분 | 확인되는 내용 | 확인되지 않은 내용 |
|---|---|---|
| 사용자 | 목표·우선순위·삭제 금지어 유지 지시·유료 모델 호출 승인·Render 키 등록 완료 통지·새 미사용 DB 역할 삭제 승인·제한 DSN의 Render 전달 승인 | 이 변경 코드의 직접 작성. 두 경로 직접 사용은 별도 자기보고와 서버 감사로 확인했으나 독립성·신원 인증은 입증하지 않음 |
| AI 지원 | 코드 분석·설계 후보 선택·구현·테스트 작성/실행·브라우저 자동화·배포 조작·기술 문서 초안 | 사용자 본인의 숙련도·인터뷰 설명 능력 |
| 자동 브라우저 계정 | 합성 입력 실행 및 승인/반려 저장 기능의 기계적 검증 | 사람의 정책 판단·실고객 사용 |

커밋 작성자 표시는 기여 확인의 충분한 근거가 아니다. 기술적 선택과 실패는 실행 장부에 원인·대안·관련 테스트 및 실패 로그 링크로 남긴다. 사람이 검토한 내용은 별도 수행 기록과 감사 ID로 추후 확인한다.
