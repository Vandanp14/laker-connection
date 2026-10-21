import paths


def test_data_dir_defaults_to_app_root(monkeypatch):
    monkeypatch.delenv("LAKER_DATA_DIR", raising=False)
    assert paths.data_dir() == paths.APP_ROOT


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LAKER_DATA_DIR", str(tmp_path))
    assert paths.data_file("residents.csv") == str(tmp_path / "residents.csv")


def test_empty_env_falls_back(monkeypatch):
    monkeypatch.setenv("LAKER_DATA_DIR", "")
    assert paths.data_dir() == paths.APP_ROOT


def test_failure_dir_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LAKER_DATA_DIR", str(tmp_path))
    assert paths.failure_dir() == str(tmp_path / "logs" / "failures")
