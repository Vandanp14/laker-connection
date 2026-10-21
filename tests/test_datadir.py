import pytest

import datadir


def test_prepare_creates_empty_files(tmp_path):
    datadir.prepare(str(tmp_path))
    assert (tmp_path / "residents.csv").read_text() == "full_name,email_or_id\n"
    assert (tmp_path / "checkins.csv").read_text() == "full_name,date,start_time,location,notes\n"
    assert (tmp_path / "submission_log.csv").read_text() == \
        "email_or_id,date,start_time,reason,submitted_at\n"
    assert (tmp_path / "VERSION").read_text().strip() == "1"
    assert (tmp_path / "logs" / "failures").is_dir()


def test_prepare_leaves_existing_files_untouched(tmp_path):
    original = b"full_name,email_or_id\r\nJane Doe,jdoe@oswego.edu\r\n"
    (tmp_path / "residents.csv").write_bytes(original)
    datadir.prepare(str(tmp_path))
    assert (tmp_path / "residents.csv").read_bytes() == original


def test_prepare_backs_up_then_migrates(tmp_path, monkeypatch):
    datadir.prepare(str(tmp_path))
    (tmp_path / "VERSION").write_text("0\n")
    before = (tmp_path / "residents.csv").read_bytes()

    def add_marker(path):
        with open(f"{path}/residents.csv", "a") as f:
            f.write("MIGRATED\n")

    monkeypatch.setattr(datadir, "DATA_VERSION", 1)
    monkeypatch.setattr(datadir, "MIGRATIONS", {0: add_marker})
    notes = datadir.prepare(str(tmp_path))

    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "residents.csv").read_bytes() == before
    assert (tmp_path / "residents.csv").read_text().endswith("MIGRATED\n")
    assert (tmp_path / "VERSION").read_text().strip() == "1"
    assert any("Backed up" in n for n in notes)


def test_prepare_refuses_newer_version(tmp_path):
    (tmp_path / "VERSION").write_text("99\n")
    with pytest.raises(RuntimeError, match="newer version"):
        datadir.prepare(str(tmp_path))
