"""Task-focused sections for the Streamlit Data and storage page."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st

from website.presentation import dataframe

from holdet_lib.backup import create_backup
from holdet_lib.errors import PayloadError
from holdet_lib.groups import GroupStore, HubConfiguration
from holdet_lib.hall_of_fame import HallOfFameStore
from holdet_lib.hub_settings import HubSettingsStore
from holdet_lib.imports import ImportPreview, apply_import, preview_import
from holdet_lib.integrity import (
    IntegrityRepairPreview,
    full_integrity_check,
    preview_integrity_repair,
    quick_integrity_check,
    repair_integrity_index,
)
from holdet_lib.local_api import dataset_catalog, register_artifact
from holdet_lib.maintenance import (
    CleanupCandidate,
    RetentionCandidate,
    archive_retention_candidates,
    build_storage_inventory,
    delete_derived_files,
    list_cleanup_candidates,
    plan_snapshot_retention,
)
from holdet_lib.paths import AppPaths
from holdet_lib.privacy import build_support_bundle
from holdet_lib.reports import (
    ReportStore,
    build_manager_game_report_package,
    build_season_report_package,
    render_html_report,
)
from holdet_lib.seasons import SeasonStore
from holdet_lib.storage import SnapshotIndex
from holdet_lib.tournament_pairings import TournamentPairingStore
from website.hub_pages import data_quality_panel


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _local_store_health(
    group_store: GroupStore,
    configuration: HubConfiguration,
    paths: AppPaths,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        _, warnings = group_store.load_configuration_with_warnings()
    except (OSError, PayloadError, ValueError) as exc:
        rows.append({"Lager": "Turneringsrevisioner", "Status": "Fejl", "Detalje": str(exc)})
    else:
        rows.append(
            {
                "Lager": "Turneringsrevisioner",
                "Status": "Advarsel" if warnings else "OK",
                "Detalje": " | ".join(warnings) if warnings else "Alle revisioner kan læses.",
            }
        )
    try:
        seasons = SeasonStore(paths.seasons_file).load()
    except (OSError, PayloadError, ValueError) as exc:
        rows.append({"Lager": "Sæsoner", "Status": "Fejl", "Detalje": str(exc)})
    else:
        rows.append({"Lager": "Sæsoner", "Status": "OK", "Detalje": f"{len(seasons)} sæsondefinitioner kan læses."})
    events, warnings = HallOfFameStore(paths.hall_of_fame_dir).scan()
    rows.append(
        {
            "Lager": "Managerhistorik",
            "Status": "Advarsel" if warnings else "OK",
            "Detalje": " | ".join(warnings) if warnings else f"{len(events)} eventrevisioner kan læses.",
        }
    )
    pairing_store = TournamentPairingStore(paths.tournament_pairing_dir)
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
    rows.append(
        {
            "Lager": "Turneringsparringer",
            "Status": "Advarsel" if pairing_warnings else "OK",
            "Detalje": " | ".join(pairing_warnings) if pairing_warnings else f"{pairing_count} publicerede parringer kan læses.",
        }
    )
    return rows


def render_overview(
    group_store: GroupStore,
    configuration: HubConfiguration,
    index: SnapshotIndex,
    paths: AppPaths,
) -> None:
    st.subheader("Overblik")
    st.caption("Læsestatus, datakilder og lagerforbrug. Der ændres ingen filer fra dette område.")
    data_quality_panel(configuration.games, configuration.groups, index, paths)
    st.subheader("Lokale stores")
    dataframe(
        _local_store_health(group_store, configuration, paths),
        hide_index=True,
        width="stretch",
        key="data-local-store-health",
    )
    inventory = build_storage_inventory(paths)
    st.subheader("Lagerforbrug pr. spil")
    if not inventory.rows:
        st.info("Der er endnu ingen lokale data eller afledte filer at opgøre.")
        return
    dataframe(
        [
            {"Spil": row.game_scope, "Kategori": row.category, "Filer": row.files, "Størrelse": row.bytes}
            for row in inventory.rows
        ],
        hide_index=True,
        width="stretch",
        key="storage-inventory-overview",
        column_config={"Størrelse": st.column_config.NumberColumn(format="bytes")},
    )


def _report_actions(package, *, title: str, stem: str, paths: AppPaths, key: str) -> None:
    content = render_html_report(package, title=title).encode("utf-8")
    st.download_button(
        "Download HTML-rapport",
        content,
        file_name=f"{stem}.html",
        mime="text/html; charset=utf-8",
        icon=":material/download:",
        type="primary",
        key=f"{key}-download-html",
        on_click="ignore",
    )
    st.caption("Åbn HTML-filen i en browser og vælg Udskriv → Gem som PDF for en A4-PDF.")
    if st.button("Gem rapport i eksportlageret", icon=":material/save:", key=f"{key}-save-html"):
        try:
            artifact = ReportStore(paths.report_dir).save(package, title=title, stem=stem)
            registered = register_artifact(paths, artifact.path)
        except (OSError, ValueError, PayloadError) as exc:
            st.error(f"Rapporten kunne ikke gemmes: {exc}")
        else:
            st.session_state[f"{key}-artifact"] = (registered.artifact_id, artifact.path.name)
            st.success(f"Rapporten er gemt som {artifact.path.name}.")
    artifact = st.session_state.get(f"{key}-artifact")
    if isinstance(artifact, tuple) and len(artifact) == 2:
        st.link_button(
            f"Download gemt rapport ({artifact[1]})",
            f"/downloads/{artifact[0]}",
            icon=":material/download:",
        )

    st.markdown("#### Anonymiseret supportpakke")
    profile = st.segmented_control(
        "Anonymiseringsprofil",
        ("share", "debug"),
        format_func=lambda value: "Deling" if value == "share" else "Fejlsøgning",
        default="share",
        key=f"{key}-privacy",
    )
    st.caption(
        "Pakken er ikke-gendannelig og har ingen reversibel pseudonymnøgle. "
        "Fejlsøgning fjerner desuden stier, URL'er, noter, tags og genkendelige labels."
    )
    if st.button("Forbered anonymiseret ZIP", icon=":material/privacy_tip:", key=f"{key}-prepare-support"):
        st.session_state[f"{key}-support"] = build_support_bundle(package, str(profile or "share"))
    support = st.session_state.get(f"{key}-support")
    if isinstance(support, bytes):
        st.download_button(
            "Download supportpakke",
            support,
            file_name=f"{stem}-support.zip",
            mime="application/zip",
            icon=":material/download:",
            key=f"{key}-download-support",
            on_click="ignore",
        )


def render_exports(
    configuration: HubConfiguration,
    index: SnapshotIndex,
    paths: AppPaths,
) -> None:
    st.subheader("Eksport og rapporter")
    st.caption(
        "Spiller- og holdeksporter understøtter CSV, XLSX og valgfri Parquet i deres eksisterende visninger. "
        "Her samles cachede managerspil og sæsoner i selvstændige HTML-rapporter."
    )
    scope = st.segmented_control(
        "Rapporttype",
        ("game", "season"),
        format_func=lambda value: "Managerspil" if value == "game" else "Sæson",
        default="game",
        key="data-report-scope",
    )
    if scope == "game":
        games = tuple(item for item in configuration.games if not item.is_archived)
        if not games:
            st.info("Tilføj et managerspil, før en managerspilrapport kan bygges.")
            return
        selected = st.selectbox("Managerspil", games, format_func=lambda item: item.name, key="data-report-game")
        if st.button("Forbered managerspilrapport", icon=":material/description:", key="prepare-manager-report"):
            st.session_state["prepared-manager-report"] = (
                selected.identity,
                build_manager_game_report_package(selected, configuration.groups, index),
            )
        prepared = st.session_state.get("prepared-manager-report")
        if isinstance(prepared, tuple) and prepared[0] == selected.identity:
            _report_actions(
                prepared[1],
                title=f"Managerspil – {selected.name}",
                stem=f"managerspil-{selected.game.slug}",
                paths=paths,
                key="manager-report",
            )
        return

    try:
        seasons = SeasonStore(paths.seasons_file).load()
        events, warnings = HallOfFameStore(paths.hall_of_fame_dir).scan()
        settings = HubSettingsStore(paths.hub_settings_file).load()
    except (OSError, ValueError, PayloadError) as exc:
        st.error(f"Sæsondata kunne ikke læses: {exc}")
        return
    if warnings:
        st.warning("Nogle historikrevisioner kunne ikke læses; rapporten bruger de øvrige data.")
    if not seasons:
        st.info("Der er ingen sæsoner at rapportere endnu.")
        return
    season = st.selectbox("Sæson", seasons, format_func=lambda item: item.name, key="data-report-season")
    if st.button("Forbered sæsonrapport", icon=":material/description:", key="prepare-season-report"):
        st.session_state["prepared-season-report"] = (
            season.season_id,
            build_season_report_package(season, events, settings.hall_of_fame_score),
        )
    prepared = st.session_state.get("prepared-season-report")
    if isinstance(prepared, tuple) and prepared[0] == season.season_id:
        _report_actions(
            prepared[1],
            title=f"Sæson – {season.name}",
            stem=f"season-{season.season_id}",
            paths=paths,
            key="season-report",
        )


def _show_import_preview(preview: ImportPreview) -> None:
    labels = {
        "backup": "Fuld Hub-backup",
        "canonical_snapshots": "Kanoniske snapshots",
        "legacy_json": "Historisk JSON-eksport",
        "archive_only": "Arkiveres uden domæneimport",
        "invalid": "Ikke gyldig til import",
    }
    st.write(f"**Klassifikation:** {labels[preview.kind]}")
    st.caption(f"SHA-256: `{preview.checksum}`")
    for warning in preview.warnings:
        st.warning(warning)
    for error in preview.errors:
        st.error(error)
    if preview.operations:
        dataframe(
            [
                {
                    "Kilde": item.source_name,
                    "Handling": "Spring over" if item.action == "skip" else "Skriv",
                    "Størrelse": item.size,
                    "SHA-256": item.sha256,
                }
                for item in preview.operations
            ],
            hide_index=True,
            width="stretch",
            key="import-preview-table",
            column_config={"Størrelse": st.column_config.NumberColumn(format="bytes")},
        )


def render_import_backup(paths: AppPaths) -> None:
    st.subheader("Import og backup")
    st.caption("Backup oprettes først ved et klik. Import valideres og vises altid før den kan skrive.")
    with st.container(border=True):
        st.markdown("#### Opret fuld backup")
        st.caption(
            "Indeholder kanonisk konfiguration, snapshots, manifester, revisioner og importerede historiske data. "
            "Integritetsindekset og afledte eksporter er ikke backupautoritet."
        )
        if st.button(
            "Opret og validér backup-ZIP",
            type="primary",
            icon=":material/archive:",
            key="create-hub-backup",
        ):
            try:
                path = create_backup(paths)
                registered = register_artifact(paths, path)
            except (OSError, ValueError, PayloadError) as exc:
                st.error(f"Backup kunne ikke oprettes: {exc}")
            else:
                st.session_state["latest-backup-artifact"] = (
                    registered.artifact_id,
                    path.name,
                    path.stat().st_size,
                )
                st.success(f"Backupen {path.name} er oprettet og valideret.")
        latest = st.session_state.get("latest-backup-artifact")
        if isinstance(latest, tuple) and len(latest) == 3:
            st.caption(f"{latest[1]} · {format_bytes(int(latest[2]))}")
            st.link_button("Download seneste backup", f"/downloads/{latest[0]}", icon=":material/download:")

    with st.container(border=True):
        st.markdown("#### Importér eller gendan")
        uploaded = st.file_uploader(
            "Vælg ZIP, JSON, TXT, Markdown eller CSV",
            type=("zip", "json", "txt", "md", "markdown", "csv"),
            key="data-import-upload",
            help="Kanoniske snapshots kan importeres. Ældre JSON gemmes separat; tekst- og CSV-filer arkiveres kun.",
        )
        if uploaded is None:
            st.info("Vælg en fil for at få en skrivefri forhåndsvisning.")
            return
        raw = uploaded.getvalue()
        try:
            preview = preview_import(raw, paths, filename=uploaded.name)
        except (OSError, ValueError, PayloadError) as exc:
            st.error(f"Filen kunne ikke forhåndsvises: {exc}")
            return
        _show_import_preview(preview)
        restore = preview.kind == "backup"
        confirmed = st.checkbox(
            (
                "Jeg forstår, at aktiv konfiguration og aktive data erstattes, og at der oprettes en rollback-backup."
                if restore
                else "Jeg har gennemgået mål, filantal og konsekvens."
            ),
            key="data-import-confirm",
        )
        if st.button(
            "Gendan valideret backup" if restore else "Udfør valideret import",
            type="primary",
            icon=":material/restore:" if restore else ":material/upload:",
            disabled=not preview.can_apply or not confirmed,
            key="apply-data-import",
        ):
            try:
                result = apply_import(preview, raw, paths)
            except (OSError, ValueError, PayloadError) as exc:
                st.error(f"Importen blev ikke udført: {exc}")
            else:
                st.cache_data.clear()
                st.session_state["data-action-message"] = (
                    f"Importen er gennemført: {result.written_files} filer skrevet og "
                    f"{result.skipped_files} identiske filer sprunget over."
                )
                st.rerun()


@st.dialog("Arkivér mellemversioner", width="large")
def _archive_dialog(
    paths: AppPaths,
    selected: tuple[RetentionCandidate, ...],
    retained_count: int,
) -> None:
    st.warning(
        "Der oprettes først en checksumvalideret ZIP med de oprindelige relative stier. "
        "Kilderne fjernes kun efter vellykket validering."
    )
    st.write(f"**Valgte filer:** {len(selected)}")
    st.write(f"**Plads i kilderne:** {format_bytes(sum(item.size for item in selected))}")
    st.write(f"**Bevarede snapshots:** {retained_count}")
    if st.button(
        "Opret arkiv og fjern valgte mellemversioner",
        type="primary",
        icon=":material/archive:",
        key="confirm-retention-archive",
    ):
        try:
            result = archive_retention_candidates(paths, selected)
        except (OSError, ValueError) as exc:
            st.error(f"Arkiveringen blev afbrudt: {exc}")
        else:
            message = (
                f"{result.archived_files} filer blev arkiveret i {result.path.name}; "
                f"{result.removed_files} kilder blev fjernet."
            )
            if result.removal_errors:
                message += " Nogle kilder kunne ikke fjernes: " + " | ".join(result.removal_errors)
            st.session_state["data-action-message"] = message
            st.session_state.pop("retention-plan", None)
            st.rerun()


@st.dialog("Slet afledte filer", width="large")
def _delete_dialog(paths: AppPaths, selected: tuple[CleanupCandidate, ...]) -> None:
    st.error(
        "De valgte afledte eksporter, backups eller arkiver slettes permanent. "
        "Konfiguration, manifester, revisioner og aktive snapshots kan ikke vælges her."
    )
    st.write(f"**Valgte filer:** {len(selected)}")
    st.write(f"**Frigivet plads:** {format_bytes(sum(item.size for item in selected))}")
    st.write("**Bevares:** konfiguration, manifester, revisioner, aktive snapshots og den nyeste backup.")
    if st.button(
        "Slet valgte afledte filer",
        type="primary",
        icon=":material/delete_forever:",
        key="confirm-derived-delete",
    ):
        try:
            deleted = delete_derived_files(paths, tuple(item.path for item in selected))
        except (OSError, ValueError) as exc:
            st.error(f"Sletningen blev afbrudt: {exc}")
        else:
            st.session_state["data-action-message"] = f"{len(deleted)} afledte filer blev slettet."
            st.session_state.pop("cleanup-candidates", None)
            st.rerun()


def _integrity_panel(paths: AppPaths) -> None:
    quick = quick_integrity_check(paths)
    with st.container(border=True):
        st.markdown("#### Integritetskontrol")
        if quick.is_clean:
            st.success(f"Hurtig kontrol: {len(quick.entries)} indekserede filer matcher metadata.")
        else:
            st.warning(f"Hurtig kontrol fandt {len(quick.issues)} afvigelser.")
        if quick.issues:
            dataframe(
                [
                    {"Niveau": item.severity, "Type": item.code, "Fil": item.path, "Detalje": item.message}
                    for item in quick.issues
                ],
                hide_index=True,
                width="stretch",
                key="quick-integrity-issues",
            )
        if st.button("Kør fuld kontrol med SHA-256", icon=":material/fact_check:", key="run-full-integrity"):
            st.session_state["full-integrity-result"] = full_integrity_check(paths)
        full = st.session_state.get("full-integrity-result")
        if full is not None:
            if full.is_clean:
                st.success(f"Fuld kontrol gennemførte {len(full.entries)} filer uden fejl.")
            else:
                st.warning(f"Fuld kontrol fandt {len(full.issues)} afvigelser. Kanoniske filer er ikke ændret.")
                dataframe(
                    [
                        {"Niveau": item.severity, "Type": item.code, "Fil": item.path, "Detalje": item.message}
                        for item in full.issues
                    ],
                    hide_index=True,
                    width="stretch",
                    key="full-integrity-issues",
                )
        if st.button("Forhåndsvis reparation af indeks", icon=":material/build:", key="preview-integrity-repair"):
            st.session_state["integrity-repair-preview"] = preview_integrity_repair(paths)
        repair = st.session_state.get("integrity-repair-preview")
        if isinstance(repair, IntegrityRepairPreview):
            st.write(
                f"Preview: {repair.added} tilføjes, {repair.changed} opdateres og {repair.removed} fjernes fra indekset."
            )
            if repair.issues:
                st.warning("Korrupte eller ukendte data forbliver synlige og ændres ikke; kun indekset erstattes.")
            confirmed = st.checkbox(
                "Erstat kun integrity-index.json med dette preview",
                key="confirm-integrity-repair",
            )
            if st.button(
                "Reparer indeks",
                type="primary",
                disabled=not confirmed,
                icon=":material/build:",
                key="apply-integrity-repair",
            ):
                try:
                    path = repair_integrity_index(paths, repair)
                except (OSError, ValueError) as exc:
                    st.error(f"Indekset blev ikke repareret: {exc}")
                else:
                    st.session_state["data-action-message"] = f"Det afledte indeks er erstattet atomisk: {path.name}."
                    st.session_state.pop("integrity-repair-preview", None)
                    st.rerun()


def _retention_panel(paths: AppPaths) -> None:
    inventory = build_storage_inventory(paths)
    with st.container(border=True):
        st.markdown("#### Lager og snapshotretention")
        st.write(f"{inventory.total_files} filer bruger {format_bytes(inventory.total_bytes)}.")
        if st.button("Find overflødige mellemversioner", icon=":material/filter_alt:", key="plan-snapshot-retention"):
            st.session_state["retention-plan"] = plan_snapshot_retention(paths)
        plan = st.session_state.get("retention-plan")
        if plan is None:
            return
        for warning in plan.warnings:
            st.warning(warning)
        if not plan.candidates:
            st.info("Der er ingen gyldige mellemversioner at arkivere. Nyeste snapshot pr. runde bevares.")
            return
        dataframe(
            [
                {
                    "Fil": item.relative_path,
                    "Spil": item.game_scope,
                    "Type": item.snapshot_type,
                    "Runde": item.round_number,
                    "Størrelse": item.size,
                    "Bevares i stedet": item.retained_path.name,
                }
                for item in plan.candidates
            ],
            hide_index=True,
            width="stretch",
            key="retention-candidates-table",
            column_config={"Størrelse": st.column_config.NumberColumn(format="bytes")},
        )
        choices = {item.relative_path: item for item in plan.candidates}
        selected_names = st.multiselect(
            "Mellemversioner der skal arkiveres",
            tuple(choices),
            default=tuple(choices),
            key="selected-retention-candidates",
        )
        selected = tuple(choices[name] for name in selected_names)
        if st.button(
            "Gennemgå arkivering",
            disabled=not selected,
            icon=":material/archive:",
            key="review-retention-archive",
        ):
            _archive_dialog(paths, selected, len(plan.retained))


def _cleanup_panel(paths: AppPaths) -> None:
    with st.container(border=True):
        st.markdown("#### Manuel oprydning af afledte filer")
        if st.button("Vis filer der må slettes", icon=":material/delete_sweep:", key="list-cleanup-candidates"):
            st.session_state["cleanup-candidates"] = list_cleanup_candidates(paths)
        cleanup = st.session_state.get("cleanup-candidates")
        if cleanup is None:
            return
        if not cleanup:
            st.info("Der er ingen afledte eksporter, backups eller mellemversionsarkiver at slette.")
            return
        dataframe(
            [
                {"Fil": item.path.name, "Kategori": item.category, "Størrelse": item.size, "Ændret": item.modified_at}
                for item in cleanup
            ],
            hide_index=True,
            width="stretch",
            key="cleanup-candidates-table",
            column_config={"Størrelse": st.column_config.NumberColumn(format="bytes")},
        )
        choices = {str(item.path): item for item in cleanup}
        selected_names = st.multiselect(
            "Afledte filer der skal slettes",
            tuple(choices),
            format_func=lambda value: choices[value].path.name,
            key="selected-cleanup-candidates",
        )
        selected = tuple(choices[name] for name in selected_names)
        if st.button(
            "Gennemgå permanent sletning",
            disabled=not selected,
            icon=":material/delete_forever:",
            key="review-derived-delete",
        ):
            _delete_dialog(paths, selected)


def render_integrity_cleanup(paths: AppPaths) -> None:
    st.subheader("Integritet og oprydning")
    st.caption(
        "Kontroller og planlæg her. Ingen retention, arkivering, indeksreparation eller sletning kører automatisk."
    )
    _integrity_panel(paths)
    _retention_panel(paths)
    _cleanup_panel(paths)


def render_api() -> None:
    current_url = st.context.url or "http://127.0.0.1:8501"
    parsed = urlsplit(current_url)
    api_base = f"{parsed.scheme}://{parsed.netloc}"
    st.subheader("Lokalt API")
    st.success("API'et er read-only, cache-only og tilgængeligt på samme loopback-port som Hubben.")
    st.code(f"{api_base}/api/v1/catalog", language=None)
    st.caption(
        "Der er ingen skriveendpoints, ingen wildcard-CORS og ingen kald til Holdet.dk. "
        "Start Hubben via website/server.py for at registrere ruterne."
    )
    st.markdown("#### Excel og Power BI")
    st.code(
        f"{api_base}/api/v1/data/players?game=dit-spil&format=csv&limit=5000",
        language=None,
    )
    st.code(
        f'Power Query: Web.Contents("{api_base}/api/v1/data/storage_usage?format=csv")',
        language=None,
    )
    catalog = dataset_catalog()
    dataframe(
        [
            {
                "Datasæt": item["name"],
                "Kolonner": ", ".join(item["columns"]),
                "Filtre": ", ".join(item["filters"]) or "–",
                "Påkrævet": ", ".join(item["required_filters"]) or "–",
            }
            for item in catalog["datasets"]
        ],
        hide_index=True,
        width="stretch",
        key="local-api-catalog",
    )
