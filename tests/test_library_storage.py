from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

import holdet_lib as holdet


@contextmanager
def temporary_directory():
    root = Path(__file__).parent / f"_test-library-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def sample_team(
    team_id: int = 101,
    *,
    name: str = "Nordlys Ægir",
    slug: str = "tour-de-france-2026",
    current_round: int = 3,
    total: int = 350,
    change: int = 30,
    unit: str = "money",
    history_rounds: tuple[int, ...] | None = None,
) -> holdet.ScrapedTeam:
    game_url = f"https://www.holdet.dk/da/fantasy/{slug}"
    game = holdet.GameUrl(game_url, "da", slug)
    reference = holdet.TeamReference(
        game,
        team_id,
        name,
        f"{game_url}/fantasyteams/{team_id}",
        account_key=f"account-{team_id}",
        account_label=f"Manager {team_id}",
        account_user_id=team_id + 1000,
    )
    rounds = history_rounds or (current_round,)
    history = tuple(
        holdet.RoundSummary(
            round_number=round_number,
            total=total - (current_round - round_number) * 10,
            change=change - (current_round - round_number),
            bank=50 if unit == "money" else None,
            player_value=(total - (current_round - round_number) * 10 - 50)
            if unit == "money"
            else None,
            bank_change=2 if unit == "money" else None,
            interest=1 if unit == "money" else None,
            player_change=20,
            transfer=0 if unit == "money" else None,
            captain_bonus=5,
            special_bonus=3,
            substitutions_used=round_number,
            round_rank=10 + round_number,
            overall_rank=20 + round_number,
        )
        for round_number in sorted(rounds, reverse=True)
    )
    latest = history[0]
    roster = (
        holdet.RosterEntry(
            0,
            team_id * 10,
            "Søren Ægir",
            "Team Ø",
            "Rytter",
            latest.player_value or latest.total,
            latest.change,
            100,
            1,
            "captain",
            is_injured=True,
        ),
    )
    overview = holdet.TeamOverview(
        current_round=current_round,
        unit=unit,
        player_value=latest.player_value,
        bank=latest.bank,
        total=latest.total,
        current_change=latest.change,
        rank=latest.overall_rank,
        rank_change=2,
        top_percent=None,
        substitutions_remaining=2,
        substitutions_limit=8,
        substitutions_used=latest.substitutions_used,
    )
    return holdet.ScrapedTeam(
        reference,
        "golf" if unit == "points" else "cycling",
        7,
        name,
        f"Manager {team_id}",
        team_id + 1000,
        overview,
        roster,
        history,
    )


class ClientAndSerializationTests(unittest.TestCase):
    def test_high_level_player_fetch_has_no_filesystem_side_effects(self) -> None:
        root_html = '<script>self.__next_f.push([1,"1:{\\"variant\\":\\"soccer\\"}\\n"])</script>'
        fixture = (Path(__file__).parent / "fixtures" / "soccer.html").read_text(
            encoding="utf-8"
        )
        game_url = "https://www.holdet.dk/da/fantasy/super-manager-fall-2026"
        cartridge = {
            "gameId": 7,
            "_embedded": {
                "games": {"7": {"id": 7, "rulesetId": 8}},
                "rulesets": {
                    "8": {
                        "id": 8,
                        "salaryCap": 50_000_000,
                        "properties": {"Format": "soccer"},
                    }
                },
            },
        }
        client = holdet.HoldetClient(
            text_fetcher=lambda url: root_html if url.endswith("2026") else fixture,
            json_fetcher=lambda url: cartridge,
        )
        with temporary_directory() as temporary:
            before = tuple(temporary.rglob("*"))
            result = client.fetch_players(game_url)
            after = tuple(temporary.rglob("*"))
        self.assertEqual(result.variant, "soccer")
        self.assertTrue(result.entries)
        self.assertEqual(before, after)

    def test_unicode_schema_one_round_trip(self) -> None:
        team = sample_team()
        generated = datetime(2026, 7, 25, 12, 3, 4, tzinfo=timezone.utc)
        payload = json.loads(holdet.team_to_json(team, generated_at=generated))
        restored = holdet.team_from_dict(payload)
        self.assertEqual(restored, team)
        self.assertEqual(restored.roster[0].name, "Søren Ægir")
        with self.assertRaises(holdet.PayloadError):
            holdet.team_from_dict({"schema_version": 2})


