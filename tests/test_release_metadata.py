from __future__ import annotations

import copy
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

import tgharbor

ROOT = Path(__file__).parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
VERSION = PROJECT["version"]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_LINUX_X64_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
README_USAGE_SVG = ROOT / "docs/assets/readme-usage.svg"
README_USAGE_MAX_BYTES = 64 * 1024


def test_final_release_version_and_changelog_are_aligned() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert VERSION == "0.2.2"
    assert PROJECT["version"] == VERSION
    assert tgharbor.__version__ == VERSION
    # The 0.2.1 image release shipped metadata without a changelog entry, so 0.2.2 is
    # the first recorded entry after the historical 0.2.0 section.
    assert "## Unreleased\n\n## 0.2.2 - 2026-11-13" in changelog
    assert "## 0.2.0 - 2026-11-07" in changelog
    assert changelog.index("## 0.2.2 - 2026-11-13") < changelog.index("## 0.2.0 - 2026-11-07")
    assert ".dev0" not in changelog


def test_dockerfile_declares_release_oci_metadata_build_arguments() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    expected_arguments = {
        "ARG TGHARBOR_IMAGE_VERSION=0.2.1",
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

    assert "--build-arg TGHARBOR_IMAGE_VERSION=0.2.1" in procedure
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


RELEASE_WORKFLOW = ROOT / ".github/workflows/docker-release.yml"
CHECKOUT_PIN = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
QEMU_PIN = "docker/setup-qemu-action@29109295f81e9208d7d86ff1c6c12d2833863392"
BUILDX_PIN = "docker/setup-buildx-action@e468171a9de216ec08956ac3ada2f0791b6bd435"
LOGIN_PIN = "docker/login-action@9780b0c442fbb1117ed29e0efdff1e18412f7567"
BUILD_PUSH_PIN = "docker/build-push-action@263435318d21b8e681c14492fe198d362a7d2c83"
EXPECTED_ACTION_PINS = {CHECKOUT_PIN, QEMU_PIN, BUILDX_PIN, LOGIN_PIN, BUILD_PUSH_PIN}
SECRET_REFERENCE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


def _release_text() -> str:
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def _release_workflow() -> dict:
    return yaml.safe_load(_release_text())


def _release_triggers() -> dict:
    workflow = _release_workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _publish_job() -> dict:
    return _release_workflow()["jobs"]["publish"]


def _release_step(predicate) -> dict:
    for step in _publish_job()["steps"]:
        if predicate(step):
            return step
    raise AssertionError("release workflow step not found")


def _secret_references(node: object) -> list[str]:
    """Collect every `secrets.NAME` reference from arbitrarily nested workflow data."""
    if isinstance(node, str):
        return SECRET_REFERENCE.findall(node)
    if isinstance(node, dict):
        references: list[str] = []
        for key, value in node.items():
            references.extend(_secret_references(key))
            references.extend(_secret_references(value))
        return references
    if isinstance(node, (list, tuple)):
        references = []
        for item in node:
            references.extend(_secret_references(item))
        return references
    return []


def _assert_validation_isolated_from_publish_secrets(workflow: dict) -> None:
    """Only the environment-scoped publish job may touch secrets, after validation."""
    jobs = workflow["jobs"]
    validate_job = jobs["validate"]
    publish_job = jobs["publish"]

    assert "environment" not in validate_job, "validation must run without an environment"
    assert publish_job.get("environment") == "dockerhub-release"

    needs = publish_job.get("needs")
    needs = {needs} if isinstance(needs, str) else set(needs or ())
    assert "validate" in needs, "publish must depend on validate"

    for name, job in jobs.items():
        references = _secret_references(job)
        if name == "publish":
            assert len(references) == 2, references
            assert set(references) == {"DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"}, references
        else:
            assert references == [], (name, references)


def test_release_workflow_has_only_approved_events_and_dispatch_input() -> None:
    triggers = _release_triggers()

    assert set(triggers) == {"workflow_run", "workflow_dispatch"}
    assert triggers["workflow_run"] == {"workflows": ["CI"], "types": ["completed"]}
    dispatch = triggers["workflow_dispatch"]
    assert dispatch["inputs"]["release_tag"]["required"] is True
    assert dispatch["inputs"]["release_tag"]["type"] == "string"
    text = _release_text()
    assert "workflow_run.event == 'push'" in text
    assert "workflow_run.conclusion == 'success'" in text


def test_release_workflow_uses_least_privilege_and_global_concurrency() -> None:
    workflow = _release_workflow()

    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"] == {
        "group": "shareur/tgharbor",
        "cancel-in-progress": False,
    }
    text = _release_text().lower()
    for forbidden in (
        "contents: write",
        "id-token",
        "packages: write",
        "security-events",
        "pull-requests: write",
        "actions: write",
    ):
        assert forbidden not in text


