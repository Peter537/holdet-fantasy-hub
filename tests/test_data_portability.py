from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO, StringIO
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

import holdet_lib as holdet


NOW = datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)
FORMULA_LIKE_TEXT = ("=1+1", "+1+1", "-1+1", "@SUM(1)", "\t=1+1", "\r=1+1")
MULTILINE_NAME = 'Comma, "quote"\r\nsecond line'


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


def spreadsheet_package(*, two_tables: bool = False) -> holdet.DataPackage:
    package = sample_package(two_tables=two_tables)
    players, *other_tables = package.tables
    additional_rows = tuple(
        {"name": MULTILINE_NAME, "value": -4, "active": False, "note": note}
        for note in FORMULA_LIKE_TEXT
    )
    return replace(package, tables=(replace(players, rows=players.rows + additional_rows), *other_tables))


def test_csv_is_rfc4180_bom_safe_and_keeps_raw_numbers() -> None:
    payload = holdet.package_to_csv(spreadsheet_package()).content
    assert payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")
    assert text.startswith("name,value,active,note\r\n")
    assert '"Ægir; Ørn"' not in text  # semicolon is not the CSV delimiter
    rows = list(csv.reader(StringIO(text, newline="")))
    assert rows == [
        ["name", "value", "active", "note"],
        ["Ægir; Ørn", "12345678", "True", "'=HYPERLINK(\"https://example.invalid\")"],
        ["Nora", "-4", "False", ""],
        *([MULTILINE_NAME, "-4", "False", "'" + note] for note in FORMULA_LIKE_TEXT),
    ]
    embedded_newlines = sum(field.count("\n") for row in rows for field in row)
    assert text.count("\n") == text.count("\r\n") == len(rows) + embedded_newlines


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
    payload = holdet.package_to_xlsx(spreadsheet_package(two_tables=True)).content
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        players = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    assert [sheet.attrib["name"] for sheet in workbook.findall(".//{*}sheet")] == ["spillere", "hold"]
    assert not players.findall(".//{*}f")
    assert not players.findall(".//{*}hyperlink")
    strings = [item.findtext("{*}t").replace("_x000D_", "\r") for item in shared.findall("{*}si")]
    cells = {cell.attrib["r"]: cell for cell in players.findall(".//{*}c")}

    assert cells["B2"].get("t", "n") == "n" and cells["B2"].findtext("{*}v") == "12345678"
    assert cells["C2"].get("t") == "b" and cells["C2"].findtext("{*}v") == "1"
    notes = [(2, '=HYPERLINK("https://example.invalid")'), *enumerate(FORMULA_LIKE_TEXT, start=4)]
    for index, note in notes:
        cell = cells[f"D{index}"]
        assert cell.get("t") == "s"
        assert strings[int(cell.findtext("{*}v"))] == "'" + note
    for index in range(3, 4 + len(FORMULA_LIKE_TEXT)):
        cell = cells[f"B{index}"]
        assert cell.get("t", "n") == "n"
        assert cell.findtext("{*}v") == "-4"
    assert strings[int(cells["A4"].findtext("{*}v"))] == MULTILINE_NAME


def test_parquet_round_trip_preserves_package_metadata_and_numbers() -> None:
    import pyarrow.parquet as pq

    serialized = holdet.package_to_parquet(sample_package())
    table = pq.read_table(BytesIO(serialized.content))
    metadata = json.loads(table.schema.metadata[b"holdet.data_package"])
    assert metadata["schema_version"] == 1
    assert metadata["scope"]["game"] == "super-manager"
    assert table.column("value").to_pylist() == [12_345_678, -4]


def test_parquet_requires_the_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    with pytest.raises(holdet.PayloadError, match=r"holdet-lib\[parquet\]"):
        holdet.package_to_parquet(sample_package())


