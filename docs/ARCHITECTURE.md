# 아키텍처

## 한 문장

**모델은 제안하고, 코드가 판정하고, 트레이스가 증명한다.** LLM 출력은 신뢰하지 않는 입력(untrusted input)으로 다루고, 승인으로 가는 모든 경로에는 코드가 검증하는 관문이 있다.

## 흐름

```mermaid
flowchart LR
    A[광고주 요청 + 상품 사실 + 설명 + 랜딩 텍스트] --> IG[0 · Safety Intercept<br/>injection / 정책 의도 / 금지 업종]
    IG -- hard block --> STOP[중단 · 안내]
    IG -- 정제된 요청 --> PL[1 · Planner<br/>GPT-4o Structured Outputs]
    PL --> PV[플랫폼 제약 검증·수리<br/>입찰 방식·예산 합·타겟]
    PV --> CW[2 · Copywriter<br/>Claude · 비제약 생성]
    CW -- JSON 깨짐 --> RP[수리: 구조화 제공자]
    CW --> OG[3 · Output Guard<br/>정규화·패턴·고지·근거·랜딩·중복·형식]
    OG --> AU[4 · 가상 유저 패널<br/>Claude · 분산으로 아첨 탐지]
    OG --> RV[5 · Reviewer<br/>GPT-4o Structured Outputs<br/>rule_id Literal · 인용 검증]
    RV -- 경계 사례 --> SO[교차 검증<br/>다른 모델 계열]
    RV -- 수정안 --> RC[수정안 재검사]
    SO --> AD[6 · Adjudicator<br/>코드가 최종 판정]
    RC --> AD
    AU --> AD
    AD --> R[점검 통과 / 수정 필요 / 게재 불가 / 담당자 확인 필요]
```

모든 상자는 트레이스 스팬이다. 실패하면 예외가 밖으로 나가지 않고 `FailureEvent(FM-xx)` 로 기록된 뒤 정해진 대체 경로를 탄다.

## 디렉터리

| 경로 | 역할 |
|---|---|
| `core/` | 도메인 스키마(`schemas.py`), LLM 출력 스키마(`llm_schemas.py` — strict 호환, rule_id Literal), 설정, 플랫폼 제약(`platform_spec.py`) |
| `llm/` | 제공자 추상화. `router.py` 가 작업→제공자 라우팅, fallback 체인, 서킷브레이커, 예산 상한, 수리 경로, 장애 시뮬레이션을 맡는다 |
| `guardrails/` | 정책 카탈로그(`policy.yaml`), 정규화(`normalize.py`), 결정론 규칙(`lexical.py`), 고지(`disclosure.py`), 근거·랜딩(`grounding.py`), 인젝션(`injection.py`), 중복(`duplicate.py`), 입력 인터셉트(`input_guard.py`), 출력 묶음(`output_guard.py`), 실패 분류(`failsafe.py`, `failure_modes.py`) |
| `agents/` | 단계별 에이전트(planner, copywriter, audience, reviewer, adjudicator)와 `pipeline.py`, 직접 고친 문구의 재검사 `recheck.py`. 프롬프트는 `prompts.py` 한 곳에 버전과 함께 |
| `observability/` | 의존성 없는 트레이서(`tracer.py`: Trace/Span, contextvars, JSON 직렬화), 가격표, 저장된 기록을 화면용으로 되살리는 `replay.py` |
| `evals/` | 데이터셋(golden dev · holdout · contrast · adversarial), 지표, 실행기(판정 원본 입력 저장·추천 τ 유효성), 회귀 게이트, 레드팀, 쌍대 판정자, CLI |
| `ui/` | Streamlit 화면 세 영역 — `workbench.py`(광고 작업) · `cases.py`(검사 사례) · `quality.py`(품질 확인). 화면 문구는 `labels.py` 한 곳, 디자인 토큰은 `theme.py`. 계산·판정은 하지 않는다 |
| `tests/` | 126개 단위·통합·화면(AppTest) 테스트 (네트워크 없음) |
| `traces/` | 재생용 샘플 트레이스 |

## 하이브리드 라우팅

