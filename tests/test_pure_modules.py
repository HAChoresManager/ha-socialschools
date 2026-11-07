"""Tests that the protocol modules stay pure (DESIGN.md §1).

``const``, ``errors``, ``payload``, ``login``, ``tokens``, ``client``,
``models`` and ``textutil`` must import nothing from ``homeassistant`` (directly
or indirectly), use only the standard library plus ``aiohttp``/``yarl``/
``multidict``, run on Python 3.12 and reach each other only through explicit
relative imports, so that ``scripts/probe.py`` can load them under a stand-in
package name without executing ``__init__.py``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from custom_components.socialschools.const import DOMAIN, VERSION

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "socialschools"
PURE_MODULES = (
    "const",
    "errors",
    "payload",
    "login",
    "tokens",
    "client",
    "models",
    "textutil",
    "reading",
    "children",
)
ALLOWED_THIRD_PARTY = frozenset({"aiohttp", "yarl", "multidict"})
PYTHON_312 = Path("/usr/bin/python3.12")
SUBPROCESS_TIMEOUT = 120  # seconds

# Loads the pure modules exactly like ``scripts/probe.py`` does: through a stand-in
# package that carries only ``__path__`` (so no ``__init__.py`` runs), after installing
# a meta-path finder that refuses every ``homeassistant`` import.  Arguments: the
# package directory, then the module names.  Prints nothing when all is well.
PROBE_LOADER = """\
import importlib
import sys
import types

package_dir, *module_names = sys.argv[1:]


class RefuseHomeAssistant:
    def find_spec(self, name, path=None, target=None):
        if name == "homeassistant" or name.startswith("homeassistant."):
            raise ImportError(f"pure module imported {name}")
        return None


sys.meta_path.insert(0, RefuseHomeAssistant())
package = types.ModuleType("ss_probe")
package.__path__ = [package_dir]
sys.modules["ss_probe"] = package
for name in module_names:
    importlib.import_module(f"ss_probe.{name}")
unexpected = sorted(
    name
    for name in sys.modules
    if name.partition(".")[0] in {"homeassistant", "custom_components"}
)
if unexpected:
    sys.exit(f"unexpected modules loaded: {unexpected}")
"""


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv (no shell) and capture its output."""
    return subprocess.run(  # noqa: S603 - fixed argv of trusted interpreters, no shell
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )


def python_312() -> str | None:
    """Return the path of a Python 3.12 interpreter, or None when there is none."""
    if PYTHON_312.is_file():
        return str(PYTHON_312)
    return shutil.which("python3.12")


def parse(module: str) -> ast.Module:
    """Parse a pure module with the Python 3.12 grammar."""
    path = PACKAGE_DIR / f"{module}.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 12))


@dataclass(frozen=True)
class Imported:
    """One import statement of a pure module, wherever it is nested."""

    lineno: int
    level: int  # 0 for an absolute import, 1 for ``from .x import`` / ``from . import x``
    module: str | None  # None only for ``from . import x``
    names: tuple[str, ...]  # the imported names of a ``from`` import

    @property
    def siblings(self) -> tuple[str, ...]:
        """Return the sibling modules a relative import reaches (empty when absolute)."""
        if self.level == 0:
            return ()
        if self.module is None:
            return self.names
        return (self.module,)


def imports_of(module: str) -> list[Imported]:
    """Return every import statement of a pure module, including guarded ones."""
    found: list[Imported] = []
    for node in ast.walk(parse(module)):
        if isinstance(node, ast.Import):
            found.extend(Imported(node.lineno, 0, alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names = tuple(alias.name for alias in node.names)
            found.append(Imported(node.lineno, node.level, node.module, names))
    return found


# --- subprocess checks ----------------------------------------------------------------------


def test_pure_modules_load_without_homeassistant(tmp_path: Path) -> None:
    """The stand-in package loader works while every homeassistant import is refused."""
    command = [sys.executable, "-I", "-c", PROBE_LOADER, str(PACKAGE_DIR), *PURE_MODULES]
    result = run(command, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == ""


def test_pure_modules_compile_on_python_312(tmp_path: Path) -> None:
    """Every pure module byte-compiles with a real Python 3.12 interpreter."""
    interpreter = python_312()
    if interpreter is None:
        pytest.skip("no Python 3.12 interpreter available")
    sources = [str(PACKAGE_DIR / f"{module}.py") for module in PURE_MODULES]
    result = run([interpreter, "-I", "-m", "py_compile", *sources], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


# --- manifest -------------------------------------------------------------------------------


def test_version_matches_manifest() -> None:
    """const.VERSION (part of the User-Agent) equals the manifest version."""
    manifest = json.loads((PACKAGE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == VERSION
    assert manifest["domain"] == DOMAIN


# --- static import checks -------------------------------------------------------------------


@pytest.mark.parametrize("module", PURE_MODULES)
def test_parses_with_python_312_grammar(module: str) -> None:
    """No syntax newer than Python 3.12 (also covers machines without a 3.12 interpreter)."""
    assert isinstance(parse(module), ast.Module)


@pytest.mark.parametrize("module", PURE_MODULES)
def test_no_homeassistant_import(module: str) -> None:
    """A pure module never imports homeassistant, not even under TYPE_CHECKING."""
    for imported in imports_of(module):
        if imported.level == 0:
            assert imported.module is not None
            assert imported.module.partition(".")[0] != "homeassistant", imported.lineno


@pytest.mark.parametrize("module", PURE_MODULES)
def test_no_absolute_custom_components_import(module: str) -> None:
    """Siblings are reached with explicit relative imports, never via custom_components."""
    for imported in imports_of(module):
        if imported.level == 0:
            assert imported.module is not None
            assert imported.module.partition(".")[0] != "custom_components", imported.lineno


@pytest.mark.parametrize("module", PURE_MODULES)
def test_relative_imports_reach_pure_siblings_only(module: str) -> None:
    """Relative imports are one level deep and name a pure module (never an HA module)."""
    for imported in imports_of(module):
        if imported.level:
            assert imported.level == 1, imported.lineno
            for sibling in imported.siblings:
                assert sibling in PURE_MODULES, (imported.lineno, sibling)


@pytest.mark.parametrize("module", PURE_MODULES)
def test_absolute_imports_are_stdlib_or_allowed_third_party(module: str) -> None:
    """Absolute imports are limited to the standard library, aiohttp, yarl and multidict."""
    found = imports_of(module)
    assert found  # every pure module at least imports ``annotations``
    for imported in found:
        if imported.level == 0:
            assert imported.module is not None
            top = imported.module.partition(".")[0]
            assert top in ALLOWED_THIRD_PARTY or top in sys.stdlib_module_names, (
                imported.lineno,
                top,
            )


def test_login_does_not_import_tokens() -> None:
    """The code exchange lives in tokens.py; login.py must not depend on it (DESIGN.md §4)."""
    for imported in imports_of("login"):
        assert "tokens" not in imported.siblings, imported.lineno
        if imported.level == 0:
            assert imported.module is not None
            assert not imported.module.endswith("tokens"), imported.lineno
