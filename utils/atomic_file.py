"""Crash- and concurrency-safe file replacement."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` so readers only ever see the old or the new content.

    Each call writes its own uniquely named temp file in the target directory (so
    concurrent writers never share or truncate one another's temp file), fsyncs it,
    then atomically renames it over the target. The temp file is removed on failure.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
