"""Wait for persisted absolute start times without depending on browser timers."""
from datetime import datetime
import time


def wait_for_start(scheduled_at, cancelled, stopping):
    if not scheduled_at:
        return not cancelled.is_set() and not stopping()
    deadline = datetime.fromisoformat(scheduled_at).timestamp()
    while not cancelled.is_set() and not stopping():
        remaining = deadline - time.time()
        if remaining <= 0:
            return True
        cancelled.wait(min(remaining, 1.0))
    return False
