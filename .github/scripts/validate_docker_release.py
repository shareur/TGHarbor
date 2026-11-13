"""Validate a Docker Hub release request before any registry login or push.

This script is intentionally credential-free with respect to Docker Hub. It
accepts only a strict stable annotated tag on the project source repository,
proves that its exact ``CI`` tag-push workflow run succeeded, binds the
checked-out release source to the peeled tag commit, and refuses to continue
when any target image tag already exists.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from release_contract import (
    CANONICAL_DEFAULT_BRANCH,
    CANONICAL_REPOSITORY,
    CI_WORKFLOW_NAME,
    CI_WORKFLOW_PATH,
    DOCKER_HUB_API_ROOT,
    GITHUB_API_ROOT,
    LATEST_TAG,
    ContractError,
    ReleaseMetadata,
    derive_release_metadata,
    parse_stable_tag,
    require_canonical_repository,
    require_full_sha,
    require_image_tag,
    write_github_outputs,
)

DEFAULT_BRANCH_REF = f"refs/heads/{CANONICAL_DEFAULT_BRANCH}"
EVENT_WORKFLOW_RUN = "workflow_run"
EVENT_WORKFLOW_DISPATCH = "workflow_dispatch"
GITHUB_API_VERSION = "2022-11-28"
MAX_API_RESPONSE_BYTES = 4_000_000
MAX_EVENT_BYTES = 1_000_000


# Contract violations and event validation failures share one public exception
# type so the CLI and tests cannot accidentally treat a rejected contract as a
# different, non-fatal error class.
ValidationError = ContractError


class JsonApi(Protocol):
    def get_json(self, path: str) -> Any:
        """Return a decoded JSON object for a GitHub REST path."""
        ...


class TagExistence(Protocol):
    def tag_exists(self, tag: str) -> bool:
        """Return whether a Docker Hub tag currently exists."""
        ...


@dataclass(frozen=True)
class ReleaseEvent:
    mode: str
    tag: str
    sha: str
    ci_run_id: int


@dataclass(frozen=True)
class RemoteTag:
    tag: str
    tag_object_sha: str
    commit_sha: str


def _require_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be a JSON object")
    return value


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _canonical_repository_from_event(value: object, *, label: str) -> str:
    mapping = _require_mapping(value, label=label)
    return require_canonical_repository(mapping.get("full_name"), label=f"{label}.full_name")


def _require_default_branch_ref(repository: object, ref: object) -> None:
    require_canonical_repository(repository)
    if ref != DEFAULT_BRANCH_REF:
        raise ValidationError(
            f"release automation must run from the protected default branch {CANONICAL_DEFAULT_BRANCH}"
        )


def parse_automatic_event(
    payload: object,
    *,
    repository: object,
    ref: object,
) -> ReleaseEvent:
    """Validate a completed ``workflow_run`` event for a tag-push CI run."""

    _require_default_branch_ref(repository, ref)
    event = _require_mapping(payload, label="workflow_run event")
    if event.get("action") != "completed":
        raise ValidationError("workflow_run action must be completed")
    if "repository" in event:
        _canonical_repository_from_event(event["repository"], label="event.repository")

    run = _require_mapping(event.get("workflow_run"), label="workflow_run")
    if run.get("name") != CI_WORKFLOW_NAME:
        raise ValidationError(f"CI workflow name must be {CI_WORKFLOW_NAME}")
    if run.get("path") != CI_WORKFLOW_PATH:
        raise ValidationError(f"CI workflow path must be {CI_WORKFLOW_PATH}")
    if run.get("event") != "push":
        raise ValidationError("CI workflow event must be a tag push")
    if run.get("status") != "completed":
        raise ValidationError("CI workflow run must be completed")
    if run.get("conclusion") != "success":
        raise ValidationError("CI workflow run must have succeeded")
    if run.get("pull_requests"):
        raise ValidationError("CI workflow run must not be a pull-request run")
    _canonical_repository_from_event(run.get("repository"), label="workflow_run.repository")
    _canonical_repository_from_event(
        run.get("head_repository"), label="workflow_run.head_repository"
    )

    tag = _require_text(run.get("head_branch"), label="workflow_run.head_branch")
    parse_stable_tag(tag)
    sha = require_full_sha(run.get("head_sha"), label="workflow_run.head_sha")
    run_id = _require_positive_int(run.get("id"), label="workflow_run.id")
    return ReleaseEvent(mode=EVENT_WORKFLOW_RUN, tag=tag, sha=sha, ci_run_id=run_id)


def parse_manual_event(
    payload: object,
    *,
    repository: object,
    ref: object,
    environ: Mapping[str, str],
) -> ReleaseEvent:
    """Validate a manual dispatch from the protected default branch."""

    _require_default_branch_ref(repository, ref)
    event = _require_mapping(payload, label="workflow_dispatch event")
    if "repository" in event:
        _canonical_repository_from_event(event["repository"], label="event.repository")
    inputs = _require_mapping(event.get("inputs"), label="workflow_dispatch.inputs")
    raw_tag = _require_text(inputs.get("release_tag"), label="release_tag input")
    parse_stable_tag(raw_tag)
    env_tag = environ.get("INPUT_RELEASE_TAG")
    if env_tag is not None and env_tag != raw_tag:
        raise ValidationError("release_tag input and INPUT_RELEASE_TAG disagree")
    return ReleaseEvent(mode=EVENT_WORKFLOW_DISPATCH, tag=raw_tag, sha="", ci_run_id=0)


def _request_url(base: str, path: str) -> str:
    return urllib.parse.urljoin(f"{base.rstrip('/')}/", path.lstrip("/"))


class GitHubApi:
    """Small read-only GitHub REST client with bounded transient retries."""

    def __init__(
        self,
        token: str | None,
        *,
        base_url: str = GITHUB_API_ROOT,
        opener: Callable[..., Any] = urllib.request.urlopen,
        attempts: int = 4,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._base_url = base_url
        self._opener = opener
        self._attempts = attempts
        self._timeout = timeout
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "TGHarbor-release-validation",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            _request_url(self._base_url, path), headers=self._headers(), method="GET"
        )
        last_status: int | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    status = getattr(response, "status", response.getcode())
                    body = response.read(MAX_API_RESPONSE_BYTES + 1)
                    if len(body) > MAX_API_RESPONSE_BYTES:
                        raise ValidationError("GitHub API response exceeded the size limit")
                    if status < 200 or status >= 300:
                        raise ValidationError(f"GitHub API returned HTTP {status}")
                    try:
                        return json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValidationError("GitHub API returned invalid JSON") from exc
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                if exc.code == 404:
                    raise ValidationError("GitHub API resource does not exist") from exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < self._attempts:
                    self._sleep(float(attempt))
                    continue
                raise ValidationError(f"GitHub API returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self._attempts:
                    self._sleep(float(attempt))
                    continue
                raise ValidationError("GitHub API request did not complete") from exc
        raise ValidationError(f"GitHub API request did not complete (last HTTP {last_status})")


def resolve_remote_tag(api: JsonApi, tag: object) -> RemoteTag:
    """Require an annotated tag whose single target is an exact commit."""

    semver = parse_stable_tag(tag)
    tag_text = f"v{semver.version}"
    encoded = urllib.parse.quote(tag_text, safe="")
    ref_path = f"/repos/{CANONICAL_REPOSITORY}/git/ref/tags/{encoded}"
    ref = _require_mapping(api.get_json(ref_path), label="tag ref")
    if ref.get("ref") != f"refs/tags/{tag_text}":
        raise ValidationError("remote tag name does not match the requested release tag")
    ref_object = _require_mapping(ref.get("object"), label="tag ref object")
    if ref_object.get("type") != "tag":
        raise ValidationError("release tag must be an annotated tag object, not a commit")
    tag_object_sha = require_full_sha(ref_object.get("sha"), label="tag object SHA")

    tag_object = _require_mapping(
        api.get_json(f"/repos/{CANONICAL_REPOSITORY}/git/tags/{tag_object_sha}"),
        label="annotated tag object",
    )
    if tag_object.get("tag") != tag_text:
        raise ValidationError("annotated tag object name does not match the release tag")
    target = _require_mapping(tag_object.get("object"), label="annotated tag target")
    if target.get("type") != "commit":
        raise ValidationError("annotated tag must point directly to a commit")
    commit_sha = require_full_sha(target.get("sha"), label="peeled commit SHA")
    return RemoteTag(tag=tag_text, tag_object_sha=tag_object_sha, commit_sha=commit_sha)


def assert_default_branch_reachability(api: JsonApi, commit_sha: object) -> str:
    """Require the peeled tag commit to be reachable from the default branch."""

    commit = require_full_sha(commit_sha, label="release commit")
    branch = _require_mapping(
        api.get_json(f"/repos/{CANONICAL_REPOSITORY}/branches/{CANONICAL_DEFAULT_BRANCH}"),
        label="default branch",
    )
    branch_commit = _require_mapping(branch.get("commit"), label="default branch commit")
    default_sha = require_full_sha(branch_commit.get("sha"), label="default branch commit SHA")
    compare = _require_mapping(
        api.get_json(f"/repos/{CANONICAL_REPOSITORY}/compare/{commit}...{default_sha}"),
        label="commit comparison",
    )
    if compare.get("status") not in {"ahead", "identical"}:
        raise ValidationError("release commit is not reachable from the default branch")
    merge_base = _require_mapping(compare.get("merge_base_commit"), label="merge base")
    if merge_base.get("sha") != commit:
        raise ValidationError("the default branch does not descend from the release commit")
    behind_by = compare.get("behind_by")
    if isinstance(behind_by, bool) or not isinstance(behind_by, int) or behind_by != 0:
        raise ValidationError("release commit is not an ancestor of the default branch")
    return default_sha


def ci_run_matches(run: object, *, tag: str, sha: str) -> bool:
    if not isinstance(run, Mapping):
        return False
    if run.get("name") != CI_WORKFLOW_NAME or run.get("path") != CI_WORKFLOW_PATH:
        return False
    if run.get("event") != "push" or run.get("status") != "completed":
        return False
    if run.get("conclusion") != "success":
        return False
    if run.get("head_branch") != tag or run.get("head_sha") != sha:
        return False
    repository = run.get("repository")
    if not isinstance(repository, Mapping):
        return False
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or full_name.casefold() != CANONICAL_REPOSITORY.casefold():
        return False
    head_repository = run.get("head_repository")
    if not isinstance(head_repository, Mapping):
        return False
    head_name = head_repository.get("full_name")
    return isinstance(head_name, str) and head_name.casefold() == CANONICAL_REPOSITORY.casefold()


def find_successful_tag_ci(api: JsonApi, *, tag: str, sha: str) -> Mapping[str, Any]:
    """Find the successful CI tag-push workflow run for an exact tag and SHA."""

    parse_stable_tag(tag)
    commit = require_full_sha(sha)
    matches: list[Mapping[str, Any]] = []
    page = 1
    while page <= 10:
        query = urllib.parse.urlencode(
            {"event": "push", "head_sha": commit, "per_page": "100", "page": str(page)}
        )
        document = _require_mapping(
            api.get_json(
                f"/repos/{CANONICAL_REPOSITORY}/actions/workflows/ci.yml/runs?{query}"
            ),
            label="CI runs",
        )
        runs = document.get("workflow_runs")
        if not isinstance(runs, list):
            raise ValidationError("CI runs response must contain workflow_runs")
        matches.extend(
            run for run in runs if ci_run_matches(run, tag=tag, sha=commit)
        )
        total_count = document.get("total_count")
        if not isinstance(total_count, int) or total_count < 0:
            raise ValidationError("CI runs response contains an invalid total_count")
        if len(runs) < 100 or page * 100 >= total_count:
            break
        page += 1
    if not matches:
        raise ValidationError(
            "no successful CI push run exists for the exact release tag and commit"
        )
    matches.sort(
        key=lambda run: (
            int(run.get("run_attempt", 0)),
            int(run.get("run_number", 0)),
            int(run.get("id", 0)),
        )
    )
    selected = matches[-1]
    _require_positive_int(selected.get("id"), label="CI run id")
    return selected


def _git_environment() -> dict[str, str]:
    environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    for key in ("PATH", "HOME", "XDG_CONFIG_HOME"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _git(release_root: Path, *arguments: str) -> str:
    command = ["git", "-C", str(release_root), *arguments]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=_git_environment(),
    )
    if result.returncode != 0:
        raise ValidationError(f"Git command failed: {' '.join(arguments)}")
    return result.stdout.strip()


_RUNTIME_VERSION_CODE = """
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import tgharbor

