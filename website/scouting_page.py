"""Global and contextual scouting workspaces built from local snapshots only."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import altair as alt
import pandas as pd
import streamlit as st

from holdet_lib import (
    AppPaths,
    ComputedPlayerColumn,
    GameUrl,
    HubSettings,
    HubSettingsStore,
    ManagerGame,
    PlayerAnnotation,
    PlayerEntry,
    PlayerStatisticsIndex,
    PlayerStatisticsStore,
    SnapshotStore,
    WATCHLIST_REASONS,
    WatchRule,
    WatchlistEntry,
    build_peer_comparison,
    build_scouting_metrics,
    build_smart_lists,
    evaluate_player_formula,
    find_similar_players,
    player_identity,
    watchlist_entry,
)
from website.navigation import PageId, page_link, relative_url
from website.presentation import (
    data_status_label,
    dataframe,
    format_relative_precise,
)


_VIEW_LABELS = {
    "watchlist": "Watchlist",
    "smartlists": "Smartlister",
    "notes": "Noter",
}
_REASON_LABELS = {
    "kaptajnkandidat": "Kaptajnkandidat",
    "vent på prisfald": "Vent på prisfald",
    "modstander til mit hold": "Modstander til mit hold",
}
_RULE_LABELS = {
    "status_change": "Enhver statusændring",
    "value_drop": "Prisfald",
    "value_rise": "Prisstigning",
    "form3_above": "Form 3 over",
    "form3_below": "Form 3 under",
    "form5_above": "Form 5 over",
    "form5_below": "Form 5 under",
}


def _player_status(entry: PlayerEntry | None) -> str:
    if entry is None:
        return "Mangler"
    labels: list[str] = []
    if not entry.is_active:
        labels.append("Inaktiv")
    if entry.is_disabled:
        labels.append("Deaktiveret")
    if entry.is_injured:
        labels.append("Skadet")
    if entry.has_suspension:
        labels.append("Karantæne")
    return " · ".join(labels) or "Aktiv"


def _settings(paths: AppPaths) -> tuple[HubSettingsStore, HubSettings]:
    store = HubSettingsStore(paths.hub_settings_file)
    try:
        return store, store.load()
    except Exception as exc:
        st.error(f"Scoutingindstillinger kunne ikke læses: {exc}")
        return store, HubSettings()


def _latest_entries(index: PlayerStatisticsIndex) -> dict[str, tuple[GameUrl, object]]:
    result: dict[str, tuple[GameUrl, object]] = {}
    seen: set[tuple[str, str]] = set()
    for snapshot in index.snapshots:
        game = snapshot.statistics.game
        identity = (game.locale.casefold(), game.slug)
        if identity in seen:
            continue
        seen.add(identity)
        for entry in snapshot.statistics.entries:
            result[player_identity(game, entry)] = (game, entry)
    return result


def _own_team_player_keys(
    paths: AppPaths,
    settings: HubSettings,
    game: GameUrl,
    player_index: PlayerStatisticsIndex,
) -> frozenset[str] | None:
    selection = next(
        (
            item
            for item in settings.own_teams
            if (item.game_locale.casefold(), item.game_slug)
            == (game.locale.casefold(), game.slug)
        ),
        None,
    )
    latest = player_index.newest(game)
    if selection is None or latest is None:
        return None
    team = SnapshotStore(paths.snapshot_dir).scan().newest(game, selection.team_id)
    if team is None:
        return None
    roster_ids = {item.player_id for item in team.team.roster}
    return frozenset(
        player_identity(game, entry)
        for entry in latest.statistics.entries
        if entry.entry_id in roster_ids
    )


def _metric_text(value: float | None, percentile: float | None) -> str:
    if value is None:
        return "–"
    absolute = f"{value:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return absolute if percentile is None else f"{absolute} · P{percentile:.0f}"


def _scouting_rows(metrics, game: GameUrl, watched: set[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in metrics:
        popularity = item.metric("popularity")
        rows.append(
            {
                "Spiller": item.name,
                "Hold": item.team,
                "Position": item.position,
                "Pris": item.value,
                "Prispercentil": item.metric("value").percentile,
                "Totalvækst": item.total_growth,
                "Vækstpercentil": item.metric("total_growth").percentile,
                "Form 3": _metric_text(item.form_3, item.metric("form_3").percentile),
                "Form 3-percentil": item.metric("form_3").percentile,
                "Form 5": _metric_text(item.form_5, item.metric("form_5").percentile),
                "Stabilitet": _metric_text(
                    item.stability, item.metric("stability").percentile
                ),
                "Stabilitetspercentil": item.metric("stability").percentile,
                "Popularitet": _metric_text(popularity.value, popularity.percentile),
                "Potentiale": item.potential.value,
                "Risiko": item.risk.value,
                "Ownership": item.ownership.label or "–",
                "Watchlist": item.player_key in watched,
                "Grundlag": item.completed_observations,
                "Detaljer": relative_url(
                    PageId.PLAYER,
                    locale=game.locale,
                    game=game.slug,
                    player=item.player_key,
                    panel="scouting",
                ),
                "_player_key": item.player_key,
            }
        )
    return rows


def _selected_keys(event, rows: list[dict[str, object]]) -> tuple[str, ...]:
    try:
        indexes = event.selection.rows
    except AttributeError:
        return ()
    return tuple(
        str(rows[index]["_player_key"])
        for index in indexes
        if 0 <= index < len(rows)
    )


def _rule_from_form(
    kind: str, threshold: float, threshold_unit: str
) -> WatchRule:
    return WatchRule(
        f"{kind}-{uuid4().hex[:8]}",
        kind,  # type: ignore[arg-type]
        None if kind == "status_change" else threshold,
        threshold_unit if kind.startswith("value_") else "absolute",  # type: ignore[arg-type]
    )


def _bulk_controls(
    *,
    scope: str,
    selected_keys: tuple[str, ...],
    game: GameUrl,
    entries: dict[str, object],
    store: HubSettingsStore,
    settings: HubSettings,
) -> None:
    st.caption(f"{len(selected_keys)} spillere valgt til én atomisk handling.")
    with st.form(f"bulk-scouting:{scope}", border=True):
        action = st.selectbox(
            "Handling",
            (
                "add_watchlist",
                "remove_watchlist",
                "add_tags",
                "remove_tags",
                "set_reasons",
                "clear_reasons",
                "set_rule",
                "clear_rules",
            ),
            format_func={
                "add_watchlist": "Føj til watchlist",
                "remove_watchlist": "Fjern fra watchlist",
                "add_tags": "Tilføj tags",
                "remove_tags": "Fjern tags",
                "set_reasons": "Sæt begrundelser",
                "clear_reasons": "Ryd begrundelser",
                "set_rule": "Tilføj alarmregel",
                "clear_rules": "Ryd alarmregler",
            }.get,
            key=f"bulk-action:{scope}",
        )
        tags = st.multiselect(
            "Tags",
            ("overvej", "undgå", "kaptajn", "langsigtet"),
            accept_new_options=True,
            key=f"bulk-tags:{scope}",
        )
        reasons = st.multiselect(
            "Begrundelser",
            WATCHLIST_REASONS,
            format_func=_REASON_LABELS.get,
            key=f"bulk-reasons:{scope}",
        )
        reason_note = st.text_input(
            "Fritekstbegrundelse",
            max_chars=280,
            key=f"bulk-reason-note:{scope}",
        )
        rule_kind = st.selectbox(
            "Regel",
            tuple(_RULE_LABELS),
            format_func=_RULE_LABELS.get,
            key=f"bulk-rule-kind:{scope}",
        )
        threshold = st.number_input(
            "Tærskel",
            value=1.0,
            key=f"bulk-rule-threshold:{scope}",
        )
        threshold_unit = st.segmented_control(
            "Pristærskel",
            ("absolute", "percent"),
            default="absolute",
            format_func={"absolute": "Beløb", "percent": "Procent"}.get,
            key=f"bulk-rule-unit:{scope}",
        )
        submitted = st.form_submit_button(
            "Anvend på valgte",
            type="primary",
            icon=":material/checklist:",
            disabled=not selected_keys,
        )
    if not submitted:
        return
    try:
        if any(key not in entries for key in selected_keys):
            raise ValueError("Mindst én valgt spiller findes ikke i det aktuelle snapshot")
        watch_by_key = {item.player_key: item for item in settings.watchlist}
        annotation_by_key = {
            item.player_key: item for item in settings.player_annotations
        }
        if action == "add_watchlist":
            for key in selected_keys:
                watch_by_key.setdefault(key, watchlist_entry(game, entries[key]))  # type: ignore[arg-type]
        elif action == "remove_watchlist":
            for key in selected_keys:
                watch_by_key.pop(key, None)
        elif action in {"add_tags", "remove_tags"}:
            for key in selected_keys:
                current = annotation_by_key.get(
                    key,
                    PlayerAnnotation(game.locale, game.slug, key),
                )
                values = set(current.tags)
                values.update(tags) if action == "add_tags" else values.difference_update(tags)
                annotation_by_key[key] = replace(
                    current,
                    tags=tuple(sorted(values)),
                    updated_at=datetime.now().astimezone(),
                )
        elif action in {"set_reasons", "clear_reasons", "set_rule", "clear_rules"}:
            if any(key not in watch_by_key for key in selected_keys):
                raise ValueError("Begrundelser og regler kræver, at alle valgte er på watchlist")
            rule = (
                _rule_from_form(rule_kind, float(threshold), str(threshold_unit))
                if action == "set_rule"
                else None
            )
            for key in selected_keys:
                current = watch_by_key[key]
                if action == "set_reasons":
                    current = replace(
                        current,
                        reasons=tuple(reasons),
                        reason_note=reason_note,
                    )
                elif action == "clear_reasons":
                    current = replace(current, reasons=(), reason_note="")
                elif action == "set_rule" and rule is not None:
                    current = replace(current, rules=(*current.rules, rule))
                else:
                    current = replace(current, rules=())
                watch_by_key[key] = current
        store.apply_player_bulk_update(
            settings,
            watchlist=tuple(watch_by_key.values()),
            annotations=tuple(annotation_by_key.values()),
        )
    except (OSError, ValueError) as exc:
        st.error(f"Bulkhandlingen blev ikke gemt: {exc}")
    else:
        st.toast("Alle valgte spillere blev opdateret samlet.")
        st.rerun()


def render_player_bulk_controls(
    *,
    scope: str,
    selected_keys: tuple[str, ...],
    game: GameUrl,
    entries: dict[str, object],
    paths: AppPaths,
) -> None:
    """Public UI adapter shared by player lists and the scouting table."""

    store, settings = _settings(paths)
    _bulk_controls(
        scope=scope,
        selected_keys=selected_keys,
        game=game,
        entries=entries,
        store=store,
        settings=settings,
    )


def _scatter(
    rows: list[dict[str, object]],
    *,
    x: str,
    y: str,
    title: str,
    scope: str,
) -> None:
    included = [
        row
        for row in rows
        if isinstance(row.get(x), (int, float))
        and isinstance(row.get(y), (int, float))
    ]
    excluded = len(rows) - len(included)
    st.subheader(title)
    st.caption(f"{len(included)} vist · {excluded} ekskluderet pga. manglende tal.")
    if not included:
        st.info("Der er ikke numerisk grundlag til dette plot.")
        return
    frame = pd.DataFrame(included)
    base = alt.Chart(frame).encode(
        x=alt.X(f"{x}:Q", title=x),
        y=alt.Y(f"{y}:Q", title=y),
        tooltip=[
            alt.Tooltip("Spiller:N"),
            alt.Tooltip("Hold:N"),
            alt.Tooltip(f"{x}:Q"),
            alt.Tooltip(f"{y}:Q"),
            alt.Tooltip("Prispercentil:Q", format=".0f"),
            alt.Tooltip("Vækstpercentil:Q", format=".0f"),
            alt.Tooltip("Form 3-percentil:Q", format=".0f"),
            alt.Tooltip("Stabilitetspercentil:Q", format=".0f"),
            alt.Tooltip("Form 3:N"),
            alt.Tooltip("Stabilitet:N"),
        ],
    )
    points = base.mark_point(size=80, opacity=0.8, filled=True).encode(
        color=alt.Color(
            "Highlight:N",
            title="Markering",
            scale=alt.Scale(
                domain=("Valgt", "Watchlist", "Øvrig"),
                range=("#ff4b4b", "#f3c969", "#6fa8dc"),
            ),
        ),
        shape=alt.Shape(
            "Highlight:N",
            title="Markering",
            scale=alt.Scale(
                domain=("Valgt", "Watchlist", "Øvrig"),
                range=("square", "diamond", "circle"),
            ),
        ),
    )
    labels = base.transform_filter("datum.Highlight !== 'Øvrig'").mark_text(
        align="left", dx=7, dy=-5, fontSize=10
    ).encode(text="Spiller:N")
    st.altair_chart(points + labels, key=f"scatter:{scope}:{x}:{y}")
    with st.expander(
        f"Datatabel · {title}",
        icon=":material/table_chart:",
        key=f"scatter-table-expander:{scope}:{x}:{y}",
        on_change="rerun",
    ) as expander:
        if expander.open:
            dataframe(
                [
                    {key: value for key, value in row.items() if key != "_player_key"}
                    for row in included
                ],
                hide_index=True,
                key=f"scatter-table:{scope}:{x}:{y}",
            )


def render_contextual_scouting_panel(
    game: GameUrl,
    index: PlayerStatisticsIndex,
    paths: AppPaths,
) -> None:
    latest = index.newest(game)
    if latest is None:
        st.info("Der findes endnu intet lokalt spillersnapshot.")
        return
    store, settings = _settings(paths)
    metrics = build_scouting_metrics(
        index,
        game,
        own_team_player_keys=_own_team_player_keys(paths, settings, game, index),
    )
    watched = {
        item.player_key
        for item in settings.watchlist
        if item.game_identity == (game.locale.casefold(), game.slug)
    }
    table_rows = _scouting_rows(metrics, game, watched)
    computed_columns = tuple(
        item
        for item in settings.computed_player_columns
        if (item.game_locale, item.game_slug)
        == (game.locale.casefold(), game.slug)
    )
    formula_errors = 0
    entry_by_key = {
        player_identity(game, entry): entry for entry in latest.statistics.entries
    }
    for row, item in zip(table_rows, metrics, strict=True):
        entry = entry_by_key[item.player_key]
        context = {
            "value": entry.value,
            "total_growth": entry.total_growth,
            "round_growth": entry.round_growth,
            "popularity": entry.popularity,
            "popularity_change": entry.popularity_change,
            "trend": entry.trend,
            "index": entry.index,
            "form_3": item.form_3,
            "form_5": item.form_5,
            "stability": item.stability,
            "growth_per_million": item.metric("growth_per_million").value,
            "potential": item.potential.value,
            "risk": item.risk.value,
            "is_active": entry.is_active,
            "is_disabled": entry.is_disabled,
            "is_injured": entry.is_injured,
            "has_suspension": entry.has_suspension,
        }
        for column in computed_columns:
            result = evaluate_player_formula(column.expression, context)
            row[column.name] = result.value
            formula_errors += result.error is not None
    chart_rows = [
        {
            **row,
            "Form 3-værdi": item.form_3,
            "Totalvækst": item.total_growth,
            "Stabilitet-værdi": item.stability,
        }
        for row, item in zip(table_rows, metrics, strict=True)
    ]
    scope = f"{game.locale}:{game.slug}"
    with st.container(horizontal=True):
        st.metric("Status", data_status_label(latest.statistics.round_status), border=True)
        st.metric("Snapshotalder", format_relative_precise(latest.generated_at), border=True)
        st.metric(
            "Sikkerhed",
            "Endelig" if latest.statistics.round_status == "complete" else "Foreløbig",
            border=True,
        )
    if computed_columns:
        st.caption(
            f"{len(computed_columns)} beregnede kolonner · "
            f"{formula_errors} tomme celler med formelfejl."
        )
    event = dataframe(
        [{key: value for key, value in row.items() if key != "_player_key"} for row in table_rows],
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        column_config={
            "Potentiale": st.column_config.ProgressColumn(min_value=0, max_value=100),
            "Risiko": st.column_config.ProgressColumn(min_value=0, max_value=100),
            "Detaljer": st.column_config.LinkColumn(display_text="Åbn spiller"),
        },
        key=f"scouting-table:{scope}:v1",
    )
    selected = _selected_keys(event, table_rows)
    selected_set = set(selected)
    for row in chart_rows:
        row["Highlight"] = (
            "Valgt"
            if row["_player_key"] in selected_set
            else "Watchlist"
            if bool(row["Watchlist"])
            else "Øvrig"
        )
    entries = {
        player_identity(game, entry): entry for entry in latest.statistics.entries
    }
    with st.expander(
        f"Bulkhandlinger · {len(selected)} valgt",
        icon=":material/checklist:",
        key=f"scouting-bulk-expander:{scope}",
        on_change="rerun",
    ) as expander:
        if expander.open:
            _bulk_controls(
                scope=scope,
                selected_keys=selected,
                game=game,
                entries=entries,
                store=store,
                settings=settings,
            )
    _scatter(chart_rows, x="Pris", y="Form 3-værdi", title="Pris mod Form 3", scope=scope)
    _scatter(
        chart_rows,
        x="Totalvækst",
        y="Stabilitet-værdi",
        title="Vækst mod stabilitet",
        scope=scope,
    )
    _scatter(chart_rows, x="Potentiale", y="Risiko", title="Potentiale mod risiko", scope=scope)


def render_contextual_watchlist_panel(
    game: GameUrl,
    index: PlayerStatisticsIndex,
    paths: AppPaths,
) -> None:
    latest = index.newest(game)
    if latest is None:
        st.info("Der findes endnu intet lokalt spillersnapshot.")
        return
    store, settings = _settings(paths)
    entries = {
        player_identity(game, entry): entry for entry in latest.statistics.entries
    }
    watched = tuple(
        item
        for item in settings.watchlist
        if item.game_identity == (game.locale.casefold(), game.slug)
    )
    if not watched:
        st.info("Watchlisten er tom. Vælg spillere under Scouting.")
        page_link(
            PageId.SCOUTING,
            "Åbn global scouting",
            icon=":material/travel_explore:",
            game=game.slug,
            locale=game.locale,
            view="watchlist",
        )
        return
    rows = [
        {
            "Spiller": item.name,
            "Hold": item.team,
            "Position": item.position,
            "Aktuel pris": getattr(entries.get(item.player_key), "value", None),
            "Begrundelser": " · ".join(_REASON_LABELS.get(reason, reason) for reason in item.reasons) or "–",
            "Fritekst": item.reason_note or "–",
            "Regler": " · ".join(_RULE_LABELS[rule.kind] for rule in item.rules) or "–",
            "Status": _player_status(entries.get(item.player_key)),
            "Snapshotalder": format_relative_precise(latest.generated_at),
            "Sikkerhed": (
                "Mangler"
                if item.player_key not in entries
                else data_status_label(
                    "final"
                    if latest.statistics.round_status == "complete"
                    else "preliminary"
                )
            ),
            "Detaljer": relative_url(
                PageId.PLAYER,
                locale=game.locale,
                game=game.slug,
                player=item.player_key,
                panel="watchlist",
            ),
        }
        for item in watched
    ]
    dataframe(
        rows,
        hide_index=True,
        column_config={"Detaljer": st.column_config.LinkColumn(display_text="Åbn spiller")},
        key=f"watchlist:{game.locale}:{game.slug}:v1",
    )
    selected_key = st.selectbox(
        "Redigér spiller",
        tuple(item.player_key for item in watched),
        format_func=lambda key: next(item.name for item in watched if item.player_key == key),
        key=f"watchlist-editor-player:{game.locale}:{game.slug}",
    )
    current = next(item for item in watched if item.player_key == selected_key)
    with st.form(f"watchlist-editor:{game.locale}:{game.slug}:{selected_key}"):
        reasons = st.multiselect(
            "Begrundelser",
            WATCHLIST_REASONS,
            default=current.reasons,
            format_func=_REASON_LABELS.get,
        )
        note = st.text_area(
            "Fritekst",
            value=current.reason_note,
            max_chars=280,
        )
        st.markdown("**Alarmregler**")
        existing_rule_ids = st.multiselect(
            "Aktive regler",
            tuple(rule.rule_id for rule in current.rules),
            default=tuple(rule.rule_id for rule in current.rules),
            format_func=lambda rule_id: next(
                f"{_RULE_LABELS[rule.kind]}"
                + (
                    ""
                    if rule.threshold is None
                    else f" · {rule.threshold:g}{' %' if rule.threshold_unit == 'percent' else ''}"
                )
                for rule in current.rules
                if rule.rule_id == rule_id
            ),
            help="Fjern en markering for at slette reglen.",
        )
        add_rule = st.checkbox("Tilføj en regel")
        rule_kind = st.selectbox(
            "Ny regel",
            tuple(_RULE_LABELS),
            format_func=_RULE_LABELS.get,
            disabled=not add_rule,
        )
        threshold = st.number_input(
            "Ny tærskel",
            value=1.0,
            disabled=not add_rule or rule_kind == "status_change",
        )
        threshold_unit = st.segmented_control(
            "Ny pristærskel",
            ("absolute", "percent"),
            default="absolute",
            format_func={"absolute": "Beløb", "percent": "Procent"}.get,
            disabled=not add_rule or not rule_kind.startswith("value_"),
        )
        save = st.form_submit_button("Gem watchlist", type="primary")
    if save:
        try:
            rules = tuple(
                rule for rule in current.rules if rule.rule_id in existing_rule_ids
            )
            if add_rule:
                rules = (
                    *rules,
                    _rule_from_form(
                        rule_kind,
                        float(threshold),
                        str(threshold_unit),
                    ),
                )
            selected = replace(
                current,
                reasons=tuple(reasons),
                reason_note=note,
                rules=rules,
            )
            updated = tuple(
                selected if item.player_key == selected_key else item
                for item in settings.watchlist
            )
            store.set_watchlist(settings, updated)
        except (OSError, ValueError) as exc:
            st.error(str(exc))
        else:
            st.rerun()


def render_player_watchlist_editor(
    game: GameUrl,
    entry: PlayerEntry,
    paths: AppPaths,
    *,
    read_only: bool = False,
) -> None:
    """Render the complete single-player watchlist editor."""

    player_key = player_identity(game, entry)
    store, settings = _settings(paths)
    watched = next(
        (item for item in settings.watchlist if item.player_key == player_key),
        None,
    )
    if watched is None:
        if st.button(
            "Føj til watchlist",
            key=f"player-watchlist-add:{player_key}",
            disabled=read_only,
            type="primary",
        ):
            store.set_watchlist(
                settings, (*settings.watchlist, watchlist_entry(game, entry))
            )
            st.rerun()
        return
    with st.form(f"player-watchlist-editor:{player_key}"):
        reasons = st.multiselect(
            "Begrundelser",
            WATCHLIST_REASONS,
            default=watched.reasons,
            format_func=_REASON_LABELS.get,
            disabled=read_only,
        )
        note = st.text_area(
            "Watchlist-fritekst",
            value=watched.reason_note,
            max_chars=280,
            disabled=read_only,
        )
        keep_rule_ids = st.multiselect(
            "Alarmregler",
            tuple(rule.rule_id for rule in watched.rules),
            default=tuple(rule.rule_id for rule in watched.rules),
            format_func=lambda rule_id: next(
                f"{_RULE_LABELS[rule.kind]}"
                + (
                    ""
                    if rule.threshold is None
                    else f" · {rule.threshold:g}{' %' if rule.threshold_unit == 'percent' else ''}"
                )
                for rule in watched.rules
                if rule.rule_id == rule_id
            ),
            help="Fjern en markering for at slette reglen.",
            disabled=read_only,
        )
        add_rule = st.checkbox("Tilføj alarmregel", disabled=read_only)
        rule_kind = st.selectbox(
            "Regeltype",
            tuple(_RULE_LABELS),
            format_func=_RULE_LABELS.get,
            disabled=read_only or not add_rule,
        )
        threshold = st.number_input(
            "Tærskel",
            value=1.0,
            disabled=read_only or not add_rule or rule_kind == "status_change",
        )
        threshold_unit = st.segmented_control(
            "Pristærskel",
            ("absolute", "percent"),
            default="absolute",
            format_func={"absolute": "Beløb", "percent": "Procent"}.get,
            disabled=read_only or not add_rule or not rule_kind.startswith("value_"),
        )
        save = st.form_submit_button(
            "Gem watchlist", type="primary", disabled=read_only
        )
    if save:
        try:
            rules = tuple(
                rule for rule in watched.rules if rule.rule_id in keep_rule_ids
            )
            if add_rule:
                rules = (
                    *rules,
                    _rule_from_form(
                        rule_kind,
                        float(threshold),
                        str(threshold_unit),
                    ),
                )
            selected = replace(
                watched,
                reasons=tuple(reasons),
                reason_note=note,
                rules=rules,
            )
            store.set_watchlist(
                settings,
                tuple(
                    selected if item.player_key == player_key else item
                    for item in settings.watchlist
                ),
            )
        except (OSError, ValueError) as exc:
            st.error(str(exc))
        else:
            st.rerun()
    if st.button(
        "Fjern fra watchlist",
        key=f"player-watchlist-remove:{player_key}",
        disabled=read_only,
    ):
        store.set_watchlist(
            settings,
            tuple(item for item in settings.watchlist if item.player_key != player_key),
        )
        st.rerun()


def _smartlists_view(game: GameUrl, index: PlayerStatisticsIndex) -> None:
    latest = index.newest(game)
    if latest is None:
        st.info("Der findes endnu intet lokalt spillersnapshot.")
        return
    keyed = {
        player_identity(game, entry): entry for entry in latest.statistics.entries
    }
    for smartlist in build_smart_lists(index, game):
        st.subheader(smartlist.label)
        if not smartlist.player_keys:
            st.caption("Ingen spillere matcher listen i den aktuelle cache.")
            continue
        dataframe(
            [
                {
                    "Spiller": keyed[key].name,
                    "Hold": keyed[key].team,
                    "Position": keyed[key].position,
                    "Pris": keyed[key].value,
                    "Detaljer": relative_url(
                        PageId.PLAYER,
                        locale=game.locale,
                        game=game.slug,
                        player=key,
                        panel="scouting",
                    ),
                }
                for key in smartlist.player_keys
                if key in keyed
            ],
            hide_index=True,
            column_config={"Detaljer": st.column_config.LinkColumn(display_text="Åbn spiller")},
            key=f"smartlist:{game.locale}:{game.slug}:{smartlist.list_id}",
        )


def _notes_view(paths: AppPaths, settings: HubSettings, index: PlayerStatisticsIndex) -> None:
    query = st.text_input(
        "Søg i noter, tags, begrundelser, spiller, hold eller position",
        placeholder="Søg på tværs af alle spil",
        key="global-scouting-note-search",
    ).strip().casefold()
    current = _latest_entries(index)
    watched = {item.player_key: item for item in settings.watchlist}
    annotations = {item.player_key: item for item in settings.player_annotations}
    keys = sorted(set(watched) | set(annotations))
    rows: list[dict[str, object]] = []
    for key in keys:
        annotation = annotations.get(key)
        watch = watched.get(key)
        located = current.get(key)
        entry = located[1] if located is not None else None
        game = located[0] if located is not None else None
        values = (
            annotation.note if annotation else "",
            " ".join(annotation.tags) if annotation else "",
            " ".join(watch.reasons) if watch else "",
            watch.reason_note if watch else "",
            getattr(entry, "name", watch.name if watch else key),
            getattr(entry, "team", watch.team if watch else "–"),
            getattr(entry, "position", watch.position if watch else "–"),
        )
        if query and query not in " ".join(values).casefold():
            continue
        locale = game.locale if game else annotation.game_locale if annotation else watch.game_locale
        slug = game.slug if game else annotation.game_slug if annotation else watch.game_slug
        rows.append(
            {
                "Spil": slug,
                "Spiller": values[4],
                "Hold": values[5],
                "Position": values[6],
                "Note": values[0] or "–",
                "Tags": values[1] or "–",
                "Begrundelser": " · ".join(_REASON_LABELS.get(item, item) for item in (watch.reasons if watch else ())) or "–",
                "Aktuelle data": "Mangler" if entry is None else "Tilgængelige",
                "Detaljer": relative_url(
                    PageId.PLAYER,
                    locale=locale,
                    game=slug,
                    player=key,
                    panel="watchlist",
                ),
            }
        )
    if rows:
        dataframe(
            rows,
            hide_index=True,
            column_config={"Detaljer": st.column_config.LinkColumn(display_text="Åbn spiller")},
            key="global-scouting-notes:v1",
        )
    else:
        st.info("Ingen noter eller watchlistposter matcher søgningen.")


def _formula_editor(
    game: GameUrl, store: HubSettingsStore, settings: HubSettings
) -> None:
    with st.expander(
        "Egne beregnede kolonner",
        icon=":material/calculate:",
        key=f"computed-columns-expander:{game.locale}:{game.slug}",
        on_change="rerun",
    ) as expander:
        if not expander.open:
            return
        selected = tuple(
            item
            for item in settings.computed_player_columns
            if (item.game_locale, item.game_slug)
            == (game.locale.casefold(), game.slug)
        )
        st.caption(
            "Sikker formelmotor: kendte metrikker, regneoperatorer, sammenligninger "
            "og abs/min/max/round/coalesce/clamp/ifelse. Ingen vilkårlig Python."
        )
        if selected:
            dataframe(
                [
                    {"Navn": item.name, "Formel": item.expression, "Decimaler": item.decimals}
                    for item in selected
                ],
                hide_index=True,
                key=f"computed-columns:{game.locale}:{game.slug}",
            )
            remove_column = st.selectbox(
                "Slet beregnet kolonne",
                tuple(item.column_id for item in selected),
                format_func=lambda column_id: next(
                    item.name for item in selected if item.column_id == column_id
                ),
                key=f"computed-column-remove:{game.locale}:{game.slug}",
            )
            if st.button(
                "Slet kolonne",
                key=f"computed-column-remove-button:{game.locale}:{game.slug}",
            ):
                store.set_computed_player_columns(
                    settings,
                    tuple(
                        item
                        for item in settings.computed_player_columns
                        if not (
                            item.column_id == remove_column
                            and (item.game_locale, item.game_slug)
                            == (game.locale.casefold(), game.slug)
                        )
                    ),
                )
                st.rerun()
        with st.form(f"computed-column-form:{game.locale}:{game.slug}"):
            name = st.text_input("Kolonnenavn")
            expression = st.text_input("Formel", placeholder="form_3 / coalesce(value, 1)")
            decimals = st.number_input("Decimaler", min_value=0, max_value=8, value=2)
            save = st.form_submit_button(
                "Tilføj kolonne", type="primary", disabled=len(selected) >= 20
            )
        if save:
            try:
                column = ComputedPlayerColumn(
                    game.locale,
                    game.slug,
                    f"custom-{uuid4().hex[:12]}",
                    name,
                    expression,
                    int(decimals),
                )
                store.set_computed_player_columns(
                    settings, (*settings.computed_player_columns, column)
                )
            except (OSError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.rerun()


def render_scouting_page(games: tuple[ManagerGame, ...], paths: AppPaths) -> None:
    st.title("Scouting", anchor="scouting")
    st.caption(
        "Global watchlist, dynamiske smartlister og søgbare noter. Alt læses fra "
        "den lokale cache; navigation henter eller skriver ikke data."
    )
    index = PlayerStatisticsStore(paths.snapshot_dir).scan()
    suggestions: dict[tuple[str, str], tuple[GameUrl, str]] = {
        game.identity: (game.game, game.name) for game in games
    }
    for snapshot in index.snapshots:
        game = snapshot.statistics.game
        suggestions.setdefault((game.locale.casefold(), game.slug), (game, game.slug))
    if not suggestions:
        st.info("Der er ingen cachede spil at scoute endnu.")
        return
    ordered = tuple(sorted(suggestions.values(), key=lambda item: item[1].casefold()))
    requested_slug = str(st.query_params.get("game", ""))
    default = next(
        (item[0].original for item in ordered if item[0].slug == requested_slug),
        ordered[0][0].original,
    )
    game_url = st.selectbox(
        "Spil",
        tuple(item[0].original for item in ordered),
        index=tuple(item[0].original for item in ordered).index(default),
        format_func=lambda value: next(
            f"{label} · {game.slug}" for game, label in ordered if game.original == value
        ),
        key="global-scouting-game",
    )
    game = next(item[0] for item in ordered if item[0].original == game_url)
    st.query_params["game"] = game.slug
    st.query_params["locale"] = game.locale
    requested_view = str(st.query_params.get("view", "watchlist"))
    if requested_view not in _VIEW_LABELS:
        requested_view = "watchlist"
    view_label = st.segmented_control(
        "Visning",
        tuple(_VIEW_LABELS.values()),
        default=_VIEW_LABELS[requested_view],
        key="global-scouting-view",
    )
    view = next(key for key, label in _VIEW_LABELS.items() if label == view_label)
    st.query_params["view"] = view
    store, settings = _settings(paths)
    if view == "watchlist":
        render_contextual_watchlist_panel(game, index, paths)
        _formula_editor(game, store, settings)
    elif view == "smartlists":
        _smartlists_view(game, index)
    else:
        _notes_view(paths, settings, index)


def render_player_scouting_detail(
    game: GameUrl,
    player_key: str,
    index: PlayerStatisticsIndex,
    paths: AppPaths,
) -> None:
    """Compact peer/similarity/decomposition section for player detail pages."""

    _, settings = _settings(paths)
    metrics = build_scouting_metrics(
        index,
        game,
        own_team_player_keys=_own_team_player_keys(paths, settings, game, index),
    )
    target = next((item for item in metrics if item.player_key == player_key), None)
    if target is None:
        st.info("Scoutingmetrikker er ikke tilgængelige for spilleren.")
        return
    headline = st.columns(3)
    headline[0].metric(
        "Potentiale",
        "–" if target.potential.value is None else f"{target.potential.value:.0f}/100",
        border=True,
    )
    headline[1].metric(
        "Risiko",
        "–" if target.risk.value is None else f"{target.risk.value:.0f}/100",
        border=True,
    )
    headline[2].metric(
        "Ownership", target.ownership.label or "Ikke tilgængelig", border=True
    )
    evidence = st.columns(2)
    evidence[0].metric(
            "Ownership-risiko",
            "Ikke tilgængelig"
            if target.ownership.ownership_risk is None
            else f"{target.ownership.ownership_risk:.1f}",
            border=True,
        )
    evidence[1].metric("Grundlag", target.completed_observations, border=True)
    peer = build_peer_comparison(metrics, player_key)
    if peer is not None:
        st.subheader("Position og prisalternativer")
        dataframe(
            [
                {
                    "Metrik": name,
                    "Spiller": (
                        target.metric(name).value
                        if name not in {"form_3", "form_5", "stability"}
                        else getattr(target, name)
                    ),
                    "Percentil": target.metric(name).percentile,
                    "Positionsmedian": value,
                    "Kohorte": dict(peer.cohort_sizes)[name],
                }
                for name, value in peer.medians
            ],
            hide_index=True,
            key=f"player-position-medians:{player_key}",
        )
        dataframe(
            [
                {
                    "Spiller": item.name,
                    "Hold": item.team,
                    "Pris": item.value,
                    "Prisforskel": item.price_delta,
                    "Form 3": item.form_3,
                    "Detaljer": relative_url(
                        PageId.PLAYER,
                        locale=game.locale,
                        game=game.slug,
                        player=item.player_key,
                        panel="scouting",
                    ),
                }
                for item in peer.alternatives
            ],
            hide_index=True,
            column_config={"Detaljer": st.column_config.LinkColumn(display_text="Sammenlign")},
            key=f"player-peer:{player_key}",
        )
    similar = find_similar_players(metrics, player_key)
    st.subheader("Lignende spillere")
    if similar:
        dataframe(
            [
                {
                    "Spiller": item.name,
                    "Hold": item.team,
                    "Afstand": item.distance,
                    "Pris P-delta": item.price_percentile_delta,
                    "Form P-delta": item.form_3_percentile_delta,
                    "Stabilitet P-delta": item.stability_percentile_delta,
                }
                for item in similar
            ],
            hide_index=True,
            key=f"player-similar:{player_key}",
        )
    else:
        st.caption("Mindst to fælles percentilmetrikker kræves.")
    with st.expander("Scoredecomposition", icon=":material/account_tree:"):
        dataframe(
            [
                {
                    "Score": score_name,
                    "Komponent": component.name,
                    "Værdi": component.value,
                    "Vægt": component.weight,
                    "Bidrag": component.contribution,
                    "Manglende grundlag": component.missing_reason or "–",
                }
                for score_name, score in (("Potentiale", target.potential), ("Risiko", target.risk))
                for component in score.components
            ],
            hide_index=True,
            key=f"player-score-decomposition:{player_key}",
        )
