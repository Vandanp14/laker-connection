"""
Data layer for the Laker Connection Starfish automation.

CSV schemas
-----------
residents.csv       full_name,email_or_id                      (unchanged)
checkins.csv        full_name,date,start_time,location,notes
                      - date        : MM-DD-YYYY
                      - start_time  : 24-hour HH:MM, MAY BE BLANK (auto-chained,
                                      see resolve_checkins)
                      - no email_or_id column      -> resolved from full_name
                      - no duration_minutes column -> config slot_duration_minutes
submission_log.csv  email_or_id,date,start_time,reason,submitted_at
                      - reason      : the config.valid_reasons value used for that
                                      submission (e.g. "Laker Connection #1 > FALL"),
                                      added 2026-09-01 so per-resident progress can
                                      show which of the 3 conversations are done, not
                                      just a raw count. May be blank on rows written
                                      before this column existed.

REQUIRED CALL ORDER (main.py today, dashboard.py later)
-------------------------------------------------------
    config         = data.load_config('config.json')
    residents      = data.load_residents('residents.csv')
    raw_checkins   = data.load_checkins('checkins.csv')
    submission_log = data.load_submission_log('submission_log.csv')

    checkins, resolution_errors = data.resolve_checkins(
        raw_checkins, residents, submission_log, config)
    if resolution_errors:
        # ABORT. Rows that failed to resolve are NOT present in `checkins`.
        ...

    errors = data.validate(checkins, residents, config)
    if errors:
        # ABORT.
        ...

    candidates = data.compute_candidates(checkins, submission_log)

resolve_checkins() MUST run before validate(), compute_candidates(),
selection.py and browser.submit_one(). All of those consume the fully-resolved
row shape:

    email_or_id, full_name, date, start_time, duration_minutes, location, notes

and none of them tolerate a blank start_time or a missing email_or_id.
Callers must treat a non-empty `resolution_errors` list as fatal: resolution
drops unresolvable rows, so downstream row indices only line up with the input
file when that list is empty.

All CSV reads use encoding='utf-8-sig' so a UTF-8 BOM (Excel / Google Sheets
exports) never glues itself onto the first header name.
"""
import csv
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

# Column order of checkins.csv. Also the header any writer (dashboard) must use.
CHECKINS_FIELDNAMES = ['full_name', 'date', 'start_time', 'location', 'notes']

DEFAULT_SLOT_DURATION_MINUTES = 15


def load_residents(path: str) -> Dict[str, str]:
    """Load residents.csv into a dict mapping email_or_id -> full_name."""
    residents = {}
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            email_or_id = (row.get('email_or_id') or '').strip()
            if email_or_id:
                residents[email_or_id] = (row.get('full_name') or '').strip()
    return residents


def load_checkins(path: str) -> List[Dict]:
    """
    Load checkins.csv (schema: full_name,date,start_time,location,notes).

    Returns raw, UNRESOLVED rows:
        {'full_name', 'date', 'start_time', 'location', 'notes', '_original_index'}

    `start_time` may be an empty string here — resolve_checkins() fills it in.
    There is no `email_or_id` and no `duration_minutes` at this stage; both are
    added by resolve_checkins().
    """
    checkins = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in CHECKINS_FIELDNAMES if c not in fieldnames]
        if missing:
            raise ValueError(
                f"{path}: missing required column(s) {missing}. "
                f"Expected header: {','.join(CHECKINS_FIELDNAMES)}"
            )
        for row in reader:
            values = {c: (row.get(c) or '').strip() for c in CHECKINS_FIELDNAMES}
            if not any(values.values()):
                continue  # skip blank filler lines
            values['_original_index'] = len(checkins)
            checkins.append(values)
    return checkins


