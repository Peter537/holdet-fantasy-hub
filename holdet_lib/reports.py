"""Cache-only manager-game and season report projections and HTML rendering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Iterable

from .data_packages import DataPackage, DataTable
from .groups import GroupDefinition, ManagerGame
from .hall_of_fame import HallOfFameEvent
from .hub_settings import HallOfFameScoreProfile
from .output import sanitize_path_component
from .persistence import aware_local, publish_immutable
from .seasons import SeasonDefinition, build_season_standings
from .standings import build_standings
from .storage import SnapshotIndex


REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    path: Path
    content: bytes
    mime_type: str = "text/html; charset=utf-8"


def _same_game(locale: str, slug: str, other_locale: str, other_slug: str) -> bool:
    return locale.casefold() == other_locale.casefold() and slug == other_slug


def build_manager_game_report_package(
    manager_game: ManagerGame,
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    *,
    generated_at: datetime | None = None,
) -> DataPackage:
    """Project only already-cached data for one manager game."""

    created = aware_local(generated_at)
    game = manager_game.game
    selected_groups = tuple(
        group
        for group in groups
        if _same_game(
            group.game.locale,
            group.game.slug,
            game.locale,
            game.slug,
        )
    )
    newest_by_team: dict[int, object] = {}
    for snapshot in snapshots.snapshots:
        snapshot_game = snapshot.team.reference.game
        if _same_game(
            snapshot_game.locale,
            snapshot_game.slug,
            game.locale,
            game.slug,
        ):
            newest_by_team.setdefault(snapshot.team.reference.team_id, snapshot)

    team_rows: list[dict[str, object]] = []
    history_rows: list[dict[str, object]] = []
    source_times: list[datetime] = []
    for team_id, raw_snapshot in sorted(newest_by_team.items()):
        snapshot = raw_snapshot
        team = snapshot.team  # type: ignore[attr-defined]
        source_times.append(snapshot.generated_at)  # type: ignore[attr-defined]
        overview = team.overview
        team_rows.append(
            {
                "team_id": team_id,
                "team_name": team.team_name,
                "manager_name": team.owner_name,
                "current_round": overview.current_round,
                "rank": overview.rank,
                "total": overview.total,
                "current_change": overview.current_change,
                "unit": overview.unit,
                "snapshot_at": snapshot.generated_at.isoformat(),  # type: ignore[attr-defined]
            }
        )
        for summary in team.history:
            history_rows.append(
                {
                    "team_id": team_id,
                    "team_name": team.team_name,
                    "manager_name": team.owner_name,
                    "round": summary.round_number,
                    "round_status": summary.round_status,
                    "total": summary.total,
                    "change": summary.change,
                    "rank": summary.overall_rank,
                    "round_rank": summary.round_rank,
                }
            )

    group_rows: list[dict[str, object]] = []
    standing_rows: list[dict[str, object]] = []
    for group in sorted(selected_groups, key=lambda item: item.name.casefold()):
        group_rounds = [
            summary.round_number
            for member in group.teams
            for snapshot in snapshots.for_team(group.game, member.team_id)
            for summary in snapshot.team.history
        ]
        latest_round = max(group_rounds, default=None)
        group_rows.append(
            {
                "group_id": group.group_id,
                "group_name": group.name,
                "members": len(group.teams),
                "latest_round": latest_round,
                "tournament": group.tournament is not None,
            }
        )
        if latest_round is None:
            continue
        for row in build_standings(group, snapshots, latest_round, "overall"):
            standing_rows.append(
                {
                    "group_id": group.group_id,
                    "group_name": group.name,
                    "round": latest_round,
                    "rank": row.rank,
                    "team_id": row.team_id,
                    "team_name": row.team_name,
                    "manager_name": row.owner_name,
                    "total": row.total,
                    "change": row.change,
                    "distance": row.distance,
                    "status": "mangler" if row.summary is None else "klar",
                }
            )

    history_rows.sort(key=lambda row: (int(row["round"]), str(row["team_name"]).casefold()))
    return DataPackage(
        document_type="manager_game_report",
        scope={"locale": game.locale, "game": game.slug, "label": manager_game.name},
        generated_at=created,
        provenance={
            "mode": "cache-only",
            "snapshot_count": len(newest_by_team),
            "newest_snapshot_at": (
                max(source_times).isoformat() if source_times else None
            ),
            "warnings": " | ".join(snapshots.warnings) if snapshots.warnings else None,
        },
        tables=(
            DataTable(
                "game",
                ("locale", "game", "label", "teams", "groups"),
                (
                    {
                        "locale": game.locale,
                        "game": game.slug,
                        "label": manager_game.name,
                        "teams": len(team_rows),
                        "groups": len(group_rows),
                    },
                ),
            ),
            DataTable(
                "teams",
                (
                    "team_id",
                    "team_name",
                    "manager_name",
                    "current_round",
                    "rank",
                    "total",
                    "current_change",
                    "unit",
                    "snapshot_at",
                ),
                tuple(team_rows),
            ),
            DataTable(
                "groups",
                ("group_id", "group_name", "members", "latest_round", "tournament"),
                tuple(group_rows),
            ),
            DataTable(
                "group_standings",
                (
                    "group_id",
                    "group_name",
                    "round",
                    "rank",
                    "team_id",
                    "team_name",
                    "manager_name",
                    "total",
                    "change",
                    "distance",
                    "status",
                ),
                tuple(standing_rows),
            ),
            DataTable(
                "round_history",
                (
                    "team_id",
                    "team_name",
                    "manager_name",
                    "round",
                    "round_status",
                    "total",
                    "change",
                    "rank",
                    "round_rank",
                ),
                tuple(history_rows),
            ),
        ),
    )


def build_season_report_package(
    season: SeasonDefinition,
    events: tuple[HallOfFameEvent, ...],
    score_profile: HallOfFameScoreProfile,
    *,
    generated_at: datetime | None = None,
) -> DataPackage:
    """Project season standings, point components and medal counts."""

    created = aware_local(generated_at)
    selected = tuple(
        event
        for event in events
        if any(
            event.competition_id == competition
            or event.competition_id.startswith(f"{competition}:")
            for competition in season.competition_ids
        )
    )
    standings = build_season_standings(season, events, score_profile)
    competition_rows: list[dict[str, object]] = []
    for competition_id in season.competition_ids:
        matches = tuple(
            event
            for event in selected
            if event.competition_id == competition_id
            or event.competition_id.startswith(f"{competition_id}:")
        )
        competition_rows.append(
            {
                "competition_id": competition_id,
                "competition_name": matches[-1].competition_name if matches else competition_id,
                "events": len(matches),
                "complete": bool(matches) and all(event.complete for event in matches),
                "latest_capture": (
                    max(event.captured_at for event in matches).isoformat()
                    if matches
                    else None
                ),
            }
        )
    medal_counts: dict[tuple[str, str], list[int]] = {}
    for event in selected:
        if event.kind == "round_win":
            continue
        for placement in event.placements:
            if placement.rank not in {1, 2, 3}:
                continue
            counts = medal_counts.setdefault(
                (placement.manager_id, placement.manager_name), [0, 0, 0]
            )
            counts[placement.rank - 1] += 1
    return DataPackage(
        document_type="season_report",
        scope={"season_id": season.season_id, "season_name": season.name},
        generated_at=created,
        provenance={
            "mode": "cache-only",
            "event_count": len(selected),
            "latest_capture": (
                max(event.captured_at for event in selected).isoformat()
                if selected
                else None
            ),
            "score_profile": getattr(score_profile, "name", type(score_profile).__name__),
        },
        tables=(
            DataTable(
                "competitions",
                (
                    "competition_id",
                    "competition_name",
                    "events",
                    "complete",
                    "latest_capture",
                ),
                tuple(competition_rows),
            ),
            DataTable(
                "season_standings",
                (
                    "rank",
                    "manager_id",
                    "manager_name",
                    "points",
                    "titles",
                    "podiums",
                    "competitions",
                    "round_wins",
                ),
                tuple(
                    {
                        "rank": row.rank,
                        "manager_id": row.manager_id,
                        "manager_name": row.manager_name,
                        "points": row.points,
                        "titles": row.titles,
                        "podiums": row.podiums,
                        "competitions": row.competitions,
                        "round_wins": row.round_wins,
                    }
                    for row in standings
                ),
            ),
            DataTable(
                "points_composition",
                ("manager_id", "manager_name", "points", "titles", "podiums", "round_wins"),
                tuple(
                    {
                        "manager_id": row.manager_id,
                        "manager_name": row.manager_name,
                        "points": row.points,
                        "titles": row.titles,
                        "podiums": row.podiums,
                        "round_wins": row.round_wins,
                    }
                    for row in standings
                ),
            ),
            DataTable(
                "medals",
                ("manager_id", "manager_name", "gold", "silver", "bronze"),
                tuple(
                    {
                        "manager_id": manager_id,
                        "manager_name": manager_name,
                        "gold": counts[0],
                        "silver": counts[1],
                        "bronze": counts[2],
                    }
                    for (manager_id, manager_name), counts in sorted(
                        medal_counts.items(), key=lambda item: item[0][1].casefold()
                    )
                ),
            ),
        ),
    )


_TABLE_TITLES = {
    "game": "Rundestatus",
    "teams": "Hold",
    "groups": "Grupper og turneringer",
    "group_standings": "Stillinger",
    "round_history": "Rundehistorik",
    "competitions": "Konkurrencer",
    "season_standings": "Samlet managerstilling",
    "points_composition": "Pointsammensætning",
    "medals": "Medaljer",
}


def render_html_report(package: DataPackage, *, title: str) -> str:
    """Render one escaped, standalone HTML file without scripts or assets."""

    metadata = [
        ("Oprettet", package.generated_at.isoformat()),
        *[(str(key), "–" if value is None else str(value)) for key, value in package.provenance.items()],
    ]
    sections: list[str] = []
    for table in package.tables:
        heading = _TABLE_TITLES.get(table.name, table.name.replace("_", " ").title())
        if not table.rows:
            body = '<p class="empty">Ingen lokale data i dette afsnit.</p>'
        else:
            headers = "".join(f"<th scope=\"col\">{escape(column)}</th>" for column in table.columns)
            rows = []
            for row in table.rows:
                cells = "".join(
                    f"<td>{escape('–' if row[column] is None else str(row[column]))}</td>"
                    for column in table.columns
                )
                rows.append(f"<tr>{cells}</tr>")
            body = (
                '<div class="table-wrap"><table><thead><tr>'
                + headers
                + "</tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table></div>"
            )
        sections.append(f"<section><h2>{escape(heading)}</h2>{body}</section>")
    provenance = "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in metadata
    )
    return f"""<!doctype html>
