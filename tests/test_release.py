"""Tests for ``scripts/release.py`` and the release workflow.

The script runs in GitHub Actions on every push to main.  Its pure parts are tested
directly; ``run`` is tested end to end in a throw-away git repository with merge
commits, with a fake ``gh`` on the PATH that records what it was asked to create.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest
import yaml

from custom_components.socialschools.const import VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
PYTHON_312 = Path("/usr/bin/python3.12")
SHA_RE = re.compile(r"[0-9a-f]{40}")

CHANGELOG = """\
# Changelog

## [Unreleased]

## [0.2.1] - 2026-11-08

### Opgelost

- Hele dagen duren niet langer een dag te lang.

## [0.2.0] - 2026-11-07

### Toegevoegd

- Gelezen per persoon.

## [0.1.0] - 2026-11-04

### Toegevoegd

- Eerste uitgave.

[0.2.1]: https://example.test/compare/v0.2.0...v0.2.1
"""
NOTES_021 = "### Opgelost\n\n- Hele dagen duren niet langer een dag te lang.\n"
NOTES_020 = "### Toegevoegd\n\n- Gelezen per persoon.\n"

FAKE_GH = """\
#!{python}
import json, os, sys
args = sys.argv[1:]
notes = ""
if "--notes-file" in args:
    with open(args[args.index("--notes-file") + 1], encoding="utf-8") as file:
        notes = file.read()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"args": args, "notes": notes}}) + "\\n")
sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
"""


def _release_module() -> Any:
    """Import scripts/release.py as a module."""
    spec = importlib.util.spec_from_file_location("release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _release_module()


# --- manifest and changelog -------------------------------------------------------------------


def test_manifest_version_is_the_integration_version() -> None:
    """The script reads the same version as const.VERSION (which a test ties to the manifest)."""
    text = (REPO_ROOT / release.MANIFEST).read_text(encoding="utf-8")
    assert release.manifest_version(text) == VERSION


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="list"),
        pytest.param("{}", id="no-version"),
        pytest.param('{"version": 2}', id="number"),
        pytest.param('{"version": "0.2"}', id="two-parts"),
        pytest.param('{"version": "v0.2.0"}', id="prefixed"),
    ],
)
def test_manifest_version_rejects_unusable_values(text: str) -> None:
    """Only an x.y.z string is a version a tag can be made of."""
    with pytest.raises(release.ReleaseError):
        release.manifest_version(text)


def test_the_current_version_has_a_changelog_section() -> None:
    """After a merge the workflow needs the CHANGELOG.md section of the manifest version."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    body = release.changelog_section(text, VERSION)
    assert body.startswith("### ")
    assert "## [" not in body


def test_changelog_section_stops_at_the_next_heading_and_the_link_definitions() -> None:
    """A section runs to the next ``## `` heading; link definitions below the last are not text."""
    assert release.changelog_section(CHANGELOG, "0.2.1") == NOTES_021
    assert release.changelog_section(CHANGELOG, "0.2.0") == NOTES_020
    assert release.changelog_section(CHANGELOG, "0.1.0") == "### Toegevoegd\n\n- Eerste uitgave.\n"


@pytest.mark.parametrize(
    ("version", "message"),
    [
        pytest.param("0.3.0", "geen sectie", id="missing"),
        pytest.param("Unreleased", "leeg", id="empty"),
        pytest.param("0.2", "geen sectie", id="prefix-of-a-version"),
    ],
)
def test_changelog_section_missing_or_empty(version: str, message: str) -> None:
    """A release without its changelog text is refused instead of published empty."""
    with pytest.raises(release.ReleaseError, match=message):
        release.changelog_section(CHANGELOG, version)


