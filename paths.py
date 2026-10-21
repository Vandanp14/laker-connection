"""
Where the app's code and the user's data live.

Code (config.json, templates, static) always sits next to this file. Data
(residents.csv, checkins.csv, submission_log.csv, settings.json, logs/) goes
to LAKER_DATA_DIR when it's set — the installer points it at
~/LakerConnection/data so updates can replace the code without touching
anyone's data. Unset, data stays next to the code, as it always has.
"""
import os

APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    override = os.environ.get("LAKER_DATA_DIR", "").strip()
    if override:
        return os.path.expanduser(override)
    return APP_ROOT


def data_file(name: str) -> str:
    return os.path.join(data_dir(), name)


def config_path() -> str:
    return os.path.join(APP_ROOT, "config.json")


def failure_dir() -> str:
    return data_file(os.path.join("logs", "failures"))
