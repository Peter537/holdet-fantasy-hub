"""UI pages for the additive Fantasy Hub centers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
from html import escape
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

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
    build_history_series,
    build_live_hall_of_fame_events,
    build_player_history,
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


def _time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def _age(value: datetime | None) -> str:
    if value is None:
        return "Ingen data"
    seconds = max(0, int((datetime.now().astimezone() - value.astimezone()).total_seconds()))
    if seconds < 60:
        return "under ét minut"
    if seconds < 3600:
        return f"{seconds // 60} min."
    if seconds < 86400:
        return f"{seconds // 3600} t."
    return f"{seconds // 86400} dage"


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

    players = _player_index(paths)
    metadata = GameMetadataStore(paths.game_metadata_dir).load(manager_game.game)
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
    with st.container(horizontal=True):
        st.metric("Aktiv runde", active_round or "Mangler", border=True)
        st.metric(
            "Næste deadline",
            _time(deadline) if deadline is not None else "Mangler metadata",
            border=True,
        )
        st.metric(
            "Holddata",
            _age(None if latest is None else latest.newest_team_data),
            border=True,
        )
        st.metric(
            "Spillerdata",
            _age(None if latest is None else latest.newest_player_data),
            border=True,
        )
        st.metric(
            "Manglende snapshots",
            len(latest.missing_team_ids) if latest is not None else len(team_labels),
            border=True,
        )

    if metadata is None:
        st.warning(
            "Tidsplan og deadline mangler. Brug den manuelle opdatering ovenfor "
            "for at hente og gemme spilmetadata."
        )
    if latest is None:
        st.info("Der er endnu ingen cachede rundedata for managerspillet.")
    else:
        labels = {
            "ready": ("Klar", "green"),
            "preliminary": ("Foreløbig", "orange"),
            "missing": ("Mangler data", "red"),
            "error": ("Seneste opdatering fejlede", "red"),
        }
        label, color = labels[latest.readiness]
        st.badge(label, color=color, icon=":material/data_check:")
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
                st.dataframe(rank_rows, hide_index=True)
            else:
                st.caption("Ingen bevægelser mellem de seneste tilgængelige runder.")
    with right:
        with st.container(border=True):
            st.markdown("**Skader og karantæner**")
            if injury_rows:
                st.dataframe(injury_rows, hide_index=True)
            else:
                st.caption("Ingen markeringer i det valgte spillersnapshot.")

    with st.container(horizontal=True):
        st.link_button(
            "Åbn spillerstatistik",
            (
                f"?view=game&locale={manager_game.game.locale}&game="
                f"{manager_game.game.slug}&section=players"
            ),
            icon=":material/query_stats:",
        )
        st.link_button(
            "Åbn holdstatistik",
            (
                f"?view=game&locale={manager_game.game.locale}&game="
                f"{manager_game.game.slug}&section=teams"
            ),
            icon=":material/groups:",
        )
        st.link_button(
            "Åbn Datastatus",
            "?view=data&section=quality",
            icon=":material/data_check:",
        )


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
        profile.label if profile.known else "Regler kan ikke valideres",
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
    st.dataframe(
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
    with st.container(horizontal=True):
        if st.button(
            "Gem valgte pÅ watchlist",
            icon=":material/star:",
            key=f"save-watchlist:{scope}",
            disabled=len(selected) < 2,
        ):
            other = tuple(
                item
                for item in settings.watchlist
                if item.game_identity != (game.locale.casefold(), game.slug)
            )
            chosen = tuple(
                watchlist_entry(game, keyed[key]) for key in selected
            )
            store.set_watchlist(settings, (*other, *chosen))
            st.toast("Watchlisten er gemt.")
            st.rerun()
        if st.button(
            "Fjern spillets favoritter",
            icon=":material/star_border:",
            key=f"clear-watchlist:{scope}",
        ):
            remaining = tuple(
                item
                for item in settings.watchlist
                if item.game_identity != (game.locale.casefold(), game.slug)
            )
            store.set_watchlist(settings, remaining)
            st.rerun()
    if len(selected) < 2:
        st.info("Vælg mindst to spillere for at sammenligne dem.")
        return
    st.dataframe(
        [
            {
                "Spiller": keyed[key].name,
                "Hold": keyed[key].team,
                "Position": keyed[key].position,
                "Pris": keyed[key].value,
                "Total vækst": keyed[key].total_growth,
                "Rundevækst": keyed[key].round_growth,
                "Status": _status_labels(keyed[key]),
            }
            for key in selected
        ],
        hide_index=True,
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
    diff = compare_round_snapshots(players, teams, game, round_number)
    if diff is None:
        st.info(
            "Den valgte runde eller en tidligere cachet runde mangler, "
            "sÅ der kan endnu ikke laves en sammenligning."
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
                    st.dataframe(rows(items), hide_index=True)
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
            "Overall-rang": item.overall_rank,
            "Grupperang": item.group_rank,
        }
        for item in history
    )
    labels = ["Værdi/point", "Rundevækst", "Overall-rang"]
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
            "Overall-rang",
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
        "ready": ("Klar", "green", ":material/check_circle:"),
        "preliminary": ("Foreløbig", "orange", ":material/schedule:"),
        "missing": ("Mangler data", "red", ":material/warning:"),
        "error": ("Fejl", "red", ":material/error:"),
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
                        "Klar" if latest.player_snapshot else "Mangler",
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
            st.link_button(
                "Åbn Rundecenter",
                (
                    f"?view=game&locale={manager_game.game.locale}&game="
                    f"{manager_game.game.slug}&section=round-center"
                ),
                icon=":material/open_in_new:",
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
                        st.dataframe(
                            [
                                {
                                    "Runde": row.round_number,
                                    "Status": status_labels[row.readiness][0],
                                    "Hold": (
                                        f"{row.team_snapshots}/"
                                        f"{row.expected_teams}"
                                    ),
                                    "Spillere": (
                                        "Ja" if row.player_snapshot else "Mangler"
                                    ),
                                    "Mangler": (
                                        ", ".join(row.missing_team_names) or "-"
                                    ),
                                }
                                for row in rows[1:]
                            ],
                            hide_index=True,
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


def hall_of_fame_view(
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    paths: AppPaths,
) -> None:
    st.title("Hall of Fame", anchor="hall-of-fame")
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
            "Live-resultater klar",
            sum(item.complete for item in live),
            border=True,
        )
        st.metric(
            "Afventer komplette data",
            sum(not item.complete for item in live),
            border=True,
        )
    if board.rows:
        st.dataframe(
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
        )
    else:
        st.info(
            "Ingen komplette resultater er frosset endnu. Live-preview vises nedenfor."
        )
    preview = build_hall_of_fame(
        tuple((*frozen, *(item for item in live if item.event_id not in {event.event_id for event in frozen}))),
        settings.hall_of_fame_score,
        include_incomplete=True,
    )
    with st.expander("Live-preview", on_change="rerun") as exp:
        if exp.open:
            if preview.rows:
                st.dataframe(
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
                )
            st.dataframe(
                [
                    {
                        "Konkurrence": item.competition_name,
                        "Type": item.kind,
                        "Runde": item.round_number,
                        "Status": "Komplet" if item.complete else "Afventer data",
                    }
                    for item in live
                ],
                hide_index=True,
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
    st.success("Hele arkivet er valideret: stier, schemaer, størrelser og checksums.")
    st.dataframe(
        [
            {"Fil": item.path, "Bytes": item.size, "SHA-256": item.sha256}
            for item in validation.manifest.files
        ],
        hide_index=True,
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