def update_checkin_note(
    path: str,
    original_index: int,
    notes: str,
    expected_identity: Optional[Tuple[str, str, str, str]] = None,
) -> None:
    """Update one check-in's note and replace the CSV atomically.

    ``expected_identity`` is the raw on-disk ``(full_name, date, start_time,
    location)`` tuple shown when the edit form was opened. It prevents a stale
    browser tab from changing the wrong row if somebody manually reorders or
    replaces ``checkins.csv`` before Save is clicked.
    """
    checkins = load_checkins(path)
    if original_index < 0 or original_index >= len(checkins):
        raise IndexError("Check-in no longer exists")

    row = checkins[original_index]
    current_identity = tuple(row[field] for field in CHECKINS_FIELDNAMES[:-1])
    if expected_identity is not None and current_identity != expected_identity:
        raise ValueError("Check-in changed since the edit form was opened")

    row['notes'] = (notes or '').strip()
    directory = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            newline='',
            encoding='utf-8',
            dir=directory,
            prefix='.checkins-',
            suffix='.csv',
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            writer = csv.DictWriter(temp_file, fieldnames=CHECKINS_FIELDNAMES)
            writer.writeheader()
            for checkin in checkins:
                writer.writerow({field: checkin[field] for field in CHECKINS_FIELDNAMES})
        os.chmod(temp_path, os.stat(path).st_mode)
        os.replace(temp_path, path)
    except Exception:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def load_submission_log(path: str) -> set:
    """
    Load submission_log.csv into a set of tuples (email_or_id, date, start_time).

    Deliberately drops `reason` — this is the identity key used by
    compute_candidates() and resolve_checkins()'s insertion-safety pinning,
    neither of which care which reason was used, only that a row was
    submitted. For the reason breakdown, see load_submission_log_details().
    """
    logged = set()
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row['email_or_id'].strip(), row['date'].strip(), row['start_time'].strip())
                logged.add(key)
    except FileNotFoundError:
        # It's okay if the file doesn't exist yet.
        pass
    return logged


def load_submission_log_details(path: str) -> List[Dict]:
    """
    Load submission_log.csv into full row dicts (email_or_id, date,
    start_time, reason, submitted_at), for compute_progress()'s per-reason
    breakdown. `reason` is '' for rows written before that column existed.
    """
    rows = []
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({
                    'email_or_id': (row.get('email_or_id') or '').strip(),
                    'date': (row.get('date') or '').strip(),
                    'start_time': (row.get('start_time') or '').strip(),
                    'reason': (row.get('reason') or '').strip(),
                    'submitted_at': (row.get('submitted_at') or '').strip(),
                })
    except FileNotFoundError:
        pass
    return rows


def append_submission_log(path: str, row: Dict) -> None:
    """
    Append a single successful submission to submission_log.csv. Shared by
    main.py and dashboard.py. `row['reason']` is set by browser.run_submission
    (the batch's run_reason, same value typed into Starfish's Reason field).
    """
    submitted_at = datetime.now().isoformat(timespec='seconds')
    with open(path, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([row['email_or_id'], row['date'], row['start_time'], row.get('reason', ''), submitted_at])


def load_config(path: str) -> dict:
    """Load config.json."""
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


LOCATION_SETTING_KEYS = ('valid_locations', 'default_location')


def load_effective_config(config_path: str, settings_path: str) -> dict:
    """
    Shipped config.json with the user's settings.json laid over it. Only the
    location keys can be overridden. A missing or unreadable settings.json is
    ignored (the shipped values apply), so a bad edit never locks anyone out.
    """
    config = load_config(config_path)
    try:
        with open(settings_path, encoding='utf-8-sig') as f:
            settings = json.load(f)
    except (FileNotFoundError, ValueError):
        return config
    if not isinstance(settings, dict):
        return config
    locations = settings.get('valid_locations')
    if isinstance(locations, list) and locations and all(isinstance(l, str) for l in locations):
        config['valid_locations'] = locations
        default = settings.get('default_location')
        config['default_location'] = default if default in locations else locations[0]
    return config


def save_location_settings(settings_path: str, locations: List[str], default_location: str) -> None:
    """Validate and atomically write the user's locations to settings.json."""
    cleaned = []
    for loc in locations:
        loc = (loc or '').strip()
        if loc and loc not in cleaned:
            cleaned.append(loc)
    if not cleaned:
        raise ValueError("Add at least one location.")
    default_location = (default_location or '').strip()
    if default_location not in cleaned:
        raise ValueError(f'The default location "{default_location}" must be one of your locations.')

    try:
        with open(settings_path, encoding='utf-8-sig') as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            settings = {}
    except (FileNotFoundError, ValueError):
        settings = {}
    settings['valid_locations'] = cleaned
    settings['default_location'] = default_location

    tmp_path = settings_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp_path, settings_path)


