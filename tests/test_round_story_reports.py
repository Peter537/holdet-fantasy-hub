from __future__ import annotations

from dataclasses import replace

import holdet_lib as holdet
from tests.test_manager_seasons_tournaments import (
    group_for,
    snapshots,
    team_with_rounds,
)


def _complete_story() -> holdet.RoundStory:
    first = team_with_rounds(
        1,
        {1: 10, 2: 40},
        owner_user_id=900,
    )
    second = team_with_rounds(
        2,
        {1: 30, 2: 20},
        owner_user_id=901,
    )
    group = group_for("forklarlig", first, second)
    return holdet.build_round_story(
        (group,),
        snapshots(first, second),
        holdet.HubSettings(),
        group.game.slug,
        2,
    )


def test_round_story_exposes_stable_explainable_facts_and_team_refs() -> None:
    story = _complete_story()

    assert tuple(item.explanation for item in story.facts) == story.paragraphs
    assert tuple(item.kind for item in story.facts) == (
        "round_winner",
        "lead_change",
        "growth_record",
        "comeback",
        "closest_duel",
    )
    assert len({item.fact_id for item in story.facts}) == len(story.facts)
    assert all(item.fact_id.startswith("da:tour-de-france-2026:round:2:") for item in story.facts)
    assert all(item.status == "final" for item in story.facts)
    assert all(item.generated_at is not None for item in story.facts)
    assert story.facts[0].source_rounds == (2,)
    assert story.facts[1].source_rounds == (1, 2)
    assert story.facts[2].source_rounds == (1, 2)
    assert story.facts[0].teams[0].team_id == 1
    assert story.facts[0].teams[0].group_ids == ("forklarlig",)
    assert story.facts[0].teams[0].holdet_url.startswith(
        "https://www.holdet.dk/da/fantasy/tour-de-france-2026/"
    )

    repeated = _complete_story()
    assert tuple(item.fact_id for item in repeated.facts) == tuple(
        item.fact_id for item in story.facts
    )
    corrected_first = team_with_rounds(
        1,
        {1: 10, 2: 15},
        owner_user_id=900,
    )
    corrected_second = team_with_rounds(
        2,
        {1: 30, 2: 50},
        owner_user_id=901,
    )
    corrected_group = group_for(
        "forklarlig",
        corrected_first,
        corrected_second,
    )
    corrected = holdet.build_round_story(
        (corrected_group,),
        snapshots(corrected_first, corrected_second),
        holdet.HubSettings(),
        corrected_group.game.slug,
        2,
    )
    corrected_winner_id = next(
        item.fact_id for item in corrected.facts if item.kind == "round_winner"
    )
    original_winner_id = next(
        item.fact_id for item in story.facts if item.kind == "round_winner"
    )
    assert corrected_winner_id == original_winner_id


def test_round_story_marks_preliminary_and_unavailable_fact_statuses() -> None:
    first = team_with_rounds(1, {1: 10}, owner_user_id=900)
    first = replace(
        first,
        history=tuple(
            replace(item, round_status="in_progress") for item in first.history
        ),
    )
    group = group_for("preview", first)
    preview = holdet.build_round_story(
        (group,),
        snapshots(first),
        holdet.HubSettings(),
        group.game.slug,
        1,
    )
    assert preview.preliminary
    assert preview.facts
    assert all(item.status == "preliminary" for item in preview.facts)
    assert all(item.preliminary for item in preview.facts)

    unavailable = holdet.build_round_story(
        (group,),
        holdet.SnapshotIndex(()),
        holdet.HubSettings(),
        group.game.slug,
        1,
    )
    assert unavailable.facts[0].kind == "data_unavailable"
    assert unavailable.facts[0].status == "unavailable"
    assert unavailable.facts[0].source_rounds == ()
    assert unavailable.facts[0].generated_at is None


def test_round_story_html_escapes_content_and_allows_only_safe_dual_links() -> None:
    story = _complete_story()
    source = story.facts[0].teams[0]
    unsafe_ref = replace(
        source,
        team_name='<img src=x onerror="alert(1)">',
        manager_name="<script>alert(1)</script>",
    )
    fact = replace(
        story.facts[0],
        fact_id='fact"><script>alert(1)</script>',
        label="Vinder <script>alert(1)</script>",
        explanation='<img src=x onerror="alert(1)">',
        teams=(unsafe_ref,),
    )
    selected = replace(
        story,
        headline="Historie <script>alert(1)</script>",
        facts=(fact,),
    )
    hub_url = (
        "http://127.0.0.1:8501/team?group=forklarlig&team=1&round=2"
    )

    html = holdet.render_round_story_html(
        selected,
        title="Delbar <historie>",
        hub_team_urls={1: hub_url},
    )

    assert "<script" not in html.casefold()
    assert "<img" not in html.casefold()
    assert "<link" not in html.casefold()
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x" in html
    assert unsafe_ref.holdet_url in html
    assert "http://127.0.0.1:8501/team?group=forklarlig&amp;team=1&amp;round=2" in html
    assert html.count('rel="noopener noreferrer"') == 2
    assert "Content-Security-Policy" in html
    assert "eksterne assets" in html
    assert "Lokale Hub-links virker kun, mens Hubben kører" in html
    assert "Åbn i lokal Hub" in html
    assert holdet.round_story_html_filename(story) == (
        "rundens-historie-tour-de-france-2026-runde-2.html"
    )
    localhost_html = holdet.render_round_story_html(
        selected,
        hub_team_urls={1: "http://localhost:8501/team?team=1"},
    )
    assert "http://localhost:8501/team?team=1" in localhost_html


def test_round_story_html_rejects_unrelated_holdet_and_non_loopback_urls() -> None:
    story = _complete_story()
    source = story.facts[0].teams[0]
    unsafe_holdet_urls = (
        (
            "https://www.holdet.dk.evil.example/da/fantasy/"
            "tour-de-france-2026/fantasyteams/1"
        ),
        "https://www.holdet.dk/da/fantasy/andet-spil/fantasyteams/1",
    )
    for unsafe_holdet_url in unsafe_holdet_urls:
        unsafe_ref = replace(source, holdet_url=unsafe_holdet_url)
        selected = replace(
            story,
            facts=(replace(story.facts[0], teams=(unsafe_ref,)),),
        )
        html = holdet.render_round_story_html(
            selected,
            hub_team_urls={1: "https://hub.example/team?team=1"},
        )

        assert "evil.example" not in html
        assert "andet-spil" not in html
        assert "hub.example" not in html
        assert "<a " not in html


def test_round_story_keeps_legacy_fields_positional_and_facts_additive() -> None:
    story = holdet.RoundStory(
        "game",
        3,
        "Overskrift",
        ("Afsnit",),
        (),
        False,
        "da",
    )

    assert story.game_slug == "game"
    assert story.paragraphs == ("Afsnit",)
    assert story.facts == ()
    html = holdet.render_round_story_html(story, hub_team_urls={})
    assert "Afsnit" in html