class SnapshotStorageTests(unittest.TestCase):
    def test_explicit_json_storage_collision_and_index_round_trip(self) -> None:
        fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with temporary_directory() as temporary:
            store = holdet.SnapshotStore(temporary)
            first = store.save_team_json(sample_team(current_round=0), now=fixed)
            second = store.save_team_json(sample_team(current_round=0), now=fixed)
            index = store.scan()
            self.assertEqual(first.name, "team-round0_0102_040405.json")
            self.assertEqual(second.name, "team-round0_0102_040405_1.json")
            self.assertEqual(len(index.snapshots), 2)
            self.assertEqual(index.snapshots[0].team.team_name, "Nordlys Ægir")
            self.assertFalse(list(temporary.rglob("*.txt")))

    def test_indexes_identical_slugs_independently_by_locale(self) -> None:
        danish = sample_team(team_id=101, total=100)
        english_game = holdet.GameUrl(
            original="https://www.holdet.dk/en/fantasy/tour-de-france-2026",
            locale="en",
            slug=danish.reference.game.slug,
        )
        english = replace(
            sample_team(team_id=101, total=900),
            reference=replace(danish.reference, game=english_game),
        )
        index = holdet.SnapshotIndex((
            holdet.TeamSnapshot(
                Path("en.json"), datetime(2026, 7, 26, tzinfo=timezone.utc), english
            ),
            holdet.TeamSnapshot(
                Path("da.json"), datetime(2026, 7, 25, tzinfo=timezone.utc), danish
            ),
        ))

        self.assertEqual(index.newest(danish.reference.game, 101).team.overview.total, 100)
        self.assertEqual(index.newest(english_game, 101).team.overview.total, 900)
        self.assertEqual(
            set(index.identities),
            {("da", "tour-de-france-2026", 101), ("en", "tour-de-france-2026", 101)},
        )
    def test_corrupt_snapshots_warn_and_do_not_break_valid_cache(self) -> None:
        with temporary_directory() as temporary:
            store = holdet.SnapshotStore(temporary)
            store.save_team_json(sample_team())
            corrupt = temporary / "bad" / "team-round3_0000_000000.json"
            corrupt.parent.mkdir()
            corrupt.write_text("{bad", encoding="utf-8")
            index = store.scan()
            self.assertEqual(len(index.snapshots), 1)
            self.assertEqual(len(index.warnings), 1)

    def test_new_history_backfills_summaries_but_rosters_require_exact_round(self) -> None:
        older_team = sample_team(current_round=3, history_rounds=(3, 2, 1))
        newer_team = sample_team(current_round=6, history_rounds=(6, 5, 4, 3, 2, 1))
        older = holdet.TeamSnapshot(
            Path("older.json"), datetime(2026, 7, 20, tzinfo=timezone.utc), older_team
        )
        newer = holdet.TeamSnapshot(
            Path("newer.json"), datetime(2026, 7, 25, tzinfo=timezone.utc), newer_team
        )
        index = holdet.SnapshotIndex((newer, older))
        self.assertIs(index.summary_for(older_team.reference.game.slug, 101, 5)[0], newer)
        self.assertIs(index.roster_for(older_team.reference.game.slug, 101, 3), older)
        self.assertIsNone(index.roster_for(older_team.reference.game.slug, 101, 5))


