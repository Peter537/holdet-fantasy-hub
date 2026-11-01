"""Native download actions and a reconnectable view of explicit refresh runs."""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from holdet_lib.game_metadata import GameMetadataStore
from holdet_lib.errors import PayloadError
from holdet_lib.output import sanitize_path_component
from holdet_lib.refresh_runs import RefreshRunBusyError
from holdet_lib.scope_exports import prepare_scope_export, serialize_scope_export
from holdet_lib.scopes import resolve_scope
from holdet_lib.storage import ManifestStore, PlayerStatisticsStore
from holdet_lib.tournament_pairings import TournamentPairingStore
from website.preferences import settings_for
from website.navigation import PageId, go_to
from website.refresh_service import create_refresh_coordinator


@st.cache_resource(show_spinner=False)
def _coordinator(paths):
    return create_refresh_coordinator(paths)


def updates_running(paths) -> bool:
    return any(run["status"] in {"running", "unstarted"} for run in _coordinator(paths).list_runs())


def _task(manager, *, competition_id=None, force=False):
    return {
        "key": f"{manager.game.locale}:{manager.game.slug}:{competition_id or 'game'}",
        "locale": manager.game.locale,
        "game": manager.game.slug,
        "competition_id": competition_id,
        "name": manager.name,
        "force": force,
    }


def _start(paths, tasks, *, export_after=False, export_history=False):
    try:
        run = _coordinator(paths).start(tasks, scope={"export_after": export_after, "export_history": export_history})
    except (OSError, ValueError, RefreshRunBusyError) as exc:
        st.error(str(exc))
        return
    st.session_state["visible-refresh-run"] = run["run_id"]
    st.rerun()


def start_manager_update(paths, manager, *, force=False, retry_manifest=None):
    """Start the same durable worker from the round-centre preview."""
    task = _task(manager, force=force)
    if retry_manifest is not None:
        try:
            if retry_manifest.path is None:
                raise ValueError("Manifestet skal være gemt, før det kan genoptages.")
            source = retry_manifest.path.resolve()
            if not source.is_relative_to(paths.manifest_dir.resolve()):
                raise ValueError("Manifestet ligger uden for manifestmappen.")
            stored = ManifestStore(paths.manifest_dir).load(source)
            if (stored.run_id != retry_manifest.run_id or stored.scope != "game"
                    or stored.game_slug != manager.game.slug
                    or stored.game_locale and stored.game_locale.casefold() != manager.game.locale.casefold()):
                raise ValueError("Manifestet tilhører et andet managerspil eller scope.")
            task["retry_manifest_path"] = source.relative_to(paths.data_dir.resolve()).as_posix()
        except (OSError, PayloadError, ValueError) as exc:
            st.error(f"Opdateringen kunne ikke genoptages: {exc}")
            return
    _start(paths, [task])


def render_global_update(paths, games):
    active = tuple(game for game in games if not game.is_archived)
    busy = updates_running(paths)
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        chosen = st.menu_button("Åbn spil", active, format_func=lambda game: game.name,
            icon=":material/arrow_forward:", key="open-game-menu") if active else None
        if chosen is not None:
            go_to(PageId.GAME, locale=chosen.game.locale, game=chosen.game.slug)
        if st.button("Opdater alle", type="primary", icon=":material/sync:", disabled=not active or busy, key="refresh-all-games"):
            _start(paths, [_task(game) for game in active])
        with st.popover("Vælg spil og omfang", icon=":material/tune:"):
            selected = st.multiselect("Managerspil", active, default=active, format_func=lambda game: game.name, key="refresh-selected-games")
            force = st.checkbox("Genhent også aktuelle data", key="refresh-force-games")
            st.caption("Manglende og tidligere foreløbige runder hentes. Arkiverede spil er ikke med.")
            if st.button("Opdater valgte spil", disabled=not selected or busy, key="refresh-start-selection"):
                _start(paths, [_task(game, force=force) for game in selected])


