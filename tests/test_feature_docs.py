"""docs/FEATURES.md: 숫자 표가 저장된 평가 결과와 같고, 한계 문장이 빠지지 않았으며, 운영 경험이 있는 것처럼 읽히는 말이 없다."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "FEATURES.md").read_text(encoding="utf-8")


def test_generated_tables_match_the_stored_reports():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_feature_docs.py"), "--check"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_generated_block_is_filled_with_a_table():
    blocks = re.findall(r"<!-- generated:(\w+):start -->\n(.*?)<!-- generated:\1:end -->", DOC, re.S)
    assert {k for k, _ in blocks} == {"dialogs", "urls", "images", "quality", "insights"}
    for key, body in blocks:
        assert body.startswith("| 지표 | 개발용 | 최종 확인용 |") and body.count("\n") >= 5, key


def test_the_limits_that_must_always_travel_with_the_numbers_are_present():
    must = [
        "실제 AI 모델로 이 새 기능들을 잰 것은 **하나도 없습니다**",          # 모델 경로는 측정하지 않았다
        "독립 검증은 없습니다",
        "실제 비전 모델이 글자를 얼마나 정확히 읽는지는 측정하지 않았습니다",
        "실제 클릭률·전환·광고주 만족과 얼마나 같이 움직이는지는 측정하지 않았습니다",
        "AI 가 쓴 보고서 문장의 품질은 측정하지 않았고",
        "이 프로젝트가 가정한 값",
        "독립 라벨러가 없습니다",
        "실제 서비스의 운영 데이터가 없습니다",
        "실제 운영 효과가 아닙니다",
        "구현하지 않은 설계",
        "이미 확정된 값은 조용히 바뀌지 않고",
    ]
    missing = [m for m in must if m not in DOC]
    assert not missing, missing


def test_nothing_reads_as_production_experience():
    banned = ["운영해 본 경험", "운영한 경험", "서비스를 운영했", "실제 광고주에게", "실서비스에서", "프로덕션에서 운영했", "트래픽을 처리했"]
    assert not [b for b in banned if b in DOC]


def test_the_known_bad_holdout_results_are_stated_not_hidden():
    assert "이상이 없는 대조 4개 중 **2개를 이상이 있다고 잘못 알림**" in DOC  # 처음 저장한 성과 리포트 평가
    assert "바꿔 쓴 지시문 2개가 사실 후보로 새어 듦" in DOC        # 상품 페이지 최종 확인용
    assert "tests/test_conv_known_gaps.py" in DOC               # 대화형 알려진 구멍


def test_the_readme_states_the_real_failure_mode_count():
    from guardrails.failure_modes import FAILURE_MODES

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    core = sum(1 for f in FAILURE_MODES if getattr(f, "feature", "core") == "core")
    assert f"{len(FAILURE_MODES)}개 실패 모드(핵심 심사 {core}개 + 기능별 {len(FAILURE_MODES) - core}개)" in readme


def test_local_links_and_images_in_the_readme_and_features_doc_exist():
    for name in ("README.md", "docs/FEATURES.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        base = (ROOT / name).parent
        for m in re.finditer(r"\]\((?!https?:|#|mailto:)([^)\s#]+)", text):
            assert (base / m.group(1)).exists(), (name, m.group(1))
