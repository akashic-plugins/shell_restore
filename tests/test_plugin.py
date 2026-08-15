from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import plugin as shell_restore
from agent.plugin_composition import CompositionRoot, PluginRuntime
from agent.plugins.composable import ComposablePlugin
from agent.plugins.manager import PluginManager
from agent.plugins.snapshot import (
    RuntimeSnapshotCompiler,
    RuntimeSnapshotStore,
    bind_runtime_snapshot,
    reset_runtime_snapshot,
)
from agent.tool_hooks.executor import ToolExecutor
from agent.tool_hooks.types import ToolExecutionRequest
from agent.tools.shell import ShellTool
from agent.tools.unified_exec import ShellProcessManager
from bus.event_bus import EventBus


@asynccontextmanager
async def _bound_root(root: CompositionRoot) -> AsyncIterator[None]:
    store = RuntimeSnapshotStore()
    store.install(RuntimeSnapshotCompiler().compile({}, composition_root=root))
    lease = store.lease()
    token = bind_runtime_snapshot(lease)
    try:
        yield
    finally:
        reset_runtime_snapshot(token)
        await lease.release()
        await store.close()


def test_v3_namespace_is_loadable() -> None:
    loaded = ComposablePlugin.from_module(shell_restore)

    assert loaded.name == "shell_restore"
    assert loaded.version == "2.0.0"
    assert loaded.inject == ()


def test_rewrite_simple_rm(tmp_path: Path) -> None:
    rewritten = shell_restore._rewrite_command("rm /tmp/a.txt", tmp_path)
    assert rewritten == f"mv -- /tmp/a.txt {tmp_path}"


def test_rewrite_sudo_rm_keeps_prefix(tmp_path: Path) -> None:
    rewritten = shell_restore._rewrite_command("sudo rm -rf /tmp/a.txt", tmp_path)
    assert rewritten == f"sudo mv -- /tmp/a.txt {tmp_path}"


def test_rewrite_multiple_targets(tmp_path: Path) -> None:
    rewritten = shell_restore._rewrite_command(
        "rm -rf /tmp/a /tmp/b /tmp/c",
        tmp_path,
    )
    assert rewritten == f"mv -- /tmp/a /tmp/b /tmp/c {tmp_path}"


def test_rewrite_non_rm_returns_none(tmp_path: Path) -> None:
    assert shell_restore._rewrite_command("echo hi", tmp_path) is None


def test_rewrite_complex_command_returns_none(tmp_path: Path) -> None:
    assert shell_restore._rewrite_command("rm /tmp/a && echo done", tmp_path) is None
    assert shell_restore._rewrite_command("rm /tmp/a || true", tmp_path) is None
    assert shell_restore._rewrite_command("rm /tmp/a | wc -l", tmp_path) is None
    assert shell_restore._rewrite_command("echo x; rm /tmp/a", tmp_path) is None
    assert shell_restore._rewrite_command("rm /tmp/a > /dev/null", tmp_path) is None


def test_restore_dir_uses_plugin_data_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AKASIC_RESTORE_DIR", raising=False)
    assert shell_restore._restore_dir(tmp_path) == tmp_path / "restore"


def test_explicit_restore_dir_has_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("AKASIC_RESTORE_DIR", str(explicit))
    assert shell_restore._restore_dir(tmp_path) == explicit


def test_blank_restore_override_uses_plugin_data_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AKASIC_RESTORE_DIR", "   ")
    assert shell_restore._restore_dir(tmp_path) == tmp_path / "restore"


