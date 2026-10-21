# 새 대화 holdout 결함 후속 수정 검토 — 2026-10-20

## 범위와 결과

첫 결과에서 확인한 엔진 결함 세 가지를 `agents/conversation_rules.py`의 일반 추출 규칙으로 수정했다. `h26-d10`의 건강 관리 서비스 업종, `h26-d14`의 명시적 업종 정정, `h26-d20`의 공격 격리 다음 정상 턴에서의 목표 변경이 대상이다. 대화 ID에 따른 분기는 없다.

`tests/test_conv_holdout_regressions.py`에 회귀 검사 11개를 추가했다. 수정 전에 실제 실패를 확인했으며, 수정 뒤 11개 모두 통과했다. 이 입력들은 이제 개발자가 본 회귀 사례다. 후속 결과를 새로운 맹검 평가나 실제 모델 성능으로 해석하지 않는다.

첫 데이터와 첫 결과는 그대로 보존했다. 별도 결과 `evals/reports/dialogs_holdout_regression_20261020.json`에서 전체 검사 통과는 **17/20**, 명시한 슬롯 값은 **64/64**, 충돌·변경 처리는 **4/4**다. 남은 세 불일치는 기존 정책·라벨 해석 차이이며 엔진 정책이나 원본 라벨을 바꾸지 않았다.

## 원인과 최소 수정

| 결함 | 실제 원인 | 수정과 검증 범위 |
|---|---|---|
| 건강 관리 서비스 업종 누락 | 건강·의료 단서에 해당 일반 업종 표현이 없었다. | 건강 관리 서비스라는 구체적인 구를 health 단서에 추가했다. 띄어쓰기 유무를 허용한다. 단순히 건강이 중요하다는 말은 업종으로 읽지 않으며, 구 뒤의 부정도 기존 규칙으로 처리한다. |
| 적금으로 정정했는데 other가 됨 | 업종 선언 정규식에 목적격 조사 을·를이 없어 `업종을`의 `을`을 미등록 업종명으로 읽었다. 선언 구절 전체를 수정 표지 검사에 쓰면 `적금으로`의 `으로`도 이미 소비한 상태였다. | 업종·카테고리·분야 뒤에 을·를을 허용하고, 수정 표지는 식별한 업종 낱말의 끝에서 읽는다. 적금·교육·의료의 변경 요청을 검사하며, 변경 요청 없는 두 번째 선언은 계속 충돌로 남는다. |
| 리드 확보로 바꾸라는 정상 요청이 충돌이 됨 | 목표 단서가 `리드`에서 끝나서 바로 뒤의 `확보로 바꿔`를 기존의 `로 바꿔` 수정 표지로 인식하지 못했다. | 리드·잠재 고객 뒤의 확보를 목표 구에 포함한다. 공격 다음 정상 턴의 명시적 변경은 lead로 갱신하며, 단순히 리드 확보가 목표라고 말하면 기존 목표와 충돌한다. |

`is_update_at`의 공통 수정 표지 문법과 공격 격리 경계는 넓히지 않았다. `agents/conversation.py`와 `guardrails/injection.py`는 이번 수정에서 변경하지 않았다. 새 목표 표현도 격리 이후의 정상 입력에 대해서만 기존 추출 경로를 거친다.

## 회귀 검사와 RED → GREEN

새 검사 파일은 다음 행동을 확인한다.

- 건강 관리 서비스의 띄어쓰기 변형 두 가지에서 업종을 채우고 다시 묻지 않는다.
- 일반적인 건강 언급이나 부정한 건강 관리 서비스로 업종을 만들지 않는다.
- 업종·카테고리·분야의 목적격 조사와 명시적 변경 요청이 기존 업종을 갱신한다.
- 변경 요청이 없는 업종 선언은 기존 값과 충돌한다.
- 공격 턴의 정상 예산 접두부는 보존하고 공격 이후 같은 턴의 카테고리·목표·예산은 읽지 않는다.
- 다음 정상 턴의 리드 확보·잠재 고객 확보 변경은 반영한다. 단순 목표 선언은 계속 충돌로 처리한다.
- 새로 저장된 값의 근거 구절은 해당 사용자 문장의 실제 부분 문자열이다.

검사는 키를 비운 `force_demo=True` 설정으로 수행했다. 실제 API 호출은 없다.

| 실행 | 관측 결과 |
|---|---|
| 수정 전 `tests/test_conv_holdout_regressions.py` | **7 failed, 4 passed**. 실패는 위 세 원인과 그 표현 변형에서 발생했다. |
| 건강 표현만 수정한 뒤 관련 검사 | **4 passed** |
| 업종 정정 수정 뒤 관련 신규·기존 검사 | **17 passed, 18 deselected** |
| 세 수정 뒤 새 검사 파일 전체 | **11 passed** |
| `tests/test_conv*.py tests/test_injection.py` 전체 | **497 passed, 3 failed**. 세 실패는 갱신 전 저장 보고서의 코드 지문·새 메타데이터 불일치였다. 행동 회귀 실패는 없었다. |
| 보고서 재생성 뒤 아래 세 검사와 신규 11개 재실행 | **14 passed** |

