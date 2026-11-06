"""Worker-owned catch-up adapter for durable refresh runs.

This module has no Streamlit state; each invocation reloads configuration and
constructs its own client and stores, so reconnecting a browser does not own the
running operation.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

from holdet_lib.analysis_inbox import AnalysisInboxStore
from holdet_lib.client import HoldetClient
from holdet_lib.errors import PayloadError
from holdet_lib.game_metadata import GameMetadataStore
from holdet_lib.groups import GroupStore
from holdet_lib.hall_of_fame import HallOfFameStore, build_live_hall_of_fame_events
from holdet_lib.hub_settings import HubFeature, HubSettingsStore, feature_enabled
from holdet_lib.refresh import RefreshMode, build_refresh_plan, select_recovery_step
from holdet_lib.persistence import replace_text_atomically
from holdet_lib.refresh_runs import RefreshRunCoordinator, RefreshRunResult
from holdet_lib.refresh_transition import PlayerTransition
from holdet_lib.scopes import resolve_scope
from holdet_lib.storage import ManifestStore, PlayerStatisticsStore, SnapshotStore
from holdet_lib.tournament import (
    build_swiss_pairing_conflicts, build_swiss_participants,
    build_tournament_state, generate_swiss_pairings, latest_tournament_round,
    published_knockout_candidates,
)
from holdet_lib.tournament_pairings import TournamentPairing, TournamentPairingStore


def _resolved_group(group, index, pairing_store):
    merged = pairing_store.with_published_matches(group)
    if (merged.tournament is not None and merged.tournament.template == "swiss"
            and build_swiss_pairing_conflicts(merged, index)):
        raise PayloadError(f"{group.name}: publicerede Swiss-parringer er i konflikt")
    return merged


def _baseline_groups(groups, index, pairing_store):
    """Freeze legacy knockout fixtures using the pre-fetch snapshot index."""
    resolved = []
    for group in groups:
        config = group.tournament
        if config is not None and config.template in {"group_knockout", "double_elimination"}:
            if not pairing_store.has_revision(group.group_id, group.active_revision):
                candidates = published_knockout_candidates(
                    group, index, latest_tournament_round(group, index)
                )
                pairing_store.publish_knockout_matches(
                    group.group_id, group.active_revision, candidates,
                    config=config, team_ids=tuple(item.team_id for item in group.teams),
                )
        resolved.append(_resolved_group(group, index, pairing_store))
    return tuple(resolved)


def _postprocess(groups, snapshot_store, pairing_store, metadata_store, settings, hall_store):
    index = snapshot_store.scan()
    safe_groups = []
    errors = []
    for original in groups:
        try:
            group = _resolved_group(original, index, pairing_store)
            config = group.tournament
            if config is not None and config.template == "swiss":
                # Each iteration advances one published round. The configured
                # final round provides a strict upper bound even with bad data.
                for _ in range(config.final_round - config.start_round + 1):
                    config = group.tournament
                    published = max((item.round_number for item in config.group_fixtures), default=config.start_round - 1)
                    if published >= config.final_round:
                        break
                    state = build_tournament_state(group, index, published)
                    matches = tuple(item for item in state.group_matches if item.fixture.round_number == published)
                    if not matches or not all(item.complete for item in matches):
                        break
                    fixtures = generate_swiss_pairings(build_swiss_participants(config, state.group_matches), published + 1)
                    pairings = tuple(TournamentPairing(item.round_number, item.team_a_id, item.team_b_id) for item in fixtures)
                    pairing_store.publish_round(group.group_id, group.active_revision, published + 1, pairings, previous_round_complete=True)
                    group = _resolved_group(original, index, pairing_store)
            elif config is not None and config.template in {"group_knockout", "double_elimination"}:
                candidates = published_knockout_candidates(group, index, latest_tournament_round(group, index))
                pairing_store.publish_knockout_matches(group.group_id, group.active_revision, candidates,
                    config=config, team_ids=tuple(item.team_id for item in group.teams))
                group = _resolved_group(original, index, pairing_store)
            safe_groups.append(group)
        except (OSError, ValueError, PayloadError) as exc:
            errors.append(f"{original.name}: {exc}")
    if safe_groups and feature_enabled(settings, HubFeature.MANAGERS):
        metadata, warnings = metadata_store.scan()
        if warnings:
            errors.extend(str(item) for item in warnings)
        final_rounds = {item.identity: item.final_round for item in metadata if item.final_round is not None}
        events = build_live_hall_of_fame_events(tuple(safe_groups), index, settings, final_rounds=final_rounds)
        hall_store.freeze_complete(events)
    if errors:
        raise PayloadError(" | ".join(errors))


def _reference(path: Path, data_dir: Path) -> str:
    return path.resolve().relative_to(data_dir.resolve()).as_posix()


def _previous_manifest(checkpoint: dict[str, Any], store: ManifestStore, run_root: Path, data_dir: Path):
    """Only use manifest paths from the configured manifest root."""
    for raw in reversed(checkpoint.get("manifest_paths", [])):
        path = Path(raw)
        if not path.is_absolute():
            path = data_dir / path
        allowed = (store.manifest_dir.resolve(), run_root.resolve())
        if not any(path.resolve().is_relative_to(root) for root in allowed):
            raise PayloadError("Manifestreferencen er uden for datamappen")
        return store.load(path)
    return None


def create_refresh_coordinator(paths, *, client_factory=HoldetClient):
    """Return a coordinator; creation and polling are side-effect free."""
    def action(task, *, cancelled, progress, checkpoint, save_checkpoint=None):
        # Resolve every value again on the worker, never reuse a page/session object.
        config = GroupStore(paths.groups_file, paths.group_revision_dir).load_configuration()
        locale, slug = task.get("locale"), task.get("game")
        matches = [item for item in config.games if item.game.locale.casefold() == str(locale).casefold() and item.game.slug == slug]
        if len(matches) != 1:
            raise PayloadError("Managerspillet findes ikke entydigt")
        manager = matches[0]
        if manager.is_archived:
            raise PayloadError("Arkiverede managerspil er skrivebeskyttede")
        settings = HubSettingsStore(paths.hub_settings_file).load()
        snapshots = SnapshotStore(paths.snapshot_dir)
        players = PlayerStatisticsStore(paths.snapshot_dir)
        manifests = ManifestStore(paths.manifest_dir)
        metadata_store = GameMetadataStore(paths.game_metadata_dir)
        pairing_store = TournamentPairingStore(paths.tournament_pairing_dir)
        old_index = snapshots.scan()
        scope = resolve_scope(manager, config.groups, old_index, settings, competition_id=task.get("competition_id"))
        groups = _baseline_groups(scope.groups, old_index, pairing_store)
        run_root = paths.data_dir / "refresh-runs" / checkpoint["run_id"]
        manifest_checkpoint = checkpoint
        if not checkpoint.get("manifest_paths") and task.get("retry_manifest_path"):
            manifest_checkpoint = {**checkpoint, "manifest_paths": [task["retry_manifest_path"]]}
        previous = _previous_manifest(manifest_checkpoint, manifests, run_root, paths.data_dir)
        expected_scope = "group" if scope.competition_id else "game"
        if previous is not None and (
            previous.scope != expected_scope
            or (expected_scope == "group" and previous.group_id != scope.competition_id)
            or previous.game_slug != manager.game.slug
            or (previous.game_locale and previous.game_locale.casefold() != manager.game.locale.casefold())
        ):
            raise PayloadError("Det gemte manifest tilhører et andet managerspil")
        published_pending = None
        last_manifest = Path(checkpoint["manifest_paths"][-1]) if checkpoint.get("manifest_paths") else None
        if last_manifest is not None and not last_manifest.is_absolute():
            last_manifest = paths.data_dir / last_manifest
        if previous is not None and last_manifest is not None and last_manifest.resolve().is_relative_to(run_root.resolve()):
            published_pending = manifests.write(previous)
            if not previous.failures and not checkpoint.get("uncertain"):
                return RefreshRunResult("success", (_reference(published_pending, paths.data_dir),), "Manifestet blev gemt uden netværkskald")
        last_progress = checkpoint.get("progress") or {}
        completed_manifest = (
            previous is not None and last_progress.get("source") == "manifest"
            and last_progress.get("status") == "fetched"
            and last_progress.get("manifest_path") in checkpoint.get("manifest_paths", [])
        )
        if completed_manifest and not previous.failures:
            return RefreshRunResult("success", detail="Det gemte manifest er allerede afsluttet")
        alerts_enabled = feature_enabled(settings, HubFeature.ALERTS)
        if (alerts_enabled and checkpoint.get("journal_schema_version") == 1
            and checkpoint.get("uncertain") and not completed_manifest
            and not checkpoint.get("player_transition")):
            raise PayloadError("Den gamle afbrudte opdatering mangler oplysninger til sikker gendannelse af statusalarmer")
        transition = None
        if (alerts_enabled or checkpoint.get("player_transition")) and save_checkpoint is not None:
            transition = PlayerTransition(paths.data_dir, checkpoint["run_id"], manager.game,
                                          players, checkpoint.get("player_transition"), save_checkpoint)
            if transition.record is not None and transition.record["snapshot_path"] and not transition.record["alerts_complete"]:
                transition.publish()
        try:
            metadata = metadata_store.load(manager.game)
        except (OSError, ValueError, PayloadError):
            metadata = None
        mode = RefreshMode.RETRY_FAILED if previous is not None else (RefreshMode.ALL if task.get("force") else RefreshMode.STALE_ONLY)
        if previous is None:
            previous = manifests.load_latest(
                manager.game.slug, game_locale=manager.game.locale,
                scope=expected_scope, group_id=scope.competition_id,
            )
        plan = build_refresh_plan(manager, groups, old_index, players.scan(manager.game),
            metadata=metadata, mode=mode, previous_manifest=previous, settings=settings,
            catch_up=True, competition_id=scope.competition_id, include_postprocess=True)
        uncertain = checkpoint.get("uncertain") and not completed_manifest and checkpoint.get("progress")
        if uncertain:
            step_id = uncertain.get("source")
            if not isinstance(step_id, str) or not step_id:
                raise PayloadError("Det afbrudte kildetrin kan ikke genskabes sikkert")
            plan = select_recovery_step(plan, step_id)
        if transition is not None and transition.record is not None and not transition.record["alerts_complete"]:
            pending_step = "postprocess" if transition.record["snapshot_path"] else "players"
            plan = select_recovery_step(plan, pending_step)
        if not plan.selected_steps:
            return RefreshRunResult("success", detail="Alle lokale data er aktuelle")

        stop = threading.Event()
        def report(event):
            try:
                progress({"source": event.step.step_id, "status": event.status,
                          "completed_steps": event.completed_steps, "total_steps": event.total_steps,
                          "label": event.step.label})
            except Exception:
                # The refresh executor deliberately isolates observers. A broken
                # durable checkpoint instead requests a stop at the next source.
                stop.set()

        def cancellation_requested():
            return stop.is_set() or cancelled()

        def postprocess():
            _postprocess(groups, snapshots, pairing_store, metadata_store, settings,
                         HallOfFameStore(paths.hall_of_fame_dir))

        # Imported at call time while allowing test injection and avoiding any UI.
        from holdet_lib.refresh import ManifestWriteError, catch_up_manager_game
        try:
            result = catch_up_manager_game(manager, groups, client_factory(), snapshots, players, manifests,
                settings=settings, inbox_store=AnalysisInboxStore(paths.analysis_inbox_file),
                metadata_store=metadata_store, plan=plan, progress=report,
                postprocess=postprocess, cancelled=cancellation_requested,
                alerts_enabled=alerts_enabled, player_transition=transition)
        except ManifestWriteError as exc:
            # Preserve the full completed child result even when the ordinary
            # manifest directory is unavailable; a retry must not refetch it.
            from holdet_lib.storage import _manifest_to_dict
            pending = run_root / f"pending-{exc.manifest.run_id}.json"
            replace_text_atomically(pending, json.dumps(_manifest_to_dict(exc.manifest), ensure_ascii=False, indent=2) + "\n")
            return RefreshRunResult("partial", (_reference(pending, paths.data_dir),), "Manifestet skal gemmes igen")
        try:
            progress({"source": "manifest", "status": "fetched", "manifest_path": _reference(result.manifest_path, paths.data_dir)})
        except Exception:
            stop.set()
        status = "partial" if stop.is_set() else "cancelled" if result.cancelled else "partial" if result.manifest and result.manifest.failures else "success"
        paths_written = (() if published_pending is None else (_reference(published_pending, paths.data_dir),))
        warnings = (*result.warnings, *(('Journalen kunne ikke gemmes; opdateringen blev standset',) if stop.is_set() else ()))
        errors = tuple(step for step in result.manifest.failures if step.status != "cancelled") if result.manifest else ()
        details = []
        if errors:
            details.append(f"{len(errors)} datakilder kunne ikke opdateres. Tidligere data er bevaret, hvor de findes.")
            first = errors[0]
            details.append(f"{first.label}: {first.error or first.reason or first.status}")
        return RefreshRunResult(status, (*paths_written, _reference(result.manifest_path, paths.data_dir)), " | ".join((*details, *warnings)) or None)

    return RefreshRunCoordinator(paths.data_dir / "refresh-runs", action, durable_checkpoint=True)
