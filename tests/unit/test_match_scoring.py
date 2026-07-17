from backend.app.services.match_scoring import compute_score, SCORING_RULE_VERSION


def test_perfect_match_scores_100():
    from backend.app.services.match_scoring import ScoreComponent
    output = type("obj", (), {
        "strengths": [type("a", (), {"requirement_id": "r1"})()] * 5,
        "gaps": [],
        "unknowns": [],
    })()
    score, components, priority = compute_score(output)
    assert score == 100
    assert priority == "high"
    assert sum(c.weight_basis_points for c in components) == 10000


def test_all_gaps_scores_0():
    output = type("obj", (), {
        "strengths": [],
        "gaps": [type("a", (), {"requirement_id": "r1"})()] * 5,
        "unknowns": [],
    })()
    score, components, priority = compute_score(output)
    assert score == 0
    assert priority == "not_recommended"


def test_deterministic():
    output = type("obj", (), {
        "strengths": [type("a", (), {"requirement_id": "r1"})()],
        "gaps": [type("a", (), {"requirement_id": "r2"})()],
        "unknowns": [type("a", (), {"requirement_id": "r3"})()],
    })()
    s1, _, _ = compute_score(output)
    s2, _, _ = compute_score(output)
    assert s1 == s2