def test_release_workflow_separates_validation_from_environment_secrets() -> None:
    workflow = _release_workflow()
    validate_job = workflow["jobs"]["validate"]
    publish_job = workflow["jobs"]["publish"]

    assert "environment" not in validate_job
    assert publish_job["environment"] == "dockerhub-release"

    _assert_validation_isolated_from_publish_secrets(workflow)

    text = _release_text()
    assert "secrets.GITHUB_TOKEN" not in text
    assert "${{ github.token }}" in text


def test_release_secret_boundary_detects_workflow_regressions() -> None:
    baseline = copy.deepcopy(_release_workflow())
    _assert_validation_isolated_from_publish_secrets(baseline)

    leaked = copy.deepcopy(baseline)
    leaked["jobs"]["validate"]["steps"][0]["env"] = {
        "DOCKERHUB_TOKEN": "${{ secrets.DOCKERHUB_TOKEN }}"
    }
    with pytest.raises(AssertionError):
        _assert_validation_isolated_from_publish_secrets(leaked)

    unscoped = copy.deepcopy(baseline)
    unscoped["jobs"]["publish"].pop("environment")
    with pytest.raises(AssertionError):
        _assert_validation_isolated_from_publish_secrets(unscoped)

    unlinked = copy.deepcopy(baseline)
    unlinked["jobs"]["publish"].pop("needs")
    with pytest.raises(AssertionError):
        _assert_validation_isolated_from_publish_secrets(unlinked)

    unapproved = copy.deepcopy(baseline)
    unapproved["jobs"]["publish"]["steps"].append(
        {"name": "Exfiltrate", "env": {"TOKEN": "${{ secrets.OTHER_TOKEN }}"}}
    )
    with pytest.raises(AssertionError):
        _assert_validation_isolated_from_publish_secrets(unapproved)


def test_release_workflow_pins_every_third_party_action_to_full_sha() -> None:
    text = _release_text()
    uses = re.findall(r"uses:\s*(\S+)", text)

    assert set(uses) == EXPECTED_ACTION_PINS
    assert uses.count(CHECKOUT_PIN) == 4
    for action in uses:
        assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action), action
    for step in (step for job in _release_workflow()["jobs"].values() for step in job["steps"]):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False


def test_release_build_is_remote_tag_bound_multiplatform_and_attested() -> None:
    build = _release_step(lambda step: step.get("id") == "build")
    with_ = build["with"]

    assert with_["context"] == (
        "https://github.com/shareur/TGHarbor.git#refs/tags/"
        "${{ needs.validate.outputs.release_tag }}"
    )
    assert with_["file"] == "Dockerfile"
    assert with_["platforms"] == "linux/amd64,linux/arm64"
    assert with_["push"] is True
    assert with_["provenance"] == "mode=max"
    assert with_["sbom"] is True
    assert "cache-from" not in with_ and "cache-to" not in with_
    assert "build-contexts" not in with_

    tags = {line.strip() for line in with_["tags"].splitlines() if line.strip()}
    assert tags == {
        "shareur/tgharbor:${{ needs.validate.outputs.release_version }}",
        "shareur/tgharbor:${{ needs.validate.outputs.minor_tag }}",
        "shareur/tgharbor:latest",
        "shareur/tgharbor:${{ needs.validate.outputs.sha_tag }}",
    }
    build_args = {line.strip() for line in with_["build-args"].splitlines() if line.strip()}
    assert build_args == {
        "TGHARBOR_IMAGE_VERSION=${{ needs.validate.outputs.release_version }}",
        "TGHARBOR_IMAGE_REVISION=${{ needs.validate.outputs.release_sha }}",
        "TGHARBOR_IMAGE_SOURCE=https://github.com/shareur/TGHarbor",
        "TGHARBOR_IMAGE_CREATED=${{ needs.validate.outputs.image_created }}",
    }
    assert "DOCKERHUB" not in str(build)
    assert "secrets." not in str(build)


