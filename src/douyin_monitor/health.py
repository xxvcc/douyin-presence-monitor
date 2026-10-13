"""Read-only process-heartbeat probe; not a promise of upstream availability."""
from __future__ import annotations

import math
import os
import time
from pathlib import Path


def healthy(snapshot: dict, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    runtime = snapshot.get("runtime", {})
    if not isinstance(runtime, dict) or runtime.get("status") != "running":
        return False
    heartbeat = runtime.get("heartbeat_at")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)):
        return False
    if not math.isfinite(heartbeat):
        return False
    if runtime.get("stopped_at"):
        return False
    return -60 <= now - heartbeat <= 90


def main() -> int:
    try:
        from .storage import status_snapshot
        from .paths import installation_root
        state_dir = Path(os.environ.get("DPM_STATE_DIR", str(installation_root() / "data")))
        result = healthy(status_snapshot(state_dir / "monitor.sqlite3"))
    except Exception:  # noqa: BLE001 -- health probes fail closed without exposing errors
        # Never echo exception text from paths, SQLite or network-adjacent objects.
        result = False
    print("healthy" if result else "unhealthy")
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
