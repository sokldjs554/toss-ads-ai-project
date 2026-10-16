# 평가

## 데이터셋 (`evals/datasets/`)

| 파일 | 내용 | 라벨 |
|---|---|---|
| `golden.jsonl` | 53건. 슬라이스: `clear`(명백 위반) · `evasion`(표기 변형·패러프레이즈·부정문) · `injection` · `gray`(경계, 수정 요청) · `benign_scary`(무서운 단어가 있지만 괜찮음 — 오탐 측정) | APPROVE / REVISE / REJECT + 잡혀야 할 `rule_ids` |
| `redteam_seeds.jsonl` | 레드팀 시드 4건 (명백한 위반) | REJECT |
| `adversarial.jsonl` | 레드팀이 **뚫은** 변형을 수확한 회귀셋 (처음엔 비어 있다) | REJECT |

라벨은 '코드가 내야 할 판정' 이다. 같은 저자가 규칙과 라벨을 썼으므로 `golden` 의 수치는 일반화의 증거가 아니라 **회귀 감시용** 이다. 사람 라벨러·독립 작성자 셋은 다음 단계다.

## 지표 (`evals/metrics.py`)

| 지표 | 정의 | 왜 보나 |
|---|---|---|
| 위험 승인율 (unsafe_approve) | 라벨이 REVISE/REJECT 인 소재를 APPROVE 한 비율 | 가장 중요한 안전 지표. 0 이어야 한다 |
| 오탐율 (false_flag) | 라벨이 APPROVE 인 소재를 REVISE/REJECT 한 비율 | 과잉 반려는 광고주 이탈 |
| 커버리지 | 사람 검토로 넘기지 않고 자동 판정한 비율 | 전부 사람에게 넘기면 '안전' 하지만 쓸모없다 |
| 규칙별 P/R/F1 | finding 의 rule_id 집합 vs 라벨 rule_id 집합 (warn 제외) | 어느 규칙이 과·소탐하는지 |
| ECE / Brier / reliability diagram | 신뢰도 = P(판정이 맞음) 으로 보고 보정 측정 | '자신 있게 틀림'(FM-14) |
| 고신뢰 오류율 | 신뢰도 ≥ 0.8 인데 틀린 비율 + 사례 id | 임계값 τ 의 근거 |
| 커버리지 곡선 | τ 별 (커버리지, 위험 승인율, 오탐율) | "0% 위험 승인으로 몇 % 를 자동 처리할 수 있나" |

모든 비율에 분모와 Wilson 95% 구간을 붙인다. 53건에서는 한 건이 2~7%p 를 움직인다.

## 실행

```bash
python -m evals.run --dataset golden --no-llm            # 결정론 계층만 (네트워크 없음)
python -m evals.run --dataset golden                     # 심사 모델 포함 (키 없으면 demo 대역)
python -m evals.run --dataset golden --gate              # 기준선 대비 회귀 게이트 (CI)
python -m evals.run --redteam --harvest                  # 레드팀 → 뚫린 변형을 adversarial.jsonl 에 수확
```

## 회귀 게이트 (`evals/regression.py`)

정확도 평균이 아니라 **방향** 을 본다. 기준선 대비 위험 승인율이 조금이라도 오르면 실패, 오탐율 +5%p·사람 검토율 +15%p 초과면 실패, recall 1.0 이던 규칙이 떨어지면 경고. 기준선 파일은 모드별(`baseline_demo.json`, `baseline_deterministic_only.json`)이라 대역 결과와 실제 모델 결과가 섞이지 않는다.

## 현재 기준선 (DEMO 모드 — 모델 성능이 아니다)

| 모드 | 커버리지 | 위험 승인율 | 오탐율 | 사람 검토율 |
|---|---|---|---|---|
| deterministic_only | 40/53 | 0/39 | 0/14 | 13/53 (benign 소재는 의미 심사 없이는 자동 승인하지 않는다) |
| demo (대역 심사) | 52/53 | 0/39 | 0/14 | 1/53 (부정문 안의 '보장' 이 의미 심사로 넘어감) |

레드팀(demo): 4 시드 × 8 전략 = 32 공격, 돌파율 0/32. 대역 공격자는 결정론 변환(띄어쓰기·제로폭·한자·패러프레이즈 사전·부정문·면책 뒤 숨기기·인젝션)이라 **실제 공격자 모델보다 약하다**. 키를 넣고 `--redteam` 을 돌리면 Claude 가 공격을 만든다.

## 품질 판정자 (`evals/judge.py`)

소재 쌍대 비교는 순서를 바꿔 두 번 묻고, 결론이 뒤집히면 무승부로 처리한 뒤 위치 뒤집기율을 보고한다 (LLM-as-judge 의 위치 편향).

## 한계

- 라벨 저자 = 규칙 저자. 독립 라벨러·실제 반려 데이터가 없다.
- 표본이 작다 (53). 한 건이 수 %p 를 움직인다.
- demo 수치는 대역이 낸 것이라 모델 성능이 아니다. LIVE 수치는 키를 넣고 직접 재야 한다 (`evals/reports/latest.json` 에 모드가 기록된다).
- 이미지·영상 소재, 랜딩 페이지 실제 fetch 는 범위 밖이다 (텍스트만).