def test_release_workflow_rechecks_tag_and_collisions_before_login() -> None:
    steps = _publish_job()["steps"]
    prepush_index = next(
        index for index, step in enumerate(steps) if "Re-resolve the tag" in step["name"]
    )
    login_index = next(index for index, step in enumerate(steps) if step.get("uses") == LOGIN_PIN)
    verify_index = next(
        index for index, step in enumerate(steps) if "Verify published" in step["name"]
    )
    assert prepush_index < login_index < verify_index
    login = steps[login_index]
    assert login["with"] == {
        "username": "${{ secrets.DOCKERHUB_USERNAME }}",
        "password": "${{ secrets.DOCKERHUB_TOKEN }}",
    }
    assert "pre-push" in str(steps[prepush_index])


def test_release_scripts_never_contain_docker_hub_credentials() -> None:
    for path in (
        ROOT / ".github/scripts/release_contract.py",
        ROOT / ".github/scripts/validate_docker_release.py",
        ROOT / ".github/scripts/verify_docker_release.py",
    ):
        text = path.read_text(encoding="utf-8")
        assert "DOCKERHUB_TOKEN" not in text
        assert "DOCKERHUB_USERNAME" not in text


def test_published_docker_hub_image_is_documented_in_both_languages() -> None:
    documents = (
        ROOT / "README.md",
        ROOT / "docs/zh-CN/README.md",
        ROOT / "docs/3-operations/301-core-downloader-operations.md",
        ROOT / "docs/zh-CN/3-operations/301-core-downloader-operations.md",
        ROOT / "docs/3-operations/302-release-security.md",
        ROOT / "docs/zh-CN/3-operations/302-release-security.md",
        ROOT / "docs/3-operations/303-v0-2-upgrade.md",
        ROOT / "docs/zh-CN/3-operations/303-v0-2-upgrade.md",
    )
    for document in documents:
        text = document.read_text(encoding="utf-8")
        assert "shareur/tgharbor" in text, document
        assert "0.2.1" in text, document

    for readme in (ROOT / "README.md", ROOT / "docs/zh-CN/README.md"):
        text = readme.read_text(encoding="utf-8")
        assert "https://hub.docker.com/r/shareur/tgharbor" in text, readme
        assert "linux/amd64" in text, readme
        assert "linux/arm64" in text, readme
        assert "sha-13c5bc7645b9" in text, readme
        assert "sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560" in text, (
            readme
        )

    release_security = (ROOT / "docs/3-operations/302-release-security.md").read_text(
        encoding="utf-8"
    )
    for token in (
        "v0.2.1",
        "13c5bc7645b9",
        "provenance",
        "SBOM",
        "latest",
        "sha-",
        "https://hub.docker.com/r/shareur/tgharbor",
        "linux/amd64",
        "linux/arm64",
        "sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560",
        "org.opencontainers.image.version=0.2.1",
        "org.opencontainers.image.revision=13c5bc7645b9ba27ea9837be3697fa1388ffb44d",
        "org.opencontainers.image.source=https://github.com/shareur/TGHarbor",
    ):
        assert token in release_security

    operations = (ROOT / "docs/3-operations/301-core-downloader-operations.md").read_text(
        encoding="utf-8"
    )
    assert "docker compose pull" in operations
    assert "docker compose run --rm tgharbor login" in operations
    assert "docker compose up -d" in operations
    assert "docker compose logs -f tgharbor" in operations
    assert re.search(r"entrypoint is\s+`tgharbor`", operations)
    assert re.search(r"default command is\s+`run`", operations)
    assert "/config/config.yaml" in operations
    assert "/data/tgharbor.db" in operations
    assert "/downloads" in operations


