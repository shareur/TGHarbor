"""Path-only publication-boundary verification for TGHarbor.

The public policy checks structural safety without embedding project-specific
path names: tracked and reachable-history path shape, ``git archive`` member
shape, tracked public link and embed targets, ignore and build-context
structure, tracked symbolic links, and fail-closed artifact uploads.

Publication tooling may inject an optional restricted-path policy from a local
file or environment variable. An absent policy is normal: public CI still runs
every generic check, and a missing local policy never implies another source
tree.

Tests exercise the pure helpers with supplied path and text lists, or call
:func:`verify_repository` against a temporary Git repository.
"""

from __future__ import annotations

import argparse
import html
import io
import re
import subprocess
import sys
import tarfile
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from publication_policy import (
    EMPTY_POLICY,
    POLICY_ENVIRONMENT_VARIABLE,
    ForbiddenPathPolicy,
    PolicyError,
    load_policy_file,
    policy_from_environment,
)

PUBLIC_GITIGNORE_REQUIRED_RULES = frozenset(
    {
        "/.env",
        "/.env.*",
        "!/.env.example",
        "*.session",
        "*.session-*",
        "*.db",
        "*.db-*",
        "*.sqlite",
        "*.sqlite-*",
        "*.sqlite3",
        "*.sqlite3-*",
        "*.log",
        "/data/",
        "/downloads/",
    }
)
DOCKERIGNORE_DENY_ALL_RULE = "*"
DOCKERIGNORE_REQUIRED_INCLUDE_RULES = frozenset(
    {
        "!pyproject.toml",
        "!README.md",
        "!LICENSE",
        "!hatch_build.py",
        "!src",
        "!src/**",
    }
)
DOCKERIGNORE_REQUIRED_INCLUDE_PATHS = frozenset(
    {"pyproject.toml", "README.md", "LICENSE", "hatch_build.py", "src"}
)

PUBLIC_DOCUMENT_SUFFIXES = frozenset({".md", ".markdown", ".html", ".htm", ".svg"})
WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
WORKFLOW_DIRECTORY = ".github/workflows/"
ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})
GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
WORKSTATION_PREFIXES = (
    "/home",
    "/Users",
    "/root",
    "/tmp",
    "/var",
    "/opt",
    "/mnt",
    "/srv",
    "/private",
)
DOCKERIGNORE_README_EXCLUDERS = frozenset(
    {"README.md", "/README.md", "*", "/*", "**", "**/*", "*.md", "/*.md"}
)
DOCKERIGNORE_README_INCLUDERS = frozenset({"!README.md", "!/README.md"})

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_ERROR = 2

_MARKDOWN_LINK = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_MARKDOWN_REFERENCE = re.compile(
    r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]*>|\S+)",
    re.MULTILINE,
)
_HTML_ATTRIBUTE = re.compile(
    r"\b(?:href|src|xlink:href)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]*:[^>\s]+)>")
_FENCED_CODE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$")
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_SCP_LIKE_REPOSITORY = re.compile(r"^[^/\s]+@[^:\s]+:")


class BoundaryError(RuntimeError):
    """Raised when the boundary cannot be evaluated reliably."""


@dataclass(frozen=True, order=True)
class BoundaryViolation:
    """A path-only diagnostic; file contents are never included."""

    path: str
    reason: str

    def format(self) -> str:
        return f"{self.path}: {self.reason}"


def _slash(value: str) -> str:
    return value.replace("\\", "/")


def _trim_repo_prefix(value: str) -> str:
    normalized = _slash(value).strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def is_forbidden_path(value: str, policy: ForbiddenPathPolicy = EMPTY_POLICY) -> bool:
    """Return whether a repository-relative path matches a restricted policy."""

    return policy.is_forbidden(value)


def _resolve_repo_path(source_path: str, target_path: str) -> str | None:
    combined = list(PurePosixPath(_slash(source_path)).parent.parts)
    for part in _slash(target_path).split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not combined:
                return None
            combined.pop()
            continue
        combined.append(part)
    return "/".join(combined)


def _is_workstation_path(target: str) -> bool:
    normalized = _slash(target).strip()
    if normalized.startswith(("~", "//")):
        return True
    if _WINDOWS_DRIVE.match(normalized):
        return True
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in WORKSTATION_PREFIXES
    )


