from __future__ import annotations

from pathlib import Path

from rushcut import host_launch
from rushcut import windows_paths


def test_my_documents_non_windows_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_paths.sys, "platform", "linux")
    monkeypatch.delenv("VEGAS_DOCUMENTS", raising=False)
    monkeypatch.setattr(windows_paths.Path, "home", classmethod(lambda cls: tmp_path))
    assert windows_paths.my_documents() == tmp_path / "Documents"


def test_my_documents_env_override(monkeypatch, tmp_path):
    redirected = tmp_path / "OneDrive" / "Documents"
    monkeypatch.setenv("VEGAS_DOCUMENTS", str(redirected))
    assert windows_paths.my_documents() == redirected


def test_my_documents_uses_known_folder_not_userprofile(monkeypatch, tmp_path):
    onedrive = tmp_path / "OneDrive" / "Documents"
    naive = tmp_path / "Users" / "someone" / "Documents"
    monkeypatch.setattr(windows_paths.sys, "platform", "win32")
    monkeypatch.delenv("VEGAS_DOCUMENTS", raising=False)
    monkeypatch.setattr(windows_paths, "_sh_get_folder_path", lambda csidl: onedrive)
    monkeypatch.setattr(windows_paths.Path, "home", classmethod(lambda cls: naive.parent))
    assert windows_paths.my_documents() == onedrive
    assert windows_paths.vegas_application_extensions_dir() == onedrive / "Vegas Application Extensions"
    assert windows_paths.vegas_script_menu_dir() == onedrive / "Vegas Script Menu"
    assert "OneDrive" in str(windows_paths.vegas_application_extensions_dir())
    assert naive / "Vegas Application Extensions" != windows_paths.vegas_application_extensions_dir()


def test_extension_candidates_prefer_known_documents(monkeypatch, tmp_path):
    onedrive = tmp_path / "OneDrive" / "Documents"
    monkeypatch.setattr(
        host_launch, "vegas_application_extensions_dir", lambda: onedrive / "Vegas Application Extensions"
    )
    monkeypatch.setattr(host_launch.Path, "home", classmethod(lambda cls: tmp_path / "Users" / "someone"))
    cands = host_launch.extension_dll_candidates()
    assert cands[0] == onedrive / "Vegas Application Extensions" / "VegasDirector.dll"
    naive = tmp_path / "Users" / "someone" / "Documents" / "Vegas Application Extensions" / "VegasDirector.dll"
    assert naive in cands
    assert cands.index(cands[0]) < cands.index(naive)


def test_build_ps1_uses_mydocuments():
    src = Path(__file__).resolve().parents[2] / "host" / "build.ps1"
    text = src.read_text(encoding="utf-8")
    assert "GetFolderPath" in text
    assert "MyDocuments" in text
    assert "$env:USERPROFILE 'Documents\\Vegas Application Extensions'" not in text
    assert "$env:USERPROFILE 'Documents\\Vegas Script Menu" not in text


def test_host_wraps_extension_writes_in_undoblock():
    src = Path(__file__).resolve().parents[2] / "host" / "VegasDirectorHost.cs"
    text = src.read_text(encoding="utf-8")
    assert "MethodNeedsUndoBlock" in text
    assert 'new UndoBlock("Vegas Director: " + req.Method)' in text
    assert "undo.Cancel" in text
    assert "E_UNEXPECTED" in text
    # Cap removed: limit<=0 is unlimited, and the inner-only break is gone.
    assert 'Json.GetInt(req.ParamsJson, "limit", 0)' in text
    assert "truncated" in text
    assert 'Json.GetInt(req.ParamsJson, "offset", 0)' in text
