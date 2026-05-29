"""Optional rsync push: copy data/ to a remote host after each scrape.

Set EKZ_RSYNC_TARGET=user@host:/remote/path in .env to enable.
Requires rsync + SSH to be available in the container and an SSH key
mounted at /root/.ssh/id_ed25519 (see README for setup).
"""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def rsync_data(data_dir: str, target: str) -> None:
    """rsync <data_dir>/ to <target> (trailing slash = contents, not dir itself).

    Skips silently if target is empty. Logs a warning on failure but never
    raises — a sync failure must not prevent the next scheduled scrape.
    """
    if not target:
        return

    src = str(Path(data_dir)) + "/"  # trailing slash: sync contents

    cmd = [
        "rsync",
        "--archive",
        "--compress",
        "--delete",
        "--checksum",
        "--quiet",
        "-e", "ssh -o StrictHostKeyChecking=no -o BatchMode=yes",
        src,
        target,
    ]

    logger.info("rsync: %s -> %s", src, target)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("rsync completed successfully")
        else:
            logger.warning(
                "rsync exited with code %d: %s",
                result.returncode,
                (result.stderr or result.stdout).strip(),
            )
    except FileNotFoundError:
        logger.warning("rsync not found — install it or set EKZ_RSYNC_TARGET=''")
    except subprocess.TimeoutExpired:
        logger.warning("rsync timed out after 120s")
    except Exception as e:
        logger.warning("rsync failed: %s", e)
