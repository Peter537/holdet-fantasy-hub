from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

import holdet_lib as holdet


NOW = datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)


def paths_for(root: Path) -> holdet.AppPaths:
    return holdet.resolve_paths(
        overrides=holdet.PathOverrides(data_root=root),
        environ={},
    )


def sample_package(*, two_tables: bool = False) -> holdet.DataPackage:
    tables = [
        holdet.DataTable(
            "spillere",
            ("name", "value", "active", "note"),
            (
                {
                    "name": "Ægir; Ørn",
                    "value": 12_345_678,
                    "active": True,
                    "note": "=HYPERLINK(\"https://example.invalid\")",
                },
                {"name": "Nora", "value": -4, "active": False, "note": None},
            ),
        )
    ]
    if two_tables:
        tables.append(
            holdet.DataTable(
                "hold",
                ("team_name", "manager_name", "points"),
                ({"team_name": "Nordlys", "manager_name": "Åse", "points": 99},),
            )
        )
    return holdet.DataPackage(
        "manager_game_report",
        {"locale": "da", "game": "super-manager"},
        NOW,
        {"source": "local-cache", "path": "C:\\private\\cache.json"},
        tuple(tables),
    )


def test_csv_is_rfc4180_bom_safe_and_keeps_raw_numbers() -> None:
    payload = holdet.package_to_csv(sample_package()).content
    assert payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")
    assert "\r\n" in text
    assert '"Ægir; Ørn"' not in text  # semicolon is not the CSV delimiter
    assert "12345678" in text
    assert "'=HYPERLINK" in text


def test_data_package_rejects_nested_or_timezone_ambiguous_metadata() -> None:
    table = holdet.DataTable("data", ("value",), ({"value": 1},))
    with pytest.raises(TypeError):
        holdet.DataPackage("test", {"nested": {"x": 1}}, NOW, {}, (table,))
    with pytest.raises(ValueError):
        holdet.DataPackage(
            "test",
            {"generated": datetime(2026, 8, 7, 12, 30)},
            NOW,
            {},
            (table,),
        )


