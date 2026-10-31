"""Persistent published tournament pairings, isolated by tournament revision."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path

from .errors import PayloadError
from .persistence import replace_text_atomically
from .tournament import (
    GroupFixture,
    TournamentConfig,
    TournamentPairing,
    PublishedKnockoutMatch,
    _knockout_layout,
    validate_tournament_config,
)


PAIRING_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class TournamentPairingRevision:
    group_id: str
    tournament_revision: int
    pairings: tuple[TournamentPairing, ...] = ()
    knockout_matches: tuple[PublishedKnockoutMatch, ...] = ()

    @property
    def published_rounds(self) -> tuple[int, ...]:
        return tuple(sorted({item.round_number for item in self.pairings}))



def validate_tournament_pairing_revision(
    value: TournamentPairingRevision,
    config: TournamentConfig,
    team_ids: tuple[int, ...],
) -> TournamentPairingRevision:
    """Validate persisted pairings in the context of their Swiss revision."""

    if value.tournament_revision < 1 or not value.group_id.strip():
        raise PayloadError("Turneringsparringerne har en ugyldig revision")
    if config.template != "swiss":
        if value.pairings or config.template not in {"group_knockout", "double_elimination"}:
            raise PayloadError("Parringerne passer ikke til turneringsformatet")
        layout = {item.match_id: item for item in _knockout_layout(config)}
        if len({item.match_id for item in value.knockout_matches}) != len(value.knockout_matches):
            raise PayloadError("Publicerede knockoutkampe indeholder dubletter")
        for item in value.knockout_matches:
            slot = layout.get(item.match_id)
            if slot is None or (
                item.stage, item.match_index, item.round_numbers, item.source_a, item.source_b
            ) != (slot.stage, slot.match_index, slot.round_numbers, slot.source_a, slot.source_b):
                raise PayloadError("Publiceret knockoutkamp passer ikke til programmet")
            ids = (item.team_a_id, item.team_b_id)
            if (all(team_id is None for team_id in ids)
                or any(team_id is not None and (isinstance(team_id, bool) or not isinstance(team_id, int) or team_id not in team_ids) for team_id in ids)
                or item.team_a_id is not None and item.team_a_id == item.team_b_id
                or config.template == "group_knockout" and None in ids
                or any(seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 1 or seed > len(team_ids))
                       for seed in (item.team_a_seed, item.team_b_seed))
                or any((team_id is None) != (seed is None)
                       for team_id, seed in zip(ids, (item.team_a_seed, item.team_b_seed)))):
                raise PayloadError("Publiceret knockoutkamp har ugyldige deltagere eller seeds")
            for source, team_id, seed in zip(
                (slot.source_a, slot.source_b), ids, (item.team_a_seed, item.team_b_seed)
            ):
                if source == "bye" and team_id is not None:
                    raise PayloadError("En friplads kan ikke have en deltager")
                if source.startswith("seed:"):
                    expected_seed = int(source.split(":", 1)[1])
                    if seed != expected_seed or (
                        config.template == "double_elimination"
                        and team_id != config.seed_order[expected_seed - 1]
                    ):
                        raise PayloadError("Publiceret knockoutseed passer ikke til programmet")
        return value
    if value.knockout_matches:
        raise PayloadError("Swiss-turneringer kan ikke have knockoutkampe")
    if any(
        item.round_number < config.start_round
        or item.round_number > config.final_round
        or item.team_a_id <= 0
        or item.team_b_id is not None
        and item.team_b_id <= 0
        or item.team_a_id == item.team_b_id
        for item in value.pairings
    ):
        raise PayloadError(
            "Turneringsparringerne indeholder ugyldige deltagere eller runder"
        )
    stored_keys = [
        (item.round_number, item.team_a_id, item.team_b_id)
        for item in value.pairings
    ]
    if len(stored_keys) != len(set(stored_keys)):
        raise PayloadError("Turneringsparringerne indeholder dubletter")
    existing = {
        (item.round_number, item.team_a_id, item.team_b_id)
        for item in config.group_fixtures
    }
    extras = tuple(
        GroupFixture(item.round_number, item.team_a_id, item.team_b_id)
        for item in value.pairings
        if (item.round_number, item.team_a_id, item.team_b_id) not in existing
    )
    validate_tournament_config(
        replace(config, group_fixtures=(*config.group_fixtures, *extras)),
        team_ids,
    )
    return value

class TournamentPairingStore:
    """Keep published fixtures stable while results may be corrected."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def _path(self, group_id: str, revision: int) -> Path:
        return self.directory / group_id / f"revision-{revision}.json"

    def has_revision(self, group_id: str, revision: int) -> bool:
        """Distinguish an explicit empty baseline from a legacy missing file."""
        return self._path(group_id, revision).exists()

    def with_published_knockout_matches(self, group):
        """Attach validated runtime fixtures, retaining None for legacy data."""
        config = group.tournament
        if config is None or config.template not in {"group_knockout", "double_elimination"}:
            return group
        if not self.has_revision(group.group_id, group.active_revision):
            return group
        revision = self.load_for_tournament(
            group.group_id, group.active_revision, config,
            tuple(item.team_id for item in group.teams),
        )
        return replace(group, tournament=replace(config, published_matches=revision.knockout_matches))

    def with_published_matches(self, group):
        """Read and validate fixtures against this revision's own membership."""
        config = group.tournament
        if config is None or config.template != "swiss":
            return self.with_published_knockout_matches(group)
        revision = self.load_for_tournament(
            group.group_id, group.active_revision, config,
            tuple(item.team_id for item in group.teams),
        )
        existing = {(item.round_number, item.team_a_id, item.team_b_id)
                    for item in config.group_fixtures}
        extras = tuple(GroupFixture(item.round_number, item.team_a_id, item.team_b_id)
                       for item in revision.pairings
                       if (item.round_number, item.team_a_id, item.team_b_id) not in existing)
        return replace(group, tournament=replace(config, group_fixtures=(*config.group_fixtures, *extras)))

    def load(self, group_id: str, revision: int) -> TournamentPairingRevision:
        path = self._path(group_id, revision)
        if not path.exists():
            return TournamentPairingRevision(group_id, revision)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Turneringsparringer kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Turneringsparringerne indeholder ugyldig JSON") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in (1, PAIRING_SCHEMA_VERSION)
            or payload.get("group_id") != group_id
            or payload.get("tournament_revision") != revision
            or not isinstance(payload.get("pairings"), list)
        ):
            raise PayloadError("Ugyldigt lager for turneringsparringer")
        pairings: list[TournamentPairing] = []
        for raw in payload["pairings"]:
            if not isinstance(raw, dict):
                raise PayloadError("Ugyldig turneringsparring")
            round_number = raw.get("round")
            team_a_id = raw.get("team_a_id")
            team_b_id = raw.get("team_b_id")
            if (
                not isinstance(round_number, int)
                or isinstance(round_number, bool)
                or not isinstance(team_a_id, int)
                or isinstance(team_a_id, bool)
                or (
                    team_b_id is not None
                    and (not isinstance(team_b_id, int) or isinstance(team_b_id, bool))
                )
            ):
                raise PayloadError("Ugyldig turneringsparring")
            pairings.append(TournamentPairing(round_number, team_a_id, team_b_id, True))
        knockout_raw = payload.get("knockout_matches", [])
        if not isinstance(knockout_raw, list):
            raise PayloadError("Ugyldige publicerede knockoutkampe")
        knockout: list[PublishedKnockoutMatch] = []
        for raw in knockout_raw:
            if not isinstance(raw, dict):
                raise PayloadError("Ugyldig publiceret knockoutkamp")
            def valid_int(value: object) -> bool:
                return isinstance(value, int) and not isinstance(value, bool) and value > 0
            if (not all(isinstance(raw.get(key), str) and raw[key] for key in ("match_id", "stage", "source_a", "source_b"))
                or not valid_int(raw.get("match_index"))
                or not isinstance(raw.get("round_numbers"), list)
                or not raw["round_numbers"]
                or not all(valid_int(v) for v in raw["round_numbers"])
                or not all(raw.get(key) is None or valid_int(raw[key]) for key in ("team_a_id", "team_b_id", "team_a_seed", "team_b_seed"))):
                raise PayloadError("Ugyldig publiceret knockoutkamp")
            knockout.append(PublishedKnockoutMatch(
                raw["match_id"], raw["stage"], raw["match_index"], tuple(raw["round_numbers"]),
                raw.get("team_a_id"), raw.get("team_b_id"), raw.get("team_a_seed"), raw.get("team_b_seed"),
                raw["source_a"], raw["source_b"],
            ))
        return TournamentPairingRevision(group_id, revision, tuple(pairings), tuple(knockout))

    def load_for_tournament(
        self,
        group_id: str,
        revision: int,
        config: TournamentConfig,
        team_ids: tuple[int, ...],
    ) -> TournamentPairingRevision:
        return validate_tournament_pairing_revision(
            self.load(group_id, revision),
            config,
            team_ids,
        )

    def save(self, value: TournamentPairingRevision) -> None:
        payload = {
            "schema_version": PAIRING_SCHEMA_VERSION,
            "group_id": value.group_id,
            "tournament_revision": value.tournament_revision,
            "pairings": [
                {
                    "round": item.round_number,
                    "team_a_id": item.team_a_id,
                    "team_b_id": item.team_b_id,
                }
                for item in value.pairings
            ],
            "knockout_matches": [
                {
                    "match_id": item.match_id, "stage": item.stage,
                    "match_index": item.match_index, "round_numbers": list(item.round_numbers),
                    "team_a_id": item.team_a_id, "team_b_id": item.team_b_id,
                    "team_a_seed": item.team_a_seed, "team_b_seed": item.team_b_seed,
                    "source_a": item.source_a, "source_b": item.source_b,
                }
                for item in value.knockout_matches
            ],
        }
        replace_text_atomically(
            self._path(value.group_id, value.tournament_revision),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def publish_round(
        self,
        group_id: str,
        revision: int,
        round_number: int,
        pairings: tuple[TournamentPairing, ...],
        *,
        previous_round_complete: bool = True,
    ) -> TournamentPairingRevision:
        if round_number < 1:
            raise PayloadError("Swiss-runden skal være positiv")
        if not pairings:
            raise PayloadError("En Swiss-runde skal indeholde mindst én parring")
        if round_number > 1 and not previous_round_complete:
            raise PayloadError(
                "Den forrige Swiss-runde skal være komplet før publicering"
            )
        if any(item.round_number != round_number for item in pairings):
            raise PayloadError("Publicerede parringer skal tilhøre den valgte runde")
        seen: set[int] = set()
        for item in pairings:
            participants = (item.team_a_id,) + (
                (item.team_b_id,) if item.team_b_id is not None else ()
            )
            if any(team_id in seen for team_id in participants):
                raise PayloadError("En deltager kan ikke spille to gange i samme runde")
            seen.update(participants)
        current = self.load(group_id, revision)
        existing = tuple(item for item in current.pairings if item.round_number == round_number)
        if existing:
            if existing != pairings:
                raise PayloadError("De publicerede parringer er i konflikt; opret en ny turneringsrevision")
            return current
        updated = TournamentPairingRevision(
            group_id,
            revision,
            (*current.pairings, *pairings),
            current.knockout_matches,
        )
        self.save(updated)
        return updated

    def publish_knockout_matches(
        self, group_id: str, revision: int,
        matches: tuple[PublishedKnockoutMatch, ...], *,
        config: TournamentConfig, team_ids: tuple[int, ...],
    ) -> TournamentPairingRevision:
        """Append concrete fixtures; existing identities can never be rewritten."""
        current = self.load_for_tournament(group_id, revision, config, team_ids)
        existing = {item.match_id: item for item in current.knockout_matches}
        additions: list[PublishedKnockoutMatch] = []
        for item in matches:
            if item.match_id in existing:
                if existing[item.match_id] != item:
                    raise PayloadError("Publiceret knockoutkamp er i konflikt; opret en ny turneringsrevision")
            else:
                existing[item.match_id] = item
                additions.append(item)
        updated = replace(current, knockout_matches=(*current.knockout_matches, *additions))
        validate_tournament_pairing_revision(updated, config, team_ids)
        if additions or not self.has_revision(group_id, revision):
            self.save(updated)
        return updated
