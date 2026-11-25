"""Start an isolated test process with a pipe and bounded cleanup."""

from contextlib import contextmanager
from multiprocessing import get_context
import sys
from unittest.mock import patch


@contextmanager
def spawned_process(target, *args):
    context = get_context("spawn")
    parent, peer = context.Pipe()
    with parent, peer:
        child = context.Process(target=target, args=(*args, peer))
        try:
            # AppTest installs a generated script as __main__. The child's
            # target is importable without replaying that script.
            with patch.object(sys.modules["__main__"], "__file__", None, create=True):
                child.start()
            peer.close()
            yield child, parent
        finally:
            if child.pid is not None:
                if child.is_alive():
                    child.kill()
                child.join(5)
            child.close()
