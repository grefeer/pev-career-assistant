from dataclasses import dataclass

SCORING_RULE_VERSION = "1.0"


@dataclass
class ScoreComponent:
    requirement_id: str
    weight_basis_points: int  # sum = 10000
    earned_basis_points: int


def compute_score(assessments) -> tuple[int, list[ScoreComponent], str]:
    """Deterministic scorer. Same input -> same output."""
    total_items = len(assessments.strengths) + len(assessments.gaps) + len(assessments.unknowns)
    if total_items == 0:
        return 0, [], "not_recommended"

    weight_per_item = 10000 // total_items
    remainder = 10000 - (weight_per_item * total_items)

    components = []
    earned = 0

    for req in assessments.strengths:
        w = weight_per_item + (1 if remainder > 0 else 0)
        if remainder > 0:
            remainder -= 1
        components.append(ScoreComponent(req.requirement_id, w, w))
        earned += w

    for req in assessments.gaps:
        w = weight_per_item + (1 if remainder > 0 else 0)
        if remainder > 0:
            remainder -= 1
        components.append(ScoreComponent(req.requirement_id, w, 0))

    for req in assessments.unknowns:
        w = weight_per_item + (1 if remainder > 0 else 0)
        if remainder > 0:
            remainder -= 1
        components.append(ScoreComponent(req.requirement_id, w, 0))

    score = round((earned / 10000) * 100)

    if score >= 75:
        priority = "high"
    elif score >= 40:
        priority = "medium"
    elif score >= 15:
        priority = "low"
    else:
        priority = "not_recommended"

    return score, components, priority