def test_multitable_csv_zip_has_checksums_and_manifest() -> None:
    serialized = holdet.package_to_csv(sample_package(two_tables=True))
    assert serialized.extension == "zip"
    with zipfile.ZipFile(BytesIO(serialized.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert [item["path"] for item in manifest["files"]] == [
            "spillere.csv",
            "hold.csv",
        ]
        for item in manifest["files"]:
            content = archive.read(item["path"])
            assert item["size"] == len(content)
            assert item["sha256"] == hashlib.sha256(content).hexdigest()


def test_xlsx_uses_separate_sheets_and_disables_formula_interpretation() -> None:
    payload = holdet.package_to_xlsx(sample_package(two_tables=True)).content
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        shared = archive.read("xl/sharedStrings.xml").decode("utf-8")
    assert 'name="spillere"' in workbook
    assert 'name="hold"' in workbook
    assert "=HYPERLINK" in shared
    assert "&apos;=HYPERLINK" in shared or "'=HYPERLINK" in shared


def test_parquet_is_optional_or_carries_package_metadata() -> None:
    try:
        serialized = holdet.package_to_parquet(sample_package())
    except holdet.PayloadError as exc:
        assert "holdet-lib[parquet]" in str(exc)
    else:
        import pyarrow.parquet as pq

        table = pq.read_table(BytesIO(serialized.content))
        metadata = json.loads(table.schema.metadata[b"holdet.data_package"])
        assert metadata["schema_version"] == 1
        assert metadata["scope"]["game"] == "super-manager"
        assert table.column("value").to_pylist() == [12_345_678, -4]


def test_anonymization_is_stable_irreversible_and_debug_minimizes() -> None:
    package = sample_package(two_tables=True)
    shared = holdet.anonymize_data_package(package, "share")
    repeated = holdet.anonymize_data_package(package, "share")
    assert shared == repeated
    assert shared.restorable is False
    assert shared.tables[1].rows[0]["manager_name"] != "Åse"
    assert shared.provenance["path"] == "[fjernet]"

    debug = holdet.anonymize_data_package(package, "debug")
    assert debug.provenance["path"] is None
    assert debug.tables[0].rows[0]["name"] != "Ægir; Ørn"
    bundle = holdet.build_support_bundle(package, "debug")
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("support-manifest.json"))
        assert manifest["restorable"] is False
        assert "mapping" not in json.dumps(manifest).casefold()


def test_support_import_rejects_tampering(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    bundle = holdet.build_support_bundle(sample_package(), "share")
    changed = BytesIO()
    with zipfile.ZipFile(BytesIO(bundle)) as original, zipfile.ZipFile(changed, "w") as target:
        for info in original.infolist():
            content = original.read(info.filename)
            if info.filename == "data-package.json":
                content += b" "
            target.writestr(info, content)
    preview = holdet.preview_import(changed.getvalue(), paths, filename="support.zip")
    assert preview.kind == "archive_only"
    assert not preview.can_apply
    assert any("Checksum" in error for error in preview.errors)


def test_html_report_escapes_data_and_has_print_css() -> None:
    package = holdet.DataPackage(
        "manager_game_report",
        {"game": "test"},
        NOW,
        {"source": "<script>alert(1)</script>"},
        (
            holdet.DataTable(
                "resultat",
                ("manager_name", "points"),
                ({"manager_name": "<img src=x onerror=alert(1)>", "points": 7},),
            ),
        ),
    )
    html = holdet.render_html_report(package, title="Rapport <test>")
    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert "@media print" in html
    assert "@page" in html
    assert "<script" not in html.casefold()


def test_legacy_import_preview_is_side_effect_free_and_conflicts_abort(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    raw = json.dumps({"export": "legacy", "manager": "Åse"}).encode("utf-8")
    preview = holdet.preview_import(raw, paths, filename="tidligere.json")
    assert preview.kind == "legacy_json"
    assert preview.can_apply
    assert not paths.data_dir.exists()

    result = holdet.apply_import(preview, raw, paths)
    assert result.written_files == 1
    duplicate = holdet.preview_import(raw, paths, filename="tidligere.json")
    assert duplicate.skipped_count == 1
    assert duplicate.can_apply

    target = duplicate._payloads[0][0]
    target.write_text("andet indhold", encoding="utf-8")
    conflict = holdet.preview_import(raw, paths, filename="tidligere.json")
    assert not conflict.can_apply
    before = target.read_bytes()
    with pytest.raises(holdet.PayloadError):
        holdet.apply_import(conflict, raw, paths)
    assert target.read_bytes() == before


def test_text_import_is_archived_without_domain_parsing(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    raw = b"name,points\r\n=calc,12\r\n"
    preview = holdet.preview_import(raw, paths, filename="old.csv")
    assert preview.kind == "archive_only"
    assert "parses ikke" in preview.warnings[0]
    holdet.apply_import(preview, raw, paths)
    assert next(paths.import_dir.rglob("*.csv")).read_bytes() == raw


def test_sport_adapter_registry_is_closed_and_rules_fail_closed() -> None:
    adapters = holdet.registered_sport_adapters()
    assert {adapter.key for adapter in adapters} == {"soccer", "cycling", "formula1", "golf"}
    assert all(adapter.capabilities.rules_certainty == "unverified" for adapter in adapters)
    assert holdet.get_sport_adapter("cycling_world_tour").key == "cycling"
    assert holdet.get_sport_adapter("soccer").normalize_position("Maalmand") == "goalkeeper"
    assert holdet.transfer_rule_profile(game_format="soccer").known is False
    assert holdet.transfer_rule_profile(game_slug="super-manager-fall-2026") == holdet.UNKNOWN_RULES
    with pytest.raises(holdet.UnsupportedGameError):
        holdet.get_sport_adapter("curling")
