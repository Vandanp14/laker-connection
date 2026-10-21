"""
Set up the user's data directory and upgrade it between app versions.

prepare() runs at startup. It creates any missing data file (header only),
and keeps a VERSION marker so a future change to a file's format (e.g. a
term column in submission_log.csv for Spring) can upgrade existing data —
always after copying the whole directory to backups/<timestamp>/ first.
"""
import csv
import os
import shutil
from datetime import datetime
from typing import Callable, Dict, List

import data

DATA_VERSION = 1

HEADERS = {
    "residents.csv": ["full_name", "email_or_id"],
    "checkins.csv": list(data.CHECKINS_FIELDNAMES),
    "submission_log.csv": ["email_or_id", "date", "start_time", "reason", "submitted_at"],
}

# from-version -> function upgrading the data dir from that version to the next.
MIGRATIONS: Dict[int, Callable[[str], None]] = {}

NEWER_VERSION_MESSAGE = (
    "Your data was saved by a newer version of Laker Connection. "
    "Re-run the installer to update."
)


def _read_version(data_dir: str):
    try:
        with open(os.path.join(data_dir, "VERSION")) as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return None


def _write_version(data_dir: str, version: int) -> None:
    with open(os.path.join(data_dir, "VERSION"), "w") as f:
        f.write(f"{version}\n")


def _backup(data_dir: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(data_dir, "backups", stamp)
    os.makedirs(dest)
    for name in os.listdir(data_dir):
        if name == "backups":
            continue
        src = os.path.join(data_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, name))
        else:
            shutil.copy2(src, os.path.join(dest, name))
    return dest


def prepare(data_dir: str) -> List[str]:
    notes = []
    os.makedirs(os.path.join(data_dir, "logs", "failures"), exist_ok=True)

    version = _read_version(data_dir)
    if version is not None and version > DATA_VERSION:
        raise RuntimeError(NEWER_VERSION_MESSAGE)

    for name, header in HEADERS.items():
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f, lineterminator="\n").writerow(header)
            notes.append(f"Created {name}")

    if version is None:
        _write_version(data_dir, DATA_VERSION)
    elif version < DATA_VERSION:
        dest = _backup(data_dir)
        notes.append(f"Backed up data to {os.path.relpath(dest, data_dir)}")
        for v in range(version, DATA_VERSION):
            MIGRATIONS[v](data_dir)
        _write_version(data_dir, DATA_VERSION)
        notes.append(f"Upgraded data from version {version} to {DATA_VERSION}")
    return notes