def _github_content_path(path: str) -> str | None:
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 3:
        return None
    if segments[2].casefold() not in {"blob", "tree", "raw", "blame", "edit"}:
        return None
    return "/".join(segments[4:])


def _unsafe_path_reason(value: str) -> str | None:
    """Return a structural reason for an unsafe repository-relative path."""

    if not value:
        return "path is empty"
    if "\x00" in value:
        return "path contains a NUL byte"
    if "\\" in value:
        return "path contains an ambiguous backslash"
    normalized = value.rstrip("/")
    if not normalized:
        return "path is empty"
    if normalized.startswith("/") or _WINDOWS_DRIVE.match(normalized):
        return "path is absolute"
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        return "path traverses outside its root"
    if any(part == "." for part in parts):
        return "path contains a current-directory component"
    if any(part == "" for part in parts):
        return "path contains an empty component"
    return None


def _check_network_target(
    parsed: urllib.parse.SplitResult, policy: ForbiddenPathPolicy
) -> str | None:
    scheme = parsed.scheme.casefold()
    if scheme not in ALLOWED_SCHEMES:
        return f"unsafe link scheme: {scheme}"
    if scheme == "mailto":
        return None
    if parsed.username or parsed.password:
        return "link URL contains embedded credentials"
    hostname = (parsed.hostname or "").casefold()
    if hostname in GITHUB_HOSTS:
        content_path = _github_content_path(urllib.parse.unquote(parsed.path))
        if content_path is not None and policy.is_forbidden(content_path):
            return "repository link targets a restricted path"
    return None


def _check_link_target(
    source_path: str, raw_target: str, policy: ForbiddenPathPolicy
) -> BoundaryViolation | None:
    target = _slash(urllib.parse.unquote(html.unescape(raw_target))).strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or target.startswith("#"):
        return None
    if _SCP_LIKE_REPOSITORY.match(target):
        return BoundaryViolation(source_path, "link target uses an SSH-style repository transport")
    if _WINDOWS_DRIVE.match(target) or _is_workstation_path(target):
        return BoundaryViolation(source_path, "link target is an absolute workstation path")
    try:
        parsed = urllib.parse.urlsplit(target)
    except ValueError:
        return BoundaryViolation(source_path, "link target is not a valid URL")
    if parsed.scheme:
        reason = _check_network_target(parsed, policy)
        if reason is not None:
            return BoundaryViolation(source_path, reason)
        return None
    path_part = target.split("#", 1)[0].split("?", 1)[0]
    if not path_part:
        return None
    if path_part.startswith("/"):
        resolved = _resolve_repo_path(".", path_part)
    else:
        resolved = _resolve_repo_path(source_path, path_part)
    if resolved is None:
        return BoundaryViolation(source_path, f"link target escapes the repository: {target}")
    if policy.is_forbidden(resolved):
        return BoundaryViolation(
            source_path,
            f"link target resolves to a restricted path: {resolved}",
        )
    return None


def _extract_link_targets(text: str) -> list[str]:
    cleaned = _HTML_COMMENT.sub("", text)
    cleaned = _FENCED_CODE.sub("", cleaned)
    cleaned = _INLINE_CODE.sub("", cleaned)
    targets = [match.group(1) for match in _MARKDOWN_LINK.finditer(cleaned)]
    targets.extend(match.group(1) for match in _MARKDOWN_REFERENCE.finditer(cleaned))
    for match in _HTML_ATTRIBUTE.finditer(cleaned):
        targets.extend(group for group in match.groups() if group is not None)
    targets.extend(match.group(1) for match in _AUTOLINK.finditer(cleaned))
    return targets


def check_public_document(
    path: str,
    text: str,
    policy: ForbiddenPathPolicy = EMPTY_POLICY,
) -> list[BoundaryViolation]:
    """Check only link and embed targets in a public Markdown/HTML/SVG surface."""

    violations: list[BoundaryViolation] = []
    for target in _extract_link_targets(text):
        violation = _check_link_target(path, target, policy)
        if violation is not None:
            violations.append(violation)
    return _dedupe(violations)


