import os
import subprocess
import sys
from pathlib import Path

import waypoint_server
from conftest import ROOT, TEST_ENV


def test_config_lives_in_user_config_dir_not_package_dir():
    assert waypoint_server.ENV_PATH == Path(os.environ["XDG_CONFIG_HOME"]) / "waypoint" / ".env"
    assert waypoint_server.ENV_PATH.parent != Path(waypoint_server.__file__).resolve().parent


def test_save_env_writes_private_file_and_keeps_unrelated_lines(monkeypatch):
    env_path = waypoint_server.ENV_PATH
    env_path.write_text("OTHER_SETTING=keep-me\nJIRA_API_TOKEN=old\n")
    monkeypatch.setitem(waypoint_server._config, "site_url", "https://x.atlassian.net")
    monkeypatch.setitem(waypoint_server._config, "email", "a@b.c")
    monkeypatch.setitem(waypoint_server._config, "api_token", "new-token")
    waypoint_server._save_env()

    lines = env_path.read_text().splitlines()
    assert "OTHER_SETTING=keep-me" in lines
    assert "JIRA_API_TOKEN=new-token" in lines and "JIRA_API_TOKEN=old" not in lines
    if os.name != "nt":
        assert env_path.stat().st_mode & 0o777 == 0o600


def test_legacy_env_is_migrated_on_first_run(tmp_path):
    """Run the server module from a copy with a legacy .env beside it and an empty config dir."""
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "waypoint_server.py").write_text((ROOT / "waypoint_server.py").read_text())
    (package_dir / ".env").write_text("JIRA_SITE_URL=https://legacy.atlassian.net\n")
    config_home = tmp_path / "config"

    env = {**os.environ, **TEST_ENV, "XDG_CONFIG_HOME": str(config_home), "APPDATA": str(config_home)}
    env.pop("JIRA_SITE_URL")  # let the file supply it
    probe = "import waypoint_server as w; print(w._config['site_url'])"
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=package_dir, env=env, capture_output=True, text=True, timeout=60
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "https://legacy.atlassian.net"
    migrated = config_home / "waypoint" / ".env"
    assert migrated.read_text() == "JIRA_SITE_URL=https://legacy.atlassian.net\n"
    if os.name != "nt":
        assert migrated.stat().st_mode & 0o777 == 0o600
        assert migrated.parent.stat().st_mode & 0o777 == 0o700


def test_environment_variables_win_over_config_file(tmp_path):
    config_home = tmp_path / "config"
    (config_home / "waypoint").mkdir(parents=True)
    (config_home / "waypoint" / ".env").write_text("JIRA_SITE_URL=https://from-file.atlassian.net\n")
    env = {
        **os.environ, **TEST_ENV, "XDG_CONFIG_HOME": str(config_home), "APPDATA": str(config_home),
        "JIRA_SITE_URL": "https://from-env.atlassian.net",
    }
    probe = "import waypoint_server as w; print(w._config['site_url'])"
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "https://from-env.atlassian.net"
