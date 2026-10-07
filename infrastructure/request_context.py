"""Per-request / per-job logging context.

The request middleware sets ``request_id`` (honouring a well-formed incoming
X-Request-ID from nginx), the auth middleware sets ``user``, and each background
job task sets ``job_id``. ``ContextFilter`` copies them onto every log record, so a
single request or job can be followed through all log lines, including code running
in worker threads (``asyncio.to_thread`` copies the context).
"""

from __future__ import annotations

import contextvars
import logging
import re
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")
user_var: contextvars.ContextVar[str] = contextvars.ContextVar("user", default="")

_INCOMING_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def new_request_id(incoming: str | None = None) -> str:
    """Use a well-formed upstream id (e.g. nginx $request_id), otherwise generate one."""
    if incoming and _INCOMING_ID_RE.match(incoming):
        return incoming
    return uuid.uuid4().hex[:8]


def current_request_id() -> str:
    """The active request id; outside a request a fresh id is generated (and not stored)."""
    return request_id_var.get() or uuid.uuid4().hex[:8]


class ContextFilter(logging.Filter):
    """Adds ``request_id``, ``job_id`` and ``user`` attributes to every record ("-" if unset)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.job_id = job_id_var.get() or "-"
        record.user = user_var.get() or "-"
        return True
