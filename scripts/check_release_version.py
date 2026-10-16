"""Refuse a release whose git tag doesn't match pyproject.toml's version.

Used by .github/workflows/ci.yml before building the Docker image:

    python scripts/check_release_version.py "$GITHUB_REF_NAME"

Exit 0 when the tag is exactly "v" + the version (v0.3.0 <-> 0.3.0), and 1
with an explanation otherwise. That way the image tag, the image's version
label, and the code's __version__ always agree.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib

SEMVER = re.compile(r"\d+\.\d+\.\d+")
DEFAULT_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def read_version(pyproject: pathlib.Path) -> str:
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def check(tag: str, version: str) -> str | None:
    """None if ``tag`` releases ``version``, else the reason it doesn't."""
    tag = tag.removeprefix("refs/tags/")
    expected = f"v{version}"
    if not SEMVER.fullmatch(version):
        return f"pyproject.toml version {version!r} is not plain X.Y.Z"
    if tag != expected:
        return (
            f"tag {tag!r} does not match pyproject.toml version {version!r} "
            f"(expected tag {expected!r}). Bump the version in pyproject.toml "
            "and tag that commit, or delete and redo the tag."
        )
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_release_version.py <tag> [pyproject.toml]", file=sys.stderr)
        return 2
    pyproject = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_PYPROJECT
    version = read_version(pyproject)
    error = check(argv[0], version)
    if error:
        print(f"release refused: {error}", file=sys.stderr)
        return 1
    print(f"release ok: tag {argv[0]} matches version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