| 작업 | 1순위 | fallback | 이유 |
|---|---|---|---|
| 캠페인 계획(PLAN) | OpenAI · `chat.completions.parse` | Anthropic · `messages.parse` | 스키마 보장이 중요. 값 수준(예산 합·입찰 허용)은 코드가 다시 검증 |
| 심사(REVIEW) · 품질 판정(JUDGE) · 수리(REPAIR) | OpenAI 구조화 | Anthropic 구조화 | `rule_id` 를 카탈로그 Literal 로 고정 → 없는 규칙을 생성할 수 없음 |
| 소재(COPY) · 패널(AUDIENCE) · 준수 제안(REDIRECT) · 레드팀(ATTACK) | Anthropic · 비제약 | OpenAI 구조화 | 창의 작업은 형식 제약 없이 돌리고(형식 제약이 품질을 떨어뜨린다는 보고), 형식이 어긋나면 구조화 제공자가 '텍스트→스키마' 수리 |
| 교차 검증(SECOND_OPINION) | Anthropic 구조화 | OpenAI | 같은 계열의 상관 오류를 피하려고 **다른** 계열에 묻는다 |

- 제공자 실패(타임아웃·429·5xx)는 서킷브레이커(연속 3회 → 60초 open)에 기록되고 다음 제공자로 넘어간다.
- 거부(refusal)·잘림(max_tokens)·스키마 불일치는 각각 타입이 있는 예외라 라우터가 다르게 처리한다 (잘림은 max_tokens 2배 재시도 1회).
- 실행 단위 예산 상한(기본 $0.50)·호출 횟수 상한은 **요청 시도 수**(실패·재시도·수리 포함)에 걸리고 재시도 직전에도 확인한다. 넘으면 `BudgetExceeded` 로 즉시 중단, 남은 소재는 담당자 확인. 응답을 못 받은 요청은 비용 미확인으로 세어 총비용을 '추정' 으로 표시하고, 제공자가 사용량을 주지 않으면 글자 수로 추정해 `cost_estimated` 를 붙인다.
- 키가 없으면 결정론 대역(`mock_provider.py`)이 LLM 자리를 맡는다. 대역은 일부러 '공격적인' 변형을 섞어 가드레일이 잡는 모습을 보여 준다. 모든 결과에 `provider=mock` 이 찍힌다.

## 가드레일 계층 (앞일수록 싸고 결정론적)

| 계층 | 하는 일 | 못 하는 일 |
|---|---|---|
| injection | 지시문·역할 전환·심사 조작 패턴(한/영), 숨은 문자. 문장 제거 + `<untrusted_data>` 포장 | 자연어로 교묘히 쓴 설득은 못 잡는다 → semantic |
| lexical | NFKC·제로폭·한자 접기·띄어쓰기 분해 후 두 표면(loose/tight)에서 패턴. 예외 창, 부정문 강등, 우회 흔적 시 심각도 상향 | 같은 뜻의 다른 말('손해 볼 일 없는')은 어휘로 못 잡는다 → semantic |
| disclosure | 업종별 필수 고지 체크리스트, 고지란의 광고 문구 | 고지 내용의 사실 여부 |
| grounding | 소재의 숫자가 값·단위 종류·뜻까지 같은 수치로 사실 목록/랜딩에 있는지, 주장이 가리킨 `fact_id` 가 존재하고 실제로 그 주장을 뒷받침하는지, 근거 사실의 조건(자동결제·심사 조건·최소 금액·기간·1인 1회·제외)이 광고에 있는지, 랜딩 어휘 겹침 | 사실 목록 자체의 진위, 여섯 종류 밖의 조건, 어휘 목록 밖의 '뜻' |
| structural | 길이·한글 비율·중복 재등록·플랫폼 제약 | — |
| semantic | LLM 심사. 결정론 finding 은 '확정 사실' 로 주고, 놓친 의미상 위반만 찾게 한다. 인용은 원문 대조, rule_id 는 스키마로 고정 | 모델이 틀릴 수 있다 → 신뢰도 임계값·교차 검증·사람 검토 |

## 판정 규칙 (adjudicator)

규칙 순서는 순수 함수 `agents/adjudicator.py::decide(inputs, τ)` 하나에만 있다. 서비스(pipeline)·직접 수정 재검사(recheck)·평가(evals)가 같은 함수를 부르므로 같은 입력·같은 모델 응답·같은 τ 면 같은 판정이 나온다.

