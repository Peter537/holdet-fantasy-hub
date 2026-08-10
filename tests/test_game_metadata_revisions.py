from __future__ import annotations

from datetime import datetime, timedelta, timezone

from holdet_lib import (
    GameMetadata,
    GameMetadataStore,
    GameUrl,
    ScheduleRound,
    compare_game_metadata,
)


def _metadata(*, fetched_at: datetime, close_offset: int = 2, salary: int = 100) -> GameMetadata:
    game = GameUrl(
        "https://www.holdet.dk/da/fantasy/demo",
        "da",
        "demo",
    )
    return GameMetadata(
        game=game,
        variant="soccer",
        format="classic",
        game_id=7,
        salary_cap=salary,
        final_round=3,
        display_name="Demo",
        rounds=(
            ScheduleRound(
                1,
                fetched_at - timedelta(days=1),
                fetched_at + timedelta(hours=close_offset),
                fetched_at + timedelta(days=1),
            ),
        ),
        fetched_at=fetched_at,
    )


def test_metadata_store_preserves_baseline_and_structured_changes(tmp_path) -> None:
    first_at = datetime(2026, 8, 9, 10, tzinfo=timezone.utc)
    first = _metadata(fetched_at=first_at)
    second = _metadata(
        fetched_at=first_at + timedelta(hours=1),
        close_offset=4,
        salary=120,
    )
    store = GameMetadataStore(tmp_path)

    store.save(first)
    store.save(second)

    revisions, warnings = store.revisions(first.game)
    assert not warnings
    assert [item.metadata.fetched_at for item in revisions] == [
        second.fetched_at,
        first.fetched_at,
    ]
    assert revisions[-1].changes == ()
    assert {(item.kind, item.field) for item in revisions[0].changes} == {
        ("game", "salary_cap"),
        ("schedule", "start"),
        ("schedule", "close"),
        ("schedule", "end"),
    }
    assert store.load(first.game) == second


def test_compare_game_metadata_ignores_fetch_timestamp() -> None:
    first_at = datetime(2026, 8, 9, 10, tzinfo=timezone.utc)
    first = _metadata(fetched_at=first_at)
    later = _metadata(fetched_at=first_at + timedelta(hours=1))

    # Schedule timestamps are intentionally tied to fetched_at in this fixture.
    fields = {item.field for item in compare_game_metadata(first, later)}
    assert fields == {"start", "close", "end"}


def test_first_revision_after_upgrade_establishes_baseline_without_change_event(
    tmp_path,
) -> None:
    first_at = datetime(2026, 8, 9, 10, tzinfo=timezone.utc)
    first = _metadata(fetched_at=first_at)
    changed = _metadata(
        fetched_at=first_at + timedelta(hours=1),
        close_offset=4,
        salary=120,
    )
    store = GameMetadataStore(tmp_path)
    store.save(first)
    revision_dir = tmp_path / "revisions" / "da--demo"
    for path in revision_dir.glob("metadata-*.json"):
        path.unlink()

    store.save(changed)

    revisions, warnings = store.revisions(first.game)
    assert not warnings
    assert len(revisions) == 2
    assert all(revision.changes == () for revision in revisions)


def test_explicit_save_recovers_a_corrupt_canonical_metadata_file(tmp_path) -> None:
    metadata = _metadata(
        fetched_at=datetime(2026, 8, 9, 10, tzinfo=timezone.utc)
    )
    current_path = tmp_path / "da--demo.json"
    current_path.write_text("{not valid json", encoding="utf-8")
    store = GameMetadataStore(tmp_path)

    saved = store.save(metadata)

    assert saved == metadata
    assert store.load(metadata.game) == metadata
    revisions, warnings = store.revisions(metadata.game)
    assert not warnings
    assert len(revisions) == 1
    assert revisions[0].changes == ()
