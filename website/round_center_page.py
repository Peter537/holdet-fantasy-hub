"""Cache-only Rundecenter composition and interaction state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import streamlit as st

from holdet_lib import (
    AppPaths,
    GameMetadata,
    GameMetadataStore,
    GroupDefinition,
    HubSettings,
    HubSettingsStore,
    ManagerGame,
    ManifestStore,
    PlayerStatisticsIndex,
    PlayerStatisticsStore,
    SnapshotIndex,
    build_round_story,
    render_round_story_html,
    round_story_html_filename,
)
from holdet_lib.storage import RefreshManifest
from holdet_lib.refresh import RefreshMode, build_refresh_plan
from holdet_lib.round_center import (
    RoundCenterReadiness,
    RoundDeviation,
    TradingWindowView,
    build_group_matrix,
    build_next_best_action,
    build_round_center_readiness,
    build_round_comparison,
    build_round_deviations,
    build_trading_window_view,
    select_round_deviations,
)
from website.navigation import PageId, page_link, relative_url
from website.presentation import dataframe, format_precise_time, format_relative_precise


@dataclass(frozen=True, slots=True)
class RoundCenterUiResult:
    """User intent emitted by the cache-only page renderer."""

    selected_round: int | None
    historical: bool
    refresh_requested: bool = False
    retry_manifest: RefreshManifest | None = None


_CATEGORY_LABELS = {
    "missing_team": "Manglende hold",
    "rules_schedule": "Regler og tidsplan",
    "injury": "Skader og karantæner",
    "club": "Klubskifter",
    "rank": "Rangspring",
}
_SEVERITY_LABELS = {
    "critical": "Kræver handling",
    "warning": "Vigtigt",
    "info": "Til orientering",
}
_MANIFEST_STATUS_LABELS = {
    "fetched": "Lykkedes",
    "reused_current": "Aktuel cache genbrugt",
    "reused_after_error": "Cache genbrugt efter fejl",
    "failed_no_cache": "Fejlede uden cache",
    "skipped_unavailable": "Ikke tilgængelig",
    "not_recorded": "Ikke registreret",
    # Compatibility with an in-memory schema-1 projection.
    "success": "Lykkedes",
    "cached_fallback": "Cache genbrugt efter fejl",
    "failed": "Fejlede uden cache",
    "skipped": "Ikke tilgængelig",
}
_MANIFEST_MODE_LABELS = {
    "all": "Alle data",
    "stale_only": "Kun forældede data",
    "retry_failed": "Kun fejlede trin",
}
_MANIFEST_RESULT_LABELS = {
    "complete": "Gennemført",
    "partial": "Delvist gennemført",
    "failed": "Fejlet",
}
_STORY_STATUS_LABELS = {
    "final": "Endelig",
    "preliminary": "Foreløbig",
    "unavailable": "Datagrundlag mangler",
}


def _identity(group: GroupDefinition) -> tuple[str, str]:
    return group.game.locale.casefold(), group.game.slug


def _game_groups(
    manager_game: ManagerGame,
    groups: Iterable[GroupDefinition],
) -> tuple[GroupDefinition, ...]:
    return tuple(
        group for group in groups if _identity(group) == manager_game.identity
    )


def _team_labels(groups: Iterable[GroupDefinition]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for group in groups:
        for member in group.teams:
            labels.setdefault(member.team_id, member.name)
    return labels


def _round_end(metadata: GameMetadata | None, round_number: int) -> datetime | None:
    if metadata is None:
        return None
    return next(
        (
            item.end
            for item in metadata.rounds
            if item.round_number == round_number
        ),
        None,
    )


def available_historical_rounds(
    metadata: GameMetadata | None,
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    manager_game: ManagerGame,
    team_ids: Iterable[int],
    *,
    now: datetime | None = None,
) -> tuple[int, ...]:
    """Return ended rounds with local data, newest first."""

    current = now or datetime.now().astimezone()
    current = current.astimezone() if current.tzinfo is not None else current.astimezone()
    current_instant = current.astimezone(timezone.utc)
    wanted = tuple(sorted(set(team_ids)))
    local_rounds = set(teams.rounds_for(manager_game.game, wanted))
    local_rounds.update(players.rounds_for(manager_game.game))
    if metadata is not None:
        ended = {
            item.round_number
            for item in metadata.rounds
            if (
                item.end
                if item.end.tzinfo is not None
                else item.end.replace(tzinfo=current.tzinfo)
            ).astimezone(timezone.utc)
            <= current_instant
        }
        return tuple(sorted(local_rounds & ended, reverse=True))

    completed: list[int] = []
    for round_number in local_rounds:
        located = tuple(
            teams.summary_for(manager_game.game, team_id, round_number)
            for team_id in wanted
        )
        player = players.newest(manager_game.game, round_number)
        statuses = tuple(
            item[1].round_status for item in located if item is not None
        ) + (
            ()
            if player is None
            else (player.statistics.round_status,)
        )
        if "complete" in statuses:
            completed.append(round_number)
    return tuple(sorted(completed, reverse=True))


def _latest_local_round(
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    manager_game: ManagerGame,
    team_ids: Iterable[int],
) -> int | None:
    rounds = set(teams.rounds_for(manager_game.game, tuple(team_ids)))
    rounds.update(players.rounds_for(manager_game.game))
    return max(rounds, default=None)


def _live_target_round(
    metadata: GameMetadata | None,
    trading: TradingWindowView,
    local_round: int | None,
    *,
    now: datetime,
) -> int | None:
    """Bind live data to the latest started round, never a future window."""

    current = now.astimezone() if now.tzinfo is not None else now.astimezone()
    current_instant = current.astimezone(timezone.utc)
    started_round = None
    if metadata is not None:
        started = tuple(
            item
            for item in metadata.rounds
            if (
                item.start
                if item.start.tzinfo is not None
                else item.start.replace(tzinfo=current.tzinfo)
            ).astimezone(timezone.utc)
            <= current_instant
        )
        if started:
            started_round = max(
                started,
                key=lambda item: (
                    (
                        item.start
                        if item.start.tzinfo is not None
                        else item.start.replace(tzinfo=current.tzinfo)
                    ).astimezone(timezone.utc),
                    item.round_number,
                ),
            ).round_number
    candidates = tuple(
        item for item in (started_round, local_round) if item is not None
    )
    if candidates:
        return max(candidates)
    return trading.round_number


def _selected_round_control(
    manager_game: ManagerGame,
    historical_rounds: tuple[int, ...],
) -> tuple[int | None, bool]:
    requested_text = str(st.query_params.get("round", "")).strip()
    requested = int(requested_text) if requested_text.isdigit() else None
    valid_requested = requested if requested in historical_rounds else None
    state_key = (
        f"round-center-view:{manager_game.game.locale}:"
        f"{manager_game.game.slug}"
    )
    query_key = f"{state_key}:query"
    query_marker = requested_text
    if st.session_state.get(query_key) != query_marker:
        st.session_state[state_key] = valid_requested
        st.session_state[query_key] = query_marker

    def sync_query() -> None:
        selected = st.session_state.get(state_key)
        if selected is None:
            st.query_params.pop("round", None)
        else:
            st.query_params["round"] = str(selected)
        st.session_state[query_key] = "" if selected is None else str(selected)

    selected = st.selectbox(
        "Vis Rundecenter",
        (None, *historical_rounds),
        format_func=lambda value: (
            "Nu" if value is None else f"Runde {value} · senest korrigeret"
        ),
        key=state_key,
        on_change=sync_query,
        width=280,
    )
    invalid = (bool(requested_text) and requested is None) or (
        requested is not None and requested not in historical_rounds
    )
    return selected, invalid


def _manifest_latest(
    paths: AppPaths,
    manager_game: ManagerGame,
) -> tuple[RefreshManifest | None, tuple[str, ...]]:
    store = ManifestStore(paths.manifest_dir)
    values, warnings = store.scan(
        manager_game.game.slug,
        game_locale=manager_game.game.locale,
        scope="game",
    )
    return (values[0] if values else None), warnings


def _safe_storage_warning(warning: str) -> tuple[str, str]:
    source, separator, detail = warning.rpartition(": ")
    if not separator:
        return "ukendt fil", "filen kunne ikke læses"
    filename = Path(source).name or "ukendt fil"
    return filename, detail.replace(source, filename)


def _manifest_health(
    manifest: RefreshManifest | None,
    *,
    relevant_step_ids: frozenset[str] | None = None,
) -> tuple[datetime | None, datetime | None]:
    if manifest is None:
        return None, None
    failures = {
        "reused_after_error",
        "failed_no_cache",
        "cached_fallback",
        "failed",
    }
    successes = {"fetched", "reused_current", "success"}
    steps = tuple(
        step
        for step in manifest.steps
        if relevant_step_ids is None or step.step_id in relevant_step_ids
    )
    has_failure = any(step.status in failures for step in steps)
    last_success = (
        manifest.completed_at
        if not has_failure
        and any(step.status in successes for step in steps)
        else None
    )
    last_error = manifest.completed_at if has_failure else None
    return last_success, last_error


def _render_trading_window(trading: TradingWindowView, *, now: datetime) -> None:
    with st.container(border=True):
        st.markdown("**Handelsvindue**")
        if trading.status == "unverified":
            st.badge(
                "Ikke verificeret",
                color="gray",
                icon=":material/help:",
            )
            st.caption("Hent spilinfo for at se åbne- og lukketider.")
        elif trading.status == "open":
            st.badge("Åbent", color="green", icon=":material/lock_open:")
            st.caption(
                "Lukker "
                + format_relative_precise(trading.transition_at, now=now)
            )
        elif trading.status == "opens":
            st.badge("Lukket", color="gray", icon=":material/lock:")
            st.caption(
                "Åbner "
                + format_relative_precise(trading.transition_at, now=now)
            )
        else:
            st.badge("Lukket", color="gray", icon=":material/lock:")
            st.caption("Der er ikke publiceret et senere handelsvindue.")


def _readiness_label(readiness: RoundCenterReadiness) -> str:
    if readiness.is_stale and readiness.status == "ready":
        return "Forældet"
    if readiness.is_stale and readiness.status == "preliminary":
        return "Foreløbig · forældet"
    return {
        "ready": "Aktuel",
        "preliminary": "Foreløbig",
        "missing": "Mangler",
        "failed": "Fejlet",
        "unverified": "Ikke verificeret",
        "completed_needs_refresh": "Afsluttet · bør genhentes",
    }[readiness.status]


def _render_status_summary(
    readiness: RoundCenterReadiness,
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    manager_game: ManagerGame,
    *,
    now: datetime,
) -> None:
    team_times = [
        located[0].generated_at
        for team_id in readiness.expected_team_ids
        if (
            located := teams.summary_for(
                manager_game.game, team_id, readiness.round_number
            )
        )
        is not None
    ]
    player = players.newest(manager_game.game, readiness.round_number)
    with st.container(horizontal=True, gap="small"):
        st.metric("Runde", readiness.round_number, border=True)
        st.metric("Datastatus", _readiness_label(readiness), border=True)
        st.metric(
            "Ældste holddata",
            format_relative_precise(min(team_times, default=None), now=now),
            border=True,
        )
        st.metric(
            "Spillerdata",
            format_relative_precise(
                None if player is None else player.generated_at,
                now=now,
            ),
            border=True,
        )
        st.metric(
            "Manglende hold", len(readiness.missing_team_ids), border=True
        )
    if readiness.completed_needs_refresh:
        st.warning(
            "Runden er afsluttet, men data bør genhentes. "
            + " · ".join(readiness.reasons),
            icon=":material/sync_problem:",
        )
    elif readiness.reasons:
        st.caption(" · ".join(readiness.reasons))


def _render_next_action(
    trading: TradingWindowView,
    readiness: RoundCenterReadiness,
    manager_game: ManagerGame,
    *,
    unread_alerts: int | None,
    read_only: bool,
    now: datetime,
) -> bool:
    action = build_next_best_action(
        trading, readiness, unread_alerts=unread_alerts
    )
    refresh_requested = False
    reason = action.reason
    seconds = trading.seconds_until_transition(now=now)
    if (
        unread_alerts
        and trading.transition_kind == "closes"
        and seconds is not None
        and seconds <= 86_400
    ):
        reason = f"Deadlinekritisk: {reason}"
    with st.container(border=True):
        st.markdown("**Næste bedste handling**")
        st.subheader(action.title)
        st.write(reason)
        if read_only:
            st.caption("Handlinger er deaktiveret i denne visning.")
        elif action.kind in {"fetch_metadata", "refresh_stale"}:
            refresh_requested = st.button(
                (
                    "Hent spilinfo og data"
                    if action.kind == "fetch_metadata"
                    else "Opdater forældede data"
                ),
                type="primary",
                icon=":material/refresh:",
                key=(
                    f"round-center-action:{manager_game.game.locale}:"
                    f"{manager_game.game.slug}:{action.kind}"
                ),
            )
        elif action.kind == "review_alerts":
            page_link(
                PageId.GAME,
                "Gennemgå alarmer",
                icon=":material/notifications:",
                locale=manager_game.game.locale,
                game=manager_game.game.slug,
                section="alerts",
            )
        elif action.kind == "review_team":
            st.link_button(
                "Gennemgå hold på Holdet.dk",
                manager_game.game.original,
                icon=":material/open_in_new:",
            )
        else:
            st.caption("Der er ingen opgave, som kræver handling nu.")
    return refresh_requested


def _step_status(step: object) -> str:
    return str(getattr(step, "status", "not_recorded"))


def _render_manifest(
    manifest: RefreshManifest | None,
    warnings: tuple[str, ...],
    *,
    read_only: bool,
    relevant_step_ids: frozenset[str] | None = None,
) -> RefreshManifest | None:
    st.subheader("Seneste opdatering", anchor="seneste-opdatering")
    for warning in warnings:
        safe_source, safe_detail = _safe_storage_warning(warning)
        st.warning(
            f"Refresh-manifestet {safe_source} kunne ikke læses: {safe_detail}",
            icon=":material/warning:",
        )
    if manifest is None:
        st.caption("Der er endnu ikke registreret en opdatering for spillet.")
        return None

    visible_steps = tuple(
        step
        for step in manifest.steps
        if relevant_step_ids is None or step.step_id in relevant_step_ids
    )
    hidden_count = len(manifest.steps) - len(visible_steps)
    groups = {
        "Lykkedes": {"fetched", "success"},
        "Cache genbrugt": {"reused_current"},
        "Fejlede": {
            "reused_after_error",
            "cached_fallback",
            "failed_no_cache",
            "failed",
        },
        "Ikke tilgængelig": {"skipped_unavailable", "skipped", "not_recorded"},
    }
    with st.container(horizontal=True, gap="small"):
        for label, statuses in groups.items():
            st.metric(
                label,
                sum(_step_status(step) in statuses for step in visible_steps),
                border=True,
            )
    st.caption(
        f"Kørsel {manifest.run_id} · {format_precise_time(manifest.completed_at)} "
        f"· {_MANIFEST_MODE_LABELS.get(manifest.mode, 'Ukendt omfang')} · "
        f"{_MANIFEST_RESULT_LABELS.get(manifest.result, 'Ikke verificeret')}"
    )
    if hidden_count:
        st.caption(
            f"{hidden_count} historiske trin for hold, der ikke længere er "
            "konfigureret, er skjult."
        )
    with st.expander("Se datakilder", icon=":material/database:"):
        rows = [
            {
                "Datakilde": step.label,
                "Resultat": _MANIFEST_STATUS_LABELS.get(
                    _step_status(step), "Ikke verificeret"
                ),
                "Cache": getattr(step, "cache_reference", None),
                "Cache fra": getattr(step, "cache_generated_at", None),
                "Oprindelig kørsel": getattr(step, "origin_run_id", None),
                "Fejl": getattr(step, "error", None),
            }
            for step in visible_steps
        ]
        dataframe(
            rows,
            hide_index=True,
            key=f"round-center-manifest:{manifest.run_id}",
        )
    retryable = tuple(
        step
        for step in visible_steps
        if getattr(step, "retryable", False)
        and (
            relevant_step_ids is None
            or step.step_id in relevant_step_ids
        )
    )
    if retryable and not read_only:
        if st.button(
            "Prøv fejlede trin igen",
            icon=":material/replay:",
            key=f"round-center-retry:{manifest.run_id}",
        ):
            return manifest
    return None


def _team_group_id(
    groups: Iterable[GroupDefinition], team_id: int
) -> str | None:
    return next(
        (
            group.group_id
            for group in groups
            if any(member.team_id == team_id for member in group.teams)
        ),
        None,
    )


def _team_relative_url(
    groups: Iterable[GroupDefinition], team_id: int, round_number: int
) -> str | None:
    group_id = _team_group_id(groups, team_id)
    if group_id is None:
        return None
    return relative_url(
        PageId.TEAM,
        group=group_id,
        team=team_id,
        round=round_number,
    )


def _loopback_team_url(relative: str | None) -> str | None:
    if relative is None:
        return None
    try:
        parsed = urlsplit(str(st.context.url))
        if (parsed.hostname or "").casefold() not in {"localhost", "127.0.0.1"}:
            return None
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    except (AttributeError, ValueError):
        return None
    return origin.rstrip("/") + relative


def _metadata_changes_for_round(
    store: GameMetadataStore,
    manager_game: ManagerGame,
    round_number: int,
) -> tuple[tuple[object, ...], tuple[str, ...]]:
    revisions, warnings = store.revisions(manager_game.game)
    changes: list[object] = []
    seen: set[tuple[str, str, int | None]] = set()
    for revision in revisions:  # newest first
        for change in revision.changes:
            if change.round_number != round_number:
                continue
            identity = (change.kind, change.field, change.round_number)
            if identity in seen:
                continue
            seen.add(identity)
            changes.append(change)
    return tuple(changes), warnings


def _render_deviations(
    deviations: tuple[RoundDeviation, ...],
    groups: tuple[GroupDefinition, ...],
    manager_game: ManagerGame,
    round_number: int,
) -> None:
    st.subheader("Rundens afvigelser", anchor="rundens-afvigelser")
    options = tuple(_CATEGORY_LABELS)
    prefix = (
        f"round-center-deviations:{manager_game.game.locale}:"
        f"{manager_game.game.slug}:{round_number}"
    )
    categories_key = f"{prefix}:categories"
    top_key = f"{prefix}:top"
    st.session_state.setdefault(categories_key, list(options))
    st.session_state.setdefault(top_key, 5)
    selected_categories = st.pills(
        "Kategorier",
        options,
        selection_mode="multi",
        format_func=lambda value: _CATEGORY_LABELS[value],
        key=categories_key,
        persist_state="session",
    )
    top_n = st.segmented_control(
        "Antal rangspring",
        (5, 10, 20),
        key=top_key,
        persist_state="session",
    )
    selected = select_round_deviations(
        deviations,
        categories=selected_categories,
        limit=int(top_n or 5),
    )
    if not selected:
        st.caption("Ingen afvigelser matcher de valgte kategorier.")
        return
    rows = []
    for item in selected:
        rows.append(
            {
                "Alvor": _SEVERITY_LABELS[item.severity],
                "Kategori": _CATEGORY_LABELS[item.category],
                "Afvigelse": item.title,
                "Forklaring": item.explanation,
                "Hold": item.team_name,
                "Spiller": item.player_name,
                "Kilder": (
                    f"R{item.previous_round} → R{item.round_number}"
                    if item.previous_round is not None
                    else f"R{item.round_number}"
                ),
                "Åbn hold": (
                    ""
                    if item.team_id is None
                    else _team_relative_url(groups, item.team_id, round_number)
                    or ""
                ),
                "Foreløbig": "Ja" if item.preliminary else "Nej",
            }
        )
    dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        key=f"{prefix}:table",
        column_config={
            "Åbn hold": st.column_config.LinkColumn(
                "Åbn hold", display_text="Åbn"
            )
        },
    )


def _render_comparison(
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    manager_game: ManagerGame,
    team_ids: tuple[int, ...],
    round_number: int,
) -> None:
    comparison = build_round_comparison(
        teams, players, manager_game.game, team_ids, round_number
    )
    st.subheader("Rundesammenligning", anchor="rundesammenligning")
    if comparison.previous_round is None:
        st.caption("Der findes ingen tidligere afsluttet runde at sammenligne med.")
        return
    st.caption(
        f"Runde {comparison.current_round} mod runde {comparison.previous_round}"
        + (" · foreløbigt datagrundlag" if comparison.preliminary else "")
    )
    expected = len(set(team_ids))
    with st.container(horizontal=True, gap="small"):
        st.metric(
            f"Hold · R{comparison.current_round}",
            f"{expected - len(comparison.missing_current_team_ids)}/{expected}",
            border=True,
        )
        st.metric(
            f"Hold · R{comparison.previous_round}",
            f"{expected - len(comparison.missing_previous_team_ids)}/{expected}",
            border=True,
        )
        st.metric(
            "Spillerdata",
            f"{2 - int(comparison.missing_current_players) - int(comparison.missing_previous_players)}/2",
            border=True,
        )
    team_labels = _team_labels(groups)
    if comparison.missing_current_team_ids:
        st.warning(
            f"Runde {comparison.current_round} mangler holddata for: "
            + ", ".join(
                team_labels.get(team_id, str(team_id))
                for team_id in comparison.missing_current_team_ids
            )
        )
    if comparison.missing_previous_team_ids:
        st.warning(
            f"Runde {comparison.previous_round} mangler holddata for: "
            + ", ".join(
                team_labels.get(team_id, str(team_id))
                for team_id in comparison.missing_previous_team_ids
            )
        )
    missing_player_rounds = tuple(
        round_number
        for round_number, missing in (
            (comparison.current_round, comparison.missing_current_players),
            (comparison.previous_round, comparison.missing_previous_players),
        )
        if missing
    )
    if missing_player_rounds:
        st.warning(
            "Spillersnapshot mangler for runde "
            + ", ".join(str(item) for item in missing_player_rounds)
            + "."
        )
    rows = [
        {
            "Hold": item.team_name,
            f"Total · R{comparison.previous_round}": item.previous_total,
            f"Total · R{comparison.current_round}": item.current_total,
            "Rundevækst": item.current_change,
            "Rang før": item.previous_rank,
            "Rang nu": item.current_rank,
            "Rangspring": item.rank_movement,
            "Åbn hold": _team_relative_url(
                groups, item.team_id, comparison.current_round
            ),
        }
        for item in comparison.teams
    ]
    dataframe(
        rows,
        hide_index=True,
        key=(
            f"round-center-comparison:{manager_game.game.locale}:"
            f"{manager_game.game.slug}:{round_number}"
        ),
        column_config={
            "Åbn hold": st.column_config.LinkColumn(
                "Åbn hold", display_text="Åbn"
            )
        },
    )


def _opponent_label(row: object) -> str:
    status = str(row.opponent_status)
    if status == "scheduled":
        suffix = f" · R{row.next_round}" if row.next_round is not None else ""
        return f"{row.next_opponent_name or 'Afventer'}{suffix}"
    return {
        "bye": "Fri",
        "awaiting": "Afventer",
        "unpublished": "Ikke publiceret",
        "eliminated": "Elimineret",
        "complete": "Afsluttet",
        "no_schedule": "Ingen kampplan",
    }.get(status, "Ikke verificeret")


def _render_group_matrix(
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    manager_game: ManagerGame,
    round_number: int,
) -> None:
    st.subheader("Gruppematrix", anchor="gruppematrix")
    if not groups:
        st.caption("Managerspillet har ingen grupper.")
        return
    key = (
        f"round-center-matrix-group:{manager_game.game.locale}:"
        f"{manager_game.game.slug}"
    )
    group_ids = tuple(group.group_id for group in groups)
    if st.session_state.get(key) not in group_ids:
        st.session_state[key] = group_ids[0]
    selected_id = st.selectbox(
        "Gruppe",
        group_ids,
        format_func=lambda value: next(
            group.name for group in groups if group.group_id == value
        ),
        key=key,
        persist_state="session",
    )
    selected_group = next(
        group for group in groups if group.group_id == selected_id
    )
    matrix = build_group_matrix(selected_group, teams, round_number)
    unit = "Point" if matrix.metric == "tournament_points" else "Total"
    rows = [
        {
            "Placering": item.rank,
            "Hold": item.team_name,
            unit: item.value,
            "Afstand til leder": item.distance,
            "Næste modstander": _opponent_label(item),
            "Datastatus": item.warning,
            "Åbn hold": relative_url(
                PageId.TEAM,
                group=selected_group.group_id,
                team=item.team_id,
                round=round_number,
            ),
        }
        for item in matrix.rows
    ]
    dataframe(
        rows,
        hide_index=True,
        height=min(540, 42 + 36 * max(1, len(rows))),
        key=f"round-center-matrix:{selected_group.group_id}:{round_number}",
        column_config={
            "Åbn hold": st.column_config.LinkColumn(
                "Åbn hold", display_text="Åbn"
            )
        },
    )
    for warning in matrix.warnings:
        st.warning(warning)


def _render_story(
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    paths: AppPaths,
    manager_game: ManagerGame,
    round_number: int,
) -> None:
    try:
        settings = HubSettingsStore(paths.hub_settings_file).load()
    except Exception as exc:
        st.warning(f"Rundens historie bruger standardindstillinger: {exc}")
        settings = HubSettings()
    story = build_round_story(
        groups,
        teams,
        settings,
        manager_game.game.slug,
        round_number,
        game_locale=manager_game.game.locale,
    )
    st.subheader("Rundens historie", anchor="rundens-historie")
    if story.preliminary:
        st.warning(story.headline)
    else:
        st.markdown(f"**{story.headline}**")
    if story.facts:
        for fact in story.facts:
            with st.container(border=True):
                st.markdown(f"**{fact.label}**")
                st.write(fact.explanation)
                source = ", ".join(str(item) for item in fact.source_rounds)
                st.caption(
                    f"Status: {_STORY_STATUS_LABELS.get(fact.status, 'Ikke verificeret')} "
                    f"· Kilderunder: {source or '–'} · "
                    f"Datatid: {format_precise_time(fact.generated_at)}"
                )
                with st.container(horizontal=True, gap="small"):
                    for team in fact.teams:
                        group_id = (
                            team.group_ids[0]
                            if team.group_ids
                            else _team_group_id(groups, team.team_id)
                        )
                        if group_id is not None:
                            page_link(
                                PageId.TEAM,
                                team.team_name,
                                icon=":material/group:",
                                group=group_id,
                                team=team.team_id,
                                round=round_number,
                            )
    else:
        for paragraph in story.paragraphs:
            st.write(paragraph)

    local_urls: dict[int, str] = {}
    for fact in story.facts:
        for team in fact.teams:
            relative = _team_relative_url(groups, team.team_id, round_number)
            if (url := _loopback_team_url(relative)) is not None:
                local_urls[team.team_id] = url
    html = render_round_story_html(
        story,
        title=f"{manager_game.name} · Rundens historie · Runde {round_number}",
        hub_team_urls=local_urls,
    )
    st.download_button(
        "Download delbar HTML",
        data=html.encode("utf-8"),
        file_name=round_story_html_filename(story),
        mime="text/html",
        icon=":material/download:",
        key=(
            f"round-story-html:{manager_game.game.locale}:"
            f"{manager_game.game.slug}:{round_number}"
        ),
    )


def render_round_center(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    paths: AppPaths,
    *,
    unread_alerts: int | None = 0,
    read_only: bool = False,
    now: datetime | None = None,
) -> RoundCenterUiResult:
    """Render the complete cache-only Rundecenter and return explicit intents."""

    current = now or datetime.now().astimezone()
    game_groups = _game_groups(manager_game, groups)
    team_labels = _team_labels(game_groups)
    team_ids = tuple(team_labels)
    relevant_step_ids = frozenset(
        {"metadata", "players", "postprocess"}
        | {f"team:{team_id}" for team_id in team_ids}
    )
    players = PlayerStatisticsStore(paths.snapshot_dir).scan(manager_game.game)
    metadata_store = GameMetadataStore(paths.game_metadata_dir)
    try:
        metadata = metadata_store.load(manager_game.game)
    except Exception as exc:
        metadata = None
        st.warning(f"Spilmetadata kunne ikke læses: {exc}")
    historical_rounds = available_historical_rounds(
        metadata,
        teams,
        players,
        manager_game,
        team_ids,
        now=current,
    )
    trading = build_trading_window_view(
        () if metadata is None else metadata.rounds,
        now=current,
    )
    local_round = _latest_local_round(
        teams, players, manager_game, team_ids
    )

    refresh_requested = False
    with st.container(
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
    ):
        st.header("Rundecenter", anchor="rundecenter")
        selected_round, invalid_round = _selected_round_control(
            manager_game, historical_rounds
        )
        historical = selected_round is not None
        if historical:
            if st.button(
                "Tilbage til nu",
                icon=":material/history_toggle_off:",
                key=(
                    f"round-center-now:{manager_game.game.locale}:"
                    f"{manager_game.game.slug}"
                ),
            ):
                st.query_params.pop("round", None)
                st.session_state[
                    f"round-center-view:{manager_game.game.locale}:"
                    f"{manager_game.game.slug}"
                ] = None
                st.rerun()
        else:
            refresh_requested = st.button(
                "Opdater managerspil",
                type="secondary",
                icon=":material/refresh:",
                disabled=read_only,
                key=(
                    f"refresh-game:{manager_game.game.locale}:"
                    f"{manager_game.game.slug}"
                ),
            )
    if invalid_round:
        st.warning(
            "Den valgte runde er ikke afsluttet eller mangler lokale data. "
            "Viser Rundecenteret for nu."
        )

    if selected_round is not None:
        target_round = selected_round
        st.info(
            f"Runde {target_round} er rekonstrueret med seneste rettelser. "
            "Senere korrektioner kan ændre visningen; aktuelle gruppe- og "
            "indstillingsdefinitioner anvendes.",
            icon=":material/history:",
        )
    else:
        target_round = _live_target_round(
            metadata,
            trading,
            local_round,
            now=current,
        )
        _render_trading_window(trading, now=current)

    latest_manifest, manifest_warnings = _manifest_latest(paths, manager_game)
    if target_round is None:
        fallback_readiness = RoundCenterReadiness(
            "unverified",
            0,
            ("Der er endnu ingen lokal runde at kontrollere.",),
        )
        refresh_requested = (
            _render_next_action(
                trading,
                fallback_readiness,
                manager_game,
                unread_alerts=unread_alerts,
                read_only=read_only,
                now=current,
            )
            or refresh_requested
        )
        st.info(
            "Der er endnu ingen lokal runde at vise. Hent spilinfo og data "
            "for at opbygge Rundecenteret."
        )
        retry_manifest = _render_manifest(
            latest_manifest,
            manifest_warnings,
            read_only=read_only,
            relevant_step_ids=relevant_step_ids,
        )
        return RoundCenterUiResult(
            None,
            False,
            refresh_requested,
            retry_manifest,
        )

    health_manifest = (
        latest_manifest
        if latest_manifest is not None
        and latest_manifest.target_round == target_round
        else None
    )
    last_success, last_error = _manifest_health(
        health_manifest,
        relevant_step_ids=relevant_step_ids,
    )
    stale_source_ids: tuple[str, ...] = ()
    if selected_round is None:
        stale_plan = build_refresh_plan(
            manager_game,
            game_groups,
            teams,
            players,
            metadata=metadata,
            mode=RefreshMode.STALE_ONLY,
            previous_manifest=latest_manifest,
            include_metadata=True,
            include_postprocess=False,
            now=current,
        )
        stale_source_ids = tuple(
            step.step_id for step in stale_plan.selected_steps
        )
    readiness = build_round_center_readiness(
        manager_game.game,
        target_round,
        team_ids,
        teams,
        players,
        round_end_at=_round_end(metadata, target_round),
        now=current,
        last_success_at=last_success,
        last_error_at=last_error,
        stale_source_ids=stale_source_ids,
    )
    if selected_round is None:
        refresh_requested = (
            _render_next_action(
                trading,
                readiness,
                manager_game,
                unread_alerts=unread_alerts,
                read_only=read_only,
                now=current,
            )
            or refresh_requested
        )
    _render_status_summary(
        readiness,
        teams,
        players,
        manager_game,
        now=current,
    )

    retry_manifest = None
    if selected_round is None:
        retry_manifest = _render_manifest(
            latest_manifest,
            manifest_warnings,
            read_only=read_only,
            relevant_step_ids=relevant_step_ids,
        )

    metadata_changes, metadata_warnings = _metadata_changes_for_round(
        metadata_store, manager_game, target_round
    )
    for warning in metadata_warnings:
        safe_source, safe_detail = _safe_storage_warning(warning)
        st.warning(
            f"Metadatarevisionen {safe_source} kunne ikke læses: {safe_detail}",
            icon=":material/warning:",
        )
    deviations = build_round_deviations(
        game_groups,
        teams,
        players,
        manager_game.game,
        target_round,
        metadata_changes=metadata_changes,
    )
    _render_deviations(
        deviations, game_groups, manager_game, target_round
    )

    left, right = st.columns(2)
    with left:
        _render_comparison(
            game_groups,
            teams,
            players,
            manager_game,
            team_ids,
            target_round,
        )
    with right:
        _render_group_matrix(
            game_groups, teams, manager_game, target_round
        )
    _render_story(
        game_groups, teams, paths, manager_game, target_round
    )
    return RoundCenterUiResult(
        target_round,
        selected_round is not None,
        refresh_requested,
        retry_manifest,
    )
