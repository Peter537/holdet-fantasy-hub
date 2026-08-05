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
    validate_tournament_config,
)


PAIRING_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TournamentPairingRevision:
    group_id: str
    tournament_revision: int
    pairings: tuple[TournamentPairing, ...] = ()

    @property
    def published_rounds(self) -> tuple[int, ...]:
        return tuple(sorted({item.round_number for item in self.pairings}))



def validate_tournament_pairing_revision(
    value: TournamentPairingRevision,
    config: TournamentConfig,
    team_ids: tuple[int, ...],
) -> TournamentPairingRevision:
    """Validate persisted pairings in the context of their Swiss revision."""

    if config.template != "swiss":
        raise PayloadError("Publicerede Swiss-parringer kræver en Swiss-turnering")
    if value.tournament_revision < 1 or not value.group_id.strip():
        raise PayloadError("Turneringsparringerne har en ugyldig revision")
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
            or payload.get("schema_version") != PAIRING_SCHEMA_VERSION
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
        return TournamentPairingRevision(group_id, revision, tuple(pairings))

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
        )
        self.save(updated)
        return updated
