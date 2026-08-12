"""Streamlit surfaces for the cache-only decision-analysis center."""

from __future__ import annotations

from datetime import datetime
import pandas as pd
import streamlit as st

from website.scouting_page import (
    render_player_scouting_detail,
    render_player_watchlist_editor,
)

from website.presentation import data_status_label, dataframe, format_relative_precise

from website.navigation import PageId, page_link, relative_url

from holdet_lib import (
    AnalysisInboxStore,
    AppPaths,
    DEFAULT_PLAYER_TAGS,
    FixtureStore,
    GameUrl,
    GameMetadataStore,
    GroupDefinition,
    HubSettingsStore,
    ManagerGame,
    OwnTeamSelection,
    PayloadError,
    PlayerAnnotation,
    PlayerStatisticsStore,
    SnapshotIndex,
    build_bank_analysis,
    build_captain_analysis,
    build_group_comparison,
    build_group_exposure,
    build_player_decision_analysis,
    build_player_change_explanation,
    build_team_decision_ledger,
    optimize_ideal_team,
    player_identity,
    rule_profile_for_game,
    simulate_transfer_scenario,
)


def _format(value: int | float | None, *, percent: bool = False) -> str:
    if value is None:
        return "Ikke tilgængelig"
    if percent:
        return f"{value:.2f} %".replace(".", ",")
    if isinstance(value, float):
        return f"{value:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{value:,}".replace(",", ".")


def _curve_svg(points: tuple[tuple[int, int], ...], value_label: str) -> str:
    """Render a responsive, accessible price/point curve without Vega state."""

    width, height = 760, 250
    left, right, top, bottom = 74, 22, 20, 42
    plot_width = width - left - right
    plot_height = height - top - bottom
    rounds = [round_number for round_number, _ in points]
    values = [value for _, value in points]
    round_min, round_max = min(rounds), max(rounds)
    value_min, value_max = min(values), max(values)
    if value_min == value_max:
        value_min -= 1
        value_max += 1

    def x(round_number: int) -> float:
        span = max(round_max - round_min, 1)
        return left + (round_number - round_min) / span * plot_width

    def y(value: int) -> float:
        return top + (value_max - value) / (value_max - value_min) * plot_height

    polyline = " ".join(
        f"{x(round_number):.1f},{y(value):.1f}"
        for round_number, value in points
    )
    markers = "".join(
        (
            f'<circle cx="{x(round_number):.1f}" cy="{y(value):.1f}" '
            'r="4" fill="#ff4b4b">'
            f"<title>Runde {round_number}: {_format(value)}</title></circle>"
        )
        for round_number, value in points
    )
    label_indexes = sorted(
        {
            round(index * (len(points) - 1) / min(len(points) - 1, 5))
            for index in range(min(len(points), 6))
        }
    )
    x_labels = "".join(
        (
            f'<text x="{x(points[index][0]):.1f}" y="{height - 13}" '
            'text-anchor="middle">'
            f"{points[index][0]}</text>"
        )
        for index in label_indexes
    )
    grid = "".join(
        (
            f'<line x1="{left}" x2="{width - right}" y1="{line_y:.1f}" '
            f'y2="{line_y:.1f}" stroke="currentColor" opacity="0.12" />'
            f'<text x="{left - 10}" y="{line_y + 4:.1f}" '
            f'text-anchor="end">{_format(label_value)}</text>'
        )
        for line_y, label_value in (
            (top, value_max),
            (top + plot_height / 2, round((value_min + value_max) / 2)),
            (top + plot_height, value_min),
        )
    )
    return (
        '<div style="width:100%;overflow-x:auto">'
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        'style="min-width:300px;color:rgba(250,250,250,.72);font-size:12px" '
        f'role="img" aria-label="{value_label} pr. faktisk runde">'
        f"<title>{value_label} pr. faktisk runde</title>"
        f"{grid}"
        f'<polyline points="{polyline}" fill="none" stroke="#ff4b4b" '
        'stroke-width="3" stroke-linejoin="round" stroke-linecap="round" />'
        f"{markers}{x_labels}"
        f'<text x="{width / 2:.1f}" y="{height - 1}" '
        'text-anchor="middle">Runde</text>'
        "</svg></div>"
    )


def _rule_profile(manager_game: ManagerGame, paths: AppPaths):
    try:
        metadata = GameMetadataStore(paths.game_metadata_dir).load(manager_game.game)
    except (OSError, PayloadError, ValueError):
        metadata = None
    if metadata is not None and metadata.rule_profile is not None:
        return metadata.rule_profile
    return rule_profile_for_game(
        manager_game.game,
        game_id=None if metadata is None else metadata.game_id,
        salary_cap=None if metadata is None else metadata.salary_cap,
        label=manager_game.name,
    )


def _game_groups(
    manager_game: ManagerGame, groups: tuple[GroupDefinition, ...]
) -> tuple[GroupDefinition, ...]:
    return tuple(
        group
        for group in groups
        if (group.game.locale.casefold(), group.game.slug) == manager_game.identity
    )


def _team_options(groups: tuple[GroupDefinition, ...]) -> dict[int, str]:
    options: dict[int, str] = {}
    for group in groups:
        for member in group.teams:
            options.setdefault(member.team_id, member.name)
    return options