def speednote_topics(config: dict) -> List[str]:
    """
    Flattened, ordered SpeedNotes topics from config['speednote_groups'].

    These are the exact Starfish SpeedNotes checkbox labels (confirmed live
    2026-10-20, identical for all three Laker Connection reasons). Empty
    list when the key is absent — that means the feature is off. A topic
    listed in two groups is a config error.
    """
    topics: List[str] = []
    for group in config.get('speednote_groups') or []:
        for topic in group.get('topics') or []:
            if topic in topics:
                raise ValueError(f"SpeedNote '{topic}' appears more than once in speednote_groups")
            topics.append(topic)
    return topics


def normalize_speednotes(values, config: dict) -> Tuple[List[str], List[str]]:
    """
    Clean a list of submitted SpeedNotes topic names against config.

    Strips whitespace, drops blanks and duplicates (first occurrence wins,
    order preserved). Matching is exact and case-sensitive, because these
    are clicked by their exact Starfish label ("Academics - Kudos/Check-In"
    vs "Wellbeing - Kudos/Check-in" differ only in case). Returns
    (topics, errors); unknown names go to errors, never to topics.
    """
    valid = set(speednote_topics(config))
    topics: List[str] = []
    errors: List[str] = []
    for raw in values:
        value = (raw or '').strip()
        if not value or value in topics:
            continue
        if value not in valid:
            errors.append(f"SpeedNote '{value}' is not one of the configured topics")
            continue
        topics.append(value)
    return topics, errors


def parse_datetime(date_str: str, time_str: str) -> datetime:
    """Parse MM-DD-YYYY and HH:MM into a datetime."""
    # Assuming time is in 24-hour format HH:MM
    dt_str = f"{date_str} {time_str}"
    return datetime.strptime(dt_str, "%m-%d-%Y %H:%M")


def _slot_duration(config: dict) -> int:
    """Fixed per-check-in duration, in minutes, from config."""
    return int(config.get('slot_duration_minutes', DEFAULT_SLOT_DURATION_MINUTES))


def _name_key(name: str) -> str:
    """
    Normalized lookup key for a full_name: case-folded, whitespace-collapsed.
    Lets hand-typed 'jane  doe' match 'Jane Doe' while
    still treating two genuinely different people as different.
    """
    return re.sub(r'\s+', ' ', (name or '').strip()).casefold()


def _row_no(row: Dict, idx: int) -> int:
    """1-based checkins.csv data-row number for error messages."""
    return int(row.get('_original_index', idx)) + 1


def build_name_index(residents: Dict[str, str]) -> Tuple[Dict[str, str], set]:
    """
    Build the full_name -> email_or_id reverse map from residents.

    Returns (name_to_email, duplicate_name_keys). Names that appear more than
    once in residents.csv are AMBIGUOUS: they are excluded from name_to_email
    and reported in duplicate_name_keys (normalized keys, see _name_key).
    """
    name_to_email: Dict[str, str] = {}
    duplicates: set = set()
    for email_or_id, full_name in residents.items():
        key = _name_key(full_name)
        if not key:
            continue
        if key in name_to_email:
            duplicates.add(key)
        else:
            name_to_email[key] = email_or_id
    for key in duplicates:
        name_to_email.pop(key, None)
    return name_to_email, duplicates


