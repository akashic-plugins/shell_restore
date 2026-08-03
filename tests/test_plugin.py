from plugin import ShellRestore


def test_plugin_no_longer_registers_shell_precheck() -> None:
    assert not hasattr(ShellRestore, "rewrite_rm_to_mv")