module = pathlib.Path(tgharbor.__file__).resolve()
if not module.is_relative_to(root) or module.name != "__init__.py":
    raise SystemExit(3)
print(tgharbor.__version__)
"""


def _runtime_package_version(release_root: Path) -> str:
    source = (release_root / "src").resolve()
    if not source.is_dir():
        raise ValidationError("release checkout is missing src/")
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }
    for key in ("PATH", "HOME"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _RUNTIME_VERSION_CODE, str(source)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise ValidationError("release checkout does not import its own tgharbor package")
    version = result.stdout.strip()
    if not version or "\n" in version:
        raise ValidationError("release checkout returned an invalid runtime version")
    return version


def verify_release_checkout(
    release_root: object,
    *,
    expected_tag: str,
    expected_sha: str,
    environ: Mapping[str, str] | None = None,
) -> ReleaseMetadata:
    """Bind checkout HEAD, tag version, project version, and runtime version."""

    if not isinstance(release_root, (str, os.PathLike)):
        raise ValidationError("release checkout path must be a filesystem path")
    root = Path(release_root).resolve()
    if not root.is_dir():
        raise ValidationError("release checkout path does not exist")
    tag = parse_stable_tag(expected_tag)
    commit = require_full_sha(expected_sha)
    head = _git(root, "rev-parse", "HEAD")
    if head != commit:
        raise ValidationError("release checkout HEAD does not match the tag commit")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValidationError("release checkout is not clean")

    pyproject_path = root / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError("release checkout has no readable pyproject version") from exc
    project_version = project.get("version") if isinstance(project, Mapping) else None
    if project_version != tag.version:
        raise ValidationError("tag version and pyproject.toml version differ")

    runtime_version = _runtime_package_version(root)
    if runtime_version != tag.version:
        raise ValidationError("tag version and runtime tgharbor.__version__ differ")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValidationError("release checkout changed while reading its version")

    epoch_text = _git(root, "show", "-s", "--format=%ct", commit)
    try:
        commit_epoch = int(epoch_text)
    except ValueError as exc:
        raise ValidationError("release commit timestamp is not an integer") from exc
    metadata = derive_release_metadata(expected_tag, commit, commit_epoch)
    if metadata.version != project_version or metadata.version != runtime_version:
        raise ValidationError("derived release version is not consistent")
    return metadata


class DockerHubTags:
    """Read-only Docker Hub tag existence checks; sends no credentials."""

    def __init__(
        self,
        *,
        base_url: str = DOCKER_HUB_API_ROOT,
        opener: Callable[..., Any] = urllib.request.urlopen,
        attempts: int = 4,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url
        self._opener = opener
        self._attempts = attempts
        self._timeout = timeout
        self._sleep = sleep

    def tag_exists(self, tag: str) -> bool:
        safe_tag = require_image_tag(tag)
        request = urllib.request.Request(
            f"{self._base_url.rstrip('/')}/{urllib.parse.quote(safe_tag, safe='')}",
            headers={
                "Accept": "application/json",
                "User-Agent": "TGHarbor-release-validation",
            },
            method="GET",
        )
        for attempt in range(1, self._attempts + 1):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    status = getattr(response, "status", response.getcode())
                    if 200 <= status < 300:
                        return True
                    raise ValidationError(f"Docker Hub tag lookup returned HTTP {status}")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return False
                if exc.code in {429, 500, 502, 503, 504} and attempt < self._attempts:
                    self._sleep(float(attempt))
                    continue
                raise ValidationError(f"Docker Hub tag lookup returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self._attempts:
                    self._sleep(float(attempt))
                    continue
                raise ValidationError("Docker Hub tag lookup did not complete") from exc
        raise ValidationError("Docker Hub tag lookup did not complete")


def ensure_target_tags_absent(tags: object, existence: TagExistence) -> None:
    if not isinstance(tags, (tuple, list)) or not tags:
        raise ValidationError("target image tags must be a non-empty sequence")
    present: list[str] = []
    for tag in tags:
        safe_tag = require_image_tag(tag)
        if existence.tag_exists(safe_tag):
            present.append(safe_tag)
    if present:
        raise ValidationError(
            "target image tag already exists; rerun verification or publish a new version "
            f"instead of overwriting it: {', '.join(present)}"
        )


def _load_event(path: object) -> Mapping[str, Any]:
    if not isinstance(path, (str, os.PathLike)) or not str(path):
        raise ValidationError("GitHub event path is required")
    event_path = Path(path)
    try:
        size = event_path.stat().st_size
        if size > MAX_EVENT_BYTES:
            raise ValidationError("GitHub event payload exceeded the size limit")
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError("GitHub event payload could not be read") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError("GitHub event payload is not valid JSON") from exc
    return _require_mapping(payload, label="GitHub event payload")


def _write_resolution_outputs(
    path: object,
    *,
    tag: str,
    sha: str,
    ci_run_id: int,
) -> None:
    semver = parse_stable_tag(tag)
    write_github_outputs(
        path,
        {
            "release_tag": tag,
            "release_sha": require_full_sha(sha),
            "release_version": semver.version,
            "minor_tag": semver.minor_version,
            "latest_tag": LATEST_TAG,
            "sha_tag": f"sha-{sha[:12]}",
            "ci_run_id": ci_run_id,
        },
    )


def _write_checkout_outputs(path: object, metadata: ReleaseMetadata) -> None:
    write_github_outputs(
        path,
        {
            "image_created": metadata.created,
            "image_source": metadata.source,
            "release_version": metadata.version,
            "minor_tag": metadata.minor_version,
            "latest_tag": LATEST_TAG,
            "sha_tag": metadata.sha_tag,
        },
    )


def _api_from_environment(environ: Mapping[str, str]) -> GitHubApi:
    return GitHubApi(environ.get("GITHUB_TOKEN"))


def _hub_from_environment(environ: Mapping[str, str]) -> DockerHubTags:
    del environ
    return DockerHubTags()


def _resolve(
    *,
    repository: str,
    ref: str,
    event_name: str,
    event_path: object,
    output_path: object,
    environ: Mapping[str, str],
    api: JsonApi,
) -> ReleaseEvent:
    payload = _load_event(event_path)
    if event_name == EVENT_WORKFLOW_RUN:
        event = parse_automatic_event(payload, repository=repository, ref=ref)
        tag = event.tag
        remote = resolve_remote_tag(api, tag)
        if remote.commit_sha != event.sha:
            raise ValidationError("tag-push CI SHA and remote peeled tag SHA differ")
        assert_default_branch_reachability(api, remote.commit_sha)
        sha = remote.commit_sha
        ci_run_id = event.ci_run_id
    elif event_name == EVENT_WORKFLOW_DISPATCH:
        manual = parse_manual_event(payload, repository=repository, ref=ref, environ=environ)
        tag = manual.tag
        remote = resolve_remote_tag(api, tag)
        assert_default_branch_reachability(api, remote.commit_sha)
        run = find_successful_tag_ci(api, tag=tag, sha=remote.commit_sha)
        ci_run_id = _require_positive_int(run.get("id"), label="CI run id")
        sha = remote.commit_sha
    else:
        raise ValidationError("only workflow_run and workflow_dispatch are supported")
    _write_resolution_outputs(output_path, tag=tag, sha=sha, ci_run_id=ci_run_id)
    return ReleaseEvent(mode=event_name, tag=tag, sha=sha, ci_run_id=ci_run_id)


def _verify_checkout_command(
    *,
    release_tag: str,
    release_sha: str,
    checkout: object,
    output_path: object,
    api: JsonApi,
) -> ReleaseMetadata:
    remote = resolve_remote_tag(api, release_tag)
    if remote.commit_sha != require_full_sha(release_sha):
        raise ValidationError("remote peeled tag SHA changed before checkout verification")
    metadata = verify_release_checkout(
        checkout, expected_tag=release_tag, expected_sha=release_sha
    )
    _write_checkout_outputs(output_path, metadata)
    return metadata


def _pre_push_command(
    *,
    release_tag: str,
    release_sha: str,
    checkout: object,
    api: JsonApi,
    existence: TagExistence,
) -> ReleaseMetadata:
    remote = resolve_remote_tag(api, release_tag)
    if remote.commit_sha != require_full_sha(release_sha):
        raise ValidationError("remote peeled tag SHA changed before registry login")
    metadata = verify_release_checkout(
        checkout, expected_tag=release_tag, expected_sha=release_sha
    )
    ensure_target_tags_absent(metadata.image_tags, existence)
    return metadata


def _build_parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a TGHarbor Docker Hub release request before login."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repository", default=environment.get("GITHUB_REPOSITORY"))
        subparser.add_argument("--ref", default=environment.get("GITHUB_REF"))
        subparser.add_argument("--output", default=environment.get("GITHUB_OUTPUT"))

    resolve = subparsers.add_parser("resolve", help="validate the event and release tag")
    add_common(resolve)
    resolve.add_argument("--event", default=environment.get("GITHUB_EVENT_NAME"))
    resolve.add_argument("--event-path", default=environment.get("GITHUB_EVENT_PATH"))

    checkout = subparsers.add_parser(
        "verify-checkout", help="bind the release checkout and emit image metadata"
    )
    add_common(checkout)
    checkout.add_argument("--checkout", required=True)
    checkout.add_argument("--release-tag", required=True)
    checkout.add_argument("--release-sha", required=True)

    pre_push = subparsers.add_parser(
        "pre-push", help="re-resolve the tag and reject existing image tags"
    )
    add_common(pre_push)
    pre_push.add_argument("--checkout", required=True)
    pre_push.add_argument("--release-tag", required=True)
    pre_push.add_argument("--release-sha", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    api_factory: Callable[[Mapping[str, str]], JsonApi] = _api_from_environment,
    hub_factory: Callable[[Mapping[str, str]], TagExistence] = _hub_from_environment,
) -> int:
    environment = os.environ if environ is None else environ
    arguments = _build_parser(environment).parse_args(argv)
    try:
        if arguments.command == "resolve":
            if not arguments.event or not arguments.event_path:
                raise ValidationError("event name and event path are required")
            event = _resolve(
                repository=arguments.repository,
                ref=arguments.ref,
                event_name=arguments.event,
                event_path=arguments.event_path,
                output_path=arguments.output,
                environ=environment,
                api=api_factory(environment),
            )
            print(f"Validated {event.tag} at {event.sha} from {event.mode}.")
            return 0
        if arguments.command == "verify-checkout":
            metadata = _verify_checkout_command(
                release_tag=arguments.release_tag,
                release_sha=arguments.release_sha,
                checkout=arguments.checkout,
                output_path=arguments.output,
                api=api_factory(environment),
            )
            print(
                f"Verified release checkout {metadata.tag} at {metadata.sha}; "
                f"image tags: {', '.join(metadata.image_tags)}."
            )
            return 0
        if arguments.command == "pre-push":
            metadata = _pre_push_command(
                release_tag=arguments.release_tag,
                release_sha=arguments.release_sha,
                checkout=arguments.checkout,
                api=api_factory(environment),
                existence=hub_factory(environment),
            )
            print(
                f"Pre-push validation passed for {metadata.tag}; "
                "no target Docker Hub tag exists."
            )
            return 0
        raise ValidationError("unknown command")
    except (ContractError, ValidationError) as exc:
        print(f"::error title=Release validation failed::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
