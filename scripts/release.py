#!/usr/bin/env python3
"""Create the missing release tags and GitHub releases of the integration (CI helper).

``.github/workflows/release.yml`` runs this on every push to ``main``, from the
repository root of a checkout with the full history::

    python3 scripts/release.py              # create what is missing
    python3 scripts/release.py --dry-run    # only print what would be created

For the version in ``custom_components/socialschools/manifest.json``, and once for
each version in ``BACKFILL_VERSIONS`` (released before this workflow existed), it
checks whether the tag ``v<version>`` exists.  When it does not, it finds the commit
at which that version arrived on ``main`` (the oldest commit of the newest run of
first-parent commits whose manifest carries it: for a merged pull request the merge
commit), takes the section of that version from ``CHANGELOG.md`` and creates the tag
and the release with the ``gh`` CLI (``GH_TOKEN`` must be set).  Only the version in
the manifest is marked as the latest release.

Only the standard library; it runs on the Python 3.12 of the GitHub runner.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

MANIFEST = "custom_components/socialschools/manifest.json"
CHANGELOG = "CHANGELOG.md"
TAG_PREFIX = "v"
BACKFILL_VERSIONS: tuple[str, ...] = ("0.2.0",)
"""Versions from before this workflow; each is tagged once, where it arrived on main."""

_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SECTION_RE = re.compile(r"## \[(?P<version>[^\]]+)\](?:\s+-\s+.*)?")
_LINK_DEFINITION_RE = re.compile(r"\[[^\]]+\]:\s")
RULESET_HINT = (
    "Wordt het aanmaken van de tag geweigerd ('Repository rule violations', 'creations "
    "being restricted'), kijk dan bij Settings > Rules > Rulesets naar een ruleset voor "
    "tags met 'Restrict creations' die v* dekt."
)


class ReleaseError(Exception):
    """Something that stops a release; the message is printed for the workflow log."""


def manifest_version(text: str) -> str:
    """Return the ``version`` of manifest.json; it must look like ``1.2.3``."""
    try:
        version = json.loads(text)["version"]
    except (ValueError, KeyError, TypeError) as err:
        raise ReleaseError(f"{MANIFEST} heeft geen leesbare version") from err
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ReleaseError(f"{MANIFEST}: version {version!r} is geen x.y.z")
    return version


def changelog_section(text: str, version: str) -> str:
    """Return the body of the ``## [version] - date`` section of the changelog.

    The section ends at the next ``## `` heading or at the link definitions below the
    last section.  A missing or empty section raises ReleaseError.
    """
    lines: list[str] | None = None
    for line in text.splitlines():
        heading = _SECTION_RE.fullmatch(line.rstrip())
        if lines is None:
            if heading is not None and heading["version"] == version:
                lines = []
            continue
        if line.startswith("## ") or _LINK_DEFINITION_RE.match(line):
            break
        lines.append(line)
    if lines is None:
        raise ReleaseError(f"{CHANGELOG} heeft geen sectie voor {version}")
    body = "\n".join(lines).strip()
    if not body:
        raise ReleaseError(f"{CHANGELOG}: de sectie voor {version} is leeg")
    return body + "\n"


def release_commit(history: Iterable[tuple[str, str | None]], version: str) -> str | None:
    """Return the commit at which ``version`` arrived on the branch, or None.

    ``history`` holds (commit, manifest version) pairs of the first-parent chain,
    newest first; it may be limited to the commits that changed the manifest.  The
    answer is the oldest commit of the newest run of commits that carry ``version``.
    """
    found: str | None = None
    for commit, carried in history:
        if carried == version:
            found = commit
        elif found is not None:
            break
    return found


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return its standard output."""
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args[:2])} mislukte: {result.stderr.strip()}")
    return result.stdout


def first_parent_history(repo: Path, ref: str = "HEAD") -> list[tuple[str, str | None]]:
    """Return (commit, manifest version) for the first-parent commits that changed the manifest.

    Newest first.  A commit whose manifest cannot be read carries None.
    """
    commits = _git(repo, "rev-list", "--first-parent", ref, "--", MANIFEST).split()
    history: list[tuple[str, str | None]] = []
    for commit in commits:
        try:
            version = manifest_version(_git(repo, "show", f"{commit}:{MANIFEST}"))
        except ReleaseError:
            version = None
        history.append((commit, version))
    return history


def tag_exists(repo: Path, tag: str) -> bool:
    """Return True when the checkout has the tag (the workflow fetches all tags)."""
    result = subprocess.run(
        ["git", "rev-parse", "--quiet", "--verify", f"refs/tags/{tag}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def create_release(repo: Path, tag: str, commit: str, notes: str, *, latest: bool) -> None:
    """Create the tag on ``commit`` and its release with the gh CLI."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as file:
        file.write(notes)
        notes_path = file.name
    command = [
        "gh",
        "release",
        "create",
        tag,
        "--target",
        commit,
        "--title",
        tag,
        "--notes-file",
        notes_path,
        f"--latest={'true' if latest else 'false'}",
    ]
    if repository := os.environ.get("GITHUB_REPOSITORY"):
        command += ["--repo", repository]
    try:
        result = subprocess.run(command, cwd=repo, check=False)
    finally:
        Path(notes_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise ReleaseError(f"gh release create {tag} mislukte. {RULESET_HINT}")


def versions_to_release(current: str, backfill: Sequence[str] = BACKFILL_VERSIONS) -> list[str]:
    """Return the backfill versions and then the current one, without duplicates."""
    return list(dict.fromkeys([*backfill, current]))


def run(repo: Path, *, dry_run: bool = False) -> int:
    """Create every missing tag and release; return the exit code."""
    current = manifest_version((repo / MANIFEST).read_text(encoding="utf-8"))
    changelog = (repo / CHANGELOG).read_text(encoding="utf-8")
    history = first_parent_history(repo)
    failures = 0
    for version in versions_to_release(current):
        tag = f"{TAG_PREFIX}{version}"
        if tag_exists(repo, tag):
            print(f"{tag}: bestaat al")
            continue
        try:
            commit = release_commit(history, version)
            if commit is None:
                raise ReleaseError(f"geen commit op deze branch met versie {version}")
            notes = changelog_section(changelog, version)
            latest = version == current
            if dry_run:
                print(f"{tag}: zou worden aangemaakt op {commit} (latest: {latest})")
                continue
            create_release(repo, tag, commit, notes, latest=latest)
        except ReleaseError as err:
            print(f"::error::{tag}: {err}")
            failures += 1
            continue
        print(f"{tag}: aangemaakt op {commit}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run."""
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument("--repo", type=Path, default=Path(), help="repository root")
    parser.add_argument("--dry-run", action="store_true", help="only print what would happen")
    args = parser.parse_args(argv)
    try:
        return run(args.repo, dry_run=args.dry_run)
    except ReleaseError as err:
        print(f"::error::{err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
