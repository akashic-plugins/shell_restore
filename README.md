# shell_restore 插件

`rm` 命令安全改写。拦截 `shell` 工具调用，把简单 `rm` 命令改写为 `mv` 命令，将目标文件移入还原目录而非直接删除，防止 LLM 误操作导致不可逆数据丢失。

## 接入点

| 接入方式 | 阶段 |
|---|---|
| `tool.input.prepare` | v3 串行 transform，只改写 shell 的 arguments |

## 运作逻辑

### 1. 拦截 shell 工具

每次 LLM 调用 `shell` 工具时，transform 接收不可变 `ToolInput`，从只读 arguments view 取出 `command`。插件通过 `with_arguments()` 返回新的参数；call identity 仍由 Core 保持。

### 2. 解析命令（_rewrite_command）

用 `shlex.split` 做 POSIX 拆词，逐 token 扫描：

1. 跳过前缀词（`sudo`、`env`、`VAR=val` 形式的环境变量赋值）。
2. 定位到 `rm`（按文件名匹配，支持完整路径如 `/bin/rm`）。
3. 跳过 `rm` 的所有 option 标志（以 `-` 开头的 token，`--` 后视为路径）。
4. 收集剩余 token 作为删除目标列表。

**安全边界**：解析过程中遇到 shell 控制符（`&&`、`||`、`;`、`|`、重定向、子 shell、命令替换等）时，放弃改写并放行原命令。插件只处理可被 shlex 安全重组的简单形式，复杂命令不做猜测式改写。

若命令中没有 `rm`，或解析不出目标路径，返回 `None`，插件不做修改。

### 3. 改写为 mv

把解析出的目标路径重新组装为：

```
[prefix...] mv -- <target1> <target2> ... <restore_dir>
```

`restore_dir` 默认是 Core 分配给当前 generation 的 `ctx.data_root/restore`，目录结构和写入仍由插件自己拥有；也可通过环境变量 `AKASIC_RESTORE_DIR` 显式覆盖。目录若不存在则在改写时自动创建。

改写后的命令字典替换原 `arguments` 并继续执行，LLM 感知不到任何变化。

## 版本记录

- `2.0.0` 迁移到 API v3：通过 typed `tool.input.prepare` 注册 transform，移除 PluginContext/ToolHook 依赖。
- `1.0.0` 初始改写逻辑（04-30 迁移自 builtin tool-hook）。
- `1.0.1` 空插件（08-03 移除改写，担心 shlex 无法理解不同 Shell 完整语法）。
- `1.0.2` 恢复改写（08-09），并新增 shell 控制符放行边界：复杂命令一律不拦截，只处理简单 `rm` 形式。
