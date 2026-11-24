"""Check archived-game permissions when a user action runs."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from holdet_lib.errors import PayloadError
from holdet_lib.groups import GroupStore, ManagerGame
from holdet_lib.models import GameUrl
from holdet_lib.paths import AppPaths
from holdet_lib.persistence import exclusive_configuration_update
from holdet_lib.refresh_runs import data_operation_running


def is_game_archived(games: Iterable[ManagerGame], game: GameUrl) -> bool:
    identity = (game.locale.casefold(), game.slug)
    return any(item.identity == identity and item.is_archived for item in games)


def require_active_game(paths: AppPaths, game: GameUrl) -> None:
    store = GroupStore(paths.groups_file, paths.group_revision_dir)
    configuration, _ = store.load_configuration_with_warnings()
    if is_game_archived(configuration.games, game):
        raise PayloadError("Managerspillet er arkiveret. Gendan det for at ændre data.")


@contextmanager
def editable_game(paths: AppPaths, game: GameUrl, *, require_idle: bool = False) -> Iterator[None]:
    """Check and apply a short configuration edit under the same lock."""
    with exclusive_configuration_update(paths.config_dir):
        require_active_game(paths, game)
        if require_idle and data_operation_running(paths.data_dir):
            raise PayloadError("Ændringer kan gemmes, når opdateringen er afsluttet")
        yield