def _export_scope(paths, scope, teams):
    return prepare_scope_export(scope, teams, TournamentPairingStore(paths.tournament_pairing_dir))


def render_scope_actions(paths, manager, groups, teams, *, competition_id=None, read_only=False, extra_actions=None):
    settings = settings_for(paths)
    scope = resolve_scope(manager, groups, teams, settings, competition_id=competition_id)
    scope = _export_scope(paths, scope, teams)
    players = PlayerStatisticsStore(paths.snapshot_dir).scan(manager.game)
    try:
        metadata = GameMetadataStore(paths.game_metadata_dir).load(manager.game)
    except (OSError, PayloadError, ValueError):
        metadata = None
    manifest = ManifestStore(paths.manifest_dir).load_latest(
        manager.game.slug, game_locale=manager.game.locale,
        scope="group" if competition_id else "game", group_id=competition_id,
    )
    identity = competition_id or f"{manager.game.locale}:{manager.game.slug}"
    busy = updates_running(paths)
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        if st.button("Opdater data", key=f"scope-refresh:{identity}", icon=":material/refresh:", disabled=read_only or scope.read_only or busy):
            _start(paths, [_task(manager, competition_id=competition_id)])
        with st.popover("Download JSON", icon=":material/download:"):
            history = st.checkbox("Medtag spillerhistorik pr. runde", key=f"scope-export-history:{identity}")
            st.caption("Én samlet JSON med alle spillere, de valgte hold og konkurrencerne. Tabelfiltre ændrer ikke indholdet.")
            has_data = bool(players.snapshots or any(teams.newest(scope.game, ref.team_id) for ref in scope.team_references))
            def data():
                return serialize_scope_export(scope, teams, players, include_history=history, metadata=metadata, manifest=manifest)
            stem = sanitize_path_component(competition_id or manager.game.slug, fallback="managerspil")
            st.download_button(
                "Download samlet JSON", data=data,
                file_name=f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json",
                mime="application/json", key=f"scope-download:{identity}", on_click="ignore",
                disabled=not has_data,
            )
            if not has_data:
                st.info("Der er ingen lokale spiller- eller holddata at eksportere endnu.")
            else:
                st.caption("Filen angiver kildernes runder, alder og eventuelle mangler. Download henter ikke nye data.")
            if st.button("Opdater før download", key=f"scope-refresh-export:{identity}", disabled=read_only or scope.read_only or busy):
                _start(paths, [_task(manager, competition_id=competition_id)], export_after=True, export_history=history)
        if extra_actions is not None:
            extra_actions()


def _completed_export(paths, run):
    """Offer the requested file directly beside the completed run."""
    from holdet_lib.groups import GroupStore
    from holdet_lib.storage import SnapshotStore

    if len(run["games"]) != 1:
        return
    task = run["games"][0]["game"]
    try:
        config = GroupStore(paths.groups_file, paths.group_revision_dir).load_configuration()
        manager = next(item for item in config.games if item.identity == (task["locale"].casefold(), task["game"]))
        teams = SnapshotStore(paths.snapshot_dir).scan()
        scope = resolve_scope(manager, config.groups, teams, settings_for(paths), competition_id=task.get("competition_id"))
        scope = _export_scope(paths, scope, teams)
        players = PlayerStatisticsStore(paths.snapshot_dir).scan(manager.game)
        manifest = ManifestStore(paths.manifest_dir).load_latest(manager.game.slug, game_locale=manager.game.locale, scope="group" if scope.competition_id else "game", group_id=scope.competition_id)
    except (OSError, PayloadError, ValueError, StopIteration, KeyError) as exc:
        st.warning(f"Den samlede fil kunne ikke klargøres: {exc}")
        return
    try:
        metadata = GameMetadataStore(paths.game_metadata_dir).load(manager.game)
    except (OSError, PayloadError, ValueError):
        metadata = None
        st.warning("Spilinfo kunne ikke læses. Filen indeholder de tilgængelige data og markerer manglende spilinfo.")
    label = "Download opdateret JSON" if run["status"] == "success" else "Download JSON med kendte mangler"
    st.download_button(label, data=lambda: serialize_scope_export(scope, teams, players,
        include_history=run.get("scope", {}).get("export_history", False), metadata=metadata, manifest=manifest),
        file_name=f"{sanitize_path_component(manager.game.slug, fallback='managerspil')}.json",
        mime="application/json", on_click="ignore", key=f"completed-export:{run['run_id']}")
    if run["status"] != "success":
        st.caption("Filen indeholder de tilgængelige lokale data og beskriver manglerne.")


