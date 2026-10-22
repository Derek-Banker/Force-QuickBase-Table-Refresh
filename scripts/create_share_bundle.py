"""Build a review archive from an explicit list of project files."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ARCHIVE_ROOT = "quickbase-table-refresh"
SHARE_FILES = (
    ".env.example",
    ".gitignore",
    "README.md",
    "api_server.py",
    "qb_refresh.py",
    "pyproject.toml",
    "refresh_config.example.json",
    "requirements.txt",
    "requirements-api.txt",
    "requirements-dev.txt",
    "requirements-tested.txt",
    "docs/architecture.md",
    "docs/reference.md",
    "scripts/create_share_bundle.py",
    "tests/conftest.py",
    "tests/test_api.py",
    "tests/test_browser.py",
    "tests/test_refresh.py",
    "tests/test_share_bundle.py",
)


def create_bundle(project_root: Path, output: Path) -> Path:
    """Include only named source files. Reject missing files and external links."""
    project_root = project_root.resolve()
    sources = []
    for name in SHARE_FILES:
        source = project_root / name
        if not source.resolve().is_relative_to(project_root):
            raise ValueError(f"The share source is outside the project: {name}")
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"The share source is missing or is a symbolic link: {name}")
        sources.append((name, source))

    output = output.resolve()
    if output in {source.resolve() for _, source in sources}:
        raise ValueError("The archive cannot replace a shared source file.")
    if output.suffix.lower() != ".zip":
        raise ValueError("The output path must use the .zip extension.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, source in sources:
            archive.writestr(f"{ARCHIVE_ROOT}/{name}", source.read_bytes())
    return output


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "dist" / "quickbase-table-refresh.zip",
        help="Output archive path. The default is dist/quickbase-table-refresh.zip.",
    )
    args = parser.parse_args()
    try:
        output = create_bundle(project_root, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot create the share archive: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
