from __future__ import annotations

from plugin import ShellRestore


def test_rewrite_simple_rm() -> None:
    rewritten = ShellRestore()._rewrite_command("rm /tmp/a.txt")
    assert rewritten is not None
    assert rewritten.startswith("mv -- /tmp/a.txt ")


def test_rewrite_sudo_rm_keeps_prefix() -> None:
    rewritten = ShellRestore()._rewrite_command("sudo rm -rf /tmp/a.txt")
    assert rewritten is not None
    assert rewritten.startswith("sudo mv -- /tmp/a.txt ")


def test_rewrite_non_rm_returns_none() -> None:
    assert ShellRestore()._rewrite_command("echo hi") is None
