"""UI pages for the additive Fantasy Hub centers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from uuid import uuid4
import hashlib
from html import escape
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from website.navigation import PageId, page_link
from website.presentation import (
    data_status_badges,
    data_status_label,
    dataframe,
    format_elo,
    format_precise_time,
    format_relative_precise,
    render_status_badges,
)

from holdet_lib import (
    AppPaths,
    GameMetadataStore,
    GroupDefinition,
    GameUrl,
    HallOfFameScoreProfile,
    HallOfFameStore,
    HubSettings,
    HubSettingsStore,
    ManagerAlias,
    ManagerGame,
    PlayerStatisticsIndex,
    PlayerStatisticsSnapshot,
    PlayerStatisticsStore,
    SnapshotIndex,
    TeamSnapshot,
    TransferScenario,
    bracket_seed_order,
    build_data_quality_report,
    build_hall_of_fame,
    build_double_elimination_bracket,
    build_history_series,
    build_intra_round_diff,
    build_live_hall_of_fame_events,
    build_player_history,
    build_scouting_metrics,
    compare_round_snapshots,
    compare_team_rounds,
    create_backup_bytes,
    format_integer,
    player_identity,
    restore_backup,
    simulate_transfers,
    transfer_rule_profile,
    validate_backup,
    watchlist_entry,
)
from holdet_lib.hub_settings import manager_identity_keys
from holdet_lib.tournament import STAGE_NAMES, KnockoutMatch, TournamentState
from holdet_lib import (
    CalendarEvent,
    ManagerCareer,
    ManagerProfile,
    SeasonStore,
    build_calendar_events,
    build_effective_manager_settings,
    build_manager_careers,
    build_manager_head_to_head,
    build_manager_ratings,
    build_manager_round_results,
    build_round_story,
    remap_manager_events,
    build_season_standings,
)
from holdet_lib.hub_settings import effective_manager_profiles
from holdet_lib import resolve_manager_identity
def _time(value: datetime | None) -> str:
    return format_precise_time(value)


def _age(value: datetime | None) -> str:
    return format_relative_precise(value)


def _round_center_link_card(
    *,
    key: str,
    label: str,
    value: str,
    icon: str,
    page_id: PageId,
    badges=(),
    **parameters: object,
) -> None:
    with st.container(border=True, key=f"round-status-{key}", width=260):
        page_link(
            page_id,
            f"**{label}**  \n{value}",
            icon=icon,
            width="stretch",
            **parameters,
        )
        if badges:
            render_status_badges(tuple(badges))


def _game_label(game: ManagerGame) -> str:
    return f"{game.name} · {game.game.slug}"


def _player_index(paths: AppPaths) -> PlayerStatisticsIndex:
    return PlayerStatisticsStore(paths.snapshot_dir).scan()


def _status_labels(entry) -> str:
    labels: list[str] = []
    if not entry.is_active:
        labels.append("Inaktiv")
    if entry.is_disabled:
        labels.append("Deaktiveret")
    if entry.is_injured:
        labels.append("Skadet")
    if entry.has_suspension:
        labels.append("Karantæne")
    return ", ".join(labels) or "Aktiv"


def manager_round_center(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    paths: AppPaths,
) -> None:
    """Render one manager game's cache-only round status."""

    round_status_slot = st.container()
    with round_status_slot:
        with st.skeleton(height=140):
            players = _player_index(paths)
            metadata = GameMetadataStore(paths.game_metadata_dir).load(
                manager_game.game
            )
            game_groups = tuple(
                group for group in groups if (
                    group.game.locale.casefold(), group.game.slug
                ) == manager_game.identity
            )
            team_labels: dict[int, str] = {}
            for group in game_groups:
                for member in group.teams:
                    team_labels.setdefault(member.team_id, member.name)
            report = build_data_quality_report(
                (manager_game,),
                game_groups,
                teams,
                players,
                manifest_dir=paths.manifest_dir,
                include_archived=True,
            )
    latest = report.rounds[0] if report.rounds else None
    active_round = (
        metadata.active_round
        if metadata is not None and metadata.active_round is not None
        else latest.round_number if latest is not None else None
    )
    deadline = metadata.next_deadline if metadata is not None else None
    team_round_status = None
    if latest is not None:
        if latest.in_progress:
            team_round_status = "in_progress"
        elif latest.unknown:
            team_round_status = "unknown"
        else:
            team_round_status = "complete"
    team_badges = data_status_badges(
        generated_at=None if latest is None else latest.newest_team_data,
        round_number=None if latest is None else latest.round_number,
        round_status=team_round_status,
        metadata=metadata,
        missing=latest is None or bool(latest.missing_team_ids),
        last_success=None if latest is None else latest.last_success,
        last_error=None if latest is None else latest.last_error,
    )
    player_badges = data_status_badges(
        generated_at=None if latest is None else latest.newest_player_data,
        round_number=None if latest is None else latest.round_number,
        round_status=None if latest is None else latest.player_round_status,
        metadata=metadata,
        missing=latest is None or not latest.player_snapshot,
        last_success=None if latest is None else latest.last_success,
        last_error=None if latest is None else latest.last_error,
    )
    missing_count = (
        len(latest.missing_team_ids) if latest is not None else len(team_labels)
    )
    with st.container(horizontal=True, gap="small"):
        _round_center_link_card(
            key="active-round",
            label="Aktiv runde",
            value=str(active_round or "Mangler"),
            icon=":material/event:",
            page_id=PageId.GAME,
            locale=manager_game.game.locale,
            game=manager_game.game.slug,
            section="groups",
        )
        _round_center_link_card(
            key="deadline",
            label="Næste deadline",
            value=_time(deadline) if deadline is not None else "Mangler metadata",
            icon=":material/calendar_month:",
            page_id=PageId.CALENDAR,
            locale=manager_game.game.locale,
            game=manager_game.game.slug,
        )
        _round_center_link_card(
            key="team-data",
            label="Holddata",
            value=_age(None if latest is None else latest.newest_team_data),
            icon=":material/groups:",
            page_id=PageId.GAME,
            badges=team_badges,
            locale=manager_game.game.locale,
            game=manager_game.game.slug,
            section="teams",
        )
        _round_center_link_card(
            key="player-data",
            label="Spillerdata",
            value=_age(None if latest is None else latest.newest_player_data),
            icon=":material/query_stats:",
            page_id=PageId.GAME,
            badges=player_badges,
            locale=manager_game.game.locale,
            game=manager_game.game.slug,
            section="players",
        )
        _round_center_link_card(
            key="missing-data",
            label="Manglende snapshots",
            value=str(missing_count),
            icon=":material/data_alert:",
            page_id=PageId.DATA,
            section="quality",
        )

    if metadata is None:
        st.warning(
            "Tidsplan og deadline mangler. Brug den manuelle opdatering ovenfor "
            "for at hente og gemme spilmetadata."
        )
    if latest is None:
        st.info("Der er endnu ingen cachede rundedata for managerspillet.")
    else:
        if latest.reasons:
            st.caption(" · ".join(latest.reasons))
        if latest.last_error_message:
            st.error(latest.last_error_message)
        if latest.missing_team_names:
            st.warning("Mangler hold: " + ", ".join(latest.missing_team_names))
        st.caption(
            f"Seneste succes: {_time(latest.last_success)} · "
            f"seneste fejl: {_time(latest.last_error)}"
        )

    target_round = latest.round_number if latest is not None else None
    if target_round is not None and game_groups:
        _, manager_settings = _manager_settings(paths)
        story = build_round_story(
            game_groups,
            teams,
            manager_settings,
            manager_game.game.slug,
            target_round,
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
    rank_rows: list[dict[str, object]] = []
    if target_round is not None:
        for team_id in team_labels:
            change = compare_team_rounds(
                teams, manager_game.game, team_id, target_round
            )
            if change is None or change.old_rank == change.new_rank:
                continue
            rank_rows.append(
                {
                    "Hold": change.team_name,
                    "Fra": change.old_rank,
                    "Til": change.new_rank,
                    "Bevægelse": change.rank_movement,
                }
            )
    latest_players = players.newest(manager_game.game, target_round)
    if latest_players is None:
        latest_players = players.newest(manager_game.game)
    injury_rows = []
    if latest_players is not None:
        for entry in latest_players.statistics.entries:
            if entry.is_injured or entry.has_suspension:
                injury_rows.append(
                    {
                        "Spiller": entry.name,
                        "Hold": entry.team,
                        "Status": _status_labels(entry),
                    }
                )
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("**Rangbevægelser**")
            if rank_rows:
                dataframe(
                    rank_rows,
                    hide_index=True,
                    key=(
                        f"round-center:{manager_game.game.locale}:"
                        f"{manager_game.game.slug}:rank-movements"
                    ),
                )
            else:
                st.caption("Ingen bevægelser mellem de seneste tilgængelige runder.")
    with right:
        with st.container(border=True):
            st.markdown("**Skader og karantæner**")
            if injury_rows:
                dataframe(
                    injury_rows,
                    hide_index=True,
                    key=(
                        f"round-center:{manager_game.game.locale}:"
                        f"{manager_game.game.slug}:injuries"
                    ),
                )
            else:
                st.caption("Ingen markeringer i det valgte spillersnapshot.")

def transfer_lab_panel(
    team_snapshot: TeamSnapshot,
    player_snapshot: PlayerStatisticsSnapshot | None,
    *,
    team_round_status: str = "unknown",
) -> None:
    """Render a transfer scenario for an already selected team and round."""

    team = team_snapshot.team
    round_number = team.overview.current_round
    st.caption(
        "Scenariet lever kun i denne browsersession og ændrer hverken Holdet, "
        "snapshots eller konfiguration."
    )
    if player_snapshot is None:
        st.error(
            f"Spillersnapshot for runde {round_number} mangler. "
            "Scenariet kan ikke valideres."
        )
        return
    statistics = player_snapshot.statistics
    profile = transfer_rule_profile(
        variant=statistics.variant,
        game_format=statistics.format or "",
        game_slug=statistics.game.slug,
    )
    context_key = (
        f"{statistics.game.locale}:{statistics.game.slug}:"
        f"{team.reference.team_id}:{round_number}:"
        f"{team_snapshot.generated_at.isoformat()}"
    )
    st.badge(
        profile.label if profile.known else f"Uverificerede standardregler · {profile.label}",
        color="green" if profile.known else "orange",
        icon=":material/rule:",
    )
    st.caption(
        f"{team.team_name} · holdrunde {round_number} · spillerrunde "
        f"{statistics.round_number}"
    )
    roster = team.roster
    roster_labels = {
        entry.player_id: (
            f"{entry.name} · {entry.position} · {format_integer(entry.value)}"
        )
        for entry in roster
    }
    sold = st.multiselect(
        "Sælg",
        tuple(roster_labels),
        format_func=lambda player_id: roster_labels[player_id],
        key=f"transfer-sold:{context_key}",
    )
    kept_ids = {
        item.player_id for item in roster if item.player_id not in set(sold)
    }
    available = tuple(
        entry
        for entry in statistics.entries
        if (
            entry.entry_id
            if entry.entry_id is not None
            else entry.source_index
        )
        not in kept_ids
    )
    available_labels = {
        (
            entry.entry_id if entry.entry_id is not None else entry.source_index
        ): (
            f"{entry.name} · {entry.team} · {entry.position} · "
            f"{format_integer(entry.value)}"
        )
        for entry in available
    }
    bought = st.multiselect(
        f"Køb ({len(sold)} plads(er) ledige)",
        tuple(available_labels),
        max_selections=len(sold) if sold else None,
        disabled=not sold,
        format_func=lambda player_id: available_labels[player_id],
        key=f"transfer-bought:{context_key}",
    )
    actual_contracts = team.overview.substitutions_remaining
    presets = ["Snapshot"] if actual_contracts is not None else []
    presets.extend(("Basis", "Guld", "Manuel"))
    preset = st.segmented_control(
        "Kontrakter",
        presets,
        default=presets[0],
        key=f"transfer-contracts:{context_key}",
    )
    if preset == "Snapshot":
        contracts = actual_contracts
    elif preset == "Basis":
        contracts = profile.base_contracts
    elif preset == "Guld":
        contracts = profile.gold_contracts
    else:
        contracts = int(
            st.number_input(
                "Resterende kontrakter",
                min_value=0,
                value=0,
                step=1,
                key=f"transfer-manual-contracts:{context_key}",
            )
        )
    target_round = int(
        st.number_input(
            "Runde for handlerne",
            min_value=1,
            value=max(1, round_number),
            step=1,
            key=f"transfer-target-round:{context_key}",
        )
    )
    if st.button(
        "Simulér scenarie",
        type="primary",
        icon=":material/science:",
        key=f"simulate-transfers:{context_key}",
    ):
        scenario = TransferScenario(
            initial_roster=roster,
            available_players=statistics.entries,
            sold_player_ids=tuple(sold),
            bought_player_ids=tuple(bought),
            starting_bank=team.overview.bank,
            contracts_remaining=(
                None if contracts is None else int(contracts)
            ),
            target_round=target_round,
            team_round=round_number,
            player_round=statistics.round_number,
            team_round_status=team_round_status,
            player_round_status=statistics.round_status,
        )
        st.session_state[f"transfer-scenario:{context_key}"] = scenario
        st.session_state[f"transfer-result:{context_key}"] = simulate_transfers(
            profile, scenario
        )
    result = st.session_state.get(f"transfer-result:{context_key}")
    if result is None:
        st.info("Vælg salg og køb, og simulér scenariet.")
        return
    with st.container(horizontal=True):
        st.metric(
            "Bank efter handler",
            "Ikke relevant"
            if result.ending_bank is None
            else format_integer(result.ending_bank),
            border=True,
        )
        st.metric("Købsgebyr", format_integer(result.transfer_fee), border=True)
        st.metric("Kontrakter brugt", result.contracts_used, border=True)
        st.metric(
            "Kontrakter tilbage",
            "Ukendt"
            if result.contracts_remaining is None
            else result.contracts_remaining,
            border=True,
        )
    if result.certainty == "preliminary":
        st.warning(
            "Foreløbig simulation: mindst én datakilde er fra en igangværende "
            "eller ubekræftet runde. Hent data igen efter rundens afslutning."
        )
    elif result.certainty == "unverified":
        st.warning(
            "Kan ikke valideres endeligt pÅ grund af datarunder eller ukendte regler."
        )
    if result.status == "valid" and result.certainty == "final":
        st.success("Scenariet overholder regelprofilen med afsluttede rundedata.")
    elif result.status == "valid":
        st.info("Scenariet overholder de kendte regler, men resultatet er ikke endeligt.")
    elif result.status == "unverified":
        st.warning("Reglerne kan ikke valideres for dette format.")
    else:
        for error in result.errors:
            st.error(error)
    for warning in result.warnings:
        st.warning(warning)
    dataframe(
        [
            {
                "Spiller": item.name,
                "Hold": item.team,
                "Position": item.position,
                "Værdi": item.value,
                "Kilde": "Køb" if item.source == "purchase" else "Beholdt",
            }
            for item in result.ending_roster
        ],
        hide_index=True,
        key=(
            f"transfer-lab:{team.reference.game.locale}:"
            f"{team.reference.game.slug}:{team.reference.team_id}:roster"
        ),
    )


def player_compare_panel(
    game: GameUrl,
    players: PlayerStatisticsIndex,
    paths: AppPaths,
    round_number: int,
) -> None:
    snapshot = players.newest(game, round_number)
    if snapshot is None:
        st.info(f"Der findes intet spillersnapshot for runde {round_number}.")
        return
    entries = snapshot.statistics.entries
    keyed = {player_identity(game, entry): entry for entry in entries}
    if len(keyed) < 2:
        st.info("Der skal være mindst to spillere i snapshottet.")
        return
    store = HubSettingsStore(paths.hub_settings_file)
    try:
        settings = store.load()
    except Exception as exc:
        st.error(str(exc))
        settings = HubSettings()
    watched = [
        item.player_key
        for item in settings.watchlist
        if item.game_identity == (game.locale.casefold(), game.slug)
        and item.player_key in keyed
    ]
    defaults = watched[:5]
    if len(defaults) < 2:
        defaults = list(keyed)[:2]
    scope = f"{game.locale}:{game.slug}:{round_number}"
    selected = st.multiselect(
        "Vælg 2-5 spillere",
        tuple(keyed),
        default=defaults,
        max_selections=5,
        format_func=lambda key: f"{keyed[key].name} · {keyed[key].team}",
        key=f"compare-players:{scope}",
    )
    if len(selected) < 2:
        st.info("Vælg mindst to spillere for at sammenligne dem.")
        return
    scouting = {
        item.player_key: item for item in build_scouting_metrics(players, game)
    }
    dataframe(
        [
            {
                "Spiller": keyed[key].name,
                "Hold": keyed[key].team,
                "Position": keyed[key].position,
                "Pris": keyed[key].value,
                "Total vækst": keyed[key].total_growth,
                "Rundevækst": keyed[key].round_growth,
                "Form 3": scouting[key].form_3 if key in scouting else None,
                "Form 3-percentil": (
                    scouting[key].metric("form_3").percentile
                    if key in scouting
                    else None
                ),
                "Prispercentil": (
                    scouting[key].metric("value").percentile
                    if key in scouting
                    else None
                ),
                "Status": _status_labels(keyed[key]),
                "Snapshotalder": format_relative_precise(snapshot.generated_at),
                "Sikkerhed": data_status_label(
                    "final"
                    if snapshot.statistics.round_status == "complete"
                    else "preliminary"
                ),
            }
            for key in selected
        ],
        hide_index=True,
        key=f"player-compare:{game.locale}:{game.slug}:selection",
    )
    history = build_player_history(players, game, tuple(selected))
    frame = pd.DataFrame(
        {
            "Runde": item.round_number,
            "Spiller": item.name,
            "Pris": item.value,
            "Rundevækst": item.round_growth,
            "Total vækst": item.total_growth,
        }
        for item in history
    )
    chart_tabs = st.tabs(
        ("Pris", "Rundevækst", "Total vækst"),
        key=f"compare-history-tabs:{scope}",
        on_change="rerun",
    )
    for tab, column in zip(
        chart_tabs, ("Pris", "Rundevækst", "Total vækst")
    ):
        if tab.open:
            with tab:
                st.line_chart(frame, x="Runde", y=column, color="Spiller")


def player_changes_panel(
    game: GameUrl,
    players: PlayerStatisticsIndex,
    teams: SnapshotIndex,
    round_number: int,
) -> None:
    mode = st.segmented_control(
        "Sammenlign",
        ("Mellem hentninger", "Mellem runder"),
        default="Mellem runder",
        key=f"player-change-mode:{game.locale}:{game.slug}",
    )
    intra = build_intra_round_diff(players, game) if mode == "Mellem hentninger" else None
    diff = (
        None
        if mode == "Mellem hentninger" and intra is None
        else intra.diff
        if intra is not None
        else compare_round_snapshots(players, teams, game, round_number)
    )
    if diff is None:
        st.info(
            "Der mangler to cachede hentninger i den valgte sammenligning."
        )
        return
    if not diff.is_final:
        st.warning(
            "Foreløbig sammenligning: mindst én af runderne er ikke afsluttet "
            "og bekræftet ved en ny hentning."
        )
    st.caption(
        f"Runde {diff.previous_round} ({_time(diff.previous_generated_at)}) · "
        f"runde {diff.current_round} ({_time(diff.current_generated_at)})"
        + (" · samme runde" if intra is not None and intra.same_round else "")
    )
    with st.container(horizontal=True):
        st.metric("PrisÅndringer", len(diff.price_changes), border=True)
        st.metric("StatusÅndringer", len(diff.status_changes), border=True)
        st.metric("Nye spillere", len(diff.added_players), border=True)
        st.metric("Fjernede spillere", len(diff.removed_players), border=True)

    def rows(items):
        return [
            {
                "Spiller": item.name,
                "Hold": item.team,
                "Position": item.position,
                "Fra pris": item.old_value,
                "Til pris": item.new_value,
                "Fra status": ", ".join(item.old_statuses) or "Aktiv",
                "Til status": ", ".join(item.new_statuses) or "Aktiv",
            }
            for item in items
        ]

    for label, items in (
        ("PrisÅndringer", diff.price_changes),
        ("StatusÅndringer", diff.status_changes),
        ("Nye spillere", diff.added_players),
        ("Fjernede spillere", diff.removed_players),
    ):
        expander = st.expander(
            f"{label} · {len(items)}",
            key=f"player-change:{game.slug}:{round_number}:{label}",
            on_change="rerun",
        )
        if expander.open:
            with expander:
                if items:
                    dataframe(
                        rows(items),
                        hide_index=True,
                        key=(
                            f"player-changes:{game.locale}:{game.slug}:"
                            f"{label}:v1"
                        ),
                    )
                else:
                    st.caption("Ingen ændringer.")


def history_panel(
    game: GameUrl,
    teams: SnapshotIndex,
    team_ids: tuple[int, ...],
    *,
    group: GroupDefinition | None = None,
    scope: str,
) -> None:
    history = build_history_series(
        teams,
        game,
        team_ids,
        group=group,
    )
    if not history:
        st.info("Der findes endnu ingen rundehistorik for det valgte udsnit.")
        return
    frame = pd.DataFrame(
        {
            "Runde": item.round_number,
            "Hold": item.team_name,
            "Værdi/point": item.total,
            "Rundevækst": item.round_growth,
            "Samlet placering": item.overall_rank,
            "Grupperang": item.group_rank,
        }
        for item in history
    )
    labels = ["Værdi/point", "Rundevækst", "Samlet placering"]
    if group is not None:
        labels.append("Grupperang")
    tabs = st.tabs(
        labels,
        key=f"history-tabs:{scope}",
        on_change="rerun",
    )
    for tab, column in zip(tabs, labels):
        if not tab.open:
            continue
        with tab:
            chart = alt.Chart(frame).mark_line(point=True).encode(
                x=alt.X("Runde:Q", title="Runde"),
                y=alt.Y(
                    f"{column}:Q",
                    title=column,
                    scale=alt.Scale(reverse=column.endswith("rang")),
                ),
                color=alt.Color("Hold:N", title="Hold"),
                tooltip=("Runde:Q", "Hold:N", f"{column}:Q"),
            )
            st.altair_chart(chart)


def game_history_panel(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
) -> None:
    game_groups = tuple(
        group for group in groups if (
            group.game.locale.casefold(), group.game.slug
        ) == manager_game.identity
    )
    selected_group = st.selectbox(
        "Gruppefilter",
        (None, *game_groups),
        format_func=lambda item: "Alle grupper" if item is None else item.name,
        key=f"game-history-group:{manager_game.game.locale}:{manager_game.game.slug}",
    )
    source_groups = game_groups if selected_group is None else (selected_group,)
    labels: dict[int, str] = {}
    for group in source_groups:
        for member in group.teams:
            labels.setdefault(member.team_id, member.name)
    if not labels:
        st.info("Managerspillet har ingen hold med historik endnu.")
        return
    selected_ids = st.multiselect(
        "Hold",
        tuple(labels),
        default=tuple(labels)[: min(3, len(labels))],
        format_func=lambda team_id: labels[team_id],
        key=f"game-history-teams:{manager_game.game.locale}:{manager_game.game.slug}",
    )
    if not selected_ids:
        st.info("Vælg mindst ét hold.")
        return
    history_panel(
        manager_game.game,
        teams,
        tuple(selected_ids),
        group=selected_group,
        scope=f"game:{manager_game.game.locale}:{manager_game.game.slug}",
    )


def group_history_panel(
    group: GroupDefinition,
    teams: SnapshotIndex,
) -> None:
    labels = {member.team_id: member.name for member in group.teams}
    selected_ids = st.multiselect(
        "Hold",
        tuple(labels),
        default=tuple(labels)[: min(3, len(labels))],
        format_func=lambda team_id: labels[team_id],
        key=f"group-history-teams:{group.group_id}",
    )
    if not selected_ids:
        st.info("Vælg mindst ét hold.")
        return
    history_panel(
        group.game,
        teams,
        tuple(selected_ids),
        group=group,
        scope=f"group:{group.group_id}",
    )


def team_changes_panel(
    teams: SnapshotIndex,
    game: GameUrl,
    team_id: int,
    round_number: int,
) -> None:
    diff = compare_team_rounds(teams, game, team_id, round_number)
    if diff is None:
        st.info(
            "Der findes ikke bÅde den valgte og en tidligere cachet runde "
            "for holdet."
        )
        return
    if not diff.is_final:
        st.warning(
            "Foreløbig sammenligning: mindst én runde er i gang eller "
            "endnu ikke bekræftet ved genhentning."
        )
    st.caption(
        f"Runde {diff.previous_round} ({_time(diff.previous_generated_at)}) · "
        f"runde {diff.current_round} ({_time(diff.current_generated_at)})"
    )
    with st.container(horizontal=True):
        st.metric(
            "Total",
            format_integer(diff.new_total or 0),
            delta=(
                None
                if diff.old_total is None or diff.new_total is None
                else format_integer(diff.new_total - diff.old_total)
            ),
            border=True,
        )
        st.metric(
            "Spillerværdi",
            "-"
            if diff.new_player_value is None
            else format_integer(diff.new_player_value),
            delta=(
                None
                if diff.old_player_value is None or diff.new_player_value is None
                else format_integer(diff.new_player_value - diff.old_player_value)
            ),
            border=True,
        )
        st.metric(
            "Samlet placering",
            "-" if diff.new_rank is None else format_integer(diff.new_rank),
            delta=diff.rank_movement,
            border=True,
        )
    if diff.roster_comparable:
        left, right = st.columns(2)
        with left:
            st.markdown("**Nye pÅ holdet**")
            st.write(list(diff.added_players) or ["Ingen"])
        with right:
            st.markdown("**Ude af holdet**")
            st.write(list(diff.removed_players) or ["Ingen"])
    else:
        st.caption(
            "Holdopstilling er ikke gemt i begge runder; medlemsÅndringer kan "
            "derfor ikke sammenlignes."
        )


def data_quality_panel(
    games: tuple[ManagerGame, ...],
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    paths: AppPaths,
) -> None:
    include_archived = st.toggle(
        "Medtag arkiverede managerspil",
        value=False,
        key="data-quality-archived",
    )
    players = _player_index(paths)
    report = build_data_quality_report(
        games,
        groups,
        teams,
        players,
        manifest_dir=paths.manifest_dir,
        include_archived=include_archived,
    )
    with st.container(horizontal=True):
        st.metric("Nyeste holdcache", _age(report.newest_team_data), border=True)
        st.metric("Nyeste spillercache", _age(report.newest_player_data), border=True)
        st.metric("Seneste succes", _time(report.last_success), border=True)
        st.metric("Seneste fejl", _time(report.last_error), border=True)
    selected_games = tuple(
        game for game in games if include_archived or not game.is_archived
    )
    if not selected_games:
        st.info("Der er ingen managerspil at analysere.")
        return
    status_labels = {
        "ready": (data_status_label("ready"), "green", ":material/check_circle:"),
        "preliminary": (
            data_status_label("preliminary"),
            "orange",
            ":material/schedule:",
        ),
        "missing": (data_status_label("missing"), "red", ":material/warning:"),
        "error": (data_status_label("error"), "red", ":material/error:"),
    }
    for manager_game in selected_games:
        rows = tuple(
            row
            for row in report.rounds
            if (row.game_locale, row.game_slug) == manager_game.identity
        )
        latest = rows[0] if rows else None
        with st.container(border=True):
            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
            ):
                st.subheader(manager_game.name)
                readiness = "missing" if latest is None else latest.readiness
                label, color, icon = status_labels[readiness]
                st.badge(label, color=color, icon=icon)
            if latest is None:
                st.caption("Ingen cachede runder. Åbn Rundecenter for at opdatere manuelt.")
            else:
                with st.container(horizontal=True):
                    st.metric("Runde", latest.round_number)
                    st.metric(
                        "Holddækning",
                        f"{latest.team_snapshots}/{latest.expected_teams}",
                    )
                    st.metric(
                        "Spillere",
                        data_status_label("ready" if latest.player_snapshot else "missing"),
                    )
                    st.metric("Holdcache", _age(latest.newest_team_data))
                    st.metric("Spillercache", _age(latest.newest_player_data))
                if latest.reasons:
                    st.caption(" · ".join(latest.reasons))
                if latest.missing_team_names:
                    st.warning(
                        "Mangler hold: " + ", ".join(latest.missing_team_names)
                    )
                if latest.last_error_message:
                    st.error(latest.last_error_message)
            page_link(
                PageId.GAME,
                "Åbn Rundecenter",
                icon=":material/open_in_new:",
                locale=manager_game.game.locale,
                game=manager_game.game.slug,
                section="round-center",
            )
            if len(rows) > 1:
                expander = st.expander(
                    "Tidligere runder",
                    key=(
                        f"quality-history:{manager_game.game.locale}:"
                        f"{manager_game.game.slug}"
                    ),
                    on_change="rerun",
                )
                if expander.open:
                    with expander:
                        dataframe(
                            [
                                {
                                    "Runde": row.round_number,
                                    "Status": status_labels[row.readiness][0],
                                    "Hold": (
                                        f"{row.team_snapshots}/"
                                        f"{row.expected_teams}"
                                    ),
                                    "Spillere": (
                                        data_status_label(
                                            "ready" if row.player_snapshot else "missing"
                                        )
                                    ),
                                    "Mangler": (
                                        ", ".join(row.missing_team_names) or "–"
                                    ),
                                }
                                for row in rows[1:]
                            ],
                            hide_index=True,
                            key=(
                                f"data-quality:{manager_game.game.locale}:"
                                f"{manager_game.game.slug}:history"
                            ),
                        )