def check_tracked_paths(
    paths: Iterable[str], policy: ForbiddenPathPolicy = EMPTY_POLICY
) -> list[BoundaryViolation]:
    violations = []
    for path in paths:
        reason = _unsafe_path_reason(path)
        if reason is not None:
            violations.append(BoundaryViolation(path, f"tracked path is unsafe: {reason}"))
            continue
        if policy.is_forbidden(path):
            violations.append(
                BoundaryViolation(path, "tracked path matches a restricted-path policy")
            )
    return _dedupe(violations)


def _history_path(line: str) -> str | None:
    line = line.rstrip("\n")
    if not line:
        return None
    _object_id, separator, path = line.partition(" ")
    if not separator or not path:
        return None
    return path


def check_history_paths(
    entries: Iterable[str], policy: ForbiddenPathPolicy = EMPTY_POLICY
) -> list[BoundaryViolation]:
    """Check ``git rev-list --objects --all`` output including deleted paths."""

    violations = []
    for entry in entries:
        path = _history_path(entry)
        if path is None:
            continue
        reason = _unsafe_path_reason(path)
        if reason is not None:
            violations.append(
                BoundaryViolation(path, f"reachable history contains an unsafe path: {reason}")
            )
            continue
        if policy.is_forbidden(path):
            violations.append(
                BoundaryViolation(path, "reachable history contains a restricted path")
            )
    return _dedupe(violations)


def check_archive_members(
    names: Iterable[str], policy: ForbiddenPathPolicy = EMPTY_POLICY
) -> list[BoundaryViolation]:
    violations = []
    for name in names:
        reason = _unsafe_path_reason(name)
        if reason is not None:
            violations.append(BoundaryViolation(name or "<empty>", f"archive member {reason}"))
            continue
        normalized = name.rstrip("/")
        if policy.is_forbidden(normalized):
            violations.append(BoundaryViolation(normalized, "archive member is restricted"))
    return _dedupe(violations)


def check_workflow_documents(documents: Mapping[str, str]) -> list[BoundaryViolation]:
    """Fail closed on artifact uploads until an exact-member allowlist exists."""

    violations = []
    for path, text in documents.items():
        if "actions/upload-artifact" in text.casefold():
            violations.append(
                BoundaryViolation(
                    path,
                    "workflow uses actions/upload-artifact without an exact-member allowlist",
                )
            )
    return _dedupe(violations)