def resolve_checkins(
    checkins: List[Dict],
    residents: Dict[str, str],
    submission_log: set,
    config: dict,
) -> Tuple[List[Dict], List[str]]:
    """
    Resolve raw checkins.csv rows into the fully-populated row shape the rest of
    the pipeline consumes.

    Args:
        checkins:       raw rows from load_checkins()
        residents:      email_or_id -> full_name, from load_residents()
        submission_log: set of (email_or_id, date, start_time), from
                        load_submission_log()
        config:         parsed config.json (uses 'slot_duration_minutes')

    Returns:
        (resolved_rows, errors)

        resolved_rows: rows in original file order, each carrying
            email_or_id, full_name (canonical spelling from residents.csv),
            date, start_time (never blank), duration_minutes, location, notes,
            _original_index
        errors: human-readable resolution errors. A row that produced an error
            is OMITTED from resolved_rows — callers must abort when this list
            is non-empty rather than proceeding with a partial batch.

    Algorithm:
      1. Reverse-map full_name -> email_or_id. A full_name appearing twice in
         residents.csv is a hard error (unresolvable).
      2. Rows are grouped by `date` (all rows sharing a date form one group,
         wherever they appear in the file), preserving file order inside the
         group. Each group is walked with a running "anchor" time:
         a. If (email_or_id, date) already has a submission_log entry, the row's
            start_time is PINNED to the logged value and becomes the anchor.
            This is the insertion-safety rule: an already-submitted check-in
            never shifts when a new row is inserted above it, so
            compute_candidates() cannot re-surface it and create a duplicate
            Starfish appointment.
         b. Else if the row has an explicit non-blank start_time, it is used
            verbatim and becomes the anchor.
         c. Else the row is chained: start_time = anchor + slot_duration_minutes
            (and becomes the new anchor). A blank start_time with no anchor yet
            for that date is an error.
      3. duration_minutes is set to config['slot_duration_minutes'] on every row.
      4. Safety net: if a resolved start_time disagrees with an existing
         submission_log entry for the same (email_or_id, date), that is a hard
         error — unreachable given step 2a, asserted anyway.
    """
    errors: List[str] = []
    slot = _slot_duration(config)
    if slot <= 0:
        errors.append(
            f"config.json: slot_duration_minutes must be a positive integer (got {slot})"
        )
        return [], errors

    name_to_email, duplicate_names = build_name_index(residents)
    for key in sorted(duplicate_names):
        names = sorted({n for n in residents.values() if _name_key(n) == key})
        errors.append(
            f"residents.csv: duplicate full_name {names} — cannot resolve "
            f"check-ins by name; make the names unique"
        )

    # (email_or_id, date) -> set of start_times already submitted
    logged_times: Dict[Tuple[str, str], set] = {}
    for eid, date_str, start_time in submission_log:
        logged_times.setdefault((eid, date_str), set()).add(start_time)

    # Group by date, preserving file order within each group.
    groups: Dict[str, List[Tuple[int, Dict]]] = {}
    for idx, row in enumerate(checkins):
        groups.setdefault(row.get('date', '').strip(), []).append((idx, row))

    resolved_by_index: Dict[int, Dict] = {}

    for date_str, group in groups.items():
        anchor: Optional[datetime] = None

        for idx, row in group:
            row_no = _row_no(row, idx)
            raw_name = (row.get('full_name') or '').strip()
            raw_start = (row.get('start_time') or '').strip()

            if not raw_name:
                errors.append(f"Row {row_no}: full_name is blank")
                continue
            if not date_str:
                errors.append(f"Row {row_no}: date is blank")
                continue

            key = _name_key(raw_name)
            if key in duplicate_names:
                errors.append(
                    f"Row {row_no}: full_name '{raw_name}' matches more than one "
                    f"resident in residents.csv — ambiguous"
                )
                continue
            email_or_id = name_to_email.get(key)
            if not email_or_id:
                errors.append(
                    f"Row {row_no}: full_name '{raw_name}' not found in residents.csv"
                )
                continue

            pinned = logged_times.get((email_or_id, date_str))
            start_time: Optional[str] = None

            if pinned:
                if len(pinned) > 1:
                    errors.append(
                        f"Row {row_no}: resident '{raw_name}' ({email_or_id}) has "
                        f"{len(pinned)} conflicting submission_log entries for {date_str} "
                        f"({sorted(pinned)}) — cannot pin a start_time"
                    )
                    continue
                # Insertion safety: keep the time already submitted to Starfish.
                start_time = next(iter(pinned))
            elif raw_start:
                start_time = raw_start
            else:
                if anchor is None:
                    errors.append(
                        f"Row {row_no}: start_time is blank and no earlier check-in on "
                        f"{date_str} to chain from — the first check-in of a date needs "
                        f"an explicit start_time (HH:MM, 24-hour)"
                    )
                    continue
                start_time = (anchor + timedelta(minutes=slot)).strftime("%H:%M")

            try:
                start_dt = parse_datetime(date_str, start_time)
            except ValueError as e:
                errors.append(
                    f"Row {row_no}: invalid date/time '{date_str} {start_time}' "
                    f"(expected MM-DD-YYYY and 24-hour HH:MM): {e}"
                )
                continue

            # Safety net for the pinning logic above.
            existing = logged_times.get((email_or_id, date_str))
            if existing and start_time not in existing:
                errors.append(
                    f"Row {row_no}: resolved start_time {start_time} for '{raw_name}' "
                    f"({email_or_id}) on {date_str} disagrees with submission_log "
                    f"{sorted(existing)} — refusing to guess"
                )
                continue

            anchor = start_dt
            resolved_by_index[idx] = {
                'email_or_id': email_or_id,
                # Canonical spelling from residents.csv — browser.py searches
                # Starfish with this string.
                'full_name': residents[email_or_id],
                'date': date_str,
                'start_time': start_time,
                'duration_minutes': slot,
                'location': (row.get('location') or '').strip(),
                'notes': (row.get('notes') or '').strip(),
                '_original_index': row.get('_original_index', idx),
            }

    resolved = [resolved_by_index[i] for i in sorted(resolved_by_index)]
    return resolved, errors