@pytest.mark.asyncio
async def test_v3_transform_rewrites_real_executor_input(tmp_path: Path) -> None:
    data_root = tmp_path / "plugin-data" / "shell_restore"
    runtime = PluginRuntime(
        plugin_id="shell_restore",
        plugin_dir=tmp_path / "plugin",
        data_dir=data_root,
        workspace=tmp_path / "workspace",
        config={},
    )
    root = CompositionRoot("shell-restore-test")
    _ = await root.mount(
        lambda ctx: shell_restore.apply(ctx, {}),
        name="shell_restore",
        runtime=runtime,
    )
    assert not data_root.joinpath("restore").exists()
    invoked: list[tuple[str, dict[str, Any]]] = []

    async def invoke(tool_name: str, arguments: dict[str, Any]) -> str:
        invoked.append((tool_name, arguments))
        return "ok"

    async with _bound_root(root):
        result = await ToolExecutor().execute(
            ToolExecutionRequest(
                call_id="call-1",
                tool_name="shell",
                arguments={"command": "rm /tmp/a.txt"},
                source="passive",
            ),
            invoke,
        )

    assert result.status == "success"
    assert invoked == [
        ("shell", {"command": f"mv -- /tmp/a.txt {data_root / 'restore'}"})
    ]
    assert data_root.joinpath("restore").is_dir()
    assert root.topology_view().listeners == (
        "transform:tool.input.prepare[akashic.tool-input.v1]:shell_restore",
    )
    await root.dispose()
    assert root.topology_view().listeners == ()


@pytest.mark.asyncio
async def test_v3_transform_preserves_file_through_real_shell(
    tmp_path: Path,
) -> None:
    source = tmp_path / "valuable.txt"
    source.write_text("keep me", encoding="utf-8")
    data_root = tmp_path / "plugin-data" / "shell_restore"
    root = CompositionRoot("shell-restore-real-shell")
    _ = await root.mount(
        lambda ctx: shell_restore.apply(ctx, {}),
        name="shell_restore",
        runtime=PluginRuntime(
            plugin_id="shell_restore",
            plugin_dir=tmp_path / "plugin",
            data_dir=data_root,
            workspace=tmp_path / "workspace",
            config={},
        ),
    )
    process_manager = ShellProcessManager()
    shell = ShellTool(process_manager, working_dir=tmp_path)

    async def invoke(tool_name: str, arguments: dict[str, Any]) -> str:
        assert tool_name == "shell"
        return await shell.execute(**arguments)

    try:
        async with _bound_root(root):
            result = await ToolExecutor().execute(
                ToolExecutionRequest(
                    call_id="call-real-shell",
                    tool_name="shell",
                    arguments={
                        "command": f"rm {source}",
                        "description": "验证可恢复删除",
                        "login": False,
                    },
                    source="passive",
                ),
                invoke,
            )
    finally:
        await process_manager.shutdown()
        await root.dispose()

    output = json.loads(str(result.output))
    assert result.status == "success"
    assert output["process_status"] == "succeeded"
    assert not source.exists()
    restored = data_root / "restore" / source.name
    assert restored.read_text(encoding="utf-8") == "keep me"


@pytest.mark.asyncio
async def test_v3_plugin_loads_through_real_generation_manager(
    tmp_path: Path,
) -> None:
    plugin_home = tmp_path / "plugins"
    plugin_home.mkdir()
    _ = shutil.copytree(
        Path(__file__).parents[1],
        plugin_home / "shell_restore",
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
        ),
    )
    manager = PluginManager(
        plugin_dirs=[plugin_home],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=tmp_path / "workspace",
        installed_cache_root=tmp_path / "plugin-home" / "cache",
    )

    await manager.load_all()

    generation = manager.generation("shell_restore")
    snapshot = manager.current_snapshot
    assert generation is not None and snapshot is not None
    assert isinstance(generation.instance, ComposablePlugin)
    assert snapshot.composition_topology is not None
    assert snapshot.composition_topology.listeners == (
        "transform:tool.input.prepare[akashic.tool-input.v1]:shell_restore",
    )
    root = snapshot.composition_root
    assert root is not None

    invoked: list[dict[str, Any]] = []

    async def invoke(_: str, arguments: dict[str, Any]) -> str:
        invoked.append(arguments)
        return "ok"

    lease = manager._snapshot_store.lease()
    token = bind_runtime_snapshot(lease)
    try:
        result = await ToolExecutor().execute(
            ToolExecutionRequest(
                call_id="manager-call",
                tool_name="shell",
                arguments={"command": "rm /tmp/manager.txt"},
                source="passive",
            ),
            invoke,
        )
    finally:
        reset_runtime_snapshot(token)
        await lease.release()

    assert result.status == "success"
    assert invoked == [
        {
            "command": (
                "mv -- /tmp/manager.txt "
                f"{generation.data_dir / 'restore'}"
            )
        }
    ]
    await manager.terminate_all()
    assert root.receipt().effects == ()
