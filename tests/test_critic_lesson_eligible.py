from agents.critic_nodes import _compute_lesson_eligible


def test_lesson_eligible_true_when_score_low_and_feedback_nonempty():
    assert _compute_lesson_eligible(score=60, feedback="needs validation") is True
    assert _compute_lesson_eligible(score=84, feedback="x") is True
    assert _compute_lesson_eligible(score=0, feedback="complete failure") is True


def test_lesson_eligible_false_when_score_high():
    assert _compute_lesson_eligible(score=85, feedback="good") is False
    assert _compute_lesson_eligible(score=100, feedback="perfect") is False


def test_lesson_eligible_false_when_feedback_empty():
    assert _compute_lesson_eligible(score=40, feedback="") is False
    assert _compute_lesson_eligible(score=40, feedback="   ") is False
    assert _compute_lesson_eligible(score=40, feedback=None) is False
