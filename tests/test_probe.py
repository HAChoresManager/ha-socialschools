"""Tests for ``scripts/probe.py``.

The probe is a stand-alone script: it must run on Python 3.12 with nothing but
``aiohttp`` installed, never import Home Assistant and never print anything
secret.  Its ``--check`` mode runs in a subprocess here; the rest is checked
by inspecting the source.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = Path("scripts") / "probe.py"
PYTHON_312 = Path("/usr/bin/python3.12")
SUBPROCESS_TIMEOUT = 120

# Identifiers that may hold a secret in the probe: exact names, or names that start
# or end with a secret-like word (``refresh_token``, ``code_verifier``, ...).
FORBIDDEN_IDENTIFIER = re.compile(
    r"^(?:password|token|code|username|authorization|manager|session|exchanged|refreshed)$"
    r"|^(?:password|token|code|secret|cookie|verifier)_"
    r"|_(?:password|token|code|secret|cookie|verifier)$"
)
# The same words anywhere in code (not inside string literals), ``_`` not being a boundary.
FORBIDDEN_WORD = re.compile(r"(?<![a-z0-9])(?:password|token|code)(?![a-z0-9])", re.IGNORECASE)
STRING_LITERAL = re.compile(r"""[rRfFbB]{0,2}(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')""")

# Runs ``probe.py --check`` with every ``homeassistant`` import forbidden and reports
# which integration-related modules ended up in sys.modules.
IMPORT_GUARD = """\
import importlib.abc
import json
import runpy
import sys


class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "homeassistant" or name.startswith("homeassistant."):
            raise ImportError(f"forbidden import: {name}")
        return None


sys.meta_path.insert(0, Guard())
script = sys.argv[1]
sys.argv = [script, "--check"]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as err:
    status = err.code
else:
    status = 0
prefixes = ("ss_probe", "custom_components", "homeassistant")
print(json.dumps(sorted(name for name in sys.modules if name.startswith(prefixes))))
sys.exit(status)
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a command from the repository root without a terminal on stdin."""
    return subprocess.run(  # noqa: S603 - fixed interpreter paths and file names from this test
        list(args),
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )


def _source() -> str:
    """Return the probe's source text."""
    return (REPO_ROOT / PROBE).read_text(encoding="utf-8")


def _print_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every ``print(...)`` call in the tree."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]


def test_check_mode_prints_ok() -> None:
    """``python -I scripts/probe.py --check`` loads the pure modules, prints ``ok`` and exits 0."""
    result = _run(sys.executable, "-I", str(PROBE), "--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert result.stderr == ""


def test_check_mode_runs_with_home_assistant_imports_forbidden() -> None:
    """The ``--check`` run succeeds under a finder that raises on any ``homeassistant`` import.

    It must load the modules under the stand-in package, not the real integration package.
    """
    result = _run(sys.executable, "-I", "-c", IMPORT_GUARD, str(PROBE))
    assert result.returncode == 0, result.stderr
    output, loaded_line = result.stdout.splitlines()
    assert output == "ok"
    loaded = json.loads(loaded_line)
    assert {"ss_probe.login", "ss_probe.tokens", "ss_probe.client", "ss_probe.models"} <= set(
        loaded
    )
    assert not [name for name in loaded if not name.startswith("ss_probe")]


def test_compiles_with_python_312(tmp_path: Path) -> None:
    """The probe is valid Python 3.12, the version of the documented container image."""
    interpreter = str(PYTHON_312) if PYTHON_312.exists() else shutil.which("python3.12")
    if interpreter is None:
        pytest.skip("python3.12 is not installed")
    result = _run(
        interpreter,
        "-I",
        "-c",
        "import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
        str(PROBE),
        str(tmp_path / "probe.pyc"),
    )
    assert result.returncode == 0, result.stderr


def test_source_never_imports_home_assistant() -> None:
    """No static import, no dynamic import and not even the word ``homeassistant`` in the source."""
    source = _source()
    tree = ast.parse(source, filename=str(PROBE))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the probe must not use relative imports"
            imported.append(node.module or "")
    assert imported
    assert not [name for name in imported if name.partition(".")[0] == "homeassistant"]
    assert "homeassistant" not in source.lower()
    assert 'PACKAGE_NAME = "ss_probe"' in source
    assert "types.ModuleType(PACKAGE_NAME)" in source


def test_prints_reference_no_secret_variables() -> None:
    """No ``print`` call references a variable or attribute that may hold a secret."""
    source = _source()
    offenders: list[str] = []
    for call in _print_calls(ast.parse(source, filename=str(PROBE))):
        for inner in ast.walk(call):
            if isinstance(inner, ast.Name):
                name = inner.id
            elif isinstance(inner, ast.Attribute):
                name = inner.attr
            else:
                continue
            if FORBIDDEN_IDENTIFIER.search(name):
                offenders.append(f"line {call.lineno}: {name}")
    assert not offenders, offenders
    # Sanity check on the raw text: no secret-like word in the code of a print line
    # (string literals, which carry the Dutch labels, are blanked first).
    for lineno, line in enumerate(source.splitlines(), 1):
        if "print(" not in line:
            continue
        code_only = STRING_LITERAL.sub('""', line)
        assert not FORBIDDEN_WORD.search(code_only), f"line {lineno}: {line.strip()}"


def test_credentials_come_only_from_prompts() -> None:
    """E-mail and password are prompted for; no command line option or environment variable."""
    source = _source()
    tree = ast.parse(source, filename=str(PROBE))
    options = [
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for arg in node.args
        if isinstance(arg, ast.Constant)
    ]
    assert options == ["--check", "--agenda-vorm", "--kinderen-vorm"]
    assert 'input("E-mailadres: ")' in source
    assert 'getpass.getpass("Wachtwoord: ")' in source
    assert "del password" in source
    environment_access = [
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
        and (node.attr if isinstance(node, ast.Attribute) else node.id) in {"environ", "getenv"}
    ]
    assert not environment_access