def _effective_rules(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _ignore_rules(text: str) -> set[str]:
    return set(_effective_rules(text))


def _dockerignore_excludes_readme(rules: set[str]) -> bool:
    if rules & DOCKERIGNORE_README_INCLUDERS:
        return False
    return bool(rules & DOCKERIGNORE_README_EXCLUDERS)


def _dockerignore_reexcludes_required_path(rules: Sequence[str]) -> list[str]:
    reexcluded = []
    for rule in rules:
        if rule.startswith("!"):
            continue
        normalized = rule.rstrip("/").lstrip("/")
        if normalized in DOCKERIGNORE_REQUIRED_INCLUDE_PATHS:
            reexcluded.append(rule)
    return reexcluded


def check_ignore_documents(gitignore: str, dockerignore: str) -> list[BoundaryViolation]:
    """Check generic public ignore structure without revealing local names."""

    violations = []
    git_rules = _ignore_rules(gitignore)
    docker_rules = _ignore_rules(dockerignore)
    docker_effective = _effective_rules(dockerignore)
    for rule in sorted(PUBLIC_GITIGNORE_REQUIRED_RULES - git_rules):
        violations.append(BoundaryViolation(".gitignore", f"missing public safety rule: {rule}"))
    for rule in sorted(DOCKERIGNORE_REQUIRED_INCLUDE_RULES - docker_rules):
        violations.append(
            BoundaryViolation(".dockerignore", f"missing build-context allowlist rule: {rule}")
        )
    if not docker_effective or docker_effective[0] != DOCKERIGNORE_DENY_ALL_RULE:
        violations.append(
            BoundaryViolation(
                ".dockerignore",
                "build context must deny all paths by default before allowlisting inputs",
            )
        )
    for rule in _dockerignore_reexcludes_required_path(docker_effective):
        violations.append(
            BoundaryViolation(
                ".dockerignore",
                f"build-context allowlist rule is excluded again: {rule}",
            )
        )
    if _dockerignore_excludes_readme(docker_rules):
        violations.append(
            BoundaryViolation(".dockerignore", "docker build context must keep README.md available")
        )
    return violations


def _run_git(root: Path, args: Sequence[str]) -> bytes:
    command = ["git", "-C", str(root), "-c", "core.quotePath=false", *args]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:
        raise BoundaryError(f"unable to run git: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        detail = stderr.splitlines()[-1] if stderr else f"exit {result.returncode}"
        raise BoundaryError(f"git {args[0]} failed: {detail}")
    return result.stdout


def _tracked_paths(root: Path) -> list[str]:
    output = _run_git(root, ["ls-files", "-z"])
    return [item.decode("utf-8", errors="replace") for item in output.split(b"\0") if item]


def _reachable_history_lines(root: Path) -> list[str]:
    output = _run_git(root, ["rev-list", "--objects", "--all"])
    return output.decode("utf-8", errors="replace").splitlines()


def _archive_member_names(root: Path) -> list[str]:
    data = _run_git(root, ["archive", "--format=tar", "HEAD"])
    names = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive.getmembers():
            names.append(member.name)
    return names


def _read_text(root: Path, relative_path: str) -> str:
    path = root / relative_path
    if path.is_symlink():
        raise BoundaryError(f"tracked path is a symbolic link: {relative_path}")
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise BoundaryError(f"tracked path escapes the repository: {relative_path}")
    return resolved_path.read_text(encoding="utf-8", errors="replace")


def _optional_text(root: Path, relative_path: str) -> str:
    path = root / relative_path
    if path.is_symlink() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _dedupe(violations: Iterable[BoundaryViolation]) -> list[BoundaryViolation]:
    unique = {(violation.path, violation.reason) for violation in violations}
    return [BoundaryViolation(path, reason) for path, reason in sorted(unique)]


def verify_repository(
    root: Path, policy: ForbiddenPathPolicy = EMPTY_POLICY
) -> list[BoundaryViolation]:
    """Run every path-only boundary check against a Git working tree."""

    root = root.resolve()
    tracked = _tracked_paths(root)
    violations = []
    violations.extend(check_tracked_paths(tracked, policy))
    violations.extend(check_history_paths(_reachable_history_lines(root), policy))
    violations.extend(check_archive_members(_archive_member_names(root), policy))

    for path in tracked:
        if policy.is_forbidden(path):
            continue
        if PurePosixPath(path).suffix.casefold() not in PUBLIC_DOCUMENT_SUFFIXES:
            continue
        if (root / path).is_symlink():
            violations.append(BoundaryViolation(path, "tracked public document is a symbolic link"))
            continue
        violations.extend(check_public_document(path, _read_text(root, path), policy))

    workflow_documents = {}
    for path in tracked:
        if policy.is_forbidden(path):
            continue
        if not path.startswith(WORKFLOW_DIRECTORY):
            continue
        if PurePosixPath(path).suffix.casefold() not in WORKFLOW_SUFFIXES:
            continue
        if (root / path).is_symlink():
            violations.append(BoundaryViolation(path, "tracked workflow is a symbolic link"))
            continue
        workflow_documents[path] = _read_text(root, path)
    violations.extend(check_workflow_documents(workflow_documents))

    gitignore_text = _optional_text(root, ".gitignore")
    dockerignore_text = _optional_text(root, ".dockerignore")
    for name in (".gitignore", ".dockerignore"):
        if (root / name).is_symlink():
            violations.append(BoundaryViolation(name, "ignore file is a symbolic link"))
    violations.extend(check_ignore_documents(gitignore_text, dockerignore_text))
    return _dedupe(violations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root to verify (default: current directory)",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help=(
            "optional JSON restricted-path policy file; "
            f"defaults to {POLICY_ENVIRONMENT_VARIABLE} when set"
        ),
    )
    arguments = parser.parse_args(argv)
    try:
        policy = (
            load_policy_file(arguments.policy)
            if arguments.policy is not None
            else policy_from_environment()
        )
        violations = verify_repository(arguments.root, policy)
    except (BoundaryError, PolicyError) as exc:
        print(f"publication boundary error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if violations:
        for violation in violations:
            print(f"boundary violation: {violation.format()}", file=sys.stderr)
        print(
            f"publication boundary failed with {len(violations)} violation(s).",
            file=sys.stderr,
        )
        return EXIT_VIOLATION
    print("publication boundary verified.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