def _identity_options(teams: SnapshotIndex) -> dict[str, str]:
    result: dict[str, str] = {}
    for snapshot in teams.snapshots:
        team = snapshot.team
        keys = manager_identity_keys(
            owner_user_id=team.owner_user_id,
            account_user_id=team.reference.account_user_id,
            account_key=team.reference.account_key,
            owner_name=team.owner_name,
        )
        for key in keys:
            result.setdefault(key, f"{team.owner_name} · {key}")
    return result


def legacy_manager_view(
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    paths: AppPaths,
) -> None:
    st.title("Managers", anchor="managers")
    settings_store = HubSettingsStore(paths.hub_settings_file)
    try:
        settings = settings_store.load()
    except Exception as exc:
        st.error(str(exc))
        settings = HubSettings()
    metadata, _ = GameMetadataStore(paths.game_metadata_dir).scan()
    final_rounds = {
        item.identity: item.final_round
        for item in metadata if item.final_round is not None
    }
    live = build_live_hall_of_fame_events(
        groups,
        teams,
        settings,
        final_rounds=final_rounds,
    )
    frozen, warnings = HallOfFameStore(paths.hall_of_fame_dir).scan()
    board = build_hall_of_fame(frozen, settings.hall_of_fame_score)
    with st.container(horizontal=True):
        st.metric("Frosne resultater", len(frozen), border=True)
        st.metric(
            "Aktuelle resultater klar",
            sum(item.complete for item in live),
            border=True,
        )
        st.metric(
            "Afventer komplette data",
            sum(not item.complete for item in live),
            border=True,
        )
    if board.rows:
        dataframe(
            [
                {
                    "Rang": row.rank,
                    "Manager": row.manager_name,
                    "Point": row.points,
                    "Titler": row.titles,
                    "Podier": row.podiums,
                    "Sejrsrate": row.win_rate,
                    "Bedste runde": row.best_round,
                    "Længste streak": row.longest_round_win_streak,
                }
                for row in board.rows
            ],
            hide_index=True,
            column_config={
                "Sejrsrate": st.column_config.NumberColumn(format="percent")
            },
            key="legacy-managers:ranking",
        )
    else:
        st.info(
            "Ingen komplette resultater er frosset endnu. En foreløbig visning findes nedenfor."
        )
    preview = build_hall_of_fame(
        tuple((*frozen, *(item for item in live if item.event_id not in {event.event_id for event in frozen}))),
        settings.hall_of_fame_score,
        include_incomplete=True,
    )
    with st.expander("Foreløbig visning", on_change="rerun") as exp:
        if exp.open:
            if preview.rows:
                dataframe(
                    [
                        {
                            "Manager": row.manager_name,
                            "Point hvis komplet": row.points,
                            "Titler": row.titles,
                            "Podier": row.podiums,
                        }
                        for row in preview.rows
                    ],
                    hide_index=True,
                    key="legacy-managers:preview-ranking",
                )
            dataframe(
                [
                    {
                        "Konkurrence": item.competition_name,
                        "Type": item.kind,
                        "Runde": item.round_number,
                        "Status": data_status_label(
                            "complete" if item.complete else "preliminary"
                        ),
                    }
                    for item in live
                ],
                hide_index=True,
                key="legacy-managers:live-events",
            )
    for warning in warnings:
        st.warning(warning)

    st.subheader("Pointprofil")
    score = settings.hall_of_fame_score
    with st.form("hall-of-fame-score"):
        group_points = [
            st.number_input(
                f"Gruppespil · plads {position}",
                min_value=0,
                value=value,
                step=1,
            )
            for position, value in enumerate(score.group_points, 1)
        ]
        tournament_winner = st.number_input(
            "Turneringsvinder", min_value=0, value=score.tournament_winner, step=1
        )
        tournament_finalist = st.number_input(
            "Finalist", min_value=0, value=score.tournament_finalist, step=1
        )
        tournament_semifinalist = st.number_input(
            "Tabende semifinalist",
            min_value=0,
            value=score.tournament_semifinalist,
            step=1,
        )
        round_win = st.number_input(
            "Global rundesejr", min_value=0, value=score.global_round_win, step=1
        )
        save_score = st.form_submit_button("Gem pointprofil")
    if save_score:
        updated_score = HallOfFameScoreProfile(
            tuple(int(value) for value in group_points),
            int(tournament_winner),
            int(tournament_finalist),
            int(tournament_semifinalist),
            int(round_win),
        )
        settings_store.save(replace(settings, hall_of_fame_score=updated_score))
        st.rerun()

    options = _identity_options(teams)
    with st.expander("Saml manageraliaser", on_change="rerun") as exp:
        if exp.open:
            with st.form("manager-alias"):
                selected_keys = st.multiselect(
                    "Identiteter, der er samme manager",
                    tuple(options),
                    format_func=lambda key: options[key],
                )
                display_name = st.text_input("Fælles managernavn")
                save_alias = st.form_submit_button("Gem alias")
            if save_alias:
                if not display_name.strip() or not selected_keys:
                    st.error("Vælg identiteter og skriv et navn.")
                else:
                    canonical = "alias:" + hashlib.sha256(
                        "|".join(sorted(selected_keys)).encode("utf-8")
                    ).hexdigest()[:16]
                    retained = tuple(
                        alias for alias in settings.manager_aliases
                        if not set(alias.identity_keys).intersection(selected_keys)
                    )
                    alias = ManagerAlias(
                        canonical, display_name.strip(), tuple(selected_keys)
                    )
                    settings_store.save(
                        replace(settings, manager_aliases=(*retained, alias))
                    )
                    st.rerun()


