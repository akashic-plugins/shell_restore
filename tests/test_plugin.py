from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import cast

import pytest

import plugin as shell_restore
from agent.plugin_composition.bindings import Bindings
from agent.plugin_composition.messages import OWNER_STATE
from agent.plugin_composition.tasks import TASKS
from agent.plugins.composable import ComposablePlugin
from agent.plugins.snapshot import lease_runtime_snapshot
from plugins.content.plugin import check_text
from plugins.tools.abandon import abandon_call
from plugins.tools.api import MessageReply, result_message_id
from plugins.tools.plugin import TOOLS
from plugins.standard_tools.plugin import STANDARD_TOOLS
from session.message import CallRef, Control, Output, ToolCall, ToolResult
from tests.test_standard_tools import environment


def test_v3_namespace_is_loadable() -> None:
    loaded = ComposablePlugin.from_module(shell_restore)
    assert loaded.name == "shell_restore"
    assert loaded.version == "3.0.0"
    assert loaded.inject == (TOOLS, STANDARD_TOOLS)


def test_rewrite_simple_rm(tmp_path: Path) -> None:
    assert shell_restore._rewrite_command("rm /tmp/a.txt", tmp_path) == f"mv -- /tmp/a.txt {tmp_path}"


def test_rewrite_sudo_rm_keeps_prefix_and_options(tmp_path: Path) -> None:
    assert shell_restore._rewrite_command("sudo rm -rf /tmp/a.txt", tmp_path) == f"sudo mv -- /tmp/a.txt {tmp_path}"
    assert shell_restore._rewrite_command("sudo -n rm -rf /tmp/a.txt", tmp_path) == f"sudo -n mv -- /tmp/a.txt {tmp_path}"
    assert shell_restore._rewrite_command("sudo -nE rm /tmp/a.txt", tmp_path) == f"sudo -nE mv -- /tmp/a.txt {tmp_path}"
    assert shell_restore._rewrite_command(
        "sudo -n --preserve-env=HOME rm /tmp/a.txt", tmp_path,
    ) == f"sudo -n --preserve-env=HOME mv -- /tmp/a.txt {tmp_path}"
    assert shell_restore._rewrite_command(
        "sudo -u root -n rm /tmp/a.txt", tmp_path,
    ) == f"sudo -u root -n mv -- /tmp/a.txt {tmp_path}"
    assert shell_restore._rewrite_command(
        "sudo --user root -n rm /tmp/a.txt", tmp_path,
    ) == f"sudo --user root -n mv -- /tmp/a.txt {tmp_path}"
    assert shell_restore._rewrite_command(
        "sudo -nuroot rm /tmp/a.txt", tmp_path,
    ) == f"sudo -nuroot mv -- /tmp/a.txt {tmp_path}"


@pytest.mark.parametrize("mode_flag", ["-e", "-l", "-s", "-i", "-v", "-h"])
def test_sudo_mode_flags_are_not_treated_as_command_prefix(mode_flag: str, tmp_path: Path) -> None:
    assert shell_restore._rewrite_command(f"sudo -n {mode_flag} rm /tmp/a.txt", tmp_path) is None


def test_rewrite_multiple_targets_and_double_dash(tmp_path: Path) -> None:
    assert shell_restore._rewrite_command(
        "rm -rf -- '/tmp/a one' /tmp/b", tmp_path,
    ) == f"mv -- '/tmp/a one' /tmp/b {tmp_path}"


@pytest.mark.parametrize("command", [
    "echo hi", "rm /tmp/a && echo done", "rm /tmp/a || true", "rm /tmp/a | wc -l",
    "echo x; rm /tmp/a", "rm /tmp/a > /dev/null", "rm '",
])
def test_non_rm_or_complex_command_is_unchanged(command: str, tmp_path: Path) -> None:
    assert shell_restore._rewrite_command(command, tmp_path) is None


def test_restore_dir_uses_plugin_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AKASIC_RESTORE_DIR", raising=False)
    assert shell_restore._restore_dir(tmp_path) == tmp_path / "restore"
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("AKASIC_RESTORE_DIR", str(explicit))
    assert shell_restore._restore_dir(tmp_path) == explicit
    monkeypatch.setenv("AKASIC_RESTORE_DIR", "   ")
    assert shell_restore._restore_dir(tmp_path) == tmp_path / "restore"