class GroupTests(unittest.TestCase):
    def test_atomic_group_config_deduplication_edit_delete_and_cross_game_rejection(self) -> None:
        team = sample_team()
        member = holdet.GroupTeam(
            team.reference.team_id,
            team.team_name,
            team.reference.source_url,
        )
        with temporary_directory() as temporary:
            path = temporary / "config" / "groups.json"
            store = holdet.GroupStore(path)
            group = store.create(
                "Tour venner", team.reference.game, (member, member), group_id="tour-venner"
            )
            self.assertEqual(len(group.teams), 1)
            self.assertFalse(list(path.parent.glob("*.tmp")))
            store.update(GroupDefinition := holdet.GroupDefinition(
                group.group_id, "Nyt navn", group.game, group.teams
            ))
            self.assertEqual(store.load()[0].name, "Nyt navn")
            wrong = sample_team(team_id=202, slug="golf-manager-2026")
            with self.assertRaisesRegex(holdet.PayloadError, "belongs to"):
                store.update(
                    holdet.GroupDefinition(
                        group.group_id,
                        group.name,
                        group.game,
                        (
                            holdet.GroupTeam(
                                wrong.reference.team_id,
                                wrong.team_name,
                                wrong.reference.source_url,
                            ),
                        ),
                    )
                )
            store.delete(group.group_id)
            self.assertEqual(store.load(), ())

    def test_current_membership_competition_ties_missing_and_golf_points(self) -> None:
        first = sample_team(1, name="Zulu", total=500, change=30)
        second = sample_team(2, name="Alpha", total=500, change=30)
        missing = sample_team(3, name="Missing")
        snapshots = holdet.SnapshotIndex(
            (
                holdet.TeamSnapshot(Path("a"), datetime(2026, 7, 25, tzinfo=timezone.utc), first),
                holdet.TeamSnapshot(Path("b"), datetime(2026, 7, 25, tzinfo=timezone.utc), second),
            )
        )
        members = tuple(
            holdet.GroupTeam(team.reference.team_id, team.team_name, team.reference.source_url)
            for team in (first, second, missing)
        )
        group = holdet.GroupDefinition("friends", "Friends", first.reference.game, members)
        rows = holdet.build_standings(group, snapshots, 3, "overall")
        self.assertEqual([(row.team_name, row.rank) for row in rows], [("Alpha", 1), ("Zulu", 1), ("Missing", None)])
        self.assertEqual(rows[0].distance, 0)
        golf = sample_team(9, slug="golf-manager-2026", total=42, change=8, unit="points")
        golf_index = holdet.SnapshotIndex((holdet.TeamSnapshot(Path("g"), datetime.now(timezone.utc), golf),))
        golf_group = holdet.GroupDefinition(
            "golf", "Golf", golf.reference.game,
            (holdet.GroupTeam(9, golf.team_name, golf.reference.source_url),),
        )
        golf_row = holdet.build_standings(golf_group, golf_index, 3, "round")[0]
        self.assertEqual((golf_row.total, golf_row.value), (42, 8))

    def test_standings_modes_keep_total_and_rank_by_active_measure(self) -> None:
        overall_leader = sample_team(1, name="Overall", total=500, change=10)
        round_leader = sample_team(2, name="Round", total=400, change=30)
        snapshots = holdet.SnapshotIndex(
            (
                holdet.TeamSnapshot(
                    Path("overall"), datetime.now(timezone.utc), overall_leader
                ),
                holdet.TeamSnapshot(
                    Path("round"), datetime.now(timezone.utc), round_leader
                ),
            )
        )
        group = holdet.GroupDefinition(
            "modes",
            "Modes",
            overall_leader.reference.game,
            tuple(
                holdet.GroupTeam(
                    team.reference.team_id, team.team_name, team.reference.source_url
                )
                for team in (overall_leader, round_leader)
            ),
        )

        overall = holdet.build_standings(group, snapshots, 3, "overall")
        self.assertEqual(
            [
                (row.team_name, row.rank, row.total, row.change, row.value, row.distance)
                for row in overall
            ],
            [
                ("Overall", 1, 500, 10, 500, 0),
                ("Round", 2, 400, 30, 400, -100),
            ],
        )

        round_rows = holdet.build_standings(group, snapshots, 3, "round")
        self.assertEqual(
            [
                (row.team_name, row.rank, row.total, row.change, row.value, row.distance)
                for row in round_rows
            ],
            [
                ("Round", 1, 400, 30, 30, 0),
                ("Overall", 2, 500, 10, 10, -20),
            ],
        )

    def test_partial_refresh_saves_success_and_cached_fallback_manifest(self) -> None:
        fresh = sample_team(1, name="Fresh")
        cached = sample_team(2, name="Cached")
        group = holdet.GroupDefinition(
            "tour-pals",
            "Tour pals",
            fresh.reference.game,
            (
                holdet.GroupTeam(1, fresh.team_name, fresh.reference.source_url),
                holdet.GroupTeam(2, cached.team_name, cached.reference.source_url),
            ),
        )

        class FakeClient:
            def fetch_team(self, reference):
                if reference.team_id == 2:
                    raise holdet.FetchError("offline")
                return fresh

        fixed = datetime(2026, 7, 25, 12, 34, 56, tzinfo=timezone.utc)
        with temporary_directory() as temporary:
            store = holdet.SnapshotStore(temporary / "snapshots")
            manifests = holdet.ManifestStore(temporary / "manifests")
            store.save_team_json(cached, now=datetime(2026, 7, 24, tzinfo=timezone.utc))
            result = holdet.refresh_group(
                group, FakeClient(), store, manifests, now=fixed
            )
            self.assertEqual([item.status for item in result.teams], ["success", "cached_fallback"])
            self.assertEqual(
                result.manifest_path.name,
                "refresh-round3_0725_143456.json",
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["teams"][1]["status"], "cached_fallback")
            repeated = holdet.refresh_group(
                group, FakeClient(), store, manifests, now=fixed
            )
            self.assertEqual(repeated.manifest_path.name, "refresh-round3_0725_143456_1.json")


if __name__ == "__main__":
    unittest.main()