def test_readmes_keep_secure_docker_quick_start_permissions() -> None:
    for readme in (ROOT / "README.md", ROOT / "docs/zh-CN/README.md"):
        text = readme.read_text(encoding="utf-8")
        assert "chmod 600 .env" in text, readme
        assert "chmod 644 config.yaml" in text, readme
        assert "chmod 600 .env config.yaml" not in text, readme


def test_operations_documents_keep_env_restricted_and_config_container_readable() -> None:
    documents = (
        ROOT / "docs/3-operations/301-core-downloader-operations.md",
        ROOT / "docs/zh-CN/3-operations/301-core-downloader-operations.md",
    )
    for document in documents:
        text = document.read_text(encoding="utf-8")
        assert "chmod 600 .env config.yaml" not in text, document
        assert "chmod 600 .env" in text, document
        assert "chmod 644 config.yaml" in text, document
        assert "sudo chgrp 10001 config.yaml" in text, document
        assert "chmod 640 config.yaml" in text, document
        assert "already in group" not in text, document
        assert "已属于" not in text, document
        assert "/config/config.yaml" in text, document
        assert "10001" in text, document


def test_active_deployment_docs_use_the_published_image_tag() -> None:
    documents = (
        ROOT / "README.md",
        ROOT / "docs/zh-CN/README.md",
        ROOT / "docs/3-operations/301-core-downloader-operations.md",
        ROOT / "docs/zh-CN/3-operations/301-core-downloader-operations.md",
        ROOT / "docs/3-operations/302-release-security.md",
        ROOT / "docs/zh-CN/3-operations/302-release-security.md",
        ROOT / "docs/3-operations/303-v0-2-upgrade.md",
        ROOT / "docs/zh-CN/3-operations/303-v0-2-upgrade.md",
    )
    for document in documents:
        text = document.read_text(encoding="utf-8")
        assert "shareur/tgharbor:0.2.0" not in text, document
        assert "tgharbor:0.2.0" not in text, document
        # 0.2.2 is a source-only release; no container image carries that tag, so
        # active deployment documents must never reference one.
        assert "tgharbor:0.2.2" not in text, document


def _readme_section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = f"## {heading}\n"
    assert marker in text, (path, heading)
    return text.split(marker, 1)[1].split("\n## ", 1)[0]


def _bullet_blocks(section: str) -> list[str]:
    return [block for block in re.split(r"\n(?=- )", section.strip()) if block.startswith("- ")]


def test_readmes_advertise_save_restricted_media_with_access_boundary() -> None:
    english = _readme_section(ROOT / "README.md", "Features")
    chinese = _readme_section(ROOT / "docs/zh-CN/README.md", "主要功能")

    english_bullets = _bullet_blocks(english)
    chinese_bullets = _bullet_blocks(chinese)
    assert english_bullets and chinese_bullets
    assert "MTProto" in english_bullets[0]
    assert "save/forward restricted" in english_bullets[0].lower()
    assert "MTProto" in chinese_bullets[0]
    for token in ("禁止保存", "禁止转发", "无法转载"):
        assert token in chinese_bullets[0], token

    for section, tokens in (
        (english, ("authorized Telegram account session", "permissions", "cannot access")),
        (chinese, ("已授权", "绕过", "无法访问")),
    ):
        for token in tokens:
            assert token in section, (token, section)


def test_feature_overviews_keep_mtproto_save_restriction_access_boundary() -> None:
    english = (ROOT / "docs/0-foundation/002-feature-overview.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs/zh-CN/0-foundation/002-feature-overview.md").read_text(encoding="utf-8")

    for text, tokens in (
        (english, ("MTProto", "save/forward restricted", "does not grant new access")),
        (chinese, ("MTProto", "禁止保存", "禁止转发", "无法转载", "不会授予新权限")),
    ):
        for token in tokens:
            assert token in text, token


