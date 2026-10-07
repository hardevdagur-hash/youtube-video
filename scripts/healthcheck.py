"""Container health probe: exit 0 only if GET /api/health returns 200 (stdlib only)."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("HEALTHCHECK_URL", "http://127.0.0.1:8000/api/health")


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=4) as response:  # noqa: S310 (fixed local URL)
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
