import os
import re
import sys
import threading
import types
from pathlib import Path

import yaml


def test_profile_runtime_env_includes_terminal_config_and_dotenv(tmp_path):
    from api.profiles import get_profile_runtime_env

    home = tmp_path / "profiles" / "server-ops"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "terminal": {
                    "backend": "ssh",
                    "cwd": "/home/dso2ng/repos",
                    "timeout": 180,
                    "ssh_host": "pollux",
                    "ssh_user": "dso2ng",
                    "persistent_shell": True,
                    "lifetime_seconds": 300,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "TERMINAL_TIMEOUT=60\n"
        "TERMINAL_SSH_HOST=pollux-from-env\n"
        "HERMES_MAX_ITERATIONS=90\n",
        encoding="utf-8",
    )

    env = get_profile_runtime_env(home)

    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_CWD"] == "/home/dso2ng/repos"
    assert env["TERMINAL_SSH_USER"] == "dso2ng"
    assert env["TERMINAL_PERSISTENT_SHELL"] == "true"
    assert env["TERMINAL_LIFETIME_SECONDS"] == "300"
    # .env remains the final override source, matching CLI/profile behaviour.
    assert env["TERMINAL_TIMEOUT"] == "60"
    assert env["TERMINAL_SSH_HOST"] == "pollux-from-env"
    assert env["HERMES_MAX_ITERATIONS"] == "90"


def test_streaming_applies_profile_runtime_env_to_agent_run():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "get_profile_runtime_env" in src
    assert "_profile_runtime_env" in src
    assert "old_profile_env" in src
    assert "os.environ.update(_profile_runtime_env)" in src


def test_streaming_thread_env_allows_profile_terminal_cwd_override():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "def _build_agent_thread_env" in src
    assert "_thread_env = _build_agent_thread_env(" in src
    assert "_set_thread_env(**_thread_env)" in src
    assert "_set_thread_env(\n            **_profile_runtime_env,\n            TERMINAL_CWD" not in src

    match = re.search(
        r"(def _build_agent_thread_env\(.*?\n)(?=\ndef |\nclass )",
        src,
        re.DOTALL,
    )
    assert match, "_build_agent_thread_env not found in api/streaming.py"
    ns: dict = {}
    exec(compile(match.group(1), "<streaming_extract>", "exec"), ns)

    env = ns["_build_agent_thread_env"](
        {
            "TERMINAL_CWD": "/profile/config/cwd",
            "HERMES_EXEC_ASK": "0",
            "HERMES_SESSION_KEY": "old-session",
            "HERMES_SESSION_ID": "old-session",
            "HERMES_SESSION_PLATFORM": "cli",
            "HERMES_HOME": "/old/profile/home",
            "TERMINAL_ENV": "ssh",
        },
        "/active/workspace",
        "active-session",
        "/active/profile/home",
    )

    assert env["TERMINAL_CWD"] == "/active/workspace"
    assert env["HERMES_EXEC_ASK"] == "1"
    assert env["HERMES_SESSION_KEY"] == "active-session"
    assert env["HERMES_SESSION_ID"] == "active-session"
    assert env["HERMES_SESSION_PLATFORM"] == "webui"
    assert env["HERMES_HOME"] == "/active/profile/home"
    assert env["TERMINAL_ENV"] == "ssh"


def test_background_worker_profile_env_uses_thread_local_without_process_env(monkeypatch, tmp_path):
    from api import config as config_api
    from api import profiles

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"terminal": {"backend": "ssh", "cwd": "/profile/cwd"}}),
        encoding="utf-8",
    )
    (home / ".env").write_text("HERMES_MAX_ITERATIONS=42\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda profile: home)
    monkeypatch.setattr(profiles, "snapshot_skill_home_modules", lambda: {"snapshot": True})
    patched_homes = []
    restored = []
    monkeypatch.setattr(profiles, "patch_skill_home_modules", lambda path: patched_homes.append(path))
    monkeypatch.setattr(profiles, "restore_skill_home_modules", lambda snapshot: restored.append(snapshot))
    monkeypatch.setitem(sys.modules, "api.streaming", types.SimpleNamespace(_ENV_LOCK=threading.Lock()))

    monkeypatch.setenv("HERMES_HOME", "/root/default")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
    config_api._clear_thread_env()

    with profiles.profile_env_for_background_worker("ops"):
        assert os.environ["HERMES_HOME"] == "/root/default"
        assert "TERMINAL_ENV" not in os.environ
        assert "TERMINAL_CWD" not in os.environ
        assert "HERMES_MAX_ITERATIONS" not in os.environ
        assert config_api._thread_ctx.env["HERMES_HOME"] == str(home)
        assert config_api._thread_ctx.env["TERMINAL_ENV"] == "ssh"
        assert config_api._thread_ctx.env["TERMINAL_CWD"] == "/profile/cwd"
        assert config_api._thread_ctx.env["HERMES_MAX_ITERATIONS"] == "42"
        assert patched_homes == [home]

    assert os.environ["HERMES_HOME"] == "/root/default"
    assert getattr(config_api._thread_ctx, "env", {}) == {}
    assert restored == [{"snapshot": True}]


def test_background_worker_profile_env_source_avoids_os_environ_mutation():
    src = Path("api/profiles.py").read_text(encoding="utf-8")
    match = re.search(
        r"(@contextmanager\ndef profile_env_for_background_worker\(.*?\n)(?=\ndef |\nclass )",
        src,
        re.DOTALL,
    )
    assert match, "profile_env_for_background_worker not found"
    body = match.group(1)

    assert "from api.config import _set_thread_env, _clear_thread_env" in body
    assert "_set_thread_env(**thread_env)" in body
    assert "_clear_thread_env()" in body
    assert "os.environ.update(runtime_env)" not in body
    assert 'os.environ["HERMES_HOME"] = str(profile_home_path)' not in body