@st.fragment(run_every="1s")
def render_run_progress(paths):
    coordinator = _coordinator(paths)
    runs, warnings = coordinator.scan_runs()
    if warnings:
        st.warning("En tidligere opdateringsjournal kunne ikke læses. Se Data og lager før oprydning.")
    if not runs:
        return
    run_id = st.session_state.get("visible-refresh-run", runs[0]["run_id"])
    try:
        run = coordinator.snapshot(run_id)
    except (OSError, ValueError):
        run = runs[0]
        run_id = run["run_id"]
    status = run["status"]
    labels = {"success": "Opdatering gennemført", "partial": "Opdatering delvist gennemført", "failed": "Opdatering mislykkedes", "cancelled": "Opdatering stoppet", "interrupted": "Opdatering blev afbrudt", "running": "Opdaterer data", "unstarted": "Forbereder opdatering"}
    previous = st.session_state.get(f"refresh-observed:{run_id}")
    st.session_state[f"refresh-observed:{run_id}"] = status
    invalidation_key = f"refresh-invalidated:{run_id}:{run.get('updated_at', '')}"
    if status not in {"running", "unstarted"} and not st.session_state.get(invalidation_key):
        st.session_state[invalidation_key] = True
        st.cache_data.clear()
        st.rerun()
    active = status in {"running", "unstarted"}
    with st.expander(labels.get(status, status), expanded=active or status != "success"):
        complete = sum(game["status"] == "success" for game in run["games"])
        processed = sum(game["status"] in {"success", "partial", "failed", "cancelled"} for game in run["games"])
        partial = sum(game["status"] == "partial" for game in run["games"])
        text = f"{processed} af {len(run['games'])} spil behandlet · {complete} fuldt opdateret"
        if partial:
            text += f" · {partial} med mangler"
        st.progress(processed / len(run["games"]), text=text)
        waiting = sum(game["status"] == "unstarted" for game in run["games"])
        if not active and waiting:
            st.caption(f"{waiting} spil afventer genoptagelse. De starter først, når du vælger Genoptag.")
        for item in run["games"]:
            game = item["game"]
            progress = item.get("progress") or {}
            item_label = ("I kø" if active else "Afventer genoptagelse") if item["status"] == "unstarted" else labels.get(item["status"], item["status"])
            st.write(f"**{game.get('name', game['game'])}** · {item_label}")
            if progress.get("label"):
                st.caption(f"{progress['label']} · {progress.get('completed_steps', 0)}/{progress.get('total_steps', 0)} datakilder")
            if item.get("detail"):
                st.caption(item["detail"])
        if active:
            if run.get("cancel_requested"):
                st.info("Stop er bestilt. Det igangværende kildetrin afsluttes først.")
            elif st.button("Stop efter denne datakilde", key=f"cancel-refresh:{run_id}"):
                coordinator.cancel(run_id)
                st.rerun(scope="fragment")
        elif status != "success":
            if st.button("Genoptag / prøv fejlede trin igen", key=f"resume-refresh:{run_id}"):
                try:
                    coordinator.resume(run_id)
                except (OSError, ValueError, RefreshRunBusyError) as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
        if not active and run.get("scope", {}).get("export_after"):
            _completed_export(paths, run)
