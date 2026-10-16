"""가드레일 모듈.

계층 (앞일수록 싸고 빠르며 결정론적):
  injection  → 입력 속 프롬프트 인젝션 탐지 (상품 설명서·광고주 요청)
  lexical    → 정규화(제로폭 문자·띄어쓰기·유사 문자 접기) 후 규칙 패턴 매칭
  disclosure → 업종별 필수 고지 누락
  grounding  → 사실 목록에 근거 없는 주장·숫자
  semantic   → LLM 심사 (여기서만 모델을 쓴다. 결과는 '후보' 이고 코드가 검증한다)
  failsafe   → 어느 단계가 실패해도 승인으로 흐르지 않게 하는 실패 분류·대체 경로

입구: `input_guard.intercept()` (광고주 요청), `output_guard.check()` (생성된 소재).
"""
