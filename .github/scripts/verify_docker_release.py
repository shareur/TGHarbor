"""Post-push verification for a TGHarbor Docker Hub release.

The verifier reads only public registry metadata, image configuration, and image
layer archives. It never authenticates to Docker Hub, never prints layer content,
and fails closed when a required digest, label, attestation, safe layer member,
or isolated runtime probe cannot be proven.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from publication_policy import (
    EMPTY_POLICY,
    ForbiddenPathPolicy,
    PolicyError,
    policy_from_environment,
)
from release_contract import (
    CANONICAL_IMAGE,
    CANONICAL_SOURCE_URL,
    DOCKER_HUB_AUTH,
    DOCKER_HUB_REGISTRY,
    ContractError,
    ReleaseMetadata,
    parse_stable_tag,
    require_canonical_image,
    require_digest,
    require_full_sha,
    require_image_tag,
    write_github_outputs,
)

RUNTIME_PLATFORMS = ("linux/amd64", "linux/arm64")
INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
ATTESTATION_TYPE_ANNOTATION = "vnd.docker.reference.type"
ATTESTATION_DIGEST_ANNOTATION = "vnd.docker.reference.digest"
ATTESTATION_MANIFEST_TYPE = "attestation-manifest"
IN_TOTO_PREDICATE_ANNOTATION = "in-toto.io/predicate-type"
PROVENANCE_PREFIX = "https://slsa.dev/provenance/"
SBOM_MARKERS = ("https://spdx.dev/Document", "https://cyclonedx.org/bom")
CANONICAL_SOURCE_PATTERN = re.compile(
    r"(?:git\+)?https://github\.com/shareur/TGHarbor(?:\.git)?(?:#.*)?"
)
CREATED_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
MAX_REGISTRY_BODY_BYTES = 32_000_000
MAX_MANIFEST_BYTES = 4_000_000
OCI_TAR_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_GZIP_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
DOCKER_TAR_LAYER_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar"
DOCKER_GZIP_LAYER_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"
UNCOMPRESSED_LAYER_MEDIA_TYPES = frozenset({OCI_TAR_LAYER_MEDIA_TYPE, DOCKER_TAR_LAYER_MEDIA_TYPE})
GZIP_LAYER_MEDIA_TYPES = frozenset({OCI_GZIP_LAYER_MEDIA_TYPE, DOCKER_GZIP_LAYER_MEDIA_TYPE})
SUPPORTED_LAYER_MEDIA_TYPES = UNCOMPRESSED_LAYER_MEDIA_TYPES | GZIP_LAYER_MEDIA_TYPES
GZIP_MAGIC = b"\x1f\x8b"
FORBIDDEN_BASENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "config.yaml",
        "config.yml",
        "compose.yaml",
        "compose.yml",
        "compose.override.yaml",
        "compose.local.yaml",
    }
)
FORBIDDEN_DIRECTORY_NAMES = frozenset({".git", "docs", "tests"})
FORBIDDEN_RELATIVE_PATHS = frozenset()
FORBIDDEN_SUFFIXES = (".session", ".session-journal", ".db", ".sqlite", ".sqlite3", ".log")
_FORBIDDEN_BASENAMES_CASEFOLD = frozenset(name.casefold() for name in FORBIDDEN_BASENAMES)
_FORBIDDEN_DIRECTORIES_CASEFOLD = frozenset(name.casefold() for name in FORBIDDEN_DIRECTORY_NAMES)
_FORBIDDEN_RELATIVE_PATHS_CASEFOLD = frozenset(path.casefold() for path in FORBIDDEN_RELATIVE_PATHS)
FORBIDDEN_HISTORY_MARKERS = (
    ".env",
    "config.yaml",
    "config.yml",
    "compose.yaml",
    "compose.yml",
    "/tests/",
    "/.git/",
    ".session",
    ".sqlite",
)

REQUIRED_LABELS = {
    "org.opencontainers.image.title": "TGHarbor",
    "org.opencontainers.image.licenses": "Apache-2.0",
}


# Contract violations, registry failures, and acceptance failures share one
# public base so a fail-closed result can never escape the CLI error handler.
VerificationError = ContractError


class RegistryError(ContractError):
    """Raised when a registry response cannot be trusted."""


class RegistryNotFound(RegistryError):
    """Raised when a registry reference does not exist."""


@dataclass(frozen=True)
class RegistryManifest:
    digest: str
    content_type: str
    body: bytes


@dataclass(frozen=True)
class PlatformImage:
    platform: str
    digest: str
    config_digest: str
    architecture: str
    os: str
    labels: Mapping[str, str]
    history: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class AttestationStatement:
    attestation_digest: str
    subject_digest: str
    predicate_type: str
    statement: Mapping[str, Any]


@dataclass(frozen=True)
class ReleaseVerification:
    digest: str
    images: Mapping[str, PlatformImage]
    provenance: Mapping[str, tuple[AttestationStatement, ...]]
    sbom: Mapping[str, tuple[AttestationStatement, ...]]
    attestation_digests: tuple[str, ...]


class Registry(Protocol):
    def get_manifest(self, reference: str) -> RegistryManifest:
        """Return an exact manifest by tag or digest."""
        ...

    def get_blob(self, digest: str) -> bytes:
        """Return a verified blob."""
        ...


def _require_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationError(f"{label} must be a JSON object")
    return value


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"{label} must be a non-empty string")
    return value


def _require_list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VerificationError(f"{label} must be a JSON list")
    return value


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    return value if isinstance(value, str) else None


class RegistryClient:
    """Anonymous Docker Hub registry reader with bounded transient retries."""

    def __init__(
        self,
        *,
        repository: str = CANONICAL_IMAGE,
        registry: str = DOCKER_HUB_REGISTRY,
        auth: str = DOCKER_HUB_AUTH,
        opener: Callable[..., Any] = urllib.request.urlopen,
        attempts: int = 4,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = require_canonical_image(repository)
        self._registry = registry.rstrip("/")
        self._auth = auth
        self._opener = opener
        self._attempts = attempts
        self._timeout = timeout
        self._sleep = sleep
        self._token: str | None = None

    def _fetch_token(self) -> str:
        query = urllib.parse.urlencode(
            {
                "service": "registry.docker.io",
                "scope": f"repository:{self._repository}:pull",
            }
        )
        request = urllib.request.Request(
            f"{self._auth}?{query}",
            headers={"User-Agent": "TGHarbor-release-verification"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                body = response.read(MAX_MANIFEST_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RegistryError("anonymous Docker Hub token request failed") from exc
        if status < 200 or status >= 300 or len(body) > MAX_MANIFEST_BYTES:
            raise RegistryError("anonymous Docker Hub token request failed")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError("anonymous Docker Hub token response is invalid") from exc
        token = document.get("token") if isinstance(document, Mapping) else None
        if not isinstance(token, str) or not token:
            raise RegistryError("anonymous Docker Hub token response has no token")
        return token

    def _request(
        self,
        url: str,
        *,
        accept: str,
        allow_404: bool,
        max_bytes: int,
    ) -> tuple[int, Mapping[str, str], bytes]:
        refreshed = False
        for attempt in range(1, self._attempts + 1):
            if self._token is None:
                self._token = self._fetch_token()
            headers = {
                "Accept": accept,
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "TGHarbor-release-verification",
            }
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    status = getattr(response, "status", response.getcode())
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise RegistryError("registry response exceeded the size limit")
                    response_headers = getattr(response, "headers", {})
                    if status < 200 or status >= 300:
                        raise RegistryError(f"registry returned HTTP {status}")
                    return status, response_headers, body
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not refreshed:
                    self._token = None
                    refreshed = True
                    continue
                if exc.code == 404 and allow_404:
                    raise RegistryNotFound("registry reference does not exist") from exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < self._attempts:
                    self._sleep(float(attempt))
                    continue
                raise RegistryError(f"registry returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self._attempts:
                    self._sleep(float(attempt))
                    continue
                raise RegistryError("registry request did not complete") from exc
        raise RegistryError("registry request did not complete")

    def get_manifest(self, reference: str) -> RegistryManifest:
        if not isinstance(reference, str) or not reference:
            raise RegistryError("registry reference must be a non-empty string")
        if not reference.startswith("sha256:"):
            require_image_tag(reference)
        url = (
            f"{self._registry}/v2/{self._repository}/manifests/"
            f"{urllib.parse.quote(reference, safe=':')}"
        )
        accept = ", ".join(sorted(INDEX_MEDIA_TYPES | MANIFEST_MEDIA_TYPES))
        _status, headers, body = self._request(
            url, accept=accept, allow_404=True, max_bytes=MAX_MANIFEST_BYTES
        )
        computed = f"sha256:{hashlib.sha256(body).hexdigest()}"
        header_digest = headers.get("Docker-Content-Digest") if headers else None
        digest = require_digest(header_digest or computed, label="manifest digest")
        if digest != computed:
            raise RegistryError("registry manifest digest header does not match its body")
        if reference.startswith("sha256:") and digest != reference:
            raise RegistryError("registry returned a different manifest than requested")
        content_type = headers.get("Content-Type", "") if headers else ""
        return RegistryManifest(
            digest=digest,
            content_type=str(content_type).split(";", 1)[0].strip(),
            body=body,
        )

    def get_blob(self, digest: str) -> bytes:
        blob_digest = require_digest(digest, label="blob digest")
        url = (
            f"{self._registry}/v2/{self._repository}/blobs/"
            f"{urllib.parse.quote(blob_digest, safe=':')}"
        )
        _status, _headers, body = self._request(
            url,
            accept="application/octet-stream",
            allow_404=False,
            max_bytes=MAX_REGISTRY_BODY_BYTES,
        )
        computed = f"sha256:{hashlib.sha256(body).hexdigest()}"
        if computed != blob_digest:
            raise RegistryError("registry blob digest does not match its content")
        return body


def parse_json_document(raw: bytes | str, *, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not valid JSON") from exc
    return _require_mapping(document, label=label)


def classify_index(
    index: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], list[Mapping[str, Any]]]:
    media_type = index.get("mediaType")
    if media_type not in INDEX_MEDIA_TYPES:
        raise VerificationError("top-level release manifest must be a multi-platform index")
    manifests = _require_list(index.get("manifests"), label="index manifests")
    runtime: dict[str, Mapping[str, Any]] = {}
    attestations: list[Mapping[str, Any]] = []
    for descriptor in manifests:
        descriptor_map = _require_mapping(descriptor, label="manifest descriptor")
        require_digest(descriptor_map.get("digest"), label="descriptor digest")
        platform = _require_mapping(descriptor_map.get("platform"), label="descriptor platform")
        os_name = _require_text(platform.get("os"), label="descriptor platform os")
        architecture = _require_text(
            platform.get("architecture"), label="descriptor platform architecture"
        )
        annotations = descriptor_map.get("annotations") or {}
        annotations = _require_mapping(annotations, label="descriptor annotations")
        is_attestation = annotations.get(ATTESTATION_TYPE_ANNOTATION) == ATTESTATION_MANIFEST_TYPE
        if is_attestation:
            if os_name != "unknown" or architecture != "unknown":
                raise VerificationError("attestation descriptors must use the unknown platform")
            subject = annotations.get(ATTESTATION_DIGEST_ANNOTATION)
            require_digest(subject, label="attestation subject digest")
            attestations.append(descriptor_map)
            continue
        if os_name != "linux" or architecture not in {"amd64", "arm64"}:
            raise VerificationError(f"unsupported runtime platform: {os_name}/{architecture}")
        if architecture in runtime:
            raise VerificationError(f"duplicate runtime platform: {architecture}")
        variant = platform.get("variant")
        if architecture == "amd64" and variant not in {None, ""}:
            raise VerificationError("linux/amd64 runtime descriptor must not set a variant")
        if architecture == "arm64" and variant not in {None, "", "v8"}:
            raise VerificationError("linux/arm64 runtime descriptor has an unsupported variant")
        runtime[architecture] = descriptor_map
    if set(runtime) != {"amd64", "arm64"}:
        raise VerificationError("release index must contain exactly linux/amd64 and linux/arm64")
    runtime_digests = {str(descriptor["digest"]) for descriptor in runtime.values()}
    for descriptor in attestations:
        subject = descriptor["annotations"][ATTESTATION_DIGEST_ANNOTATION]
        if subject not in runtime_digests:
            raise VerificationError("attestation descriptor does not reference a runtime image")
    return runtime, attestations


def validate_release_labels(labels: Mapping[str, Any], metadata: ReleaseMetadata) -> None:
    expected = {
        **REQUIRED_LABELS,
        "org.opencontainers.image.version": metadata.version,
        "org.opencontainers.image.revision": metadata.sha,
        "org.opencontainers.image.source": metadata.source,
        "org.opencontainers.image.created": metadata.created,
    }
    for key, value in expected.items():
        if labels.get(key) != value:
            raise VerificationError(f"OCI label mismatch: {key}")
    revision = labels.get("org.opencontainers.image.revision")
    require_full_sha(revision, label="revision label")
    source = labels.get("org.opencontainers.image.source")
    if not isinstance(source, str) or CANONICAL_SOURCE_PATTERN.fullmatch(source) is None:
        raise VerificationError("source label does not match the verified project source")


def assert_history_has_no_project_payload(history: object) -> None:
    entries = _require_list(history, label="image history")
    for entry in entries:
        entry_map = _require_mapping(entry, label="history entry")
        for key in ("created_by", "comment", "author"):
            value = entry_map.get(key)
            if not isinstance(value, str):
                continue
            lowered = value.lower()
            for marker in FORBIDDEN_HISTORY_MARKERS:
                if marker in lowered:
                    raise VerificationError(
                        "image history references a forbidden project payload name"
                    )


def _forbidden_layer_path_reason(
    normalized: str, policy: ForbiddenPathPolicy = EMPTY_POLICY
) -> str | None:
    """Return a path-only policy reason for a normalized layer member path."""

    parts = tuple(part.casefold() for part in normalized.split("/") if part)
    if not parts:
        return None
    relative = "/".join(parts)
    if policy.is_forbidden(relative):
        return "forbidden restricted path"
    if relative in _FORBIDDEN_RELATIVE_PATHS_CASEFOLD:
        return "forbidden restricted file"
    if any(part in _FORBIDDEN_DIRECTORIES_CASEFOLD for part in parts):
        return "forbidden development directory"
    basename = parts[-1]
    if basename in _FORBIDDEN_BASENAMES_CASEFOLD:
        return "forbidden restricted, configuration, or development file"
    if basename.endswith(FORBIDDEN_SUFFIXES):
        return "forbidden user-state file suffix"
    return None


def _normalize_layer_member_path(name: str) -> tuple[str, str | None]:
    """Normalize a layer member name without reading its content."""

    if not name:
        return "<empty>", "layer member has an empty path"
    if "\x00" in name:
        return "<invalid>", "layer member path contains a NUL byte"
    normalized = name
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if not normalized:
        return "<empty>", "layer member has an empty path"
    if "\\" in normalized:
        return normalized.replace("\\", "/"), "layer member path contains an ambiguous backslash"
    if normalized.startswith("/") or WINDOWS_DRIVE_PATTERN.match(normalized):
        return normalized, "layer member path is absolute"
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        return normalized, "layer member path traverses outside the layer root"
    if any(part == "." for part in parts):
        return normalized, "layer member path contains a current-directory component"
    if any(part == "" for part in parts):
        return normalized, "layer member path contains an empty path component"
    if posixpath.normpath(normalized) != normalized:
        return normalized, "layer member path does not normalize safely"
    return normalized, None


def _resolve_layer_link_target(
    member_path: str, target: str, *, hardlink: bool
) -> tuple[str | None, str]:
    """Resolve a link target textually; links are never followed."""

    if not target:
        return None, "link target is empty"
    if "\x00" in target:
        return None, "link target contains a NUL byte"
    if "\\" in target:
        return None, "link target contains an ambiguous backslash"
    if target.startswith("/") or WINDOWS_DRIVE_PATTERN.match(target):
        return None, "link target is absolute"
    target = target.rstrip("/")
    if not target:
        return None, "link target is empty"
    parts = target.split("/")
    base = [] if hardlink else member_path.split("/")[:-1]
    for part in parts:
        if part == "":
            return None, "link target contains an empty path component"
        if part == ".":
            continue
        if part == "..":
            if not base:
                return None, "link target escapes the layer root"
            base.pop()
            continue
        base.append(part)
    resolved = "/".join(base)
    if not resolved:
        return None, "link target resolves to the layer root"
    return resolved, ""


def _inspect_layer_member(
    layer_digest: str,
    member: tarfile.TarInfo,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> None:
    """Reject one unsafe or forbidden layer member using metadata only."""

    normalized, path_reason = _normalize_layer_member_path(member.name)
    if path_reason is not None:
        raise VerificationError(f"runtime layer {layer_digest} member {normalized}: {path_reason}")
    if member.ischr() or member.isblk():
        raise VerificationError(
            f"runtime layer {layer_digest} member {normalized}: device member is not allowed"
        )
    if member.isfifo():
        raise VerificationError(
            f"runtime layer {layer_digest} member {normalized}: FIFO member is not allowed"
        )
    if member.issym() or member.islnk():
        resolved, link_reason = _resolve_layer_link_target(
            normalized, member.linkname, hardlink=member.islnk()
        )
        if link_reason:
            raise VerificationError(
                f"runtime layer {layer_digest} member {normalized}: {link_reason}"
            )
        assert resolved is not None
        forbidden_reason = _forbidden_layer_path_reason(resolved, policy)
        if forbidden_reason is not None:
            raise VerificationError(
                f"runtime layer {layer_digest} member {normalized}: "
                "link target resolves to a forbidden path"
            )
        return
    if not (member.isreg() or member.isdir()):
        raise VerificationError(
            f"runtime layer {layer_digest} member {normalized}: unsupported layer member type"
        )
    forbidden_reason = _forbidden_layer_path_reason(normalized, policy)
    if forbidden_reason is not None:
        raise VerificationError(
            f"runtime layer {layer_digest} member {normalized}: {forbidden_reason}"
        )


def inspect_layer_archive(
    layer_digest: str,
    media_type: str,
    data: bytes,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> None:
    """Fail closed on unsafe members in one runtime layer archive."""

    digest = require_digest(layer_digest, label="runtime layer digest")
    if media_type not in SUPPORTED_LAYER_MEDIA_TYPES:
        raise VerificationError(
            f"runtime layer {digest} uses unsupported media type {media_type!r}"
        )
    if not isinstance(data, bytes) or not data:
        raise VerificationError(f"runtime layer {digest} is empty or not bytes")
    compressed = media_type in GZIP_LAYER_MEDIA_TYPES
    if compressed and not data.startswith(GZIP_MAGIC):
        raise VerificationError(f"runtime layer {digest} does not contain a gzip stream")
    mode = "r:gz" if compressed else "r:"
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as archive:
            for member in archive.getmembers():
                _inspect_layer_member(digest, member, policy)
    except VerificationError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        kind = "gzip" if compressed else "tar"
        raise VerificationError(f"runtime layer {digest} is not a readable {kind} archive") from exc


def inspect_runtime_layers(
    registry: Registry,
    layers: object,
    *,
    platform: str,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> tuple[str, ...]:
    """Fetch and inspect every runtime layer before any filesystem application."""

    descriptors = _require_list(layers, label=f"{platform} runtime image layers")
    inspected: list[str] = []
    for descriptor in descriptors:
        layer_map = _require_mapping(descriptor, label=f"{platform} runtime layer descriptor")
        digest = require_digest(layer_map.get("digest"), label="runtime layer digest")
        media_type = _require_text(layer_map.get("mediaType"), label="runtime layer media type")
        if media_type not in SUPPORTED_LAYER_MEDIA_TYPES:
            raise VerificationError(
                f"runtime layer {digest} uses unsupported media type {media_type!r}"
            )
        data = registry.get_blob(digest)
        size = layer_map.get("size")
        if size is not None:
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise VerificationError(f"runtime layer {digest} descriptor size is invalid")
            if size != len(data):
                raise VerificationError(
                    f"runtime layer {digest} size does not match its descriptor"
                )
        inspect_layer_archive(digest, media_type, data, policy)
        inspected.append(digest)
    return tuple(inspected)


def _get_exact_manifest(registry: Registry, digest: str) -> RegistryManifest:
    manifest = registry.get_manifest(require_digest(digest))
    if manifest.digest != digest:
        raise VerificationError("registry returned an unexpected manifest digest")
    return manifest


def _parse_platform_image(
    registry: Registry,
    descriptor: Mapping[str, Any],
    metadata: ReleaseMetadata,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> PlatformImage:
    digest = require_digest(descriptor.get("digest"), label="platform manifest digest")
    manifest = _get_exact_manifest(registry, digest)
    document = parse_json_document(manifest.body, label="platform manifest")
    if document.get("mediaType") not in MANIFEST_MEDIA_TYPES:
        raise VerificationError("platform manifest has an unsupported media type")
    config = _require_mapping(document.get("config"), label="platform config descriptor")
    config_digest = require_digest(config.get("digest"), label="platform config digest")
    config_document = parse_json_document(registry.get_blob(config_digest), label="platform config")
    platform = _require_mapping(descriptor.get("platform"), label="platform descriptor")
    architecture = _require_text(config_document.get("architecture"), label="config architecture")
    os_name = _require_text(config_document.get("os"), label="config os")
    if architecture != platform.get("architecture") or os_name != platform.get("os"):
        raise VerificationError("platform config architecture/os does not match its descriptor")
    config_section = _require_mapping(config_document.get("config"), label="config section")
    labels = config_section.get("Labels") or {}
    labels = _require_mapping(labels, label="config labels")
    validate_release_labels(labels, metadata)
    assert_history_has_no_project_payload(config_document.get("history", []))
    inspect_runtime_layers(
        registry,
        document.get("layers", []),
        platform=f"{os_name}/{architecture}",
        policy=policy,
    )
    return PlatformImage(
        platform=f"{os_name}/{architecture}",
        digest=digest,
        config_digest=config_digest,
        architecture=architecture,
        os=os_name,
        labels={str(key): str(value) for key, value in labels.items()},
        history=tuple(
            entry for entry in config_document.get("history", []) if isinstance(entry, Mapping)
        ),
    )


def _iter_strings(value: object) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, Mapping):
        for nested in value.values():
            strings.extend(_iter_strings(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            strings.extend(_iter_strings(nested))
    return strings


def statement_binds_subject(statement: Mapping[str, Any], subject_digest: str) -> bool:
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return False
    digest_hex = subject_digest.split(":", 1)[-1]
    for subject in subjects:
        if not isinstance(subject, Mapping):
            continue
        digests = subject.get("digest")
        if not isinstance(digests, Mapping):
            continue
        if any(value in {subject_digest, digest_hex} for value in digests.values()):
            return True
    return False


def provenance_binds_source_and_revision(
    statement: Mapping[str, Any], metadata: ReleaseMetadata
) -> bool:
    predicate = statement.get("predicate")
    if not isinstance(predicate, Mapping):
        return False
    values = _iter_strings(predicate)
    has_source = any(CANONICAL_SOURCE_PATTERN.fullmatch(value) for value in values)
    has_revision = any(value == metadata.sha for value in values)
    return has_source and has_revision


def _parse_attestation_statements(
    registry: Registry,
    descriptor: Mapping[str, Any],
    platform_by_digest: Mapping[str, str],
) -> list[AttestationStatement]:
    digest = require_digest(descriptor.get("digest"), label="attestation manifest digest")
    annotations = _require_mapping(descriptor.get("annotations"), label="attestation annotations")
    subject_digest = require_digest(
        annotations.get(ATTESTATION_DIGEST_ANNOTATION), label="attestation subject"
    )
    if subject_digest not in platform_by_digest:
        raise VerificationError("attestation subject is not a runtime image")
    manifest = _get_exact_manifest(registry, digest)
    document = parse_json_document(manifest.body, label="attestation manifest")
    if document.get("mediaType") not in MANIFEST_MEDIA_TYPES:
        raise VerificationError("attestation manifest has an unsupported media type")
    manifest_subject = document.get("subject")
    if manifest_subject is not None:
        subject_map = _require_mapping(manifest_subject, label="attestation subject")
        if subject_map.get("digest") != subject_digest:
            raise VerificationError("attestation manifest subject does not match its descriptor")
    layers = _require_list(document.get("layers"), label="attestation layers")
    if not layers:
        raise VerificationError("attestation manifest has no statement layers")
    statements: list[AttestationStatement] = []
    for layer in layers:
        layer_map = _require_mapping(layer, label="attestation layer")
        blob_digest = require_digest(layer_map.get("digest"), label="attestation blob digest")
        layer_annotations = layer_map.get("annotations") or {}
        layer_annotations = _require_mapping(
            layer_annotations, label="attestation layer annotations"
        )
        statement = parse_json_document(registry.get_blob(blob_digest), label="in-toto statement")
        predicate_type = statement.get("predicateType")
        if not isinstance(predicate_type, str) or not predicate_type:
            raise VerificationError("in-toto statement has no predicate type")
        annotated_type = layer_annotations.get(IN_TOTO_PREDICATE_ANNOTATION)
        if annotated_type is not None and annotated_type != predicate_type:
            raise VerificationError("attestation layer predicate type does not match its statement")
        if not statement_binds_subject(statement, subject_digest):
            raise VerificationError("attestation statement does not bind its image subject")
        statements.append(
            AttestationStatement(
                attestation_digest=digest,
                subject_digest=subject_digest,
                predicate_type=predicate_type,
                statement=statement,
            )
        )
    return statements


def verify_attestations(
    registry: Registry,
    descriptors: list[Mapping[str, Any]],
    images: Mapping[str, PlatformImage],
    metadata: ReleaseMetadata,
) -> tuple[
    dict[str, tuple[AttestationStatement, ...]],
    dict[str, tuple[AttestationStatement, ...]],
    tuple[str, ...],
]:
    platform_by_digest = {image.digest: platform for platform, image in images.items()}
    statements: list[AttestationStatement] = []
    attestation_digests: set[str] = set()
    for descriptor in descriptors:
        descriptor_digest = require_digest(
            descriptor.get("digest"), label="attestation manifest digest"
        )
        attestation_digests.add(descriptor_digest)
        statements.extend(_parse_attestation_statements(registry, descriptor, platform_by_digest))
    provenance: dict[str, list[AttestationStatement]] = {platform: [] for platform in images}
    sbom: dict[str, list[AttestationStatement]] = {platform: [] for platform in images}
    platform_by_digest = {image.digest: platform for platform, image in images.items()}
    for statement in statements:
        platform = platform_by_digest[statement.subject_digest]
        if statement.predicate_type.startswith(PROVENANCE_PREFIX):
            if not provenance_binds_source_and_revision(statement.statement, metadata):
                raise VerificationError(
                    "provenance does not bind the project source and release revision"
                )
            provenance[platform].append(statement)
        elif any(marker in statement.predicate_type for marker in SBOM_MARKERS):
            sbom[platform].append(statement)
    for platform in images:
        if not provenance[platform]:
            raise VerificationError(f"missing verifiable provenance for {platform}")
        if not sbom[platform]:
            raise VerificationError(f"missing verifiable SBOM for {platform}")
    return (
        {platform: tuple(values) for platform, values in provenance.items()},
        {platform: tuple(values) for platform, values in sbom.items()},
        tuple(sorted(attestation_digests)),
    )


def _resolve_tag(
    registry: Registry,
    tag: str,
    expected_digest: str,
    *,
    attempts: int,
    delay: float,
    sleep: Callable[[float], None],
) -> RegistryManifest:
    last_problem = "not resolved"
    for attempt in range(1, attempts + 1):
        try:
            manifest = registry.get_manifest(tag)
        except RegistryNotFound:
            last_problem = "tag not found"
        except RegistryError as exc:
            last_problem = str(exc)
        else:
            if manifest.digest == expected_digest:
                return manifest
            last_problem = "digest mismatch"
        if attempt < attempts:
            sleep(delay)
    raise VerificationError(
        f"Docker Hub tag {tag} did not resolve to the Buildx digest after {attempts} attempts "
        f"({last_problem})"
    )


def verify_registry_release(
    registry: Registry,
    *,
    tags: tuple[str, ...],
    expected_digest: str,
    metadata: ReleaseMetadata,
    retry_attempts: int = 12,
    retry_delay: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> ReleaseVerification:
    digest = require_digest(expected_digest, label="Buildx output digest")
    if set(tags) != set(metadata.image_tags) or len(set(tags)) != 4:
        raise VerificationError("release must publish exactly the four required tags")
    if retry_attempts < 1:
        raise VerificationError("retry attempts must be positive")
    resolved: list[RegistryManifest] = []
    for tag in tags:
        require_image_tag(tag)
        resolved.append(
            _resolve_tag(
                registry,
                tag,
                digest,
                attempts=retry_attempts,
                delay=retry_delay,
                sleep=sleep,
            )
        )
    if any(manifest.digest != digest for manifest in resolved):
        raise VerificationError("published tags do not share one top-level digest")
    index = parse_json_document(resolved[0].body, label="release index")
    runtime_descriptors, attestation_descriptors = classify_index(index)
    images: dict[str, PlatformImage] = {}
    for descriptor in runtime_descriptors.values():
        image = _parse_platform_image(registry, descriptor, metadata, policy)
        images[image.platform] = image
    if set(images) != set(RUNTIME_PLATFORMS):
        raise VerificationError("release image does not expose both required platforms")
    provenance, sbom, attestation_digests = verify_attestations(
        registry, attestation_descriptors, images, metadata
    )
    return ReleaseVerification(
        digest=digest,
        images=images,
        provenance=provenance,
        sbom=sbom,
        attestation_digests=attestation_digests,
    )


_SMOKE_SCRIPT_TEMPLATE = r"""
import json
import os
import pathlib
import sys
import tgharbor