def _certainty(provenance) -> None:
    renderer = st.success if provenance.certainty == "final" else st.warning
    round_text = (
        " · runde " + ", ".join(str(value) for value in provenance.rounds)
        if provenance.rounds
        else ""
    )
    suffix = f" · {provenance.sample_size} observationer{round_text}"
    renderer(data_status_label(provenance.certainty) + suffix)
    if provenance.missing_reasons:
        st.caption(" · ".join(provenance.missing_reasons))


def _selected_analysis_panel(manager_game: ManagerGame) -> str:
    labels = ("Beslutninger", "Gruppe", "Idealhold", "Eksperimentel")
    slugs = ("decisions", "group", "ideal", "experimental")
    requested = str(st.query_params.get("analysis", ""))
    default = slugs.index(requested) if requested in slugs else 0
    key = f"analysis-panel-{manager_game.game.locale}-{manager_game.game.slug}"
    selected = st.segmented_control(
        "Analyseområde",
        labels,
        default=labels[default],
        key=key,
    )
    selected = selected or labels[default]
    slug = slugs[labels.index(selected)]
    if requested != slug:
        st.query_params["analysis"] = slug
    return slug


@st.fragment
def analysis_panel(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    snapshots: SnapshotIndex,
    paths: AppPaths,
    *,
    read_only: bool = False,
) -> None:
    """Render one lazily selected analysis area inside the game page."""

    st.header("Analyse og beslutninger")
    st.caption(
        "Alle tal beregnes fra lokale snapshots. Regelafhængige resultater "
        "vises kun, når den konkrete sæson er verificeret."
    )
    panel = _selected_analysis_panel(manager_game)
    if panel == "decisions":
        game_groups = _game_groups(manager_game, groups)
        player_index = PlayerStatisticsStore(paths.snapshot_dir).scan(
            manager_game.game
        )
        rules = _rule_profile(manager_game, paths)
        _decision_panel(
            manager_game,
            game_groups,
            snapshots,
            player_index,
            rules,
            paths,
            read_only=read_only,
        )
    elif panel == "group":
        game_groups = _game_groups(manager_game, groups)
        player_index = PlayerStatisticsStore(paths.snapshot_dir).scan(
            manager_game.game
        )
        _group_panel(manager_game, game_groups, snapshots, player_index, paths)
    elif panel == "ideal":
        player_index = PlayerStatisticsStore(paths.snapshot_dir).scan(
            manager_game.game
        )
        rules = _rule_profile(manager_game, paths)
        _ideal_panel(manager_game, player_index, rules)
    else:
        game_groups = _game_groups(manager_game, groups)
        player_index = PlayerStatisticsStore(paths.snapshot_dir).scan(
            manager_game.game
        )
        _experimental_panel(
            manager_game,
            game_groups,
            snapshots,
            player_index,
            paths,
            read_only=read_only,
        )


