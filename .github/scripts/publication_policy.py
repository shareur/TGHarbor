"""Path-only restricted-path policy shared by release verification scripts.

The public repository carries no project-specific restricted path names.
Publication tooling may inject an additional JSON policy from a local file or
environment variable. These helpers validate that policy and match repository
relative paths without reading or printing file contents. An absent policy is
normal and never implies another source tree.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

POLICY_ENVIRONMENT_VARIABLE = "TGHARBOR_PUBLICATION_POLICY"
MAX_POLICY_BYTES = 64_000
_POLICY_KEYS = frozenset({"directories", "basenames", "relative_paths"})


class PolicyError(ValueError):
    """Raised when an injected restricted-path policy cannot be trusted."""


def _normalize_entry(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"policy {label} entries must be strings")
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.strip("/")
    if not normalized:
        raise PolicyError(f"policy {label} entries must not be empty")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyError(
            f"policy {label} entries must be relative paths without traversal components"
        )
    if len(parts[0]) == 2 and parts[0][1] == ":":
        raise PolicyError(f"policy {label} entries must not be absolute paths")
    return "/".join(parts).casefold()


def _entries(document: Mapping[str, object], key: str) -> frozenset[str]:
    value = document.get(key, [])
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise PolicyError(f"policy {key} must be a JSON list")
    return frozenset(_normalize_entry(entry, label=key) for entry in value)


@dataclass(frozen=True)
class ForbiddenPathPolicy:
    """A set of restricted directory names, basenames, and relative paths."""

    directory_names: frozenset[str] = frozenset()
    basenames: frozenset[str] = frozenset()
    relative_paths: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        return not (self.directory_names or self.basenames or self.relative_paths)

    def is_forbidden(self, value: str) -> bool:
        normalized = value.replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.strip("/")
        parts = tuple(part.casefold() for part in normalized.split("/") if part not in {"", "."})
        if not parts:
            return False
        if any(part in self.directory_names for part in parts):
            return True
        if parts[-1] in self.basenames:
            return True
        return "/".join(parts) in self.relative_paths


EMPTY_POLICY = ForbiddenPathPolicy()


def policy_from_document(document: object) -> ForbiddenPathPolicy:
    """Build a policy from a decoded JSON document."""

    if not isinstance(document, Mapping):
        raise PolicyError("publication policy must be a JSON object")
    unknown = set(document) - _POLICY_KEYS
    if unknown:
        raise PolicyError("publication policy contains unsupported keys")
    return ForbiddenPathPolicy(
        directory_names=_entries(document, "directories"),
        basenames=_entries(document, "basenames"),
        relative_paths=_entries(document, "relative_paths"),
    )


def load_policy_file(path: object) -> ForbiddenPathPolicy:
    """Read and validate a JSON policy file without exposing its contents."""

    if not isinstance(path, (str, os.PathLike)) or not str(path):
        raise PolicyError("publication policy path must be a filesystem path")
    policy_path = Path(path)
    try:
        if policy_path.stat().st_size > MAX_POLICY_BYTES:
            raise PolicyError("publication policy file exceeded the size limit")
        raw = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError("publication policy file could not be read") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError("publication policy file is not valid JSON") from exc
    return policy_from_document(document)


def policy_from_environment(environ: Mapping[str, str] | None = None) -> ForbiddenPathPolicy:
    """Load an optional policy path from the environment; empty means public mode."""

    environment = os.environ if environ is None else environ
    path = environment.get(POLICY_ENVIRONMENT_VARIABLE, "").strip()
    if not path:
        return EMPTY_POLICY
    return load_policy_file(path)
