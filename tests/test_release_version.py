"""Tests for the release version rules (no network, no Docker).

Background: a release is made by pushing a git tag such as "v0.3.0". The
GitHub workflow then builds the Docker image and labels it with that
version. Two things must never drift apart:
  * the git tag and the version in pyproject.toml (the workflow refuses to
    release if they differ: scripts/check_release_version.py);
  * pyproject.toml and what the running code reports as __version__ (it is
    read from the installed package's metadata, not typed in a second time).
"""

import importlib.util
import pathlib
import tomllib

import pytest

import plane_pages_mcp

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_check_script():
    # The script lives in scripts/, not in the package, so load it by path.
    spec = importlib.util.spec_from_file_location(
        "check_release_version", ROOT / "scripts" / "check_release_version.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load_check_script()


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_version_reported_by_the_code_matches_pyproject():
    # Checks: plane_pages_mcp.__version__ equals the version in pyproject.toml.
    # Why: there used to be a second, hand-typed copy in __init__.py, and it
    # said 0.1.0 while v0.2.3 was in production. It now comes from the
    # installed package's metadata, which pyproject.toml writes. If this
    # fails locally after a version bump, reinstall the package
    # (pip install -e .).
    assert plane_pages_mcp.__version__ == _pyproject_version()


def test_pyproject_version_is_plain_semver():
    # Checks: the version in pyproject.toml looks like X.Y.Z (digits only).
    # Why: the release tag must be exactly "v" + this version. A version like
    # "0.3" or "0.3.0rc1" could never match a v*.*.* tag, so every release
    # would be refused.
    assert check.SEMVER.fullmatch(_pyproject_version())


@pytest.mark.parametrize("tag", ["v0.3.0", "refs/tags/v0.3.0"])
def test_matching_tag_is_accepted(tag):
    # Checks: the tag "v0.3.0" (or the full ref "refs/tags/v0.3.0") matches
    # version 0.3.0, so check() returns no error.
    # Why: this is the normal release; it must go through.
    assert check.check(tag, "0.3.0") is None


@pytest.mark.parametrize("tag, version", [
    ("v0.3.1", "0.3.0"),   # forgot to bump pyproject.toml
    ("v0.3.0", "0.2.3"),   # tagged the wrong commit
    ("0.3.0", "0.3.0"),    # tag without the "v"
    ("v0.3", "0.3.0"),     # incomplete tag
    ("v0.3.0-rc1", "0.3.0"),
])
def test_mismatched_or_malformed_tag_is_refused(tag, version):
    # Checks: any tag that isn't exactly "v" + the pyproject version is
    # refused with a message that shows both values.
    # Why: otherwise an image labelled "v0.3.1" could contain 0.3.0's code
    # (or the reverse). Nobody could then trust the tag on the running
    # container.
    error = check.check(tag, version)
    assert error is not None and version in error


def test_script_exit_code(tmp_path):
    # Checks: run as a program, the script exits 0 for a matching tag and 1
    # (printing the reason) for a mismatch, reading the version from a given
    # pyproject.toml.
    # Why: the GitHub workflow relies on the exit code. A non-zero exit is
    # what stops the release before anything is built or pushed.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    assert check.main(["v1.2.3", str(pyproject)]) == 0
    assert check.main(["v1.2.4", str(pyproject)]) == 1
