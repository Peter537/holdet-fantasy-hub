"""Explicit local tool preferences and compatible one-session access."""

from __future__ import annotations

import streamlit as st

from holdet_lib.errors import PayloadError
from holdet_lib.hub_settings import HubFeature, HubSettings, HubSettingsStore, feature_enabled
from holdet_lib.paths import AppPaths
from website.navigation import PageId, page_link


FEATURE_LABELS = {
    HubFeature.SCOUTING: "Scouting og watchlists",
    HubFeature.ALERTS: "Statusalarmer",
    HubFeature.ANALYSIS: "Beslutningsanalyse og transferlaboratorium",
    HubFeature.MANAGERS: "Managers, karrierer og sæsoner",
    HubFeature.CALENDAR: "Kalender",
    HubFeature.STORIES: "Rundens historie",
}


def _settings_snapshot(paths: AppPaths) -> tuple[HubSettings, bool]:
    try:
        return HubSettingsStore(paths.hub_settings_file).load(), True
    except (OSError, PayloadError, ValueError):
        return HubSettings(), False


def _settings_warning() -> None:
    st.warning(
        "Indstillingerne kunne ikke læses. Gemte data er ikke ændret. "
        "Ret indstillingsfilen, eller gendan den fra backup under Data og lager. "
        "Du kan stadig se gemte resultater; indstillinger kan først gemmes efter gendannelse."
    )


def settings_readable(paths: AppPaths) -> bool:
    """Check whether an explicit edit can preserve all existing settings."""

    return _settings_snapshot(paths)[1]


def settings_for(paths: AppPaths) -> HubSettings:
    settings, readable = _settings_snapshot(paths)
    if not readable:
        _settings_warning()
    return settings


def tool_visible(paths: AppPaths, feature: HubFeature | str) -> bool:
    value = HubFeature(feature)
    return feature_enabled(settings_for(paths), value) or bool(
        st.session_state.get(f"tool-open-once:{value.value}", False)
    )


def require_tool(
    paths: AppPaths,
    feature: HubFeature | str,
    *,
    read_only: bool = False,
) -> bool:
    value = HubFeature(feature)
    if tool_visible(paths, value):
        return True
    st.info(f"{FEATURE_LABELS[value]} er et valgfrit værktøj. Dine gemte data er bevaret.")
    readable = settings_readable(paths)
    with st.container(horizontal=True):
        if st.button("Åbn denne gang", key=f"tool-once:{value.value}"):
            st.session_state[f"tool-open-once:{value.value}"] = True
            st.rerun()
        if st.button(
            "Aktivér værktøj", key=f"tool-enable:{value.value}",
            disabled=read_only or not readable,
        ) and not read_only and readable:
            try:
                store = HubSettingsStore(paths.hub_settings_file)
                settings = store.load()
                store.set_enabled_features(settings, (*settings.enabled_features, value))
            except (OSError, PayloadError, ValueError):
                _settings_warning()
            else:
                st.rerun()
    return False


def render_settings(paths: AppPaths) -> None:
    st.title("Indstillinger")
    st.subheader("Valgfrie værktøjer")
    st.caption("Vælg de værktøjer, du vil se. Deaktivering sletter ingen gemte data. Alarmer oprettes kun, mens Statusalarmer er aktiveret.")
    store = HubSettingsStore(paths.hub_settings_file)
    settings = settings_for(paths)
    readable = settings_readable(paths)
    with st.form("feature-preferences"):
        selected = tuple(
            feature for feature, label in FEATURE_LABELS.items()
            if st.checkbox(
                label, value=feature_enabled(settings, feature),
                key=f"feature:{feature.value}", disabled=not readable,
            )
        )
        save = st.form_submit_button("Gem værktøjer", type="primary", disabled=not readable)
    if save and readable:
        try:
            store.set_enabled_features(store.load(), selected)
        except (OSError, PayloadError, ValueError):
            _settings_warning()
        else:
            for feature in HubFeature:
                st.session_state.pop(f"tool-open-once:{feature.value}", None)
            st.session_state["feature-preferences-saved"] = True
            st.rerun()
    if st.session_state.pop("feature-preferences-saved", False):
        st.success("Værktøjsindstillingerne er gemt.")
    st.subheader("Data og administration")
    page_link(PageId.DATA, "Data, import og backup", icon=":material/database:")
    page_link(PageId.ARCHIVE, "Arkiverede managerspil", icon=":material/archive:")
    page_link(PageId.MANAGE_GAMES, "Tilføj managerspil", icon=":material/add:")
