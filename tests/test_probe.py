"""Tests for ``scripts/probe.py``.

The probe is a stand-alone script: it must run on Python 3.12 with nothing but
``aiohttp`` installed, never import Home Assistant and never print anything
secret.  Its ``--check`` mode runs in a subprocess here; the rest is checked
by inspecting the source.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

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


def _probe_module() -> Any:
    """Import scripts/probe.py as a module (its helpers need no network)."""
    spec = importlib.util.spec_from_file_location("probe_under_test", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agenda_shape_prints_only_all_day_and_raw_times(capsys: pytest.CaptureFixture[str]) -> None:
    """``--agenda-vorm`` shows allDay, raw start/end and their zone; never titles or text."""
    probe = _probe_module()
    probe.describe_agenda(
        [
            {
                "title": "private-titel",
                "description": "private-tekst",
                "location": "private-plek",
                "allDay": True,
                "start": "2026-11-26T00:00:00Z",
                "end": "2026-11-27T00:00:00Z",
            },
            {"allDay": False, "start": "2026-11-30T19:00:00", "end": "2026-11-30T20:30:00+01:00"},
            {"allDay": "ja", "start": "private-geen-datum", "end": None},
        ]
    )
    out = capsys.readouterr().out
    assert "private-" not in out
    assert "start=2026-11-26T00:00:00Z (tijdzone: Z)" in out
    assert "start=2026-11-30T19:00:00 (tijdzone: geen)" in out
    assert "end=2026-11-30T20:30:00+01:00 (tijdzone: +01:00)" in out
    assert "allDay=<str>  start=<str> (tijdzone: onbekend)" in out


def test_children_shape_prints_field_names_and_overlap_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kinderen-vorm`` shows field names and yes/no answers; never names or ids."""
    probe = _probe_module()
    account = {
        "displayName": "private-ouder",
        "roles": [
            {
                "id": 11,
                "type": 1,
                "school": {"id": 7, "name": "private-school"},
                "connections": [
                    {
                        "id": 5,
                        "displayName": "private-kind",
                        "communities": [{"id": 900, "name": "private-6A"}],
                    }
                ],
            },
            {"id": 12, "type": 2, "school": {"id": 8}},
        ],
    }
    posts = [{"communities": [{"id": 900, "groupId": 3, "name": "private-6A"}]}]
    probe.describe_children(account, posts, 1)
    out = capsys.readouterr().out
    assert "private-" not in out
    assert "900" not in out
    assert (
        "Velden van roles[0].connections[0]: communities (list), displayName (str), id (int)" in out
    )
    assert "Community-id's in connections: ja" in out
    assert "Overlap van die id's met communities[].id: ja" in out
    assert "Overlap van die id's met communities[].groupId: nee" in out


def _account_with(connections: list[Any]) -> dict[str, Any]:
    """Return a ``useraccounts/current`` body with one parent role holding ``connections``."""
    return {
        "displayName": "private-ouder",
        "roles": [
            {
                "id": 11,
                "type": 1,
                "school": {"id": 7, "name": "private-school"},
                "connections": connections,
            }
        ],
    }


def _children_output(
    probe: Any, capsys: pytest.CaptureFixture[str], connections: list[Any], posts: list[Any]
) -> str:
    """Run ``describe_children`` and return what it printed; no value may have leaked."""
    probe.describe_children(_account_with(connections), posts, 1)
    out = capsys.readouterr().out
    assert "private-" not in out
    for number in ("987001", "987002", "555001", "555002", "424242"):
        assert number not in out
    return out


CHILD = {
    "id": 424242,
    "displayName": "private-kind",
    "firstName": "private-voornaam",
    "code": "private-code",
    "yearClass": "private-6A",
}
FEED = [
    {"communities": [{"id": 555001, "groupId": 987001, "name": "private-6A"}]},
    {"communities": [{"id": 555002, "groupId": 987002, "name": "private-Schoolnieuws"}]},
]


