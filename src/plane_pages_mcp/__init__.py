"""plane_pages_mcp — MCP server for Plane CE Pages (read/write via Postgres + live converter)."""

from importlib.metadata import PackageNotFoundError, version

# The one source of the version is pyproject.toml. Installing the package
# (pip install ., as the Dockerfile does) writes it into the package metadata,
# and we read it from there instead of keeping a second copy that can drift.
try:
    __version__ = version("plane_pages_mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
