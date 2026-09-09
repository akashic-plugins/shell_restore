from __future__ import annotations

import logging
import os
import shlex
from collections.abc import Mapping
from pathlib import Path

from agent.plugin_composition import Context
from plugins.tools.plugin import TOOLS
from plugins.standard_tools.plugin import STANDARD_TOOLS

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
_SUDO_COMMAND_FLAGS = {
    "-n",
    "--non-interactive",
    "-A",
    "--askpass",
    "-b",
    "--background",
    "-B",
    "--bell",
    "-E",
    "--preserve-env",
    "-H",
    "--set-home",
    "-k",
    "--reset-timestamp",
    "-K",
    "--remove-timestamp",
    "-P",
    "--preserve-groups",
    "-S",
    "--stdin",
}
_SUDO_OPTIONS_WITH_VALUE = {
    "-u",
    "--user",
    "-g",
    "--group",
    "-p",
    "--prompt",
    "-C",
    "--close-from",
    "-D",
    "--chdir",
    "-R",
    "--chroot",
    "-T",
    "--command-timeout",
    "--host",
}
_SUDO_COMMAND_SHORT_FLAGS = frozenset("nAbBEHkKPS")
_SUDO_SHORT_OPTIONS_WITH_VALUE = frozenset({"u", "g", "p", "C", "D", "R", "T"})

api_version = 3
name = "shell_restore"
version = "3.0.0"
desc = "把简单 rm 调用改写到插件自有还原目录"
author = "Akashic"
inject = (TOOLS, STANDARD_TOOLS)


async def apply(ctx: Context, config: object) -> None:
    """Register the shell argument transform against this generation data root."""

    # 1. Core 只分配路径；插件拥有还原目录和命令改写规则。
    _ = config
    restore_dir = _restore_dir(ctx.data_root)

    # 2. Shell binding 固定这一位参数 owner，恢复继续使用同一实现。
    async def rewrite_rm_to_mv(arguments: Mapping[str, object]) -> Mapping[str, object]:
        command = str(arguments.get("command", "")).strip()
        rewritten = _rewrite_command(command, restore_dir)
        if rewritten is None:
            return arguments
        restore_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[%s:rewrite_rm_to_mv] rm → mv: %r", name, rewritten)
        return {**arguments, "command": rewritten}

    _ = await ctx.require(TOOLS).register_prepare(
        ctx, tool=ctx.require(STANDARD_TOOLS).select("shell"), name="restore", prepare=rewrite_rm_to_mv,
    )


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
        if token == "sudo":
            prefix.append(token)
            index += 1
            consumed = _consume_sudo_options(tokens, index, prefix)
            if consumed is None:
                return None
            index = consumed
            continue
        if token == "env" or "=" in token:
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


def _consume_sudo_options(
    tokens: list[str],
    index: int,
    prefix: list[str],
) -> int | None:
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            prefix.append(token)
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        if token in _SUDO_COMMAND_FLAGS:
            prefix.append(token)
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            option = token.split("=", 1)[0]
            if (
                option not in _SUDO_OPTIONS_WITH_VALUE
                and option != "--preserve-env"
            ):
                return None
            prefix.append(token)
            index += 1
            continue
        if token.startswith("-") and not token.startswith("--") and len(token) > 2:
            consumed = _consume_sudo_short_cluster(tokens, index, prefix)
            if consumed is None:
                return None
            index = consumed
            continue
        if token not in _SUDO_OPTIONS_WITH_VALUE:
            return None
        prefix.append(token)
        index += 1
        if index >= len(tokens):
            return None
        prefix.append(tokens[index])
        index += 1
    return index


def _consume_sudo_short_cluster(
    tokens: list[str],
    index: int,
    prefix: list[str],
) -> int | None:
    token = tokens[index]
    cluster = token[1:]
    for offset, option in enumerate(cluster):
        if option in _SUDO_COMMAND_SHORT_FLAGS:
            continue
        if option not in _SUDO_SHORT_OPTIONS_WITH_VALUE:
            return None
        prefix.append(token)
        if offset + 1 < len(cluster):
            return index + 1
        if index + 1 >= len(tokens):
            return None
        prefix.append(tokens[index + 1])
        return index + 2
    prefix.append(token)
    return index + 1