def _decision_panel(
    manager_game,
    groups,
    snapshots,
    player_index,
    rules,
    paths,
    *,
    read_only,
) -> None:
    st.subheader("Beslutningsregnskab", anchor="beslutningsregnskab")
    options = _team_options(groups)
    if not options:
        st.info("Tilføj et hold til en gruppe, før holdbeslutninger kan analyseres.")
        return
    settings_store = HubSettingsStore(paths.hub_settings_file)
    settings = settings_store.load()
    saved = next(
        (
            item.team_id
            for item in settings.own_teams
            if (item.game_locale.casefold(), item.game_slug) == manager_game.identity
            and item.team_id in options
        ),
        None,
    )
    team_ids = tuple(sorted(options, key=lambda team_id: options[team_id].casefold()))
    team_id = st.selectbox(
        "Mit hold",
        team_ids,
        index=team_ids.index(saved) if saved in team_ids else 0,
        format_func=options.__getitem__,
        key=f"analysis-team-{manager_game.game.slug}",
    )
    if st.button(
        "Gem som mit standardhold",
        disabled=read_only,
        key=f"save-own-team-{manager_game.game.slug}",
    ):
        settings_store.set_own_team(
            settings,
            OwnTeamSelection(
                manager_game.game.locale.casefold(),
                manager_game.game.slug,
                int(team_id),
            ),
        )
        st.success("Standardholdet er gemt.")
    rounds = snapshots.rounds_for(manager_game.game, (int(team_id),))
    if not rounds:
        st.info("Holdet har ingen lokale rundesnapshots.")
        return
    selected_round = st.selectbox(
        "Runde",
        rounds,
        key=f"analysis-round-{manager_game.game.slug}-{team_id}",
    )
    if not rules.verified:
        st.warning(
            "Sæsonreglerne er ikke verificeret. Kaptajn-, bank- og "
            "transfergebyrberegninger vises derfor som uverificerede."
        )
    ledger = build_team_decision_ledger(
        snapshots, player_index, manager_game.game, int(team_id), rules
    )
    if ledger.decisions:
        st.markdown("#### Transferregnskab")
        dataframe(
            [
                {
                    "Runde": item.round_number,
                    "Købt": ", ".join(item.bought),
                    "Solgt": ", ".join(item.sold),
                    "Købtes vækst": item.bought_growth,
                    "Solgtes vækst": item.sold_counterfactual_growth,
                    "Gebyr": item.fee_cost,
                    "Beslutningsdelta": item.decision_delta,
                    "Uden handel": item.no_trade_change,
                    "Status": item.provenance.certainty,
                    "Anvendte runder": ", ".join(
                        str(value) for value in item.provenance.rounds
                    ),
                    "Manglende input": " · ".join(
                        item.provenance.missing_reasons
                    ),
                }
                for item in ledger.decisions
            ],
            hide_index=True,
            width="stretch",
            key=(
                f"analysis:{manager_game.game.locale}:"
                f"{manager_game.game.slug}:decision-ledger"
            ),
        )
        cards = st.columns(3)
        cards[0].metric(
            "Sum af et-rundes deltaer", _format(ledger.verified_total)
        )
        cards[1].metric(
            "Bedste transfer",
            "—" if ledger.best is None else f"Runde {ledger.best.round_number}",
            None if ledger.best is None else _format(ledger.best.decision_delta),
        )
        cards[2].metric(
            "Værste transfer",
            "—" if ledger.worst is None else f"Runde {ledger.worst.round_number}",
            None if ledger.worst is None else _format(ledger.worst.decision_delta),
        )
        st.caption(
            "Sæsonsummen er summerede et-rundes kontrafaktiske beslutninger — "
            "ikke en fuld alternativ sæsonbane."
        )
    else:
        st.info("Ingen runder med registrerede spillerudskiftninger blev fundet.")
    located = snapshots.summary_for(manager_game.game, int(team_id), int(selected_round))
    roster_snapshot = snapshots.roster_for(manager_game.game, int(team_id), int(selected_round))
    if located is None or roster_snapshot is None:
        st.warning("Runden mangler enten historik eller en præcis rundetrup.")
        return
    st.markdown("#### Kaptajn")
    captain = build_captain_analysis(roster_snapshot, located[1], rules)
    st.metric("Faktisk kaptajnbonus", _format(captain.actual_bonus))
    _certainty(captain.provenance)
    if captain.alternatives:
        dataframe(
            [
                {
                    "Spiller": item.name,
                    "Alternativ bonus": item.bonus,
                    "Alternativ rundeændring": item.total_change,
                }
                for item in captain.alternatives
            ],
            hide_index=True,
            width="stretch",
            key=(
                f"analysis:{manager_game.game.locale}:"
                f"{manager_game.game.slug}:captain-alternatives"
            ),
        )
    if located[1].bank is not None:
        st.markdown("#### Bankens break-even")
        max_investment = max(0, int(located[1].bank))
        investment = int(
            st.number_input(
                "Overvejet investering",
                min_value=0,
                max_value=max_investment,
                value=min(1_000_000, max_investment),
                step=100_000,
                key=f"bank-investment-{manager_game.game.slug}-{team_id}-{selected_round}",
            )
        )
        if investment > 0:
            growth_by_player: dict[str, dict[int, int]] = {}
            for player_snapshot in player_index.for_game(manager_game.game):
                if (
                    player_snapshot.statistics.round_status != "complete"
                    or player_snapshot.statistics.round_number > int(selected_round)
                ):
                    continue
                for entry in player_snapshot.statistics.entries:
                    if entry.round_growth is None:
                        continue
                    key = player_identity(manager_game.game, entry)
                    growth_by_player.setdefault(key, {})[
                        player_snapshot.statistics.round_number
                    ] = entry.round_growth
            windows = []
            for values_by_round in growth_by_player.values():
                values = [value for _, value in sorted(values_by_round.items())]
                windows.append(
                    (
                        sum(values[-3:]) / 3 if len(values) >= 3 else None,
                        sum(values[-5:]) / 5 if len(values) >= 5 else None,
                    )
                )
            bank = build_bank_analysis(
                int(located[1].bank),
                investment,
                rules,
                actual_interest=located[1].interest,
                round_number=int(selected_round),
                historical_windows=windows,
            )
            columns = st.columns(3)
            columns[0].metric("Faktisk rente", _format(bank.actual_interest))
            columns[1].metric(
                "Regelberegnet rente", _format(bank.full_bank_interest)
            )
            columns[2].metric("Transfergebyr", _format(bank.transfer_fee))
            thresholds = st.columns(3)
            thresholds[0].metric("Tabt rente", _format(bank.lost_interest))
            thresholds[1].metric(
                "Break-even-vækst", _format(bank.break_even_growth)
            )
            thresholds[2].metric(
                "Break-even", _format(bank.break_even_percent, percent=True)
            )
            shares = st.columns(2)
            shares[0].metric(
                "Slog grænsen · form 3",
                _format(
                    None
                    if bank.beat_share_3 is None
                    else bank.beat_share_3 * 100,
                    percent=True,
                ),
                f"{bank.compared_players_3} spillere",
            )
            shares[1].metric(
                "Slog grænsen · form 5",
                _format(
                    None
                    if bank.beat_share_5 is None
                    else bank.beat_share_5 * 100,
                    percent=True,
                ),
                f"{bank.compared_players_5} spillere",
            )
            _certainty(bank.provenance)


