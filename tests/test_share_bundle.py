"""Verify that the review archive excludes private and generated files."""

from zipfile import ZipFile

import pytest

from scripts.create_share_bundle import ARCHIVE_ROOT, SHARE_FILES, create_bundle


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    for name in SHARE_FILES:
        source = root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"Example source: {name}\n", encoding="utf-8")
    return root


def test_bundle_contains_only_explicit_sources(project, tmp_path):
    private_names = (
        ".env",
        ".state/browser.json",
        "refresh_config.json",
        "work/capture.har",
        ".venv/private.txt",
        "docs/private-notes.md",
    )
    for name in private_names:
        private = project / name
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("private-sentinel", encoding="utf-8")

    output = create_bundle(project, tmp_path / "dist" / "review.zip")

    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            f"{ARCHIVE_ROOT}/{name}" for name in SHARE_FILES
        }
        assert all(b"private-sentinel" not in archive.read(name) for name in archive.namelist())
        assert archive.testzip() is None


def test_missing_source_does_not_replace_existing_archive(project, tmp_path):
    (project / "README.md").unlink()
    output = tmp_path / "review.zip"
    output.write_bytes(b"existing-archive")

    with pytest.raises(ValueError, match="README.md"):
        create_bundle(project, output)

    assert output.read_bytes() == b"existing-archive"


def test_output_cannot_replace_a_source(project):
    readme = project / "README.md"
    original = readme.read_bytes()

    with pytest.raises(ValueError, match="cannot replace"):
        create_bundle(project, readme)

    assert readme.read_bytes() == original


def test_output_cannot_replace_private_configuration(project):
    private = project / ".env"
    private.write_text("private-sentinel", encoding="utf-8")

    with pytest.raises(ValueError, match=".zip extension"):
        create_bundle(project, private)

    assert private.read_text(encoding="utf-8") == "private-sentinel"