# --- choosing the commit ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("history", "version", "expected"),
    [
        pytest.param(
            [("m2", "0.2.1"), ("m1", "0.2.0"), ("a", "0.1.0")], "0.2.1", "m2", id="newest"
        ),
        pytest.param([("m2", "0.2.1"), ("m1", "0.2.0"), ("a", "0.1.0")], "0.2.0", "m1", id="older"),
        pytest.param([("m2", "0.2.1"), ("m1", "0.2.0")], "0.3.0", None, id="absent"),
        pytest.param(
            [("x", "0.2.1"), ("m2", "0.2.1"), ("m1", "0.2.0")], "0.2.1", "m2", id="later-edit"
        ),
        pytest.param(
            [("r", "0.2.0"), ("m2", "0.2.1"), ("m1", "0.2.0")], "0.2.0", "r", id="newest-run"
        ),
        pytest.param([("b", None), ("m1", "0.2.0")], "0.2.0", "m1", id="unreadable-manifest"),
        pytest.param([], "0.2.0", None, id="empty"),
    ],
)
def test_release_commit(history: list[tuple[str, str | None]], version: str, expected: str) -> None:
    """The oldest commit of the newest run that carries the version is where it arrived."""
    assert release.release_commit(history, version) == expected


# --- end to end in a git repository -----------------------------------------------------------


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate git from the user's configuration (signing, hooks) and give it an identity."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.test")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return its output."""
    result = subprocess.run(  # noqa: S603 - fixed arguments from this test
        ["git", *args],  # noqa: S607 - git from the PATH, like the script
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    """Write ``files`` and commit them; return the commit id."""
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _manifest(version: str) -> dict[str, str]:
    """Return the manifest file of ``version``."""
    return {release.MANIFEST: json.dumps({"domain": "socialschools", "version": version})}


def _merge(repo: Path, branch: str) -> str:
    """Merge ``branch`` into main with a merge commit, as GitHub merges a pull request."""
    _git(repo, "checkout", "--quiet", "main")
    _git(repo, "merge", "--quiet", "--no-ff", "-m", f"Merge pull request from {branch}", branch)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history_repo(tmp_path: Path, git_env: None) -> Iterator[tuple[Path, str, str]]:
    """Return a repository like this one: 0.1.0 on main, 0.2.0 and 0.2.1 merged by pull request.

    Yields (repository, merge commit of 0.2.0, merge commit of 0.2.1).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _commit(repo, "0.1.0", {**_manifest("0.1.0"), "CHANGELOG.md": CHANGELOG, "README.md": "a"})
    _git(repo, "checkout", "--quiet", "-b", "feat/0.2.0")
    _commit(repo, "bump to 0.2.0", _manifest("0.2.0"))
    _commit(repo, "docs", {"README.md": "b"})
    _git(repo, "checkout", "--quiet", "main")
    _commit(repo, "ci change on main", {"ci.yml": "x"})
    merge_020 = _merge(repo, "feat/0.2.0")
    _git(repo, "checkout", "--quiet", "-b", "feat/0.2.1")
    _commit(repo, "fix", {"README.md": "c"})
    _commit(repo, "bump to 0.2.1", _manifest("0.2.1"))
    merge_021 = _merge(repo, "feat/0.2.1")
    _commit(repo, "dependabot after the release", {"ci.yml": "y"})
    yield repo, merge_020, merge_021


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake ``gh`` on the PATH that logs its arguments and notes; return the log path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    return log


def _gh_calls(log: Path) -> list[dict[str, Any]]:
    """Return the logged gh calls."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_history_finds_the_merge_commits(history_repo: tuple[Path, str, str]) -> None:
    """On the first-parent chain a version arrives with the merge commit of its pull request."""
    repo, merge_020, merge_021 = history_repo
    history = release.first_parent_history(repo)
    assert [version for _, version in history] == ["0.2.1", "0.2.0", "0.1.0"]
    assert release.release_commit(history, "0.2.0") == merge_020
    assert release.release_commit(history, "0.2.1") == merge_021


def test_run_creates_the_release_of_the_manifest_version(
    history_repo: tuple[Path, str, str],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only v0.2.1, the manifest version: on its merge commit, latest, with its changelog text.

    Older versions (0.2.0 here) are not tagged afterwards.
    """
    repo, _, merge_021 = history_repo
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    assert release.run(repo) == 0
    (call,) = _gh_calls(fake_gh)
    assert call["args"][:5] == ["release", "create", "v0.2.1", "--target", merge_021]
    assert SHA_RE.fullmatch(call["args"][4])
    assert "--latest=true" in call["args"]
    assert call["args"][call["args"].index("--title") + 1] == "v0.2.1"
    assert call["args"][-2:] == ["--repo", "owner/repo"]
    assert call["notes"] == NOTES_021
    assert capsys.readouterr().out.splitlines() == [f"v0.2.1: aangemaakt op {merge_021}"]


def test_run_skips_an_existing_tag(
    history_repo: tuple[Path, str, str], fake_gh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the tag exists (the checkout fetches the tags), a run creates nothing."""
    repo, _, merge_021 = history_repo
    _git(repo, "tag", "v0.2.1", merge_021)
    assert release.run(repo) == 0
    assert _gh_calls(fake_gh) == []
    assert capsys.readouterr().out.splitlines() == ["v0.2.1: bestaat al"]


def test_dry_run_creates_nothing(
    history_repo: tuple[Path, str, str], fake_gh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` only says what it would create."""
    repo, _, merge_021 = history_repo
    assert release.main(["--repo", str(repo), "--dry-run"]) == 0
    assert _gh_calls(fake_gh) == []
    assert capsys.readouterr().out.splitlines() == [f"v0.2.1: zou worden aangemaakt op {merge_021}"]


def test_a_refused_tag_fails_the_run_with_a_hint(
    history_repo: tuple[Path, str, str],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When gh fails, the run fails and names both known causes and what to do."""
    repo, _, _ = history_repo
    monkeypatch.setenv("FAKE_GH_EXIT", "1")
    assert release.run(repo) == 1
    out = capsys.readouterr().out
    assert "::error::v0.2.1: gh release create v0.2.1 mislukte." in out
    assert "Resource not accessible by integration" in out
    assert "workflowbestanden" in out
    assert "Restrict creations" in out
    assert len(_gh_calls(fake_gh)) == 1


def test_a_version_without_changelog_text_is_not_released(
    history_repo: tuple[Path, str, str], fake_gh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No release without its CHANGELOG.md section."""
    repo, _, _ = history_repo
    _commit(repo, "changelog without 0.2.1", {"CHANGELOG.md": CHANGELOG.replace("[0.2.1]", "[x]")})
    assert release.run(repo) == 1
    assert _gh_calls(fake_gh) == []
    assert "::error::v0.2.1: CHANGELOG.md heeft geen sectie voor 0.2.1" in capsys.readouterr().out


def test_a_version_without_a_commit_is_not_released(
    history_repo: tuple[Path, str, str], fake_gh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manifest version that no first-parent commit carries cannot be placed: nothing is made."""
    repo, _, _ = history_repo
    _git(repo, "checkout", "--quiet", "-b", "loose")
    (repo / release.MANIFEST).write_text(json.dumps({"version": "0.9.0"}), encoding="utf-8")
    assert release.run(repo) == 1
    assert _gh_calls(fake_gh) == []
    assert "::error::v0.9.0: geen commit op deze branch met versie 0.9.0" in capsys.readouterr().out


# --- the script and the workflow --------------------------------------------------------------


def test_compiles_with_python_312(tmp_path: Path) -> None:
    """The runner's python3 is 3.12: the script must be valid there."""
    interpreter = str(PYTHON_312) if PYTHON_312.exists() else shutil.which("python3.12")
    if interpreter is None:
        pytest.skip("python3.12 is not installed")
    result = subprocess.run(  # noqa: S603 - fixed interpreter and file names
        [
            interpreter,
            "-I",
            "-c",
            "import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
            str(SCRIPT),
            str(tmp_path / "release.pyc"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _workflow() -> dict[Any, Any]:
    """Return the parsed release workflow."""
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_runs_only_on_main_and_never_on_pull_requests() -> None:
    """Push to main (and a manual run on main); no pull request event, so no required check."""
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    job = workflow["jobs"]["release"]
    assert job["if"] == "github.ref == 'refs/heads/main'"


def test_workflow_permissions_and_actions() -> None:
    """Only contents: write; only GitHub's own actions, pinned to a commit; gh with the token."""
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "write"}
    steps = workflow["jobs"]["release"]["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]
    assert uses
    for action in uses:
        name, _, ref = action.partition("@")
        assert name.startswith("actions/"), action
        assert SHA_RE.fullmatch(ref), action
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    run_step = next(step for step in steps if "run" in step)
    assert run_step["run"] == "python3 scripts/release.py"
    assert run_step["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_validate_workflow_has_no_release_job() -> None:
    """The checks a pull request must pass (validate.yml) do not tag or release anything."""
    validate = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    )
    assert set(validate["jobs"]) == {"hassfest", "hacs", "tests"}
