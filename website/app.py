"""Local, on-demand Streamlit dashboard for Holdet fantasy groups."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from holdet_lib._formatting import count_label
from holdet_lib.accounts import AccountStore
from holdet_lib.team_exports import (
    TEAM_EXPORT_FORMATS, TeamExportStore, build_team_export,
)
from holdet_lib.version import VERSION
from website.data_page import data_storage_view

from holdet_lib import (
    GroupDefinition,
    GroupMatch,
    HubConfiguration,
    GroupStore,
    GroupTeam,
    GameUrl,
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
    STAGE_NAMES,
    TournamentState,
    TeamSnapshot,
    TeamReference,
    build_player_export,
    build_standings,
    build_tournament_head_to_head,
    build_tournament_state,
    filter_player_statistics,
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
    parse_direct_team_url,
    refresh_game,
    refresh_group,
    resolve_paths,
)


APP_PATHS = resolve_paths()
CONFIG_DIR = APP_PATHS.config_dir
OUTPUT_DIR = APP_PATHS.snapshot_dir
MANIFEST_DIR = APP_PATHS.manifest_dir
GROUPS_PATH = APP_PATHS.groups_file
ACCOUNTS_PATH = APP_PATHS.accounts_file
PLAYER_EXPORT_DIR = APP_PATHS.player_export_dir
TEAM_EXPORT_DIR = APP_PATHS.team_export_dir

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

def _client() -> HoldetClient:
    return HoldetClient()


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
        return "Ingen lokale data endnu \u00b7 Klar til manuel opdatering"
    prefix = f"Viser lokale data fra {_format_local_date(generated_at)}"
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


def _format_table_integer(value: object) -> str:
    if value is None or bool(pd.isna(value)):
        return "–"
    integer = int(value)
    if isinstance(value, bool) or value != integer:
        raise ValueError(f"table value must be a whole number, got {value!r}")
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
    st.query_params.clear()
    st.query_params["view"] = view
    for key, value in parameters.items():
        st.query_params[key] = str(value)
    st.rerun()


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
    **parameters: object,
) -> None:
    """Render a native, keyboard-accessible card without a browser reload."""
    label = (
        f"**{_markdown_literal(title)}**  \n"
        f":small[{_markdown_literal(subtitle)}]  \n"
        f"{_markdown_literal(detail)}  \n"
        f"**{_markdown_literal(action)} \u2192**"
    )
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
    folders = (
        MANIFEST_DIR / group.game.slug / "game",
        MANIFEST_DIR / group.game.slug / "groups" / group.group_id,
    )
    for folder in folders:
        candidates = sorted(folder.glob("refresh-round*.json"), reverse=True)
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                teams = payload.get("teams") if isinstance(payload, dict) else None
                if not isinstance(teams, list):
                    continue
                member_ids = {member.team_id for member in group.teams}
                return frozenset(
                    item["team_id"]
                    for item in teams
                    if isinstance(item, dict)
                    and isinstance(item.get("team_id"), int)
                    and item["team_id"] in member_ids
                    and item.get("status") != "success"
                )
            except (OSError, json.JSONDecodeError):
                continue
    return frozenset()


def _styles() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: #111419; }
        [data-testid="stHeader"] { background: rgba(17,20,25,.92); }
        [data-testid="stSidebar"] { background: #191d24; }
        .block-container { max-width: 1180px; padding-top: 2rem; }
        .holdet-title { letter-spacing: -.04em; margin-bottom: .15rem; }
        .muted { color: #9da6b5; }
        div[class*="st-key-nav-card-"] { margin: .2rem 0 .9rem; border-radius: 15px; }
        div[class*="st-key-nav-card-"] [data-testid="stButton"] button {
            width: 100%; min-height: 120px; padding: 1rem 1.15rem;
            justify-content: flex-start !important; align-items: flex-start !important;
            border-radius: 15px; text-align: left !important;
            border: 1px solid rgba(255,255,255,.10) !important;
            box-shadow: 0 10px 30px rgba(0,0,0,.17);
            cursor: pointer; transition: transform .16s ease, box-shadow .16s ease,
                        border-color .16s ease;
        }
        div[class*="st-key-nav-card-"] [data-testid="stMarkdownContainer"] {
            width: 100%;
        }
        div[class*="st-key-nav-card-"] [data-testid="stMarkdownContainer"] p {
            margin: 0; width: 100%; text-align: left !important; line-height: 1.55;
        }
        div[class*="st-key-nav-card-"] [data-testid="stMarkdownContainer"] strong {
            font-size: 1.2rem;
        }
        div[class*="st-key-nav-card-"] [data-testid="stButton"] button > div {
            width: 100% !important;
            justify-content: flex-start !important;
            align-items: flex-start !important;
        }
        div[class*="st-key-nav-card-"] [data-testid="stButton"] button > div > span {
            width: 100% !important;
            justify-content: flex-start !important;
            align-items: flex-start !important;
        }
        div[class*="st-key-nav-card-"] [data-testid="stButton"] button p {
            width: 100%; text-align: left !important;
        }
        div[class*="st-key-nav-card-"] [data-testid="stButton"] button:hover {
            transform: translateY(-3px); box-shadow: 0 14px 34px rgba(0,0,0,.25);
            border-color: rgba(255,255,255,.28) !important;
        }
        div[class*="st-key-nav-card-"] [data-testid="stButton"] button:focus-visible {
            outline: 3px solid #ff4b4b; outline-offset: 3px;
        }
        div[class*="st-key-sidebar-group-"] {
            margin-left: 1.25rem;
            width: calc(100% - 1.25rem);
        }
        div[class*="st-key-sidebar-group-"] [data-testid="stButton"] button {
            min-height: 2.25rem;
            padding: .35rem .7rem;
            background: #232831 !important;
            border-color: #3a414d !important;
            color: #e7eaf0 !important;
            justify-content: flex-start;
            text-align: left;
        }
        div[class*="st-key-sidebar-group-"] [data-testid="stButton"] button[kind="primary"] {
            border-left: 3px solid #ff4b4b !important;
            background: #292e37 !important;
        }
        div[class*="st-key-sidebar-group-"] [data-testid="stButton"] button:focus-visible {
            outline: 2px solid #ff4b4b;
            outline-offset: 2px;
        }
        .positive { color: #76da91; }
        .negative { color: #ff777d; }
        div[data-testid="stDataFrame"] { border: 1px solid #303642; border-radius: 12px; overflow: hidden; }
        </style>
        """,
        unsafe_allow_html=True,
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
        st.caption("MANAGERSPIL")
        for game in active_games:
            active = selected_game is not None and game.identity == selected_game.identity
            if st.button(
                game.name,
                key=f"nav-game-{game.game.locale}-{game.game.slug}",
                width="stretch",
                type="primary" if active else "secondary",
            ):
                _navigate("game", locale=game.game.locale, game=game.game.slug)
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
            "Data og lager",
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
) -> None:
    color, foreground = _colors(manager_game.game.slug)
    group_count, team_count, _ = _game_statistics(manager_game, groups, index)
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
            f"{_group_count_label(group_count)} · {team_count} unikke hold · "
            f"{_snapshot_data_status(newest_snapshot)}"
        ),
        color=color,
        foreground=foreground,
        aria_label=f"Åbn managerspil {manager_game.name}",
        icon=_sport_icon(manager_game.game.slug),
        action="\u00c5bn og opdater manuelt",
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
        st.markdown(
            '<h1 class="holdet-title">Mine managerspil</h1>',
            unsafe_allow_html=True,
        )
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
    st.markdown(
        '<p class="muted">Grupper og turneringer samlet efter managerspil.</p>',
        unsafe_allow_html=True,
    )
    if games:
        columns = st.columns(2)
        for position, manager_game in enumerate(_sorted_manager_games(games)):
            with columns[position % 2]:
                _manager_game_card(manager_game, groups, index)
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
    try:
        with st.spinner(
            "Henter seneste spillerstatistik …"
            if round_number is None
            else f"Henter spillerstatistik for runde {round_number} …"
        ):
            statistics = _client().fetch_players(game, round_number=round_number)
            if round_number is not None and statistics.round_number != round_number:
                raise PayloadError(
                    f"Holdet returnerede runde {statistics.round_number} "
                    f"i stedet for runde {round_number}."
                )
            saved = PlayerStatisticsStore(OUTPUT_DIR).save(statistics)
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
        )
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
    value = st.number_input(label, value=None, step=1_000, key=key)
    return int(value) if value is not None else None


