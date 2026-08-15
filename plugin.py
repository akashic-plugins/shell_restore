from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

from agent.plugin_composition import Context
from agent.tools.events import TOOL_INPUT_PREPARE, ToolInput

logger = logging.getLogger("plugin.shell_restore")

_SHELL_CONTROL = {
    "&&",
    "||",
    ";",
    "|",
    "&",
    ">",
    ">>",
    "<",
    "<<",
    "`",
    "$(",
    "{",
    "}",
    "(",
    ")",
}

api_version = 3
name = "shell_restore"
version = "2.0.0"
desc = "把简单 rm 调用改写到插件自有还原目录"
author = "Akashic"
inject: tuple[()] = ()


async def apply(ctx: Context, config: object) -> None:
    """Register the shell argument transform against this generation data root."""

    # 1. Core 只分配路径；插件拥有还原目录和命令改写规则。
    _ = config
    restore_dir = _restore_dir(ctx.data_root)

    # 2. Transform 只处理 shell，其他工具原样通过。
    def rewrite_rm_to_mv(tool_input: ToolInput) -> ToolInput:
        if tool_input.tool_name != "shell":
            return tool_input
        command = str(tool_input.arguments.get("command", "")).strip()
        rewritten = _rewrite_command(command, restore_dir)
        if rewritten is None:
            return tool_input
        restore_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[%s:rewrite_rm_to_mv] rm → mv: %r", name, rewritten)
        arguments = tool_input.mutable_arguments()
        arguments["command"] = rewritten
        return tool_input.with_arguments(arguments)

    _ = await ctx.on(TOOL_INPUT_PREPARE, rewrite_rm_to_mv)


def _rewrite_command(command: str, restore_dir: Path) -> str | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    # 1. 读取 rm 前面的前缀（sudo、env、VAR=val 等）。
    prefix: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if Path(token).name == "rm":
            break
        if token == "sudo" or token == "env" or "=" in token:
            prefix.append(token)
            index += 1
            continue
        return None
    if index >= len(tokens) or Path(tokens[index]).name != "rm":
        return None

    # 2. 跳过 rm 与 option，复杂 shell 语法保持原样放行。
    index += 1
    targets: list[str] = []
    parsing_options = True
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token in _SHELL_CONTROL or token.startswith("$("):
            return None
        if parsing_options and token == "--":
            parsing_options = False
            continue
        if parsing_options and token.startswith("-") and token != "-":
            continue
        parsing_options = False
        targets.append(token)
    if not targets:
        return None

    # 3. 改写为 mv -- targets... restore_dir。
    return shlex.join([*prefix, "mv", "--", *targets, str(restore_dir)])


def _restore_dir(data_root: Path) -> Path:
    explicit = os.environ.get("AKASIC_RESTORE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    return data_root / "restore"