@pytest.mark.asyncio
async def test_real_tools_execution_moves_file_and_receipt_does_not_repeat(tmp_path: Path) -> None:
    host, store, log, _artifacts, sources = environment(tmp_path)
    shutil.copytree(
        Path(__file__).parents[1], sources / "shell_restore",
        ignore=shutil.ignore_patterns(".git", ".akashic-core", ".plugin-contracts", ".venv", "node_modules", ".pytest_cache", "__pycache__", "tests"),
    )
    source = tmp_path / "valuable file.txt"
    source.write_text("keep me", encoding="utf-8")
    authorized = []

    async def allow(_binding: str, arguments: object):
        authorized.append(arguments)
        return {"allowed": True}

    try:
        await host.load_all()
        bindings = Bindings(log, host._archive, host.open_binding)
        async with lease_runtime_snapshot(host.snapshot_store) as snapshot:
            catalog = snapshot.composition_root.context.require(TOOLS)
            binding = catalog.bind(snapshot.composition_root.context.require(STANDARD_TOOLS).select("shell"), bindings, configuration={
                "working_dir": str(tmp_path), "allow_network": False,
            })
            assert bindings.describe(binding, TOOLS)["prepare"] == "restore"
            execution = catalog.execution(allow)
            arguments = {
                "command": f"rm -- {json.dumps(str(source))}",
                "description": "verify recoverable removal",
                "login": False,
            }
            result = await execution.execute("remove", binding, arguments)
            repeated = await execution.execute("remove", binding, arguments)

        assert result.outcome == "success", result
        assert repeated == result
        assert not source.exists()
        restore_dirs = list((tmp_path / "workspace" / "plugin-data").glob("shell_restore-*/restore"))
        assert len(restore_dirs) == 1
        restored = restore_dirs[0] / source.name
        assert restored.read_text(encoding="utf-8") == "keep me"
        assert json.loads(cast(str, result.parts[0].value))["process_status"] == "succeeded"
        assert len(authorized) == 1
        assert cast(dict[str, object], authorized[0])["command"].startswith("mv -- ")
    finally:
        await host.terminate_all()
        log.close()
        store.close()


@pytest.mark.asyncio
async def test_abandon_before_start_does_not_move_file(tmp_path: Path) -> None:
    host, store, log, _artifacts, sources = environment(tmp_path)
    shutil.copytree(
        Path(__file__).parents[1], sources / "shell_restore",
        ignore=shutil.ignore_patterns(".git", ".akashic-core", ".plugin-contracts", ".venv", "node_modules", ".pytest_cache", "__pycache__", "tests"),
    )
    source = tmp_path / "keep.txt"
    source.write_text("still here", encoding="utf-8")
    try:
        await host.load_all()
        bindings = Bindings(log, host._archive, host.open_binding)
        async with lease_runtime_snapshot(host.snapshot_store) as snapshot:
            catalog = snapshot.composition_root.context.require(TOOLS)
            binding = catalog.bind(snapshot.composition_root.context.require(STANDARD_TOOLS).select("shell"), bindings, configuration={"working_dir": str(tmp_path)})
            output = log.writer(
                "abandon", author="assistant", source="conversation", body_types=(Output,),
                content={}, check_call=lambda call: None,
            )
            output.append("remove-call", Output((ToolCall(binding, {
                "command": f"rm {source}", "description": "abandon fixture", "login": False,
            }),), "continue"))
            ref = CallRef("remove-call", 0)
            writer = log.writer(
                "abandon", author="tool", source="conversation", body_types=(ToolResult,),
                content={"text": check_text}, call_ref=ref,
            )
            reply = MessageReply(result_message_id(ref), ref, log.reader("abandon"), writer, lambda: None)
            control = log.writer(
                "abandon", author="user", source="conversation", body_types=(Control,), content={},
            )
            control.append("abandon-control", Control("abandon", reply.reader.head(source="conversation")))
            owner = catalog._ctx.require(OWNER_STATE).open(catalog._ctx)
            tasks = catalog._ctx.require(TASKS).open(catalog._ctx)
            result = await abandon_call(owner, tasks, reply, task_key="effects")

        assert result.outcome == "denied"
        assert source.read_text(encoding="utf-8") == "still here"
        assert not list((tmp_path / "workspace" / "plugin-data").glob("shell_restore-*/restore"))
    finally:
        await host.terminate_all()
        log.close()
        store.close()
