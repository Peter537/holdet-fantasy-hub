"""Shared Streamlit UI renderers for the Holdet fantasy hub."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import sys
from urllib.parse import quote
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st
import holdet_lib as holdet

from holdet_lib._formatting import count_label
from holdet_lib.accounts import AccountStore
from holdet_lib.team_exports import (
    TEAM_EXPORT_FORMATS, TeamExportStore, build_team_export,
)
from holdet_lib.version import VERSION
from holdet_lib.seasons import SeasonStore
from website.data_page import data_storage_view
from website.hub_pages import (
    game_history_panel,
    group_history_panel,
    history_panel,
    player_changes_panel,
    player_compare_panel,
    render_tournament_bracket,
    team_changes_panel,
    transfer_lab_panel,
)
from website.analysis_pages import analysis_panel, alerts_view, player_detail_view
from website.hub_pages import (
    calendar_view,
    managers_view,
)
from website.navigation import PageId, go_to, page_link, relative_url
from website.round_center_page import render_round_center
from website.presentation import (
    data_status_label,
    dataframe,
    format_relative_precise,
    next_schedule_action,
    sport_label,
)


from holdet_lib import (
    GroupFixture,
    GroupDefinition,
    GroupMatch,
    HubConfiguration,
    GroupStore,
    GroupTeam,
    GameUrl,
    GameMetadataStore,
    HallOfFameStore,
    HubSettingsStore,
    AnalysisInboxStore,
    SavedPlayerFilter,
    build_live_hall_of_fame_events,

    build_manager_ratings,
    build_round_story,
    resolve_manager_identity,
    FetchError,
    HoldetClient,
    KnockoutMatch,
    ManagerGame,
    ManifestStore,
    PayloadError,
    MISSING_VALUE_MODES,
    PLAYER_COLUMNS,
    PLAYER_EXPORT_FORMATS,
    PLAYER_SORT_FIELDS,
    PLAYER_STATUSES,
    STATUS_LABELS_DA,
    PlayerExportStore,
    PlayerStatisticsQuery,
    PlayerStatisticsStore,
    SnapshotIndex,
    SnapshotStore,
    StandingRow,
    TournamentPairing,
    TournamentPairingRevision,
    TournamentPairingStore,
    build_swiss_pairing_conflicts,
    build_swiss_participants,
    STAGE_NAMES,
    TournamentState,
    TeamSnapshot,
    TeamReference,
    build_player_export,
    build_player_decision_analysis,
    build_watchlist_alerts,
    select_alert_baseline,
    build_standings,
    build_tournament_head_to_head,
    build_tournament_state,
    filter_player_statistics,
    generate_swiss_pairings,
    format_integer,
    generate_draw_seed,
    group_team_from_snapshot,
    latest_tournament_round,
    load_accounts,
    knockout_size_for,
    normalize_game_url,
    normalize_manager_game,
    player_column_labels,
    player_display_rows,
    player_identity,
    parse_direct_team_url,
    refresh_manager_game,
    refresh_group,
    resolve_paths,
)
from holdet_lib.refresh import (
    RefreshMode,
    RefreshPlan,
    RefreshProgressEvent,
    build_refresh_plan,
)
from holdet_lib.storage import RefreshManifest


def _configure_paths() -> None:
    """Refresh environment-derived paths for the current app execution."""

    global APP_PATHS, CONFIG_DIR, OUTPUT_DIR, MANIFEST_DIR
    global GROUPS_PATH, ACCOUNTS_PATH, PLAYER_EXPORT_DIR, TEAM_EXPORT_DIR
    APP_PATHS = resolve_paths()
    CONFIG_DIR = APP_PATHS.config_dir
    OUTPUT_DIR = APP_PATHS.snapshot_dir
    MANIFEST_DIR = APP_PATHS.manifest_dir
    GROUPS_PATH = APP_PATHS.groups_file
    ACCOUNTS_PATH = APP_PATHS.accounts_file
    PLAYER_EXPORT_DIR = APP_PATHS.player_export_dir
    TEAM_EXPORT_DIR = APP_PATHS.team_export_dir


_configure_paths()

GAME_COLORS = {
    "tour": ("#f4b400", "#1d1b13"),
    "super-manager": ("#2f8f55", "#f6fff9"),
    "motor": ("#d23b3b", "#fff8f8"),
    "golf": ("#2774a8", "#f7fcff"),
}


@st.cache_data(ttl=2, max_entries=4, show_spinner=False)
def _scan_snapshots(root: str) -> SnapshotIndex:
    """Cache the immutable snapshot index across lightweight UI reruns."""

    return SnapshotStore(Path(root)).scan()


def _invalidate_snapshot_index() -> None:
    _scan_snapshots.clear()
def _freeze_complete_hall_of_fame(
    groups: tuple[GroupDefinition, ...],
    *,
    include_round_wins: bool,
) -> int:
    try:
        settings = HubSettingsStore(APP_PATHS.hub_settings_file).load()
        metadata, _ = GameMetadataStore(APP_PATHS.game_metadata_dir).scan()
        final_rounds = {
            item.identity: item.final_round
            for item in metadata
            if item.final_round is not None
        }
        events = build_live_hall_of_fame_events(
            groups,
            SnapshotStore(OUTPUT_DIR).scan(),
            settings,
            final_rounds=final_rounds,
        )
        if not include_round_wins:
            events = tuple(item for item in events if item.kind != "round_win")
        paths = HallOfFameStore(APP_PATHS.hall_of_fame_dir).freeze_complete(events)
    except Exception as exc:
        st.session_state["hall_of_fame_freeze_warning"] = str(exc)
        return 0
    return len(paths)

def _with_published_tournament_pairings(
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
) -> tuple[tuple[GroupDefinition, ...], tuple[str, ...]]:
    """Read and validate frozen pairings without mutating configuration."""

    store = TournamentPairingStore(APP_PATHS.tournament_pairing_dir)
    resolved: list[GroupDefinition] = []
    warnings: list[str] = []
    for group in groups:
        config = group.tournament
        if config is None or config.template != "swiss":
            resolved.append(group)
            continue
        try:
            published = store.load_for_tournament(
                group.group_id,
                group.active_revision,
                config,
                tuple(item.team_id for item in group.teams),
            )
        except PayloadError as exc:
            warnings.append(
                f"{group.name}: publicerede parringer kunne ikke l\u00e6ses: {exc}"
            )
            resolved.append(group)
            continue
        existing = {
            (item.round_number, item.team_a_id, item.team_b_id)
            for item in config.group_fixtures
        }
        extras = tuple(
            GroupFixture(item.round_number, item.team_a_id, item.team_b_id)
            for item in published.pairings
            if (
                item.round_number,
                item.team_a_id,
                item.team_b_id,
            ) not in existing
        )
        fixtures = tuple(
            sorted(
                (*config.group_fixtures, *extras),
                key=lambda item: (
                    item.round_number,
                    item.team_a_id,
                    -1 if item.team_b_id is None else item.team_b_id,
                ),
            )
        )
        merged = replace(
            group,
            tournament=replace(config, group_fixtures=fixtures),
        )
        conflicts = build_swiss_pairing_conflicts(merged, index)
        if conflicts:
            rounds = ", ".join(str(item.round_number) for item in conflicts)
            warnings.append(
                f"{group.name}: de frosne Swiss-parringer i runde {rounds} "
                "afviger fra de korrigerede resultater. Opret en ny "
                "turneringsrevision for at genberegne dem."
            )
        resolved.append(merged)
    return tuple(resolved), tuple(warnings)


def _publish_next_swiss_round(
    group: GroupDefinition,
    index: SnapshotIndex,
) -> TournamentPairingRevision | None:
    """Publish at most one next Swiss round after an explicit refresh."""

    config = group.tournament
    if config is None or config.template != "swiss":
        return None
    published_round = max(
        (item.round_number for item in config.group_fixtures),
        default=config.start_round - 1,
    )
    if published_round >= config.final_round:
        return None
    state = build_tournament_state(group, index, published_round)
    previous_matches = tuple(
        item
        for item in state.group_matches
        if item.fixture.round_number == published_round
    )
    if not previous_matches or not all(item.complete for item in previous_matches):
        return None
    participants = build_swiss_participants(config, state.group_matches)
    next_round = published_round + 1
    fixtures = generate_swiss_pairings(participants, next_round)
    pairings = tuple(
        TournamentPairing(
            item.round_number,
            item.team_a_id,
            item.team_b_id,
        )
        for item in fixtures
    )
    return TournamentPairingStore(
        APP_PATHS.tournament_pairing_dir
    ).publish_round(
        group.group_id,
        group.active_revision,
        next_round,
        pairings,
        previous_round_complete=True,
    )




def _client() -> HoldetClient:
    # Resolve through the package at call time so tests and embedders can
    # replace the network client without re-importing every page module.
    return holdet.HoldetClient()
def _save_game_metadata_if_available(game: GameUrl, info: object | None = None) -> bool:
    try:
        resolved = info
        if resolved is None:
            client = _client()
            fetch = getattr(client, "fetch_game_info", None)
            if not callable(fetch):
                return False
            resolved = fetch(game)
        GameMetadataStore(APP_PATHS.game_metadata_dir).save(resolved)
    except Exception:
        return False
    return True





def _colors(slug: str) -> tuple[str, str]:
    for marker, colors in GAME_COLORS.items():
        if marker in slug:
            return colors
    return "#6f62d9", "#fbfaff"


_DANISH_MONTHS = (
    "",
    "januar",
    "februar",
    "marts",
    "april",
    "maj",
    "juni",
    "juli",
    "august",
    "september",
    "oktober",
    "november",
    "december",
)


def _format_local_date(value: datetime) -> str:
    local = value.astimezone()
    return f"{local.day}. {_DANISH_MONTHS[local.month]}"


def _local_data_status(
    generated_at: datetime | None,
    round_number: int | None = None,
    round_status: str | None = None,
) -> str:
    if generated_at is None:
        return "Mangler · Klar til manuel opdatering"
    prefix = f"Lokale data: {format_relative_precise(generated_at)}"
    if round_number is None or round_number <= 0:
        return prefix
    if round_status == "in_progress":
        return f"{prefix} \u00b7 Runde {round_number} er ikke afsluttet"
    if round_status == "unknown":
        return f"{prefix} \u00b7 Rundestatus ukendt"
    if round_status == "complete":
        return f"{prefix} \u00b7 Runde {round_number} er afsluttet"
    return prefix


def _sport_icon(slug: str) -> str:
    icons = {
        "super-manager": ":material/sports_soccer:",
        "motor": ":material/sports_motorsports:",
        "golf": ":material/sports_golf:",
        "tour": ":material/directions_bike:",
    }
    return next(
        (icon for marker, icon in icons.items() if marker in slug),
        ":material/trophy:",
    )


def _snapshot_data_status(snapshot: TeamSnapshot | None) -> str:
    if snapshot is None:
        return _local_data_status(None)
    latest = max(
        snapshot.team.history,
        key=lambda summary: summary.round_number,
        default=None,
    )
    return _local_data_status(
        snapshot.generated_at,
        None if latest is None else latest.round_number,
        None if latest is None else latest.round_status,
    )


def _format_number(value: int | None, *, signed: bool = False) -> str:
    if value is None:
        return "–"
    prefix = "+" if signed and value > 0 else ""
    return prefix + format_integer(value)


def _game_label_sort_key(item: tuple[GameUrl, str]) -> tuple[str, str, str]:
    game, label = item
    return (
        label.casefold(),
        game.locale.casefold(),
        game.slug.casefold(),
    )


def _manager_game_sort_key(game: ManagerGame) -> tuple[str, str, str]:
    return _game_label_sort_key((game.game, game.name))


def _sorted_manager_games(
    games: Iterable[ManagerGame],
) -> tuple[ManagerGame, ...]:
    return tuple(sorted(games, key=_manager_game_sort_key))


def _unread_alert_state() -> tuple[dict[tuple[str, str], int], str | None]:
    """Return unread counts plus a visible verification error, without writes."""

    try:
        alerts = AnalysisInboxStore(APP_PATHS.analysis_inbox_file).load()
    except (OSError, PayloadError, ValueError) as exc:
        return {}, str(exc)
    counts: dict[tuple[str, str], int] = {}
    for alert in alerts:
        if not alert.is_unread:
            continue
        identity = (alert.game_locale, alert.game_slug)
        counts[identity] = counts.get(identity, 0) + 1
    return counts, None


def _active_alert_counts() -> dict[tuple[str, str], int]:
    """Return non-dismissed alert counts by game without mutating the inbox."""

    try:
        alerts = AnalysisInboxStore(APP_PATHS.analysis_inbox_file).load()
    except (OSError, PayloadError, ValueError):
        return {}
    counts: dict[tuple[str, str], int] = {}
    for alert in alerts:
        if alert.dismissed_at is not None:
            continue
        identity = (alert.game_locale, alert.game_slug)
        counts[identity] = counts.get(identity, 0) + 1
    return counts


def _unread_alert_counts() -> dict[tuple[str, str], int]:
    """Compatibility projection for callers that only render a count."""

    return _unread_alert_state()[0]


def _requested_game_from_query() -> GameUrl | None:
    locale = str(st.query_params.get("locale", "")).casefold()
    slug = str(st.query_params.get("game", ""))
    if not locale or not slug:
        return None
    try:
        return normalize_game_url(
            f"https://www.holdet.dk/{quote(locale, safe='')}/fantasy/"
            f"{quote(slug, safe='')}"
        )
    except (PayloadError, ValueError):
        return None


def _legacy_alert_target(
    games: Iterable[ManagerGame],
) -> ManagerGame | None:
    ordered = _sorted_manager_games(games)
    counts = _unread_alert_counts()
    for candidates in (
        tuple(game for game in ordered if not game.is_archived),
        tuple(game for game in ordered if game.is_archived),
    ):
        target = next((game for game in candidates if counts.get(game.identity)), None)
        if target is not None:
            return target
        if candidates:
            return candidates[0]
    return None


def _format_table_integer(value: object) -> str:
    if value is None or bool(pd.isna(value)):
        return "–"
    integer = int(value)
    if isinstance(value, bool) or value != integer:
        raise ValueError(f"Tabelværdien skal være et helt tal, men var {value!r}")
    return format_integer(integer)


def _style_integer_columns(
    rows: list[dict[str, object]], columns: tuple[str, ...]
) -> pd.io.formats.style.Styler:
    frame = pd.DataFrame(rows)
    formatters = {
        column: _format_table_integer for column in columns if column in frame.columns
    }
    return frame.style.format(formatters, na_rep="–")


def _navigate(view: str, **parameters: object) -> None:
    go_to(view, **parameters)




def _stateful_tabs(
    labels: tuple[str, ...] | list[str],
    slugs: tuple[str, ...] | list[str],
    *,
    key: str,
    parameter: str,
):
    label_tuple = tuple(labels)
    slug_tuple = tuple(slugs)
    requested = str(st.query_params.get(parameter, ""))
    desired = (
        label_tuple[slug_tuple.index(requested)]
        if requested in slug_tuple
        else label_tuple[0]
    )
    if (
        requested in slug_tuple
        or key not in st.session_state
        or st.session_state[key] not in label_tuple
    ):
        st.session_state[key] = desired

    def sync_tab() -> None:
        selected = st.session_state[key]
        st.query_params[parameter] = slug_tuple[label_tuple.index(selected)]
        if parameter == "section" and "panel" in st.query_params:
            del st.query_params["panel"]

    return st.tabs(
        label_tuple,
        default=st.session_state[key],
        key=key,
        on_change=sync_tab,
    )


def _round_selectbox(
    label: str,
    rounds: tuple[int, ...] | list[int],
    *,
    key: str,
    default: int | None = None,
) -> int:
    options = tuple(int(value) for value in rounds)
    requested = str(st.query_params.get("round", ""))
    requested_round = int(requested) if requested.isdigit() else None
    desired = (
        requested_round
        if requested_round in options
        else default if default in options
        else options[0]
    )
    if requested_round in options or key not in st.session_state:
        st.session_state[key] = desired

    def sync_round() -> None:
        st.query_params["round"] = str(st.session_state[key])

    return int(
        st.selectbox(
            label,
            options,
            key=key,
            on_change=sync_round,
        )
    )
def _markdown_literal(value: str) -> str:
    """Escape inline Markdown markers in user-provided card text."""
    for marker in ("\\", "`", "*", "_", "[", "]", "<", ">"):
        value = value.replace(marker, "\\" + marker)
    return value


def _navigation_card(
    *,
    card_key: str,
    title: str,
    subtitle: str,
    detail: str,
    color: str,
    foreground: str,
    aria_label: str,
    view: str,
    icon: str | None = None,
    action: str = "\u00c5bn",
    signals: tuple[str, ...] = (),
    metadata: str | None = None,
    **parameters: object,
) -> None:
    """Render a native, keyboard-accessible card without a browser reload."""
    lines = [
        f"**{_markdown_literal(title)}**",
        f":small[{_markdown_literal(subtitle)}]",
    ]
    lines.extend(_markdown_literal(signal) for signal in signals)
    if detail:
        lines.append(_markdown_literal(detail))
    if metadata:
        lines.append(f":small[Teknisk ID · {_markdown_literal(metadata)}]")
    lines.append(f"**{_markdown_literal(action)} \u2192**")
    label = "  \n".join(lines)
    with st.container(key=f"nav-card-{card_key}"):
        st.html(
            f"""<style>
            .st-key-nav-card-{card_key} button {{
                background: linear-gradient(125deg, {color}, #20252d) !important;
                color: {foreground} !important;
            }}
            </style>"""
        )
        if st.button(
            label,
            key=f"open-card-{card_key}",
            help=aria_label,
            icon=icon,
            type="tertiary",
            width="stretch",
        ):
            _navigate(view, **parameters)


def _latest_by_identity(index: SnapshotIndex) -> dict[tuple[str, str, int], TeamSnapshot]:
    result: dict[tuple[str, str, int], TeamSnapshot] = {}
    for snapshot in index.snapshots:
        result.setdefault(snapshot.identity, snapshot)
    return result


def _manifest_statuses(group: GroupDefinition) -> frozenset[int]:
    store = ManifestStore(MANIFEST_DIR)
    game_manifests, _ = store.scan(
        group.game.slug,
        game_locale=group.game.locale,
        scope="game",
    )
    group_manifests, _ = store.scan(
        group.game.slug,
        game_locale=group.game.locale,
        scope="group",
        group_id=group.group_id,
    )
    manifests = sorted(
        (*game_manifests, *group_manifests),
        key=lambda item: (item.completed_at, item.run_id),
        reverse=True,
    )
    member_ids = {member.team_id for member in group.teams}
    for manifest in manifests:
        relevant = tuple(
            step
            for step in manifest.steps
            if step.source == "team" and step.team_id in member_ids
        )
        if relevant:
            return frozenset(
                int(step.team_id)
                for step in relevant
                if step.team_id is not None
                and step.status in {"reused_after_error", "failed_no_cache"}
            )
    return frozenset()


def _styles() -> None:
    st.html(
        """
        <style>
        [class*="st-key-nav-card-"] {
            margin: .2rem 0 .9rem;
            border-radius: 15px;
        }
        [class*="st-key-nav-card-"] button {
            width: 100%; min-height: 120px; padding: 1rem 1.15rem;
            justify-content: flex-start !important; align-items: flex-start !important;
            border-radius: 15px; text-align: left !important;
            border: 1px solid rgba(255,255,255,.10) !important;
            box-shadow: 0 10px 30px rgba(0,0,0,.17);
            cursor: pointer; transition: transform .16s ease, box-shadow .16s ease,
                        border-color .16s ease;
        }
        [class*="st-key-nav-card-"] button > div,
        [class*="st-key-nav-card-"] button > div > span {
            width: 100% !important;
            justify-content: flex-start !important;
            align-items: flex-start !important;
        }
        [class*="st-key-nav-card-"] button p {
            width: 100%; text-align: left !important;
            line-height: 1.55;
        }
        [class*="st-key-nav-card-"] button:hover {
            transform: translateY(-3px); box-shadow: 0 14px 34px rgba(0,0,0,.25);
            border-color: rgba(255,255,255,.28) !important;
        }
        [class*="st-key-nav-card-"] button:focus-visible {
            outline: 3px solid #ff4b4b; outline-offset: 3px;
        }
        [class*="st-key-home-game-slot-"] [class*="st-key-nav-card-"] button {
            min-height: 190px;
        }
        [class*="st-key-sticky-action-bar-"] {
            position: sticky;
            top: 3.75rem;
            z-index: 5;
            overflow-x: auto;
            padding: .55rem .65rem;
            margin: .15rem 0 .75rem;
            border: 1px solid #343b47;
            border-radius: 12px;
            background: rgba(17, 20, 25, .96);
            box-shadow: 0 8px 24px rgba(0, 0, 0, .22);
        }
        [class*="st-key-sticky-action-bar-"] :where(button, [tabindex]) {
            scroll-margin-top: 7rem;
        }
        [class*="st-key-round-status-"] {
            min-height: 112px;
        }
        [class*="st-key-round-status-"] a {
            width: 100%;
            min-height: 3.5rem;
            align-items: flex-start;
            justify-content: flex-start;
        }
        [class*="st-key-sidebar-group-"] {
            margin-left: 1.25rem;
            width: calc(100% - 1.25rem);
        }
        [class*="st-key-sidebar-group-"] button {
            min-height: 2.25rem;
            padding: .35rem .7rem;
            justify-content: flex-start;
            text-align: left;
        }
        [class*="st-key-sidebar-group-"] button:focus-visible {
            outline: 2px solid #ff4b4b;
            outline-offset: 2px;
        }
        :where(button, a, input, textarea, [tabindex]):focus-visible {
            outline: 3px solid #ff4b4b;
            outline-offset: 3px;
        }
        @media (prefers-reduced-motion: reduce) {
            [class*="st-key-nav-card-"] button {
                transition: none !important;
            }
            [class*="st-key-nav-card-"] button:hover {
                transform: none;
            }
        }
        @media (max-width: 640px) {
            [class*="st-key-sticky-action-bar-"] {
                top: 3.25rem;
                padding: .45rem .5rem;
            }
            [class*="st-key-home-game-slot-"] [class*="st-key-nav-card-"] button {
                min-height: 190px;
            }
        }
        </style>
        """
    )


def _sidebar_group_button(
    group: GroupDefinition,
    selected_group: GroupDefinition | None,
) -> None:
    with st.container(key=f"sidebar-group-{group.group_id}"):
        if st.button(
            group.name,
            key=f"nav-{group.group_id}",
            width="stretch",
            type="primary" if selected_group == group else "secondary",
        ):
            _navigate("group", group=group.group_id)

def _sidebar(
    games: tuple[ManagerGame, ...],
    groups: tuple[GroupDefinition, ...],
    selected_game: ManagerGame | None,
    selected_group: GroupDefinition | None,
    view: str,
) -> None:
    active_games = _sorted_manager_games(
        game for game in games if not game.is_archived
    )
    archived_selected = bool(selected_game and selected_game.is_archived)
    with st.sidebar:
        st.markdown("## HOLDET FANTASY HUB")
        if st.button(
            "Mine managerspil",
            icon=":material/home:",
            width="stretch",
            type="primary" if view == "home" else "secondary",
        ):
            _navigate("home")

        if st.button(
            "Tilføj managerspil",
            key="add-manager-game-sidebar",
            icon=":material/add:",
            type="tertiary",
            width="content",
        ):
            _navigate("manage-games")
        if st.button(
            "Spillerstatistik",
            key="standalone-player-statistics-sidebar",
            icon=":material/query_stats:",
            width="stretch",
            type="primary" if view == "players" else "secondary",
        ):
            _navigate("players")
        if st.button(
            "Holdstatistik",
            key="standalone-team-statistics-sidebar",
            icon=":material/groups:",
            width="stretch",
            type="primary" if view == "teams" else "secondary",
        ):
            _navigate("teams")
        unread_alerts = _unread_alert_counts()
        st.caption("MANAGERSPIL")
        for game in active_games:
            active = selected_game is not None and game.identity == selected_game.identity
            unread_count = unread_alerts.get(game.identity, 0)
            game_label = (
                f"{game.name} ({unread_count} ulæste)"
                if unread_count
                else game.name
            )
            if st.button(
                game_label,
                key=f"nav-game-{game.game.locale}-{game.game.slug}",
                width="stretch",
                type="primary" if active else "secondary",
            ):
                _navigate(
                    "game",
                    locale=game.game.locale,
                    game=game.game.slug,
                    section="round-center",
                )
            if active:
                for group in groups:
                    if _game_identity(group.game) != game.identity:
                        continue
                    _sidebar_group_button(group, selected_group)

        st.divider()
        if st.button(
            "Arkiverede managerspil",
            icon=":material/archive:",
            width="stretch",
            type="primary" if view == "archive" or archived_selected else "secondary",
        ):
            _navigate("archive")

        st.divider()
        if st.button(
            "Managers",
            key="managers-sidebar",
            icon=":material/military_tech:",
            width="stretch",
            type="primary" if view in {"managers", "hall-of-fame"} else "secondary",
        ):
            _navigate("managers")
        if st.button(
            "Kalender",
            key="calendar-sidebar",
            icon=":material/calendar_month:",
            width="stretch",
            type="primary" if view == "calendar" else "secondary",
        ):
            _navigate("calendar")

        if st.button(
            "Data og lager",
            icon=":material/database:",
            width="stretch",
            type="primary" if view == "data" else "secondary",
        ):
            _navigate("data")
        st.caption("Uofficielt værktøj til Holdet.dk. Data hentes aldrig automatisk.")
        st.caption(f"Version {VERSION}")


def _warning_panel(index: SnapshotIndex) -> None:
    if not index.warnings:
        return
    with st.expander(
        f"{count_label(len(index.warnings), 'fil', 'filer')} blev ignoreret"
    ):
        st.warning(
            "Nogle snapshots var beskadigede eller havde en ukendt version. "
            "Resten af siden virker fortsat."
        )
        for warning in index.warnings:
            st.code(warning)


def _group_card(group: GroupDefinition, index: SnapshotIndex) -> None:
    color, foreground = _colors(group.game.slug)
    snapshots = tuple(
        snapshot
        for member in group.teams
        if (snapshot := index.newest(group.game, member.team_id)) is not None
    )
    available = len(snapshots)
    newest_snapshot = max(
        snapshots,
        key=lambda snapshot: snapshot.generated_at,
        default=None,
    )
    detail = (
        f"Gruppestilling \u00b7 {len(group.teams)} hold \u00b7 "
        f"{available} med data"
    )
    if group.kind == "tournament" and group.tournament is not None:
        state = build_tournament_state(
            group, index, latest_tournament_round(group, index)
        )
        next_matches = state.next_matches
        if state.champion_id is not None:
            champion = next(
                (row.team_name for row in state.standings if row.team_id == state.champion_id),
                str(state.champion_id),
            )
            detail = f"Turnering · Afsluttet · Mester: {champion}"
        elif next_matches:
            match = next_matches[0]
            next_round = (
                match.fixture.round_number
                if isinstance(match, GroupMatch)
                else match.round_numbers[0]
            )
            detail = (
                f"Turnering · {state.phase} · næste runde {next_round} "
                f"({count_label(len(next_matches), 'kamp', 'kampe')})"
            )
        else:
            detail = f"Turnering · {state.phase}"
    detail = f"{detail} \u00b7 {_snapshot_data_status(newest_snapshot)}"
    _navigation_card(
        card_key=f"group-{group.group_id}",
        title=group.name,
        subtitle=group.game.slug,
        detail=detail,
        color=color,
        foreground=foreground,
        aria_label=f"Åbn gruppe {group.name}",
        icon=_sport_icon(group.game.slug),
        action="\u00c5bn gruppen",
        view="group",
        group=group.group_id,
    )


def _game_statistics(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
) -> tuple[int, int, int]:
    game_groups = tuple(
        group for group in groups if _game_identity(group.game) == manager_game.identity
    )
    team_ids = tuple(dict.fromkeys(
        member.team_id for group in game_groups for member in group.teams
    ))
    rounds = index.rounds_for(manager_game.game, team_ids) if team_ids else ()
    return len(game_groups), len(team_ids), max(rounds, default=0)


def _group_count_label(count: int) -> str:
    return count_label(count, "gruppe", "grupper")


def _manager_game_card(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    player_index=None,
    metadata_by_identity: dict[tuple[str, str], object] | None = None,
    active_alerts: dict[tuple[str, str], int] | None = None,
) -> None:
    color, foreground = _colors(manager_game.game.slug)
    group_count, team_count, _ = _game_statistics(manager_game, groups, index)
    player_snapshot = (
        None if player_index is None else player_index.newest(manager_game.game)
    )
    metadata = (metadata_by_identity or {}).get(manager_game.identity)
    signals: list[str] = []
    alert_count = (active_alerts or {}).get(manager_game.identity, 0)
    if alert_count:
        signals.append(count_label(alert_count, "aktiv alarm", "aktive alarmer"))
    if player_snapshot is None:
        signals.append("Spillerdata mangler")
    else:
        signals.append(
            f"Spillerdata: {format_relative_precise(player_snapshot.generated_at)}"
        )
    schedule_action = next_schedule_action(metadata)
    signals.append(schedule_action or "Tidsplan er ikke verificeret")
    _navigation_card(
        card_key=f"game-{manager_game.game.locale}-{manager_game.game.slug}",
        title=manager_game.name,
        subtitle=sport_label(manager_game.game.slug),
        detail=f"{_group_count_label(group_count)} · {team_count} unikke hold",
        color=color,
        foreground=foreground,
        aria_label=f"Åbn managerspil {manager_game.name}",
        icon=_sport_icon(manager_game.game.slug),
        action="Åbn managerspil",
        signals=tuple(signals),
        metadata=manager_game.game.slug,
        view="game",
        locale=manager_game.game.locale,
        game=manager_game.game.slug,
    )


def _home(
    games: tuple[ManagerGame, ...],
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
) -> None:
    with st.container(
        key="home-header",
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
    ):
        st.title("Mine managerspil", anchor="mine-managerspil")
        with st.container(horizontal=True):
            if st.button(
                "Spillerstatistik",
                key="standalone-player-statistics-home",
                icon=":material/query_stats:",
                width="content",
            ):
                _navigate("players")
            if st.button(
                "Holdstatistik",
                key="standalone-team-statistics-home",
                icon=":material/groups:",
                width="content",
            ):
                _navigate("teams")
            if st.button(
                "Tilføj managerspil",
                key="add-manager-game-home",
                icon=":material/add:",
                type="primary",
                width="content",
            ):
                _navigate("manage-games")
    st.caption("Grupper og turneringer samlet efter managerspil.")
    home_data_slot = st.container()
    with home_data_slot:
        with st.skeleton(height=120):
            player_index = PlayerStatisticsStore(OUTPUT_DIR).scan()
            metadata_values, _ = GameMetadataStore(
                APP_PATHS.game_metadata_dir
            ).scan()
            metadata_by_identity = {
                item.identity: item for item in metadata_values
            }
            active_alerts = _active_alert_counts()
    if games:
        with st.container(horizontal=True, gap="medium", key="home-game-grid"):
            for manager_game in _sorted_manager_games(games):
                with st.container(
                    width=520,
                    key=(
                        f"home-game-slot-{manager_game.game.locale}-"
                        f"{manager_game.game.slug}"
                    ),
                ):
                    _manager_game_card(
                        manager_game,
                        groups,
                        index,
                        player_index,
                        metadata_by_identity,
                        active_alerts,
                    )
    else:
        st.info("Du har ingen managerspil endnu. Tilføj dit første managerspil.")


def _archive_date(manager_game: ManagerGame) -> str:
    if manager_game.archived_at is None:
        return "–"
    return datetime.fromisoformat(manager_game.archived_at).astimezone().strftime(
        "%d.%m.%Y %H:%M"
    )


def _restore_manager_game(store: GroupStore, manager_game: ManagerGame) -> None:
    try:
        store.restore_manager_game(manager_game.game)
    except PayloadError as exc:
        st.error(str(exc))
    else:
        _navigate(
            "game", locale=manager_game.game.locale, game=manager_game.game.slug
        )


def _archive_view(
    games: tuple[ManagerGame, ...],
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
) -> None:
    st.title("Arkiverede managerspil", anchor="arkiverede-managerspil")
    st.caption(
        "Arkiverede managerspil og deres data bevares lokalt. Åbn et spil "
        "for at gendanne, opdatere eller redigere det igen."
    )
    archived = _sorted_manager_games(
        game for game in games if game.is_archived
    )
    if not archived:
        st.info("Der er ingen arkiverede managerspil.")
        return
    columns = st.columns(2)
    for position, manager_game in enumerate(archived):
        with columns[position % 2]:
            color, foreground = _colors(manager_game.game.slug)
            group_count, team_count, _ = _game_statistics(
                manager_game, groups, index
            )
            newest_snapshot = max(
                (
                    snapshot
                    for snapshot in index.snapshots
                    if _game_identity(snapshot.team.reference.game)
                    == manager_game.identity
                ),
                key=lambda snapshot: snapshot.generated_at,
                default=None,
            )
            _navigation_card(
                card_key=f"game-{manager_game.game.locale}-{manager_game.game.slug}",
                title=manager_game.name,
                subtitle=manager_game.game.slug,
                detail=(
                    f"Arkiveret {_archive_date(manager_game)} · "
                    f"{_group_count_label(group_count)} · {team_count} unikke hold · "
                    f"{_snapshot_data_status(newest_snapshot)}"
                ),
                color=color,
                foreground=foreground,
                aria_label=f"Åbn arkiveret managerspil {manager_game.name}",
                icon=_sport_icon(manager_game.game.slug),
                action="\u00c5bn arkivspillet",
                view="game",
                locale=manager_game.game.locale,
                game=manager_game.game.slug,
            )



def _archived_banner(
    store: GroupStore,
    manager_game: ManagerGame,
    *,
    allow_restore: bool,
) -> None:
    st.warning(
        f"{manager_game.name} er arkiveret. Du ser kun lokalt gemte data; "
        "hentning og redigering er deaktiveret."
    )
    if allow_restore and st.button(
        "Gendan managerspil",
        icon=":material/unarchive:",
        key=f"restore-current-{manager_game.game.locale}-{manager_game.game.slug}",
    ):
        _restore_manager_game(store, manager_game)


def _manage_games_view(store: GroupStore) -> None:
    st.title("Administrer managerspil", anchor="administrer-managerspil")
    st.caption("Tilføj et Holdet-managerspil med en URL eller slug. Der hentes ingen data nu.")
    with st.form("create-manager-game"):
        source = st.text_input(
            "Holdet-URL eller slug",
            placeholder="super-manager-fall-2026",
        )
        name = st.text_input("Navn (valgfrit)")
        submitted = st.form_submit_button("Tilføj managerspil", type="primary")
    if submitted:
        try:
            manager_game = store.create_manager_game(source, name)
        except (PayloadError, ValueError) as exc:
            st.error(str(exc))
        else:
            _navigate(
                "game",
                locale=manager_game.game.locale,
                game=manager_game.game.slug,
            )


def _player_statistics_rows(
    statistics, query: PlayerStatisticsQuery | None = None
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    return player_display_rows(statistics, query or PlayerStatisticsQuery())


@st.cache_data(max_entries=16, show_spinner=False)
def _player_decision_analysis_batch(
    player_index,
    game: GameUrl,
    player_keys: tuple[str, ...],
):
    """Cache pure derived statistics by immutable snapshot inputs."""

    return tuple(
        build_player_decision_analysis(player_index, game, player_key)
        for player_key in player_keys
    )


def _network_failure(message: str, details: str) -> None:
    st.warning(message)
    with st.expander("Tekniske detaljer"):
        st.code(details)


def _action_failure(message: str, error: Exception, *, warning: bool = False) -> None:
    if isinstance(error, FetchError):
        _network_failure(
            f"{message}. Holdet kunne ikke kontaktes efter flere forsøg.",
            str(error),
        )
        return
    renderer = st.warning if warning else st.error
    renderer(f"{message}: {error}")


def _player_failure_details(error: object) -> tuple[str, int | None, bool]:
    if isinstance(error, dict):
        details = str(error.get("details", "Ukendt fejl"))
        round_number = error.get("round_number")
        if not isinstance(round_number, int):
            round_number = None
        return details, round_number, bool(error.get("network", False))
    # Backwards compatibility for string values kept in a running session.
    return str(error), None, True


def _fetch_player_statistics(game: GameUrl, round_number: int | None) -> None:
    error_key = (game.locale.casefold(), game.slug)
    snapshot_store = PlayerStatisticsStore(OUTPUT_DIR)
    before_index = snapshot_store.scan(game)
    alert_warning: str | None = None
    try:
        with st.spinner(
            "Henter seneste spillerstatistik …"
            if round_number is None
            else f"Henter spillerstatistik for runde {round_number} …"
        ):
            _save_game_metadata_if_available(game)

            statistics = _client().fetch_players(game, round_number=round_number)
            if round_number is not None and statistics.round_number != round_number:
                raise PayloadError(
                    f"Holdet returnerede runde {statistics.round_number} "
                    f"i stedet for runde {round_number}."
                )
            saved = snapshot_store.save(statistics)
            alert_count = 0
            previous = select_alert_baseline(
                before_index,
                game,
                statistics.round_number,
            )
            if round_number is None and previous is not None:
                try:
                    current = snapshot_store.scan(game).newest(
                        game, statistics.round_number
                    )
                    if current is not None:
                        settings = HubSettingsStore(
                            APP_PATHS.hub_settings_file
                        ).load()
                        alerts = build_watchlist_alerts(
                            previous,
                            current,
                            settings.watchlist,
                        )
                        AnalysisInboxStore(
                            APP_PATHS.analysis_inbox_file
                        ).merge(alerts)
                        alert_count = len(alerts)
                except (OSError, PayloadError, ValueError) as exc:
                    alert_warning = str(exc)
    except Exception as exc:
        st.session_state.setdefault("player_statistics_errors", {})[error_key] = {
            "details": str(exc),
            "network": isinstance(exc, FetchError),
            "round_number": round_number,
        }
        st.rerun()
    else:
        st.session_state.setdefault("player_statistics_errors", {}).pop(error_key, None)
        st.session_state.setdefault("player_statistics_notices", {})[error_key] = (
            f"Runde {statistics.round_number} blev hentet og gemt som {saved.name}."
            + (
                f" {alert_count} nye statusalarmer blev oprettet."
                if alert_count
                else ""
            )
        )
        if alert_count:
            st.session_state.setdefault(
                "player_statistics_alert_counts", {}
            )[error_key] = alert_count
        if alert_warning:
            st.session_state.setdefault(
                "player_statistics_alert_warnings", {}
            )[error_key] = alert_warning
        st.rerun()


@dataclass(frozen=True, slots=True)
class _PlayerBatchResult:
    fetched: tuple[int, ...]
    skipped: tuple[int, ...]
    failures: tuple[tuple[int, str], ...]


def _fetch_missing_player_rounds(
    game: GameUrl,
    from_round: int,
    to_round: int,
    *,
    store: PlayerStatisticsStore | None = None,
    client: HoldetClient | None = None,
) -> _PlayerBatchResult:
    """Fetch uncached rounds in ascending order with one reused client."""

    if from_round < 1 or to_round < from_round:
        raise ValueError("Rundeintervallet er ugyldigt.")
    if client is None:
        _save_game_metadata_if_available(game)
    snapshot_store = store or PlayerStatisticsStore(OUTPUT_DIR)
    holdet_client = client or _client()
    cached = set(snapshot_store.scan(game).rounds_for(game))
    fetched: list[int] = []
    skipped: list[int] = []
    failures: list[tuple[int, str]] = []
    for round_number in range(from_round, to_round + 1):
        if round_number in cached:
            skipped.append(round_number)
            continue
        try:
            statistics = holdet_client.fetch_players(
                game, round_number=round_number
            )
            if statistics.round_number != round_number:
                raise PayloadError(
                    f"Holdet returnerede runde {statistics.round_number} "
                    f"i stedet for runde {round_number}."
                )
            snapshot_store.save(statistics)
        except Exception as exc:
            failures.append((round_number, str(exc)))
            continue
        fetched.append(round_number)
        cached.add(round_number)
    return _PlayerBatchResult(
        tuple(fetched),
        tuple(skipped),
        tuple(failures),
    )


def _optional_number_input(label: str, key: str) -> int | None:
    value = st.number_input(
        label,
        value=None,
        step=1_000,
        key=key,
        persist_state="session",
    )
    return int(value) if value is not None else None


def _set_player_filter_state(
    scope: str,
    query: PlayerStatisticsQuery,
) -> None:
    """Apply a validated profile to the existing native filter widgets."""

    st.session_state[f"{scope}-search"] = query.search
    st.session_state[f"{scope}-min-value"] = query.min_value
    st.session_state[f"{scope}-max-value"] = query.max_value
    st.session_state[f"{scope}-teams"] = list(query.teams)
    st.session_state[f"{scope}-positions"] = list(query.positions)
    st.session_state[f"{scope}-min-total-growth"] = query.min_total_growth
    st.session_state[f"{scope}-max-total-growth"] = query.max_total_growth
    st.session_state[f"{scope}-min-round-growth"] = query.min_round_growth
    st.session_state[f"{scope}-max-round-growth"] = query.max_round_growth
    st.session_state[f"{scope}-missing-total"] = query.missing_total_growth
    st.session_state[f"{scope}-missing-round"] = query.missing_round_growth
    st.session_state[f"{scope}-columns"] = list(query.columns[1:])
    st.session_state[f"{scope}-sort-field"] = query.sort_field
    st.session_state[f"{scope}-sort-order"] = query.sort_order
    labels = {"ignore": "Ignorér", "require": "Kræv", "exclude": "Udeluk"}
    for status in PLAYER_STATUSES:
        st.session_state[f"{scope}-status-{status}"] = labels[
            query.status_rule(status)
        ]
    st.session_state[f"{scope}-applied-query"] = query


def _player_filter_chips(
    statistics,
    query: PlayerStatisticsQuery,
) -> tuple[str, ...]:
    labels = player_column_labels(statistics)
    chips: list[str] = []
    if query.search.strip():
        chips.append(f"Søg: {query.search.strip()}")
    if query.teams:
        chips.append(
            f"{labels['team']}: {query.teams[0]}"
            + (f" +{len(query.teams) - 1}" if len(query.teams) > 1 else "")
        )
    if query.positions:
        chips.append(
            f"{labels['position']}: {query.positions[0]}"
            + (f" +{len(query.positions) - 1}" if len(query.positions) > 1 else "")
        )
    for label, minimum, maximum in (
        (labels["value"], query.min_value, query.max_value),
        (labels["total_growth"], query.min_total_growth, query.max_total_growth),
        (labels["round_growth"], query.min_round_growth, query.max_round_growth),
    ):
        if minimum is not None and maximum is not None:
            chips.append(
                f"{label}: {format_integer(minimum)}–{format_integer(maximum)}"
            )
        elif minimum is not None:
            chips.append(f"{label} ≥ {format_integer(minimum)}")
        elif maximum is not None:
            chips.append(f"{label} ≤ {format_integer(maximum)}")
    rule_labels = {"require": "kræv", "exclude": "udeluk"}
    for status, rule in query.status_rules:
        if rule != "ignore":
            chips.append(f"{STATUS_LABELS_DA[status]}: {rule_labels[rule]}")
    if query.missing_total_growth != "include":
        chips.append(f"Manglende {labels['total_growth'].casefold()}")
    if query.missing_round_growth != "include":
        chips.append(f"Manglende {labels['round_growth'].casefold()}")
    if query.columns != PLAYER_COLUMNS:
        chips.append(f"Kolonner: {len(query.columns)}")
    if query.sort_field != "value" or query.sort_order != "desc":
        direction = "stigende" if query.sort_order == "asc" else "faldende"
        sort_label = "Kilderækkefølge" if query.sort_field == "source" else labels[query.sort_field]
        chips.append(f"Sortering: {sort_label}, {direction}")
    return tuple(chips)


def _built_in_player_filters(statistics) -> tuple[tuple[str, str, PlayerStatisticsQuery], ...]:
    profiles: list[tuple[str, str, PlayerStatisticsQuery]] = []
    money_game = statistics.unit != "points"
    defenders = tuple(
        sorted(
            {
                item.position
                for item in statistics.entries
                if "forsvar" in item.position.casefold()
                or "defender" in item.position.casefold()
            },
            key=str.casefold,
        )
    )
    if money_game and defenders:
        profiles.append(
            (
                "builtin-cheap-defenders",
                "Billige forsvarere",
                PlayerStatisticsQuery(
                    positions=defenders,
                    max_value=5_000_000,
                    sort_field="value",
                    sort_order="asc",
                ),
            )
        )
    if money_game:
        profiles.append(
            (
                "builtin-active-under-five",
                "Aktive under 5 mio.",
                PlayerStatisticsQuery(
                    max_value=5_000_000,
                    status_rules=(("inactive", "exclude"), ("disabled", "exclude")),
                    sort_field="total_growth",
                    sort_order="desc",
                ),
            )
        )
    profiles.append(
        (
            "builtin-injured",
            "Skadede spillere",
            PlayerStatisticsQuery(
                status_rules=(("injured", "require"),),
                sort_field="value",
                sort_order="desc",
            ),
        )
    )
    return tuple(profiles)


def _player_filter_profile_manager(statistics, scope: str) -> tuple[object, object]:
    game = statistics.game
    store = HubSettingsStore(APP_PATHS.hub_settings_file)
    settings = store.load()
    saved = tuple(
        item
        for item in settings.saved_player_filters
        if item.game_locale.casefold() == game.locale.casefold()
        and item.game_slug == game.slug
    )
    profiles = {
        profile_id: (name, query, None)
        for profile_id, name, query in _built_in_player_filters(statistics)
    }
    profiles.update(
        {
            f"saved-{item.filter_id}": (item.name, item.query, item)
            for item in saved
        }
    )
    if profiles:
        with st.container(horizontal=True, vertical_alignment="bottom"):
            selected_id = st.selectbox(
                "Filterprofil",
                tuple(profiles),
                format_func=lambda profile_id: profiles[profile_id][0],
                key=f"{scope}-profile",
                persist_state="session",
            )
            st.button(
                "Anvend profil",
                icon=":material/filter_alt:",
                key=f"{scope}-apply-profile",
                on_click=_set_player_filter_state,
                args=(scope, profiles[selected_id][1]),
            )
        selected_saved = profiles[selected_id][2]
        if selected_saved is not None:
            with st.expander("Administrer gemt filterprofil"):
                renamed = st.text_input(
                    "Profilnavn",
                    value=selected_saved.name,
                    max_chars=80,
                    key=f"{scope}-rename-profile",
                )
                actions = st.columns(2)
                if actions[0].button(
                    "Gem nyt navn", key=f"{scope}-save-profile-name"
                ):
                    values = tuple(
                        replace(item, name=renamed.strip())
                        if item.filter_id == selected_saved.filter_id
                        else item
                        for item in settings.saved_player_filters
                    )
                    try:
                        store.set_saved_player_filters(settings, values)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
                if actions[1].button(
                    "Slet profil", key=f"{scope}-delete-profile"
                ):
                    values = tuple(
                        item
                        for item in settings.saved_player_filters
                        if item.filter_id != selected_saved.filter_id
                    )
                    store.set_saved_player_filters(settings, values)
                    st.rerun()
    return store, settings


def _save_player_filter_profile(
    statistics,
    scope: str,
    query: PlayerStatisticsQuery,
    store,
    settings,
) -> None:
    with st.expander("Gem aktuelle filtre"):
        with st.form(f"{scope}-save-filter-profile"):
            name = st.text_input("Profilnavn", max_chars=80)
            submitted = st.form_submit_button("Gem filterprofil")
        if submitted:
            if not name.strip():
                st.error("Profilnavnet må ikke være tomt.")
            else:
                profile = SavedPlayerFilter(
                    uuid4().hex,
                    name.strip(),
                    statistics.game.locale,
                    statistics.game.slug,
                    query,
                )
                try:
                    store.set_saved_player_filters(
                        settings,
                        (*settings.saved_player_filters, profile),
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()


def _player_filter_query(statistics, scope: str) -> PlayerStatisticsQuery:
    labels = player_column_labels(statistics)
    search = st.text_input(
        "Søg",
        placeholder="Navn, hold/land eller position/kategori",
        key=f"{scope}-search",
        persist_state="session",
    )
    value_columns = st.columns(2)
    with value_columns[0]:
        min_value = _optional_number_input(
            f"Minimum {labels['value'].casefold()}", f"{scope}-min-value"
        )
    with value_columns[1]:
        max_value = _optional_number_input(
            f"Maksimum {labels['value'].casefold()}", f"{scope}-max-value"
        )

    st.markdown("**Statusfiltre**")
    status_choices = ("Ignorér", "Kræv", "Udeluk")
    status_to_rule = {"Ignorér": "ignore", "Kræv": "require", "Udeluk": "exclude"}
    status_columns = st.columns(4)
    rules: list[tuple[str, str]] = []
    for column, status in zip(status_columns, PLAYER_STATUSES, strict=True):
        with column:
            choice = st.selectbox(
                STATUS_LABELS_DA[status],
                status_choices,
                key=f"{scope}-status-{status}",
                persist_state="session",
            )
        if status_to_rule[choice] != "ignore":
            rules.append((status, status_to_rule[choice]))

    teams = tuple(sorted({entry.team for entry in statistics.entries}, key=str.casefold))
    positions = tuple(
        sorted({entry.position for entry in statistics.entries}, key=str.casefold)
    )
    with st.expander("Avancerede filtre og kolonner"):
        selected_teams = tuple(
            st.multiselect(
                labels["team"], teams, key=f"{scope}-teams",
                persist_state="session",
            )
        )
        selected_positions = tuple(
            st.multiselect(
                labels["position"], positions, key=f"{scope}-positions",
                persist_state="session",
            )
        )
        total_columns = st.columns(2)
        with total_columns[0]:
            min_total = _optional_number_input(
                f"Minimum {labels['total_growth'].casefold()}",
                f"{scope}-min-total-growth",
            )
        with total_columns[1]:
            max_total = _optional_number_input(
                f"Maksimum {labels['total_growth'].casefold()}",
                f"{scope}-max-total-growth",
            )
        round_columns = st.columns(2)
        with round_columns[0]:
            min_round = _optional_number_input(
                f"Minimum {labels['round_growth'].casefold()}",
                f"{scope}-min-round-growth",
            )
        with round_columns[1]:
            max_round = _optional_number_input(
                f"Maksimum {labels['round_growth'].casefold()}",
                f"{scope}-max-round-growth",
            )
        missing_labels = {
            "include": "Medtag manglende",
            "exclude": "Udeluk manglende",
            "only": "Kun manglende",
        }
        missing_columns = st.columns(2)
        with missing_columns[0]:
            missing_total = st.selectbox(
                f"Manglende {labels['total_growth'].casefold()}",
                MISSING_VALUE_MODES,
                format_func=missing_labels.__getitem__,
                key=f"{scope}-missing-total",
                persist_state="session",
            )
        with missing_columns[1]:
            missing_round = st.selectbox(
                f"Manglende {labels['round_growth'].casefold()}",
                MISSING_VALUE_MODES,
                format_func=missing_labels.__getitem__,
                key=f"{scope}-missing-round",
                persist_state="session",
            )
        optional_columns = tuple(column for column in PLAYER_COLUMNS if column != "name")
        selected_optional = tuple(
            st.multiselect(
                "Kolonner",
                optional_columns,
                default=optional_columns,
                format_func=labels.__getitem__,
                key=f"{scope}-columns",
                persist_state="session",
            )
        )
        sort_columns = st.columns(2)
        sort_labels = {
            "source": "Oprindelig rækkefølge",
            **{key: labels[key] for key in PLAYER_SORT_FIELDS if key in labels},
        }
        with sort_columns[0]:
            sort_field = st.selectbox(
                "Sortér efter",
                PLAYER_SORT_FIELDS,
                format_func=sort_labels.__getitem__,
                key=f"{scope}-sort-field",
                persist_state="session",
            )
        with sort_columns[1]:
            sort_order = st.selectbox(
                "Sorteringsretning",
                ("desc", "asc"),
                format_func={"desc": "Faldende", "asc": "Stigende"}.__getitem__,
                key=f"{scope}-sort-order",
                persist_state="session",
            )

    return PlayerStatisticsQuery(
        search=search,
        teams=selected_teams,
        positions=selected_positions,
        min_value=min_value,
        max_value=max_value,
        min_total_growth=min_total,
        max_total_growth=max_total,
        min_round_growth=min_round,
        max_round_growth=max_round,
        missing_total_growth=missing_total,
        missing_round_growth=missing_round,
        status_rules=tuple(rules),
        columns=("name",) + selected_optional,
        sort_field=sort_field,
        sort_order=sort_order,
    )


def _reset_player_filters(scope: str) -> None:
    prefixes = (
        f"{scope}-search", f"{scope}-min-", f"{scope}-max-",
        f"{scope}-status-", f"{scope}-teams", f"{scope}-positions",
        f"{scope}-missing-", f"{scope}-columns", f"{scope}-sort-",
        f"{scope}-applied-query",
    )
    for key in tuple(st.session_state):
        if any(str(key).startswith(prefix) for prefix in prefixes):
            st.session_state.pop(key, None)


def _player_export_controls(statistics, query, selected, scope: str) -> None:
    format_labels = {
        "txt": "TXT",
        "json": "JSON",
        "md": "Markdown",
        "csv": "CSV",
        "xlsx": "XLSX",
        "parquet": "Parquet",
    }
    formats = st.pills(
        "Filformater",
        PLAYER_EXPORT_FORMATS,
        default=("txt",),
        selection_mode="multi",
        format_func=format_labels.__getitem__,
        key=f"{scope}-export-formats",
    )
    ready_key = f"{scope}-ready-export-{statistics.round_number}"
    if st.button(
        "Opret eksport",
        icon=":material/file_export:",
        key=f"{scope}-create-export-{statistics.round_number}",
        disabled=not formats,
    ):
        try:
            document = build_player_export(
                statistics,
                query,
                generated_at=datetime.now().astimezone(),
                source_generated_at=selected.generated_at,
            )
            artifacts = PlayerExportStore(PLAYER_EXPORT_DIR).save(
                document, tuple(formats or ())
            )
        except (PayloadError, OSError, ValueError) as exc:
            st.error(f"Eksporten kunne ikke oprettes: {exc}")
        else:
            st.session_state[ready_key] = artifacts
            st.rerun()
    artifacts = st.session_state.get(ready_key, ())
    if not artifacts:
        return
    st.success("Eksporten er gemt lokalt og klar til download.")
    for artifact in artifacts:
        st.code(str(artifact.path))
    with st.container(horizontal=True):
        for artifact in artifacts:
            st.download_button(
                f"Download {artifact.format.upper()}",
                data=artifact.content,
                file_name=artifact.path.name,
                mime=artifact.mime_type,
                icon=":material/download:",
                on_click="ignore",
                key=f"{scope}-download-{statistics.round_number}-{artifact.format}-{artifact.path.name}",
            )


def _player_export_section(statistics, query, selected, scope: str) -> None:
    with st.expander(
        "Eksport",
        icon=":material/file_export:",
    ):
        _player_export_controls(statistics, query, selected, scope)



@st.fragment
def _player_list_panel(selected, empty_label: str) -> None:
    statistics = selected.statistics
    game = statistics.game
    scope = f"player-filter-{game.locale}-{game.slug}"
    applied_key = f"{scope}-applied-query"
    applied_query = st.session_state.get(applied_key, PlayerStatisticsQuery())
    active_chips = _player_filter_chips(statistics, applied_query)
    with st.container(
        key=f"sticky-action-bar-player-{game.locale}-{game.slug}",
        horizontal=True,
        vertical_alignment="center",
        gap="small",
    ):
        with st.popover(
            f"Filtre · {len(active_chips)}",
            icon=":material/filter_alt:",
            key=f"{scope}-filter-panel",
        ):
            settings_store, settings = _player_filter_profile_manager(
                statistics, scope
            )
            with st.form(f"{scope}-filters", border=False):
                try:
                    query = _player_filter_query(statistics, scope)
                except ValueError as exc:
                    st.error(str(exc))
                    return
                submitted = st.form_submit_button(
                    "Anvend filtre",
                    icon=":material/filter_alt:",
                    type="primary",
                )
            _save_player_filter_profile(
                statistics,
                scope,
                query,
                settings_store,
                settings,
            )
        for chip in active_chips[:6]:
            st.badge(chip, color="gray")
        if len(active_chips) > 6:
            st.badge(f"+{len(active_chips) - 6} filtre", color="gray")
        st.button(
            "Nulstil",
            icon=":material/filter_alt_off:",
            key=f"{scope}-reset-filters",
            on_click=_reset_player_filters,
            args=(scope,),
            disabled=not active_chips,
        )
    if submitted:
        st.session_state[applied_key] = query
        st.rerun()
    st.session_state.setdefault(applied_key, query)
    applied_query = st.session_state[applied_key]
    entries = filter_player_statistics(statistics, applied_query)
    with st.container(
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
    ):
        st.caption(
            f"{len(entries)} af {len(statistics.entries)} spillere · runde "
            f"{statistics.round_number} · {format_relative_precise(selected.generated_at)}"
        )
    if entries:
        results_slot = st.container()
        with results_slot:
            with st.skeleton(height=180):
                rows, integer_columns = _player_statistics_rows(
                    statistics, applied_query
                )
                player_index = PlayerStatisticsStore(OUTPUT_DIR).scan(game)
                player_keys = tuple(
                    player_identity(game, entry) for entry in entries
                )
                analyses = _player_decision_analysis_batch(
                    player_index,
                    game,
                    player_keys,
                )
                annotations = {
                    item.player_key: item
                    for item in settings.player_annotations
                    if item.game_locale.casefold() == game.locale.casefold()
                    and item.game_slug == game.slug
                }
                for row, entry, key, analysis in zip(
                    rows,
                    entries,
                    player_keys,
                    analyses,
                    strict=True,
                ):
                    if statistics.unit != "points":
                        row["Vækst pr. mio."] = (
                            "–"
                            if analysis is None or analysis.growth_per_million is None
                            else f"{analysis.growth_per_million:.1f}".replace(".", ",")
                        )
                    row["Form 3"] = (
                        "–"
                        if analysis is None or analysis.form_3 is None
                        else f"{analysis.form_3:.1f}".replace(".", ",")
                    )
                    row["Form 5"] = (
                        "–"
                        if analysis is None or analysis.form_5 is None
                        else f"{analysis.form_5:.1f}".replace(".", ",")
                    )
                    row["Stabilitet"] = (
                        "–"
                        if analysis is None or analysis.stability_score is None
                        else f"{analysis.stability_score}/100 · {analysis.stability_label}"
                    )
                    row["Datastatus"] = data_status_label(
                        "unverified"
                        if analysis is None
                        else analysis.provenance.certainty
                    )
                    row["Grundlag"] = (
                        0 if analysis is None else analysis.provenance.sample_size
                    )
                    row["Tags"] = (
                        " · ".join(annotations[key].tags)
                        if key in annotations
                        else "–"
                    )
                    row["Detaljer"] = relative_url(
                        PageId.PLAYER,
                        locale=game.locale,
                        game=game.slug,
                        player=key,
                        round=statistics.round_number,
                    )
            dataframe(
                _style_integer_columns(rows, integer_columns),
                hide_index=True,
                width="stretch",
                column_config={
                    "Detaljer": st.column_config.LinkColumn(
                        "Detaljer", display_text="Åbn spiller"
                    ),
                    "Form 3": st.column_config.Column(
                        "Form 3",
                        help="Gennemsnitlig udvikling over de seneste tre runder.",
                    ),
                    "Form 5": st.column_config.Column(
                        "Form 5",
                        help="Gennemsnitlig udvikling over de seneste fem runder.",
                    ),
                    "Vækst pr. mio.": st.column_config.Column(
                        "Vækst pr. mio.",
                        help="Historisk vækst divideret med den aktuelle pris i millioner.",
                    ),
                },
                key=(
                    f"player-statistics:{game.locale}:{game.slug}:v1"
                ),
            )
        _player_export_section(statistics, applied_query, selected, scope)
    else:
        st.info("Ingen spillere matcher de valgte filtre.")


def _player_statistics_panel(
    game: GameUrl,
    *,
    read_only: bool = False,
    empty_label: str = "spillet",
) -> None:
    store = PlayerStatisticsStore(OUTPUT_DIR)
    index = store.scan(game)
    rounds = index.rounds_for(game)
    identity = (game.locale.casefold(), game.slug)
    notice = st.session_state.setdefault("player_statistics_notices", {}).pop(identity, None)
    if notice:
        st.success(notice)
    alert_count = st.session_state.setdefault(
        "player_statistics_alert_counts", {}
    ).pop(identity, 0)
    if alert_count:
        page_link(
            PageId.ALERTS,
            f"Se statusalarmer ({alert_count})",
            icon=":material/notifications:",
            locale=game.locale,
            game=game.slug,
        )
    alert_warning = st.session_state.setdefault(
        "player_statistics_alert_warnings", {}
    ).pop(identity, None)
    if alert_warning:
        st.warning(
            "Spillerdata blev gemt, men statusalarmer kunne ikke opdateres: "
            f"{alert_warning}"
        )
    batch = st.session_state.setdefault(
        "player_statistics_batch_results", {}
    ).pop(identity, None)
    if isinstance(batch, _PlayerBatchResult):
        summary = (
            f"{len(batch.fetched)} hentet \u00b7 "
            f"{len(batch.skipped)} allerede gemt \u00b7 "
            f"{len(batch.failures)} fejl"
        )
        (st.warning if batch.failures else st.success)(summary)
        if batch.failures:
            with st.expander("Fejl under hentningen"):
                for failed_round, details in batch.failures:
                    st.code(f"Runde {failed_round}: {details}")
    error = st.session_state.get("player_statistics_errors", {}).get(identity)
    if error:
        details, failed_round, is_network = _player_failure_details(error)
        if is_network:
            _network_failure(
                "Holdet kunne ikke kontaktes efter flere forsøg. Cachede data "
                "vises fortsat, hvor det er muligt.",
                details,
            )
        else:
            st.warning(f"Seneste hentning mislykkedes: {details}")
        if not read_only and st.button(
            "Prøv igen",
            icon=":material/refresh:",
            key=(
                f"retry-player-statistics-{game.locale}-{game.slug}-"
                f"{empty_label}-{failed_round if failed_round is not None else 'latest'}"
            ),
        ):
            _fetch_player_statistics(game, failed_round)
    if index.warnings:
        with st.expander(
            f"{count_label(len(index.warnings), 'spillerfil', 'spillerfiler')} "
            "blev ignoreret"
        ):
            st.warning(
                "Nogle spiller-snapshots var beskadigede eller havde en ukendt "
                "version. Resten af statistikken virker fortsat."
            )
            for warning in index.warnings:
                st.code(warning)

    st.caption(
        "Data hentes kun, når du trykker på en henteknap. Tidligere runder "
        "vises direkte fra den lokale cache."
    )
    if st.button(
        "Hent seneste spillerstatistik",
        type="primary",
        icon=":material/download:",
        key=f"fetch-latest-players-{game.locale}-{game.slug}-{empty_label}",
        disabled=read_only,
        help="Gendan managerspillet for at hente data." if read_only else None,
    ):
        _fetch_player_statistics(game, None)

    if not read_only:
        with st.expander("Hent manglende runder"):
            st.caption(
                "Henter kun runder, der ikke allerede er gemt, i stigende "
                "r\u00e6kkef\u00f8lge. En enkelt fejl stopper ikke resten."
            )
            interval = st.columns(2)
            with interval[0]:
                from_round = int(
                    st.number_input(
                        "Fra runde",
                        min_value=1,
                        value=1,
                        step=1,
                        key=f"batch-from-{game.locale}-{game.slug}-{empty_label}",
                    )
                )
            with interval[1]:
                to_round = int(
                    st.number_input(
                        "Til runde",
                        min_value=1,
                        value=max(rounds, default=1),
                        step=1,
                        key=f"batch-to-{game.locale}-{game.slug}-{empty_label}",
                    )
                )
            if st.button(
                "Hent manglende runder",
                icon=":material/download:",
                key=f"batch-fetch-{game.locale}-{game.slug}-{empty_label}",
                disabled=to_round < from_round,
            ):
                with st.spinner(
                    f"Henter manglende runder {from_round}-{to_round} ..."
                ):
                    result = _fetch_missing_player_rounds(
                        game,
                        from_round,
                        to_round,
                        store=store,
                    )
                st.session_state.setdefault(
                    "player_statistics_batch_results", {}
                )[identity] = result
                st.rerun()

    rounds = index.rounds_for(game)
    if not rounds:
        st.info(_local_data_status(None))
        return
    latest_known_round = max(rounds)
    selected_round = _round_selectbox(
        "Runde",
        tuple(range(latest_known_round, 0, -1)) or (0,),
        key=f"player-round-{game.locale}-{game.slug}-{empty_label}",
        default=latest_known_round,
    )
    selected = index.newest(game, selected_round)
    action_label = (
        f"Opdater runde {selected_round}" if selected is not None
        else f"Hent runde {selected_round}"
    )
    if st.button(
        action_label,
        icon=":material/refresh:" if selected is not None else ":material/download:",
        key=f"fetch-player-round-{game.locale}-{game.slug}-{empty_label}",
        disabled=read_only,
        help="Gendan managerspillet for at hente data." if read_only else None,
    ):
        _fetch_player_statistics(game, selected_round)
    if selected is None:
        st.info(
            f"Runde {selected_round} findes ikke i den lokale cache."
            if read_only else
            f"Runde {selected_round} er ikke hentet endnu. Tryk på ‘Hent runde {selected_round}’."
        )
        return

    statistics = selected.statistics
    status_text = _local_data_status(
        selected.generated_at,
        statistics.round_number,
        statistics.round_status,
    )
    if statistics.round_status == "complete":
        st.caption(status_text)
    elif statistics.round_status == "in_progress":
        st.warning(
            f"{status_text}. Data kan vises, men turneringspoint gives "
            "f\u00f8rst, n\u00e5r runden er afsluttet og hentet igen."
        )
    else:
        st.warning(
            f"{status_text}. Hent runden igen for at bekr\u00e6fte den, "
            "f\u00f8r data kan give turneringspoint."
        )
    player_tabs = _stateful_tabs(
        (
            "Spillerliste",
            "Sammenligning og watchlist",
            "Ændringer",
        ),
        ("list", "compare", "changes"),
        key=(
            f"player-panels-{game.locale}-{game.slug}-{empty_label}"
        ),
        parameter="panel",
    )
    list_tab, compare_tab, changes_tab = player_tabs
    if list_tab.open:
        with list_tab:
            _player_list_panel(selected, empty_label)
    if compare_tab.open:
        with compare_tab:
            player_compare_panel(
                game,
                index,
                APP_PATHS,
                int(selected_round),
            )
    if changes_tab.open:
        with changes_tab:
            player_changes_panel(
                game,
                index,
                _scan_snapshots(str(OUTPUT_DIR.resolve())),
                int(selected_round),
            )

def _player_statistics_tab(
    manager_game: ManagerGame, *, read_only: bool = False
) -> None:
    st.header("Spillerstatistik", anchor="spillerstatistik")
    _player_statistics_panel(
        manager_game.game,
        read_only=read_only,
        empty_label="managerspillet",
    )


def _standalone_player_statistics(games: tuple[ManagerGame, ...]) -> None:
    st.title("Spillerstatistik", anchor="spillerstatistik")
    st.caption(
        "Vælg et gemt spil, eller indtast en Holdet-URL eller slug. Spillet "
        "bliver ikke føjet til Mine managerspil."
    )
    index = PlayerStatisticsStore(OUTPUT_DIR).scan()
    suggestions: dict[tuple[str, str], tuple[GameUrl, str]] = {}
    for manager_game in games:
        suggestions.setdefault(
            manager_game.identity,
            (manager_game.game, manager_game.name),
        )
    for snapshot in index.snapshots:
        game = snapshot.statistics.game
        suggestions.setdefault(
            (game.locale.casefold(), game.slug),
            (game, game.slug),
        )
    sorted_suggestions = sorted(
        suggestions.values(),
        key=_game_label_sort_key,
    )
    option_values = [item[0].original for item in sorted_suggestions]
    option_labels = {
        item[0].original: f"{item[1]} · {item[0].slug}"
        for item in sorted_suggestions
    }
    selection_key = "standalone-player-game"
    requested_game = _requested_game_from_query()
    if requested_game is not None:
        requested = suggestions.get(
            (requested_game.locale.casefold(), requested_game.slug)
        )
        st.session_state[selection_key] = (
            requested[0].original if requested is not None else requested_game.original
        )

    def sync_standalone_game() -> None:
        selected = st.session_state.get(selection_key)
        if not selected:
            return
        try:
            selected_game = normalize_manager_game(str(selected)).game
        except (PayloadError, ValueError):
            return
        st.query_params["locale"] = selected_game.locale
        st.query_params["game"] = selected_game.slug

    selected_source = st.selectbox(
        "Spil eller Holdet-URL",
        option_values,
        index=None,
        accept_new_options=True,
        placeholder="Vælg et spil, eller indtast URL/slug",
        format_func=lambda value: option_labels.get(value, value),
        key=selection_key,
        on_change=sync_standalone_game,
    )
    if not selected_source:
        st.info("Vælg eller indtast et managerspil for at se spillerstatistik.")
        return
    try:
        game = normalize_manager_game(str(selected_source)).game
    except (PayloadError, ValueError) as exc:
        st.error(f"Ugyldigt managerspil: {exc}")
        return
    unread_alerts = _unread_alert_counts().get(
        (game.locale.casefold(), game.slug), 0
    )
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"Valgt spil: {game.slug} · sprog: {game.locale}")
        page_link(
            PageId.ALERTS,
            (
                f"Se statusalarmer ({unread_alerts})"
                if unread_alerts
                else "Se statusalarmer"
            ),
            icon=":material/notifications:",
            locale=game.locale,
            game=game.slug,
        )
    _player_statistics_panel(game, empty_label="spillet")
@st.dialog("Arkivér managerspil")
def _confirm_archive_manager_game(
    store: GroupStore,
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
) -> None:
    team_count = len({team.team_id for group in groups for team in group.teams})
    st.warning(
        "Managerspillet skjules fra forsiden og den aktive navigation. "
        "Grupper, turneringer, snapshots og andre lokale data bevares."
    )
    st.write(f"**Managerspil:** {manager_game.name}")
    st.write(f"**Grupper:** {len(groups)}")
    st.write(f"**Unikke hold:** {team_count}")
    actions = st.container(horizontal=True)
    with actions:
        confirm = st.button("Bekræft arkivering", type="primary")
        cancel = st.button("Annuller")
    if cancel:
        st.session_state.pop("pending_archive_manager_game", None)
        st.rerun()
    if confirm:
        try:
            _freeze_complete_hall_of_fame(groups, include_round_wins=True)

            store.archive_manager_game(manager_game.game)
        except PayloadError as exc:
            st.error(str(exc))
        else:
            st.session_state.pop("pending_archive_manager_game", None)
            _navigate("archive")


def _refresh_preview_rows(plan: RefreshPlan) -> list[dict[str, object]]:
    """Project a refresh plan for display without touching external state."""

    rows: list[dict[str, object]] = []
    metadata_revalidation = (
        plan.mode == RefreshMode.STALE_ONLY
        and any(
            step.step_id == "metadata" and step.selected
            for step in plan.steps
        )
    )
    for step in plan.steps:
        if step.selected:
            action = "Køres" if step.source == "postprocess" else "Hentes"
        elif (
            metadata_revalidation
            and step.source in {"players", "team"}
            and step.available
        ):
            action = "Genvalideres efter spilinfo"
        elif not step.available:
            action = "Ikke tilgængelig"
        elif step.current_generated_at is not None:
            action = "Cache genbruges"
        else:
            action = "Ikke nødvendig"
        rows.append(
            {
                "Datakilde": step.label,
                "Handling": action,
                "Begrundelse": step.reason,
                "Cache fra": step.current_generated_at,
            }
        )
    return rows


def _refresh_event_label(event: RefreshProgressEvent) -> str:
    labels = {
        "running": "arbejder",
        "fetched": "hentet",
        "reused_current": "aktuel cache genbrugt",
        "reused_after_error": "cache genbrugt efter fejl",
        "failed_no_cache": "fejlet uden cache",
        "skipped_unavailable": "ikke tilgængelig",
    }
    return labels[event.status]


@st.dialog("Forhåndsvis opdatering", width="large")
def _manager_game_refresh_dialog(
    manager_game: ManagerGame,
    game_groups: tuple[GroupDefinition, ...],
    retry_manifest: RefreshManifest | None = None,
) -> None:
    """Preview and run the only network-writing Rundecenter interaction."""

    retry = retry_manifest is not None
    identity = f"{manager_game.game.locale}:{manager_game.game.slug}"
    if retry:
        mode = RefreshMode.RETRY_FAILED
        st.caption(
            f"Kun fejlede trin fra kørsel {retry_manifest.run_id} forsøges igen. "
            "Tidligere succeser bæres med som genbrugt cache."
        )
    else:
        scope = st.segmented_control(
            "Omfang",
            ("Kun forældede data", "Alle data"),
            default="Kun forældede data",
            key=f"refresh-scope:{identity}",
        )
        mode = (
            RefreshMode.ALL
            if scope == "Alle data"
            else RefreshMode.STALE_ONLY
        )

    snapshot_store = SnapshotStore(OUTPUT_DIR)
    player_store = PlayerStatisticsStore(OUTPUT_DIR)
    manifest_store = ManifestStore(MANIFEST_DIR)
    metadata_store = GameMetadataStore(APP_PATHS.game_metadata_dir)
    pending_manifest_key = f"pending-refresh-manifest:{identity}"
    pending_manifest = st.session_state.get(pending_manifest_key)
    if isinstance(pending_manifest, RefreshManifest):
        st.error(
            "Datakilderne blev behandlet, men manifestet kunne ikke gemmes. "
            "Genprøv kun den lokale manifestskrivning; der bruges ikke netværk."
        )
        with st.container(horizontal=True, horizontal_alignment="right"):
            if st.button(
                "Luk",
                key=f"pending-manifest-close:{pending_manifest.run_id}",
            ):
                st.rerun()
            retry_manifest_write = st.button(
                "Prøv manifest igen",
                type="primary",
                icon=":material/save:",
                key=f"pending-manifest-retry:{pending_manifest.run_id}",
            )
        if retry_manifest_write:
            try:
                manifest_path = manifest_store.write(pending_manifest)
            except Exception as exc:
                st.error(f"Manifestet kunne stadig ikke gemmes: {exc}")
            else:
                st.session_state.pop(pending_manifest_key, None)
                st.session_state["game_refresh_notice"] = {
                    "severity": (
                        "warning" if pending_manifest.failures else "success"
                    ),
                    "message": (
                        "Manifestet blev gemt uden nye netværkskald: "
                        f"{manifest_path.name}."
                    ),
                }
                st.rerun()
        return
    latest_manifest = manifest_store.load_latest(
        manager_game.game.slug,
        game_locale=manager_game.game.locale,
        scope="game",
    )
    teams = snapshot_store.scan()
    players = player_store.scan(manager_game.game)
    try:
        metadata = metadata_store.load(manager_game.game)
    except (OSError, PayloadError, ValueError) as exc:
        metadata = None
        st.warning(
            "Spilinfo kan ikke verificeres lokalt og planlægges derfor hentet: "
            f"{exc}"
        )
    plan = build_refresh_plan(
        manager_game,
        game_groups,
        teams,
        players,
        metadata=metadata,
        mode=mode,
        previous_manifest=(retry_manifest if retry else latest_manifest),
        include_metadata=True,
        include_postprocess=True,
    )
    configured_team_count = sum(
        step.source == "team" and step.available for step in plan.steps
    )
    st.write(
        f"Planen behandler spilinfo, spillere og hold. "
        f"**{plan.selected_team_count} af {configured_team_count} unikke hold** "
        f"er planlagt hentet; i alt behandles "
        f"{plan.selected_source_count} trin i denne kørsel."
    )
    if (
        mode == RefreshMode.STALE_ONLY
        and any(
            step.step_id == "metadata" and step.selected
            for step in plan.steps
        )
        and plan.selected_team_count < configured_team_count
    ):
        st.caption(
            "Ny spilinfo kan gøre flere caches forældede. Derfor kan op til "
            f"{configured_team_count} hold blive hentet efter genvalideringen."
        )
    dataframe(
        _refresh_preview_rows(plan),
        hide_index=True,
        key=(
            f"refresh-preview:{identity}:{mode.value}:"
            f"{plan.retry_of or 'normal'}"
        ),
    )
    if not plan.selected_steps:
        st.info(
            "Alle lokale data er aktuelle. Der oprettes ikke et manifest, "
            "før en opdatering faktisk startes."
        )

    actions = st.container(horizontal=True, horizontal_alignment="right")
    with actions:
        close_requested = st.button(
            "Luk",
            key=f"refresh-close:{identity}:{plan.retry_of or mode.value}",
        )
        start_requested = st.button(
            "Start opdatering",
            type="primary",
            icon=":material/sync:",
            disabled=not plan.selected_steps,
            key=f"refresh-start:{identity}:{plan.retry_of or mode.value}",
        )
    if close_requested:
        st.rerun()
    if not start_requested:
        return

    settings_error: str | None = None
    try:
        settings = HubSettingsStore(APP_PATHS.hub_settings_file).load()
    except (OSError, PayloadError, ValueError) as exc:
        settings = None
        settings_error = str(exc)
        st.warning(
            "Statusalarmer kan ikke beregnes i denne kørsel, fordi de lokale "
            f"indstillinger ikke kunne læses: {exc}"
        )
    inbox_store = AnalysisInboxStore(APP_PATHS.analysis_inbox_file)
    completed_teams = 0
    after_errors: set[str] = set()
    after_warnings: set[str] = set()
    published_count = 0
    if settings_error is not None:
        after_errors.add(
            "Statusalarmer: kunne ikke beregnes: " + settings_error
        )

    def collect_pairing_messages(messages: Iterable[str]) -> None:
        for message in messages:
            if "afviger fra de korrigerede resultater" in message:
                after_warnings.add(message)
            else:
                after_errors.add(message)

    def run_postprocess() -> None:
        nonlocal published_count
        _invalidate_snapshot_index()
        refreshed_index = _scan_snapshots(str(OUTPUT_DIR.resolve()))
        resolved_groups, pairing_errors = _with_published_tournament_pairings(
            game_groups, refreshed_index
        )
        collect_pairing_messages(pairing_errors)
        for game_group in resolved_groups:
            try:
                published = _publish_next_swiss_round(
                    game_group, refreshed_index
                )
            except (OSError, PayloadError, ValueError) as exc:
                after_errors.add(f"{game_group.name}: {exc}")
            else:
                published_count += published is not None
        resolved_groups, pairing_errors = _with_published_tournament_pairings(
            game_groups, refreshed_index
        )
        collect_pairing_messages(pairing_errors)
        st.session_state.pop("hall_of_fame_freeze_warning", None)
        _freeze_complete_hall_of_fame(
            resolved_groups,
            include_round_wins=True,
        )
        if warning := st.session_state.pop(
            "hall_of_fame_freeze_warning", None
        ):
            after_errors.add(f"Hall of Fame: {warning}")
        if after_errors:
            raise PayloadError(" | ".join(sorted(after_errors)))

    with st.status(
        "Opdatering kører",
        expanded=True,
        state="running",
    ) as operation:
        progress_bar = st.progress(0.0, text="Forbereder datakilder")
        current_step = st.empty()

        def on_progress(event: RefreshProgressEvent) -> None:
            nonlocal completed_teams
            if event.step.source == "team" and event.status != "running":
                completed_teams += 1
            if event.step.source == "team" and configured_team_count:
                detail = (
                    f"Hold {completed_teams}/op til {configured_team_count} "
                    f"· {event.step.label}"
                )
            else:
                detail = event.step.label
            current_step.write(f"**{detail}** · {_refresh_event_label(event)}")
            if event.status != "running":
                icon = "⚠️" if event.error else "✅"
                operation.write(
                    f"{icon} {event.step.label}: {_refresh_event_label(event)}"
                    + (f" · {event.error}" if event.error else "")
                )
                if event.completed_steps == event.total_steps:
                    current_step.write("**Manifest** · gemmes lokalt")
            fraction = (
                event.completed_steps / (event.total_steps + 1)
                if event.total_steps
                else 0.0
            )
            progress_bar.progress(
                min(1.0, max(0.0, fraction)),
                text=f"Datakilder {event.completed_steps}/{event.total_steps}",
            )

        try:
            result = refresh_manager_game(
                manager_game,
                game_groups,
                _client(),
                snapshot_store,
                player_store,
                manifest_store,
                settings=settings,
                inbox_store=inbox_store,
                metadata_store=metadata_store,
                plan=plan,
                progress=on_progress,
                postprocess=run_postprocess,
            )
        except holdet.ManifestWriteError as exc:
            st.session_state[pending_manifest_key] = exc.manifest
            operation.update(
                label="Data gemt, men manifest mangler",
                state="error",
                expanded=True,
            )
            st.error(
                "Datakilder kan allerede være gemt. Genprøv manifestet uden "
                "netværk via knappen i denne dialog."
            )
            if st.button(
                "Prøv manifest igen",
                type="primary",
                icon=":material/save:",
                key=f"manifest-write-retry-now:{exc.manifest.run_id}",
            ):
                try:
                    manifest_path = manifest_store.write(exc.manifest)
                except Exception as retry_exc:
                    st.error(
                        f"Manifestet kunne stadig ikke gemmes: {retry_exc}"
                    )
                else:
                    st.session_state.pop(pending_manifest_key, None)
                    st.session_state["game_refresh_notice"] = {
                        "severity": (
                            "warning" if exc.manifest.failures else "success"
                        ),
                        "message": (
                            "Manifestet blev gemt uden nye netværkskald: "
                            f"{manifest_path.name}."
                        ),
                    }
                    st.rerun()
            return
        except Exception as exc:
            operation.update(
                label="Opdateringen kunne ikke gennemføres",
                state="error",
                expanded=True,
            )
            st.error(str(exc))
            return
        finally:
            _invalidate_snapshot_index()

        progress_bar.progress(1.0, text="Alle planlagte trin behandlet")
        operation.write(f"✅ Manifest gemt: {result.manifest_path.name}")

        failed_steps = (
            () if result.manifest is None else result.manifest.failures
        )
        partial = bool(failed_steps)
        operation.update(
            label=(
                "Opdatering delvist gennemført"
                if partial
                else "Opdatering gennemført"
            ),
            state="error" if partial else "complete",
            expanded=partial,
        )
        fetched = sum(
            step.status == "fetched"
            for step in (() if result.manifest is None else result.manifest.steps)
        )
        reused = sum(
            step.reused_cache
            for step in (() if result.manifest is None else result.manifest.steps)
        )
        notice = (
            f"{fetched} datakilder hentet · {reused} cachede kilder genbrugt. "
            f"Manifest: {result.manifest_path.name}."
        )
        if published_count:
            notice += f" {published_count} Swiss-runder publiceret."
        if failed_steps:
            notice += (
                f" {len(failed_steps)} trin fejlede og kan forsøges igen "
                "fra manifestpanelet."
            )
        if after_errors:
            notice += " Efterbehandling: " + " | ".join(sorted(after_errors))
        if after_warnings:
            notice += " Turneringsadvarsel: " + " | ".join(
                sorted(after_warnings)
            )
        severity = (
            "error"
            if failed_steps and fetched == 0 and reused == 0
            else "warning"
            if partial or after_warnings
            else "success"
        )
        st.session_state["game_refresh_notice"] = {
            "severity": severity,
            "message": notice,
        }
    st.rerun()



def _game_round_center_tab(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    *,
    read_only: bool = False,
) -> None:
    game_groups = tuple(
        group
        for group in groups
        if _game_identity(group.game) == manager_game.identity
    )
    if notice := st.session_state.pop("game_refresh_notice", None):
        if isinstance(notice, dict):
            message = str(notice.get("message", "Opdateringen er afsluttet."))
            severity = str(notice.get("severity", "success"))
            if severity == "error":
                st.error(message)
            elif severity == "warning":
                st.warning(message)
            else:
                st.success(message)
        else:
            st.success(str(notice))
    resolved_groups, pairing_warnings = _with_published_tournament_pairings(
        game_groups, index
    )
    for warning in pairing_warnings:
        st.warning(warning, icon=":material/warning:")
    unread_counts, alert_error = _unread_alert_state()
    if alert_error is not None:
        st.warning(
            "Statusalarmer kunne ikke verificeres: " + alert_error,
            icon=":material/notification_important:",
        )
    intent = render_round_center(
        manager_game,
        resolved_groups,
        index,
        APP_PATHS,
        unread_alerts=(
            None
            if alert_error is not None
            else unread_counts.get(manager_game.identity, 0)
        ),
        read_only=read_only,
    )
    if read_only or intent.historical:
        return
    if intent.retry_manifest is not None:
        _manager_game_refresh_dialog(
            manager_game, resolved_groups, intent.retry_manifest
        )
    elif intent.refresh_requested:
        _manager_game_refresh_dialog(manager_game, resolved_groups)


def _game_groups_tab(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    *,
    read_only: bool = False,
) -> None:
    game_groups = tuple(
        group
        for group in groups
        if _game_identity(group.game) == manager_game.identity
    )
    group_count, team_count, latest_round = _game_statistics(
        manager_game, groups, index
    )
    with st.container(horizontal=True):
        st.metric("Grupper", group_count, border=True)
        st.metric("Unikke hold", team_count, border=True)
        st.metric("Seneste datarunde", latest_round or "–", border=True)
    if game_groups:
        columns = st.columns(2)
        for position, group in enumerate(game_groups):
            with columns[position % 2]:
                _group_card(group, index)
    else:
        st.info("Managerspillet har ingen grupper endnu.")

def _game_settings_tab(
    manager_game: ManagerGame,
    game_groups: tuple[GroupDefinition, ...],
    store: GroupStore,
    *,
    read_only: bool = False,
) -> None:
    info_key = manager_game.identity
    info = st.session_state.get("game_info", {}).get(info_key)
    if read_only:
        st.info(
            "Spilindstillingerne er skrivebeskyttede, mens managerspillet er arkiveret."
        )
        st.write(f"**Visningsnavn:** {manager_game.name}")
        st.write(f"**Spil:** {manager_game.game.slug}")
        st.write(f"**Sprog:** {manager_game.game.locale}")
        st.write(f"**Grupper:** {len(game_groups)}")
        if manager_game.archived_at:
            st.write(f"**Arkiveret:** {_archive_date(manager_game)}")
        return

    if st.session_state.get("pending_archive_manager_game") == manager_game.identity:
        _confirm_archive_manager_game(store, manager_game, game_groups)
    if st.button("Hent spilinfo", icon=":material/event:", key="fetch-manager-game-info"):
        try:
            with st.spinner("Henter officielt navn og runder …"):
                info = _fetch_and_remember_game_info(manager_game.game)
        except Exception as exc:
            _action_failure("Spilinfo kunne ikke hentes", exc)
    if info is not None:
        official = getattr(info, "display_name", None)
        final_round = getattr(info, "final_round", None)
        st.caption(
            f"Officielt navn: {official or '–'} · finalerunde: {final_round or '–'}"
        )
    with st.form("rename-manager-game"):
        renamed = st.text_input("Visningsnavn", value=manager_game.name)
        rename = st.form_submit_button("Gem navn")
    if rename:
        try:
            store.rename_manager_game(manager_game.game, renamed)
        except (PayloadError, ValueError) as exc:
            st.error(str(exc))
        else:
            st.rerun()
    if info is not None and getattr(info, "display_name", None):
        if st.button("Brug officielt navn"):
            store.rename_manager_game(manager_game.game, info.display_name)
            st.rerun()
    if game_groups:
        st.caption("Slet alle grupper i managerspillet, før managerspillet kan slettes.")
    confirm = st.checkbox(
        f"Jeg vil slette {manager_game.name}",
        key="confirm-delete-manager-game",
        disabled=bool(game_groups),
    )
    if st.button("Slet managerspil", disabled=bool(game_groups) or not confirm):
        try:
            store.delete_manager_game(manager_game.game)
        except PayloadError as exc:
            st.error(str(exc))
        else:
            _navigate("home")
    st.divider()
    if st.button(
        "Arkivér managerspil",
        icon=":material/archive:",
        width="stretch",
    ):
        st.session_state["pending_archive_manager_game"] = manager_game.identity
        _confirm_archive_manager_game(store, manager_game, game_groups)





def _game_view(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    store: GroupStore,
    *,
    read_only: bool = False,
) -> None:
    color, _ = _colors(manager_game.game.slug)
    st.markdown(
        f'<div style="height:6px;border-radius:6px;background:{color};margin-bottom:1rem"></div>',
        unsafe_allow_html=True,
    )
    st.title(manager_game.name, anchor=f"managerspil-{manager_game.game.slug}")
    st.caption(manager_game.game.slug)
    game_groups = tuple(
        group
        for group in groups
        if _game_identity(group.game) == manager_game.identity
    )
    active_alerts = _active_alert_counts().get(manager_game.identity, 0)
    alert_tab_label = f"Statusalarmer · {active_alerts}"
    group_tab_label = f"Grupper · {len(game_groups)}"
    tabs = _stateful_tabs(
        (
            "Rundecenter",
            group_tab_label,
            "Spillerstatistik",
            alert_tab_label,
            "Holdstatistik",
            "Historik",
            "Analyse",
            "Administration",
            "Indstillinger",
        ),
        (
            "round-center",
            "groups",
            "players",
            "alerts",
            "teams",
            "history",
            "analysis",
            "administration",
            "settings",
        ),
        key=f"game-tabs-{manager_game.game.locale}-{manager_game.game.slug}",
        parameter="section",
    )
    (
        round_center_tab,
        groups_tab,
        players_tab,
        alerts_tab,
        teams_tab,
        history_tab,
        analysis_tab,
        manage_tab,
        settings_tab,
    ) = tabs
    if round_center_tab.open:
        with round_center_tab:
            _game_round_center_tab(
                manager_game, groups, index, read_only=read_only
            )
    if groups_tab.open:
        with groups_tab:
            _game_groups_tab(
                manager_game, groups, index, read_only=read_only
            )
    if players_tab.open:
        with players_tab:
            _player_statistics_tab(manager_game, read_only=read_only)
    if alerts_tab.open:
        with alerts_tab:
            alerts_view(
                APP_PATHS,
                manager_game.game,
                read_only=read_only,
            )
    if teams_tab.open:
        with teams_tab:
            _team_statistics_game_tab(
                manager_game, groups, index, read_only=read_only
            )
    if history_tab.open:
        with history_tab:
            game_history_panel(manager_game, groups, index)
    if analysis_tab.open:
        with analysis_tab:
            analysis_panel(
                manager_game,
                groups,
                index,
                APP_PATHS,
                read_only=read_only,
            )
    if manage_tab.open:
        with manage_tab:
            _manage_game(
                manager_game, game_groups, index, store, read_only=read_only
            )
    if settings_tab.open:
        with settings_tab:
            _game_settings_tab(
                manager_game, game_groups, store, read_only=read_only
            )

def _standing_warning(row: StandingRow) -> str | None:
    notices = []
    if row.warning:
        notices.append(row.warning)
    if row.stale:
        notices.append(
            "Seneste opdatering mislykkedes; viser cachede data, hvor det er muligt"
        )
    return f"{row.team_name}: {' · '.join(notices)}" if notices else None


def _standings_table(
    group: GroupDefinition, index: SnapshotIndex, round_number: int, mode: str
):
    stale_ids = _manifest_statuses(group)
    standings = build_standings(
        group,
        index,
        round_number,
        mode,
        stale_team_ids=stale_ids,
    )
    rows = []
    for row in standings:
        rows.append(
            {
                "Rang": row.rank,
                "Manager": row.owner_name,
                "Hold": row.team_name,
                "Værdi": row.summary.total if row.summary else None,
                "Vækst": row.change,
                "Afstand": row.distance,
                "Hold-ID": row.team_id,
            }
        )
    st.caption("Klik p\u00e5 en r\u00e6kke for at \u00e5bne holdet")
    event = dataframe(
        _style_integer_columns(rows, ("Værdi", "Vækst", "Afstand")),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"standing-{group.group_id}-v1",
        column_config={"Hold-ID": None},
    )
    selected = event.selection.rows
    if selected:
        chosen = rows[selected[0]]
        _navigate(
            "team",
            group=group.group_id,
            team=chosen["Hold-ID"],
            round=round_number,
        )
    for row in standings:
        if warning := _standing_warning(row):
            st.warning(warning)
    return standings




def _round_story_panel(
    group: GroupDefinition,
    index: SnapshotIndex,
    round_number: int,
) -> None:
    try:
        settings = HubSettingsStore(APP_PATHS.hub_settings_file).load()
    except (OSError, PayloadError, ValueError) as exc:
        st.warning(f"Rundens historie kunne ikke l\u00e6ses: {exc}")
        return
    story = build_round_story(
        (group,),
        index,
        settings,
        group.game.slug,
        round_number,
        game_locale=group.game.locale,
    )
    st.subheader("Rundens historie")
    if story.preliminary:
        st.warning(story.headline)
    else:
        st.markdown(f"**{story.headline}**")
    for paragraph in story.paragraphs:
        st.write(paragraph)
    if story.awards:
        with st.container(horizontal=True):
            for award in story.awards:
                st.metric(award.title, award.detail, border=True)


def _standings_group_view(group: GroupDefinition, index: SnapshotIndex) -> None:
    color, _ = _colors(group.game.slug)
    st.markdown(
        f'<div style="height:6px;border-radius:6px;background:{color};margin-bottom:1rem"></div>',
        unsafe_allow_html=True,
    )
    st.title(group.name, anchor=f"gruppe-{group.group_id}")
    if group.official_url:
        st.link_button(
            "Åbn officiel gruppe",
            group.official_url,
            icon=":material/open_in_new:",
        )
    st.caption(f"{group.game.slug} · {len(group.teams)} faste hold")
    if not group.teams:
        st.info("Gruppen har ingen hold endnu.")
        return
    tabs = _stateful_tabs(
        ("Stilling", "Historik"),
        ("standings", "history"),
        key=f"group-tabs-{group.group_id}",
        parameter="section",
    )
    standings_tab, history_tab = tabs
    if standings_tab.open:
        with standings_tab:
            rounds = index.rounds_for(
                group.game,
                tuple(member.team_id for member in group.teams),
            )
            if not rounds:
                st.warning(
                    "Der er endnu ingen kompatible snapshots for gruppen."
                )
            else:
                controls = st.columns([1, 2, 3])
                with controls[0]:
                    round_number = _round_selectbox(
                        "Runde",
                        rounds,
                        key=f"round-{group.group_id}",
                        default=rounds[0],
                    )
                with controls[1]:
                    label = st.segmented_control(
                        "Visning",
                        ("Samlet", "Runde"),
                        default="Samlet",
                        key=f"mode-{group.group_id}",
                    )
                _standings_table(
                    group,
                    index,
                    int(round_number),
                    "overall" if label == "Samlet" else "round",
                )
                _round_story_panel(group, index, int(round_number))
    if history_tab.open:
        with history_tab:
            group_history_panel(group, index)

def _match_round(match: GroupMatch | KnockoutMatch) -> int:
    return (
        match.fixture.round_number
        if isinstance(match, GroupMatch)
        else match.round_numbers[0]
    )


def _round_data_state(
    index: SnapshotIndex,
    game: GameUrl,
    team_ids: Iterable[int],
    round_numbers: Iterable[int],
) -> str:
    statuses: list[str] = []
    for team_id in team_ids:
        for round_number in round_numbers:
            located = index.summary_for(game, team_id, round_number)
            if located is None:
                return "missing"
            statuses.append(located[1].round_status)
    if "in_progress" in statuses:
        return "in_progress"
    if "unknown" in statuses:
        return "unknown"
    return "complete"


def _round_data_label(status: str) -> str:
    return {
        "complete": data_status_label("complete"),
        "in_progress": data_status_label("in_progress"),
        "unknown": data_status_label("unknown"),
        "missing": data_status_label("missing"),
    }[status]


def _tournament_standings_table(
    group: GroupDefinition, state: TournamentState, round_number: int
) -> None:
    rows = [
        {
            "Plac.": row.rank,
            "Manager": row.owner_name,
            "Hold": row.team_name,
            "K": row.played,
            "V": row.wins,
            "U": row.draws,
            "T": row.losses,
            "For": row.growth_for,
            "Imod": row.growth_against,
            "Forskel": row.growth_difference,
            "Point": row.points,
            "Hold-ID": row.team_id,
        }
        for row in state.standings
    ]
    st.caption("Klik p\u00e5 en r\u00e6kke for at \u00e5bne holdet")
    event = dataframe(
        _style_integer_columns(rows, ("For", "Imod", "Forskel")),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"tournament-standing-{group.group_id}-v1",
        column_config={"Hold-ID": None},
    )
    if event.selection.rows:
        chosen = rows[event.selection.rows[0]]
        _navigate(
            "team",
            group=group.group_id,
            team=chosen["Hold-ID"],
            round=round_number,
        )


def _tournament_overview(
    group: GroupDefinition, state: TournamentState
) -> None:
    champion = next(
        (
            row.team_name
            for row in state.standings
            if row.team_id == state.champion_id
        ),
        None,
    )
    with st.container(horizontal=True):
        st.metric("Fase", state.phase, border=True)
        st.metric("Aktive hold", len(state.active_team_ids), border=True)
        st.metric("Eliminerede", len(state.eliminated_team_ids), border=True)
        st.metric("Mester", champion or "–", border=True)
    if state.next_matches:
        st.subheader("Næste opgør")
        for match in state.next_matches:
            if isinstance(match, GroupMatch):
                label = f"{match.team_a_name} – {match.team_b_name}"
            else:
                label = (
                    f"{match.team_a_name or 'Afventer'} – "
                    f"{match.team_b_name or 'Afventer'}"
                )
            st.markdown(f"**Runde {_match_round(match)}:** {label}")
    elif champion is not None:
        st.success(f"{champion} har vundet turneringen.")
    else:
        st.info("Der er ingen fastlagt næste kamp endnu.")
    for warning in state.warnings:
        st.warning(warning)


def _tournament_head_to_head(
    group: GroupDefinition, state: TournamentState, index: SnapshotIndex
) -> None:
    st.subheader("Indbyrdes sammenligning")
    if len(group.teams) < 2:
        st.info("H2H kræver mindst to hold.")
        return
    names = {row.team_id: row.team_name for row in state.standings}
    for member in group.teams:
        names.setdefault(member.team_id, member.name)
    team_ids = [member.team_id for member in group.teams]
    controls = st.columns(2)
    with controls[0]:
        team_a_id = int(
            st.selectbox(
                "Hold A",
                team_ids,
                format_func=lambda team_id: names[team_id],
                key=f"h2h-a-{group.group_id}",
            )
        )
    team_b_options = [team_id for team_id in team_ids if team_id != team_a_id]
    with controls[1]:
        team_b_id = int(
            st.selectbox(
                "Hold B",
                team_b_options,
                format_func=lambda team_id: names[team_id],
                key=f"h2h-b-{group.group_id}-{team_a_id}",
            )
        )
    summary = build_tournament_head_to_head(
        group, index, team_a_id, team_b_id, state.as_of_round
    )
    metrics = st.columns(4)
    metrics[0].metric("Færdige kampe", summary.played)
    metrics[1].metric(f"Sejre · {summary.team_a_name}", summary.team_a_wins)
    metrics[2].metric("Uafgjorte", summary.draws)
    metrics[3].metric(f"Sejre · {summary.team_b_name}", summary.team_b_wins)
    growth = st.columns(3)
    growth[0].metric(
        f"Samlet vækst · {summary.team_a_name}",
        _format_number(summary.team_a_growth),
    )
    growth[1].metric(
        f"Samlet vækst · {summary.team_b_name}",
        _format_number(summary.team_b_growth),
    )
    growth[2].metric("Forskel", _format_number(summary.growth_difference))
    if not summary.matches:
        st.info("Holdene har ingen indbyrdes kampe til og med den valgte runde.")
        return
    rows: list[dict[str, object]] = []
    for match in summary.matches:
        if not match.complete:
            status = _round_data_label(
                _round_data_state(
                    index,
                    group.game,
                    (match.team_a_id, match.team_b_id),
                    (match.round_number,),
                )
            )
        elif match.winner_id is None:
            status = "Uafgjort"
        else:
            status = f"{names[match.winner_id]} vandt"
        if match.advanced_by_seed_id is not None:
            status += f" · {names[match.advanced_by_seed_id]} videre på seed"
        rows.append(
            {
                "Runde": match.round_number,
                "Fase": match.phase,
                summary.team_a_name: match.team_a_change,
                summary.team_b_name: match.team_b_change,
                "Resultat": status,
            }
        )
    dataframe(
        _style_integer_columns(
            rows, (summary.team_a_name, summary.team_b_name)
        ),
        hide_index=True,
        width="stretch",
        key=f"tournament:{group.group_id}:head-to-head",
    )


def _tournament_matches(
    group: GroupDefinition, state: TournamentState, index: SnapshotIndex
) -> None:
    _tournament_head_to_head(group, state, index)
    st.divider()
    st.subheader(
        "Alle gruppespilskampe"
        if group.tournament is not None and group.tournament.template == "group_knockout"
        else "Alle kampe"
    )
    rows: list[dict[str, object]] = []
    for match in state.group_matches:
        if match.fixture.is_bye:
            status = "Sidder over"
        elif match.complete:
            status = "Uafgjort" if match.winner_id is None else "Færdig"
        elif match.fixture.round_number <= state.as_of_round:
            assert match.fixture.team_b_id is not None
            status = _round_data_label(
                _round_data_state(
                    index,
                    group.game,
                    (match.fixture.team_a_id, match.fixture.team_b_id),
                    (match.fixture.round_number,),
                )
            )
        else:
            status = "Planlagt"
        rows.append(
            {
                "Runde": match.fixture.round_number,
                "Hold A": match.team_a_name,
                "Vækst A": match.team_a_change,
                "Hold B": match.team_b_name or "–",
                "Vækst B": match.team_b_change,
                "Status": status,
            }
        )
    if group.tournament is not None and group.tournament.template == "double_elimination":
        for match in state.knockout_matches:
            participants = tuple(
                team_id
                for team_id in (match.team_a_id, match.team_b_id)
                if team_id is not None
            )
            if match.complete:
                status = "Færdig" if len(participants) == 2 else "Automatisk videre"
            elif len(participants) < 2:
                status = "Afventer tidligere kamp"
            elif match.round_numbers[-1] <= state.as_of_round:
                status = _round_data_label(
                    _round_data_state(
                        index, group.game, participants, match.round_numbers
                    )
                )
            else:
                status = "Planlagt"
            rows.append(
                {
                    "Fase": match.stage,
                    "Runde": "–".join(str(value) for value in match.round_numbers),
                    "Hold A": match.team_a_name or "Afventer",
                    "Vækst A": match.team_a_change,
                    "Hold B": match.team_b_name or "Afventer",
                    "Vækst B": match.team_b_change,
                    "Status": status,
                }
            )
    dataframe(
        _style_integer_columns(rows, ("Vækst A", "Vækst B")),
        hide_index=True,
        width="stretch",
        key=f"tournament:{group.group_id}:matches",
    )


def _tournament_bracket(group: GroupDefinition, state: TournamentState) -> None:
    assert group.tournament is not None
    render_tournament_bracket(group, state)
    return

    if not state.knockout_matches:
        st.info(
            "Knockout-seedningen vises, når alle gruppespilskampe har komplette data."
        )
        return
    stage_names: list[str] = []
    size = group.tournament.knockout_size
    while size >= 2:
        stage_names.append(STAGE_NAMES[size])
        size //= 2
    columns = st.columns(len(stage_names))
    for column, stage in zip(columns, stage_names):
        with column:
            st.subheader(stage)
            matches = [item for item in state.knockout_matches if item.stage == stage]
            for match in matches:
                with st.container(border=True):
                    rounds = (
                        str(match.round_numbers[0])
                        if len(match.round_numbers) == 1
                        else f"{match.round_numbers[0]}–{match.round_numbers[-1]}"
                    )
                    st.caption(f"Runde {rounds}")
                    a_name = match.team_a_name or "Afventer"
                    b_name = match.team_b_name or "Afventer"
                    a_score = _format_number(match.team_a_change)
                    b_score = _format_number(match.team_b_change)
                    a_marker = (
                        " ✓"
                        if match.winner_id is not None
                        and match.winner_id == match.team_a_id
                        else ""
                    )
                    b_marker = (
                        " ✓"
                        if match.winner_id is not None
                        and match.winner_id == match.team_b_id
                        else ""
                    )
                    st.markdown(f"**{a_name}**{a_marker}  \n{a_score}")
                    st.markdown(f"**{b_name}**{b_marker}  \n{b_score}")
                    if (
                        match.complete
                        and match.team_a_change == match.team_b_change
                        and match.winner_id is not None
                    ):
                        st.caption("Lighed: højeste seed går videre")


def _tournament_data_status(
    group: GroupDefinition, state: TournamentState, index: SnapshotIndex
) -> None:
    required: dict[int, set[int]] = {member.team_id: set() for member in group.teams}
    for match in state.group_matches:
        if match.fixture.is_bye or match.fixture.round_number > state.as_of_round:
            continue
        required[match.fixture.team_a_id].add(match.fixture.round_number)
        assert match.fixture.team_b_id is not None
        required[match.fixture.team_b_id].add(match.fixture.round_number)
    for match in state.knockout_matches:
        if match.team_a_id is None or match.team_b_id is None:
            continue
        for round_number in match.round_numbers:
            if round_number <= state.as_of_round:
                required[match.team_a_id].add(round_number)
                required[match.team_b_id].add(round_number)

    status_rows: list[dict[str, object]] = []
    has_missing = False
    has_in_progress = False
    has_unknown = False
    for member in group.teams:
        missing: list[int] = []
        in_progress: list[int] = []
        unknown: list[int] = []
        for round_number in sorted(required[member.team_id]):
            located = index.summary_for(
                group.game, member.team_id, round_number
            )
            if located is None:
                missing.append(round_number)
            elif located[1].round_status == "in_progress":
                in_progress.append(round_number)
            elif located[1].round_status == "unknown":
                unknown.append(round_number)
        has_missing = has_missing or bool(missing)
        has_in_progress = has_in_progress or bool(in_progress)
        has_unknown = has_unknown or bool(unknown)
        newest = index.newest(group.game, member.team_id)
        status_rows.append(
            {
                "Hold": newest.team.team_name if newest else member.name,
                "Datakilde": _snapshot_data_status(newest),
                "Runde i gang": ", ".join(map(str, in_progress)) or "\u2013",
                "Rundestatus ukendt": ", ".join(map(str, unknown)) or "\u2013",
                "Mangler data": ", ".join(map(str, missing)) or "\u2013",
            }
        )
    pending = has_missing or has_in_progress or has_unknown
    with st.expander("Datastatus", expanded=pending):
        if has_missing:
            st.warning("Mangler data til en eller flere planlagte kampe.")
        if has_in_progress:
            st.warning(
                "Runde i gang. Data vises, men turneringspoint gives ikke, "
                "f\u00f8r runden er afsluttet og hentet igen."
            )
        if has_unknown:
            st.warning(
                "Rundestatus ukendt. Data vises, men turneringspoint holdes "
                "tilbage, indtil en manuel genhentning bekr\u00e6fter runden."
            )
        if not pending:
            st.success("Alle n\u00f8dvendige runder er komplette.")
        st.table(pd.DataFrame(status_rows), border="horizontal")


def _tournament_view(
    group: GroupDefinition,
    all_groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    *,
    read_only: bool = False,
) -> None:
    assert group.tournament is not None
    color, _ = _colors(group.game.slug)
    latest_round = latest_tournament_round(group, index)
    st.markdown(
        f'<div style="height:6px;border-radius:6px;background:{color};margin-bottom:1rem"></div>',
        unsafe_allow_html=True,
    )
    st.title(group.name, anchor=f"gruppe-{group.group_id}")
    if group.official_url:
        st.link_button(
            "Åbn officiel gruppe",
            group.official_url,
            icon=":material/open_in_new:",
        )
    st.caption(f"{group.game.slug} · Turnering · {len(group.teams)} faste hold")

    round_key = f"tournament-round-{group.group_id}"
    if st.button(
        "Opdater turnering",
        type="primary",
        width="stretch",
        disabled=read_only,
        help="Gendan managerspillet for at opdatere turneringen." if read_only else None,
    ):
        with st.spinner(f"Henter alle {len(group.teams)} deltagere …"):
            _save_game_metadata_if_available(group.game)

            result = refresh_group(
                group,
                _client(),
                SnapshotStore(OUTPUT_DIR),
                ManifestStore(MANIFEST_DIR),
            )
        _invalidate_snapshot_index()
        refreshed_index = _scan_snapshots(str(OUTPUT_DIR.resolve()))
        pairing_notice: tuple[str, str] | None = None
        try:
            published_pairing = _publish_next_swiss_round(
                group,
                refreshed_index,
            )
        except (OSError, PayloadError, ValueError) as exc:
            pairing_notice = ("error", str(exc))
        else:
            if published_pairing is not None:
                pairing_notice = (
                    "success",
                    "N\u00e6ste Swiss-runde blev publiceret.",
                )
        resolved_groups, pairing_warnings = _with_published_tournament_pairings(
            (group,),
            refreshed_index,
        )
        resolved_group = resolved_groups[0]
        freeze_groups = tuple(
            resolved_group
            if item.group_id == group.group_id
            else item
            for item in all_groups
            if _game_identity(item.game) == _game_identity(group.game)
        )
        _freeze_complete_hall_of_fame(
            freeze_groups,
            include_round_wins=True,
        )
        if pairing_warnings and pairing_notice is None:
            pairing_notice = ("error", pairing_warnings[0])


        successes = sum(item.status == "success" for item in result.teams)
        fallbacks = sum(item.status == "cached_fallback" for item in result.teams)
        failures = tuple(item for item in result.teams if item.status == "failed")
        st.session_state[f"tournament-refresh-notice-{group.group_id}"] = {
            "successes": successes,
            "fallbacks": fallbacks,
            "failures": tuple((item.team_name, item.error) for item in failures),
            "manifest": result.manifest_path.name,
            "pairing": pairing_notice,
        }
        st.session_state[round_key] = max(
            group.tournament.start_round,
            min(group.tournament.final_round, result.round_number),
        )
        st.rerun()

    notice = st.session_state.pop(
        f"tournament-refresh-notice-{group.group_id}", None
    )
    if notice:
        st.success(
            f"{notice['successes']} hold opdateret. "
            f"{notice['fallbacks']} bruger cache. Manifest: {notice['manifest']}"
        )
        for team_name, error in notice["failures"]:
            st.error(f"{team_name}: {error or 'Opdateringen mislykkedes'}")
        if notice.get("pairing"):
            level, message = notice["pairing"]
            if level == "error":
                st.error(f"Swiss-parringen kunne ikke publiceres: {message}")
            else:
                st.success(message)

    rounds = list(range(group.tournament.start_round, group.tournament.final_round + 1))
    if round_key not in st.session_state or st.session_state[round_key] not in rounds:
        st.session_state[round_key] = latest_round
    round_number = int(
        st.selectbox(
            "Vis turneringen til og med runde",
            rounds,
            key=round_key,
        )
    )
    state = build_tournament_state(group, index, round_number)
    _tournament_data_status(group, state, index)
    has_bracket = group.tournament.template in {
        "group_knockout",
        "double_elimination",
    }
    standing_label = (
        "Gruppestilling"
        if group.tournament.template == "group_knockout"
        else "Stilling"
    )
    bracket_label = (
        "Knockout"
        if group.tournament.template == "group_knockout"
        else "Bracket"
    )
    tab_labels = ["Overblik", standing_label, "Kampe"]
    tab_routes = ["overview", "standings", "matches"]
    if has_bracket:
        tab_labels.append(bracket_label)
        tab_routes.append("knockout")
    tab_labels.append("Historik")
    tab_routes.append("history")
    tabs = _stateful_tabs(
        tuple(tab_labels),
        tuple(tab_routes),
        key=f"tournament-tabs-{group.group_id}",
        parameter="section",
    )
    overview_tab, standings_tab, matches_tab = tabs[:3]
    knockout_tab = tabs[3] if has_bracket else None
    history_tab = tabs[-1]
    if overview_tab.open:
        with overview_tab:
            _tournament_overview(group, state)
            _round_story_panel(group, index, round_number)
    if standings_tab.open:
        with standings_tab:
            _tournament_standings_table(group, state, round_number)
    if matches_tab.open:
        with matches_tab:
            _tournament_matches(group, state, index)
    if knockout_tab is not None and knockout_tab.open:
        with knockout_tab:
            _tournament_bracket(group, state)
    if history_tab.open:
        with history_tab:
            group_history_panel(group, index)

def _team_status(entry) -> str:
    labels = {
        "inactive": "Inaktiv",
        "disabled": "Deaktiveret",
        "injured": "Skadet",
        "suspended": "Karantæne",
    }
    return " · ".join(labels[value] for value in entry.statuses) or "Aktiv"


def _team_reference(member: GroupTeam, game: GameUrl) -> TeamReference:
    return TeamReference(
        game=game,
        team_id=member.team_id,
        team_name=member.name,
        source_url=member.source_url,
        account_key=member.account_key,
        account_label=member.account_label,
        account_user_id=member.account_user_id,
        profile_url=member.profile_url,
    )


def _team_error_key(reference: TeamReference) -> tuple[str, str, int]:
    return (reference.game.locale.casefold(), reference.game.slug, reference.team_id)


def _fetch_team_statistics(reference: TeamReference) -> None:
    key = _team_error_key(reference)
    try:
        with st.spinner(f"Henter {reference.team_name} …"):
            _save_game_metadata_if_available(reference.game)
            team = _client().fetch_team(reference)
            saved = SnapshotStore(OUTPUT_DIR).save_team_json(team)
            _invalidate_snapshot_index()
    except Exception as exc:
        st.session_state.setdefault("team_statistics_errors", {})[key] = {
            "details": str(exc),
            "network": isinstance(exc, FetchError),
        }
    else:
        st.session_state.setdefault("team_statistics_errors", {}).pop(key, None)
        st.session_state.setdefault("team_statistics_notices", {})[key] = (
            f"{team.team_name} blev hentet og gemt som {saved.name}."
        )
    st.rerun()


def _team_candidates(
    game: GameUrl,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
) -> dict[int, TeamReference]:
    candidates: dict[int, TeamReference] = {}
    for snapshot in index.snapshots:
        reference = snapshot.team.reference
        if _game_identity(reference.game) == _game_identity(game):
            candidates.setdefault(reference.team_id, reference)
    for group in groups:
        if _game_identity(group.game) != _game_identity(game):
            continue
        for member in group.teams:
            candidates.setdefault(member.team_id, _team_reference(member, game))
    discovered = st.session_state.get("discovered_teams", {})
    if isinstance(discovered, dict):
        for reference in discovered.get(_game_identity(game), ()):
            if isinstance(reference, TeamReference):
                candidates.setdefault(reference.team_id, reference)
    direct = st.session_state.get("direct_team_candidates", {})
    if isinstance(direct, dict):
        for reference in direct.values():
            if isinstance(reference, TeamReference) and _game_identity(reference.game) == _game_identity(game):
                candidates.setdefault(reference.team_id, reference)
    return candidates


def _team_history_rows(team) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for summary in team.history:
        row = {
            "Runde": summary.round_number,
            "Total": summary.total,
            "Vækst": summary.change,
            "Spillervækst": summary.player_change,
            "Kaptajnbonus": summary.captain_bonus,
            "Specialbonus": summary.special_bonus,
            "Udskiftninger": summary.substitutions_used,
            "Runderang": summary.round_rank,
            "Samlet placering": summary.overall_rank,
            "Runderangændring": summary.round_rank_change,
            "Ændring i samlet placering": summary.overall_rank_change,
        }
        if team.overview.unit == "money":
            row.update({
                "Spillerværdi": summary.player_value,
                "Bank": summary.bank,
                "Bankændring": summary.bank_change,
                "Rente": summary.interest,
                "Transfer": summary.transfer,
            })
        rows.append(row)
    return rows


def _team_export_section(
    snapshot: TeamSnapshot,
    index: SnapshotIndex,
    round_number: int,
) -> None:
    team = snapshot.team
    scope_label = st.radio(
        "Omfang",
        ("Komplet snapshot", "Valgt runde"),
        horizontal=True,
        key=f"team-export-scope-{team.reference.game.slug}-{team.reference.team_id}",
    )
    formats = st.pills(
        "Formater",
        TEAM_EXPORT_FORMATS,
        default=("txt", "json"),
        selection_mode="multi",
        format_func=lambda value: value.upper(),
        key=f"team-export-formats-{team.reference.game.slug}-{team.reference.team_id}",
    )
    scope = "full" if scope_label == "Komplet snapshot" else "round"
    located = index.summary_for(team.reference.game, team.reference.team_id, round_number)
    roster_snapshot = index.roster_for(team.reference.game, team.reference.team_id, round_number)
    ready_key = f"team-export-ready-{team.reference.game.slug}-{team.reference.team_id}"
    disabled = not formats or (scope == "round" and located is None)
    if st.button("Opret eksport", type="primary", disabled=disabled, key=f"create-{ready_key}"):
        try:
            source = snapshot if scope == "full" else located[0]
            document = build_team_export(
                source.team,
                scope=scope,
                round_number=round_number,
                roster=None if roster_snapshot is None else tuple(roster_snapshot.team.roster),
                source_generated_at=source.generated_at,
                roster_generated_at=None if roster_snapshot is None else roster_snapshot.generated_at,
            )
            artifacts = TeamExportStore(TEAM_EXPORT_DIR).save(document, tuple(formats))
        except (PayloadError, OSError, ValueError) as exc:
            st.error(f"Eksporten kunne ikke oprettes: {exc}")
        else:
            st.session_state[ready_key] = artifacts
    artifacts = st.session_state.get(ready_key, ())
    for artifact in artifacts:
        st.code(str(artifact.path), language=None)
        st.download_button(
            f"Download {artifact.format.upper()}",
            data=artifact.content,
            file_name=artifact.path.name,
            mime=artifact.mime_type,
            on_click="ignore",
            key=f"download-{ready_key}-{artifact.format}-{artifact.path.name}",
        )


def _render_team_snapshot(
    snapshot: TeamSnapshot,
    index: SnapshotIndex,
    *,
    default_round: int | None = None,
    caption: str | None = None,
    history_groups: tuple[GroupDefinition, ...] = (),
) -> None:
    team = snapshot.team
    rounds = index.rounds_for(team.reference.game, (team.reference.team_id,))
    if not rounds:
        rounds = (team.overview.current_round,)
    chosen = default_round if default_round in rounds else rounds[0]
    round_number = _round_selectbox(
        "Runde",
        rounds,
        key=f"team-round-{team.reference.game.slug}-{team.reference.team_id}",
        default=chosen,
    )
    located = index.summary_for(team.reference.game, team.reference.team_id, round_number)
    summary = located[1] if located else None
    roster_snapshot = index.roster_for(team.reference.game, team.reference.team_id, round_number)
    st.title(team.team_name, anchor=f"hold-{team.reference.team_id}")
    st.caption(caption or f"{team.owner_name} · {team.reference.game.slug} · runde {round_number}")
    status_text = _local_data_status(
        located[0].generated_at if located is not None else snapshot.generated_at,
        round_number,
        None if summary is None else summary.round_status,
    )
    if summary is not None and summary.round_status == "complete":
        st.caption(status_text)
    elif summary is not None and summary.round_status == "in_progress":
        st.warning(
            f"{status_text}. Data vises, men giver ikke turneringspoint, "
            "f\u00f8r runden er afsluttet og hentet igen."
        )
    elif summary is not None:
        st.warning(
            f"{status_text}. Hent holdet igen for at bekr\u00e6fte runden; "
            "turneringspoint holdes tilbage."
        )
    tabs = _stateful_tabs(
        (
            "Overblik",
            "Holdopstilling",
            "Transferlaboratorium",
            "Historik",
            "Ændringer",
            "Eksport",
        ),
        ("overview", "roster", "transfer", "history", "changes", "export"),
        key=f"team-tabs-{team.reference.game.slug}-{team.reference.team_id}",
        parameter="panel",
    )
    (
        overview_tab,
        roster_tab,
        transfer_tab,
        history_tab,
        changes_tab,
        export_tab,
    ) = tabs
    if overview_tab.open:
        with overview_tab:
            if summary is None:
                st.warning(f"Mangler rundesammendrag for runde {round_number}.")
            else:
                metrics = st.columns(4)
                metrics[0].metric("Total", _format_number(summary.total))
                metrics[1].metric("Rundevækst", _format_number(summary.change, signed=True))
                metrics[2].metric("Samlet placering", _format_number(summary.overall_rank))
                metrics[3].metric("Runderang", _format_number(summary.round_rank))
                details = st.columns(4)
                if team.overview.unit == "money":
                    details[0].metric("Spillerværdi", _format_number(summary.player_value))
                    details[1].metric("Bank", _format_number(summary.bank))
                else:
                    details[0].metric("Point", _format_number(summary.total))
                details[2].metric("Udskiftninger", _format_number(summary.substitutions_used))
                details[3].metric("Topplacering", "–" if team.overview.top_percent is None else f"{team.overview.top_percent}%")
                growth = [
                    ("Spillervækst", summary.player_change),
                    ("Bankændring", summary.bank_change),
                    ("Rente", summary.interest),
                    ("Transfer", summary.transfer),
                    ("Kaptajnbonus", summary.captain_bonus),
                    ("Specialbonus", summary.special_bonus),
                    ("Runderangændring", summary.round_rank_change),
                    ("Ændring i samlet placering", summary.overall_rank_change),
                ]
                dataframe(
                    _style_integer_columns(
                        [
                            {"Del": label, "Ændring": value}
                            for label, value in growth
                            if value is not None
                        ],
                        ("Ændring",),
                    ),
                    hide_index=True,
                    width="stretch",
                    key=(
                        f"team:{team.reference.game.locale}:"
                        f"{team.reference.game.slug}:"
                        f"{team.reference.team_id}:growth"
                    ),
                )
    if roster_tab.open:
        with roster_tab:
            if roster_snapshot is None:
                st.info("Der findes et rundesammendrag, men ingen holdopstilling blev gemt præcis i denne runde.")
            else:
                value_label = "Point" if team.overview.unit == "points" else "Værdi"
                rows = [{
                    "#": position,
                    "Navn": player.name,
                    "Hold/land": player.team,
                    "Position/kategori": player.position,
                    value_label: player.value,
                    "Rundevækst": player.round_change,
                    "Vækst siden køb": player.since_purchase_change,
                    "Købsrunde": player.purchase_round,
                    "Rolle": "Kaptajn" if player.role == "captain" else player.role,
                    "Status": _team_status(player),
                } for position, player in enumerate(roster_snapshot.team.roster, 1)]
                dataframe(
                    _style_integer_columns(
                        rows,
                        (value_label, "Rundevækst", "Vækst siden køb"),
                    ),
                    hide_index=True,
                    width="stretch",
                    key=(
                        f"team:{team.reference.game.locale}:"
                        f"{team.reference.game.slug}:"
                        f"{team.reference.team_id}:roster"
                    ),
                )
    if transfer_tab.open:
        with transfer_tab:
            if roster_snapshot is None:
                st.info(
                    "Der findes ingen præcis holdopstilling for den valgte runde."
                )
            else:
                player_snapshot = PlayerStatisticsStore(OUTPUT_DIR).scan(
                    team.reference.game
                ).newest(team.reference.game, round_number)
                transfer_lab_panel(
                    roster_snapshot,
                    player_snapshot,
                    team_round_status=(
                        "unknown" if summary is None else summary.round_status
                    ),
                )
    if history_tab.open:
        with history_tab:
            matching_groups = tuple(
                group
                for group in history_groups
                if any(
                    member.team_id == team.reference.team_id
                    for member in group.teams
                )
            )
            selected_group = (
                matching_groups[0] if len(matching_groups) == 1 else None
            )
            if len(matching_groups) > 1:
                selected_group = st.selectbox(
                    "Gruppe for grupperang",
                    (None, *matching_groups),
                    format_func=lambda item: (
                        "Ingen grupperang" if item is None else item.name
                    ),
                    key=(
                        f"team-history-group-{team.reference.game.slug}-"
                        f"{team.reference.team_id}"
                    ),
                )
            history_panel(
                team.reference.game,
                index,
                (team.reference.team_id,),
                group=selected_group,
                scope=(
                    f"team:{team.reference.game.locale}:"
                    f"{team.reference.game.slug}:{team.reference.team_id}"
                ),
            )
            rows = _team_history_rows(snapshot.team)
            if rows:
                with st.expander("Rundedetaljer"):
                    numeric = tuple(
                        key for key in rows[0] if key not in {"Runde"}
                    )
                    dataframe(
                        _style_integer_columns(rows, numeric),
                        hide_index=True,
                        width="stretch",
                        key=(
                            f"team:{team.reference.game.locale}:"
                            f"{team.reference.game.slug}:"
                            f"{team.reference.team_id}:round-details"
                        ),
                    )
    if changes_tab.open:
        with changes_tab:
            team_changes_panel(
                index,
                team.reference.game,
                team.reference.team_id,
                round_number,
            )
    if export_tab.open:
        with export_tab:
            _team_export_section(snapshot, index, round_number)


def _team_statistics_panel(
    game: GameUrl,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    *,
    read_only: bool = False,
    locked_reference: TeamReference | None = None,
    default_round: int | None = None,
    caption: str | None = None,
) -> None:
    candidates = _team_candidates(game, groups, index)
    if locked_reference is not None:
        candidates[locked_reference.team_id] = locked_reference
    if not read_only and locked_reference is None:
        if st.button("Find hold på konfigurerede konti", key=f"discover-teams-{game.locale}-{game.slug}"):
            references, warnings = _discover_account_teams(game)
            _remember_discovery(game, references, warnings)
            st.rerun()
        _render_discovery_status(game)
        direct = st.text_input(
            "Direkte fantasy-team URL eller ID",
            key=f"direct-team-{game.locale}-{game.slug}",
        )
        if st.button("Tilføj hold til visningen", key=f"use-direct-team-{game.locale}-{game.slug}", disabled=not direct.strip()):
            try:
                member = _parse_direct_lines(direct, game)[0]
            except (PayloadError, ValueError, IndexError) as exc:
                st.error(str(exc))
            else:
                reference = _team_reference(member, game)
                st.session_state.setdefault("direct_team_candidates", {})[_team_error_key(reference)] = reference
                st.rerun()
        candidates = _team_candidates(game, groups, index)
    if not candidates:
        st.info("Der er endnu ingen kendte hold til dette managerspil.")
        return
    ordered = sorted(candidates.values(), key=lambda item: (item.team_name.casefold(), item.team_id))
    if locked_reference is not None:
        selected = locked_reference
    else:
        choice_key = (
            f"team-statistics-choice-{game.locale}-{game.slug}"
        )
        requested_team = str(st.query_params.get("team", ""))
        requested_reference = next(
            (
                item
                for item in ordered
                if str(item.team_id) == requested_team
            ),
            None,
        )
        if requested_reference is not None:
            st.session_state[choice_key] = requested_reference

        def sync_team_choice() -> None:
            chosen_team = st.session_state.get(choice_key)
            if chosen_team is not None:
                st.query_params["team"] = str(chosen_team.team_id)

        selected = st.selectbox(
            "Hold",
            ordered,
            index=None,
            placeholder="Vælg et hold",
            format_func=lambda item: (
                f"{item.team_name} — {item.account_label} "
                f"(ID {item.team_id})"
            ),
            key=choice_key,
            on_change=sync_team_choice,
        )
    if selected is None:
        st.info("V\u00e6lg et hold for at se holdstatistik.")
        return
    newest = index.newest(game, selected.team_id)
    error_key = _team_error_key(selected)
    error = st.session_state.get("team_statistics_errors", {}).get(error_key)
    notice = st.session_state.get("team_statistics_notices", {}).pop(error_key, None)
    if notice:
        st.success(notice)
    if error:
        details = str(error.get("details", error)) if isinstance(error, dict) else str(error)
        _network_failure("Holdet kunne ikke kontaktes efter flere forsøg. Cachede data vises fortsat, hvor det er muligt.", details)
        if not read_only and st.button("Prøv igen", key=f"retry-team-{game.slug}-{selected.team_id}"):
            _fetch_team_statistics(selected)
    if not read_only:
        label = "Hent hold" if newest is None else "Opdater hold"
        if st.button(label, type="primary", key=f"fetch-team-{game.slug}-{selected.team_id}"):
            _fetch_team_statistics(selected)
    if newest is None:
        st.info(_local_data_status(None))
        return
    _render_team_snapshot(
        newest,
        index,
        default_round=default_round,
        caption=caption,
        history_groups=groups,
    )


def _team_statistics_game_tab(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    *,
    read_only: bool,
) -> None:
    game_groups = tuple(group for group in groups if _game_identity(group.game) == manager_game.identity)
    _team_statistics_panel(manager_game.game, game_groups, index, read_only=read_only)


def _standalone_team_statistics(
    games: tuple[ManagerGame, ...],
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
) -> None:
    st.title("Holdstatistik", anchor="holdstatistik")
    st.caption("Vælg et gemt spil, eller indtast en Holdet-URL eller slug. Spillet bliver ikke føjet til Mine managerspil.")
    known: dict[tuple[str, str], tuple[GameUrl, str]] = {
        game.identity: (game.game, game.name) for game in games
    }
    for snapshot in index.snapshots:
        game = snapshot.team.reference.game
        known.setdefault(_game_identity(game), (game, game.slug))
    sorted_known = sorted(known.values(), key=_game_label_sort_key)
    option_values = [item[0].original for item in sorted_known]
    option_labels = {item[0].original: item[1] for item in sorted_known}
    source = st.selectbox(
        "Spil eller Holdet-URL",
        option_values,
        index=None,
        accept_new_options=True,
        placeholder="V\u00e6lg et spil, eller indtast URL/slug",
        format_func=lambda value: option_labels.get(value, value),
        key="standalone-team-game",
    )
    if not source:
        st.info("Vælg eller indtast et managerspil.")
        return
    try:
        game = _normalize_game_source(str(source))
    except (PayloadError, ValueError) as exc:
        st.error(str(exc))
        return
    st.caption(f"Valgt spil: {game.slug} · sprog: {game.locale}")
    _team_statistics_panel(game, groups, index)


def _team_view(
    group: GroupDefinition,
    index: SnapshotIndex,
    team_id: int,
    *,
    read_only: bool = False,
) -> None:
    member = next((team for team in group.teams if team.team_id == team_id), None)
    if member is None:
        st.error("Holdet findes ikke i denne gruppe.")
        return
    if st.button("← Tilbage til gruppen"):
        _navigate("group", group=group.group_id)
    requested = st.query_params.get("round")
    default_round = int(requested) if requested and str(requested).isdigit() else None
    _team_statistics_panel(
        group.game,
        (group,),
        index,
        read_only=read_only,
        locked_reference=_team_reference(member, group.game),
        default_round=default_round,
        caption=f"{member.account_label} · {group.name}",
    )

def _candidate_snapshots(index: SnapshotIndex) -> dict[str, TeamSnapshot]:
    return {
        f"{locale}:{slug}:{team_id}": snapshot
        for (locale, slug, team_id), snapshot in _latest_by_identity(index).items()
    }


@dataclass(frozen=True, slots=True)
class _TeamCandidate:
    game: GameUrl
    member: GroupTeam


def _canonical_game_url(game: GameUrl) -> str:
    return f"https://www.holdet.dk/{game.locale}/fantasy/{game.slug}"


def _game_identity(game: GameUrl) -> tuple[str, str]:
    return game.locale.casefold(), game.slug


def _candidate_key(game: GameUrl, team_id: int) -> str:
    return f"{game.locale.casefold()}:{game.slug}:{team_id}"


def _normalize_game_source(source: str) -> GameUrl:
    value = source.strip()
    if not value:
        raise PayloadError("Vælg eller indtast et spil.")
    if "://" not in value:
        value = f"https://www.holdet.dk/da/fantasy/{value}"
    return normalize_game_url(value)


def _group_team_from_reference(reference: TeamReference) -> GroupTeam:
    return GroupTeam(
        team_id=reference.team_id,
        name=reference.team_name,
        source_url=reference.source_url,
        account_key=reference.account_key,
        account_label=reference.account_label,
        account_user_id=reference.account_user_id,
        profile_url=reference.profile_url,
    )


def _snapshot_candidates(index: SnapshotIndex) -> dict[str, _TeamCandidate]:
    result: dict[str, _TeamCandidate] = {}
    for snapshot in _candidate_snapshots(index).values():
        game = snapshot.team.reference.game
        result[_candidate_key(game, snapshot.team.reference.team_id)] = _TeamCandidate(
            game,
            group_team_from_snapshot(snapshot),
        )
    return result


def _discovered_candidates() -> dict[str, _TeamCandidate]:
    result: dict[str, _TeamCandidate] = {}
    discovered = st.session_state.get("discovered_teams", {})
    if not isinstance(discovered, dict):
        return result
    for references in discovered.values():
        for reference in references:
            if not isinstance(reference, TeamReference):
                continue
            result[_candidate_key(reference.game, reference.team_id)] = _TeamCandidate(
                reference.game,
                _group_team_from_reference(reference),
            )
    return result


def _discover_account_teams(
    game: GameUrl,
) -> tuple[tuple[TeamReference, ...], tuple[str, ...]]:
    accounts = load_accounts(ACCOUNTS_PATH)
    found: dict[tuple[str, int], TeamReference] = {}
    warnings: list[str] = []
    for account in accounts:
        try:
            references = _client().discover_account_teams(account, game=game)
        except Exception as exc:
            warnings.append(f"{account.label}: {exc}")
            continue
        for reference in references:
            found.setdefault((reference.game.locale.casefold(), reference.game.slug, reference.team_id), reference)
    return tuple(found.values()), tuple(warnings)


def _remember_game_info(game: GameUrl, info: object) -> None:
    _save_game_metadata_if_available(game, info)

    final_round = getattr(info, "final_round", None)
    if not isinstance(final_round, int) or isinstance(final_round, bool) or final_round < 1:
        raise PayloadError("Spillets schedule indeholder ingen gyldig finalerunde.")
    st.session_state.setdefault("game_info", {})[_game_identity(game)] = info


def _fetch_and_remember_game_info(game: GameUrl) -> object:
    info = _client().fetch_game_info(game)
    _remember_game_info(game, info)
    return info


def _known_game_info(game: GameUrl | None) -> object | None:
    if game is None:
        return None
    values = st.session_state.get("game_info", {})
    return values.get(_game_identity(game)) if isinstance(values, dict) else None


def _remember_discovery(
    game: GameUrl,
    references: tuple[TeamReference, ...],
    warnings: tuple[str, ...],
) -> None:
    identity = _game_identity(game)
    discovered = st.session_state.setdefault("discovered_teams", {})
    discovery_warnings = st.session_state.setdefault("discovery_warnings", {})
    discovered[identity] = references
    discovery_warnings[identity] = warnings


def _render_discovery_status(game: GameUrl) -> None:
    identity = _game_identity(game)
    discovered = st.session_state.get("discovered_teams", {})
    warnings = st.session_state.get("discovery_warnings", {})
    if identity in discovered:
        count = len(discovered[identity])
        if count:
            st.success(f"{count} hold fundet på de konfigurerede konti.")
        else:
            st.info("Ingen hold til dette spil blev fundet på de konfigurerede konti.")
    for warning in warnings.get(identity, ()):
        st.warning(warning)


def _candidates_for_game(
    candidates: dict[str, _TeamCandidate],
    game: GameUrl,
) -> dict[str, _TeamCandidate]:
    identity = _game_identity(game)
    return {
        key: candidate
        for key, candidate in candidates.items()
        if _game_identity(candidate.game) == identity
    }


def _candidate_labels(candidates: dict[str, _TeamCandidate]) -> dict[str, str]:
    return {
        key: (
            f"{candidate.member.name} — {candidate.member.account_label} "
            f"(ID {candidate.member.team_id})"
        )
        for key, candidate in candidates.items()
    }


def _parse_direct_lines(text: str, game: GameUrl) -> tuple[GroupTeam, ...]:
    result: list[GroupTeam] = []
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if line.isdecimal():
            line = (
                f"https://www.holdet.dk/{game.locale}/fantasy/{game.slug}"
                f"/fantasyteams/{line}"
            )
        reference = parse_direct_team_url(line)
        if reference is None:
            raise PayloadError(f"Ugyldig fantasy-team URL: {line}")
        if _game_identity(reference.game) != _game_identity(game):
            raise PayloadError("Alle hold i en gruppe skal tilhøre det samme spil.")
        result.append(_group_team_from_reference(reference))
    return tuple(result)


@st.dialog("Genberegn turnering")
def _confirm_tournament_rebuild(
    store: GroupStore,
    group: GroupDefinition,
    name: str,
    members: tuple[GroupTeam, ...],
    final_round: int,
    template: str,
    definition_options: dict[str, object],
    start_round: int,
    rounds_per_tie: int,
) -> None:
    assert group.tournament is not None
    old_ids = {team.team_id for team in group.teams}
    new_ids = {team.team_id for team in members}
    st.warning(
        "Hele turneringen genberegnes fra startrunden. Den nuværende "
        "lodtrækning arkiveres, og alle kampe, seedninger og resultater "
        "beregnes igen."
    )
    st.write(f"**Revision:** {group.active_revision} → {group.active_revision + 1}")
    st.write(f"**Hold:** {len(old_ids)} → {len(new_ids)}")
    st.write(
        f"**Finalerunde:** {group.tournament.final_round} → {final_round}"
    )
    template_names = {
        "league": "Liga",
        "swiss": "Schweizersystem",
        "group_knockout": "Gruppespil + knockout",
        "double_elimination": "Double elimination",
    }
    st.write(
        f"**Format:** {template_names[group.tournament.template]} → "
        f"{template_names[template]}"
    )
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    if added:
        st.caption("Tilføjede hold-ID'er: " + ", ".join(map(str, added)))
    if removed:
        st.caption("Fjernede hold-ID'er: " + ", ".join(map(str, removed)))
    actions = st.container(horizontal=True)
    with actions:
        confirm = st.button("Genberegn turnering", type="primary")
        cancel = st.button("Annuller")
    if cancel:
        st.session_state.pop("pending_tournament_rebuild", None)
        st.rerun()
    if confirm:
        try:
            rebuilt = store.rebuild_tournament(
                group.group_id,
                members,
                final_round=final_round,
                name=name,
                template=template,
                definition_options=definition_options,
                start_round=start_round,
                rounds_per_tie=rounds_per_tie,
            )
        except (PayloadError, ValueError) as exc:
            st.error(str(exc))
        else:
            st.session_state.pop("pending_tournament_rebuild", None)
            st.session_state["manage_notice"] = (
                f"Turneringen blev gemt som revision {rebuilt.active_revision}."
            )
            st.rerun()


def _manage_game(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    store: GroupStore,
    *,
    read_only: bool = False,
) -> None:
    game = manager_game.game
    st.caption(
        "Gruppestillinger kan redigeres direkte. Turneringsændringer opretter "
        "en ny revision og kræver særskilt bekræftelse. Data hentes kun via "
        "de viste knapper."
    )
    if read_only:
        st.info(
            "Gruppeadministrationen er skrivebeskyttet, mens managerspillet er arkiveret."
        )
        if not groups:
            st.write("Managerspillet har ingen grupper.")
            return
        for group in groups:
            kind = "Turnering" if group.kind == "tournament" else "Gruppestilling"
            with st.container(border=True):
                st.subheader(group.name)
                st.caption(f"{kind} · {len(group.teams)} hold")
                for team in group.teams:
                    st.write(f"{team.name} · Hold-ID {team.team_id}")
        return
    if notice := st.session_state.pop("manage_notice", None):
        st.success(notice)
    st.session_state.setdefault("discovered_teams", {})
    st.session_state.setdefault("discovery_warnings", {})
    st.session_state.setdefault("game_info", {})
    if pending := st.session_state.get("pending_tournament_rebuild"):
        _confirm_tournament_rebuild(store, *pending)
    snapshot_candidates = _snapshot_candidates(index)

    with st.expander("Opret ny gruppe", expanded=not groups):
        st.markdown("#### 1. Vælg type og spil")
        group_type = st.segmented_control(
            "Gruppetype", ("Gruppestilling", "Turnering"), default="Gruppestilling",
            key="create-group-type",
        )
        st.caption(f"Managerspil: {manager_game.name} · {game.slug}")

        with st.container(horizontal=True):
            fetch_info = st.button(
                "Hent spilinfo", icon=":material/event:",
                key="fetch-create-game-info",
            )
            discover = st.button(
                "Find hold på konfigurerede konti", icon=":material/search:",
                key="discover-create-game",
            )
        if fetch_info:
            try:
                with st.spinner("Henter spillets runder …"):
                    _fetch_and_remember_game_info(game)
            except Exception as exc:
                _action_failure("Spilinfo kunne ikke hentes", exc)
        if discover:
            try:
                with st.spinner("Henter spilinfo og søger efter hold …"):
                    try:
                        _fetch_and_remember_game_info(game)
                    except Exception as exc:
                        _action_failure("Spilinfo kunne ikke hentes", exc, warning=True)
                    references, warnings = _discover_account_teams(game)
            except Exception as exc:
                st.error(str(exc))
            else:
                _remember_discovery(game, references, warnings)

        _render_discovery_status(game)
        info = _known_game_info(game)
        final_round_value = getattr(info, "final_round", None)

        all_candidates = {**_discovered_candidates(), **snapshot_candidates}
        same_game = _candidates_for_game(all_candidates, game)
        labels = _candidate_labels(same_game)
        st.markdown("#### 2. Udfyld navn, deltagere og regler")
        name = st.text_input("Gruppenavn", key="create-group-name")
        selected = st.multiselect(
            "Fundne hold", list(labels), format_func=lambda key: labels[key],
            key=f"create-members-{game.locale.casefold()}-{game.slug}",
        )
        direct = st.text_area(
            "Direkte fantasy-team URLs eller ID'er (én pr. linje)",
            key="create-direct-teams",
        )

        start_round = 1
        rounds_per_tie = 1
        draw_seed: str | None = None
        template = "group_knockout"
        seed_rule = "random"
        definition_options: dict[str, object] = {}
        draw_seed_key = (
            f"create-tournament-seed-{game.locale.casefold()}-{game.slug}"
        )
        if group_type == "Turnering":
            template_labels = {
                "Liga": "league",
                "Schweizersystem": "swiss",
                "Gruppespil + knockout": "group_knockout",
                "Double elimination": "double_elimination",
            }
            template_label = st.segmented_control(
                "Turneringsformat",
                tuple(template_labels),
                default="Gruppespil + knockout",
                key="create-tournament-template",
            )
            template = template_labels[template_label or "Gruppespil + knockout"]
            st.caption(
                {
                    "league": "Alle m\u00f8der alle en eller to gange.",
                    "swiss": "Scoregrupper med rematch-undgåelse og fair bye.",
                    "group_knockout": "Gruppespil efterfulgt af krydsseedet bracket.",
                    "double_elimination": "Andet nederlag eliminerer; reset-finale er reserveret.",
                }[template]
            )
            settings = st.columns(3)
            with settings[0]:
                start_round = int(st.number_input(
                    "Startrunde", min_value=1, value=1, step=1,
                ))
            with settings[1]:
                st.metric(
                    "Finalerunde",
                    str(final_round_value) if final_round_value else "Hent spilinfo",
                )
            with settings[2]:
                rounds_per_tie = int(st.selectbox(
                    "Runder pr. knockoutopgør", (1, 2),
                ))
            rule_labels = {
                "Tilfældig": "random",
                "Manuel rækkefølge": "manual",
                "Aktuel Elo": "elo",
            }
            rule_label = st.selectbox(
                "Seedning",
                tuple(rule_labels),
                key="create-tournament-seed-rule",
            )
            seed_rule = rule_labels[rule_label]
            point_columns = st.columns(3)
            with point_columns[0]:
                win_points = int(st.number_input(
                    "Point for sejr", min_value=1, value=3, step=1,
                ))
            with point_columns[1]:
                draw_points = int(st.number_input(
                    "Point for uafgjort", min_value=0, value=1, step=1,
                ))
            with point_columns[2]:
                loss_points = int(st.number_input(
                    "Point for nederlag", min_value=0, value=0, step=1,
                ))
            standing_options = {
                "Scoreforskel": "score_difference",
                "Opnået score": "score_for",
                "Indbyrdes": "head_to_head",
                "Buchholz": "buchholz",
                "Seed": "entry_seed",
            }
            selected_standing_rules = st.multiselect(
                "Tie-breakers i stillingen (i rækkefølge)",
                tuple(standing_options),
                default=("Scoreforskel", "Opnået score", "Indbyrdes", "Seed"),
            )
            knockout_options = {
                "Sidste rundes vækst": "last_round_growth",
                "Samlet Holdet-total": "overall_total",
            }
            selected_knockout_rules = st.multiselect(
                "Tie-breakers i knockout",
                tuple(knockout_options),
                default=tuple(knockout_options),
                help="Højere seed anvendes altid som sidste sportslige reservekriterium.",
            )
            definition_options = {
                "match_points": (win_points, draw_points, loss_points),
                "seed_rule": seed_rule,
                "standings_tiebreakers": tuple(
                    standing_options[item] for item in selected_standing_rules
                ),
                "knockout_tiebreakers": (
                    *(knockout_options[item] for item in selected_knockout_rules),
                    "higher_seed",
                ),
            }
            if template == "league":
                definition_options["league_legs"] = int(st.selectbox(
                    "Indbyrdes kampe", (1, 2),
                ))
            elif template == "swiss":
                definition_options["swiss_rounds"] = int(st.number_input(
                    "Schweizerrunder", min_value=1, value=3, step=1,
                ))
            elif template == "group_knockout":
                group_count = int(st.number_input(
                    "Antal grupper", min_value=1, max_value=8, value=1, step=1,
                ))
                definition_options["group_count"] = group_count
                if group_count > 1:
                    definition_options["qualifiers_per_group"] = int(
                        st.number_input(
                            "Direkte kvalificerede pr. gruppe",
                            min_value=0,
                            value=1,
                            step=1,
                        )
                    )
                definition_options["bronze_match"] = st.checkbox(
                    "Spil bronzekamp",
                    value=False,
                )
            draw_seed = st.session_state.setdefault(
                draw_seed_key, generate_draw_seed()
            )
            st.caption("Lodtrækningsseed")
            seed_row = st.container(horizontal=True, vertical_alignment="bottom")
            with seed_row:
                st.code(draw_seed, language=None)
                if st.button(
                    "Ny lodtrækning",
                    icon=":material/casino:",
                    key="new-create-tournament-draw",
                ):
                    st.session_state[draw_seed_key] = generate_draw_seed()
                    st.rerun()
            st.markdown("#### 3. Vis valideret forhåndsvisning")
            preview_members = [same_game[key].member for key in selected]
            preview_error: Exception | None = None
            if direct.strip():
                try:
                    preview_members.extend(_parse_direct_lines(direct, game))
                except (PayloadError, ValueError) as exc:
                    preview_error = exc
            unique_count = len({member.team_id for member in preview_members})
            if preview_error is None and seed_rule in {"manual", "elo"}:
                seed_order = tuple(member.team_id for member in preview_members)
                if seed_rule == "elo":
                    try:
                        manager_settings = HubSettingsStore(
                            APP_PATHS.hub_settings_file
                        ).load()
                        ratings_by_manager = {
                            item.manager_id: item.rating
                            for item in build_manager_ratings(
                                groups, index, manager_settings
                            )
                        }

                        def elo_for(member: GroupTeam) -> tuple[float, int]:
                            snapshot = index.newest(game, member.team_id)
                            if snapshot is None:
                                return (-1500.0, member.team_id)
                            manager_id, _ = resolve_manager_identity(
                                manager_settings,
                                owner_user_id=snapshot.team.owner_user_id,
                                account_user_id=member.account_user_id,
                                account_key=member.account_key,
                                owner_name=snapshot.team.owner_name,
                                fallback_key=(
                                    f"{game.locale}:{game.slug}:team:"
                                    f"{member.team_id}"
                                ),
                            )
                            return (
                                -ratings_by_manager.get(manager_id, 1500.0),
                                member.team_id,
                            )

                        seed_order = tuple(
                            member.team_id
                            for member in sorted(preview_members, key=elo_for)
                        )
                    except (OSError, PayloadError, ValueError) as exc:
                        preview_error = exc
                definition_options["seed_order"] = seed_order
            if preview_error is not None:
                st.warning(str(preview_error))
            elif not isinstance(final_round_value, int):
                st.caption("Hent spilinfo for at finde spillets finalerunde.")
            elif unique_count >= 2:
                try:
                    preview_config = store.plan_tournament(
                        game, tuple(preview_members), start_round=start_round,
                        final_round=final_round_value,
                        rounds_per_tie=rounds_per_tie, draw_seed=draw_seed,
                        template=template,
                        definition_options=definition_options,
                    )
                except (PayloadError, ValueError) as exc:
                    st.warning(str(exc))
                else:
                    draw_seed = preview_config.draw_seed
                    st.session_state[draw_seed_key] = draw_seed
                    if template == "league":
                        series_label = (
                            "kampserie"
                            if preview_config.league_legs == 1
                            else "kampserier"
                        )
                        preview_message = (
                            f"{unique_count} hold · {preview_config.league_legs} "
                            f"indbyrdes {series_label} · runde {start_round}–"
                            f"{preview_config.final_round}"
                        )
                    elif template == "swiss":
                        preview_message = (
                            f"{unique_count} hold · {preview_config.swiss_rounds} "
                            f"schweizerrunder · første parring er klar i runde {start_round}"
                        )
                    elif template == "double_elimination":
                        match_count = 4 * len(preview_config.group_fixtures) - 1
                        preview_message = (
                            f"{unique_count} hold · {match_count} bracketpladser inkl. "
                            f"betinget reset-finale · runde {start_round}–"
                            f"{preview_config.final_round}"
                        )
                    else:
                        stage_count = preview_config.knockout_stage_count
                        preview_message = (
                            f"{unique_count} hold · top {preview_config.knockout_size} "
                            f"går videre · gruppespil runde {start_round}–"
                            f"{preview_config.group_end_round} · "
                            f"{count_label(stage_count, 'knockoutfase', 'knockoutfaser')}"
                        )
                    st.info(preview_message)
                    if unique_count == 2:
                        st.warning(
                            "Med to hold findes der kun én mulig modstanderplan. "
                            "Et nyt seed kan derfor ikke ændre kampene."
                        )
                    names = {member.team_id: member.name for member in preview_members}
                    with st.expander("Forhåndsvis kampplan"):
                        dataframe(
                            [
                                {
                                    "Runde": fixture.round_number,
                                    "Hold A": names[fixture.team_a_id],
                                    "Hold B": (
                                        names[fixture.team_b_id]
                                        if fixture.team_b_id is not None else "Pause"
                                    ),
                                }
                                for fixture in preview_config.group_fixtures
                            ],
                            hide_index=True,
                            width="stretch",
                            key="create-tournament:schedule-preview",
                        )
            else:
                st.caption("En turnering kræver mindst to hold.")
        else:
            st.markdown("#### 3. Vis valideret forhåndsvisning")
            st.caption(
                f"{name.strip() or 'Navn mangler'} · "
                f"{len(selected)} valgte hold · eventuelle direkte links "
                "valideres ved det afsluttende submit."
            )

        with st.form("create-group-review", border=True):
            st.markdown("#### 4. Gennemse og opret")
            st.caption(
                f"{group_type or 'Gruppestilling'} · "
                f"{name.strip() or 'Navn mangler'} · "
                f"{len(selected)} valgte hold"
            )
            create_group = st.form_submit_button(
                "Opret gruppe",
                type="primary",
                icon=":material/add_circle:",
            )
        if create_group:
            try:
                if game is None:
                    raise game_error or PayloadError("Vælg eller indtast et spil.")
                members = tuple(same_game[key].member for key in selected)
                members += _parse_direct_lines(direct, game)
                if not members:
                    raise PayloadError("Vælg mindst ét hold.")
                if group_type == "Turnering":
                    if not isinstance(final_round_value, int):
                        raise PayloadError("Hent spilinfo før turneringen oprettes.")
                    store.create_tournament(
                        name, game, members, start_round=start_round,
                        final_round=final_round_value,
                        rounds_per_tie=rounds_per_tie,
                        draw_seed=draw_seed,
                        template=template,
                        definition_options=definition_options,
                    )
                else:
                    store.create(name, game, members)
            except (PayloadError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.session_state.pop(draw_seed_key, None)
                st.success("Gruppen blev oprettet.")
                st.rerun()

    for group in groups:
        type_label = "Turnering" if group.kind == "tournament" else "Gruppestilling"
        with st.expander(f"{group.name} · {type_label} · {group.game.slug}"):
            with st.form(f"official-link-{group.group_id}"):
                official_url = st.text_input(
                    "Officiel Holdet-gruppe eller miniliga",
                    value=group.official_url or "",
                    placeholder=f"https://www.holdet.dk/{group.game.locale}/...",
                )
                link_types = ("group", "minileague")
                current_type = group.official_link_type or "group"
                official_link_type = st.selectbox(
                    "Linktype",
                    link_types,
                    index=link_types.index(current_type),
                )
                save_official = st.form_submit_button("Gem officielt link")
            if save_official:
                try:
                    store.update(
                        replace(
                            group,
                            official_url=official_url.strip() or None,
                            official_link_type=(
                                official_link_type if official_url.strip() else None
                            ),
                        )
                    )
                except PayloadError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
            if st.button(
                "Find flere hold på konfigurerede konti", icon=":material/search:",
                key=f"discover-edit-{group.group_id}",
            ):
                try:
                    with st.spinner("Henter spilinfo og søger efter hold …"):
                        try:
                            _fetch_and_remember_game_info(group.game)
                        except Exception as exc:
                            _action_failure("Spilinfo kunne ikke hentes", exc, warning=True)
                        references, warnings = _discover_account_teams(group.game)
                except Exception as exc:
                    st.error(str(exc))
                else:
                    _remember_discovery(group.game, references, warnings)
            if group.kind == "tournament" and st.button(
                "Hent aktuel spilinfo", icon=":material/event:",
                key=f"fetch-edit-info-{group.group_id}",
            ):
                try:
                    with st.spinner("Henter spillets runder …"):
                        _fetch_and_remember_game_info(group.game)
                except Exception as exc:
                    _action_failure("Spilinfo kunne ikke hentes", exc)

            _render_discovery_status(group.game)
            all_candidates = {**_discovered_candidates(), **snapshot_candidates}
            same_game = _candidates_for_game(all_candidates, group.game)
            for member in group.teams:
                same_game.setdefault(
                    _candidate_key(group.game, member.team_id),
                    _TeamCandidate(group.game, member),
                )
            defaults = [_candidate_key(group.game, member.team_id) for member in group.teams]
            labels = _candidate_labels(same_game)

            if group.kind == "tournament":
                assert group.tournament is not None
                active_format = {
                    "league": "Liga",
                    "swiss": "Schweizersystem",
                    "group_knockout": "Gruppespil + knockout",
                    "double_elimination": "Double elimination",
                }[group.tournament.template]
                tie_detail = (
                    f" · {count_label(group.tournament.rounds_per_tie, 'runde', 'runder')} pr. opgør"
                    if group.tournament.template in {"group_knockout", "double_elimination"}
                    else ""
                )
                st.caption(
                    f"Aktiv revision {group.active_revision} · {active_format} · runde "
                    f"{group.tournament.start_round}–{group.tournament.final_round}{tie_detail}"
                )
                st.caption(
                    "Lodtrækningsseed: "
                    + (group.tournament.draw_seed or "Ikke gemt (ældre turnering)")
                )
                info = _known_game_info(group.game)
                fetched_final = getattr(info, "final_round", None)
                if (
                    group.tournament.template == "group_knockout"
                    and isinstance(fetched_final, int)
                    and fetched_final != group.tournament.final_round
                ):
                    st.warning(
                        f"Holdet angiver nu finalerunde {fetched_final}; den aktive "
                        f"revision bruger {group.tournament.final_round}. Gem ændringer "
                        "for at vælge, om turneringen skal genberegnes."
                    )
                format_options = {
                    "Liga": "league",
                    "Schweizersystem": "swiss",
                    "Gruppespil + knockout": "group_knockout",
                    "Double elimination": "double_elimination",
                }
                current_format = next(
                    label
                    for label, value in format_options.items()
                    if value == group.tournament.template
                )
                standing_labels = {
                    "score_difference": "Scoreforskel",
                    "score_for": "Opnået score",
                    "head_to_head": "Indbyrdes",
                    "buchholz": "Buchholz",
                    "entry_seed": "Seed",
                }
                knockout_labels = {
                    "last_round_growth": "Sidste rundes vækst",
                    "overall_total": "Samlet Holdet-total",
                }
                with st.form(f"edit-{group.group_id}"):
                    renamed = st.text_input("Navn", value=group.name)
                    selected = st.multiselect(
                        "Hold", list(labels), default=defaults,
                        format_func=lambda key: labels[key],
                    )
                    direct = st.text_area("Tilføj direkte fantasy-team URLs eller ID'er")
                    edited_format_label = st.selectbox(
                        "Format",
                        tuple(format_options),
                        index=tuple(format_options).index(current_format),
                    )
                    edited_template = format_options[edited_format_label]
                    structure = st.columns(2)
                    with structure[0]:
                        edited_start_round = int(st.number_input(
                            "Startrunde",
                            min_value=1,
                            value=group.tournament.start_round,
                            step=1,
                        ))
                    with structure[1]:
                        edited_rounds_per_tie = int(st.selectbox(
                            "Runder pr. knockoutopgør",
                            (1, 2),
                            index=(1, 2).index(group.tournament.rounds_per_tie),
                        ))
                    point_columns = st.columns(3)
                    edited_points = tuple(
                        int(column.number_input(
                            label, min_value=minimum, value=value, step=1
                        ))
                        for column, label, minimum, value in zip(
                            point_columns,
                            ("Point for sejr", "Point for uafgjort", "Point for nederlag"),
                            (1, 0, 0),
                            group.tournament.match_points,
                        )
                    )
                    edited_seed_rule = st.selectbox(
                        "Seedregel",
                        ("random", "manual", "elo"),
                        index=("random", "manual", "elo").index(group.tournament.seed_rule),
                    )
                    edited_standing_rules = tuple(st.multiselect(
                        "Tie-breakers i stillingen",
                        tuple(standing_labels),
                        default=group.tournament.standings_tiebreakers,
                        format_func=lambda value: standing_labels[value],
                    ))
                    edited_knockout_rules = tuple(st.multiselect(
                        "Tie-breakers i knockout",
                        tuple(knockout_labels),
                        default=tuple(
                            value
                            for value in group.tournament.knockout_tiebreakers
                            if value != "higher_seed"
                        ),
                        format_func=lambda value: knockout_labels[value],
                        help="Højere seed er altid det sidste sportslige reservekriterium.",
                    ))
                    league_legs = group.tournament.league_legs
                    swiss_rounds = group.tournament.swiss_rounds
                    group_count = group.tournament.group_count
                    qualifiers_per_group = group.tournament.qualifiers_per_group
                    bronze_match = group.tournament.bronze_match
                    if edited_template == "league":
                        league_legs = int(st.selectbox(
                            "Indbyrdes kampe",
                            (1, 2),
                            index=(1, 2).index(group.tournament.league_legs),
                        ))
                    elif edited_template == "swiss":
                        swiss_rounds = int(st.number_input(
                            "Schweizerrunder",
                            min_value=1,
                            value=group.tournament.swiss_rounds or 3,
                            step=1,
                        ))
                    elif edited_template == "group_knockout":
                        group_count = int(st.number_input(
                            "Antal grupper",
                            min_value=1, max_value=8,
                            value=group.tournament.group_count, step=1,
                        ))
                        qualifiers_per_group = (
                            int(st.number_input(
                                "Direkte kvalificerede pr. gruppe",
                                min_value=0,
                                value=group.tournament.qualifiers_per_group or 1,
                                step=1,
                            ))
                            if group_count > 1
                            else None
                        )
                        bronze_match = st.checkbox(
                            "Spil bronzekamp", value=group.tournament.bronze_match
                        )
                    save = st.form_submit_button("Gem ændringer")
                if save:
                    try:
                        members = [same_game[key].member for key in selected]
                        members.extend(_parse_direct_lines(direct, group.game))
                        members_tuple = tuple(members)
                        old_ids = tuple(team.team_id for team in group.teams)
                        new_ids = tuple(dict.fromkeys(team.team_id for team in members_tuple))
                        membership_changed = set(old_ids) != set(new_ids)
                        edited_options: dict[str, object] = {
                            "match_points": edited_points,
                            "seed_rule": edited_seed_rule,
                            "standings_tiebreakers": edited_standing_rules,
                            "knockout_tiebreakers": (
                                *edited_knockout_rules, "higher_seed"
                            ),
                            "league_legs": league_legs,
                            "swiss_rounds": swiss_rounds,
                            "group_count": group_count,
                            "qualifiers_per_group": qualifiers_per_group,
                            "bronze_match": bronze_match,
                        }
                        if edited_seed_rule in {"manual", "elo"}:
                            prior_order = group.tournament.seed_order or old_ids
                            edited_options["seed_order"] = (
                                *(team_id for team_id in prior_order if team_id in set(new_ids)),
                                *(team_id for team_id in new_ids if team_id not in set(prior_order)),
                            )
                        target_knockout_rules = (
                            *edited_knockout_rules, "higher_seed"
                        )
                        structural_changed = any((
                            edited_template != group.tournament.template,
                            edited_start_round != group.tournament.start_round,
                            edited_rounds_per_tie != group.tournament.rounds_per_tie,
                            edited_points != group.tournament.match_points,
                            edited_seed_rule != group.tournament.seed_rule,
                            edited_standing_rules != group.tournament.standings_tiebreakers,
                            target_knockout_rules != group.tournament.knockout_tiebreakers,
                            league_legs != group.tournament.league_legs,
                            swiss_rounds != group.tournament.swiss_rounds,
                            group_count != group.tournament.group_count,
                            qualifiers_per_group != group.tournament.qualifiers_per_group,
                            bronze_match != group.tournament.bronze_match,
                        ))
                        final_changed = (
                            edited_template == "group_knockout"
                            and isinstance(fetched_final, int)
                            and fetched_final != group.tournament.final_round
                        )
                        if (
                            membership_changed
                            and edited_template == "group_knockout"
                            and not isinstance(fetched_final, int)
                        ):
                            raise PayloadError(
                                "Hent aktuel spilinfo før deltagerne ændres."
                            )
                        if membership_changed or final_changed or structural_changed:
                            target_final = (
                                fetched_final
                                if isinstance(fetched_final, int)
                                else group.tournament.final_round
                            )
                            store.plan_tournament(
                                group.game,
                                members_tuple,
                                start_round=edited_start_round,
                                final_round=target_final,
                                rounds_per_tie=edited_rounds_per_tie,
                                draw_seed=group.tournament.draw_seed,
                                template=edited_template,
                                definition_options=edited_options,
                            )
                            pending = (
                                group,
                                renamed.strip(),
                                members_tuple,
                                target_final,
                                edited_template,
                                edited_options,
                                edited_start_round,
                                edited_rounds_per_tie,
                            )
                            st.session_state["pending_tournament_rebuild"] = pending
                            _confirm_tournament_rebuild(store, *pending)
                        else:
                            store.update(replace(group, name=renamed.strip()))
                            st.success("Turneringens navn blev gemt.")
                            st.rerun()
                    except (PayloadError, ValueError) as exc:
                        st.error(str(exc))

                if group.archived_revisions:
                    st.markdown("#### Tidligere revisioner")
                    dataframe(
                        [
                            {
                                "Revision": item.revision,
                                "Arkiveret": item.archived_at,
                                "Periode": (
                                    f"{item.tournament.start_round}–"
                                    f"{item.tournament.final_round}"
                                ),
                                "Hold": len(item.teams),
                                "Seed": item.tournament.draw_seed or "–",
                                "Årsag": item.reason,
                            }
                            for item in reversed(group.archived_revisions)
                        ],
                        hide_index=True,
                        width="stretch",
                        key=f"tournament:{group.group_id}:archived-revisions",
                    )
            else:
                with st.form(f"edit-{group.group_id}"):
                    renamed = st.text_input("Navn", value=group.name)
                    selected = st.multiselect(
                        "Hold", list(labels), default=defaults,
                        format_func=lambda key: labels[key],
                    )
                    direct = st.text_area("Tilføj direkte fantasy-team URLs eller ID'er")
                    save = st.form_submit_button("Gem ændringer")
                if save:
                    try:
                        members = [same_game[key].member for key in selected]
                        members.extend(_parse_direct_lines(direct, group.game))
                        store.update(replace(
                            group, name=renamed.strip(), teams=tuple(members),
                        ))
                    except (PayloadError, ValueError) as exc:
                        st.error(str(exc))
                    else:
                        st.success("Gruppen blev gemt.")
                        st.rerun()

            confirm = st.checkbox(
                f"Jeg vil slette {group.name}",
                key=f"confirm-delete-{group.group_id}",
            )
            if st.button(
                "Slet gruppe", key=f"delete-{group.group_id}", disabled=not confirm,
            ):
                try:
                    active_competitions = {
                        competition
                        for season in SeasonStore(APP_PATHS.seasons_file).load()
                        if not season.is_archived
                        for competition in season.competition_ids
                    }
                    if group.group_id in active_competitions:
                        raise PayloadError("Fjern gruppen fra den aktive sæson før sletning.")
                    store.delete(group.group_id)
                except PayloadError as exc:
                    st.error(str(exc))
                else:

                    st.rerun()



def _not_found_view() -> None:
    st.title("Siden findes ikke")
    st.error(
        "Linket peger på en rute, som ikke længere findes. "
        "Åbn funktionen fra dens managerspil-, hold-, spiller- eller datasammenhæng."
    )
    page_link(
        PageId.HOME,
        "Gå til Mine managerspil",
        icon=":material/home:",
    )


@dataclass(frozen=True)
class UiContext:
    """Per-session inputs shared by the entrypoint and file-backed pages."""

    group_store: GroupStore
    account_store: AccountStore
    configuration: HubConfiguration
    configuration_warnings: tuple[str, ...]
    pairing_warnings: tuple[str, ...]
    games: tuple[ManagerGame, ...]
    active_games: tuple[ManagerGame, ...]
    groups: tuple[GroupDefinition, ...]
    index: SnapshotIndex
    group: GroupDefinition | None
    selected_game: ManagerGame | None
    read_only: bool


_UI_CONTEXT_KEY = "_holdet_ui_context"


def install_styles() -> None:
    _styles()


def build_ui_context() -> UiContext:
    _configure_paths()
    group_store = GroupStore(GROUPS_PATH, APP_PATHS.group_revision_dir)
    account_store = AccountStore(ACCOUNTS_PATH)
    try:
        configuration, configuration_warnings = (
            group_store.load_configuration_with_warnings()
        )
    except PayloadError as exc:
        configuration = HubConfiguration((), ())
        configuration_warnings = ()
        st.error(f"Hubkonfigurationen kunne ikke læses: {exc}")
    games = configuration.games
    active_games = tuple(game for game in games if not game.is_archived)
    index = _scan_snapshots(str(OUTPUT_DIR.resolve()))
    groups, pairing_warnings = _with_published_tournament_pairings(
        configuration.groups,
        index,
    )

    group_id = st.query_params.get("group")
    group = next((item for item in groups if item.group_id == group_id), None)
    requested_locale = str(st.query_params.get("locale", "")).casefold()
    requested_slug = str(st.query_params.get("game", ""))
    selected_game = next(
        (
            item for item in games
            if item.game.locale.casefold() == requested_locale
            and item.game.slug == requested_slug
        ),
        None,
    )
    if group is not None:
        selected_game = next(
            (item for item in games if item.identity == _game_identity(group.game)),
            selected_game,
        )

    return UiContext(
        group_store=group_store,
        account_store=account_store,
        configuration=configuration,
        configuration_warnings=tuple(configuration_warnings),
        pairing_warnings=tuple(pairing_warnings),
        games=games,
        active_games=active_games,
        groups=groups,
        index=index,
        group=group,
        selected_game=selected_game,
        read_only=bool(selected_game and selected_game.is_archived),
    )


def set_ui_context(context: UiContext) -> None:
    # Replace this value on every full rerun. Never share per-user state globally.
    st.session_state[_UI_CONTEXT_KEY] = context


def get_ui_context() -> UiContext:
    context = st.session_state.get(_UI_CONTEXT_KEY)
    if not isinstance(context, UiContext):
        raise RuntimeError("UI-konteksten er ikke initialiseret af appens entrypoint.")
    return context


def render_sidebar(context: UiContext, page_id: PageId) -> None:
    _sidebar(
        context.games,
        context.groups,
        context.selected_game,
        context.group,
        page_id.value,
    )


def render_shared_shell(context: UiContext, page_id: PageId) -> None:
    _warning_panel(context.index)
    for warning in (*context.configuration_warnings, *context.pairing_warnings):
        st.warning(warning)
    if (
        context.read_only
        and context.selected_game is not None
        and page_id in {PageId.GAME, PageId.GROUP, PageId.TEAM, PageId.PLAYER}
    ):
        _archived_banner(
            context.group_store,
            context.selected_game,
            allow_restore=page_id is PageId.GAME,
        )


def render_page(page_id: PageId) -> None:
    context = get_ui_context()
    games = context.games
    groups = context.groups
    index = context.index
    selected_game = context.selected_game
    group = context.group
    read_only = context.read_only

    if page_id is PageId.HOME:
        _home(context.active_games, groups, index)
    elif page_id is PageId.MANAGE_GAMES:
        _manage_games_view(context.group_store)
    elif page_id is PageId.ARCHIVE:
        _archive_view(games, groups, index)
    elif page_id is PageId.GAME and selected_game is not None:
        _game_view(
            selected_game,
            groups,
            index,
            context.group_store,
            read_only=read_only,
        )
    elif page_id is PageId.MANAGERS:
        managers_view(groups, index, APP_PATHS)
    elif page_id is PageId.CALENDAR:
        calendar_view(groups, APP_PATHS, index)
    elif page_id is PageId.DATA:
        data_storage_view(
            context.account_store,
            context.group_store,
            context.configuration,
            index,
            APP_PATHS,
        )
    elif page_id is PageId.ALERTS:
        _render_alerts_page(context)
    elif page_id is PageId.PLAYER:
        _render_player_page(context)
    elif page_id is PageId.PLAYERS:
        _standalone_player_statistics(games)
    elif page_id is PageId.TEAMS:
        _standalone_team_statistics(games, groups, index)
    elif page_id is PageId.GROUP and group is not None:
        if group.kind == "tournament":
            _tournament_view(group, groups, index, read_only=read_only)
        else:
            _standings_group_view(group, index)
    elif page_id is PageId.TEAM and group is not None:
        raw_team_id = st.query_params.get("team", "")
        if str(raw_team_id).isdigit():
            _team_view(group, index, int(raw_team_id), read_only=read_only)
        else:
            st.error("Ugyldigt hold-ID.")
    elif page_id is PageId.GAME:
        st.error("Managerspillet findes ikke.")
        page_link(PageId.HOME, "Gå til Mine managerspil")
    elif page_id in {PageId.GROUP, PageId.TEAM, PageId.PLAYER}:
        st.error("Gruppen, holdet eller spilleren findes ikke.")
        page_link(PageId.HOME, "Gå til Mine managerspil")
    else:
        _not_found_view()


def _render_alerts_page(context: UiContext) -> None:
    requested_game = _requested_game_from_query()
    if requested_game is not None and context.selected_game is not None:
        _navigate(
            "game",
            locale=context.selected_game.game.locale,
            game=context.selected_game.game.slug,
            section="alerts",
        )
    elif requested_game is not None:
        alerts_view(APP_PATHS, requested_game, standalone=True)
    elif target := _legacy_alert_target(context.games):
        _navigate(
            "game",
            locale=target.game.locale,
            game=target.game.slug,
            section="alerts",
        )
    else:
        st.title("Statusalarmer", anchor="statusalarmer")
        st.info(
            "Tilføj et managerspil eller vælg et spil under "
            "Spillerstatistik for at bruge statusalarmer."
        )


def _render_player_page(context: UiContext) -> None:
    detail_game = context.selected_game
    if detail_game is None:
        requested_game = _requested_game_from_query()
        if requested_game is not None:
            detail_game = ManagerGame(requested_game, requested_game.slug)
    player_key = str(st.query_params.get("player", ""))
    if player_key and detail_game is not None:
        player_detail_view(
            detail_game,
            player_key,
            APP_PATHS,
            read_only=context.read_only,
        )
    else:
        st.title("Spilleren blev ikke fundet")
        st.error("Spillerlinket mangler en spilleridentitet.")