def validate(checkins: List[Dict], residents: Dict[str, str], config: dict,
             submission_log: Optional[set] = None) -> List[str]:
    """
    Validate RESOLVED checkin rows (the output of resolve_checkins()).
    Returns a list of error messages.

    Note on FR3: the resident-identity check is now full_name-based, because
    checkins.csv no longer carries email_or_id. resolve_checkins() is the
    primary enforcement point (an unresolvable name never reaches here); the
    check below is a defensive re-check so validate() stays meaningful when
    called on hand-built rows.

    The old FR4 "duration_minutes >= minimum_duration_minutes" check is gone:
    duration is now the fixed config constant slot_duration_minutes, not user
    input. Its replacement is a one-time sanity check on that constant.
    """
    errors = []
    slot = _slot_duration(config)
    if slot <= 0:
        errors.append(
            f"config.json: slot_duration_minutes must be a positive integer (got {slot})"
        )

    name_to_email, duplicate_names = build_name_index(residents)
    for key in sorted(duplicate_names):
        names = sorted({n for n in residents.values() if _name_key(n) == key})
        errors.append(f"residents.csv: duplicate full_name {names} — cannot resolve by name")

    # FR3: every row's resident must be identifiable in residents.csv (by name).
    for idx, row in enumerate(checkins):
        row_no = _row_no(row, idx)
        name = (row.get('full_name') or '').strip()
        if not name:
            errors.append(f"Row {row_no}: full_name is blank")
            continue
        key = _name_key(name)
        if key in duplicate_names:
            errors.append(
                f"Row {row_no}: full_name '{name}' matches more than one resident "
                f"in residents.csv — ambiguous"
            )
            continue
        expected_eid = name_to_email.get(key)
        if not expected_eid:
            errors.append(f"Row {row_no}: full_name '{name}' not found in residents.csv")
            continue
        eid = (row.get('email_or_id') or '').strip()
        if not eid:
            errors.append(
                f"Row {row_no}: email_or_id is missing — rows must be run through "
                f"resolve_checkins() before validate()"
            )
        elif eid != expected_eid:
            errors.append(
                f"Row {row_no}: email_or_id '{eid}' does not match residents.csv entry "
                f"'{expected_eid}' for full_name '{name}'"
            )

    # FR5: location must be one of the configured valid values. Rows already
    # submitted are history — the user may since have renamed or removed that
    # location in Settings, which must not invalidate the whole file.
    valid_locations = config.get('valid_locations')
    if valid_locations:
        submitted = submission_log or set()
        for idx, row in enumerate(checkins):
            key = (row.get('email_or_id'), row['date'], row['start_time'])
            if key in submitted:
                continue
            if row['location'] not in valid_locations:
                errors.append(
                    f"Row {_row_no(row, idx)}: location '{row['location']}' is not one "
                    f"of {valid_locations}"
                )

    # FR6: No overlapping windows on same date (actually any overlap across whole file)
    # We'll convert each checkin to a datetime window and check for overlaps.
    windows = []  # list of (start_dt, end_dt, row_no)
    for idx, row in enumerate(checkins):
        row_no = _row_no(row, idx)
        try:
            start_dt = parse_datetime(row['date'], row['start_time'])
            end_dt = start_dt + timedelta(minutes=int(row.get('duration_minutes') or slot))
            windows.append((start_dt, end_dt, row_no))
        except ValueError as e:
            errors.append(f"Row {row_no}: invalid date/time format: {e}")

    # Sort by start time
    windows.sort(key=lambda x: x[0])
    # Check adjacent windows for overlap
    for i in range(len(windows) - 1):
        curr_start, curr_end, curr_no = windows[i]
        next_start, next_end, next_no = windows[i + 1]
        if curr_end > next_start:  # overlap if current end > next start
            errors.append(
                f"Overlap detected: Row {curr_no} ({curr_start} to {curr_end}) "
                f"overlaps with Row {next_no} ({next_start} to {next_end})"
            )

    # Cadence: only one check-in per resident per calendar month
    by_resident: Dict[str, List[int]] = {}
    for idx, row in enumerate(checkins):
        by_resident.setdefault(row.get('email_or_id', ''), []).append(idx)

    for eid, idxs in by_resident.items():
        month_seen: Dict[Tuple[int, int], int] = {}
        for idx in idxs:
            row = checkins[idx]
            try:
                dt = parse_datetime(row['date'], row['start_time'])
            except ValueError:
                continue  # already reported above
            key = (dt.year, dt.month)
            if key in month_seen:
                other_idx = month_seen[key]
                errors.append(
                    f"Row {_row_no(checkins[other_idx], other_idx)} and "
                    f"Row {_row_no(row, idx)}: resident '{eid}' has two check-ins in "
                    f"the same month ({dt.strftime('%m-%Y')}) — only one check-in per resident "
                    f"per month is allowed"
                )
            else:
                month_seen[key] = idx

    return errors