<html lang="da"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
:root{{--ink:#14213d;--muted:#5b6474;--line:#d8dee9;--wash:#f5f7fa;--accent:#d43f3a}}
*{{box-sizing:border-box}} body{{margin:0;color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1180px;margin:auto;padding:32px}} h1{{font-size:2rem;margin:0 0 8px}} h2{{font-size:1.2rem;margin:28px 0 10px}}
.lede,.empty{{color:var(--muted)}} dl{{display:grid;grid-template-columns:minmax(130px,220px) 1fr;gap:4px 16px;background:var(--wash);padding:16px;border-radius:8px}}
dt{{font-weight:650}} dd{{margin:0;overflow-wrap:anywhere}} .table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:8px}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}} th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{background:var(--wash);font-size:.83rem}} tr:last-child td{{border-bottom:0}} footer{{margin-top:32px;color:var(--muted);font-size:.85rem}}
@page{{size:A4;margin:14mm}} @media print{{main{{max-width:none;padding:0}} .table-wrap{{overflow:visible;border-color:#999}} section{{break-inside:avoid}} a{{color:inherit;text-decoration:none}}}}
@media (max-width:600px){{main{{padding:20px 14px}} dl{{grid-template-columns:1fr;gap:2px}} dd{{margin-bottom:8px}}}}
</style></head><body><main><header><h1>{escape(title)}</h1><p class="lede">Lokal cache-rapport · schema {REPORT_SCHEMA_VERSION}</p></header>
<section aria-labelledby="proveniens"><h2 id="proveniens">Dataproveniens</h2><dl>{provenance}</dl></section>
{"".join(sections)}<footer>Rapporten indeholder ingen JavaScript eller eksterne assets. Brug browserens Udskriv-funktion for PDF.</footer>
</main></body></html>"""


class ReportStore:
    """Explicitly publish immutable HTML reports."""

    def __init__(self, report_dir: Path | str) -> None:
        self.report_dir = Path(report_dir)

    def save(self, package: DataPackage, *, title: str, stem: str) -> ReportArtifact:
        content = render_html_report(package, title=title).encode("utf-8")
        game = package.scope.get("game")
        if isinstance(game, str) and game.strip():
            scope_dir = sanitize_path_component(game, fallback="game")
        else:
            season = package.scope.get("season_id")
            season_dir = sanitize_path_component(str(season), fallback="season")
            scope_dir = f"season-{season_dir}"
        target_dir = self.report_dir / scope_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        base = "".join(character if character.isalnum() or character in "-_" else "-" for character in stem).strip("-") or "report"
        number = 0
        while True:
            suffix = "" if number == 0 else f"-{number + 1}"
            path = target_dir / f"{base}{suffix}.html"
            try:
                publish_immutable(path, content)
            except FileExistsError:
                number += 1
                continue
            return ReportArtifact(path.resolve(), content)
