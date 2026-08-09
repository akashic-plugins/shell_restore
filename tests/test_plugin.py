from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin import ShellRestore


def _plugin(data_dir: Path) -> ShellRestore:
    plugin = ShellRestore()
    plugin.context = SimpleNamespace(data_dir=data_dir)
    return plugin


def test_rewrite_simple_rm(tmp_path: Path) -> None:
    rewritten = _plugin(tmp_path)._rewrite_command("rm /tmp/a.txt")
    assert rewritten is not None
    assert rewritten.startswith("mv -- /tmp/a.txt ")


def test_rewrite_sudo_rm_keeps_prefix(tmp_path: Path) -> None:
    rewritten = _plugin(tmp_path)._rewrite_command("sudo rm -rf /tmp/a.txt")
    assert rewritten is not None
    assert rewritten.startswith("sudo mv -- /tmp/a.txt ")


def test_rewrite_multiple_targets(tmp_path: Path) -> None:
    rewritten = _plugin(tmp_path)._rewrite_command("rm -rf /tmp/a /tmp/b /tmp/c")
    assert rewritten is not None
    assert rewritten.startswith("mv -- /tmp/a /tmp/b /tmp/c ")


def test_rewrite_non_rm_returns_none() -> None:
    assert ShellRestore()._rewrite_command("echo hi") is None


def test_rewrite_complex_command_returns_none(tmp_path: Path) -> None:
    assert _plugin(tmp_path)._rewrite_command("rm /tmp/a && echo done") is None
    assert _plugin(tmp_path)._rewrite_command("rm /tmp/a || true") is None
    assert _plugin(tmp_path)._rewrite_command("rm /tmp/a | wc -l") is None
    assert _plugin(tmp_path)._rewrite_command("echo x; rm /tmp/a") is None
    assert _plugin(tmp_path)._rewrite_command("rm /tmp/a > /dev/null") is None


def test_restore_dir_uses_plugin_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AKASIC_RESTORE_DIR", raising=False)
    assert _plugin(tmp_path)._restore_dir() == str(tmp_path / "restore")


def test_explicit_restore_dir_has_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("AKASIC_RESTORE_DIR", str(explicit))
    assert _plugin(tmp_path)._restore_dir() == str(explicit)


def test_blank_restore_override_uses_plugin_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AKASIC_RESTORE_DIR", "   ")
    assert _plugin(tmp_path)._restore_dir() == str(tmp_path / "restore")