def compute_candidates(checkins: List[Dict], submission_log: set) -> List[Dict]:
    """
    Return list of checkins rows not yet in submission_log.
    Expects RESOLVED rows (see resolve_checkins).
    """
    candidates = []
    for row in checkins:
        key = (row['email_or_id'], row['date'], row['start_time'])
        if key not in submission_log:
            candidates.append(row)
    return candidates


def compute_progress(
    residents: Dict[str, str],
    submission_log_details: List[Dict],
    config: dict,
    target_per_resident: int = 3,
) -> List[Dict]:
    """
    Per-resident progress toward the semester target, using submission_log
    (actually-submitted appointments) as the source of truth.

    Returns one dict per resident: email_or_id, full_name, submitted,
    target, done_this_month, reasons_done (the subset of config.valid_reasons
    already submitted at least once for this resident, in valid_reasons
    order — lets the UI show a "Laker Connection #1 done" tile per reason
    instead of just a raw count), and unassigned_submissions. Rows with a
    blank `reason` (written before that column existed) count toward
    `submitted` and are surfaced explicitly rather than lighting a guessed
    reason tile.
    """
    valid_reasons = config.get('valid_reasons') or []
    counts: Dict[str, int] = {}
    unassigned_counts: Dict[str, int] = {}
    months_done: Dict[str, set] = {}
    reasons_done: Dict[str, set] = {}
    for entry in submission_log_details:
        eid = entry['email_or_id']
        counts[eid] = counts.get(eid, 0) + 1
        reason = entry.get('reason', '')
        if reason:
            reasons_done.setdefault(eid, set()).add(reason)
        else:
            unassigned_counts[eid] = unassigned_counts.get(eid, 0) + 1
        try:
            dt = datetime.strptime(entry['date'], "%m-%d-%Y")
        except ValueError:
            continue
        months_done.setdefault(eid, set()).add((dt.year, dt.month))

    current_month = (datetime.now().year, datetime.now().month)
    progress = []
    for eid, name in residents.items():
        done_for_eid = reasons_done.get(eid, set())
        progress.append({
            "email_or_id": eid,
            "full_name": name,
            "submitted": counts.get(eid, 0),
            "target": target_per_resident,
            "done_this_month": current_month in months_done.get(eid, set()),
            "reasons_done": [r for r in valid_reasons if r in done_for_eid],
            "unassigned_submissions": unassigned_counts.get(eid, 0),
        })
    return progress
