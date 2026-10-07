"""Spreadsheet formula-injection protection for CSV exports.

Video titles, channel names and transcripts are attacker-controllable (anyone can
upload a video titled ``=HYPERLINK(...)``). Excel, LibreOffice and Google Sheets
evaluate a cell as a formula when it starts with ``=``, ``+``, ``-`` or ``@``, and
some also act on a leading tab or carriage return. Such cells are prefixed with a
single quote, the OWASP-recommended neutraliser, which spreadsheets render as plain
text. All other values (including numbers) are written unchanged.
"""

from __future__ import annotations

from typing import Any

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def safe_csv_cell(value: Any) -> Any:
    """Return ``value`` with a leading quote if a spreadsheet would treat it as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGERS):
        return "'" + value
    return value


def safe_csv_row(row: list[Any]) -> list[Any]:
    return [safe_csv_cell(v) for v in row]
