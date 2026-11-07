from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

import tgharbor

ROOT = Path(__file__).parents[1]
VERSION = "0.2.0"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_LINUX_X64_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"


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


def _ci_text() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _ci_workflow() -> dict:
    return yaml.safe_load(_ci_text())


def _secrets_job_section() -> str:
    text = _ci_text()
    marker = "\n  secrets:\n"
    assert marker in text, "CI must define a secrets job"
    return text.split(marker, 1)[1]


def test_secret_scan_uses_pinned_checksum_verified_gitleaks_cli() -> None:
    text = _ci_text()
    section = _secrets_job_section()

    assert "gitleaks/gitleaks-action" not in text
    assert f'GITLEAKS_VERSION: "{GITLEAKS_VERSION}"' in section
    assert f'GITLEAKS_LINUX_X64_SHA256: "{GITLEAKS_LINUX_X64_SHA256}"' in section
    assert (
        '"https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${asset}"'
    ) in section
    assert '"$RUNNER_TEMP/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"' in section
    assert 'asset="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"' in section
    assert "sha256sum --check --strict -" in section
    assert section.index("sha256sum --check") < section.index("tar --extract")
    assert '"$install_root/gitleaks" version' in section


def test_secret_scan_covers_complete_history_without_event_ranges() -> None:
    job = _ci_workflow()["jobs"]["secrets"]
    checkout = job["steps"][0]

    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}

    text = _ci_text()
    assert "gitleaks git ." in text
    for event_range in (
        "github.event.before",
        "github.event.pull_request",
        "github.event.base",
        "--log-opts",
        "--pre-commit",
        "--staged",
    ):
        assert event_range not in text


def test_secret_scan_redacts_findings_and_distinguishes_exit_codes() -> None:
    section = _secrets_job_section()

    assert "--redact=100" in section
    assert "--no-banner" in section
    assert "--no-color" in section
    assert "--verbose" in section
    assert "--exit-code 2" in section
    assert 'if [ "$status" -eq 2 ]' in section
    assert "operational failure" in section
    assert "exit 1" in section
    assert "exit 2" in section
    assert "git rev-parse --verify --quiet HEAD" in section
    assert "refusing to report a clean scan" in section
    assert "git rev-parse --is-shallow-repository" in section
    assert "complete reachable history is required" in section


def test_secret_scan_stays_least_privilege_and_receives_no_credentials() -> None:
    workflow = _ci_workflow()
    job = workflow["jobs"]["secrets"]

    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in job
    assert "env" not in job
    for step in job["steps"]:
        env = step.get("env") or {}
        assert "GITHUB_TOKEN" not in env
        assert "GITLEAKS_LICENSE" not in env
        assert "GITLEAKS_CONFIG" not in env

    text = _ci_text()
    assert "secrets." not in text
    assert "contents: write" not in text
    assert "security-events" not in text
    assert "id-token" not in text
