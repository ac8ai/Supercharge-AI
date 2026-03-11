"""Signal handlers for graceful shutdown of SuperchargeAI processes.

Registers SIGTERM and SIGINT handlers that raise SystemExit, allowing
async finally blocks and context managers to run cleanup before exit.

Does NOT use signal.SIG_IGN for SIGCHLD — that interferes with
subprocess.run() which expects to wait() on children. Zombie prevention
for detached children is handled by daemon threads (see memory.py).
"""

from __future__ import annotations

import signal
import sys


def _graceful_exit(signum: int, frame: object) -> None:  # noqa: ARG001
    """Signal handler that raises SystemExit to trigger cleanup."""
    raise SystemExit(128 + signum)


def setup_signal_handlers() -> None:
    """Register SIGTERM and SIGINT handlers for graceful shutdown.

    Safe to call multiple times — idempotent. Only effective in the
    main thread (signal.signal raises ValueError otherwise).
    """
    try:
        signal.signal(signal.SIGTERM, _graceful_exit)
        signal.signal(signal.SIGINT, _graceful_exit)
    except (ValueError, OSError):
        # Not in main thread or signal handling unavailable
        print(
            "[SuperchargeAI] Could not register signal handlers (not main thread?)",
            file=sys.stderr,
        )
