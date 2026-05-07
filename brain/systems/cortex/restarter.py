"""
Dashboard Restarter — restart the illo-dashboard systemd service.

SAFE: The dashboard and worker are separate processes. Restarting the dashboard
does NOT affect running agents — they continue in the cortex-worker process.

Usage as module:
    from brain.systems.cortex.restarter import restart_dashboard
    restart_dashboard()

Usage as CLI:
    python3 -m services.dashboard_restarter
"""
from __future__ import annotations
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def restart_dashboard() -> bool:
    """Restart the illo-dashboard systemd service.

    Safe to call at any time — running agents live in the separate
    cortex-worker process and are not affected.

    Returns:
        True if restart succeeded, False otherwise.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "illo-dashboard"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("illo-dashboard restarted successfully")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to restart illo-dashboard: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.error("Restart timed out after 30s")
        return False


def main():
    success = restart_dashboard()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