def backup_view(paths: AppPaths) -> None:
    st.subheader("Backup og gendannelse", anchor="backup-og-gendannelse")
    st.caption(
        "Backup indeholder kun kanonisk konfiguration, snapshots, manifester, "
        "revisioner, metadata og Hall of Fame-ledger - ikke afledte eksporter."
    )
    data, manifest = create_backup_bytes(paths)
    st.download_button(
        "Download hele Hubben som ZIP",
        data,
        file_name=f"holdet-hub-{manifest.created_at.strftime('%Y%m%d-%H%M%S')}.zip",
        mime="application/zip",
        icon=":material/download:",
        type="primary",
    )
    with st.container(horizontal=True):
        st.metric("Filer", len(manifest.files), border=True)
        st.metric("Størrelse", f"{manifest.total_bytes / 1024:.1f} KB", border=True)

    uploaded = st.file_uploader(
        "Vælg backup-ZIP til forhåndsvisning",
        type=("zip",),
        key="restore-upload",
    )
    if uploaded is None:
        return
    raw = uploaded.getvalue()
    validation = validate_backup(raw)
    if not validation.is_valid or validation.manifest is None:
        for error in validation.errors:
            st.error(error)
        return
    st.success("Hele arkivet er valideret: stier, skemaer, størrelser og kontrolsummer.")
    dataframe(
        [
            {"Fil": item.path, "Bytes": item.size, "SHA-256": item.sha256}
            for item in validation.manifest.files
        ],
        hide_index=True,
        key="backup:manifest",
    )
    confirm = st.checkbox(
        "Jeg forstår, at aktiv konfiguration og aktive data erstattes",
        key="restore-confirm",
    )
    if st.button(
        "Gendan valideret backup",
        type="primary",
        icon=":material/restore:",
        disabled=not confirm,
    ):
        try:
            result = restore_backup(raw, paths)
        except Exception as exc:
            st.error(str(exc))
        else:
            st.cache_data.clear()
            st.success(
                f"{result.restored_files} filer blev gendannet. "
                f"Rollback: {result.rollback_path.name}"
            )
            st.rerun()


