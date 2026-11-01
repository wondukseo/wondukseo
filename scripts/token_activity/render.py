"""Render account activity as a count-free SVG calendar using the standard library."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape
import re


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LABELS = {
    "unknown": "No data",
    "none": "No activity",
    "low": "Low activity",
    "moderate": "Moderate activity",
    "high": "High activity",
    "peak": "Very high activity",
}
_WEEKS = 53
_LEFT = 60
_TOP = 80
_PITCH = 14
_CELL = 11


def _validated_snapshot(snapshot: dict) -> tuple[datetime, dict[date, int]]:
    if not isinstance(snapshot, dict) or snapshot.get("source") != "codex-account-usage":
        raise ValueError("Expected a Codex account usage snapshot.")
    timestamp = snapshot.get("fetched_at")
    if not isinstance(timestamp, str):
        raise ValueError("The snapshot needs a UTC fetched_at timestamp.")
    try:
        fetched = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid fetched_at timestamp.") from error
    if fetched.tzinfo is None or fetched.utcoffset() != timedelta(0):
        raise ValueError("The fetched_at timestamp must be UTC.")

    rows = snapshot.get("daily")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Daily account usage is missing; no chart was generated.")
    daily = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each daily entry must contain a date and token value.")
        raw_date = row.get("date")
        if not isinstance(raw_date, str) or not _DATE.fullmatch(raw_date):
            raise ValueError("Daily dates must use YYYY-MM-DD.")
        try:
            day = date.fromisoformat(raw_date)
        except ValueError as error:
            raise ValueError("Invalid daily date.") from error
        value = row.get("tokens")
        if type(value) is not int or value < 0:
            raise ValueError("Daily token values must be nonnegative integers.")
        if day in daily:
            raise ValueError("Duplicate daily dates are not supported.")
        daily[day] = value
    return fetched, daily


def _activity_level(value: int | None, peak: int) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "none"
    if value * 4 <= peak:
        return "low"
    if value * 2 <= peak:
        return "moderate"
    if value * 4 <= peak * 3:
        return "high"
    return "peak"


def _month_labels(start: date, today: date) -> list[tuple[int, str]]:
    labels = [(0, _MONTHS[start.month - 1])]
    for offset in range(1, (today - start).days + 1):
        day = start + timedelta(days=offset)
        if day.day != 1:
            continue
        column = offset // 7
        if column - labels[-1][0] < 3:
            labels.pop()
        labels.append((column, _MONTHS[day.month - 1]))
    return labels


def render_svg(snapshot: dict, today: date = None) -> str:
    """Return a Sunday-first activity card containing dates and colors, never counts.

    Missing dates remain explicitly unknown. Only a reported zero is shown as
    no activity. The current week is partial: future dates have no cells.
    """
    fetched, daily = _validated_snapshot(snapshot)
    if today is None:
        today = datetime.now(timezone.utc).date()
    if type(today) is not date:
        raise ValueError("today must be a date.")
    try:
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        start = sunday - timedelta(weeks=_WEEKS - 1)
    except OverflowError as error:
        raise ValueError("today is outside the supported calendar range.") from error
    visible = {day: value for day, value in daily.items() if start <= day <= today}
    if not visible:
        raise ValueError("No daily account usage falls within the displayed calendar.")
    peak = max(visible.values(), default=0)

    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="830" height="218" viewBox="0 0 830 218" role="img" aria-labelledby="activity-title activity-description">',
        '<title id="activity-title">ChatGPT Token Activity</title>',
        '<desc id="activity-description">Account activity via Codex. Color intensity represents activity. Outlined squares mean no data; dark filled squares mean no activity. Future dates are blank.</desc>',
        '<style>',
        'text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }',
        '.heading { fill: #e8f1f5; font-size: 17px; font-weight: 650; letter-spacing: -.25px; }',
        '.subtitle { fill: #93a9bc; font-size: 11px; }',
        '.label { fill: #91a5b7; font-size: 10px; }',
        '.cell { stroke-width: .7; }',
        '.unknown { fill: none; stroke: #3a4b5e; stroke-dasharray: 2 1.5; }',
        '.none { fill: #1b2a3b; stroke: #283c51; }',
        '.low { fill: #175567; stroke: #226577; }',
        '.moderate { fill: #147d86; stroke: #23909a; }',
        '.high { fill: #26ad9c; stroke: #40beac; }',
        '.peak { fill: #72dfb3; stroke: #90ebc6; }',
        '</style>',
        '<rect x=".5" y=".5" width="829" height="217" rx="15" fill="#0d1725" stroke="#263749"/>',
        '<rect x="24" y="22" width="3" height="28" rx="1.5" fill="#49c8ae"/>',
        '<text x="37" y="33" class="heading">ChatGPT Token Activity</text>',
        '<text x="37" y="50" class="subtitle">Account activity via Codex</text>',
    ]
    for column, month in _month_labels(start, today):
        lines.append(f'<text x="{_LEFT + column * _PITCH}" y="70" class="label">{month}</text>')
    for row, weekday in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        lines.append(f'<text x="24" y="{_TOP + row * _PITCH + 9}" class="label">{weekday}</text>')

    lines.append('<g aria-label="Daily activity calendar">')
    for offset in range((today - start).days + 1):
        day = start + timedelta(days=offset)
        level = _activity_level(visible.get(day), peak)
        label = escape(f"{day.isoformat()}: {_LABELS[level]}", quote=True)
        x = _LEFT + (offset // 7) * _PITCH
        y = _TOP + (offset % 7) * _PITCH
        lines.append(f'<rect x="{x}" y="{y}" width="{_CELL}" height="{_CELL}" rx="2" class="cell {level}" aria-label="{label}"><title>{label}</title></rect>')
    lines.append('</g>')

    updated = f"Updated {_MONTHS[fetched.month - 1]} {fetched.day:02d}, {fetched.year} UTC"
    lines.extend([
        f'<text x="24" y="202" class="label">{updated}</text>',
        '<rect x="244" y="193" width="11" height="11" rx="2" class="cell unknown"/>',
        '<text x="262" y="202" class="label">No data</text>',
        '<text x="634" y="202" class="label">Less</text>',
    ])
    for index, level in enumerate(("none", "low", "moderate", "high", "peak")):
        lines.append(f'<rect x="{668 + index * _PITCH}" y="193" width="11" height="11" rx="2" class="cell {level}" aria-label="{_LABELS[level]}"/>')
    lines.extend(['<text x="744" y="202" class="label">More</text>', '</svg>'])
    return "\n".join(lines) + "\n"
