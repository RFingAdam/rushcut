from __future__ import annotations

import json

from rushcut.host_client import VegasHostClient, VegasHostConfig, _load_host_token
from rushcut.host_launch import find_vegas_exe, launch_vegas
from rushcut.oneshot import write_oneshot_job
from rushcut.session import Session


def test_token_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("VEGAS_HOST_TOKEN", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    token_dir = tmp_path / "vegas-director-mcp"
    token_dir.mkdir()
    (token_dir / "token").write_text("a" * 32, encoding="utf-8")
    assert _load_host_token() == "a" * 32
    monkeypatch.setenv("VEGAS_HOST_TOKEN", "override-token")
    assert _load_host_token() == "override-token"


def test_client_includes_token(monkeypatch):
    monkeypatch.setenv("VEGAS_HOST_TOKEN", "secret-token")
    sent = {}

    def fake_send(self, line: str) -> str:
        sent.update(json.loads(line))
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    monkeypatch.setattr(VegasHostClient, "_send", fake_send)
    client = VegasHostClient(VegasHostConfig(transport="tcp"))
    assert client.call("ping")["ok"] is True
    assert sent["token"] == "secret-token"
    assert sent["method"] == "ping"


def test_launch_vegas_soft_fails_off_windows(monkeypatch):
    # Force the non-Windows path so this never starts a real VEGAS on a dev box.
    monkeypatch.setattr("rushcut.host_launch.sys.platform", "linux")
    result = launch_vegas()
    assert result["ok"] is False
    assert "Windows" in result["error"]
    # Discovery helpers must not throw.
    assert find_vegas_exe() is None or isinstance(find_vegas_exe(), str)


def test_oneshot_job_written(tmp_path):
    written = write_oneshot_job(
        {
            "open": r"D:\proj\raid.veg",
            "ops": [{"op": "add_track", "type": "video", "name": "V1"}],
            "save": r"D:\proj\raid.veg",
            "exit": True,
        },
        dest_dir=str(tmp_path),
    )
    assert written["ok"] is True
    cs = (tmp_path / "job.cs").read_text(encoding="utf-8")
    job = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))
    assert "class EntryPoint" in cs
    assert "FromVegas" in cs
    assert job["ops"][0]["op"] == "add_track"


def test_session_reports_live(monkeypatch):
    class C:
        config = VegasHostConfig()

        def call(self, method, params=None):
            return {"ok": True, "host": "vegas-director", "mode": "script"}

    session = Session(client=C())
    monkeypatch.setattr(session, "host_reachable", lambda: True)
    info = session.ensure()
    assert info["used_mode"] == "live"
    assert info["ok"] is True