def _score(value: int | None) -> str:
    return "-" if value is None else format_integer(value)


def render_tournament_bracket(
    group: GroupDefinition,
    state: TournamentState,
) -> None:
    """Render a responsive, sanitized CSS-grid bracket without JavaScript."""

    assert group.tournament is not None
    if group.tournament.template == "double_elimination":
        bracket = build_double_elimination_bracket(len(group.teams))
        seed_names = {
            seed: next(
                (
                    member.name
                    for member in group.teams
                    if member.team_id == team_id
                ),
                f"Seed {seed}",
            )
            for seed, team_id in enumerate(group.tournament.seed_order, 1)
        }
        dataframe(
            [
                {
                    "Kamp": item.match_id,
                    "Bracket": item.bracket,
                    "Bracket-runde": item.bracket_round,
                    "Plads A": seed_names.get(item.team_a_seed, item.source_a or "Bye"),
                    "Plads B": seed_names.get(item.team_b_seed, item.source_b or "Bye"),
                    "Reset-finale": item.reset_final,
                }
                for item in bracket
            ],
            hide_index=True,
            width="stretch",
            key=f"tournament:{group.group_id}:double-elimination-bracket",
        )
        st.caption("GF2 spilles kun, hvis taberbracket-vinderen vinder GF1.")
        return
    names = {row.team_id: row.team_name for row in state.standings}
    for member in group.teams:
        names.setdefault(member.team_id, member.name)
    team_ids = tuple(item.team_id for item in group.teams)
    selected = st.selectbox(
        "Fremhæv vej til finalen",
        (None, *team_ids),
        format_func=lambda team_id: "Ingen fremhævning" if team_id is None else names[team_id],
        key=f"bracket-highlight-{group.group_id}",
    )

    stage_names: list[str] = []
    size = group.tournament.knockout_size
    while size >= 2:
        stage_names.append(STAGE_NAMES[size])
        size //= 2
    matches_by_stage: dict[str, list[KnockoutMatch | None]] = {
        stage: [] for stage in stage_names
    }
    for match in state.knockout_matches:
        matches_by_stage.setdefault(match.stage, []).append(match)

    provisional = not bool(state.knockout_matches)
    if provisional:
        qualified = list(state.standings[: group.tournament.knockout_size])
        order = bracket_seed_order(group.tournament.knockout_size)
        seed_map = {row.rank: row for row in qualified}
        first = stage_names[0]
        for index in range(0, len(order), 2):
            a = seed_map.get(order[index])
            b = seed_map.get(order[index + 1])
            matches_by_stage[first].append(
                KnockoutMatch(
                    first,
                    index // 2 + 1,
                    (),
                    None if a is None else a.team_id,
                    None if b is None else b.team_id,
                    None if a is None else a.team_name,
                    None if b is None else b.team_name,
                    None if a is None else a.rank,
                    None if b is None else b.rank,
                    None,
                    None,
                    None,
                    False,
                )
            )
    expected = group.tournament.knockout_size // 2
    for stage in stage_names:
        current = matches_by_stage.setdefault(stage, [])
        while len(current) < expected:
            current.append(None)
        expected = max(1, expected // 2)

    columns: list[str] = []
    for stage in stage_names:
        cards: list[str] = []
        for index, match in enumerate(matches_by_stage[stage], 1):
            if match is None:
                a_name = b_name = "Afventer vinder"
                a_seed = b_seed = None
                a_score = b_score = "-"
                winner = None
                round_label = "Kommende"
                highlight = False
            else:
                a_name = match.team_a_name or "Afventer"
                b_name = match.team_b_name or "Afventer"
                a_seed = match.team_a_seed
                b_seed = match.team_b_seed
                a_score = _score(match.team_a_change)
                b_score = _score(match.team_b_change)
                winner = match.winner_id
                round_label = (
                    "Foreløbig seedning"
                    if provisional and not match.round_numbers
                    else "Runde " + (
                        str(match.round_numbers[0])
                        if len(match.round_numbers) == 1
                        else f"{match.round_numbers[0]}-{match.round_numbers[-1]}"
                    )
                )
                highlight = selected is not None and selected in {
                    match.team_a_id, match.team_b_id, match.winner_id
                }
            def participant(name, seed, score, is_winner):
                seed_html = f'<span class="seed">{seed}</span>' if seed is not None else '<span class="seed">-</span>'
                winner_class = " winner" if is_winner else ""
                return (
                    f'<div class="participant{winner_class}">{seed_html}'
                    f'<span class="name">{escape(name)}</span>'
                    f'<strong>{escape(score)}</strong></div>'
                )
            cards.append(
                f'<article class="match{" highlight" if highlight else ""}">'
                f'<div class="round">{escape(round_label)}</div>'
                f'{participant(a_name, a_seed, a_score, winner is not None and match is not None and winner == match.team_a_id)}'
                f'{participant(b_name, b_seed, b_score, winner is not None and match is not None and winner == match.team_b_id)}'
                f'</article>'
            )
        columns.append(
            f'<section class="stage"><h3>{escape(stage)}</h3>'
            + "".join(cards)
            + "</section>"
        )
    provisional_text = (
        '<p class="provisional">Foreløbige seeds - gruppespillet er ikke færdigt.</p>'
        if provisional else ""
    )
    html = f"""
    <style>
      .bracket-wrap {{overflow-x:auto;padding:.25rem 0 1rem}}
      .bracket {{display:grid;grid-template-columns:repeat({len(stage_names)},minmax(250px,1fr));gap:1rem;min-width:{max(720, len(stage_names)*260)}px;align-items:stretch}}
      .stage {{display:flex;flex-direction:column;justify-content:space-around;gap:1rem}}
      .stage h3 {{font-size:1rem;margin:.1rem 0 .35rem;color:var(--text-color)}}
      .match {{border:1px solid rgba(128,128,128,.35);border-radius:12px;padding:.55rem;background:rgba(128,128,128,.06);box-shadow:0 4px 14px rgba(0,0,0,.08)}}
      .match.highlight {{border:2px solid #ff4b4b;background:rgba(255,75,75,.10)}}
      .round,.provisional {{font-size:.78rem;opacity:.72;margin:0 0 .35rem}}
      .participant {{display:grid;grid-template-columns:1.5rem 1fr auto;gap:.45rem;align-items:center;padding:.4rem;border-radius:8px}}
      .participant+.participant {{border-top:1px solid rgba(128,128,128,.2)}}
      .participant.winner {{font-weight:700;background:rgba(46,160,67,.14)}}
      .seed {{opacity:.65;text-align:center}} .name {{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    </style>
    {provisional_text}
    <div class="bracket-wrap"><div class="bracket">{"".join(columns)}</div></div>
    """
    st.html(html)




def _manager_settings(paths: AppPaths) -> tuple[HubSettingsStore, HubSettings]:
    store = HubSettingsStore(paths.hub_settings_file)
    try:
        return store, store.load()
    except Exception as exc:
        st.warning(f"Managerindstillinger kunne ikke l\u00e6ses: {exc}")
        return store, HubSettings()


@st.cache_data(max_entries=8, show_spinner=False)
def _cached_manager_live_events(groups, teams, settings, final_rounds):
    return build_live_hall_of_fame_events(
        groups,
        teams,
        settings,
        final_rounds=final_rounds,
    )


@st.cache_data(max_entries=8, show_spinner=False)
def _cached_manager_round_results(groups, teams, settings):
    return build_manager_round_results(groups, teams, settings)


@st.cache_data(max_entries=8, show_spinner=False)
def _cached_manager_ratings(groups, teams, settings):
    return build_manager_ratings(groups, teams, settings)


@st.cache_data(max_entries=8, show_spinner=False)
def _cached_manager_board(frozen, settings):
    return build_hall_of_fame(
        frozen,
        settings.hall_of_fame_score,
        settings=settings,
    )


@st.cache_data(max_entries=8, show_spinner=False)
def _cached_manager_careers(frozen, round_results):
    return build_manager_careers(frozen, round_results)


def _manager_tabs():
    labels = (
        "Rangliste",
        "Medaljer og rekorder",
        "Sammenlign",
        "Sæsoner",
        "Identiteter",
    )
    slugs = (
        "ranking",
        "records",
        "compare",
        "seasons",
        "identities",
    )
    key = "manager-tabs"
    requested = str(st.query_params.get("section", ""))
    desired = (
        labels[slugs.index(requested)]
        if requested in slugs
        else labels[0]
    )
    if (
        requested in slugs
        or key not in st.session_state
        or st.session_state[key] not in labels
    ):
        st.session_state[key] = desired

    def sync_tab() -> None:
        selected = st.session_state[key]
        st.query_params["section"] = slugs[labels.index(selected)]

    return st.tabs(
        labels,
        default=st.session_state[key],
        key=key,
        on_change=sync_tab,
    )


@st.fragment
def managers_view(
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    paths: AppPaths,
) -> None:
    """Render the consolidated manager center."""

    st.title("Managers", anchor="managers")
    manager_slot = st.container()
    with manager_slot:
        with st.skeleton(height=140):
            settings_store, settings = _manager_settings(paths)
            settings = build_effective_manager_settings(settings, groups, teams)
            metadata, metadata_warnings = GameMetadataStore(
                paths.game_metadata_dir
            ).scan()
            final_rounds = {
                item.identity: item.final_round
                for item in metadata
                if item.final_round is not None
            }
            live = _cached_manager_live_events(
                groups, teams, settings, final_rounds
            )
            frozen, ledger_warnings = HallOfFameStore(
                paths.hall_of_fame_dir
            ).scan()
            frozen = remap_manager_events(frozen, settings)
            round_results = _cached_manager_round_results(groups, teams, settings)
            profiles = effective_manager_profiles(settings)

    with st.container(horizontal=True):
        st.metric("Managerprofiler", len(profiles), border=True)
        st.metric(
            "Ratingperioder",
            len({
                (item.game_locale, item.game_slug, item.round_number)
                for item in round_results
            }),
            border=True,
        )
        st.metric("Frosne resultater", len(frozen), border=True)
        st.metric("Aktuelle resultater", sum(item.complete for item in live), border=True)
    if st.button(
        "Genopbyg historik fra cache",
        icon=":material/history:",
        help="Publicerer kun komplette cachede resultater; navigationen skriver aldrig.",
    ):
        try:
            published = HallOfFameStore(paths.hall_of_fame_dir).freeze_complete(live)
        except Exception as exc:
            st.error(f"Historikken kunne ikke genopbygges: {exc}")
        else:
            st.success(f"{len(published)} komplette resultater er kontrolleret.")
            st.rerun()


    (
        rank_tab,
        medal_tab,
        compare_tab,
        season_tab,
        identity_tab,
    ) = _manager_tabs()
    if rank_tab.open:
        with rank_tab:
            board = _cached_manager_board(frozen, settings)
            ratings = _cached_manager_ratings(groups, teams, settings)
            rating_by_id = {item.manager_id: item for item in ratings}
            rows = []
            for row in board.rows:
                rating = rating_by_id.get(row.manager_id)
                rows.append(
                    {
                        "Rang": str(row.rank),
                        "Manager": row.manager_name,
                        "Point": row.points,
                        "Elo": format_elo(None if rating is None else rating.rating),
                        "Status": (
                            "Ingen rating"
                            if rating is None
                            else "Forel\u00f8big" if rating.provisional else "Etableret"
                        ),
                        "Perioder": 0 if rating is None else rating.periods,
                        "Titler": row.titles,
                        "Podier": row.podiums,
                    }
                )
            known = {row.manager_id for row in board.rows}
            rows.extend(
                {
                    "Rang": "–",
                    "Manager": rating.manager_name,
                    "Point": 0,
                    "Elo": format_elo(rating.rating),
                    "Status": "Forel\u00f8big" if rating.provisional else "Etableret",
                    "Perioder": rating.periods,
                    "Titler": 0,
                    "Podier": 0,
                }
                for rating in ratings
                if rating.manager_id not in known
            )
            if rows:
                dataframe(
                    rows,
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "Rang": st.column_config.TextColumn(
                            "Rang",
                            help="Placering i Hall of Fame; – betyder endnu ikke placeret.",
                        ),
                        "Elo": st.column_config.TextColumn(
                            "Elo",
                            help="Managerens Elo-rating afrundet til nærmeste heltal.",
                        ),
                    },
                    key="managers:ranking",
                )
            else:
                st.info("Ingen komplette managerresultater endnu.")
            with st.expander("Foreløbig visning", on_change="rerun") as preview:
                if preview.open:
                    dataframe(
                        [
                            {
                                "Konkurrence": item.competition_name,
                                "Runde": item.round_number,
                                "Status": data_status_label(
                                    "complete" if item.complete else "preliminary"
                                ),
                            }
                            for item in live
                        ],
                        hide_index=True,
                        width="stretch",
                        key="managers:ranking-preview",
                    )
            st.subheader("Pointprofil")
            score = settings.hall_of_fame_score
            with st.form("manager-score-profile"):
                group_points = tuple(
                    int(
                        st.number_input(
                            f"Gruppeplacering {position}",
                            min_value=0,
                            value=value,
                            step=1,
                        )
                    )
                    for position, value in enumerate(score.group_points, 1)
                )
                winner = int(st.number_input("Turneringsvinder", min_value=0, value=score.tournament_winner))
                finalist = int(st.number_input("Finalist", min_value=0, value=score.tournament_finalist))
                semifinalist = int(st.number_input("Semifinalist", min_value=0, value=score.tournament_semifinalist))
                round_win = int(st.number_input("Rundesejr", min_value=0, value=score.global_round_win))
                save_score = st.form_submit_button("Gem pointprofil")
            if save_score:
                settings_store.save(
                    replace(
                        settings,
                        hall_of_fame_score=HallOfFameScoreProfile(
                            group_points,
                            winner,
                            finalist,
                            semifinalist,
                            round_win,
                        ),
                    )
                )
                st.rerun()

    if medal_tab.open:
        with medal_tab:
            careers = _cached_manager_careers(frozen, round_results)
            if careers:
                dataframe(
                    [
                        {
                            "Manager": item.manager_name,
                            "Guld": item.gold,
                            "S\u00f8lv": item.silver,
                            "Bronze": item.bronze,
                            "Titler": item.titles,
                            "Podier": item.podiums,
                            "Tr\u00e6skeer": item.wooden_spoons,
                            "Rundesejre": item.round_wins,
                            "Sejrsstreak": item.longest_win_streak,
                            "Runder som nr. 1": item.first_place_rounds,
                            "F\u00f8ringsstreak": item.longest_first_place_streak,
                        }
                        for item in careers
                    ],
                    hide_index=True,
                    width="stretch",
                    key="managers:medals-records",
                )
            else:
                st.info("Medaljer og rekorder vises efter f\u00f8rste komplette event.")

    if compare_tab.open:
        with compare_tab:
            ratings = _cached_manager_ratings(groups, teams, settings)
            careers = _cached_manager_careers(frozen, round_results)
            names = {
                item.manager_id: item.manager_name
                for item in ratings
            }
            names.update({item.manager_id: item.manager_name for item in careers})
            manager_ids = tuple(sorted(names, key=lambda key: (names[key].casefold(), key)))
            if len(manager_ids) < 2:
                st.info("Sammenligning kr\u00e6ver mindst to managers.")
            else:
                requested_manager = str(st.query_params.get("manager", ""))
                requested_opponent = str(st.query_params.get("opponent", ""))
                first_index = manager_ids.index(requested_manager) if requested_manager in manager_ids else 0
                first = st.selectbox(
                    "Manager",
                    manager_ids,
                    index=first_index,
                    format_func=lambda key: names[key],
                    key="manager-compare-first",
                )
                opponents = tuple(item for item in manager_ids if item != first)
                second_index = opponents.index(requested_opponent) if requested_opponent in opponents else 0
                second = st.selectbox(
                    "Modstander",
                    opponents,
                    index=second_index,
                    format_func=lambda key: names[key],
                    key="manager-compare-second",
                )
                game_identities = tuple(
                    sorted(
                        {
                            (group.game.locale.casefold(), group.game.slug)
                            for group in groups
                        }
                    )
                )
                slug_counts = {
                    slug: sum(item[1] == slug for item in game_identities)
                    for _, slug in game_identities
                }
                game_filter = st.selectbox(
                    "Managerspil",
                    (None, *game_identities),
                    format_func=lambda identity: (
                        "Alle managerspil"
                        if identity is None
                        else (
                            identity[1]
                            if slug_counts[identity[1]] == 1
                            else f"{identity[1]} ({identity[0]})"
                        )
                    ),
                    key="manager-compare-game",
                )
                selected_groups = (
                    groups
                    if game_filter is None
                    else tuple(
                        group
                        for group in groups
                        if (
                            group.game.locale.casefold(),
                            group.game.slug,
                        )
                        == game_filter
                    )
                )
                competition_names = {
                    group.group_id: group.name for group in selected_groups
                }
                competition_options = (
                    "Alle konkurrencer",
                    *sorted(competition_names),
                )
                competition_filter = st.selectbox(
                    "Konkurrence",
                    competition_options,
                    format_func=lambda key: competition_names.get(key, key),
                    key="manager-compare-competition",
                )
                try:
                    compare_seasons = SeasonStore(paths.seasons_file).load()
                except Exception as exc:
                    compare_seasons = ()
                    st.warning(f"Sæsonfilteret kunne ikke læses: {exc}")
                season_by_id = {
                    item.season_id: item for item in compare_seasons
                }
                season_options = (
                    "Alle sæsoner",
                    *sorted(
                        season_by_id,
                        key=lambda key: season_by_id[key].name.casefold(),
                    ),
                )
                requested_season = str(st.query_params.get("season", ""))
                season_index = (
                    season_options.index(requested_season)
                    if requested_season in season_options
                    else 0
                )
                season_filter = st.selectbox(
                    "Sæson",
                    season_options,
                    index=season_index,
                    format_func=lambda key: (
                        season_by_id[key].name if key in season_by_id else key
                    ),
                    key="manager-compare-season",
                )
                if competition_filter != "Alle konkurrencer":
                    selected_groups = tuple(
                        group
                        for group in selected_groups
                        if group.group_id == competition_filter
                    )
                if season_filter != "Alle sæsoner":
                    season_competitions = set(
                        season_by_id[season_filter].competition_ids
                    )
                    selected_groups = tuple(
                        group
                        for group in selected_groups
                        if group.group_id in season_competitions
                    )

                h2h = build_manager_head_to_head(first, second, selected_groups, teams, settings)
                official = h2h.summary("official")
                shared = h2h.summary("shared_round")
                with st.container(horizontal=True):
                    st.metric(
                        "Officielle V-U-T",
                        f"{official[0]}-{official[1]}-{official[2]}",
                        border=True,
                    )
                    st.metric(
                        "F\u00e6lles runder V-U-T",
                        f"{shared[0]}-{shared[1]}-{shared[2]}",
                        border=True,
                    )
                    official_growth = h2h.total_growth("official")
                    st.metric(
                        "Officiel samlet v\u00e6kst",
                        f"{official_growth[0]}-{official_growth[1]}",
                        border=True,
                    )
                biggest = h2h.biggest_win("official")
                closest = h2h.closest_meeting("official")
                if biggest is not None:
                    st.caption(
                        "St\u00f8rste sejr: "
                        f"{biggest.manager_score}-{biggest.opponent_score} "
                        f"i {biggest.competition_id}."
                    )
                if closest is not None:
                    st.caption(
                        "N\u00e6rmeste officielle m\u00f8de: "
                        f"{closest.manager_score}-{closest.opponent_score} "
                        f"i {closest.competition_id}."
                    )
                meetings = (*h2h.official, *h2h.shared_rounds)
                dataframe(
                    [
                        {
                            "Spor": "Officiel kamp" if item.track == "official" else "F\u00e6lles grupperunde",
                            "Spil": (
                                f"{item.game_slug} ({item.game_locale})"
                            ),
                            "Konkurrence": item.competition_id,
                            "Runder": ", ".join(map(str, item.round_numbers)),
                            names[first]: item.manager_score,
                            names[second]: item.opponent_score,
                            "Tidspunkt": item.occurred_at,
                        }
                        for item in meetings
                    ],
                    hide_index=True,
                    width="stretch",
                    key=f"managers:comparison:{first}:{second}",
                )

    if season_tab.open:
        with season_tab:
            season_store = SeasonStore(paths.seasons_file)
            try:
                seasons = season_store.load()
            except Exception as exc:
                seasons = ()
                st.warning(f"S\u00e6soner kunne ikke l\u00e6ses: {exc}")
            active = tuple(item for item in seasons if not item.is_archived)
            show_archived = st.checkbox("Vis arkiverede sæsoner", value=False)
            if show_archived:
                active = seasons
            if active:
                requested_season = str(st.query_params.get("season", ""))
                active_ids = tuple(item.season_id for item in active)
                season_index = (
                    active_ids.index(requested_season)
                    if requested_season in active_ids
                    else 0
                )
                season = st.selectbox(
                    "S\u00e6son",
                    active,
                    format_func=lambda item: item.name,
                    index=season_index,
                )
                standings = build_season_standings(
                    season,
                    frozen,
                    settings.hall_of_fame_score,
                )
                dataframe(
                    [
                        {
                            "Rang": item.rank,
                            "Manager": item.manager_name,
                            "Point": item.points,
                            "Titler": item.titles,
                            "Podier": item.podiums,
                            "Rundesejre": item.round_wins,
                        }
                        for item in standings
                    ],
                    hide_index=True,
                    width="stretch",
                    key=f"managers:season:{season.season_id}:standings",
                )
                if not season.is_archived:
                    with st.expander(
                        "Rediger s\u00e6son",
                        on_change="rerun",
                    ) as edit_season:
                        if edit_season.open:
                            competitions = {
                                group.group_id: group.name
                                for group in groups
                            }
                            with st.form(
                                f"edit-season-{season.season_id}"
                            ):
                                edited_name = st.text_input(
                                    "S\u00e6sonnavn",
                                    value=season.name,
                                )
                                edited_competitions = st.multiselect(
                                    "Grupper og turneringer",
                                    tuple(competitions),
                                    default=tuple(
                                        item
                                        for item in season.competition_ids
                                        if item in competitions
                                    ),
                                    format_func=lambda key: competitions[key],
                                )
                                save_season = st.form_submit_button(
                                    "Gem s\u00e6son"
                                )
                            if save_season:
                                try:
                                    season_store.update(
                                        seasons,
                                        season.season_id,
                                        name=edited_name,
                                        competition_ids=tuple(
                                            edited_competitions
                                        ),
                                    )
                                except Exception as exc:
                                    st.error(str(exc))
                                else:
                                    st.rerun()
                if (
                    not season.is_archived
                    and st.button(
                        "Arkivér sæson",
                        key=f"archive-season-{season.season_id}",
                    )
                ):
                    season_store.archive(seasons, season.season_id)
                    st.rerun()
            with st.expander("Opret s\u00e6son", on_change="rerun") as create_expander:
                if create_expander.open:
                    competitions = {group.group_id: group.name for group in groups}
                    with st.form("create-season"):
                        name = st.text_input("Navn")
                        selected = st.multiselect(
                            "Grupper og turneringer",
                            tuple(competitions),
                            format_func=lambda key: competitions[key],
                        )
                        create = st.form_submit_button("Opret s\u00e6son")
                    if create:
                        try:
                            season_store.create(seasons, name, tuple(selected))
                        except Exception as exc:
                            st.error(str(exc))
                        else:
                            st.rerun()

    if identity_tab.open:
        with identity_tab:
            identity_options = _identity_options(teams)
            automatic_profiles = build_effective_manager_settings(
                HubSettings(),
                groups,
                teams,
            ).manager_profiles
            automatic_component = {
                key: frozenset(profile.identity_keys)
                for profile in automatic_profiles
                for key in profile.identity_keys
            }
            if profiles:
                dataframe(
                    [
                        {
                            "Manager": item.display_name,
                            "Manager-ID": item.manager_id,
                            "Identiteter": ", ".join(item.identity_keys),
                            "Manuelle links": ", ".join(
                                item.manual_identity_keys
                            ),
                            "Profiler": ", ".join(item.profile_urls),
                        }
                        for item in profiles
                    ],
                    hide_index=True,
                    width="stretch",
                    key="managers:identities",
                )
            with st.form("manager-profile-merge"):
                target = st.selectbox(
                    "F\u00f8j til profil",
                    (None, *profiles),
                    format_func=lambda item: (
                        "Opret ny profil"
                        if item is None
                        else item.display_name
                    ),
                )
                selected_keys = st.multiselect(
                    "Identiteter for samme person",
                    tuple(identity_options),
                    format_func=lambda key: identity_options[key],
                )
                display_name = st.text_input(
                    "Visningsnavn",
                    value="" if target is None else target.display_name,
                )
                merge = st.form_submit_button("Saml identiteter")
            if merge:
                if not selected_keys or not display_name.strip():
                    st.error(
                        "V\u00e6lg identiteter og skriv et navn."
                    )
                else:
                    selected_anchors = set(selected_keys)
                    selected_components = tuple(
                        automatic_component.get(
                            key,
                            frozenset((key,)),
                        )
                        for key in selected_anchors
                    )
                    selected_set = (
                        set().union(*selected_components)
                        if selected_components
                        else set()
                    )
                    retained: list[ManagerProfile] = []
                    target_keys = set(
                        () if target is None else target.identity_keys
                    )
                    target_manual = set(
                        () if target is None else target.manual_identity_keys
                    )
                    target_urls = set(
                        () if target is None else target.profile_urls
                    )
                    for profile in profiles:
                        if target is not None and profile.manager_id == target.manager_id:
                            continue
                        overlap = selected_set.intersection(
                            profile.identity_keys
                        )
                        if overlap:
                            target_keys.update(overlap)
                            target_manual.update(
                                set(profile.manual_identity_keys)
                                & selected_set
                            )
                            target_urls.update(profile.profile_urls)
                        remaining = tuple(
                            key
                            for key in profile.identity_keys
                            if key not in selected_set
                        )
                        if remaining:
                            retained.append(
                                replace(
                                    profile,
                                    identity_keys=remaining,
                                    manual_identity_keys=tuple(
                                        key
                                        for key in profile.manual_identity_keys
                                        if key in remaining
                                    ),
                                )
                            )
                    target_keys.update(selected_set)
                    target_manual.update(selected_anchors)
                    target_urls.update(
                        snapshot.team.reference.profile_url
                        for snapshot in teams.snapshots
                        if snapshot.team.reference.profile_url
                        and selected_set.intersection(
                            manager_identity_keys(
                                owner_user_id=snapshot.team.owner_user_id,
                                account_user_id=(
                                    snapshot.team.reference.account_user_id
                                ),
                                account_key=(
                                    snapshot.team.reference.account_key
                                ),
                                owner_name=snapshot.team.owner_name,
                            )
                        )
                    )
                    manager_id = (
                        target.manager_id
                        if target is not None
                        else f"manager:{uuid4().hex}"
                    )
                    retained.append(
                        ManagerProfile(
                            manager_id,
                            display_name.strip(),
                            tuple(sorted(target_keys)),
                            tuple(sorted(target_urls)),
                            tuple(sorted(target_manual)),
                        )
                    )
                    try:
                        settings_store.set_manager_profiles(
                            settings,
                            tuple(retained),
                        )
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()

            if profiles:
                st.subheader("Omd\u00f8b profil")
                with st.form("manager-profile-rename"):
                    rename_profile = st.selectbox(
                        "Profil",
                        profiles,
                        format_func=lambda item: item.display_name,
                        key="manager-profile-rename-target",
                    )
                    renamed = st.text_input(
                        "Nyt visningsnavn",
                        value=rename_profile.display_name,
                    )
                    rename = st.form_submit_button("Gem navn")
                if rename:
                    if not renamed.strip():
                        st.error("Visningsnavnet m\u00e5 ikke v\u00e6re tomt.")
                    else:
                        settings_store.set_manager_profiles(
                            settings,
                            tuple(
                                replace(
                                    profile,
                                    display_name=renamed.strip(),
                                )
                                if profile.manager_id
                                == rename_profile.manager_id
                                else profile
                                for profile in profiles
                            ),
                        )
                        st.rerun()

                st.subheader("Oph\u00e6v manuelle links")
                unlink_profile = st.selectbox(
                    "Profil med manuelt link",
                    profiles,
                    format_func=lambda item: item.display_name,
                    key="manager-profile-unlink-target",
                )
                unlink_options = tuple(
                    key
                    for key in unlink_profile.manual_identity_keys
                    if key in unlink_profile.identity_keys
                )
                with st.form("manager-profile-unlink"):
                    unlink_keys = st.multiselect(
                        "Identiteter der skal skilles ud",
                        unlink_options,
                        format_func=lambda key: identity_options.get(
                            key,
                            key,
                        ),
                    )
                    unlink = st.form_submit_button(
                        "Oph\u00e6v valgte links"
                    )
                if unlink:
                    components = {
                        automatic_component.get(key, frozenset((key,)))
                        for key in unlink_keys
                    }
                    removed = set().union(*components) if components else set()
                    removed.intersection_update(unlink_profile.identity_keys)
                    remaining = tuple(
                        key
                        for key in unlink_profile.identity_keys
                        if key not in removed
                    )
                    if not removed:
                        st.error("V\u00e6lg mindst \u00e9n manuel identitet.")
                    elif not remaining:
                        st.error(
                            "Mindst \u00e9n identitetskomponent skal blive "
                            "p\u00e5 den eksisterende profil."
                        )
                    else:
                        retained = [
                            profile
                            for profile in profiles
                            if profile.manager_id != unlink_profile.manager_id
                        ]
                        retained.append(
                            replace(
                                unlink_profile,
                                identity_keys=remaining,
                                manual_identity_keys=tuple(
                                    key
                                    for key in unlink_profile.manual_identity_keys
                                    if key in remaining
                                ),
                            )
                        )
                        for component in sorted(
                            components,
                            key=lambda values: sorted(values),
                        ):
                            component_keys = tuple(
                                sorted(set(component).intersection(removed))
                            )
                            if not component_keys:
                                continue
                            label = identity_options.get(
                                component_keys[0],
                                component_keys[0],
                            ).split(" \u00b7 ")[0]
                            retained.append(
                                ManagerProfile(
                                    f"manager:{uuid4().hex}",
                                    label,
                                    component_keys,
                                )
                            )
                        try:
                            settings_store.set_manager_profiles(
                                settings,
                                tuple(retained),
                            )
                        except Exception as exc:
                            st.error(str(exc))
                        else:
                            st.rerun()

    for warning in (*metadata_warnings, *ledger_warnings):
        st.warning(warning)