forbidden_names = {
    ".env", ".env.local", ".env.production", "config.yaml", "config.yml",
    "compose.yaml", "compose.yml", "compose.override.yaml", "compose.local.yaml",
} | set(__EXTRA_BASENAMES__)
forbidden_dirs = {".git", "docs", "tests"} | set(__EXTRA_DIRECTORIES__)
forbidden_relative = set(__EXTRA_RELATIVE_PATHS__)
forbidden_suffixes = (".session", ".session-journal", ".db", ".sqlite", ".sqlite3", ".log")
directories = {}
for root in ("/app", "/app/src/tgharbor", "/config", "/data", "/downloads"):
    path = pathlib.Path(root)
    directories[root] = path.is_dir()
problems = []
for root in ("/app", "/config", "/data", "/downloads"):
    path = pathlib.Path(root)
    if not path.is_dir():
        continue
    for member in path.rglob("*"):
        relative = member.relative_to(path).as_posix()
        if member.name in forbidden_names:
            problems.append(str(member))
        if any(part in forbidden_dirs for part in member.parts):
            problems.append(str(member))
        if relative in forbidden_relative:
            problems.append(str(member))
        if member.name.endswith(forbidden_suffixes):
            problems.append(str(member))
for path in ("/config", "/data", "/downloads"):
    if pathlib.Path(path).is_dir() and next(pathlib.Path(path).iterdir(), None) is not None:
        problems.append(str(pathlib.Path(path)))