@pytest.mark.parametrize("profile", ["share", "debug"])
def test_support_bundle_projects_only_approved_season_fields(profile: str) -> None:
    package = holdet.DataPackage(
        "season_report",
        {
            "season_id": "private-season-id",
            "season_name": "Private Season",
            "unknown_scope_key": 832143,
        },
        NOW,
        {
            "mode": "cache-only", "path": r"C:\private\cache.json", "score_profile": "Private Scoring",
            "latest_capture": NOW.isoformat(), "unknown_provenance_key": True,
        },
        (
            holdet.DataTable(
                "competitions",
                ("competition_id", "competition_name", "events", "complete", "note", "new_identifier"),
                ({
                    "competition_id": "private-competition-id", "competition_name": "Private Competition",
                    "events": 17, "complete": True, "note": "Private Note", "new_identifier": 876653,
                },),
            ),
            holdet.DataTable("private_table_name", ("value",), ({"value": 837234},)),
        ),
    )
    bundle = holdet.build_support_bundle(package, profile)
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {"support-manifest.json", "data-package.json"}
        manifest = json.loads(archive.read("support-manifest.json"))
        payload = archive.read("data-package.json")

    text = payload.decode("utf-8")
    assert "private" not in text.casefold()
    for unknown in ("unknown_scope_key", "unknown_provenance_key", "score_profile", "note", "new_identifier"):
        assert unknown not in text
    projected = json.loads(payload)
    competition = projected["tables"][0]["rows"][0]
    assert competition["events"] == 17
    assert competition["complete"] is True
    assert competition["competition_id"].startswith("Konkurrence ")
    assert projected["scope"]["season_id"].startswith("Sæson ")
    assert ("competition_name" in competition) == (profile == "share")
    assert ("season_name" in projected["scope"]) == (profile == "share")
    assert ("latest_capture" in projected["provenance"]) == (profile == "share")
    assert manifest["privacy_profile"] == projected["privacy_profile"] == profile
    assert manifest["restorable"] is projected["restorable"] is False
    assert manifest["files"] == [{
        "path": "data-package.json", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
    }]


def test_support_pseudonyms_repeat_within_one_package_and_change_across_exports() -> None:
    package = holdet.DataPackage(
        "manager_game_report", {"locale": "unknown-enum-value", "game": "private-game"},
        NOW, {},
        (holdet.DataTable("game", ("game", "teams"), (
            {"game": "private-game", "teams": 3},
        )),),
    )
    first = holdet.anonymize_data_package(package, "share")
    second = holdet.anonymize_data_package(package, "share")
    for projected in (first, second):
        assert projected.scope["game"] == projected.tables[0].rows[0]["game"]
        assert projected.scope["locale"] is None
        assert projected.tables[0].rows[0]["teams"] == 3
    assert first.scope["game"] != second.scope["game"]


def test_unknown_support_package_type_only_exposes_counts() -> None:
    sample = sample_package()
    package = holdet.DataPackage("private-document-type", sample.scope, NOW, sample.provenance, sample.tables)
    projected = holdet.anonymize_data_package(package, "share")
    assert projected.document_type == "support_summary"
    assert projected.scope == projected.provenance == {}
    assert [table.name for table in projected.tables] == ["summary"]
    assert projected.tables[0].rows == ({"table_count": 1, "row_count": 2},)


def test_support_import_rejects_tampering(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    bundle = holdet.build_support_bundle(sample_package(), "share")
    valid = holdet.preview_import(bundle, paths, filename="support.zip")
    assert valid.kind == "archive_only"
    assert valid.can_apply
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
    assert {adapter.key for adapter in adapters} == {
        "soccer",
        "cycling",
        "formula1",
        "golf",
        "nfl",
        "handball",
    }
    assert all(adapter.capabilities.rules_certainty == "unverified" for adapter in adapters)
    assert holdet.get_sport_adapter("cycling_world_tour").key == "cycling"
    assert holdet.get_sport_adapter("soccer").normalize_position("Maalmand") == "goalkeeper"
    assert holdet.get_sport_adapter("nfl").default_unit == "money"
    assert holdet.get_sport_adapter("handball").default_unit == "money"
    assert holdet.transfer_rule_profile(game_format="soccer").known is False
    assert holdet.transfer_rule_profile(variant="nfl") == holdet.UNKNOWN_RULES
    assert holdet.transfer_rule_profile(game_format="nfl") == holdet.UNKNOWN_RULES
    assert holdet.transfer_rule_profile(variant="handball") == holdet.UNKNOWN_RULES
    assert holdet.transfer_rule_profile(game_slug="super-manager-fall-2026") == holdet.UNKNOWN_RULES
    with pytest.raises(holdet.UnsupportedGameError):
        holdet.get_sport_adapter("curling")
