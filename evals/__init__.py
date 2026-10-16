"""평가 스위트.

datasets/   라벨된 사례 (JSONL). 같은 사람이 규칙과 라벨을 썼다는 한계를 README 에 적는다.
metrics.py  위험 승인율·오탐율·규칙별 P/R·보정(ECE)·고신뢰 오류율·커버리지 곡선
runner.py   사례 → 가드레일(+심사 모델) → 예측. 모드(demo/live)를 결과에 기록한다 (FM-22)
regression.py  기준선(baseline)과 비교해 게이트를 통과/실패시킨다 (CI 가 돌린다)
redteam.py  공격자 모델이 위반 소재를 변형 → 방어선 통과율 측정 → 뚫린 사례를 회귀셋에 수확
judge.py    쌍대 품질 비교 + 위치 뒤집기로 위치 편향 측정
"""
