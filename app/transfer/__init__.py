"""Transfer manager — the single shared bandwidth budget every byte-mover
draws from (architecture-v2.md §5.2, CLAUDE.md invariant 5).

Public surface is re-exported here; the modules underneath are:
  clock.py     injectable time (real CV waits in production, deterministic
               time-travel under test)
  schedule.py  rate strings and cron-style windows, pure functions
  bucket.py    the token bucket: a pure decision core plus a threaded wrapper
  manager.py   the up/down pair, per-endpoint etiquette, EWMA, live rates
  pool.py      the acquisition worker pool

SCOPE BOUNDARY, stated once so a literal reading of invariant 5 does not
mislead: app/embeddings.py and app/llm.py move bytes over HTTP without
drawing from this budget. They are LAN calls to a model runtime (§5.2 is about
the WAN link that downloading, delta sync and seeding compete for), and
throttling them would throttle interactive query latency against hardware that
is not the scarce resource. Every WAN byte-mover — the acquisition transports —
does draw from the budget, via app/acquire/budget.py.
"""
from .bucket import (BucketCore, Priority, RateLease, TokenBucket,
                      TransferAborted)
from .clock import Clock, FakeClock, RealClock
from .manager import (Direction, TransferHandle, TransferManager,
                       current_handle, get_manager, set_manager)
from .schedule import Schedule, ScheduleError, parse_rate, parse_schedule

__all__ = [
    "BucketCore", "Priority", "RateLease", "TokenBucket", "TransferAborted",
    "Clock", "FakeClock", "RealClock",
    "Direction", "TransferHandle", "TransferManager",
    "current_handle", "get_manager", "set_manager",
    "Schedule", "ScheduleError", "parse_rate", "parse_schedule",
]
