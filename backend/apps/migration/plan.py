"""Pure migration plan constants.

No Django and no Temporal imports on purpose. The Temporal workflow runs inside a
deterministic sandbox and the inline runner does not, and this module is the one piece
of the plan both of them can import without breaking either. Keeping the chunk sizes
here means a change to the batch strategy cannot be applied in one execution path and
forgotten in the other.
"""

from __future__ import annotations

from typing import Any

# Journal pages pulled per staging activity. Smaller means a crash costs less work and
# a resume re-reads less from the customer's ERP; larger means fewer round trips.
STAGE_PAGES_PER_ACTIVITY = 5

# Staged rows normalised, and entries upserted, per activity.
BATCH_SIZE = 500

# Ceiling on chunks per phase. A loop that cannot terminate is a bug, and the migration
# should fail loudly rather than spin against a customer's production system.
MAX_CHUNKS = 500

DEFAULT_ACTIVITY_TIMEOUT_SECONDS = 120
DEFAULT_HEARTBEAT_SECONDS = 10


def build_config(
    *,
    activity_timeout_seconds: int = DEFAULT_ACTIVITY_TIMEOUT_SECONDS,
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
    batch_size: int = BATCH_SIZE,
    pages_per_stage_activity: int = STAGE_PAGES_PER_ACTIVITY,
    max_chunks: int = MAX_CHUNKS,
    **extra: Any,
) -> dict[str, Any]:
    """Assemble the plain dict handed to the workflow.

    Passed in as an argument rather than read from settings inside the workflow, so the
    workflow body stays free of ambient global state and a replay is guaranteed to see
    the same values it saw the first time.
    """
    return {
        "activity_timeout_seconds": int(activity_timeout_seconds),
        "heartbeat_seconds": int(heartbeat_seconds),
        "batch_size": int(batch_size),
        "pages_per_stage_activity": int(pages_per_stage_activity),
        "max_chunks": int(max_chunks),
        **extra,
    }
