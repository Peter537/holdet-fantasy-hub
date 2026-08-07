"""Irreversible anonymization and checksummed support bundles."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from io import BytesIO
import json
import re
import zipfile

from .data_packages import DataPackage, DataTable, package_to_dict


ANONYMIZATION_PROFILES = ("share", "debug")

_PRIVATE_FIELDS = frozenset(
    {
        "account",
        "account_key",
        "account_label",
        "owner",
        "owner_name",
        "owner_user_id",
        "manager",
        "manager_id",
        "manager_name",
        "group_id",
        "group_name",
        "team_id",
        "team_name",
        "user_id",
        "profile_url",
    }
)
_DEBUG_DROP_MARKERS = (
    "path",
    "url",
    "note",
    "tag",
    "label",
    "description",
    "comment",
    "free_text",
)
_DEBUG_ENUM_FIELDS = frozenset(
    {
        "locale",
        "format",
        "unit",
        "scope",
        "status",
        "statuses",
        "role",
        "round_status",
        "document_type",
        "variant",
    }
)


def _pseudonym(kind: str, value: object, salt: str) -> str:
    digest = hashlib.sha256(
        f"{salt}|{kind}|{value}".encode("utf-8", errors="replace")
    ).hexdigest()[:10].upper()
    labels = {
        "manager": "Manager",
        "team": "Fantasyhold",
        "account": "Konto",
        "user": "Bruger",
        "text": "Tekst",
    }
    return f"{labels.get(kind, 'ID')} {digest}"


def _field_kind(field: str) -> str:
    lowered = field.casefold()
    if "manager" in lowered or "owner" in lowered:
        return "manager"
    if "team" in lowered:
        return "team"
    if "account" in lowered:
        return "account"
    return "user"


def _is_url_or_path(value: str) -> bool:
    return bool(
        re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I)
        or re.match(r"^(?:[A-Za-z]:[\\/]|/)", value)
    )


def _anonymize_mapping(
    values: dict[str, object], *, profile: str, salt: str
) -> dict[str, object]:
    result: dict[str, object] = {}
    for field, value in values.items():
        lowered = field.casefold()
        if profile == "debug" and any(marker in lowered for marker in _DEBUG_DROP_MARKERS):
            result[field] = None
            continue
        if lowered in _PRIVATE_FIELDS and value not in {None, ""}:
            result[field] = _pseudonym(_field_kind(lowered), value, salt)
            continue
        if profile == "debug" and isinstance(value, str):
            if _is_url_or_path(value):
                result[field] = None
            elif lowered in _DEBUG_ENUM_FIELDS:
                result[field] = value
            elif value:
                result[field] = _pseudonym("text", value, salt)
            else:
                result[field] = value
            continue
        if isinstance(value, str) and _is_url_or_path(value):
            result[field] = None if profile == "debug" else "[fjernet]"
        else:
            result[field] = value
    return result


def anonymize_data_package(package: DataPackage, profile: str) -> DataPackage:
    selected = profile.casefold()
    if selected not in ANONYMIZATION_PROFILES:
        raise ValueError(f"Ukendt anonymiseringsprofil: {profile}")
    salt = hashlib.sha256(
        (
            f"{package.document_type}|{package.generated_at.isoformat()}|"
            + json.dumps(dict(package.scope), sort_keys=True, ensure_ascii=False)
        ).encode("utf-8")
    ).hexdigest()
    tables = tuple(
        DataTable(
            table.name,
            table.columns,
            tuple(
                _anonymize_mapping(dict(row), profile=selected, salt=salt)
                for row in table.rows
            ),
        )
        for table in package.tables
    )
    return replace(
        package,
        scope=_anonymize_mapping(dict(package.scope), profile=selected, salt=salt),
        provenance=_anonymize_mapping(
            dict(package.provenance), profile=selected, salt=salt
        ),
        tables=tables,
        privacy_profile=selected,
        restorable=False,
    )


def build_support_bundle(package: DataPackage, profile: str) -> bytes:
    """Create a non-restorable ZIP without a reversible pseudonym mapping."""

    anonymized = anonymize_data_package(package, profile)
    payload = (
        json.dumps(package_to_dict(anonymized), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "document_type": "anonymized_support_bundle",
        "created_at": anonymized.generated_at.isoformat(),
        "privacy_profile": anonymized.privacy_profile,
        "restorable": False,
        "files": [
            {
                "path": "data-package.json",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        archive.writestr("data-package.json", payload)
        archive.writestr(
            "support-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
    return output.getvalue()
