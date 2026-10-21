# 문구 비교 wire/API 소규모 실측 증거

2026-10-21 [실행 39579660982](https://github.com/sokldjs554/toss-ads-ai-project/actions/runs/39579660982)는 **완료**됐다. 기존에 본 가상 문구 4쌍을 원래·반대 순서로 한 번씩 비교한 API 경계 확인이다. 전체 159회 평가를 다시 실행하지 않았다.

## 확인된 결과

[원본 결과 JSON](https://github.com/sokldjs554/toss-ads-ai-project/blob/1dd74074b50b249de580c94712a70cd40775bf00/evals/reports/quality_wire_smoke/quality-wire-20261021-smoke1.json)을 기준으로 실제 요청 **8회**, schema 통과 **8/8**, 인용·근거 계약 검사 통과 **8/8**, 보존된 원본 completion **8개**다. 요청·반환 모델은 모두 `gpt-5.4-mini-2026-03-17`이며, reasoning effort는 `none`, SDK 재시도는 0이다. fallback·추가 수리·자동재실행은 없다.

| 고정 사례 | 두 순서 합산 상태 | 채택 결과 |
| --- | --- | --- |
| QP-H01 | consensus | a |
| QP-H05 | consensus | tie |
| QP-H13 | consensus | tie |
| QP-H25 | consensus | tie |

`communication-wire-v3-20261021`은 공급자 출력 형식의 버전이며, 판단 기준 `communication-v2-20261021`과 구분한다. wire의 단일 judgment를 기존 의미 계약으로 정규화하며 의미를 바꾸거나 실패를 정답으로 수리하지 않는다. schema·인용 근거 검사와 두 순서의 채택 판단도 분리해 기록했다.

## 비용과 예약

공개 단가에 응답 토큰을 곱한 사용량 기준 계산액은 **$0.0285105**다. 캐시 할인을 적용하지 않은 원장 계산값이며 공급자의 실제 청구서 금액은 아니다.

이번 run은 호출 전에 **$0.50 전체 한도**를 영구 예약했다. 누적 예약은 **$2.50/$4**이며 기존 **$2 예약 행은 그대로** 보존했다. 계산액이 예약보다 작아도 예약을 반환하거나 재사용하지 않는다. 예약 한도와 사용량 계산액은 서로 다른 값이다.

## 추적과 해석 범위

- [실행 소스 f55cc89](https://github.com/sokldjs554/toss-ads-ai-project/commit/f55cc89b6c9beb14edb587bc6f615c825aaf23cd)
- [API 키 주입 전 예약 커밋 20c6841](https://github.com/sokldjs554/toss-ads-ai-project/commit/20c68414047c2b1a98364ab1379b2e4c46bbad86)
- [결과 커밋 1dd7407](https://github.com/sokldjs554/toss-ads-ai-project/commit/1dd74074b50b249de580c94712a70cd40775bf00)

기존 원본 보고서 4개와 과거 예약 장부의 SHA-256은 manifest에 보존되어 있으며 현재 파일과 모두 일치한다. 기존 점수·라벨·분모를 다시 쓰지 않았다. 원문 completion, wire 응답, 정규화 응답, 모델·사용량·예약/정산 계산값을 별도 결과에 남겼다.

이 4쌍은 이전에 본 입력이다. 독립적인 사람 라벨을 새로 만들거나 검증하지 않았으며, 작성자 라벨 대비 정확도를 산출하지 않았다. 이번 결과는 작은 고정 입력에서 SDK/API와 새 wire 계약이 작동한 증거다. 문구 전달력 정확도·일반화·클릭률·전환율 또는 성능 향상을 측정한 결과로 해석하지 않는다.
