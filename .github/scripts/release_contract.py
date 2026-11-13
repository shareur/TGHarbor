"""Shared, testable contracts for the TGHarbor Docker Hub release workflow.

The release automation must never derive identity from a mutable workflow
checkout or from local build context. These helpers centralise the project
source repository, image, tag, and output-file rules so the validation and
post-push verification scripts enforce the same boundary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CANONICAL_REPOSITORY = "shareur/TGHarbor"
CANONICAL_REPOSITORY_OWNER = "shareur"
CANONICAL_REPOSITORY_NAME = "TGHarbor"
CANONICAL_DEFAULT_BRANCH = "master"
CANONICAL_IMAGE = "shareur/tgharbor"
CANONICAL_SOURCE_URL = "https://github.com/shareur/TGHarbor"
CI_WORKFLOW_NAME = "CI"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
LATEST_TAG = "latest"
DOCKER_HUB_API_ROOT = "https://hub.docker.com/v2/repositories/shareur/tgharbor/tags"
GITHUB_API_ROOT = "https://api.github.com"
DOCKER_HUB_REGISTRY = "https://registry-1.docker.io"
DOCKER_HUB_AUTH = "https://auth.docker.io/token"

STABLE_TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
FULL_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
OUTPUT_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
IMAGE_TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


class ContractError(ValueError):
    """Raised when a release input violates a security contract."""


@dataclass(frozen=True)
class SemVer:
    """A strict stable major.minor.patch release selected by a ``v`` tag."""

    major: int
    minor: int
    patch: int

    @property
    def version(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def minor_version(self) -> str:
        return f"{self.major}.{self.minor}"


@dataclass(frozen=True)
class ReleaseMetadata:
    """Immutable release identity derived from the tag and its commit."""

    tag: str
    version: str
    minor_version: str
    sha: str
    created: str
    source: str = CANONICAL_SOURCE_URL

    @property
    def sha_tag(self) -> str:
        return f"sha-{self.sha[:12]}"

    @property
    def image_tags(self) -> tuple[str, str, str, str]:
        return (self.version, self.minor_version, LATEST_TAG, self.sha_tag)

    def image_references(self, image: str = CANONICAL_IMAGE) -> tuple[str, ...]:
        return tuple(f"{image}:{tag}" for tag in self.image_tags)


def parse_stable_tag(value: object) -> SemVer:
    """Accept only ``vMAJOR.MINOR.PATCH`` with no prefixes, suffixes, or spaces."""

    if not isinstance(value, str):
        raise ContractError("release tag must be a string")
    match = STABLE_TAG_PATTERN.fullmatch(value)
    if match is None:
        raise ContractError("release tag must be a stable annotated tag in vMAJOR.MINOR.PATCH form")
    major, minor, patch = (int(component) for component in match.groups())
    return SemVer(major=major, minor=minor, patch=patch)


def require_full_sha(value: object, *, label: str = "commit") -> str:
    """Require a full, lowercase, 40-character Git object ID."""

    if not isinstance(value, str) or FULL_SHA_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{label} must be a full 40-character lowercase commit SHA")
    return value


def require_digest(value: object, *, label: str = "digest") -> str:
    """Require a full lowercase ``sha256:`` digest."""

    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{label} must be a full lowercase sha256 digest")
    return value


def require_canonical_repository(value: object, *, label: str = "repository") -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a string")
    if value.casefold() != CANONICAL_REPOSITORY.casefold():
        raise ContractError(f"{label} must be {CANONICAL_REPOSITORY}")
    return CANONICAL_REPOSITORY


def require_canonical_image(value: object) -> str:
    if not isinstance(value, str) or value.casefold() != CANONICAL_IMAGE:
        raise ContractError(f"image repository must be {CANONICAL_IMAGE}")
    return CANONICAL_IMAGE


def require_image_tag(value: object) -> str:
    if not isinstance(value, str) or IMAGE_TAG_PATTERN.fullmatch(value) is None:
        raise ContractError("image tag contains unsupported characters")
    if value in {".", ".."}:
        raise ContractError("image tag must not be a path component")
    return value


def deterministic_created(commit_epoch: object) -> str:
    """Return the commit timestamp as deterministic RFC 3339 UTC."""

    if isinstance(commit_epoch, bool) or not isinstance(commit_epoch, int):
        raise ContractError("commit timestamp must be an integer")
    if commit_epoch <= 0:
        raise ContractError("commit timestamp must be positive")
    try:
        timestamp = datetime.fromtimestamp(commit_epoch, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ContractError("commit timestamp is outside the supported range") from exc
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_release_metadata(tag: object, sha: object, commit_epoch: object) -> ReleaseMetadata:
    semver = parse_stable_tag(tag)
    commit = require_full_sha(sha, label="release commit")
    created = deterministic_created(commit_epoch)
    return ReleaseMetadata(
        tag=tag,  # type: ignore[arg-type]
        version=semver.version,
        minor_version=semver.minor_version,
        sha=commit,
        created=created,
    )


def canonical_image_reference(tag: object, image: object = CANONICAL_IMAGE) -> str:
    repository = require_canonical_image(image)
    return f"{repository}:{require_image_tag(tag)}"


def _safe_output_value(value: object) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, str)):
        rendered = str(value)
    else:
        raise ContractError("GitHub output values must be strings, integers, or booleans")
    if any(character in rendered for character in ("\x00", "\r", "\n")):
        raise ContractError("GitHub output values must not contain control newlines")
    return rendered


def write_github_outputs(path: object, values: Mapping[str, object]) -> None:
    """Append single-line ``key=value`` entries without allowing output injection."""

    if not isinstance(values, Mapping):
        raise ContractError("GitHub outputs must be a mapping")
    rendered: list[tuple[str, str]] = []
    for key, value in values.items():
        if not isinstance(key, str) or OUTPUT_KEY_PATTERN.fullmatch(key) is None:
            raise ContractError("GitHub output keys must be shell-safe identifiers")
        rendered.append((key, _safe_output_value(value)))
    if not rendered:
        return
    output_path = Path(path)
    with output_path.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in rendered:
            handle.write(f"{key}={value}\n")