def _group_panel(manager_game, groups, snapshots, player_index, paths) -> None:
    st.subheader(
        "Gruppefører og eksponering", anchor="gruppefoerer-og-eksponering"
    )
    if not groups:
        st.info("Managerspillet har ingen grupper.")
        return
    group = st.selectbox(
        "Gruppe",
        groups,
        format_func=lambda item: item.name,
        key="analysis-group",
    )
    team_ids = tuple(item.team_id for item in group.teams)
    rounds = snapshots.rounds_for(group.game, team_ids)
    if not rounds:
        st.info("Gruppen har ingen lokale rundedata.")
        return
    round_number = st.selectbox("Runde", rounds, key=f"analysis-group-round-{group.group_id}")
    labels = {item.team_id: item.name for item in group.teams}
    settings = HubSettingsStore(paths.hub_settings_file).load()
    default_team_id = next(
        (
            item.team_id
            for item in settings.own_teams
            if (item.game_locale.casefold(), item.game_slug)
            == manager_game.identity
            and item.team_id in team_ids
        ),
        None,
    )
    own_id = st.selectbox(
        "Mit hold i sammenligningen",
        team_ids,
        index=team_ids.index(default_team_id) if default_team_id in team_ids else 0,
        format_func=labels.__getitem__,
        key=f"analysis-group-team-{group.group_id}",
    )
    comparison = build_group_comparison(
        group, snapshots, player_index, int(own_id), int(round_number)
    )
    if comparison is None:
        st.warning("Sammenligningen kræver både dit og gruppeførerens rundetrup.")
    else:
        st.markdown(
            f"#### {comparison.own_team_name} mod {comparison.leader_team_name}"
        )
        metrics = st.columns(4)
        metrics[0].metric("Fælles", len(comparison.common_players))
        metrics[1].metric("Kun dit hold", len(comparison.own_only))
        metrics[2].metric("Faktisk swing", _format(comparison.actual_swing))
        metrics[3].metric("Formbaseret proxy", _format(comparison.form_proxy_swing))
        details = st.columns(3)
        details[0].write("**Fælles spillere**")
        details[0].write(", ".join(comparison.common_players) or "Ingen")
        details[1].write("**Kun dit hold**")
        details[1].write(", ".join(comparison.own_only) or "Ingen")
        details[2].write("**Kun føreren**")
        details[2].write(", ".join(comparison.leader_only) or "Ingen")
        _certainty(comparison.provenance)
    exposure = build_group_exposure(group, snapshots, int(round_number))
    st.markdown("#### Gruppens eksponering")
    st.caption(
        f"Dækning: {len(exposure.covered_team_ids)} af {exposure.total_teams} hold."
    )
    if exposure.missing_team_ids:
        st.warning(
            "Mangler rundetrup for: "
            + ", ".join(labels.get(team_id, str(team_id)) for team_id in exposure.missing_team_ids)
        )
    if exposure.rows:
        dataframe(
            [
                {
                    "Spiller": item.name,
                    "Ejere": item.owners,
                    "Dækkede hold": item.covered_teams,
                    "Eksponering": item.ownership_percent / 100,
                }
                for item in exposure.rows
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "Eksponering": st.column_config.ProgressColumn(format="percent")
            },
            key=(
                f"analysis:{manager_game.game.locale}:"
                f"{manager_game.game.slug}:group-exposure"
            ),
        )
    else:
        st.info("Ingen dækkede rundetrupper er tilgængelige for eksponering.")


def _ideal_panel(manager_game, player_index, rules) -> None:
    st.subheader(
        "Idealhold for en afsluttet runde", anchor="idealhold-afsluttet-runde"
    )
    rounds = tuple(
        round_number
        for round_number in player_index.rounds_for(manager_game.game)
        if (snapshot := player_index.newest(manager_game.game, round_number)) is not None
        and snapshot.statistics.round_status == "complete"
    )
    if not rounds:
        st.info("Hent en afsluttet spillerunde for at beregne idealholdet.")
        return
    round_number = st.selectbox("Afsluttet runde", rounds, key=f"ideal-round-{manager_game.game.slug}")
    if not rules.verified:
        st.warning(
            "Idealhold er låst, indtil budget-, trups-, formations- og "
            "klubregler er verificeret for denne sæson."
        )
        return
    timeout = st.slider("Tidsgrænse i sekunder", 1, 30, 5)
    result_key = f"ideal-result-{manager_game.game.slug}-{round_number}"
    if st.button("Beregn idealhold", type="primary"):
        snapshot = player_index.newest(manager_game.game, int(round_number))
        assert snapshot is not None
        with st.spinner("Søger efter den eksakte optimale trup …"):
            st.session_state[result_key] = optimize_ideal_team(
                snapshot.statistics.entries,
                rules,
                round_number=int(round_number),
                round_complete=True,
                timeout_seconds=float(timeout),
            )
    result = st.session_state.get(result_key)
    if result is None:
        return
    if result.status == "optimal":
        st.success("Idealholdet er bevist optimalt.")
    elif result.status == "timeout":
        st.warning(
            "Søgningen blev ikke bevist optimal; bedste fundne trup vises "
            "som foreløbig."
        )
    elif result.status == "infeasible":
        st.error("Ingen trup opfylder alle verificerede regler.")
    else:
        st.warning("Datagrundlaget er ikke verificeret til idealhold.")
    st.caption(
        f"{result.explored_nodes:,} noder undersøgt · "
        f"{result.excluded_missing_growth} spillere uden rundevækst ekskluderet · "
        f"bedste værdi {_format(result.objective)} · "
        f"sikkert loft {_format(result.objective_upper_bound)}."
    )
    if result.players:
        dataframe(
            [
                {
                    "Spiller": item.name,
                    "Hold": item.team,
                    "Position": item.position,
                    "Pris": item.value,
                    "Rundevækst": item.round_growth,
                }
                for item in result.players
            ],
            hide_index=True,
            width="stretch",
            key=(
                f"analysis:{manager_game.game.locale}:"
                f"{manager_game.game.slug}:ideal-team"
            ),
        )