def _reset_calendar_filters() -> None:
    for key in tuple(st.session_state):
        if str(key).startswith("calendar:"):
            st.session_state.pop(key, None)
    for parameter in ("locale", "game"):
        if parameter in st.query_params:
            del st.query_params[parameter]


def _calendar_filter_chips(
    values,
    *,
    slug_counts,
    group_names,
    manager_names,
) -> tuple[str, ...]:
    game_filter, group_filter, manager_filter, date_filter, include_past = values
    chips: list[str] = []
    if game_filter is not None:
        label = (
            game_filter[1]
            if slug_counts.get(game_filter[1], 0) == 1
            else f"{game_filter[1]} ({game_filter[0]})"
        )
        chips.append(f"Spil: {label}")
    if group_filter is not None:
        chips.append(f"Gruppe: {group_names.get(group_filter, group_filter)}")
    if manager_filter is not None:
        chips.append(f"Manager: {manager_names.get(manager_filter, manager_filter)}")
    if isinstance(date_filter, date):
        chips.append(f"Dato: {date_filter.strftime('%d.%m.%Y')}")
    if include_past:
        chips.append("Viser tidligere begivenheder")
    return tuple(chips)


def _calendar_filter_controls(
    game_identities,
    slug_counts,
    event_group_ids,
    group_names,
    manager_names,
):
    requested_locale = str(st.query_params.get("locale", "")).casefold()
    requested_slug = str(st.query_params.get("game", ""))
    requested = (requested_locale, requested_slug)
    requested = requested if requested in game_identities else None
    if requested is not None and st.session_state.get("calendar:query-game") != requested:
        st.session_state["calendar:game"] = requested
        st.session_state["calendar:applied"] = (requested, None, None, None, False)
        st.session_state["calendar:query-game"] = requested
    applied = st.session_state.get(
        "calendar:applied",
        (requested, None, None, None, False),
    )
    chips = _calendar_filter_chips(
        applied,
        slug_counts=slug_counts,
        group_names=group_names,
        manager_names=manager_names,
    )
    with st.container(
        key="sticky-action-bar-calendar",
        horizontal=True,
        vertical_alignment="center",
        gap="small",
    ):
        with st.popover(
            f"Filtre · {len(chips)}",
            icon=":material/filter_alt:",
            key="calendar:filter-panel",
        ):
            with st.form("calendar-filters", border=False):
                game_filter = st.selectbox(
                    "Managerspil",
                    (None, *game_identities),
                    format_func=lambda identity: (
                        "Alle managerspil"
                        if identity is None
                        else (
                            identity[1]
                            if slug_counts[identity[1]] == 1
                            else f"{identity[1]} ({identity[0]})"
                        )
                    ),
                    key="calendar:game",
                    persist_state="session",
                )
                group_filter = st.selectbox(
                    "Gruppe eller turnering",
                    (None, *event_group_ids),
                    format_func=lambda key: (
                        "Alle grupper" if key is None else group_names.get(key, key)
                    ),
                    key="calendar:group",
                    persist_state="session",
                )
                manager_filter = st.selectbox(
                    "Manager",
                    (
                        None,
                        *sorted(
                            manager_names,
                            key=lambda key: (manager_names[key].casefold(), key),
                        ),
                    ),
                    format_func=lambda key: (
                        "Alle managers"
                        if key is None
                        else manager_names.get(key, key)
                    ),
                    key="calendar:manager",
                    persist_state="session",
                )
                date_filter = st.date_input(
                    "Dato",
                    value=None,
                    key="calendar:date",
                    persist_state="session",
                )
                include_past = st.toggle(
                    "Vis tidligere begivenheder",
                    value=False,
                    key="calendar:include-past",
                    persist_state="session",
                )
                submitted = st.form_submit_button(
                    "Anvend filtre",
                    icon=":material/filter_alt:",
                    type="primary",
                )
        for chip in chips[:5]:
            st.badge(chip, color="gray")
        st.button(
            "Nulstil",
            icon=":material/filter_alt_off:",
            key="calendar:reset-filters",
            on_click=_reset_calendar_filters,
            disabled=not chips,
        )
    if submitted:
        applied = (
            game_filter,
            group_filter,
            manager_filter,
            date_filter,
            include_past,
        )
        st.session_state["calendar:applied"] = applied
        st.session_state["calendar:query-game"] = game_filter
        if game_filter is None:
            for parameter in ("locale", "game"):
                if parameter in st.query_params:
                    del st.query_params[parameter]
        else:
            st.query_params["locale"] = game_filter[0]
            st.query_params["game"] = game_filter[1]
        st.rerun()
    return applied