def test_children_groups_linked_by_group_id(capsys: pytest.CaptureFixture[str]) -> None:
    """Group objects: their field names and types, and which field links to the feed."""
    probe = _probe_module()
    group = {"groupId": 987001, "id": "private-guid", "name": "private-6A"}
    out = _children_output(probe, capsys, [{**CHILD, "groups": [group]}], FEED)
    lines = out.splitlines()
    assert "Groepen per connection: 1" in lines
    assert (
        "Vorm van roles[0].connections[0].groups[0]: object; "
        "velden: groupId (int), id (str), name (str)"
    ) in lines
    assert (
        "groupId uit de feed in de groepen van een kind: ja, bij 1 van 2 berichten "
        "(via groups[].groupId)"
    ) in lines
    assert "id uit de feed in de groepen van een kind: nee" in lines
    assert (
        "Groepsnaam van een kind gelijk aan een communitynaam in de feed: "
        "ja, bij 1 van 2 berichten (via groups[].name)"
    ) in lines
    assert "yearClass exact gelijk aan een communitynaam in de feed: ja, bij 1 van 2 berichten" in (
        lines
    )
    assert (
        "yearClass gelijk zonder verschil in hoofdletters en spaties: ja, bij 1 van 2 berichten"
    ) in lines


def test_children_groups_as_bare_ids(capsys: pytest.CaptureFixture[str]) -> None:
    """A group list of bare numbers: the type is shown and the link goes through ``groups[]``."""
    probe = _probe_module()
    connections = [{**CHILD, "groups": [987001, 987002]}, {**CHILD, "groups": []}]
    out = _children_output(probe, capsys, connections, FEED)
    lines = out.splitlines()
    assert "Groepen per connection: 2, 0" in lines
    assert "Vorm van roles[0].connections[0].groups[0]: int" in lines
    assert (
        "groupId uit de feed in de groepen van een kind: ja, bij 2 van 2 berichten (via groups[])"
    ) in lines
    assert "id uit de feed in de groepen van een kind: nee" in lines
    assert "Groepsnaam van een kind gelijk aan een communitynaam in de feed: nee" in lines


def test_children_without_groups_compare_year_class_loosely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without groups nothing links by group; yearClass differs only in case and spacing."""
    probe = _probe_module()
    child = {**CHILD, "yearClass": " private-6a ", "groups": []}
    sibling = {"id": 424242, "displayName": "private-broer", "groups": None}
    out = _children_output(probe, capsys, [child, sibling], FEED)
    lines = out.splitlines()
    assert "Groepen per connection: 0, 0" in lines
    assert "Vorm van connections[].groups[0]: geen groepen (lijst leeg of ontbreekt)" in lines
    assert "groupId uit de feed in de groepen van een kind: nee" in lines
    assert "yearClass exact gelijk aan een communitynaam in de feed: nee" in lines
    assert (
        "yearClass gelijk zonder verschil in hoofdletters en spaties: ja, bij 1 van 2 berichten"
    ) in lines


def test_children_with_a_feed_without_group_ids(capsys: pytest.CaptureFixture[str]) -> None:
    """Communities without ``groupId`` cannot answer the question: the probe says so."""
    probe = _probe_module()
    group = {"groupId": 987001, "name": "private-6A"}
    feed = [{"communities": [{"id": 555001, "name": "private-Iets"}]}]
    child = {key: value for key, value in CHILD.items() if key != "yearClass"}
    out = _children_output(probe, capsys, [{**child, "groups": [group]}], feed)
    lines = out.splitlines()
    assert (
        "groupId uit de feed in de groepen van een kind: onbekend "
        "(geen communities[].groupId in de feed)"
    ) in lines
    assert "id uit de feed in de groepen van een kind: nee" in lines
    assert "yearClass gelijk aan een communitynaam in de feed: onbekend (geen yearClass)" in lines


def test_children_with_an_empty_feed(capsys: pytest.CaptureFixture[str]) -> None:
    """Without posts there is nothing to compare; the probe still shows the shape of the groups."""
    probe = _probe_module()
    group = {"groupId": 987001, "name": "private-6A"}
    out = _children_output(probe, capsys, [{**CHILD, "groups": [group]}], [])
    lines = out.splitlines()
    assert (
        "Vorm van roles[0].connections[0].groups[0]: object; velden: groupId (int), name (str)"
        in (lines)
    )
    assert "Communitynamen in de feed: geen, dus niets te vergelijken" in lines
