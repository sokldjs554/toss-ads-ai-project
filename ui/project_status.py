"""Public evidence summary. No credentials, database access or model calls."""
import streamlit as st

STATUS_URL = "https://github.com/sokldjs554/toss-ads-ai-project/blob/main/docs/CURRENT_STATUS.md"


def render():
    with st.expander("확인된 사용·검증 범위"):
        st.markdown("**2026-10-24 기록:** 본인 1명 · LIVE 요청 6건 · 판단 저장 6건(승인 3·수정 필요 3). 합성 광고의 단기 파일럿입니다.")
        st.write("RAG 근거 검토와 실제 check_policy 도구 호출 경로를 사용했습니다. 본인 계정 API 10회가 성공했으며, 이 확인은 정책 정확도 평가와 다릅니다.")
        st.caption("모델이 지적한 누락 조건이 불명확한 사례와 정책 위반·지속 하락을 놓친 사례가 남아 있습니다. 인용 일치가 의미의 타당성을 보장하지는 않습니다.")
        st.caption("본인 사용 근거는 3f78335 시점입니다. 화면 상단 빌드는 현재 배포 코드이며, 안내 갱신은 새 모델 실험이 아닙니다.")
        st.markdown(f"[현재 상태·원자료·남은 한계]({STATUS_URL})")
