"""
Import residents / check-ins from a CSV the user filled in from a template.

Both imports are all-or-nothing: the merged result is checked first and any
problem rejects the whole file, citing the uploaded file's line numbers, so
nothing half-lands in residents.csv / checkins.csv. Accepts what Excel,
Numbers and Google Sheets export (BOM, CRLF, "Full Name" header style,
trailing empty columns/lines).
"""
import csv
import io
import re
from typing import Dict, List, Tuple

import data


class ImportError_(Exception):
    """Import rejected; .messages holds one plain-English line per problem."""

    def __init__(self, messages: List[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


def _normalize_header(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_")


def read_upload(raw: bytes, required: List[str]) -> List[Dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportError_(["The file is empty. Expected header: " + ",".join(required)])
    columns = [_normalize_header(h) for h in header]
    missing = [c for c in required if c not in columns]
    if missing:
        raise ImportError_([
            f"Missing column(s): {', '.join(missing)}. Expected header: {','.join(required)}"
        ])
    positions = {c: columns.index(c) for c in required}

    rows = []
    for values in reader:
        line = reader.line_num
        row = {c: (values[i].strip() if i < len(values) else "") for c, i in positions.items()}
        if not any(row.values()):
            continue
        row["_line"] = line
        rows.append(row)
    return rows


_DATE_MDY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})$")
_DATE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$")


def normalize_date(value: str) -> str:
    """Spreadsheet date styles (10/1/2026, 10/1/26, 10-1-2026, 2026-10-01)
    -> MM-DD-YYYY. Anything unrecognized is returned as-is for validate()
    to report."""
    value = (value or "").strip()
    m = _DATE_MDY.match(value)
    if m:
        month, day, year = m.groups()
        if len(year) == 2:
            year = "20" + year
        return f"{int(month):02d}-{int(day):02d}-{year}"
    m = _DATE_ISO.match(value)
    if m:
        year, month, day = m.groups()
        return f"{int(month):02d}-{int(day):02d}-{year}"
    return value


def normalize_time(value: str) -> str:
    """Spreadsheet time styles (14:00:00, 9:00, 2:00 PM) -> 24h HH:MM.
    Blank stays blank (auto-chained); unrecognized is returned as-is."""
    value = (value or "").strip()
    m = _TIME.match(value)
    if not m:
        return value
    hour, minute, ampm = int(m.group(1)), m.group(2), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            return value
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return f"{hour:02d}:{minute}"


def merge_residents(existing: Dict[str, str], rows: List[Dict]) -> Tuple[List[Tuple[str, str]], int]:
    errors = []
    existing_by_name = {data._name_key(name): eid for eid, name in existing.items()}
    seen_ids: Dict[str, int] = {}
    seen_names: Dict[str, int] = {}
    new_pairs = []
    skipped = 0

    for row in rows:
        line, name, eid = row["_line"], row["full_name"], row["email_or_id"]
        if not name or not eid:
            errors.append(f"Line {line}: needs both a full_name and an email_or_id")
            continue
        key = data._name_key(name)
        if eid in seen_ids:
            errors.append(f"Line {line}: {eid} is also on line {seen_ids[eid]}")
            continue
        if key in seen_names:
            errors.append(f'Line {line}: "{name}" is also on line {seen_names[key]}')
            continue
        seen_ids[eid], seen_names[key] = line, line

        if eid in existing:
            if data._name_key(existing[eid]) == key:
                skipped += 1
            else:
                errors.append(f'Line {line}: {eid} is already on your roster as "{existing[eid]}", '
                              f'not "{name}"')
            continue
        if key in existing_by_name:
            errors.append(f'Line {line}: "{name}" is already on your roster with '
                          f'{existing_by_name[key]}, not {eid}')
            continue
        new_pairs.append((name, eid))

    if errors:
        raise ImportError_(errors)
    return new_pairs, skipped


def _identity(row: Dict) -> Tuple[str, str, str]:
    return (data._name_key(row["full_name"]), row["date"].strip(), row["start_time"].strip())


def merge_checkins(existing_raw: List[Dict], rows: List[Dict], residents: Dict[str, str],
                   submission_log: set, config: dict) -> Tuple[List[Dict], int]:
    existing_ids = {_identity(dict(r, date=normalize_date(r["date"]),
                                   start_time=normalize_time(r["start_time"])))
                    for r in existing_raw}
    new_rows = []
    skipped = 0
    for row in rows:
        row = dict(row, date=normalize_date(row["date"]), start_time=normalize_time(row["start_time"]))
        if _identity(row) in existing_ids:
            skipped += 1
            continue
        new_rows.append(row)

    merged = [dict(r, _original_index=i) for i, r in enumerate(existing_raw)]
    labels = {i + 1: f"Row {i + 1} of your saved check-ins" for i in range(len(merged))}
    for row in new_rows:
        idx = len(merged)
        merged.append({**{c: row[c] for c in data.CHECKINS_FIELDNAMES}, "_original_index": idx})
        labels[idx + 1] = f"Line {row['_line']}"

    resolved, resolution_errors = data.resolve_checkins(merged, residents, submission_log, config)
    validation_errors = data.validate(resolved, residents, config, submission_log)
    errors = resolution_errors + validation_errors
    if errors:
        relabel = lambda m: labels.get(int(m.group(1)), m.group(0))
        raise ImportError_([re.sub(r"\bRow (\d+)\b", relabel, e) for e in errors])
    return [{c: row[c] for c in data.CHECKINS_FIELDNAMES} for row in new_rows], skipped
