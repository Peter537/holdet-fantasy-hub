from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from holdet_lib.errors import PayloadError
from holdet_lib.rule_pages import (
    RulePageStore, fetch_rule_page, parse_rule_document, validate_capture,
)
from holdet_lib.rules import AUDITED_RULE_PROFILES, game_rule_from_dict, game_rule_to_dict, rule_profile_for_game
from holdet_lib.transfers import simulate_transfers, TransferScenario
from holdet_lib.decision_analysis import build_captain_analysis
from tests.test_decision_analysis import GAME, NOW, roster, summary, team_snapshot, verified_rules
from tests.rule_page_fixtures import DOCUMENT, HTML, capture


def test_synthetic_document_extracts_hidden_and_native_sections_without_scripts():
    result = parse_rule_document(HTML)
    assert result["section_count"] == 3
    assert result["table_count"] == 2
    assert result["extraction_complete"] and not result["missing"]
    sections = {item["heading"]: item["text"] for item in result["sections"]}
    assert "17 example minutes" in sections["Participation example"]
    assert "æøå" in sections["Participation example"]
    assert "two blue tokens" in sections["Transfer example"]
    assert "nested content" in sections["Native example"]
    assert result["tables"] == [
        [["Invented event", "Example score"], ["Blue token", "11"]],
        [["Orange token", "-3"]],
    ]
    assert "ignored-style-marker" not in result["text"]
    assert "ignoredScriptMarker" not in result["text"]


def test_game_profiles_start_unverified_without_shipped_season_rules():
    assert AUDITED_RULE_PROFILES == ()
    profile = rule_profile_for_game(GAME, game_id=42, salary_cap=12_345, label="Synthetic game")
    assert profile.identity == (GAME.locale.casefold(), GAME.slug)
    assert profile.game_id == 42 and profile.label == "Synthetic game"
    assert profile.salary_cap == 12_345 and profile.budget_enabled is True
    assert not profile.verified and profile.roster_size is None and not profile.position_limits
    assert profile.source_url is None and profile.document_url is None
    assert profile.source_sha256 is None and profile.accessed_on is None
    assert profile.interest(123) is None and profile.transfer_fee(123) is None


@pytest.mark.parametrize("field", ["bank_interest_rounding", "transfer_fee_rounding"])
@pytest.mark.parametrize("value", [[], {}, True, 1, "unknown"])
def test_malformed_rounding_is_a_validation_error_for_json_and_direct_profiles(field, value):
    raw = game_rule_to_dict(verified_rules())
    raw[field] = value
    with pytest.raises(ValueError, match="afrundingsregel"):
        game_rule_from_dict(raw)
    with pytest.raises(ValueError, match="afrundingsregel"):
        replace(verified_rules(), **{field: value})


def test_captured_text_is_not_automatically_an_audited_rule_profile(tmp_path):
    value = capture()
    assert value["rules_verified"] is False and value["document_url"] == DOCUMENT
    store = RulePageStore(tmp_path)
    before = list(tmp_path.iterdir())
    assert store.latest(GAME) is None and store.profile(GAME) is None
    assert list(tmp_path.iterdir()) == before
    store.save(GAME, value)
    assert store.latest(GAME) == value
    assert store.profile(GAME) is None


def test_capture_order_uses_instants_in_legacy_files_across_dst_fallback(tmp_path):
    store = RulePageStore(tmp_path)
    old = capture(now=datetime.fromisoformat("2026-10-25T02:50:00+02:00"))
    store.audit(GAME, old, game_rule_to_dict(verified_rules(sport="soccer")), "Reviewed rules")
    new = capture(HTML + "<!-- changed -->", now=datetime.fromisoformat("2026-10-25T02:10:00+01:00"))
    for value in (old, new):
        path = store.save(GAME, value)
        legacy = path.with_name("capture-" + datetime.fromisoformat(value["fetched_at"]).strftime("%Y%m%dT%H%M%S%f")
                                + "-" + value["source_sha256"][:12] + ".json")
        if path != legacy:
            path.rename(legacy)
    assert store.latest(GAME) == new
    assert store.profile(GAME) is None


def test_audit_order_uses_reviewed_instants_in_legacy_files_across_dst_fallback(tmp_path):
    store = RulePageStore(tmp_path)
    value = capture()
    profile = store.audit(GAME, value, game_rule_to_dict(verified_rules(sport="soccer")), "Reviewed rules")
    for path in tmp_path.rglob("audit-*.json"):
        path.unlink()
    for stamp, label in (("2026-10-25T02:50:00+02:00", "older"), ("2026-10-25T02:10:00+01:00", "newer")):
        name = "audit-" + datetime.fromisoformat(stamp).strftime("%Y%m%dT%H%M%S%f") + ".json"
        path = next(tmp_path.rglob("capture-*.json")).with_name(name)
        path.write_text(json.dumps({"schema_version": 1, "reviewed_at": stamp,
                                   "review_note": "Reviewed rules",
                                   "profile": game_rule_to_dict(replace(profile, label=label))}), encoding="utf-8")
    assert store.profile(GAME).label == "newer"


