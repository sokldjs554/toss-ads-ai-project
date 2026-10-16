from agents.audience import summarize
from core.llm_schemas import AudienceOut, PersonaFeedbackOut


def fb(pid, a, t, c=4, click=True):
    return PersonaFeedbackOut(persona_id=pid, appeal=a, trust=t, clarity=c, would_click=click, concern="x", quote="q")


def test_sycophancy_flag():
    out = AudienceOut(feedbacks=[fb("p1", 5, 5), fb("p2", 5, 5), fb("p3", 5, 5), fb("p4", 5, 5)], summary="s")
    rep = summarize("v", out)
    assert rep.sycophancy_flag and rep.diversity == 0.0


def test_diverse_panel_not_flagged():
    out = AudienceOut(feedbacks=[fb("p1", 4, 3), fb("p2", 5, 4), fb("p3", 2, 1, click=False), fb("p4", 4, 2)], summary="s")
    rep = summarize("v", out)
    assert not rep.sycophancy_flag and rep.click_rate == 0.75


def test_unknown_persona_dropped_and_scores_clamped():
    out = AudienceOut(feedbacks=[fb("p1", 9, 0), fb("ghost", 3, 3)], summary="s")
    rep = summarize("v", out)
    assert len(rep.feedbacks) == 1 and rep.feedbacks[0].appeal == 5 and rep.feedbacks[0].trust == 1
