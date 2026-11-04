"""Keep a local record of order attempts across crashes and concurrent runs."""

import os
from contextlib import contextmanager
from datetime import datetime, timezone

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def lock_order_guard(directory, project_id):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{project_id}.state").open("a+b") as state:
        state.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(state.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(state, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BlockingIOError("该活动的订单记录已被另一个实例锁定") from error
        yield state


def has_order_attempt(state):
    state.seek(0)
    return bool(state.read(1))


def record_order_attempt(state):
    if not has_order_attempt(state):
        timestamp = datetime.now(timezone.utc).isoformat()
        state.write(timestamp.encode("ascii"))
        state.flush()
        os.fsync(state.fileno())


def clear_order_attempt(state):
    state.truncate(0)
    state.flush()
    os.fsync(state.fileno())
