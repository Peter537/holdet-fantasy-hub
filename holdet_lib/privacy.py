"""Pseudonymized support projections and checksummed ZIP bundles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
from io import BytesIO
import json
import secrets
import zipfile

from .data_packages import DataPackage, DataTable, Scalar, package_to_dict


ANONYMIZATION_PROFILES = ("share", "debug")
SUPPORT_BUNDLE_FILENAME = "holdet-support.zip"


@dataclass(frozen=True, slots=True)
class _PackagePolicy:
    scope: str
    provenance: str
    tables: Mapping[str, str]


_TEAM_FIELDS = "team_id team_name manager_name"
_GROUP_FIELDS = "group_id group_name"
_MANAGER_FIELDS = "manager_id manager_name"
_SEASON_RESULTS = f"{_MANAGER_FIELDS} points titles podiums round_wins"

# Adding a field to a report does not add it to these support projections.
_POLICIES = {
    "manager_game_report": _PackagePolicy(
        scope="locale game label",
        provenance="mode snapshot_count newest_snapshot_at",
        tables={
            "game": "locale game label teams groups",
            "teams": f"{_TEAM_FIELDS} current_round rank total current_change unit snapshot_at",
            "groups": f"{_GROUP_FIELDS} members latest_round tournament",
            "group_standings": f"{_GROUP_FIELDS} {_TEAM_FIELDS} round rank total change distance status",
            "round_history": f"{_TEAM_FIELDS} round round_status total change rank round_rank",
        },
    ),
    "season_report": _PackagePolicy(
        scope="season_id season_name",
        provenance="mode event_count latest_capture",
        tables={
            "competitions": "competition_id competition_name events complete latest_capture",
            "season_standings": f"rank {_SEASON_RESULTS} competitions",
            "points_composition": _SEASON_RESULTS,
            "medals": f"{_MANAGER_FIELDS} gold silver bronze",
        },
    ),
    "player_export": _PackagePolicy(
        scope="locale game round",
        provenance="source_generated_at unit output_order",
        tables={
            "players": "name team position value total_growth round_growth popularity popularity_change trend index",
        },
    ),
    "team_export": _PackagePolicy(
        scope="locale game team_id round scope",
        provenance="source_generated_at roster_generated_at unit",
        tables={
            "team": "team_id team_name owner_name account_key account_label",
            "overview": (
                "current_round unit player_value bank total current_change rank rank_change top_percent "
                "substitutions_remaining substitutions_limit substitutions_used"
            ),
            "roster": "player_id name team position value round_change since_purchase_change purchase_round role",
            "history": (
                "round total change bank player_value bank_change interest player_change transfer captain_bonus "
                "special_bonus substitutions_used round_rank overall_rank round_rank_change overall_rank_change"
            ),
        },
    ),
}
_IDENTIFIERS = {
    "account_key": "Konto",
    "competition_id": "Konkurrence",
    "game": "Spil",
    "group_id": "Gruppe",
    "manager_id": "Manager",
    "player_id": "Spiller",
    "season_id": "Sæson",
    "team_id": "Fantasyhold",
}
_LABELS = {
    "account_label": "Kontonavn",
    "competition_name": "Konkurrencenavn",
    "group_name": "Gruppenavn",
    "label": "Spilnavn",
    "manager_name": "Managernavn",
    "name": "Spillernavn",
    "owner_name": "Managernavn",
    "position": "Position",
    "season_name": "Sæsonnavn",
    "team": "Klubnavn",
    "team_name": "Holdnavn",
}
_CHOICES = {
    "locale": frozenset({"da", "en", "nb", "no", "sv"}),
    "mode": frozenset({"cache-only"}),
    "output_order": frozenset({"query", "display"}),
    "role": frozenset({"captain", "none"}),
    "round_status": frozenset({"complete", "in_progress", "unknown"}),
    "scope": frozenset({"full", "round"}),
    "status": frozenset({"mangler", "klar"}),
    "unit": frozenset({"money", "points"}),
}
_TIMESTAMPS = frozenset({
    "latest_capture", "newest_snapshot_at", "roster_generated_at", "snapshot_at", "source_generated_at",
})


def _selected_fields(fields: str, *, debug: bool) -> tuple[str, ...]:
    return tuple(
        field for field in fields.split()
        if not debug or (field not in _LABELS and field not in _TIMESTAMPS)
    )


def _project_value(field: str, value: Scalar, secret: bytes) -> Scalar:
    if kind := _IDENTIFIERS.get(field) or _LABELS.get(field):
        if type(value) not in (str, int) or value == "":
            return None
        message = f"{kind}\0{type(value).__name__}\0{value}".encode("utf-8", errors="surrogatepass")
        token = hmac.digest(secret, message, "sha256").hex()[:16].upper()
        return f"{kind} {token}"
    if choices := _CHOICES.get(field):
        return value if isinstance(value, str) and value in choices else None
    if field in _TIMESTAMPS:
        if not isinstance(value, str):
            return None
        try:
            timestamp = datetime.fromisoformat(value)
            return timestamp.astimezone(timezone.utc).isoformat() if timestamp.tzinfo is not None else None
        except (ValueError, OverflowError):
            return None
    return value if type(value) in (bool, int, float) else None


def anonymize_data_package(package: DataPackage, profile: str) -> DataPackage:
    """Project approved support fields with pseudonyms scoped to this export."""
    selected = profile.casefold()
    if selected not in ANONYMIZATION_PROFILES:
        raise ValueError(f"Ukendt profil for supportpakke: {profile}")
    debug = selected == "debug"
    secret = secrets.token_bytes(32)
    policy = _POLICIES.get(package.document_type)
    scope: dict[str, Scalar] = {}
    provenance: dict[str, Scalar] = {}
    tables: list[DataTable] = []
    if policy is not None:
        scope = {
            field: _project_value(field, package.scope[field], secret)
            for field in _selected_fields(policy.scope, debug=debug)
            if field in package.scope
        }
        provenance = {
            field: _project_value(field, package.provenance[field], secret)
            for field in _selected_fields(policy.provenance, debug=debug)
            if field in package.provenance
        }
        for table in package.tables:
            fields = _selected_fields(policy.tables.get(table.name, ""), debug=debug)
            columns = tuple(field for field in fields if field in table.columns)
            if columns:
                tables.append(DataTable(
                    table.name,
                    columns,
                    tuple(
                        {field: _project_value(field, row[field], secret) for field in columns}
                        for row in table.rows
                    ),
                ))
    if not tables:
        tables.append(DataTable(
            "summary",
            ("table_count", "row_count"),
            ({"table_count": len(package.tables), "row_count": sum(len(table.rows) for table in package.tables)},),
        ))
    return replace(
        package,
        document_type=package.document_type if policy is not None else "support_summary",
        scope=scope,
        provenance=provenance,
        tables=tuple(tables),
        privacy_profile=selected,
        restorable=False,
    )


def build_support_bundle(package: DataPackage, profile: str) -> bytes:
    """Create a non-restorable ZIP without including the pseudonym key."""
    projected = anonymize_data_package(package, profile)
    payload = (
        json.dumps(package_to_dict(projected), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "document_type": "anonymized_support_bundle",
        "created_at": projected.generated_at.isoformat(),
        "privacy_profile": projected.privacy_profile,
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