def _experimental_panel(
    manager_game,
    groups,
    snapshots,
    player_index,
    paths,
    *,
    read_only,
) -> None:
    st.subheader("Eksperimentelle modeller", anchor="eksperimentelle-modeller")
    st.warning(
        "Dette område viser modeller og usikkerhedsintervaller — ikke facit "
        "eller anbefalinger."
    )
    store = HubSettingsStore(paths.hub_settings_file)
    settings = store.load()
    enabled = manager_game.identity in settings.experimental_games
    requested = st.checkbox(
        "Aktivér eksperimentelle analyser for dette spil",
        value=enabled,
        key=f"experimental-enabled-{manager_game.game.slug}",
    )
    if requested != enabled and st.button(
        "Gem eksperimentel adgang",
        disabled=read_only,
        key=f"save-experimental-{manager_game.game.slug}",
    ):
        store.set_experimental_game(settings, manager_game.game, requested)
        st.rerun()
    if not enabled:
        st.info("Aktivér og gem adgangen for at se fixture- og simulationsmodulerne.")
        return
    st.markdown("#### Fixtures og difficulty")
    try:
        fixture_snapshot = FixtureStore(paths.fixture_dir).load(manager_game.game)
    except (OSError, PayloadError, ValueError) as exc:
        fixture_snapshot = None
        st.warning(f"Den lokale fixturecache kunne ikke læses: {exc}")
    if fixture_snapshot is None:
        st.info(
            "Ingen schema-valideret offentlig fixture-adapter er verificeret "
            "for denne sæson. Der vises derfor ingen opfundet difficulty-score."
        )
    else:
        fixture_rows = []
        for item in fixture_snapshot.records:
            row = {
                "Runde": item.round_number,
                "Hold": item.team,
                "Modstander": item.opponent,
                "Hjemme/ude": {
                    "home": "Hjemme",
                    "away": "Ude",
                    "neutral": "Neutral",
                }[item.home_away],
                "Start": item.start_at.astimezone().strftime("%d.%m.%Y %H:%M"),
            }
            if fixture_snapshot.source.difficulty_verified:
                row["Officiel difficulty"] = item.official_difficulty
            fixture_rows.append(row)
        if fixture_rows:
            dataframe(
                fixture_rows,
                hide_index=True,
                width="stretch",
                key=(
                    f"analysis:{manager_game.game.locale}:"
                    f"{manager_game.game.slug}:fixtures"
                ),
            )
        else:
            st.info("Fixturecachen er verificeret, men indeholder ingen kampe.")
        if not fixture_snapshot.source.difficulty_verified:
            st.caption("Difficulty er ikke verificeret; kun fixturelisten vises.")
        st.caption(
            f"Kilde: {fixture_snapshot.source.source_url} · hentet "
            f"{fixture_snapshot.fetched_at.astimezone().strftime('%d.%m.%Y %H:%M')}"
        )
    options = _team_options(groups)
    if not options:
        return
    team_id = st.selectbox(
        "Hold til scenarie",
        tuple(options),
        format_func=options.__getitem__,
        key=f"model-team-{manager_game.game.slug}",
    )
    latest_team = snapshots.newest(manager_game.game, int(team_id))
    latest_players = player_index.newest(manager_game.game)
    if latest_team is None or latest_players is None:
        st.info("Modellen kræver både et hold- og spillersnapshot.")
        return
    roster_ids = {item.player_id for item in latest_team.team.roster}
    available = {
        (item.entry_id if item.entry_id is not None else item.source_index): item
        for item in latest_players.statistics.entries
    }
    sell_id = st.selectbox(
        "Spiller ud",
        tuple(sorted(roster_ids)),
        format_func=lambda player_id: next(
            (item.name for item in latest_team.team.roster if item.player_id == player_id),
            str(player_id),
        ),
        key=f"model-sell-{manager_game.game.slug}",
    )
    buy_ids = tuple(
        player_id for player_id in sorted(available)
        if player_id not in roster_ids
    )
    if not buy_ids:
        st.info("Spillersnapshotet har ingen købskandidater uden for truppen.")
        return
    buy_id = st.selectbox(
        "Spiller ind",
        buy_ids,
        format_func=lambda player_id: available[player_id].name,
        key=f"model-buy-{manager_game.game.slug}",
    )
    baseline = tuple(str(player_id) for player_id in sorted(roster_ids))
    scenario = tuple(
        str(player_id)
        for player_id in sorted((roster_ids - {int(sell_id)}) | {int(buy_id)})
    )
    vectors = []
    positions: dict[str, str] = {}
    for snapshot in player_index.for_game(manager_game.game):
        if snapshot.statistics.round_status != "complete":
            continue
        vector: dict[str, int | None] = {}
        for item in snapshot.statistics.entries:
            item_id = item.entry_id if item.entry_id is not None else item.source_index
            vector[str(item_id)] = item.round_growth
            positions[str(item_id)] = item.position
        vectors.append((snapshot.statistics.round_number, vector))
    result_key = f"simulation-result-{manager_game.game.slug}-{team_id}"
    seed_text = st.text_input(
        "Seed (valgfri)",
        placeholder="Tomt felt bruger et deterministisk seed",
        key=f"model-seed-{manager_game.game.slug}",
    )
    if st.button("Kør 10.000 scenarier", type="primary"):
        try:
            chosen_seed = None if not seed_text.strip() else int(seed_text)
            if chosen_seed is not None and chosen_seed < 0:
                raise ValueError
        except ValueError:
            st.error("Seed skal være et ikke-negativt heltal.")
        else:
            with st.spinner("Bootstrapper tre fremtidige runder …"):
                st.session_state[result_key] = simulate_transfer_scenario(
                    vectors,
                    baseline,
                    scenario,
                    positions=positions,
                    seed=chosen_seed,
                )
    result = st.session_state.get(result_key)
    if result is None:
        return
    cards = st.columns(4)
    cards[0].metric("Median-delta", _format(result.median_delta))
    cards[1].metric("P10", _format(result.p10))
    cards[2].metric("P90", _format(result.p90))
    cards[3].metric(
        "Slår baseline",
        _format(
            None if result.probability_better is None else result.probability_better * 100,
            percent=True,
        ),
    )
    st.caption(
        f"Status: {result.status} · seed {result.seed} · "
        f"{result.provenance.sample_size} historiske runder · "
        f"inputdækning {result.input_coverage:.1%}."
    )
    _certainty(result.provenance)
    if result.backtest_observations:
        st.markdown("#### Walk-forward-backtest")
        backtest = st.columns(3)
        backtest[0].metric("Model · MAE", _format(result.backtest_model_mae))
        backtest[1].metric(
            "Seneste runde · MAE", _format(result.backtest_latest_mae)
        )
        backtest[2].metric(
            "3-runders snit · MAE", _format(result.backtest_form_3_mae)
        )
        st.caption(
            f"{result.backtest_observations} walk-forward-observationer. "
            "Lavere absolut gennemsnitsfejl er bedre."
        )