1. 코드 검사에서 **block** 이 하나라도 있으면 게재 불가 — 모델은 뒤집지 못한다 (확신과 무관).
2. 검토 모델이 실패했으면 담당자 확인.
3. 모델이 게재 불가라 했지만 **원문에서 검증된** block 인용이 없으면 담당자 확인. 모델이 수정·게재 불가를 말했는데 근거 finding 이 하나도 남지 않았어도 담당자 확인.
4. 원문에 없는 인용이 있으면 담당자 확인, 확신 < τ 면 담당자 확인, 두 계열의 판정이 다르면 담당자 확인.
5. fix 가 있으면 수정 필요.
6. 그 외 점검 통과.

**수정안은 판정과 별개의 상태를 가진다** (`PreflightReport.rewrite_status`: `none` · `verified` · `failed_recheck` · `unchecked`). 모델이 제안한 수정안(`verdict.proposed_rewrite`)은 전체 코드 검사를 다시 통과했을 때만 `verified_rewrite` 가 되고, 어떤 판정 경로(수정 필요·게재 불가·담당자 확인)에서도 재검사에 실패한 수정안은 제시하지 않는다. 수정안의 주장→근거 연결은 바뀐 문구에 그대로 남은 주장만 이어받는다.

τ(자동 판정에 필요한 최소 확신)는 평가의 임계값 곡선으로 정한다. 곡선은 저장된 판정 원본 입력으로 `decide()` 를 다시 불러 계산하고, 추천값은 제공자·모델·프롬프트·검사 기준·데이터 버전이 현재 설정과 같을 때만 쓴다 (`evals/runner.py::recommendation_for`).

## 화면 구조

| 영역 | 내용 | 상태 |
|---|---|---|
| 광고 작업 (`ui/workbench.py`) | 상품 정보 → 반드시 포함해야 할 조건 → 광고 요청 → 광고 초안 만들기. 광고안마다 원문(확인이 필요한 구절 강조) → 가장 중요한 이유 → 구절별 이유 → 판단 근거 → 다음 행동 → 수정 제안(검증된 것만) → 직접 고쳐서 다시 검사 → 상세 | 입력 전 · 실행 중(실제 단계 진행) · 완료 · 일부 단계 실패 · 전체 실행 실패 · 결과 없음 · 입력 변경 후 재실행 필요 · 담당자 확인 필요 |
| 검사 사례 (`ui/cases.py`) | 저장된 실제 모델 실행 기록(날짜·모델·비용 표시, `observability/replay.py`)과 대표 사례. 광고 작업으로 불러오기 | 저장된 결과는 '저장된 실행 결과' 로만 표시 |
| 품질 확인 (`ui/quality.py`) | 평가 결과(건수와 쉬운 말), 개선 이력, 처리 기록, 기술 상세(확신 보정·임계값 곡선·규칙별 P/R·우회 표현 검사·실패 모드·검사 규칙) | |

실행 방식은 항상 표시된다: `예시 실행 · 실제 AI 호출 없음` / `실제 AI 호출 · 한 제공자` / `실제 AI 호출 · 두 제공자` / `저장된 실행 결과 · 날짜`. 서버 API 키는 `app.py` 에서만 읽고 화면에는 설정 여부만 보인다. 담당자 확인은 실제 대기열이 없으므로 '전달되었다' 고 표시하지 않고, 복사·내려받기할 수 있는 확인 요청문만 만든다.

## 트레이스 모델

`Trace(name, trace_id, metadata, root: Span)` · `Span(name, kind ∈ {stage, llm, guardrail, tool}, status ∈ {ok, error, intercepted, fallback}, attributes, events, children)`.
LLM 스팬 속성: provider, model, 시도 체인, 제약 디코딩 여부, 토큰, 비용, 지연, 수리 여부, 프롬프트/응답 원문. 가드레일 스팬 속성: finding 목록, 체크리스트.
JSON 으로 내보내고 다시 읽어 '재생' 할 수 있다 — 관측 탭의 `traces/sample_*.json`.

## 왜 Streamlit 하나에 다 넣었나

면접관이 `pip install -r requirements.txt && streamlit run app.py` 한 줄로 에이전트의 움직임·가드레일·트레이스·평가를 전부 볼 수 있어야 한다는 요구 때문이다. 계산 코드는 UI 와 분리되어 있어(`ui/` 는 그리기만 한다) FastAPI 등으로 옮기는 데 UI 코드 변경이 필요 없다.