def _player_filter_query(statistics, scope: str) -> PlayerStatisticsQuery:
    labels = player_column_labels(statistics)
    search = st.text_input(
        "Søg",
        placeholder="Navn, hold/land eller position/kategori",
        key=f"{scope}-search",
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
                labels["team"], teams, key=f"{scope}-teams"
            )
        )
        selected_positions = tuple(
            st.multiselect(
                labels["position"], positions, key=f"{scope}-positions"
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
            )
        with missing_columns[1]:
            missing_round = st.selectbox(
                f"Manglende {labels['round_growth'].casefold()}",
                MISSING_VALUE_MODES,
                format_func=missing_labels.__getitem__,
                key=f"{scope}-missing-round",
            )
        optional_columns = tuple(column for column in PLAYER_COLUMNS if column != "name")
        selected_optional = tuple(
            st.multiselect(
                "Kolonner",
                optional_columns,
                default=optional_columns,
                format_func=labels.__getitem__,
                key=f"{scope}-columns",
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
            )
        with sort_columns[1]:
            sort_order = st.selectbox(
                "Sorteringsretning",
                ("desc", "asc"),
                format_func={"desc": "Faldende", "asc": "Stigende"}.__getitem__,
                key=f"{scope}-sort-order",
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
    )
    for key in tuple(st.session_state):
        if any(str(key).startswith(prefix) for prefix in prefixes):
            st.session_state.pop(key, None)
    st.rerun()


def _player_export_section(statistics, query, selected, scope: str) -> None:
    st.subheader("Eksport")
    formats = st.pills(
        "Filformater",
        PLAYER_EXPORT_FORMATS,
        default=("txt",),
        selection_mode="multi",
        format_func={"txt": "TXT", "json": "JSON", "md": "Markdown"}.__getitem__,
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
    selected_round = st.selectbox(
        "Runde",
        tuple(range(latest_known_round, 0, -1)) or (0,),
        key=f"player-round-{game.locale}-{game.slug}-{empty_label}",
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
    scope = f"player-filter-{game.locale}-{game.slug}"
    try:
        query = _player_filter_query(statistics, scope)
    except ValueError as exc:
        st.error(str(exc))
        return
    entries = filter_player_statistics(statistics, query)
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        st.caption(
            f"{len(entries)} af {len(statistics.entries)} spillere · runde "
            f"{statistics.round_number} · gemt "
            f"{selected.generated_at.astimezone().strftime('%d.%m.%Y %H:%M:%S')}"
        )
        if st.button(
            "Nulstil filtre",
            icon=":material/filter_alt_off:",
            key=f"{scope}-reset",
        ):
            _reset_player_filters(scope)
    if entries:
        rows, integer_columns = _player_statistics_rows(statistics, query)
        st.dataframe(
            _style_integer_columns(rows, integer_columns),
            hide_index=True,
            width="stretch",
            key=f"player-statistics-{game.locale}-{game.slug}-{statistics.round_number}",
        )
        _player_export_section(statistics, query, selected, scope)
    else:
        st.info("Ingen spillere matcher de valgte filtre.")


def _player_statistics_tab(
    manager_game: ManagerGame, *, read_only: bool = False
) -> None:
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
    option_labels = {
        item[0].original: item[1] for item in sorted_suggestions
    }
    selected_source = st.selectbox(
        "Managerspil",
        option_values,
        index=None,
        accept_new_options=True,
        placeholder="Vælg et spil, eller indtast URL/slug",
        format_func=lambda value: option_labels.get(value, value),
        key="standalone-player-game",
    )
    if not selected_source:
        st.info("Vælg eller indtast et managerspil for at se spillerstatistik.")
        return
    try:
        game = normalize_manager_game(str(selected_source)).game
    except (PayloadError, ValueError) as exc:
        st.error(f"Ugyldigt managerspil: {exc}")
        return
    st.caption(f"Valgt spil: {game.slug} · sprog: {game.locale}")
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
            store.archive_manager_game(manager_game.game)
        except PayloadError as exc:
            st.error(str(exc))
        else:
            st.session_state.pop("pending_archive_manager_game", None)
            _navigate("archive")


def _game_groups_tab(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    index: SnapshotIndex,
    *,
    read_only: bool = False,
) -> None:
    game_groups = tuple(
        group for group in groups if _game_identity(group.game) == manager_game.identity
    )
    if st.button(
        "Opdater managerspil",
        type="primary",
        width="stretch",
        disabled=read_only,
        help="Gendan managerspillet for at opdatere data." if read_only else None,
    ):
        if not game_groups:
            st.warning("Managerspillet har ingen grupper at opdatere.")
        else:
            with st.spinner("Henter hvert aktivt hold højst én gang …"):
                result = refresh_game(
                    manager_game,
                    game_groups,
                    _client(),
                    SnapshotStore(OUTPUT_DIR),
                    ManifestStore(MANIFEST_DIR),
                )
            _invalidate_snapshot_index()
            successes = sum(item.status == "success" for item in result.teams)
            fallbacks = sum(item.status == "cached_fallback" for item in result.teams)
            st.session_state["game_refresh_notice"] = (
                f"{successes} hold opdateret. {fallbacks} bruger cache. "
                f"Manifest: {result.manifest_path.name}"
            )
            st.rerun()
    if notice := st.session_state.pop("game_refresh_notice", None):
        st.success(notice)

    group_count, team_count, latest_round = _game_statistics(manager_game, groups, index)
    metric_cols = st.columns(3)
    metric_cols[0].metric("Grupper", group_count)
    metric_cols[1].metric("Unikke hold", team_count)
    metric_cols[2].metric("Seneste datarunde", latest_round or "–")

    st.subheader("Grupper")
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
    groups_tab, players_tab, teams_tab, manage_tab, settings_tab = st.tabs(
        ("Grupper", "Spillerstatistik", "Holdstatistik", "Administrer grupper", "Spilindstillinger"),
        key=f"game-tabs-{manager_game.game.locale}-{manager_game.game.slug}",
        on_change="rerun",
    )
    if groups_tab.open:
        with groups_tab:
            _game_groups_tab(
                manager_game, groups, index, read_only=read_only
            )
    if players_tab.open:
        with players_tab:
            _player_statistics_tab(manager_game, read_only=read_only)
    if teams_tab.open:
        with teams_tab:
            _team_statistics_game_tab(manager_game, groups, index, read_only=read_only)
    game_groups = tuple(
        group for group in groups if _game_identity(group.game) == manager_game.identity
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
    event = st.dataframe(
        _style_integer_columns(rows, ("Værdi", "Vækst", "Afstand")),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"standing-{group.group_id}-{round_number}-{mode}",
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


def _standings_group_view(group: GroupDefinition, index: SnapshotIndex) -> None:
    color, _ = _colors(group.game.slug)
    st.markdown(
        f'<div style="height:6px;border-radius:6px;background:{color};margin-bottom:1rem"></div>',
        unsafe_allow_html=True,
    )
    st.title(group.name, anchor=f"gruppe-{group.group_id}")
    st.caption(f"{group.game.slug} · {len(group.teams)} faste hold")
    if not group.teams:
        st.info("Gruppen har ingen hold endnu.")
        return
    rounds = index.rounds_for(
        group.game.slug, tuple(member.team_id for member in group.teams)
    )
    if not rounds:
        st.warning("Der er endnu ingen kompatible snapshots for gruppen.")
        return
    controls = st.columns([1, 2, 3])
    with controls[0]:
        round_number = st.selectbox("Runde", rounds, key=f"round-{group.group_id}")
    with controls[1]:
        label = st.radio(
            "Visning", ("Overall", "Runde"), horizontal=True, key=f"mode-{group.group_id}"
        )
    _standings_table(group, index, int(round_number), "overall" if label == "Overall" else "round")




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
        "complete": "F\u00e6rdig",
        "in_progress": "Runde i gang",
        "unknown": "Rundestatus ukendt",
        "missing": "Mangler data",
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
    event = st.dataframe(
        _style_integer_columns(rows, ("For", "Imod", "Forskel")),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"tournament-standing-{group.group_id}-{round_number}",
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
    st.dataframe(
        _style_integer_columns(
            rows, (summary.team_a_name, summary.team_b_name)
        ),
        hide_index=True,
        width="stretch",
    )


def _tournament_matches(
    group: GroupDefinition, state: TournamentState, index: SnapshotIndex
) -> None:
    _tournament_head_to_head(group, state, index)
    st.divider()
    st.subheader("Alle gruppespilskampe")
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
    st.dataframe(
        _style_integer_columns(rows, ("Vækst A", "Vækst B")),
        hide_index=True,
        width="stretch",
    )


def _tournament_bracket(group: GroupDefinition, state: TournamentState) -> None:
    assert group.tournament is not None
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
    group: GroupDefinition, index: SnapshotIndex, *, read_only: bool = False
) -> None:
    assert group.tournament is not None
    color, _ = _colors(group.game.slug)
    latest_round = latest_tournament_round(group, index)
    st.markdown(
        f'<div style="height:6px;border-radius:6px;background:{color};margin-bottom:1rem"></div>',
        unsafe_allow_html=True,
    )
    st.title(group.name, anchor=f"gruppe-{group.group_id}")
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
            result = refresh_group(
                group,
                _client(),
                SnapshotStore(OUTPUT_DIR),
                ManifestStore(MANIFEST_DIR),
            )
        _invalidate_snapshot_index()
        successes = sum(item.status == "success" for item in result.teams)
        fallbacks = sum(item.status == "cached_fallback" for item in result.teams)
        failures = tuple(item for item in result.teams if item.status == "failed")
        st.session_state[f"tournament-refresh-notice-{group.group_id}"] = {
            "successes": successes,
            "fallbacks": fallbacks,
            "failures": tuple((item.team_name, item.error) for item in failures),
            "manifest": result.manifest_path.name,
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
    overview_tab, standings_tab, matches_tab, knockout_tab = st.tabs(
        ["Overblik", "Gruppestilling", "Kampe", "Knockout"],
        key=f"tournament-tabs-{group.group_id}",
        on_change="rerun",
    )
    if overview_tab.open:
        with overview_tab:
            _tournament_overview(group, state)
    if standings_tab.open:
        with standings_tab:
            _tournament_standings_table(group, state, round_number)
    if matches_tab.open:
        with matches_tab:
            _tournament_matches(group, state, index)
    if knockout_tab.open:
        with knockout_tab:
            _tournament_bracket(group, state)


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
            "Overall-rang": summary.overall_rank,
            "Runderangændring": summary.round_rank_change,
            "Overall-rangændring": summary.overall_rank_change,
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
) -> None:
    team = snapshot.team
    rounds = index.rounds_for(team.reference.game, (team.reference.team_id,))
    if not rounds:
        rounds = (team.overview.current_round,)
    chosen = default_round if default_round in rounds else rounds[0]
    round_number = int(st.selectbox(
        "Runde", rounds, index=rounds.index(chosen),
        key=f"team-round-{team.reference.game.slug}-{team.reference.team_id}",
    ))
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
    overview_tab, roster_tab, history_tab, export_tab = st.tabs(
        ("Overblik", "Holdopstilling", "Historik", "Eksport"),
        key=f"team-tabs-{team.reference.game.slug}-{team.reference.team_id}",
        on_change="rerun",
    )
    if overview_tab.open:
        with overview_tab:
            if summary is None:
                st.warning(f"Mangler rundesammendrag for runde {round_number}.")
            else:
                metrics = st.columns(4)
                metrics[0].metric("Total", _format_number(summary.total))
                metrics[1].metric("Rundevækst", _format_number(summary.change, signed=True))
                metrics[2].metric("Overall-rang", _format_number(summary.overall_rank))
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
                    ("Overall-rangændring", summary.overall_rank_change),
                ]
                st.dataframe(_style_integer_columns(
                    [{"Del": label, "Ændring": value} for label, value in growth if value is not None],
                    ("Ændring",),
                ), hide_index=True, width="stretch")
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
                st.dataframe(_style_integer_columns(rows, (value_label, "Rundevækst", "Vækst siden køb")), hide_index=True, width="stretch")
    if history_tab.open:
        with history_tab:
            rows = _team_history_rows(snapshot.team)
            numeric = tuple(key for key in rows[0] if key not in {"Runde"}) if rows else ()
            if rows:
                st.dataframe(_style_integer_columns(rows, numeric), hide_index=True, width="stretch")
            else:
                st.info("Der findes endnu ingen rundehistorik.")
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
    selected = locked_reference or st.selectbox(
        "Hold",
        ordered,
        index=None,
        placeholder="V\u00e6lg et hold",
        format_func=lambda item: f"{item.team_name} — {item.account_label} (ID {item.team_id})",
        key=f"team-statistics-choice-{game.locale}-{game.slug}",
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
    _render_team_snapshot(newest, index, default_round=default_round, caption=caption)


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
        "Managerspil",
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
        draw_seed_key = (
            f"create-tournament-seed-{game.locale.casefold()}-{game.slug}"
        )
        if group_type == "Turnering":
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
            draw_seed = st.session_state.setdefault(
                draw_seed_key, generate_draw_seed()
            )
            st.caption("Lodtr?kningsseed")
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
            preview_members = [same_game[key].member for key in selected]
            preview_error: Exception | None = None
            if direct.strip():
                try:
                    preview_members.extend(_parse_direct_lines(direct, game))
                except (PayloadError, ValueError) as exc:
                    preview_error = exc
            unique_count = len({member.team_id for member in preview_members})
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
                    )
                except (PayloadError, ValueError) as exc:
                    st.warning(str(exc))
                else:
                    draw_seed = preview_config.draw_seed
                    st.session_state[draw_seed_key] = draw_seed
                    stage_count = preview_config.knockout_stage_count
                    st.info(
                        f"{unique_count} hold · top {preview_config.knockout_size} "
                        f"går videre · gruppespil runde {start_round}–"
                        f"{preview_config.group_end_round} · "
                        f"{count_label(stage_count, 'knockoutfase', 'knockoutfaser')}"
                    )
                    if unique_count == 2:
                        st.warning(
                            "Med to hold findes der kun én mulig modstanderplan. "
                            "Et nyt seed kan derfor ikke ændre kampene."
                        )
                    names = {member.team_id: member.name for member in preview_members}
                    with st.expander("Forhåndsvis kampplan"):
                        st.dataframe(
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
                        )
            else:
                st.caption("En turnering kræver mindst to hold.")

        if st.button("Opret gruppe", type="primary"):
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
                st.caption(
                    f"Aktiv revision {group.active_revision} · runde "
                    f"{group.tournament.start_round}–{group.tournament.final_round} · "
                    f"top {group.tournament.knockout_size} · "
                    f"{count_label(group.tournament.rounds_per_tie, 'runde', 'runder')} "
                    "pr. opgør"
                )
                st.caption(
                    "Lodtrækningsseed: "
                    + (group.tournament.draw_seed or "Ikke gemt (ældre turnering)")
                )
                info = _known_game_info(group.game)
                fetched_final = getattr(info, "final_round", None)
                if isinstance(fetched_final, int) and fetched_final != group.tournament.final_round:
                    st.warning(
                        f"Holdet angiver nu finalerunde {fetched_final}; den aktive "
                        f"revision bruger {group.tournament.final_round}. Gem ændringer "
                        "for at vælge, om turneringen skal genberegnes."
                    )
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
                        members_tuple = tuple(members)
                        old_ids = tuple(team.team_id for team in group.teams)
                        new_ids = tuple(dict.fromkeys(team.team_id for team in members_tuple))
                        membership_changed = set(old_ids) != set(new_ids)
                        final_changed = (
                            isinstance(fetched_final, int)
                            and fetched_final != group.tournament.final_round
                        )
                        if membership_changed and not isinstance(fetched_final, int):
                            raise PayloadError(
                                "Hent aktuel spilinfo før deltagerne ændres."
                            )
                        if membership_changed or final_changed:
                            target_final = (
                                fetched_final if isinstance(fetched_final, int)
                                else group.tournament.final_round
                            )
                            size = knockout_size_for(len(set(new_ids)))
                            group_end = target_final - (
                                size.bit_length() - 1
                            ) * group.tournament.rounds_per_tie
                            if group_end < group.tournament.start_round:
                                raise PayloadError(
                                    "Ændringen efterlader ikke mindst én gruppespilsrunde."
                                )
                            pending = (
                                group, renamed.strip(), members_tuple, target_final
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
                    st.dataframe(
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
                        hide_index=True, width="stretch",
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
                store.delete(group.group_id)
                st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Holdet Fantasy Hub",
        page_icon="🏆",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _styles()
    group_store = GroupStore(GROUPS_PATH, APP_PATHS.group_revision_dir)
    account_store = AccountStore(ACCOUNTS_PATH)
    try:
        configuration = group_store.load_configuration()
    except PayloadError as exc:
        configuration = HubConfiguration((), ())
        st.error(f"Hubkonfigurationen kunne ikke læses: {exc}")
    games = configuration.games
    active_games = tuple(game for game in games if not game.is_archived)
    groups = configuration.groups
    index = _scan_snapshots(str(OUTPUT_DIR.resolve()))

    view = st.query_params.get("view", "home")
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

    _sidebar(games, groups, selected_game, group, str(view))
    _warning_panel(index)
    read_only = bool(selected_game and selected_game.is_archived)
    if read_only and selected_game is not None and view in {"game", "group", "team"}:
        _archived_banner(
            group_store,
            selected_game,
            allow_restore=view == "game",
        )

    if view == "manage-games":
        _manage_games_view(group_store)
    elif view == "archive":
        _archive_view(games, groups, index)
    elif view == "game" and selected_game is not None:
        _game_view(
            selected_game, groups, index, group_store, read_only=read_only
        )
    elif view == "data":
        data_storage_view(account_store, group_store, configuration, index, APP_PATHS)
    elif view == "players":
        _standalone_player_statistics(games)
    elif view == "teams":
        _standalone_team_statistics(games, groups, index)
    elif view == "group" and group is not None:
        if group.kind == "tournament":
            _tournament_view(group, index, read_only=read_only)
        else:
            _standings_group_view(group, index)
    elif view == "team" and group is not None:
        raw_team_id = st.query_params.get("team", "")
        if str(raw_team_id).isdigit():
            _team_view(group, index, int(raw_team_id), read_only=read_only)
        else:
            st.error("Ugyldigt hold-ID.")
    elif view == "game":
        st.error("Managerspillet findes ikke.")
        _home(active_games, groups, index)
    else:
        _home(active_games, groups, index)


if __name__ == "__main__":
    main()

