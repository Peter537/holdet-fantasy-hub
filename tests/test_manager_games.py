from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

import holdet_lib as holdet
from tests.test_library_storage import sample_team


class TemporaryDirectory:
    def __enter__(self) -> Path:
        self.path = Path(__file__).parent / f"_test-manager-games-{uuid4().hex}"
        self.path.mkdir()
        return self.path

    def __exit__(self, *_args) -> None:
        shutil.rmtree(self.path)


class ManagerGameStorageTests(unittest.TestCase):
    def test_legacy_schemas_infer_only_nonempty_games_without_writing(self) -> None:
        team = sample_team(1)
        raw_group = {
            "id": "active",
            "name": "Venner",
            "game": {
                "url": team.reference.game.original,
                "locale": team.reference.game.locale,
                "slug": team.reference.game.slug,
            },
            "teams": [{
                "id": 1,
                "name": team.team_name,
                "source_url": team.reference.source_url,
            }],
        }
        empty = {
            **raw_group,
            "id": "empty",
            "name": "Tom",
            "game": {
                "url": "https://www.holdet.dk/da/fantasy/empty-game",
                "locale": "da",
                "slug": "empty-game",
            },
            "teams": [],
        }
        for schema_version in (1, 2, 3, 4):
            with self.subTest(schema_version=schema_version), TemporaryDirectory() as root:
                path = root / "groups.json"
                original = json.dumps(
                    {"schema_version": schema_version, "groups": [raw_group, empty]},
                    ensure_ascii=False,
                )
                path.write_text(original, encoding="utf-8")
                configuration = holdet.GroupStore(path).load_configuration()
                self.assertEqual(
                    [game.name for game in configuration.games],
                    [team.reference.game.slug],
                )
                self.assertEqual(len(configuration.groups), 2)
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_persistent_empty_game_crud_and_blocked_delete(self) -> None:
        with TemporaryDirectory() as root:
            store = holdet.GroupStore(root / "groups.json")
            manager_game = store.create_manager_game(
                "future-manager-2027", "Fremtidens managerspil"
            )
            self.assertEqual(manager_game.game.locale, "da")
            self.assertEqual(store.load(), ())
            renamed = store.rename_manager_game(manager_game.game, "Nyt navn")
            self.assertEqual(renamed.name, "Nyt navn")
            team = sample_team(1, slug=manager_game.game.slug)
            member = holdet.GroupTeam(1, team.team_name, team.reference.source_url)
            store.create("Liga", manager_game.game, (member,), group_id="liga")
            with self.assertRaisesRegex(holdet.PayloadError, "mens det har grupper"):
                store.delete_manager_game(manager_game.game)
            store.delete("liga")
            store.delete_manager_game(manager_game.game)
            self.assertEqual(store.load_configuration().games, ())

    def test_schema_five_loads_active_without_rewriting(self) -> None:
        with TemporaryDirectory() as root:
            path = root / "groups.json"
            game = sample_team(1).reference.game
            original = json.dumps(
                {
                    "schema_version": 5,
                    "games": [
                        {
                            "name": "Historisk spil",
                            "game": {
                                "url": game.original,
                                "locale": game.locale,
                                "slug": game.slug,
                            },
                        }
                    ],
                    "groups": [],
                },
                ensure_ascii=False,
            )
            path.write_text(original, encoding="utf-8")
            loaded = holdet.GroupStore(path).load_configuration()
            self.assertEqual(len(loaded.games), 1)
            self.assertFalse(loaded.games[0].is_archived)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_archive_restore_round_trip_preserves_order_groups_and_data(self) -> None:
        with TemporaryDirectory() as root:
            path = root / "groups.json"
            store = holdet.GroupStore(path)
            first = store.create_manager_game("first-game", "Første")
            second = store.create_manager_game("second-game", "Andet")
            team = sample_team(1, slug=first.game.slug)
            group = store.create(
                "Liga",
                first.game,
                (holdet.GroupTeam(1, team.team_name, team.reference.source_url),),
                group_id="liga",
            )
            fixed = datetime(2026, 7, 28, 18, 30, tzinfo=timezone.utc)
            archived = store.archive_manager_game(first.game, now=fixed)
            self.assertTrue(archived.is_archived)
            self.assertEqual(archived.archived_at, fixed.isoformat())

            configuration = store.load_configuration()
            self.assertEqual(
                [item.game.slug for item in configuration.games],
                [first.game.slug, second.game.slug],
            )
            self.assertEqual(configuration.groups, (group,))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 8)
            self.assertEqual(payload["games"][0]["archived_at"], fixed.isoformat())
            with self.assertRaisesRegex(holdet.PayloadError, "allerede arkiveret"):
                store.archive_manager_game(first.game)
            with self.assertRaisesRegex(holdet.PayloadError, "ikke arkiveret"):
                store.restore_manager_game(second.game)

            restored = store.restore_manager_game(first.game)
            self.assertFalse(restored.is_archived)
            after = store.load_configuration()
            self.assertEqual(
                [item.game.slug for item in after.games],
                [first.game.slug, second.game.slug],
            )
            self.assertEqual(after.groups, (group,))

    def test_duplicate_identity_and_schema_seven_shape(self) -> None:
        with TemporaryDirectory() as root:
            path = root / "groups.json"
            store = holdet.GroupStore(path)
            created = store.create_manager_game(
                "https://www.holdet.dk/en/fantasy/game-2027/nested"
            )
            self.assertEqual(created.name, "game-2027")
            self.assertEqual(created.game.locale, "en")
            self.assertEqual(
                created.game.original,
                "https://www.holdet.dk/en/fantasy/game-2027",
            )
            with self.assertRaisesRegex(holdet.PayloadError, "findes allerede"):
                store.create_manager_game(created.game, "Duplicate")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 8)
            self.assertEqual(payload["games"][0]["name"], "game-2027")
            self.assertEqual(payload["groups"], [])


