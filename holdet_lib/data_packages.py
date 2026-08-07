"""Versioned tabular packages and portable serializers.

The models in this module are deliberately independent of Holdet domain
objects.  Domain services project their data into a :class:`DataPackage` and
all machine-readable exports share the same serializers from that point.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
from io import BytesIO, StringIO
import json
import math
import re
from typing import TypeAlias
import zipfile

from .errors import PayloadError


DATA_PACKAGE_SCHEMA_VERSION = 1
TABULAR_EXPORT_FORMATS = ("csv", "xlsx", "parquet")

Scalar: TypeAlias = str | int | float | bool | None


def _normalize_metadata(values: Mapping[str, object], *, label: str) -> dict[str, Scalar]:
    normalized: dict[str, Scalar] = {}
    for key, raw in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label} skal have navngivne tekstfelter")
        value = raw
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError(f"{label}.{key} har et tidspunkt uden tidszone")
            value = value.isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{label}.{key} må ikke være NaN eller uendelig")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"{label}.{key} indeholder en ikke-portabel værdi")
        normalized[key.strip()] = value
    return normalized


@dataclass(frozen=True, slots=True)
class DataTable:
    """One named, ordered table containing only portable scalar values."""

    name: str
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Scalar], ...]

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("Tabelnavnet skal være et sikkert, ikke-tomt navn")
        if not self.columns or any(not value.strip() for value in self.columns):
            raise ValueError("En datatabel skal have navngivne kolonner")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("En datatabel må ikke have duplikerede kolonner")
        expected = set(self.columns)
        normalized: list[dict[str, Scalar]] = []
        for index, row in enumerate(self.rows, 1):
            if set(row) != expected:
                raise ValueError(
                    f"Række {index} i {name} matcher ikke tabellens kolonner"
                )
            values: dict[str, Scalar] = {}
            for column in self.columns:
                value = row[column]
                if isinstance(value, (datetime, date)):
                    value = value.isoformat()
                elif isinstance(value, Decimal):
                    value = float(value)
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("NaN og uendelige tal kan ikke eksporteres")
                if value is not None and not isinstance(value, (str, int, float, bool)):
                    raise TypeError(
                        f"{name}.{column} indeholder en ikke-portabel værdi"
                    )
                values[column] = value
            normalized.append(values)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "rows", tuple(normalized))


@dataclass(frozen=True, slots=True)
class DataPackage:
    """A versioned collection of related tables and their provenance."""

    document_type: str
    scope: Mapping[str, Scalar]
    generated_at: datetime
    provenance: Mapping[str, Scalar]
    tables: tuple[DataTable, ...]
    schema_version: int = DATA_PACKAGE_SCHEMA_VERSION
    privacy_profile: str | None = None
    restorable: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != DATA_PACKAGE_SCHEMA_VERSION:
            raise ValueError("Ukendt DataPackage-schema")
        if not self.document_type.strip():
            raise ValueError("Datapakken skal have en dokumenttype")
        if self.generated_at.tzinfo is None:
            raise ValueError("Datapakkens tidspunkt skal have tidszone")
        if not self.tables:
            raise ValueError("Datapakken skal indeholde mindst én tabel")
        names = [table.name.casefold() for table in self.tables]
        if len(names) != len(set(names)):
            raise ValueError("Datapakken må ikke have duplikerede tabelnavne")
        if self.privacy_profile not in {None, "share", "debug"}:
            raise ValueError("Ukendt anonymiseringsprofil")
        object.__setattr__(self, "document_type", self.document_type.strip())
        object.__setattr__(self, "scope", _normalize_metadata(self.scope, label="scope"))
        object.__setattr__(
            self,
            "provenance",
            _normalize_metadata(self.provenance, label="provenance"),
        )


@dataclass(frozen=True, slots=True)
class SerializedPackage:
    content: bytes
    extension: str
    mime_type: str


def package_to_dict(package: DataPackage) -> dict[str, object]:
    return {
        "schema_version": package.schema_version,
        "document_type": package.document_type,
        "scope": dict(package.scope),
        "generated_at": package.generated_at.isoformat(),
        "provenance": dict(package.provenance),
        "privacy_profile": package.privacy_profile,
        "restorable": package.restorable,
        "tables": [
            {
                "name": table.name,
                "columns": list(table.columns),
                "row_count": len(table.rows),
                "rows": [dict(row) for row in table.rows],
            }
            for table in package.tables
        ],
    }


def _manifest(package: DataPackage, files: Sequence[tuple[str, bytes]]) -> bytes:
    payload = {
        "schema_version": DATA_PACKAGE_SCHEMA_VERSION,
        "document_type": package.document_type,
        "scope": dict(package.scope),
        "generated_at": package.generated_at.isoformat(),
        "provenance": dict(package.provenance),
        "privacy_profile": package.privacy_profile,
        "restorable": package.restorable,
        "files": [
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in files
        ],
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def neutralize_spreadsheet_text(value: str) -> str:
    """Neutralize formula-like text while preserving non-spreadsheet exports."""

    if not value:
        return value
    stripped = value.lstrip(" \v\f")
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _spreadsheet_value(value: Scalar) -> Scalar:
    return neutralize_spreadsheet_text(value) if isinstance(value, str) else value


def table_to_csv(table: DataTable) -> bytes:
    """Serialize RFC 4180 CSV as UTF-8 with BOM."""

    output = StringIO(newline="")
    writer = csv.writer(output, dialect="excel", lineterminator="\r\n")
    writer.writerow(table.columns)
    for row in table.rows:
        writer.writerow(
            "" if row[column] is None else _spreadsheet_value(row[column])
            for column in table.columns
        )
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def _zip_files(package: DataPackage, files: Sequence[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for name, content in files:
            archive.writestr(name, content)
        archive.writestr("manifest.json", _manifest(package, files))
    return output.getvalue()


def package_to_csv(package: DataPackage) -> SerializedPackage:
    files = tuple((f"{table.name}.csv", table_to_csv(table)) for table in package.tables)
    if len(files) == 1:
        return SerializedPackage(files[0][1], "csv", "text/csv; charset=utf-8")
    return SerializedPackage(_zip_files(package, files), "zip", "application/zip")


def _xlsx_sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "_", name).strip("'")[:31] or "Data"
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        marker = f"-{suffix}"
        candidate = base[: 31 - len(marker)] + marker
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def package_to_xlsx(package: DataPackage) -> SerializedPackage:
    try:
        import xlsxwriter
    except ImportError as exc:  # pragma: no cover - dependency-specific branch
        raise PayloadError(
            "XLSX kræver afhængigheden xlsxwriter. Installér projektets standardafhængigheder."
        ) from exc

    output = BytesIO()
    workbook = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "strings_to_numbers": False,
        },
    )
    header = workbook.add_format({"bold": True, "bg_color": "#E8EEF7"})
    used: set[str] = set()
    try:
        for table in package.tables:
            worksheet = workbook.add_worksheet(_xlsx_sheet_name(table.name, used))
            worksheet.freeze_panes(1, 0)
            for column_index, column in enumerate(table.columns):
                worksheet.write(0, column_index, column, header)
            for row_index, row in enumerate(table.rows, 1):
                for column_index, column in enumerate(table.columns):
                    value = _spreadsheet_value(row[column])
                    if value is None:
                        worksheet.write_blank(row_index, column_index, None)
                    elif isinstance(value, bool):
                        worksheet.write_boolean(row_index, column_index, value)
                    elif isinstance(value, (int, float)):
                        worksheet.write_number(row_index, column_index, value)
                    else:
                        worksheet.write_string(row_index, column_index, value)
            worksheet.set_column(0, max(0, len(table.columns) - 1), 18)
            if table.rows:
                worksheet.autofilter(0, 0, len(table.rows), len(table.columns) - 1)
    finally:
        workbook.close()
    return SerializedPackage(
        output.getvalue(),
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _table_to_parquet(package: DataPackage, table: DataTable) -> bytes:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - dependency-specific branch
        raise PayloadError(
            "Parquet er valgfrit. Installér holdet-lib[parquet] for at aktivere formatet."
        ) from exc

    arrays = {column: [row[column] for row in table.rows] for column in table.columns}
    arrow_table = pa.table(arrays)
    metadata = dict(arrow_table.schema.metadata or {})
    metadata[b"holdet.data_package"] = json.dumps(
        {
            "schema_version": package.schema_version,
            "document_type": package.document_type,
            "table": table.name,
            "scope": dict(package.scope),
            "generated_at": package.generated_at.isoformat(),
            "provenance": dict(package.provenance),
            "privacy_profile": package.privacy_profile,
            "restorable": package.restorable,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    arrow_table = arrow_table.replace_schema_metadata(metadata)
    output = BytesIO()
    pq.write_table(arrow_table, output, compression="snappy")
    return output.getvalue()


def package_to_parquet(package: DataPackage) -> SerializedPackage:
    files = tuple(
        (f"{table.name}.parquet", _table_to_parquet(package, table))
        for table in package.tables
    )
    if len(files) == 1:
        return SerializedPackage(files[0][1], "parquet", "application/vnd.apache.parquet")
    return SerializedPackage(_zip_files(package, files), "zip", "application/zip")


def serialize_data_package(package: DataPackage, format: str) -> SerializedPackage:
    selected = format.casefold()
    if selected == "csv":
        return package_to_csv(package)
    if selected == "xlsx":
        return package_to_xlsx(package)
    if selected == "parquet":
        return package_to_parquet(package)
    raise ValueError(f"Ikke-understøttet tabulært format: {format}")
