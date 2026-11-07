from __future__ import annotations

import tomllib
from pathlib import Path

import tgharbor

ROOT = Path(__file__).parents[1]
VERSION = "0.2.0"


def test_final_release_version_and_changelog_are_aligned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert project["version"] == VERSION
    assert tgharbor.__version__ == VERSION
    assert "## Unreleased\n\n## 0.2.0 - 2026-11-07" in changelog
    assert ".dev0" not in changelog


def test_dockerfile_declares_release_oci_metadata_build_arguments() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    expected_arguments = {
        "ARG TGHARBOR_IMAGE_VERSION=0.2.0",
        "ARG TGHARBOR_IMAGE_REVISION=uncommitted",
        "ARG TGHARBOR_IMAGE_SOURCE=local",
        "ARG TGHARBOR_IMAGE_CREATED=1970-01-01T00:00:00Z",
    }
    assert expected_arguments <= set(dockerfile.splitlines())

    expected_labels = {
        'org.opencontainers.image.title="TGHarbor"',
        'org.opencontainers.image.version="${TGHARBOR_IMAGE_VERSION}"',
        'org.opencontainers.image.revision="${TGHARBOR_IMAGE_REVISION}"',
        'org.opencontainers.image.source="${TGHARBOR_IMAGE_SOURCE}"',
        'org.opencontainers.image.licenses="Apache-2.0"',
        'org.opencontainers.image.created="${TGHARBOR_IMAGE_CREATED}"',
    }
    assert all(label in dockerfile for label in expected_labels)
    assert dockerfile.index("ARG TGHARBOR_IMAGE_VERSION") < dockerfile.index("LABEL ")


def test_dockerfile_preserves_non_root_runtime_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "USER tgharbor" in dockerfile
    assert 'ENTRYPOINT ["tgharbor"]' in dockerfile
    assert 'CMD ["run"]' in dockerfile


def test_release_procedure_requires_candidate_provenance_overrides() -> None:
    procedure = (ROOT / "docs/3-operations/302-release-security.md").read_text(encoding="utf-8")

    assert "--build-arg TGHARBOR_IMAGE_VERSION=0.2.0" in procedure
    assert '--build-arg TGHARBOR_IMAGE_REVISION="$CANDIDATE_COMMIT"' in procedure
    assert '--build-arg TGHARBOR_IMAGE_SOURCE="$TGHARBOR_IMAGE_SOURCE"' in procedure
    assert '--build-arg TGHARBOR_IMAGE_CREATED="$TGHARBOR_IMAGE_CREATED"' in procedure
    assert "Those revision, source, and created defaults identify a non-release" in procedure
