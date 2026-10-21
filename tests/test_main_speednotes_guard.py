import json
import sys

import pytest

import main


def test_cli_refuses_to_run_without_speednote_picker(tmp_path, monkeypatch, capsys):
    """The CLI has no SpeedNotes prompt yet; with SpeedNotes configured it must
    stop instead of submitting appointments with no topics ticked."""
    (tmp_path / "config.json").write_text(json.dumps({
        "speednote_groups": [{"name": "Housing", "topics": ["Room Selection"]}],
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main.paths, "APP_ROOT", str(tmp_path))
    monkeypatch.setenv("LAKER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["main.py", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1
    assert "dashboard" in capsys.readouterr().out