def _svg_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _readme_usage_svg() -> tuple[ET.Element, str]:
    raw = README_USAGE_SVG.read_text(encoding="utf-8")
    return ET.fromstring(raw), raw


def _balanced_css_end(text: str, open_brace: int) -> int:
    assert text[open_brace] == "{"
    depth = 0
    for index in range(open_brace, len(text)):
        character = text[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unbalanced CSS braces")


def _css_block_body(text: str, header_pattern: str) -> tuple[int, int, str]:
    match = re.search(header_pattern, text, re.IGNORECASE)
    assert match is not None, f"CSS block not found: {header_pattern}"
    open_brace = text.find("{", match.start())
    assert open_brace != -1, header_pattern
    close_brace = _balanced_css_end(text, open_brace)
    return match.start(), close_brace + 1, text[open_brace + 1 : close_brace]


def _css_value(css: str, selector: str, property_name: str) -> str:
    pattern = re.compile(
        rf"{re.escape(selector)}\s*\{{[^{{}}]*?\b{re.escape(property_name)}\s*:\s*"
        r"(#[0-9a-fA-F]{6})\b"
    )
    match = pattern.search(css)
    assert match is not None, (selector, property_name)
    return match.group(1).lower()


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    foreground_luminance = _relative_luminance(foreground)
    background_luminance = _relative_luminance(background)
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _numbered_section_items(section: str) -> dict[str, str]:
    matches = list(re.finditer(r"^(\d+)\.\s+", section, re.MULTILINE))
    items: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        body = section[match.end() : end].strip()
        items[match.group(1)] = body.split("\n\n", 1)[0].strip()
    return items


def _section_image(section: str, expected_target: str) -> str:
    matches = re.findall(r"!\[([^\]]+)\]\(([^)]+)\)", section)
    assert len(matches) == 1, matches
    alt, target = matches[0]
    assert target == expected_target, target
    return alt


def test_readme_usage_svg_is_safe_accessible_and_within_size_limit() -> None:
    root, raw = _readme_usage_svg()
    assert README_USAGE_SVG.stat().st_size <= README_USAGE_MAX_BYTES

    assert root.tag == f"{{{SVG_NAMESPACE}}}svg"
    assert root.get("width") == "960"
    assert root.get("height") == "360"
    assert root.get("viewBox") == "0 0 960 360"

    title = root.find(f"{{{SVG_NAMESPACE}}}title")
    description = root.find(f"{{{SVG_NAMESPACE}}}desc")
    assert title is not None and (title.text or "").strip()
    assert description is not None
    description_text = " ".join((description.text or "").split()).lower()
    assert "request acceptance" in description_text
    assert "not a completed download" in description_text

    lowered = raw.lower()
    for forbidden in (
        "<!doctype",
        "<!entity",
        "javascript:",
        "vbscript:",
        "data:",
        "@import",
        "@font-face",
        "url(",
        "image-set(",
        "expression(",
    ):
        assert forbidden not in lowered, forbidden
    for tag in ("script", "foreignobject", "iframe", "object", "embed", "image", "use"):
        assert not re.search(rf"<{tag}[\s>]", lowered), tag
    assert not re.search(r"<a[\s>]", lowered)

    allowed_tags = {
        "svg",
        "title",
        "desc",
        "style",
        "rect",
        "g",
        "circle",
        "path",
        "line",
        "text",
    }
    for element in root.iter():
        tag = _svg_local_name(element.tag)
        assert tag in allowed_tags, tag
        for attribute, value in element.attrib.items():
            name = _svg_local_name(attribute)
            assert not name.startswith("on"), (tag, attribute)
            assert name != "href", (tag, attribute)
            lowered_value = value.lower()
            for forbidden_reference in (
                "http://",
                "https://",
                "data:",
                "ftp:",
                "mailto:",
                "javascript:",
                "vbscript:",
                "//",
            ):
                assert forbidden_reference not in lowered_value, (tag, attribute, value)

    style = root.find(f"{{{SVG_NAMESPACE}}}style")
    assert style is not None and style.text
    style_text = style.text.lower()
    for forbidden in ("@import", "url(", "image-set(", "@font-face", "expression("):
        assert forbidden not in style_text, forbidden


def test_readme_usage_svg_keeps_both_lanes_and_reduced_motion_safe_animation() -> None:
    root, raw = _readme_usage_svg()
    lanes = {
        element.get("id"): element
        for element in root.findall(f"{{{SVG_NAMESPACE}}}g")
        if element.get("id")
    }
    assert {"lane-one", "lane-two"} <= set(lanes)

    lane_one_text = " ".join("".join(lanes["lane-one"].itertext()).split())
    lane_two_text = " ".join("".join(lanes["lane-two"].itertext()).split())
    assert "video.mp4" in lane_one_text
    assert "TGHarbor Bot" in lane_one_text
    assert "https://t.me" not in lane_one_text
    assert "video.mp4" in lane_two_text
    assert "TGHarbor Bot" in lane_two_text
    assert "https://t.me/…/42" in lane_two_text

    assert root.find(f".//{{{SVG_NAMESPACE}}}path[@id='lane-one-check-mark']") is not None
    assert root.find(f".//{{{SVG_NAMESPACE}}}path[@id='lane-two-check-mark']") is not None

    lowered = raw.lower()
    assert 'display="none"' not in lowered
    assert 'visibility="hidden"' not in lowered
    for lane in lanes.values():
        assert lane.get("opacity") is None

    style = root.find(f"{{{SVG_NAMESPACE}}}style")
    assert style is not None and style.text
    style_text = style.text
    gate_start, gate_end, animated_css = _css_block_body(
        style_text, r"@media\s*\(prefers-reduced-motion:\s*no-preference\)\s*\{"
    )
    assert "animation:" in animated_css
    assert "@keyframes" in animated_css
    assert ".lane-one-highlight" in animated_css
    assert ".lane-two-highlight" in animated_css

    animation_declarations = re.compile(r"(?:^|[{;\s])animation(?:-[a-z-]+)?\s*:", re.IGNORECASE)
    declarations = list(animation_declarations.finditer(style_text))
    assert declarations, "animation declarations must exist"
    for declaration in declarations:
        assert gate_start <= declaration.start() < gate_end, (
            "animation declarations must stay inside the reduced-motion media block"
        )
    outside_animation_gate = style_text[:gate_start] + style_text[gate_end:]
    assert not re.search(r"@keyframes", outside_animation_gate, re.IGNORECASE)

    forbidden_motion = re.compile(
        r"(?i)\b(transform|translate|rotate|scale|skew|offset|position|top|right|bottom|left|"
        r"margin(?:-[a-z-]+)?)\b\s*:"
    )
    assert not forbidden_motion.search(animated_css), "motion must be opacity-only"

    keyframe_headers = list(re.finditer(r"@keyframes\s+[\w-]+\s*\{", animated_css, re.IGNORECASE))
    assert len(keyframe_headers) >= 4
    for keyframe in keyframe_headers:
        open_brace = keyframe.end() - 1
        close_brace = _balanced_css_end(animated_css, open_brace)
        properties = {
            name.lower()
            for name in re.findall(
                r"(?:^|[;{]\s*)([a-z-]+)\s*:",
                animated_css[open_brace + 1 : close_brace],
                re.IGNORECASE,
            )
        }
        assert properties
        assert properties == {"opacity"}, properties


def test_readme_usage_svg_check_marks_meet_non_text_contrast() -> None:
    root, raw = _readme_usage_svg()
    style = root.find(f"{{{SVG_NAMESPACE}}}style")
    assert style is not None and style.text
    style_text = style.text
    light_start, _, light_css = _css_block_body(
        style_text, r"@media\s*\(prefers-color-scheme:\s*light\)\s*\{"
    )
    palettes = {"dark": style_text[:light_start], "light": light_css}

    for mark_id in ("lane-one-check-mark", "lane-two-check-mark"):
        mark = root.find(f".//{{{SVG_NAMESPACE}}}path[@id='{mark_id}']")
        assert mark is not None
        assert "check-mark" in (mark.get("class") or "").split(), mark_id
    assert raw.count('class="check-mark"') == 2

    palette_values = {}
    for palette, css in palettes.items():
        mark_color = _css_value(css, ".check-mark", "stroke")
        background_color = _css_value(css, ".soft-ok", "fill")
        palette_values[palette] = (mark_color, background_color)
        ratio = _contrast_ratio(mark_color, background_color)
        assert ratio >= 3.0, (palette, mark_color, background_color, ratio)

    dark_mark, dark_background = palette_values["dark"]
    light_mark, light_background = palette_values["light"]
    assert dark_mark != light_mark
    assert _relative_luminance(dark_mark) > _relative_luminance(dark_background)
    assert _relative_luminance(light_mark) < _relative_luminance(light_background)


def test_readmes_document_bilingual_usage_paths_and_access_boundary() -> None:
    english_readme = ROOT / "README.md"
    chinese_readme = ROOT / "docs/zh-CN/README.md"
    english_text = english_readme.read_text(encoding="utf-8")
    chinese_text = chinese_readme.read_text(encoding="utf-8")

    assert (
        english_text.index("## Features")
        < english_text.index("## Usage")
        < english_text.index("## Quick start")
    )
    assert (
        chinese_text.index("## 主要功能")
        < chinese_text.index("## 使用方法")
        < chinese_text.index("## 开发")
    )

    english = _readme_section(english_readme, "Usage")
    chinese = _readme_section(chinese_readme, "使用方法")

    english_alt = _section_image(english, "docs/assets/readme-usage.svg")
    chinese_alt = _section_image(chinese, "../assets/readme-usage.svg")
    assert len(english_alt) >= 80
    assert len(chinese_alt) >= 40
    for token in (
        "forward",
        "link",
        "private",
        "TGHarbor Bot",
        "request acceptance",
        "green",
        "not a completed download",
    ):
        assert token.lower() in english_alt.lower(), token
    for token in ("转发", "链接", "TGHarbor Bot", "请求已接受"):
        assert token in chinese_alt, token

    english_asset = (english_readme.parent / "docs/assets/readme-usage.svg").resolve()
    chinese_asset = (chinese_readme.parent / "../assets/readme-usage.svg").resolve()
    assert english_asset == chinese_asset == README_USAGE_SVG.resolve()
    assert not (ROOT / "docs/assets/readme-usage-zh.svg").exists()

    english_items = _numbered_section_items(english)
    chinese_items = _numbered_section_items(chinese)
    assert list(english_items) == ["1", "2"]
    assert list(chinese_items) == ["1", "2"]

    english_first = english_items["1"].lower()
    english_second = english_items["2"].lower()
    chinese_first = chinese_items["1"]
    chinese_second = chinese_items["2"]

    assert "forward" in english_first and "one telegram message" in english_first
    assert "file" in english_first and "http" not in english_first
    assert "copy" in english_second and "single" in english_second and "only" in english_second
    assert "https://t.me" in english_second
    assert "转发" in chinese_first and "一条" in chinese_first
    assert "文件" in chinese_first and "http" not in chinese_first
    assert "复制" in chinese_second and "单条" in chinese_second and "只" in chinese_second
    assert "https://t.me" in chinese_second
    for section in (english, chinese):
        assert "媒体" in section or "media" in section.lower()

    for section, tokens in (
        (
            english,
            (
                "configured authorized users",
                "already-authorized",
                "Telegram user session",
                "already accesses",
                "does not bypass",
            ),
        ),
        (
            chinese,
            (
                "配置中授权的用户",
                "已授权",
                "用户会话",
                "本来就能访问",
                "不会绕过",
            ),
        ),
    ):
        lowered = section.lower()
        for token in tokens:
            assert token.lower() in lowered, token
