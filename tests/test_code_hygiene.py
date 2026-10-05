"""Whole-package checks for bugs that tests of individual features can miss."""
import ast
import symtable
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "handoff"


def _module_imports(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return names


def _functions(table):
    for child in table.get_children():
        if child.get_type() == "function":
            yield child
        yield from _functions(child)


def test_no_function_shadows_a_module_import():
    # In Ixel, `stats = describe_large_paste(...)` inside a function made every
    # `stats.` in it an UnboundLocalError
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        imported = _module_imports(ast.parse(source))
        for func in _functions(symtable.symtable(source, str(path), "exec")):
            for sym in func.get_symbols():
                if sym.get_name() in imported and sym.is_local() and sym.is_assigned() and not sym.is_imported():
                    problems.append(f"{path.relative_to(PACKAGE.parent)}: {func.get_name()}() assigns "
                                    f"'{sym.get_name()}', hiding the module-level import")
    assert not problems, "\n".join(problems)


def test_text_files_are_opened_with_an_explicit_encoding():
    # Windows' default is the ANSI code page (cp1252), so a file written without
    # an encoding can't be read back as UTF-8
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            keywords = {k.arg for k in node.keywords}
            if name in ("read_text", "write_text") and "encoding" not in keywords:
                problems.append(f"{path.name}:{node.lineno} {name}() without encoding=")
            if name == "open" and isinstance(func, ast.Name) and "encoding" not in keywords:
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if not (isinstance(mode, ast.Constant) and "b" in str(mode.value)):
                    problems.append(f"{path.name}:{node.lineno} open() in text mode without encoding=")
            if name in ("run", "Popen", "check_output") and "text" in keywords and "encoding" not in keywords:
                problems.append(f"{path.name}:{node.lineno} {name}(text=True) without encoding=")
    assert not problems, "\n".join(problems)


def test_help_shows_every_command_usage_verbatim():
    # "[--flags]" in a usage string would be swallowed as a Rich style tag
    from handoff import cli

    with cli.console.capture() as captured:
        cli.cmd_help([])
    text = captured.get()
    for usage, _ in cli.COMMANDS:
        assert usage in text, usage


def test_windows_installer_is_plain_ascii():
    # Windows PowerShell 5.1 reads a script without a BOM in the ANSI code page,
    # so one curly quote or dash in install.ps1 can break parsing
    root = PACKAGE.parent
    for name in ("install.ps1", "install.sh"):
        data = (root / name).read_bytes()
        bad = [i for i, byte in enumerate(data) if byte > 0x7F]
        assert not bad, f"{name}: non-ASCII byte at offset {bad[0]}"


def test_programs_are_looked_up_on_path_only():
    # On Windows, shutil.which (and a bare program name) looks in the current folder first, and that is often a
    # project you cloned: `handoff doctor` ran a planted claude.cmd, panel reviews a planted ixel.cmd
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and node.attr == "which" and isinstance(node.value, ast.Name)
                    and node.value.id == "shutil"):
                problems.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno} uses shutil.which; "
                                "use proc.find_on_path")
    assert not problems, "\n".join(problems)


_STARTERS = {"run", "Popen", "check_output", "check_call", "call", "run_tree"}


def test_programs_are_never_started_by_a_bare_name():
    # Started by a bare name (["taskkill", ...]), Windows looks for the program in the current folder first. Each
    # command a list starts with must be a full path: from proc.find_on_path, sys.executable or the like
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if (name in _STARTERS and isinstance(first, ast.List) and first.elts
                    and isinstance(first.elts[0], ast.Constant) and isinstance(first.elts[0].value, str)):
                problems.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno} starts "
                                f"{first.elts[0].value!r} by name; use its full path (proc.find_on_path)")
    assert not problems, "\n".join(problems)


def test_taskkill_comes_from_system32(monkeypatch):
    from handoff import proc
    monkeypatch.setenv("SystemRoot", "D:\\Windows")
    assert proc.taskkill().replace("\\", "/") == "D:/Windows/System32/taskkill.exe"
    monkeypatch.delenv("SystemRoot")
    assert proc.taskkill().replace("\\", "/").endswith("Windows/System32/taskkill.exe")
