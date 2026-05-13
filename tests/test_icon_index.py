from __future__ import annotations

from backend.generator import icon_index


def test_icon_lexical_boost_rewards_name_and_tag_matches() -> None:
    query = "target objective metric callout"
    target_icon = {
        "name": "target",
        "category": "metric",
        "tags": ["objective", "goal"],
        "text": "visual metaphor: target",
    }
    unrelated_icon = {
        "name": "calendar",
        "category": "time",
        "tags": ["date"],
        "text": "visual metaphor: calendar",
    }

    assert icon_index._lexical_boost(query, target_icon) > icon_index._lexical_boost(
        query,
        unrelated_icon,
    )