print(json.dumps({
    "python": "%d.%d.%d" % sys.version_info[:3],
    "platform": sys.platform,
    "version": tgharbor.__version__,
    "uid": os.getuid(),
    "gid": os.getgid(),
    "cwd": os.getcwd(),
    "directories": directories,
    "forbidden": problems,
}, sort_keys=True))
"""


def build_smoke_script(policy: ForbiddenPathPolicy = EMPTY_POLICY) -> str:
    return (
        _SMOKE_SCRIPT_TEMPLATE.replace("__EXTRA_BASENAMES__", json.dumps(sorted(policy.basenames)))
        .replace("__EXTRA_DIRECTORIES__", json.dumps(sorted(policy.directory_names)))
        .replace("__EXTRA_RELATIVE_PATHS__", json.dumps(sorted(policy.relative_paths)))
    )


def smoke_test_command(
    platform: str,
    digest: str,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> list[str]:
    if platform not in RUNTIME_PLATFORMS:
        raise VerificationError(f"unsupported smoke-test platform: {platform}")
    image_digest = require_digest(digest, label="smoke-test digest")
    require_canonical_image(CANONICAL_IMAGE)
    return [
        "docker",
        "run",
        "--rm",
        "--platform",
        platform,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--entrypoint",
        "python",
        f"{CANONICAL_IMAGE}@{image_digest}",
        "-c",
        build_smoke_script(policy),
    ]


def verify_smoke_payload(stdout: str, *, expected_version: str) -> Mapping[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise VerificationError("isolated runtime probe produced no output")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise VerificationError("isolated runtime probe output is not JSON") from exc
    document = _require_mapping(payload, label="isolated runtime probe")
    python_version = document.get("python")
    if not isinstance(python_version, str) or not python_version.startswith("3.15."):
        raise VerificationError("release image does not run Python 3.15")
    if document.get("platform") != "linux":
        raise VerificationError("release image runtime platform is not linux")
    if document.get("version") != expected_version:
        raise VerificationError("release image reports the wrong tgharbor version")
    if document.get("uid") != 10001 or document.get("gid") != 10001:
        raise VerificationError("release image does not run as uid/gid 10001")
    if document.get("cwd") != "/app":
        raise VerificationError("release image working directory is not /app")
    directories = _require_mapping(document.get("directories"), label="runtime directories")
    if not directories or not all(value is True for value in directories.values()):
        raise VerificationError("release image is missing an expected runtime directory")
    if document.get("forbidden"):
        raise VerificationError("release image contains forbidden project payload paths")
    return document


def run_smoke_tests(
    images: Mapping[str, PlatformImage],
    *,
    expected_version: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 180.0,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> dict[str, Mapping[str, Any]]:
    if set(images) != set(RUNTIME_PLATFORMS):
        raise VerificationError("smoke tests require both runtime platforms")
    results: dict[str, Mapping[str, Any]] = {}
    for platform in RUNTIME_PLATFORMS:
        command = smoke_test_command(platform, images[platform].digest, policy)
        try:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VerificationError(f"isolated runtime probe could not run for {platform}") from exc
        if completed.returncode != 0:
            raise VerificationError(
                f"isolated runtime probe failed for {platform} with exit code "
                f"{completed.returncode}"
            )
        results[platform] = verify_smoke_payload(
            completed.stdout, expected_version=expected_version
        )
    return results


def _require_created(value: object) -> str:
    if not isinstance(value, str) or CREATED_PATTERN.fullmatch(value) is None:
        raise VerificationError("image created timestamp must be deterministic RFC 3339 UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise VerificationError("image created timestamp is invalid") from exc
    return value


def _metadata_from_arguments(arguments: argparse.Namespace) -> ReleaseMetadata:
    semver = parse_stable_tag(arguments.release_tag)
    if arguments.release_version != semver.version:
        raise VerificationError("release tag and release version differ")
    sha = require_full_sha(arguments.release_sha, label="release revision")
    source = arguments.image_source
    if source != CANONICAL_SOURCE_URL:
        raise VerificationError("image source must match the verified project source")
    return ReleaseMetadata(
        tag=arguments.release_tag,
        version=semver.version,
        minor_version=semver.minor_version,
        sha=sha,
        created=_require_created(arguments.image_created),
        source=source,
    )


def _write_summary(
    summary_path: object,
    result: ReleaseVerification,
    smoke: Mapping[str, Mapping[str, Any]],
) -> None:
    lines = [
        "## Docker Hub release verification",
        "",
        f"- Top-level digest: `{result.digest}`",
        f"- Attestation manifests: {len(result.attestation_digests)}",
        "",
        "| Platform | Manifest digest | Config digest | Python | UID |",
        "| --- | --- | --- | --- | --- |",
    ]
    for platform in RUNTIME_PLATFORMS:
        image = result.images[platform]
        probe = smoke[platform]
        lines.append(
            f"| `{platform}` | `{image.digest}` | `{image.config_digest}` | "
            f"`{probe['python']}` | `{probe['uid']}` |"
        )
    summary = "\n".join(lines) + "\n"
    with Path(summary_path).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(summary)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a published TGHarbor Docker Hub release.")
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--image-created", required=True)
    parser.add_argument("--image-source", required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--output", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    parser.add_argument("--retry-attempts", type=int, default=12)
    parser.add_argument("--retry-delay", type=float, default=10.0)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    registry_factory: Callable[[], Registry] = RegistryClient,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        policy = policy_from_environment()
        metadata = _metadata_from_arguments(arguments)
        result = verify_registry_release(
            registry_factory(),
            tags=metadata.image_tags,
            expected_digest=arguments.expected_digest,
            metadata=metadata,
            retry_attempts=arguments.retry_attempts,
            retry_delay=arguments.retry_delay,
            sleep=sleep,
            policy=policy,
        )
        smoke = run_smoke_tests(
            result.images,
            expected_version=metadata.version,
            runner=runner,
            policy=policy,
        )
        write_github_outputs(
            arguments.output,
            {
                "image_digest": result.digest,
                "amd64_digest": result.images["linux/amd64"].digest,
                "arm64_digest": result.images["linux/arm64"].digest,
                "amd64_config_digest": result.images["linux/amd64"].config_digest,
                "arm64_config_digest": result.images["linux/arm64"].config_digest,
                "attestation_count": len(result.attestation_digests),
            },
        )
        if arguments.summary:
            _write_summary(arguments.summary, result, smoke)
        print(
            f"Verified {metadata.tag} at {result.digest} for linux/amd64 and linux/arm64 "
            "with provenance, SBOM, OCI labels, and isolated runtime probes."
        )
        return 0
    except (ContractError, VerificationError, RegistryError, PolicyError) as exc:
        print(f"::error title=Release verification failed::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