def player_detail_view(
    manager_game: ManagerGame,
    player_key: str,
    paths: AppPaths,
    *,
    read_only: bool = False,
) -> None:
    index = PlayerStatisticsStore(paths.snapshot_dir).scan(manager_game.game)
    analysis = build_player_decision_analysis(index, manager_game.game, player_key)
    latest = index.newest(manager_game.game)
    latest_entry = None
    if latest is not None:
        latest_entry = next(
            (
                item
                for item in latest.statistics.entries
                if player_identity(manager_game.game, item) == player_key
            ),
            None,
        )
    if analysis is None or latest_entry is None:
        st.title("Spilleren blev ikke fundet")
        st.info("Spilleren findes ikke i de lokale snapshots for dette spil.")
        page_link(
            PageId.GAME,
            "Tilbage til spillerstatistik",
            locale=manager_game.game.locale,
            game=manager_game.game.slug,
            section="players",
        )
        return
    st.title(analysis.name, anchor=f"spiller-{latest_entry.entry_id or latest_entry.source_index}")
    st.caption(
        f"{latest_entry.team} · {latest_entry.position} · "
        f"seneste lokale runde {latest.statistics.round_number}"
    )
    with st.container(horizontal=True):
        st.metric(
            "Status",
            "Aktiv" if latest_entry.is_active and not latest_entry.is_disabled else "Ikke aktiv",
            border=True,
        )
        st.metric("Snapshotalder", format_relative_precise(latest.generated_at), border=True)
        st.metric(
            "Sikkerhed",
            data_status_label(
                "final" if latest.statistics.round_status == "complete" else "preliminary"
            ),
            border=True,
        )
    value_label = (
        "Aktuel pris" if latest.statistics.unit != "points" else "Aktuelle point"
    )
    if latest.statistics.unit != "points":
        primary_metrics = st.columns(3)
        primary_metrics[0].metric(value_label, _format(analysis.latest_value))
        primary_metrics[1].metric(
            "Vækst pr. aktuel mio.", _format(analysis.growth_per_million)
        )
        primary_metrics[2].metric(
            "Stabilitet",
            "—"
            if analysis.stability_score is None
            else f"{analysis.stability_score}/100",
            analysis.stability_label,
        )
        form_metrics = st.columns(2)
        form_metrics[0].metric("Form 3", _format(analysis.form_3))
        form_metrics[1].metric("Form 5", _format(analysis.form_5))
    else:
        primary_metrics = st.columns(2)
        primary_metrics[0].metric(value_label, _format(analysis.latest_value))
        primary_metrics[1].metric(
            "Stabilitet",
            "—"
            if analysis.stability_score is None
            else f"{analysis.stability_score}/100",
            analysis.stability_label,
        )
        form_metrics = st.columns(2)
        form_metrics[0].metric("Form 3", _format(analysis.form_3))
        form_metrics[1].metric("Form 5", _format(analysis.form_5))
    _certainty(analysis.provenance)
    st.subheader("Runde-for-runde udvikling")
    if analysis.curve:
        round_status = {
            item.statistics.round_number: item.statistics.round_status
            for item in index.for_game(manager_game.game)
        }
        value_column = "Pris" if latest.statistics.unit != "points" else "Point"
        frame = pd.DataFrame(analysis.curve, columns=("Runde", value_column))
        frame["Datastatus"] = frame["Runde"].map(
            lambda round_number: data_status_label(
                "final"
                if round_status.get(round_number) == "complete"
                else "preliminary"
            )
        )
        if len(frame) >= 2:
            st.markdown(
                _curve_svg(analysis.curve, value_column),
                unsafe_allow_html=True,
            )
        else:
            st.info(
                "Der er kun ét numerisk datapunkt. Grafen vises, når mindst "
                "to runder er gemt."
            )
        dataframe(
            frame,
            hide_index=True,
            width="stretch",
            key=(
                f"player:{manager_game.game.locale}:"
                f"{manager_game.game.slug}:{player_key}:curve"
            ),
        )
        if any(value != "Aktuel" for value in frame["Datastatus"]):
            st.caption(
                "Punkter fra en ikke-afsluttet runde er markeret som "
                "Foreløbig i tabellen."
            )
    else:
        st.info("Der er ingen numeriske værdier at tegne endnu.")
    st.subheader("Scouting")
    render_player_scouting_detail(
        manager_game.game, player_key, index, paths
    )
    st.subheader("Statushistorik")
    status_rows = []
    for snapshot in sorted(index.for_game(manager_game.game), key=lambda item: item.generated_at):
        entry = next(
            (
                item
                for item in snapshot.statistics.entries
                if player_identity(manager_game.game, item) == player_key
            ),
            None,
        )
        if entry is None:
            continue
        states = []
        if not entry.is_active:
            states.append("Inaktiv")
        if entry.is_disabled:
            states.append("Deaktiveret")
        if entry.is_injured:
            states.append("Skadet")
        if entry.has_suspension:
            states.append("Karantæne")
        status_rows.append(
            {
                "Runde": snapshot.statistics.round_number,
                "Hentet": snapshot.generated_at,
                "Spillerstatus": " · ".join(states) if states else "Aktiv",
                "Datastatus": (
                    data_status_label("final")
                    if snapshot.statistics.round_status == "complete"
                    else data_status_label("preliminary")
                ),
            }
        )
    if status_rows:
        dataframe(
            status_rows,
            hide_index=True,
            width="stretch",
            key=(
                f"player:{manager_game.game.locale}:"
                f"{manager_game.game.slug}:{player_key}:status"
            ),
        )
    else:
        st.info("Der er ingen gemte statusobservationer for spilleren.")
    snapshots = index.for_game(manager_game.game)
    st.subheader("Hvorfor ændrede denne spiller sig?")
    if len(snapshots) >= 2:
        explanation = build_player_change_explanation(
            snapshots[1], snapshots[0], player_key
        )
        if explanation is not None:
            st.caption(explanation.reconciliation_reason)
            dataframe(
                [
                    {
                        "Felt": item.field,
                        "Før": item.previous,
                        "Nu": item.current,
                        "Delta": item.delta,
                        "Evidens": (
                            "Kausalt/additivt"
                            if item.evidence == "causal"
                            else "Observeret samtidigt"
                        ),
                    }
                    for item in explanation.observations
                ],
                hide_index=True,
                key=f"player-change-explanation:{player_key}",
            )
    else:
        st.info("Der kræves mindst to lokale hentninger for en ændringsforklaring.")
    st.subheader("Egne noter og tags")
    settings_store = HubSettingsStore(paths.hub_settings_file)
    settings = settings_store.load()
    annotation = next(
        (item for item in settings.player_annotations if item.player_key == player_key),
        PlayerAnnotation(manager_game.game.locale, manager_game.game.slug, player_key),
    )
    with st.form(f"player-note-{player_key}"):
        note = st.text_area(
            "Note",
            value=annotation.note,
            max_chars=2_000,
            placeholder="Hvorfor overvejer eller undgår du spilleren?",
        )
        tags = st.text_input(
            "Tags, adskilt med komma",
            value=", ".join(annotation.tags),
            help="Standard: " + ", ".join(DEFAULT_PLAYER_TAGS) + ". Egne tags er tilladt.",
        )
        save_note = st.form_submit_button("Gem note og tags", disabled=read_only)
    if save_note:
        try:
            updated_annotation = PlayerAnnotation(
                manager_game.game.locale.casefold(),
                manager_game.game.slug,
                player_key,
                note,
                tuple(item.strip() for item in tags.split(",") if item.strip()),
                datetime.now().astimezone(),
            )
            others = tuple(
                item for item in settings.player_annotations if item.player_key != player_key
            )
            settings_store.set_player_annotations(settings, (*others, updated_annotation))
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success("Noten og tags er gemt.")
    st.subheader("Watchlist")
    render_player_watchlist_editor(
        manager_game.game,
        latest_entry,
        paths,
        read_only=read_only,
    )
    page_link(
        PageId.GAME,
        "Tilbage til spillerstatistik",
        locale=manager_game.game.locale,
        game=manager_game.game.slug,
        section="players",
    )