class GameRefreshTests(unittest.TestCase):
    def test_deduplicates_active_teams_and_writes_game_manifest(self) -> None:
        first = sample_team(1, name="Fælles")
        second = sample_team(2, name="Andet")
        game = holdet.ManagerGame(first.reference.game, "Tourspillet")
        shared = holdet.GroupTeam(1, first.team_name, first.reference.source_url)
        other = holdet.GroupTeam(2, second.team_name, second.reference.source_url)
        groups = (
            holdet.GroupDefinition("one", "En", game.game, (shared, other)),
            holdet.GroupDefinition("two", "To", game.game, (shared,)),
        )
        fetched: list[int] = []

        class Client:
            def fetch_team(self, reference):
                fetched.append(reference.team_id)
                return first if reference.team_id == 1 else second

        fixed = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as root:
            result = holdet.refresh_game(
                game,
                groups,
                Client(),
                holdet.SnapshotStore(root / "snapshots"),
                holdet.ManifestStore(root / "manifests"),
                now=fixed,
            )
            self.assertEqual(fetched, [1, 2])
            self.assertEqual(result.attempted_team_ids, (1, 2))
            self.assertEqual(result.skipped_team_ids, ())
            self.assertEqual(
                result.manifest_path.parent,
                root / "manifests" / game.game.slug / "game",
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["scope"], "game")
            self.assertEqual(len(manifest["groups"]), 2)

    def test_completed_tournament_fetches_all_members_and_records_champion(self) -> None:
        teams = (
            sample_team(1, name="Alpha", history_rounds=(3, 2, 1)),
            sample_team(2, name="Beta", history_rounds=(3, 2, 1)),
        )
        game = holdet.ManagerGame(teams[0].reference.game, "Tourspillet")
        members = tuple(
            holdet.GroupTeam(
                team.reference.team_id, team.team_name, team.reference.source_url
            )
            for team in teams
        )
        group = holdet.GroupDefinition(
            "cup",
            "Cup",
            game.game,
            members,
            "tournament",
            holdet.create_tournament_config(
                (1, 2), 1, 3, 1, shuffle=lambda values: None
            ),
        )
        fetched: list[int] = []

        class Client:
            def fetch_team(self, reference):
                fetched.append(reference.team_id)
                return teams[reference.team_id - 1]

        with TemporaryDirectory() as root:
            snapshots = holdet.SnapshotStore(root / "snapshots")
            for team in teams:
                snapshots.save_team_json(team)
            result = holdet.refresh_game(
                game,
                (group,),
                Client(),
                snapshots,
                holdet.ManifestStore(root / "manifests"),
            )
            self.assertEqual(fetched, [1, 2])
            self.assertEqual(result.attempted_team_ids, (1, 2))
            self.assertEqual(result.skipped_team_ids, ())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["groups"][0]["phase"], "Afsluttet")
            self.assertIsNotNone(manifest["groups"][0]["champion_id"])
            self.assertEqual(
                set(manifest["groups"][0]["eliminated_team_ids"]),
                {1, 2} - {manifest["groups"][0]["champion_id"]},
            )

    def test_failure_uses_cached_snapshot(self) -> None:
        cached = sample_team(1, name="Cache")
        game = holdet.ManagerGame(cached.reference.game, "Spil")
        member = holdet.GroupTeam(1, cached.team_name, cached.reference.source_url)
        standings = holdet.GroupDefinition("standing", "Liga", game.game, (member,))

        class Client:
            def fetch_team(self, reference):
                raise holdet.FetchError("offline")

        with TemporaryDirectory() as root:
            snapshots = holdet.SnapshotStore(root / "snapshots")
            snapshots.save_team_json(cached)
            result = holdet.refresh_game(
                game,
                (standings,),
                Client(),
                snapshots,
                holdet.ManifestStore(root / "manifests"),
            )
            self.assertEqual(result.attempted_team_ids, (1,))
            self.assertEqual(result.teams[0].status, "cached_fallback")


class DisplayNameTests(unittest.TestCase):
    def test_server_rendered_h1_is_normalized(self) -> None:
        html = "<main><h1>Super Manager <span>Efterår 2026</span></h1></main>"
        self.assertEqual(
            holdet.parse_game_display_name(html), "Super Manager Efterår 2026"
        )
        self.assertIsNone(holdet.parse_game_display_name("<main>Ingen titel</main>"))


if __name__ == "__main__":
    unittest.main()
