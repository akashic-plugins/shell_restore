# shell_restore 插件

兼容保留的空插件。`1.0.1` 起不再注册 `shell` 前置 hook，也不再解析或改写用户 Shell 命令。

Akashic Shell 由当前用户的默认 Shell 执行；插件无法只靠 POSIX `shlex` 准确理解不同 Shell 的完整语法，因此删除原先的 `rm` → `mv` 猜测式改写。