def alerts_view(
    paths: AppPaths,
    game: GameUrl,
    *,
    standalone: bool = False,
    read_only: bool = False,
) -> None:
    if standalone:
        st.title("Statusalarmer", anchor="statusalarmer")
        st.caption(f"Viser kun alarmer for {game.slug}.")
    else:
        st.header("Statusalarmer", anchor="statusalarmer")
    st.caption(
        "Alarmer oprettes kun efter en manuel opdatering og sammenlignes med "
        "det seneste lokale spillersnapshot."
    )

    identity = (game.locale.casefold(), game.slug)
    settings_error: str | None = None
    try:
        settings = HubSettingsStore(paths.hub_settings_file).load()
    except (OSError, PayloadError, ValueError) as exc:
        watched = ()
        settings_error = str(exc)
    else:
        watched = tuple(
            item for item in settings.watchlist if item.game_identity == identity
        )

    watchlist_label = (
        "Se watchlist" if read_only else "Administrér watchlist"
    )
    with st.container(horizontal=True, vertical_alignment="center"):
        if settings_error is None:
            suffix = "spiller" if len(watched) == 1 else "spillere"
            st.write(f"**Watchlist:** {len(watched)} {suffix}")
        page_link(
            PageId.PLAYERS if standalone else PageId.GAME,
            watchlist_label,
            icon=":material/star:",
            locale=game.locale,
            game=game.slug,
            panel="watchlist",
            **({} if standalone else {"section": "players"}),
        )
    if settings_error is not None:
        st.warning(
            "Watchlisten kunne ikke læses. Statusalarmer kan stadig vises: "
            f"{settings_error}"
        )

    store = AnalysisInboxStore(paths.analysis_inbox_file)
    try:
        alerts = tuple(
            item
            for item in store.load()
            if (item.game_locale, item.game_slug) == identity
        )
    except (OSError, PayloadError, ValueError) as exc:
        st.error(
            "Alarmindbakken kunne ikke læses. Ingen alarmer er ændret. "
            f"Detaljer: {exc}"
        )
        return

    if not alerts:
        if settings_error is None and not watched:
            st.info(
                "Der er ingen spillere på watchlisten endnu. Vælg spillere "
                "under Spillerstatistik for at få fremtidige statusalarmer."
            )
        else:
            st.info(
                "Der er ingen registrerede statusændringer for watchlisten i "
                "dette managerspil."
            )
        return

    filters = st.columns(2)
    state = filters[0].selectbox(
        "Læsestatus",
        ("Alle", "Ulæste", "Læste"),
        key=f"alert-state-{game.locale}-{game.slug}",
    )
    kind_labels = {
        "injured": "Skadet",
        "disabled": "Deaktiveret",
        "inactive": "Inaktiv",
        "suspended": "Karantæne",
        "removed": "Fjernet fra spillerlisten",
        "sold": "Solgt",
        "activated": "Aktiveret",
        "recovered": "Bedret status",
        "status_change": "Statusændring",
        "value_drop": "Prisfald",
        "value_rise": "Prisstigning",
        "form3_above": "Form 3 over",
        "form3_below": "Form 3 under",
        "form5_above": "Form 5 over",
        "form5_below": "Form 5 under",
    }
    kinds = filters[1].multiselect(
        "Hændelsestype",
        tuple(kind_labels),
        format_func=kind_labels.__getitem__,
        key=f"alert-kinds-{game.locale}-{game.slug}",
    )
    show_dismissed = st.checkbox(
        "Vis afviste alarmer",
        value=False,
        key=f"alert-dismissed-{game.locale}-{game.slug}",
    )
    visible = tuple(
        item
        for item in alerts
        if (show_dismissed or item.dismissed_at is None)
        and (state == "Alle" or (state == "Ulæste") == item.is_unread)
        and (not kinds or item.kind in kinds)
    )
    if not visible:
        st.info("Ingen statusalarmer matcher de valgte filtre.")
    for item in visible:
        with st.container(border=True):
            st.markdown(f"**{item.player_name}**")
            st.write(item.message)
            st.caption(
                f"Runde {item.round_number if item.round_number is not None else 'ukendt'} · "
                f"{item.detected_at.astimezone().strftime('%d.%m.%Y %H:%M')}"
            )
            player_url = relative_url(
                PageId.PLAYER,
                locale=game.locale,
                game=game.slug,
                player=item.player_key,
                round=item.round_number,
            )
            with st.container(horizontal=True):
                st.link_button(
                    "Se spiller",
                    player_url,
                    icon=":material/person:",
                    key=f"open-alert-player-{item.alert_id}",
                )
                if item.read_at is None:
                    st.button(
                        "Markér som læst",
                        key=f"read-alert-{item.alert_id}",
                        on_click=store.mark_read,
                        args=(item.alert_id,),
                    )
                if item.dismissed_at is None:
                    st.button(
                        "Afvis",
                        key=f"dismiss-alert-{item.alert_id}",
                        on_click=store.dismiss,
                        args=(item.alert_id,),
                    )
    if any(item.dismissed_at is not None for item in alerts):
        st.button(
            "Ryd afviste alarmer",
            type="secondary",
            key=f"clear-alerts-{game.locale}-{game.slug}",
            on_click=store.clear_dismissed,
            kwargs={"game_identity": identity},
        )