@st.fragment
def calendar_view(
    groups: tuple[GroupDefinition, ...],
    paths: AppPaths,
    teams: SnapshotIndex,
) -> None:
    """Render the global cache-only schedule."""

    st.title("Kalender", anchor="kalender")
    calendar_slot = st.container()
    with calendar_slot:
        with st.skeleton(height=140):
            metadata, warnings = GameMetadataStore(paths.game_metadata_dir).scan()
            events = build_calendar_events(groups, metadata)
            _, settings = _manager_settings(paths)
            settings = build_effective_manager_settings(settings, groups, teams)
            manager_names: dict[str, str] = {}
            managers_by_event: dict[str, frozenset[str]] = {}
            for event in events:
                event_managers: set[str] = set()
                for team_id in event.participant_ids:
                    snapshot = teams.newest(
                        (event.game_locale, event.game_slug),
                        team_id,
                    )
                    if snapshot is None:
                        continue
                    team = snapshot.team
                    manager_id, manager_name = resolve_manager_identity(
                        settings,
                        owner_user_id=team.owner_user_id,
                        account_user_id=team.reference.account_user_id,
                        account_key=team.reference.account_key,
                        owner_name=team.owner_name,
                        fallback_key=(
                            f"{event.game_locale}:{event.game_slug}:team:{team_id}"
                        ),
                    )
                    manager_names[manager_id] = manager_name
                    event_managers.add(manager_id)
                managers_by_event[event.event_id] = frozenset(event_managers)

    game_identities = tuple(
        sorted(
            {
                (item.game_locale.casefold(), item.game_slug)
                for item in events
            }
        )
    )
    slug_counts = {
        slug: sum(identity[1] == slug for identity in game_identities)
        for _, slug in game_identities
    }
    group_names = {group.group_id: group.name for group in groups}
    (
        game_filter,
        group_filter,
        manager_filter,
        date_filter,
        include_past,
    ) = _calendar_filter_controls(
        game_identities,
        slug_counts,
        tuple(sorted({item.group_id for item in events})),
        group_names,
        manager_names,
    )
    now = datetime.now().astimezone()

    def matches_date(item: CalendarEvent) -> bool:
        if isinstance(date_filter, date):
            start = item.start or item.deadline or item.end
            end = item.end or item.deadline or item.start
            return (
                start is not None
                and end is not None
                and start.date() <= date_filter <= end.date()
            )
        if include_past or item.missing_time:
            return True
        latest = item.end or item.deadline or item.start
        return latest is not None and latest >= now

    selected = tuple(
        item
        for item in events
        if (
            game_filter is None
            or (
                item.game_locale.casefold(),
                item.game_slug,
            )
            == game_filter
        )
        and (group_filter is None or item.group_id == group_filter)
        and (
            manager_filter is None
            or manager_filter in managers_by_event[item.event_id]
        )
        and matches_date(item)
    )
    timed = tuple(item for item in selected if not item.missing_time)
    missing = tuple(item for item in selected if item.missing_time)
    st.caption(f"{len(selected)} kalenderbegivenheder matcher de anvendte filtre.")
    if timed:
        dataframe(
            [
                {
                    "Start": item.start,
                    "Deadline": item.deadline,
                    "Slut": item.end,
                    "Managerspil": (
                        item.game_slug
                        if slug_counts[item.game_slug] == 1
                        else f"{item.game_slug} ({item.game_locale})"
                    ),
                    "Kamp": item.title,
                    "Runde": item.round_number,
                }
                for item in timed
            ],
            hide_index=True,
            width="stretch",
            key="calendar:events",
        )
        for item in timed:
            with st.container(horizontal=True):
                page_link(
                    PageId.GROUP,
                    item.title,
                    icon=":material/open_in_new:",
                    group=item.group_id,
                    round=item.round_number,
                )
                if item.official_url:
                    st.link_button(
                        "Officiel gruppe",
                        item.official_url,
                        icon=":material/language:",
                        key=f"calendar-official-{item.event_id}",
                    )
    else:
        st.info("Ingen cachede kalenderbegivenheder matcher filtrene.")
    if missing:
        st.subheader("Tidspunkt mangler")
        for item in missing:
            with st.container(horizontal=True):
                st.write(f"{item.title} - runde {item.round_number}")
                page_link(
                    PageId.GAME,
                    "Åbn spilinfo",
                    icon=":material/event:",
                    locale=item.game_locale.casefold(),
                    game=item.game_slug,
                    section="administration",
                )
                if item.official_url:
                    st.link_button(
                        "Officiel gruppe",
                        item.official_url,
                        icon=":material/language:",
                        key=f"calendar-missing-official-{item.event_id}",
                    )
    for warning in warnings:
        st.warning(warning)
