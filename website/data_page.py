"""Data-and-storage page for the local Streamlit dashboard."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from holdet_lib._formatting import count_label
from holdet_lib.accounts import AccountStore, rename_account_and_groups
from holdet_lib.errors import PayloadError
from holdet_lib.groups import GroupDefinition, GroupStore, HubConfiguration
from holdet_lib.hall_of_fame import HallOfFameStore
from holdet_lib.models import AccountConfig
from holdet_lib.seasons import SeasonStore
from holdet_lib.tournament_pairings import TournamentPairingStore
from holdet_lib.paths import AppPaths, open_in_explorer
from holdet_lib.storage import SnapshotIndex
from website.hub_pages import backup_view, data_quality_panel

def _local_store_health(
    group_store: GroupStore,
    configuration: HubConfiguration,
    app_paths: AppPaths,
) -> list[dict[str, str]]:
    """Read each additive store independently and report bounded failures."""

    rows: list[dict[str, str]] = []

    try:
        _, revision_warnings = group_store.load_configuration_with_warnings()
    except (OSError, PayloadError, ValueError) as exc:
        rows.append({
            "Lager": "Turneringsrevisioner",
            "Status": "Fejl",
            "Detalje": str(exc),
        })
    else:
        rows.append({
            "Lager": "Turneringsrevisioner",
            "Status": "Advarsel" if revision_warnings else "OK",
            "Detalje": (
                " | ".join(revision_warnings)
                if revision_warnings
                else "Alle arkiverede revisioner kan læses."
            ),
        })

    try:
        seasons = SeasonStore(app_paths.seasons_file).load()
    except (OSError, PayloadError, ValueError) as exc:
        rows.append({"Lager": "Sæsoner", "Status": "Fejl", "Detalje": str(exc)})
    else:
        rows.append({
            "Lager": "Sæsoner",
            "Status": "OK",
            "Detalje": f"{len(seasons)} sæsondefinitioner kan læses.",
        })

    events, ledger_warnings = HallOfFameStore(app_paths.hall_of_fame_dir).scan()
    rows.append({
        "Lager": "Managerhistorik",
        "Status": "Advarsel" if ledger_warnings else "OK",
        "Detalje": (
            " | ".join(ledger_warnings)
            if ledger_warnings
            else f"{len(events)} eventrevisioner kan læses."
        ),
    })

    pairing_store = TournamentPairingStore(app_paths.tournament_pairing_dir)
    pairing_warnings: list[str] = []
    pairing_count = 0
    for group in configuration.groups:
        config = group.tournament
        if config is None or config.template != "swiss":
            continue
        try:
            revision = pairing_store.load_for_tournament(
                group.group_id,
                group.active_revision,
                config,
                tuple(item.team_id for item in group.teams),
            )
        except (OSError, PayloadError, ValueError) as exc:
            pairing_warnings.append(f"{group.name}: {exc}")
        else:
            pairing_count += len(revision.pairings)
    rows.append({
        "Lager": "Turneringsparringer",
        "Status": "Advarsel" if pairing_warnings else "OK",
        "Detalje": (
            " | ".join(pairing_warnings)
            if pairing_warnings
            else f"{pairing_count} publicerede parringer kan læses."
        ),
    })
    return rows


def _clear_account_discovery_cache() -> None:
    st.session_state.pop("discovered_teams", None)
    st.session_state.pop("discovery_warnings", None)


def _account_membership_count(
    account_key: str, groups: tuple[GroupDefinition, ...]
) -> int:
    return sum(
        team.account_key == account_key
        for group in groups
        for team in group.teams
    )


def _clear_pending_account_dialog() -> None:
    st.session_state.pop("pending_account_dialog", None)


@st.dialog("Tilføj konto", on_dismiss=_clear_pending_account_dialog)
def _add_account_dialog(store: AccountStore) -> None:
    label = st.text_input("Visningsnavn", key="new-account-label")
    profile_url = st.text_input(
        "Holdet-profil-URL",
        placeholder="https://www.holdet.dk/da/users/123456/teams",
        key="new-account-profile-url",
    )
    preview: AccountConfig | None = None
    error: str | None = None
    if label.strip() and profile_url.strip():
        try:
            _, preview = store.prepare_create(label, profile_url)
        except (PayloadError, ValueError) as exc:
            error = str(exc)
    if preview is not None:
        st.caption(
            f"Bruger-ID: {preview.user_id} · teknisk nøgle: `{preview.key}`"
        )
    elif error:
        st.error(error)
    else:
        st.caption("Nøgle og bruger-ID beregnes automatisk uden netværkskald.")
    if st.button(
        "Gem konto", type="primary", disabled=preview is None, width="stretch"
    ):
        try:
            store.create(label, profile_url)
        except (PayloadError, ValueError) as exc:
            st.error(str(exc))
        else:
            _clear_account_discovery_cache()
            st.session_state.pop("pending_account_dialog", None)
            st.session_state.pop("new-account-label", None)
            st.session_state.pop("new-account-profile-url", None)
            st.toast("Kontoen blev tilføjet.")
            st.rerun()
    if st.button("Annuller", key="cancel-add-account"):
        st.session_state.pop("pending_account_dialog", None)
        st.session_state.pop("new-account-label", None)
        st.session_state.pop("new-account-profile-url", None)
        st.rerun()


@st.dialog("Omdøb konto", on_dismiss=_clear_pending_account_dialog)
def _rename_account_dialog(
    account_store: AccountStore,
    group_store: GroupStore,
    account: AccountConfig,
    membership_count: int,
) -> None:
    st.caption(
        f"Nøgle `{account.key}` og bruger-ID {account.user_id} forbliver uændret."
    )
    if membership_count:
        membership_label = count_label(
            membership_count,
            "eksisterende gruppemedlemskab",
            "eksisterende gruppemedlemskaber",
        )
        st.info(f"Navnet opdateres også i {membership_label}.")
    label = st.text_input(
        "Visningsnavn", value=account.label, key=f"rename-account-{account.key}"
    )
    if st.button(
        "Gem navn",
        type="primary",
        disabled=not label.strip() or label.strip() == account.label,
        width="stretch",
    ):
        try:
            rename_account_and_groups(
                account_store, group_store, account.key, label
            )
        except (PayloadError, OSError, ValueError) as exc:
            st.error(str(exc))
        else:
            _clear_account_discovery_cache()
            st.session_state.pop("pending_account_dialog", None)
            st.toast("Kontonavnet blev gemt.")
            st.rerun()
    if st.button("Annuller", key="cancel-rename-account"):
        st.session_state.pop("pending_account_dialog", None)
        st.rerun()


@st.dialog("Slet konto", on_dismiss=_clear_pending_account_dialog)
def _delete_account_dialog(
    store: AccountStore,
    account: AccountConfig,
    membership_count: int,
) -> None:
    st.warning(
        "Kontoen fjernes kun fra fremtidig kontoopdagelse. "
        "Eksisterende grupper, snapshots og historiske data bevares."
    )
    st.write(f"**Konto:** {account.label}")
    st.write(f"**Bevarede gruppemedlemskaber:** {membership_count}")
    if st.button("Bekræft sletning", type="primary", width="stretch"):
        try:
            store.delete(account.key)
        except (PayloadError, OSError) as exc:
            st.error(str(exc))
        else:
            _clear_account_discovery_cache()
            st.session_state.pop("pending_account_dialog", None)
            st.toast("Kontoen blev slettet.")
            st.rerun()
    if st.button("Annuller", key="cancel-delete-account"):
        st.session_state.pop("pending_account_dialog", None)
        st.rerun()


def _storage_locations_tab(app_paths: AppPaths) -> None:
    locations = (
        ("Konfiguration", app_paths.config_dir, "config"),
        ("Snapshots", app_paths.snapshot_dir, "snapshots"),
        ("Manifester", app_paths.manifest_dir, "manifests"),
        ("Turneringsrevisioner", app_paths.group_revision_dir, "revisions"),
        ("Eksporter", app_paths.export_dir, "exports"),
    )
    for label, path, key in locations:
        with st.container(border=True):
            st.subheader(label)
            st.code(str(Path(path).resolve()), language=None)
            if st.button(
                "Åbn mappe i Stifinder",
                key=f"open-storage-{key}",
                icon=":material/folder_open:",
            ):
                if open_in_explorer(path):
                    st.success("Mappen blev åbnet i Windows Stifinder.")
                else:
                    st.warning(
                        "Windows Stifinder kunne ikke åbnes. Brug stien ovenfor."
                    )


def _saved_accounts_tab(
    account_store: AccountStore,
    group_store: GroupStore,
    accounts: tuple[AccountConfig, ...] | None,
    groups: tuple[GroupDefinition, ...],
    account_error: str | None,
) -> None:
    if account_error is not None:
        st.error(
            "Kontofilen kunne ikke læses og kan derfor ikke overskrives fra "
            f"dashboardet: {account_error}"
        )
        return
    assert accounts is not None
    pending = st.session_state.get("pending_account_dialog")
    if pending == ("add", None):
        _add_account_dialog(account_store)
    elif isinstance(pending, tuple) and len(pending) == 2:
        action, account_key = pending
        pending_account = next(
            (item for item in accounts if item.key == account_key), None
        )
        if pending_account is None:
            st.session_state.pop("pending_account_dialog", None)
        else:
            memberships = _account_membership_count(account_key, groups)
            if action == "rename":
                _rename_account_dialog(
                    account_store, group_store, pending_account, memberships
                )
            elif action == "delete":
                _delete_account_dialog(
                    account_store, pending_account, memberships
                )
    if st.button("Tilføj konto", icon=":material/person_add:"):
        st.session_state["pending_account_dialog"] = ("add", None)
        st.rerun()

    if not accounts:
        st.info("Der er ingen gemte konti endnu.")
        return

    rows = [
        {
            "Navn": account.label,
            "Bruger-ID": account.user_id,
            "Teknisk nøgle": account.key,
            "Gruppemedlemskaber": _account_membership_count(account.key, groups),
            "Profil": account.profile_url,
        }
        for account in sorted(
            accounts, key=lambda item: (item.label.casefold(), item.user_id)
        )
    ]
    event = st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        key="saved-accounts-table",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Profil": st.column_config.LinkColumn(
                "Holdet-profil", display_text="Åbn profil"
            ),
        },
    )
    selected_rows = list(event.selection.rows)
    if not selected_rows:
        st.caption("Vælg en konto i tabellen for at omdøbe eller slette den.")
        return
    selected_row = rows[selected_rows[0]]
    account = next(
        item for item in accounts if item.key == selected_row["Teknisk nøgle"]
    )
    memberships = int(selected_row["Gruppemedlemskaber"])
    actions = st.container(horizontal=True)
    with actions:
        if st.button(
            "Omdøb konto", icon=":material/edit:", key="rename-selected-account"
        ):
            st.session_state["pending_account_dialog"] = ("rename", account.key)
            st.rerun()
        if st.button(
            "Slet konto", icon=":material/delete:", key="delete-selected-account"
        ):
            st.session_state["pending_account_dialog"] = ("delete", account.key)
            st.rerun()




def _data_tabs():
    labels = (
        "Gemte konti",
        "Datastatus",
        "Lagerplaceringer",
        "Backup og gendannelse",
    )
    slugs = ("accounts", "quality", "locations", "backup")
    key = "data-storage-tabs"
    requested = str(st.query_params.get("section", ""))
    desired = labels[slugs.index(requested)] if requested in slugs else labels[0]
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
def data_storage_view(
    account_store: AccountStore,
    group_store: GroupStore,
    configuration: HubConfiguration,
    index: SnapshotIndex,
    app_paths: AppPaths,
) -> None:
    st.title("Data og lager", anchor="data-og-lager")
    st.caption(
        "Konti, datastatus, lagerplaceringer og en samlet backup af Hubben."
    )
    try:
        accounts: tuple[AccountConfig, ...] | None = account_store.load()
        account_error = None
    except (PayloadError, OSError, ValueError) as exc:
        accounts = None
        account_error = str(exc)

    with st.container(horizontal=True):
        st.metric(
            "Gemte konti",
            len(accounts) if accounts is not None else "–",
            border=True,
        )
        st.metric("Managerspil", len(configuration.games), border=True)
        st.metric("Grupper", len(configuration.groups), border=True)
        st.metric("Teamsnapshots", len(index.snapshots), border=True)

    (
        accounts_tab,
        quality_tab,
        locations_tab,
        backup_tab,
    ) = _data_tabs()
    if accounts_tab.open:
        with accounts_tab:
            _saved_accounts_tab(
                account_store,
                group_store,
                accounts,
                configuration.groups,
                account_error,
            )
    if quality_tab.open:
        with quality_tab:
            st.subheader("Datastatus")
            st.caption(
                "Se om de lokale data kan bruges, hvad der mangler, og hvor "
                "du kan opdatere manuelt."
            )
            data_quality_panel(
                configuration.games,
                configuration.groups,
                index,
                app_paths,
            )
            st.subheader("Lokale stores")
            st.caption(
                "Sæsoner, managerhistorik og turneringsdata kontrolleres "
                "uafhængigt, så en fejl ikke skjuler de øvrige data."
            )
            st.dataframe(
                _local_store_health(group_store, configuration, app_paths),
                hide_index=True,
                width="stretch",
            )
    if locations_tab.open:
        with locations_tab:
            _storage_locations_tab(app_paths)
    if backup_tab.open:
        with backup_tab:
            backup_view(app_paths)