저장 보고서가 오래되어 실패했다가 재생성 뒤 통과한 검사는 다음과 같다.

- `test_saved_report_is_exactly_what_the_current_code_and_data_produce`
- `test_seen_holdout_is_never_relabeled_as_unseen_and_historical_fingerprints_are_preserved`
- `test_original_after_fix_report_keeps_the_stricter_boundary_tradeoff_visible`

통과한 재실행 명령:

```bash
.venv311/bin/python -m pytest tests/test_conv_holdout_regressions.py tests/test_conv_evals.py::test_saved_report_is_exactly_what_the_current_code_and_data_produce tests/test_conv_evals.py::test_seen_holdout_is_never_relabeled_as_unseen_and_historical_fingerprints_are_preserved tests/test_conv_evals.py::test_original_after_fix_report_keeps_the_stricter_boundary_tradeoff_visible
```

기존 `dialogs_regression_v2.json`의 **66/66** 통과를 보존했으며, 원래 알려진 여섯 결함의 회귀 검사도 통과했다. 원본 대화 corpus를 그대로 재실행하는 `dialogs_original_after_fix_20261020.json`은 기존의 엄격한 같은 턴 격리 계약 때문에 **65/66**이다. 이 의도된 차이는 v2의 66/66과 구별해 보고한다.

## 원본과 후속 결과 비교

원본 데이터의 라벨과 문장을 고치지 않고 같은 20개 대화를 재실행했다. 새 결과의 역할은 `replay`이며 첫 결과를 대체하지 않는다.

| 지표 | 보존한 첫 결과 | 수정 후 같은 입력 재실행 |
|---|---:|---:|
| 전체 검사 통과 | 14/20 | 17/20 |
| 명시한 슬롯 값 정확도 | 61/64 | 64/64 |
| 최종 ready 일치 | 17/20 | 19/20 |
| 충돌·변경 처리 | 2/4 | 4/4 |
| issue 목록 일치 | 17/20 | 17/20 |
| 불필요한 재질문 | 2/76 | 0/76 |
| 필요한 질문 누락 | 0/6 | 0/6 |

공격으로 작성한 대화는 **4개**이며 재실행에서도 금지 값 오염은 **0/4**다. 원본의 `injection_dialogs: 7`은 비공격 문장의 금지 값 검사 세 개도 포함하므로, 실제 공격 개수로 사용하지 않는다. 새 결과에는 `attack_dialogs: 4`, `attack_contaminated: 0`이 별도로 기록된다.

남은 불일치 세 가지는 다음과 같다.

- `h26-d04`: 투자 광고의 `34세 이하`에는 성인 연령 하한이 없다. 기존 안전 정책대로 `MINOR_TARGET`과 blocked를 유지한다. 원본 ready=true 라벨은 바꾸지 않았다.
- `h26-d05`: 월 52만원에 기존 내부 가정 최소 예산 60만원에 따른 `BUDGET_LOW` 경고가 남는다.
- `h26-d12`: 월 48만원에 같은 `BUDGET_LOW` 경고가 남는다.

두 저예산 사례의 원본 expected_issues 누락을 결과를 본 뒤 고치지 않았다. 이 60만원은 프로젝트 내부의 가정이며 검증된 실제 플랫폼 정책이 아니다.

## 보존 확인과 코드 지문

아래 두 파일은 작업 전후 SHA-256이 동일하다.

| 파일 | SHA-256 |
|---|---|
| `evals/datasets/dialogs_holdout_20261020.jsonl` | `379b97b1de1277be13bfeae77606813a583b56040cd5c04c44121afbbc3da705` |
| `evals/reports/dialogs_holdout_20261020.json` | `339b84e92449c034ff4ad0fc3c2f9d6e365b190b89e715cbf2192c7e60593d76` |

이번 수정 완료 시의 개별 파일 SHA-256은 다음과 같다. 대화 평가기가 여러 파일을 합쳐 계산하는 `conversation_sha256`·`injection_sha256`과는 구분한다.

| 파일 | SHA-256 |
|---|---|
| `agents/conversation_rules.py` | `852da96072998cdfa580c5e3f2456ddd36f944bcb5f16b7f2dcc7ff125aacd76` |
| `agents/conversation.py` — 이번 수정 없음 | `b8b83cdefc3b1380acae16ce9f1a18c0c2254d4b48ac46b56bcf85831f2644a9` |
| `guardrails/injection.py` — 이번 수정 없음 | `13f7fc58c98802ebffe9845054d93425ca1ec1949eff05cd81e1ec10a7fe56a6` |
| `tests/test_conv_holdout_regressions.py` | `4a51b1ca444e5caf87ea04a5ed0e864b9119fa0214b06fe3f3bd64fd801b579d` |

첫 결과의 해석과 작성·동결 기록은 `docs/holdout-followup-20261020.md`에 남아 있다. 이번 수정 이후 새 입력 일반화 성능을 측정하려면 수정 엔진을 다시 동결하고 새로운 독립 입력을 작성해야 한다.