def test_reviewed_profile_requires_complete_matching_evidence_and_invalidates_on_change(tmp_path):
    store, value = RulePageStore(tmp_path), capture()
    profile = store.audit(GAME, value, game_rule_to_dict(verified_rules(sport="soccer")),
                          "Reviewed roster, finance, contract and captain sections")
    assert store.profile(GAME) == profile
    changed = capture(HTML.replace(">11<", ">12<", 1), now=NOW + timedelta(days=1))
    store.save(GAME, changed)
    assert store.profile(GAME) is None
    assert len(list(tmp_path.rglob("capture-*.json"))) == 2
    assert len(list(tmp_path.rglob("audit-*.json"))) == 1


def test_changed_evidence_cannot_be_relabelled_or_used_without_a_note(tmp_path):
    store, value = RulePageStore(tmp_path), capture()
    with pytest.raises(PayloadError, match="another game"):
        validate_capture({**value, "game": {"locale": "da", "slug": "other"}}, GAME)
    with pytest.raises(PayloadError, match="fingerprint"):
        validate_capture({**value, "html": HTML + "changed"}, GAME)
    with pytest.raises(PayloadError, match="extraction"):
        validate_capture({**value, "text": "made up"}, GAME)
    with pytest.raises(PayloadError, match="review note"):
        store.audit(GAME, value, game_rule_to_dict(verified_rules()), "")
    assert not list(tmp_path.iterdir())


def test_external_iframes_are_not_fetched_and_incomplete_sections_remain_unverified():
    calls = []
    def fetch(url):
        calls.append(url)
        return '<iframe src="https://example.test/private"></iframe>'
    with pytest.raises(PayloadError, match="browser"):
        fetch_rule_page(GAME, fetch_text=fetch)
    assert len(calls) == 1
    result = parse_rule_document(
        '<table><tr><td>score</td><td>1</td></tr></table>'
        '<div class="collapsible-panel"><div class="collapsible">Footnote</div>'
        '<div class="collapsible-content"></div></div>' + "<p>rules </p>" * 80
    )
    assert not result["extraction_complete"] and result["missing"] == ["empty_section:1"]


def test_initial_round_transfer_exemption_and_later_fee_use_integer_arithmetic():
    rules = verified_rules(sport="soccer", free_through_round=1, fee_free_through_round=1)
    current = (roster(1, 0, role="captain"), roster(2, 0))
    from tests.test_decision_analysis import entry
    scenario = TransferScenario(current, (entry(3, 0),), (2,), (3,), 0, 3, 1, 1, 1, (1,))
    initial = simulate_transfers(rules.to_transfer_profile(), scenario)
    assert initial.status == "valid" and initial.transfer_fee == 0
    assert initial.ending_bank == 0 and initial.contracts_used == 0
    later = simulate_transfers(rules.to_transfer_profile(), replace(scenario, target_round=2))
    assert later.status == "invalid" and later.transfer_fee == 10_000
    assert later.contracts_used == 1
    large = 9_007_199_254_740_993
    large_rules = replace(rules, position_limits=(), max_from_team=None)
    result = simulate_transfers(large_rules.to_transfer_profile(), replace(
        scenario, available_players=(entry(3, 0, value=large),),
        starting_bank=large * 2, target_round=2,
    ))
    assert result.transfer_fee == (large + 99) // 100


def test_motor_captain_must_be_a_driver_and_positive_bonus_has_a_floor():
    from holdet_lib.transfers import MOTOR_RULES
    positions = ("Kører",) * 4 + ("Konstruktør",) * 2 + ("Pitcrew",)
    players = tuple(replace(roster(index + 1, 0), position=position)
                    for index, position in enumerate(positions))
    profile = replace(MOTOR_RULES, captain_count=1, captain_positions=("driver",))
    scenario = TransferScenario(players, (), starting_bank=0, captain_player_ids=(7,))
    assert simulate_transfers(profile, scenario).status == "invalid"
    assert simulate_transfers(profile, replace(scenario, captain_player_ids=(1,))).status == "valid"
    current = team_snapshot(1, 1, (roster(1, -10, role="captain"), roster(2, 5)),
                            (summary(1, 0, captain_bonus=0),))
    result = build_captain_analysis(current, current.team.history[0],
                                   verified_rules(captain_positive_only=True))
    assert not result.mismatch and result.computed_actual_bonus == 0
    assert sorted(row.bonus for row in result.alternatives) == [0, 5]
