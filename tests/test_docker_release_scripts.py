from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import urllib.error
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
REPOSITORY = "shareur/TGHarbor"
REF = "refs/heads/master"
SHA = "8f4d8563e0d3eaf89a9081961ba6f9b76f12add9"
OTHER_SHA = "b" * 40
TAG = "v0.2.0"
TAG_OBJECT = "a" * 40
DEFAULT_BRANCH_SHA = "c" * 40
EPOCH = 1794073375
INDEX_DIGEST = "sha256:" + "e" * 64
CANDIDATE_TAG = "v0.2.3"
CANDIDATE_SHA = "2b9ed1f70860601759177758842c219f5126d096"
CANDIDATE_SHA_TAG = "sha-2b9ed1f70860"
LIVE_DIGEST = "sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560"
LIVE_REVISION = "13c5bc7645b9ba27ea9837be3697fa1388ffb44d"
LIVE_SHA_TAG = f"sha-{LIVE_REVISION[:12]}"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"tgharbor_release_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publication_policy = _load_script("publication_policy")
sys.modules["publication_policy"] = publication_policy
contract = _load_script("release_contract")
sys.modules["release_contract"] = contract
# The credential-free validator reuses the anonymous registry reader from the
# post-push verifier. Register that module name before loading the validator so
# the shared abstractions are never duplicated.
verify = _load_script("verify_docker_release")
sys.modules["verify_docker_release"] = verify
validate = _load_script("validate_docker_release")

RESTRICTED_POLICY = publication_policy.ForbiddenPathPolicy(
    directory_names=frozenset({"restricted-tree"}),
    basenames=frozenset({"governance-note.md"}),
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


def layer_blob_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def regular_member(name: str, payload: bytes = b"neutral\n") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    return info, payload


def directory_member(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, None


def symlink_member(name: str, target: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


def hardlink_member(name: str, target: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    return info, None


def special_member(name: str, member_type: bytes) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = member_type
    if member_type in {tarfile.CHRTYPE, tarfile.BLKTYPE}:
        info.devmajor = 1
        info.devminor = 3
    return info, None


def layer_archive(*members: tuple[tarfile.TarInfo, bytes | None], compress: bool = True) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, payload in members:
            archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    data = buffer.getvalue()
    return gzip.compress(data) if compress else data


PLATFORM_DIGESTS = {"amd64": digest("1"), "arm64": digest("2")}
CONFIG_DIGESTS = {"amd64": digest("3"), "arm64": digest("4")}
ATTESTATION_DIGESTS = {"amd64": digest("5"), "arm64": digest("6")}
PROVENANCE_BLOBS = {"amd64": digest("7"), "arm64": digest("8")}
SBOM_BLOBS = {"amd64": digest("9"), "arm64": digest("a")}


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"{}",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, _amount: int = -1) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


class SequenceOpener:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        del timeout
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeCatalog:
    def __init__(
        self,
        tags: list[validate.RegistryTag] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.tags = list(tags or ())
        self.error = error
        self.calls = 0

    def list_tags(self) -> tuple[validate.RegistryTag, ...]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return tuple(self.tags)


class FakeGitHub:
    def __init__(
        self,
        mapping: dict[str, Any],
        *,
        run_document: dict[str, Any] | None = None,
    ) -> None:
        self.mapping = mapping
        self.run_document = run_document
        self.paths: list[str] = []

    def get_json(self, path: str) -> Any:
        self.paths.append(path)
        if path in self.mapping:
            return copy.deepcopy(self.mapping[path])
        if self.run_document is not None and "/actions/workflows/ci.yml/runs?" in path:
            return copy.deepcopy(self.run_document)
        raise validate.ValidationError("GitHub API resource does not exist")


def remote_mapping(
    *,
    ref_type: str = "tag",
    target_type: str = "commit",
    tag: str = TAG,
    tag_name: str = TAG,
    commit: str = SHA,
    tag_object: str = TAG_OBJECT,
) -> dict[str, Any]:
    return {
        f"/repos/{REPOSITORY}/git/ref/tags/{tag}": {
            "ref": f"refs/tags/{tag}",
            "object": {"type": ref_type, "sha": tag_object},
        },
        f"/repos/{REPOSITORY}/git/tags/{tag_object}": {
            "tag": tag_name,
            "object": {"type": target_type, "sha": commit},
        },
    }


def automatic_payload(**run_overrides: Any) -> dict[str, Any]:
    run = {
        "id": 12345,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": TAG,
        "head_sha": SHA,
        "pull_requests": [],
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    }
    run.update(run_overrides)
    return {
        "action": "completed",
        "repository": {"full_name": REPOSITORY},
        "workflow_run": run,
    }


def manual_payload(tag: str = TAG) -> dict[str, Any]:
    return {
        "repository": {"full_name": REPOSITORY},
        "inputs": {"release_tag": tag},
    }


def successful_run(**overrides: Any) -> dict[str, Any]:
    run = {
        "id": 777,
        "run_attempt": 1,
        "run_number": 2,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": TAG,
        "head_sha": SHA,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    }
    run.update(overrides)
    return run


def reachability_mapping(
    *,
    status: str = "ahead",
    behind_by: int = 0,
    merge_base: str = SHA,
    default_branch: str = DEFAULT_BRANCH_SHA,
) -> dict[str, Any]:
    return {
        f"/repos/{REPOSITORY}/branches/master": {"commit": {"sha": default_branch}},
        f"/repos/{REPOSITORY}/compare/{SHA}...{default_branch}": {
            "status": status,
            "behind_by": behind_by,
            "ahead_by": 1,
            "merge_base_commit": {"sha": merge_base},
        },
    }


def full_api(
    *,
    mapping: dict[str, Any] | None = None,
    run_document: dict[str, Any] | None = None,
) -> FakeGitHub:
    combined = remote_mapping()
    combined.update(reachability_mapping())
    combined.update(mapping or {})
    return FakeGitHub(
        combined,
        run_document=run_document or {"total_count": 1, "workflow_runs": [successful_run()]},
    )


@pytest.mark.parametrize("tag", ["v0.0.0", "v0.2.0", "v1.2.3", "v10.20.30"])
def test_strict_stable_semver_accepts_only_required_release_tags(tag: str) -> None:
    semver = contract.parse_stable_tag(tag)
    assert semver.version == tag[1:]


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2",
        "v1.2.3.4",
        "v1.2.3-rc.1",
        "v1.2.3+build.1",
        " v1.2.3",
        "v1.2.3 ",
        "v1.2.3\n",
        "v١.2.3",
        "",
        None,
        123,
    ],
)
def test_strict_stable_semver_rejects_unsafe_tags(tag: object) -> None:
    with pytest.raises(contract.ContractError):
        contract.parse_stable_tag(tag)


@pytest.mark.parametrize("tag", ["0.0.0", "0.2.3", "10.20.30"])
def test_published_version_tag_accepts_only_canonical_major_minor_patch(tag: str) -> None:
    assert contract.parse_published_version_tag(tag).version == tag


@pytest.mark.parametrize(
    "tag",
    [
        "v0.2.3",
        "00.2.3",
        "0.02.3",
        "0.2.03",
        "0.2",
        "0.2.3.4",
        "0.2.3-rc.1",
        "0.2.3+build.1",
        "latest",
        "sha-13c5bc7645b9",
        "",
        None,
        123,
    ],
)
def test_published_version_tag_rejects_ambiguous_identities(tag: object) -> None:
    with pytest.raises(contract.ContractError):
        contract.parse_published_version_tag(tag)


def test_release_metadata_is_deterministic_and_derives_exact_tags() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    assert metadata.version == "0.2.0"
    assert metadata.minor_version == "0.2"
    assert metadata.created == "2026-11-07T17:42:55Z"
    assert metadata.image_tags == ("0.2.0", "0.2", "latest", "sha-8f4d8563e0d3")
    assert metadata.immutable_image_tags == ("0.2.0", "sha-8f4d8563e0d3")
    assert metadata.mutable_image_tags == ("0.2", "latest")
    assert metadata.image_references() == (
        "shareur/tgharbor:0.2.0",
        "shareur/tgharbor:0.2",
        "shareur/tgharbor:latest",
        "shareur/tgharbor:sha-8f4d8563e0d3",
    )
    assert contract.derive_release_metadata(TAG, SHA, EPOCH) == metadata
    with pytest.raises(contract.ContractError):
        contract.derive_release_metadata(TAG, SHA, True)


def test_workflow_pins_build_context_and_publish_source_to_validated_commits() -> None:
    workflow = (ROOT / ".github" / "workflows" / "docker-release.yml").read_text(encoding="utf-8")
    validate_job, publish_job = workflow.split("    publish:", 1)
    assert "default_branch_sha: ${{ steps.resolve.outputs.default_branch_sha }}" in validate_job
    assert "ref: ${{ needs.validate.outputs.default_branch_sha }}" in publish_job
    assert "refs/heads/master" not in publish_job
    assert (
        "context: https://github.com/shareur/TGHarbor.git#${{ needs.validate.outputs.release_sha }}"
        in publish_job
    )
    assert (
        "refs/tags/${{ needs.validate.outputs.release_tag }}"
        not in publish_job.split("context:", 1)[1].split("\n", 1)[0]
    )
    assert "TGHARBOR_IMAGE_REVISION=${{ needs.validate.outputs.release_sha }}" in publish_job


def test_github_outputs_are_single_line_and_injection_safe(tmp_path: Path) -> None:
    output = tmp_path / "output"
    contract.write_github_outputs(
        output, {"release_tag": TAG, "release_sha": SHA, "count": 4, "ok": True}
    )
    assert output.read_text(encoding="utf-8").splitlines() == [
        f"release_tag={TAG}",
        f"release_sha={SHA}",
        "count=4",
        "ok=true",
    ]
    with pytest.raises(contract.ContractError):
        contract.write_github_outputs(output, {"release_tag": "v0.2.0\ninjected=yes"})
    with pytest.raises(contract.ContractError):
        contract.write_github_outputs(output, {"bad-key": "value"})
    with pytest.raises(contract.ContractError):
        contract.write_github_outputs(output, {"bad": object()})
    assert output.read_text(encoding="utf-8").count("injected") == 0


def test_automatic_event_accepts_only_completed_tag_push() -> None:
    event = validate.parse_automatic_event(automatic_payload(), repository=REPOSITORY, ref=REF)
    assert event.mode == "workflow_run"
    assert event.tag == TAG
    assert event.sha == SHA
    assert event.ci_run_id == 12345


@pytest.mark.parametrize(
    "mutation",
    [
        {"action": "requested"},
        {"workflow_run": {"name": "Other CI"}},
        {"workflow_run": {"path": ".github/workflows/other.yml"}},
        {"workflow_run": {"event": "pull_request"}},
        {"workflow_run": {"status": "in_progress"}},
        {"workflow_run": {"conclusion": "failure"}},
        {"workflow_run": {"head_branch": "0.2.0"}},
        {"workflow_run": {"head_branch": "v01.2.3"}},
        {"workflow_run": {"head_sha": "abc"}},
        {"workflow_run": {"repository": {"full_name": "evil/repo"}}},
        {"workflow_run": {"head_repository": {"full_name": "evil/repo"}}},
        {"workflow_run": {"pull_requests": [{"number": 1}]}},
        {"repository": {"full_name": "evil/repo"}},
    ],
)
def test_automatic_event_rejects_forged_or_non_tag_runs(mutation: dict[str, Any]) -> None:
    payload = automatic_payload()
    for key, value in mutation.items():
        if key in {"action", "repository"}:
            payload[key] = value
        else:
            payload["workflow_run"].update(value)
    with pytest.raises(validate.ValidationError):
        validate.parse_automatic_event(payload, repository=REPOSITORY, ref=REF)


def test_automatic_event_rejects_non_default_branch_context() -> None:
    with pytest.raises(validate.ValidationError):
        validate.parse_automatic_event(
            automatic_payload(), repository=REPOSITORY, ref="refs/heads/dev"
        )
    with pytest.raises(validate.ValidationError):
        validate.parse_automatic_event(automatic_payload(), repository="evil/repo", ref=REF)


def test_manual_event_requires_protected_default_branch_and_exact_input() -> None:
    event = validate.parse_manual_event(
        manual_payload(), repository=REPOSITORY, ref=REF, environ={}
    )
    assert event.mode == "workflow_dispatch"
    assert event.tag == TAG
    assert event.sha == ""
    with pytest.raises(validate.ValidationError):
        validate.parse_manual_event(
            manual_payload(), repository=REPOSITORY, ref="refs/heads/dev", environ={}
        )
    with pytest.raises(validate.ValidationError):
        validate.parse_manual_event(
            manual_payload("v1.2.3-rc.1"), repository=REPOSITORY, ref=REF, environ={}
        )
    with pytest.raises(validate.ValidationError):
        validate.parse_manual_event(
            manual_payload("v0.3.0"),
            repository=REPOSITORY,
            ref=REF,
            environ={"INPUT_RELEASE_TAG": TAG},
        )
    with pytest.raises(validate.ValidationError):
        validate.parse_manual_event({"inputs": {}}, repository=REPOSITORY, ref=REF, environ={})


def test_remote_tag_must_be_annotated_and_point_directly_to_a_commit() -> None:
    remote = validate.resolve_remote_tag(FakeGitHub(remote_mapping()), TAG)
    assert remote.tag_object_sha == TAG_OBJECT
    assert remote.commit_sha == SHA

    with pytest.raises(validate.ValidationError):
        validate.resolve_remote_tag(FakeGitHub(remote_mapping(ref_type="commit")), TAG)
    with pytest.raises(validate.ValidationError):
        validate.resolve_remote_tag(FakeGitHub(remote_mapping(target_type="tag")), TAG)
    with pytest.raises(validate.ValidationError):
        validate.resolve_remote_tag(FakeGitHub(remote_mapping(tag_name="v0.2.1")), TAG)
    with pytest.raises(validate.ValidationError):
        validate.resolve_remote_tag(FakeGitHub({}), TAG)


def test_automatic_resolution_rejects_moved_tag_sha(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(automatic_payload(head_sha=OTHER_SHA)), encoding="utf-8")
    with pytest.raises(validate.ValidationError):
        validate._resolve(
            repository=REPOSITORY,
            ref=REF,
            event_name="workflow_run",
            event_path=event_path,
            output_path=tmp_path / "output",
            environ={},
            api=full_api(),
        )


def test_manual_resolution_finds_exact_successful_tag_ci_run(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(manual_payload()), encoding="utf-8")
    output = tmp_path / "output"
    event = validate._resolve(
        repository=REPOSITORY,
        ref=REF,
        event_name="workflow_dispatch",
        event_path=event_path,
        output_path=output,
        environ={},
        api=full_api(),
    )
    assert event.sha == SHA
    rendered = output.read_text(encoding="utf-8")
    assert "release_tag=v0.2.0" in rendered
    assert "ci_run_id=777" in rendered
    assert f"default_branch_sha={DEFAULT_BRANCH_SHA}" in rendered
    assert "default_branch_sha=refs/heads/master" not in rendered


def test_automatic_resolution_outputs_the_exact_default_branch_sha(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(automatic_payload()), encoding="utf-8")
    output = tmp_path / "output"
    validate._resolve(
        repository=REPOSITORY,
        ref=REF,
        event_name="workflow_run",
        event_path=event_path,
        output_path=output,
        environ={},
        api=full_api(),
    )
    rendered = output.read_text(encoding="utf-8")
    assert f"default_branch_sha={DEFAULT_BRANCH_SHA}" in rendered
    assert f"release_sha={SHA}" in rendered


def test_manual_resolution_rejects_missing_or_failed_ci(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(manual_payload()), encoding="utf-8")
    failed = {"total_count": 1, "workflow_runs": [successful_run(conclusion="failure")]}
    with pytest.raises(validate.ValidationError):
        validate._resolve(
            repository=REPOSITORY,
            ref=REF,
            event_name="workflow_dispatch",
            event_path=event_path,
            output_path=tmp_path / "output",
            environ={},
            api=full_api(run_document=failed),
        )
    wrong_sha = {"total_count": 1, "workflow_runs": [successful_run(head_sha=OTHER_SHA)]}
    with pytest.raises(validate.ValidationError):
        validate._resolve(
            repository=REPOSITORY,
            ref=REF,
            event_name="workflow_dispatch",
            event_path=event_path,
            output_path=tmp_path / "output",
            environ={},
            api=full_api(run_document=wrong_sha),
        )


def test_find_successful_ci_rejects_wrong_branch_sha_repo_and_path() -> None:
    api = FakeGitHub({}, run_document={"total_count": 1, "workflow_runs": [successful_run()]})
    assert validate.find_successful_tag_ci(api, tag=TAG, sha=SHA)["id"] == 777
    for overrides in (
        {"head_branch": "v0.2.1"},
        {"head_sha": OTHER_SHA},
        {"path": ".github/workflows/other.yml"},
        {"name": "Other"},
        {"repository": {"full_name": "evil/repo"}},
        {"head_repository": {"full_name": "evil/repo"}},
        {"status": "in_progress"},
        {"event": "pull_request"},
    ):
        bad_api = FakeGitHub(
            {},
            run_document={
                "total_count": 1,
                "workflow_runs": [successful_run(**overrides)],
            },
        )
        with pytest.raises(validate.ValidationError):
            validate.find_successful_tag_ci(bad_api, tag=TAG, sha=SHA)


@pytest.mark.parametrize(
    "field",
    [
        "run_attempt",
        "run_number",
        "id",
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        "1",
        0,
        -1,
        True,
        None,
        2**63,
    ],
)
def test_find_successful_ci_rejects_malformed_run_fields(field: str, value: object) -> None:
    api = FakeGitHub(
        {},
        run_document={"total_count": 1, "workflow_runs": [successful_run(**{field: value})]},
    )
    with pytest.raises(validate.ValidationError):
        validate.find_successful_tag_ci(api, tag=TAG, sha=SHA)


def test_default_branch_reachability_requires_ancestor_not_divergence() -> None:
    assert (
        validate.assert_default_branch_reachability(FakeGitHub(reachability_mapping()), SHA)
        == DEFAULT_BRANCH_SHA
    )
    assert (
        validate.assert_default_branch_reachability(
            FakeGitHub(reachability_mapping(status="identical", merge_base=SHA)), SHA
        )
        == DEFAULT_BRANCH_SHA
    )
    for mapping in (
        reachability_mapping(status="diverged"),
        reachability_mapping(behind_by=1),
        reachability_mapping(merge_base=OTHER_SHA),
        {},
    ):
        with pytest.raises(validate.ValidationError):
            validate.assert_default_branch_reachability(FakeGitHub(mapping), SHA)


def _make_release_repo(
    path: Path,
    *,
    pyproject_version: str = "0.2.0",
    runtime_version: str = "0.2.0",
) -> str:
    package = path / "src" / "tgharbor"
    package.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "tgharbor"\nversion = "{pyproject_version}"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(f'__version__ = "{runtime_version}"\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "release"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_release_checkout_binds_tag_project_runtime_and_commit_time(tmp_path: Path) -> None:
    repo = tmp_path / "release"
    repo.mkdir()
    sha = _make_release_repo(repo)
    metadata = validate.verify_release_checkout(repo, expected_tag=TAG, expected_sha=sha)
    assert metadata.version == "0.2.0"
    assert metadata.sha == sha
    assert metadata.created.endswith("Z")


def test_release_checkout_rejects_version_or_head_mismatch(tmp_path: Path) -> None:
    mismatch_runtime = tmp_path / "runtime"
    mismatch_runtime.mkdir()
    runtime_sha = _make_release_repo(mismatch_runtime, runtime_version="0.1.9")
    with pytest.raises(validate.ValidationError):
        validate.verify_release_checkout(
            mismatch_runtime, expected_tag=TAG, expected_sha=runtime_sha
        )

    mismatch_project = tmp_path / "project"
    mismatch_project.mkdir()
    project_sha = _make_release_repo(mismatch_project, pyproject_version="0.1.9")
    with pytest.raises(validate.ValidationError):
        validate.verify_release_checkout(
            mismatch_project, expected_tag=TAG, expected_sha=project_sha
        )

    wrong_tag = tmp_path / "tag"
    wrong_tag.mkdir()
    tag_sha = _make_release_repo(wrong_tag)
    with pytest.raises(validate.ValidationError):
        validate.verify_release_checkout(wrong_tag, expected_tag="v0.3.0", expected_sha=tag_sha)

    wrong_head = tmp_path / "head"
    wrong_head.mkdir()
    _make_release_repo(wrong_head)
    with pytest.raises(validate.ValidationError):
        validate.verify_release_checkout(wrong_head, expected_tag=TAG, expected_sha=OTHER_SHA)


def candidate_metadata() -> contract.ReleaseMetadata:
    return contract.derive_release_metadata(CANDIDATE_TAG, CANDIDATE_SHA, EPOCH)


def registry_tag(tag: str, digest_value: str = LIVE_DIGEST) -> validate.RegistryTag:
    return validate.RegistryTag(tag=tag, digest=digest_value)


def content_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def prior_release_graph(
    *,
    version: str | None = "0.2.1",
    revision: str | None = LIVE_REVISION,
    source: str | None = contract.CANONICAL_SOURCE_URL,
    architectures: tuple[str, ...] = ("amd64", "arm64"),
    duplicate_architecture: str | None = None,
    platform_overrides: dict[str, dict[str, str | None]] | None = None,
    index_media_type: str | None = "application/vnd.oci.image.index.v1+json",
    manifest_media_type: str | None = "application/vnd.oci.image.manifest.v1+json",
    config_media_type: str | None = "application/vnd.oci.image.config.v1+json",
    omit_config_digest: bool = False,
    config_digest_override: str | None = None,
    index_digest: str = LIVE_DIGEST,
) -> dict[str, Any]:
    """Build a real-shaped multi-platform prior image with mutable aliases."""

    manifests: dict[str, bytes] = {}
    blobs: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []
    for architecture in architectures:
        labels: dict[str, str] = {
            "org.opencontainers.image.title": "TGHarbor",
            "org.opencontainers.image.licenses": "Apache-2.0",
            "org.opencontainers.image.created": "2026-11-07T17:42:55Z",
        }
        if version is not None:
            labels["org.opencontainers.image.version"] = version
        if revision is not None:
            labels["org.opencontainers.image.revision"] = revision
        if source is not None:
            labels["org.opencontainers.image.source"] = source
        for key, value in (platform_overrides or {}).get(architecture, {}).items():
            if value is None:
                labels.pop(key, None)
            else:
                labels[key] = value
        config_body = json.dumps(
            {
                "architecture": architecture,
                "os": "linux",
                "config": {"Labels": labels},
                "history": [],
            }
        ).encode("utf-8")
        config_digest = content_digest(config_body)
        blobs[config_digest] = config_body
        config_descriptor: dict[str, Any] = {"size": len(config_body)}
        if config_media_type is not None:
            config_descriptor["mediaType"] = config_media_type
        if not omit_config_digest:
            config_descriptor["digest"] = config_digest_override or config_digest
        if config_digest_override is not None and config_digest_override != config_digest:
            blobs[config_digest_override] = config_body
        manifest: dict[str, Any] = {"config": config_descriptor, "layers": []}
        if manifest_media_type is not None:
            manifest["mediaType"] = manifest_media_type
        manifest_body = json.dumps(manifest).encode("utf-8")
        manifest_digest = content_digest(manifest_body)
        manifests[manifest_digest] = manifest_body
        entries.append(
            {
                "mediaType": manifest_media_type or "invalid",
                "digest": manifest_digest,
                "size": len(manifest_body),
                "platform": {"architecture": architecture, "os": "linux"},
            }
        )
        if duplicate_architecture == architecture:
            entries.append(copy.deepcopy(entries[-1]))
    index: dict[str, Any] = {
        "schemaVersion": 2,
        "manifests": entries,
    }
    if index_media_type is not None:
        index["mediaType"] = index_media_type
    manifests[index_digest] = json.dumps(index).encode("utf-8")
    return {
        "manifests": manifests,
        "blobs": blobs,
        "tags": {"latest": index_digest, "0.2": index_digest},
        "index_digest": index_digest,
    }


def prior_registry(**overrides: Any) -> FakeRegistry:
    return FakeRegistry(prior_release_graph(**overrides))


def test_registry_policy_accepts_current_live_forward_only_state() -> None:
    validate.validate_registry_tag_policy(
        candidate_metadata(),
        (
            registry_tag("0.2.1"),
            registry_tag(LIVE_SHA_TAG),
            registry_tag("latest"),
        ),
        prior_registry(),
    )


def test_registry_policy_accepts_absent_aliases_or_empty_first_release() -> None:
    assert validate.validate_registry_tag_policy(candidate_metadata(), (), prior_registry()) is None
    validate.validate_registry_tag_policy(
        candidate_metadata(),
        (
            registry_tag("0.2.1"),
            registry_tag(LIVE_SHA_TAG),
        ),
        prior_registry(),
    )


def test_registry_policy_accepts_latest_and_matching_minor_aliases() -> None:
    validate.validate_registry_tag_policy(
        candidate_metadata(),
        (
            registry_tag("0.2.1"),
            registry_tag(LIVE_SHA_TAG),
            registry_tag("0.2"),
            registry_tag("latest"),
        ),
        prior_registry(),
    )


def test_registry_policy_accepts_multiple_stable_tags_sharing_the_alias_digest() -> None:
    old_digest = digest("d")
    validate.validate_registry_tag_policy(
        candidate_metadata(),
        (
            registry_tag("0.1.9", old_digest),
            registry_tag("0.2.0", old_digest),
            registry_tag("0.2.1", old_digest),
            registry_tag(LIVE_SHA_TAG, old_digest),
            registry_tag("0.2", old_digest),
            registry_tag("latest", old_digest),
        ),
        prior_registry(index_digest=old_digest),
    )


def test_registry_policy_rejects_immutable_version_or_sha_tag_present() -> None:
    old_digest = digest("d")
    with pytest.raises(validate.ValidationError, match="immutable release tag already exists"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.3", old_digest),
                registry_tag("0.2.1", LIVE_DIGEST),
                registry_tag(LIVE_SHA_TAG),
            ),
            prior_registry(),
        )
    with pytest.raises(validate.ValidationError, match="immutable release tag already exists"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.1", LIVE_DIGEST),
                registry_tag(LIVE_SHA_TAG),
                registry_tag(CANDIDATE_SHA_TAG, LIVE_DIGEST),
            ),
            prior_registry(),
        )


def test_registry_policy_rejects_alias_digest_without_stable_or_sha_identity() -> None:
    with pytest.raises(validate.ValidationError, match="semantic-version identity"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag(LIVE_SHA_TAG),
                registry_tag("latest"),
            ),
            prior_registry(),
        )
    with pytest.raises(validate.ValidationError, match="sha-\\* identity"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.1"),
                registry_tag("latest"),
            ),
            prior_registry(),
        )


def test_registry_policy_rejects_newer_or_equal_alias_versions() -> None:
    newer_digest = digest("f")
    with pytest.raises(validate.ValidationError, match="must be higher"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.4", newer_digest),
                registry_tag("sha-ffffffffffff", newer_digest),
                registry_tag("latest", newer_digest),
            ),
            prior_registry(index_digest=newer_digest),
        )
    with pytest.raises(validate.ValidationError, match="immutable release tag already exists"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.3", newer_digest),
                registry_tag("sha-ffffffffffff", newer_digest),
                registry_tag("latest", newer_digest),
            ),
            prior_registry(index_digest=newer_digest),
        )


def test_registry_policy_rejects_global_rollback_even_without_aliases() -> None:
    newer_digest = digest("f")
    with pytest.raises(validate.ValidationError, match="highest published stable"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.4", newer_digest),
                registry_tag("sha-ffffffffffff", newer_digest),
            ),
            prior_registry(),
        )


def test_registry_policy_rejects_wrong_major_minor_for_minor_alias() -> None:
    wrong_digest = digest("f")
    with pytest.raises(validate.ValidationError, match="same major/minor"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.1.0", wrong_digest),
                registry_tag("sha-ffffffffffff", wrong_digest),
                registry_tag("0.2", wrong_digest),
            ),
            prior_registry(index_digest=wrong_digest, version="0.1.0"),
        )


def test_registry_policy_rejects_non_highest_and_rollback_alias_digests() -> None:
    old_digest = digest("d")
    newest_digest = digest("e")
    stable_and_sha = (
        registry_tag("0.2.0", old_digest),
        registry_tag("sha-dddddddddddd", old_digest),
        registry_tag("0.2.1", newest_digest),
        registry_tag("sha-eeeeeeeeeeee", newest_digest),
    )
    with pytest.raises(validate.ValidationError, match="non-highest or rollback"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (*stable_and_sha, registry_tag("latest", old_digest)),
            prior_registry(),
        )


def test_registry_policy_rejects_divergent_aliases() -> None:
    old_digest = digest("d")
    newest_digest = digest("e")
    with pytest.raises(validate.ValidationError):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.1", old_digest),
                registry_tag("sha-dddddddddddd", old_digest),
                registry_tag("0.2.2", newest_digest),
                registry_tag("sha-eeeeeeeeeeee", newest_digest),
                registry_tag("0.2", old_digest),
                registry_tag("latest", newest_digest),
            ),
            prior_registry(),
        )


def test_registry_policy_rejects_malformed_or_duplicate_catalog_metadata() -> None:
    for tags in (
        (registry_tag("v0.2.1", LIVE_DIGEST),),
        (registry_tag("0.2.1-rc.1", LIVE_DIGEST),),
        (registry_tag("sha-13C5BC7645B9", LIVE_DIGEST),),
        (registry_tag("0.2.1", "not-a-digest"),),
        (registry_tag("0.2.1", LIVE_DIGEST), registry_tag("0.2.1", LIVE_DIGEST)),
        (object(),),
        "not-a-sequence",
    ):
        with pytest.raises(validate.ValidationError):
            validate.validate_registry_tag_policy(candidate_metadata(), tags, prior_registry())


def test_registry_policy_rejects_alias_image_label_mismatches() -> None:
    alias_catalog = (
        registry_tag("0.2.1"),
        registry_tag(LIVE_SHA_TAG),
        registry_tag("latest"),
    )
    version_label = "org.opencontainers.image.version"
    revision_label = "org.opencontainers.image.revision"
    cases = [
        (prior_registry(version="0.2.0"), "highest stable tag"),
        (prior_registry(version=None), "highest stable tag"),
        (prior_registry(source="https://evil.example/TGHarbor"), "canonical source"),
        (prior_registry(source=None), "canonical source"),
        (prior_registry(revision=OTHER_SHA), "sha-\\* tag"),
        (prior_registry(revision=None), "revision"),
        (prior_registry(revision="a" * 40), "sha-\\* tag"),
        (prior_registry(index_digest=digest("f")), "not its catalog digest"),
    ]
    for registry, message in cases:
        with pytest.raises(validate.ValidationError, match=message):
            validate.validate_registry_tag_policy(candidate_metadata(), alias_catalog, registry)

    revision_b = "b" * 40
    divergent_tags = (
        *alias_catalog,
        registry_tag(f"sha-{revision_b[:12]}"),
    )
    with pytest.raises(validate.ValidationError, match="platform images disagree"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            divergent_tags,
            prior_registry(platform_overrides={"arm64": {revision_label: revision_b}}),
        )

    with pytest.raises(validate.ValidationError):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            alias_catalog,
            prior_registry(platform_overrides={"amd64": {version_label: 123}}),
        )


def test_registry_policy_rejects_unsupported_or_malformed_prior_image_shapes() -> None:
    alias_catalog = (
        registry_tag("0.2.1"),
        registry_tag(LIVE_SHA_TAG),
        registry_tag("latest"),
    )
    cases = [
        prior_registry(architectures=("amd64",)),
        prior_registry(duplicate_architecture="amd64"),
        prior_registry(index_media_type="application/json"),
        prior_registry(manifest_media_type="application/json"),
        prior_registry(config_media_type="application/json"),
        prior_registry(omit_config_digest=True),
        prior_registry(config_digest_override=digest("f")),
    ]
    for registry in cases:
        with pytest.raises(validate.ValidationError):
            validate.validate_registry_tag_policy(candidate_metadata(), alias_catalog, registry)

    graph = prior_release_graph()
    index = json.loads(graph["manifests"][LIVE_DIGEST])
    platform_digest = index["manifests"][0]["digest"]
    graph["manifests"][platform_digest] += b" "
    with pytest.raises(validate.ValidationError, match="manifest digest"):
        validate.validate_registry_tag_policy(
            candidate_metadata(), alias_catalog, FakeRegistry(graph)
        )


def test_registry_policy_rejects_prior_image_registry_failures() -> None:
    class FailingRegistry:
        def get_manifest(self, _reference: str):
            raise verify.RegistryError("registry unavailable")

        def get_blob(self, _digest: str) -> bytes:
            raise verify.RegistryError("registry unavailable")

    with pytest.raises(validate.ValidationError, match="could not resolve the OCI identity"):
        validate.validate_registry_tag_policy(
            candidate_metadata(),
            (
                registry_tag("0.2.1"),
                registry_tag(LIVE_SHA_TAG),
                registry_tag("latest"),
            ),
            FailingRegistry(),
        )


def test_registry_reader_bounds_responses_and_reports_auth_and_network_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_body = json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json"}).encode()
    manifest_digest = content_digest(manifest_body)
    opener = SequenceOpener(
        [
            FakeResponse(200, json.dumps({"token": "anonymous-pull-token"}).encode()),
            FakeResponse(
                200,
                manifest_body,
                {
                    "Content-Type": "application/vnd.oci.image.index.v1+json",
                    "Docker-Content-Digest": manifest_digest,
                },
            ),
        ]
    )
    manifest = verify.RegistryClient(opener=opener).get_manifest("latest")
    assert manifest.content_type == "application/vnd.oci.image.index.v1+json"
    assert manifest.digest == manifest_digest
    assert opener.requests[1].get_header("Authorization") == "Bearer anonymous-pull-token"

    auth_opener = SequenceOpener(
        [urllib.error.HTTPError("https://auth.docker.io", 401, "denied", {}, None)]
    )
    with pytest.raises(verify.RegistryError):
        verify.RegistryClient(opener=auth_opener, attempts=1).get_manifest("latest")

    network_opener = SequenceOpener([urllib.error.URLError("offline")])
    with pytest.raises(verify.RegistryError):
        verify.RegistryClient(opener=network_opener, attempts=1, sleep=lambda _: None).get_manifest(
            "latest"
        )

    monkeypatch.setattr(verify, "MAX_MANIFEST_BYTES", 4)
    oversized_client = verify.RegistryClient(
        opener=SequenceOpener([FakeResponse(200, b"x" * 10)]), attempts=1
    )
    oversized_client._token = "pre-fetched-test-token"
    with pytest.raises(verify.RegistryError, match="size limit"):
        oversized_client.get_manifest("latest")


def test_docker_hub_existence_retries_transient_failures_without_credentials() -> None:
    opener = SequenceOpener(
        [
            urllib.error.HTTPError("https://hub.docker.com", 503, "busy", {}, None),
            FakeResponse(200),
        ]
    )
    sleeps: list[float] = []
    client = validate.DockerHubTags(opener=opener, sleep=sleeps.append)
    assert client.tag_exists("0.2.0") is True
    assert sleeps == [1.0]
    assert opener.requests[0].get_header("Authorization") is None

    opener_404 = SequenceOpener(
        [urllib.error.HTTPError("https://hub.docker.com", 404, "missing", {}, None)]
    )
    assert validate.DockerHubTags(opener=opener_404).tag_exists("0.2.0") is False

    opener_500 = SequenceOpener(
        [urllib.error.HTTPError("https://hub.docker.com", 500, "error", {}, None)] * 3
    )
    with pytest.raises(validate.ValidationError):
        validate.DockerHubTags(opener=opener_500, attempts=3, sleep=lambda _: None).tag_exists(
            "0.2.0"
        )


def test_docker_hub_existence_retries_network_errors() -> None:
    opener = SequenceOpener([urllib.error.URLError("offline"), FakeResponse(200)])
    assert validate.DockerHubTags(opener=opener, sleep=lambda _: None).tag_exists("0.2.0")


def tag_api_entry(tag: str, digest_value: str = LIVE_DIGEST) -> dict[str, str]:
    return {"name": tag, "digest": digest_value}


def tag_api_page(
    results: list[dict[str, str]],
    *,
    count: object | None = None,
    next_page: str | None = None,
) -> FakeResponse:
    payload = {
        "count": len(results) if count is None else count,
        "next": next_page,
        "results": results,
    }
    return FakeResponse(200, json.dumps(payload).encode("utf-8"))


def test_docker_hub_list_tags_returns_complete_catalog_without_credentials() -> None:
    opener = SequenceOpener(
        [
            tag_api_page(
                [
                    tag_api_entry("0.2.1"),
                    tag_api_entry(LIVE_SHA_TAG),
                    tag_api_entry("latest"),
                ]
            )
        ]
    )
    tags = validate.DockerHubTags(opener=opener).list_tags()
    assert {(record.tag, record.digest) for record in tags} == {
        ("0.2.1", LIVE_DIGEST),
        (LIVE_SHA_TAG, LIVE_DIGEST),
        ("latest", LIVE_DIGEST),
    }
    request = opener.requests[0]
    assert request.get_header("Authorization") is None
    assert "page_size=100" in request.full_url
    assert "page=1" in request.full_url


def test_docker_hub_live_catalog_flows_through_forward_only_policy() -> None:
    opener = SequenceOpener(
        [
            tag_api_page(
                [
                    tag_api_entry("0.2.1"),
                    tag_api_entry(LIVE_SHA_TAG),
                    tag_api_entry("latest"),
                ]
            )
        ]
    )
    tags = validate.DockerHubTags(opener=opener).list_tags()
    assert "0.2" not in {record.tag for record in tags}
    assert "0.2.3" not in {record.tag for record in tags}
    assert CANDIDATE_SHA_TAG not in {record.tag for record in tags}
    validate.validate_registry_tag_policy(candidate_metadata(), tags, prior_registry())


def test_docker_hub_list_tags_paginates_and_retries_transient_failures() -> None:
    opener = SequenceOpener(
        [
            urllib.error.HTTPError("https://hub.docker.com", 503, "busy", {}, None),
            tag_api_page(
                [tag_api_entry("0.2.1"), tag_api_entry(LIVE_SHA_TAG)],
                count=3,
                next_page="https://hub.docker.com/v2/repositories/shareur/tgharbor/tags?page=2",
            ),
            tag_api_page([tag_api_entry("latest")], count=3),
        ]
    )
    sleeps: list[float] = []
    tags = validate.DockerHubTags(opener=opener, sleep=sleeps.append).list_tags()
    assert len(tags) == 3
    assert sleeps == [1.0]
    assert "page=2" in opener.requests[2].full_url


def test_docker_hub_list_tags_fails_closed_on_inconsistent_metadata() -> None:
    valid_entry = tag_api_entry("0.2.1")
    cases = [
        [FakeResponse(200, b"not-json")],
        [FakeResponse(200, json.dumps({"count": True, "results": []}).encode("utf-8"))],
        [
            tag_api_page([valid_entry], count=3, next_page="page-2"),
            tag_api_page([], count=2),
        ],
        [tag_api_page([valid_entry], count=1, next_page="page-2")],
        [
            tag_api_page([valid_entry], count=3, next_page="page-2"),
            tag_api_page([], count=3),
        ],
        [
            tag_api_page([valid_entry], count=2, next_page="page-2"),
            tag_api_page([valid_entry], count=2),
        ],
        [tag_api_page([tag_api_entry("0.2.1", "not-a-digest")])],
        [urllib.error.HTTPError("https://hub.docker.com", 404, "missing", {}, None)],
    ]
    for outcomes in cases:
        with pytest.raises(validate.ValidationError):
            validate.DockerHubTags(
                opener=SequenceOpener(outcomes), attempts=1, sleep=lambda _: None
            ).list_tags()


def test_docker_hub_list_tags_bounds_pages_and_response_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validate, "MAX_TAG_PAGES", 1)
    with pytest.raises(validate.ValidationError, match="safe page limit"):
        validate.DockerHubTags(
            opener=SequenceOpener(
                [
                    tag_api_page(
                        [tag_api_entry("0.2.1")],
                        count=2,
                        next_page="page-2",
                    )
                ]
            ),
            sleep=lambda _: None,
        ).list_tags()

    monkeypatch.setattr(validate, "MAX_TAG_API_RESPONSE_BYTES", 4)
    with pytest.raises(validate.ValidationError, match="size limit"):
        validate.DockerHubTags(
            opener=SequenceOpener([tag_api_page([tag_api_entry("0.2.1")])]),
            sleep=lambda _: None,
        ).list_tags()


def test_pre_push_command_applies_forward_only_alias_policy(tmp_path: Path) -> None:
    repo = tmp_path / "release"
    repo.mkdir()
    sha = _make_release_repo(repo, pyproject_version="0.2.3", runtime_version="0.2.3")
    api = FakeGitHub(remote_mapping(tag=CANDIDATE_TAG, tag_name=CANDIDATE_TAG, commit=sha))
    catalog = FakeCatalog(
        [
            registry_tag("0.2.1"),
            registry_tag(LIVE_SHA_TAG),
            registry_tag("latest"),
        ]
    )
    metadata = validate._pre_push_command(
        release_tag=CANDIDATE_TAG,
        release_sha=sha,
        checkout=repo,
        api=api,
        catalog=catalog,
        registry=prior_registry(),
    )
    assert metadata.version == "0.2.3"
    assert metadata.sha_tag == f"sha-{sha[:12]}"
    assert catalog.calls == 1

    catalog_with_immutable = FakeCatalog(
        [
            registry_tag("0.2.1"),
            registry_tag(LIVE_SHA_TAG),
            registry_tag("latest"),
            registry_tag("0.2.3"),
        ]
    )
    with pytest.raises(validate.ValidationError, match="immutable release tag already exists"):
        validate._pre_push_command(
            release_tag=CANDIDATE_TAG,
            release_sha=sha,
            checkout=repo,
            api=api,
            catalog=catalog_with_immutable,
            registry=prior_registry(),
        )


def _provenance_statement(subject: str = PLATFORM_DIGESTS["amd64"]) -> dict[str, Any]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "pkg:docker/shareur/tgharbor", "digest": {"sha256": subject}}],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "externalParameters": {
                    "source": {
                        "uri": "https://github.com/shareur/TGHarbor.git",
                        "digest": {"sha1": SHA},
                    }
                }
            }
        },
    }


def _sbom_statement(subject: str = PLATFORM_DIGESTS["amd64"]) -> dict[str, Any]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "pkg:docker/shareur/tgharbor", "digest": {"sha256": subject}}],
        "predicateType": "https://spdx.dev/Document",
        "predicate": {"spdxVersion": "SPDX-2.3"},
    }


def release_graph(
    metadata: contract.ReleaseMetadata,
    *,
    include_provenance: bool = True,
    include_sbom: bool = True,
    labels_override: dict[str, str] | None = None,
    statement_subject_override: str | None = None,
    history: list[dict[str, Any]] | None = None,
    runtime_layers: dict[str, list[tuple[str, bytes]]] | None = None,
) -> dict[str, Any]:
    manifests: dict[str, bytes] = {}
    blobs: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []
    for architecture in ("amd64", "arm64"):
        labels = {
            "org.opencontainers.image.title": "TGHarbor",
            "org.opencontainers.image.version": metadata.version,
            "org.opencontainers.image.revision": metadata.sha,
            "org.opencontainers.image.source": metadata.source,
            "org.opencontainers.image.licenses": "Apache-2.0",
            "org.opencontainers.image.created": metadata.created,
        }
        labels.update(labels_override or {})
        config = {
            "architecture": architecture,
            "os": "linux",
            "config": {"Labels": labels},
            "history": history or [{"created_by": "COPY pyproject.toml README.md ./"}],
        }
        blobs[CONFIG_DIGESTS[architecture]] = json.dumps(config).encode("utf-8")
        layer_descriptors: list[dict[str, Any]] = []
        for media_type, payload in (runtime_layers or {}).get(architecture, []):
            layer_digest = layer_blob_digest(payload)
            blobs[layer_digest] = payload
            layer_descriptors.append(
                {
                    "mediaType": media_type,
                    "digest": layer_digest,
                    "size": len(payload),
                }
            )
        manifests[PLATFORM_DIGESTS[architecture]] = json.dumps(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": CONFIG_DIGESTS[architecture],
                    "size": 1,
                },
                "layers": layer_descriptors,
            }
        ).encode("utf-8")
        entries.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": PLATFORM_DIGESTS[architecture],
                "size": 1,
                "platform": {"architecture": architecture, "os": "linux"},
            }
        )
        subject = statement_subject_override or PLATFORM_DIGESTS[architecture]
        layers: list[dict[str, Any]] = []
        if include_provenance:
            provenance = _provenance_statement(subject)
            blobs[PROVENANCE_BLOBS[architecture]] = json.dumps(provenance).encode("utf-8")
            layers.append(
                {
                    "mediaType": "application/vnd.in-toto+json",
                    "digest": PROVENANCE_BLOBS[architecture],
                    "size": 1,
                    "annotations": {"in-toto.io/predicate-type": provenance["predicateType"]},
                }
            )
        if include_sbom:
            sbom = _sbom_statement(subject)
            blobs[SBOM_BLOBS[architecture]] = json.dumps(sbom).encode("utf-8")
            layers.append(
                {
                    "mediaType": "application/vnd.in-toto+json",
                    "digest": SBOM_BLOBS[architecture],
                    "size": 1,
                    "annotations": {"in-toto.io/predicate-type": sbom["predicateType"]},
                }
            )
        manifests[ATTESTATION_DIGESTS[architecture]] = json.dumps(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "subject": {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": PLATFORM_DIGESTS[architecture],
                    "size": 1,
                },
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": CONFIG_DIGESTS[architecture],
                    "size": 1,
                },
                "layers": layers,
            }
        ).encode("utf-8")
        entries.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": ATTESTATION_DIGESTS[architecture],
                "size": 1,
                "platform": {"architecture": "unknown", "os": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": PLATFORM_DIGESTS[architecture],
                },
            }
        )
    index = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "schemaVersion": 2,
        "manifests": entries,
    }
    manifests[INDEX_DIGEST] = json.dumps(index).encode("utf-8")
    tags = {tag: INDEX_DIGEST for tag in metadata.image_tags}
    return {"manifests": manifests, "blobs": blobs, "tags": tags}


class FakeRegistry:
    def __init__(self, graph: dict[str, Any]) -> None:
        self.graph = graph
        self.missing: dict[str, int] = {}
        self.calls: list[str] = []

    def get_manifest(self, reference: str) -> verify.RegistryManifest:
        self.calls.append(reference)
        if self.missing.get(reference, 0) > 0:
            self.missing[reference] -= 1
            raise verify.RegistryNotFound("not yet visible")
        if reference in self.graph["tags"]:
            digest = self.graph["tags"][reference]
            body = self.graph["manifests"][digest]
            return verify.RegistryManifest(digest, "application/vnd.oci.image.index.v1+json", body)
        if reference in self.graph["manifests"]:
            return verify.RegistryManifest(
                reference,
                "application/vnd.oci.image.manifest.v1+json",
                self.graph["manifests"][reference],
            )
        raise verify.RegistryNotFound("missing")

    def get_blob(self, digest: str) -> bytes:
        try:
            return self.graph["blobs"][digest]
        except KeyError as exc:
            raise verify.RegistryNotFound("missing blob") from exc


def test_index_classification_distinguishes_runtime_and_attestation_descriptors() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    graph = release_graph(metadata)
    index = json.loads(graph["manifests"][INDEX_DIGEST])
    runtime, attestations = verify.classify_index(index)
    assert set(runtime) == {"amd64", "arm64"}
    assert len(attestations) == 2
    assert all(
        descriptor["platform"] == {"architecture": "unknown", "os": "unknown"}
        for descriptor in attestations
    )


def test_index_classification_rejects_missing_platforms_and_mislabeled_descriptors() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    graph = release_graph(metadata)
    index = json.loads(graph["manifests"][INDEX_DIGEST])
    index["manifests"] = index["manifests"][:2]
    with pytest.raises(verify.VerificationError):
        verify.classify_index(index)

    graph = release_graph(metadata)
    index = json.loads(graph["manifests"][INDEX_DIGEST])
    index["manifests"][-1]["platform"] = {"architecture": "amd64", "os": "linux"}
    with pytest.raises(verify.VerificationError):
        verify.classify_index(index)

    graph = release_graph(metadata)
    index = json.loads(graph["manifests"][INDEX_DIGEST])
    index["manifests"].append(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest("f"),
            "size": 1,
            "platform": {"architecture": "unknown", "os": "unknown"},
        }
    )
    with pytest.raises(verify.VerificationError):
        verify.classify_index(index)


def _verify_graph(
    metadata: contract.ReleaseMetadata,
    graph: dict[str, Any],
    *,
    attempts: int = 2,
    sleep=None,
    policy: publication_policy.ForbiddenPathPolicy = publication_policy.EMPTY_POLICY,
) -> verify.ReleaseVerification:
    return verify.verify_registry_release(
        FakeRegistry(graph),
        tags=metadata.image_tags,
        expected_digest=INDEX_DIGEST,
        metadata=metadata,
        retry_attempts=attempts,
        retry_delay=0,
        sleep=sleep or (lambda _: None),
        policy=policy,
    )


def test_post_push_verification_still_requires_all_four_target_tags() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    graph = release_graph(metadata)
    with pytest.raises(verify.VerificationError, match="exactly the four required tags"):
        verify.verify_registry_release(
            FakeRegistry(graph),
            tags=metadata.image_tags[:3],
            expected_digest=INDEX_DIGEST,
            metadata=metadata,
            retry_attempts=1,
            retry_delay=0,
            sleep=lambda _: None,
        )


def test_registry_release_verifies_digests_labels_provenance_and_sbom() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    result = _verify_graph(metadata, release_graph(metadata))
    assert result.digest == INDEX_DIGEST
    assert set(result.images) == {"linux/amd64", "linux/arm64"}
    assert result.images["linux/amd64"].digest == PLATFORM_DIGESTS["amd64"]
    assert result.images["linux/arm64"].config_digest == CONFIG_DIGESTS["arm64"]
    assert result.provenance["linux/amd64"]
    assert result.sbom["linux/arm64"]
    assert len(result.attestation_digests) == 2


def test_registry_release_fails_closed_on_missing_attestations_or_label_mismatch() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    with pytest.raises(verify.VerificationError):
        _verify_graph(metadata, release_graph(metadata, include_provenance=False))
    with pytest.raises(verify.VerificationError):
        _verify_graph(metadata, release_graph(metadata, include_sbom=False))
    with pytest.raises(verify.VerificationError):
        _verify_graph(
            metadata,
            release_graph(
                metadata,
                labels_override={"org.opencontainers.image.version": "9.9.9"},
            ),
        )
    with pytest.raises(verify.VerificationError):
        _verify_graph(
            metadata,
            release_graph(metadata, statement_subject_override=digest("f")),
        )


def test_registry_release_rejects_forbidden_payload_history() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    graph = release_graph(metadata, history=[{"created_by": "COPY .env /app/.env"}])
    with pytest.raises(verify.VerificationError):
        _verify_graph(metadata, graph)
    assert (
        verify.assert_history_has_no_project_payload(
            [{"created_by": "COPY pyproject.toml README.md ./"}]
        )
        is None
    )


def test_registry_release_retries_eventual_consistency_and_fails_on_ambiguity() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    registry = FakeRegistry(release_graph(metadata))
    registry.missing[metadata.image_tags[0]] = 2
    sleeps: list[float] = []
    verify.verify_registry_release(
        registry,
        tags=metadata.image_tags,
        expected_digest=INDEX_DIGEST,
        metadata=metadata,
        retry_attempts=3,
        retry_delay=0,
        sleep=sleeps.append,
    )
    assert sleeps == [0.0, 0.0]

    class MismatchRegistry(FakeRegistry):
        def get_manifest(self, reference: str) -> verify.RegistryManifest:
            manifest = super().get_manifest(reference)
            if reference == metadata.image_tags[0]:
                return verify.RegistryManifest(digest("f"), manifest.content_type, manifest.body)
            return manifest

    with pytest.raises(verify.VerificationError):
        verify.verify_registry_release(
            MismatchRegistry(release_graph(metadata)),
            tags=metadata.image_tags,
            expected_digest=INDEX_DIGEST,
            metadata=metadata,
            retry_attempts=3,
            retry_delay=0,
            sleep=lambda _: None,
        )


def test_statement_binding_helpers_require_exact_subject_source_and_revision() -> None:
    statement = _provenance_statement()
    assert verify.statement_binds_subject(statement, PLATFORM_DIGESTS["amd64"])
    assert verify.statement_binds_subject(
        {"subject": [{"digest": {"sha256": PLATFORM_DIGESTS["amd64"].split(":")[1]}}]},
        PLATFORM_DIGESTS["amd64"],
    )
    assert not verify.statement_binds_subject(statement, PLATFORM_DIGESTS["arm64"])
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    assert verify.provenance_binds_source_and_revision(statement, metadata)
    wrong_source = copy.deepcopy(statement)
    wrong_source["predicate"]["buildDefinition"]["externalParameters"]["source"]["uri"] = (
        "https://evil.example/TGHarbor.git"
    )
    assert not verify.provenance_binds_source_and_revision(wrong_source, metadata)
    wrong_revision = copy.deepcopy(statement)
    wrong_revision["predicate"]["buildDefinition"]["externalParameters"]["source"]["digest"][
        "sha1"
    ] = OTHER_SHA
    assert not verify.provenance_binds_source_and_revision(wrong_revision, metadata)


def test_provenance_accepts_only_typed_buildkit_source_paths() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    statement = _provenance_statement()
    assert verify.provenance_binds_source_and_revision(statement, metadata)

    alternate = copy.deepcopy(statement)
    external = alternate["predicate"]["buildDefinition"]["externalParameters"]
    source = external.pop("source")
    external["config"] = {"source": source}
    assert verify.provenance_binds_source_and_revision(alternate, metadata)

    both = copy.deepcopy(alternate)
    both["predicate"]["buildDefinition"]["externalParameters"]["source"] = {
        "uri": contract.CANONICAL_SOURCE_URL,
        "digest": {"sha1": SHA},
    }
    assert not verify.provenance_binds_source_and_revision(both, metadata)


def test_provenance_rejects_build_argument_only_source_and_revision_strings() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    statement = _provenance_statement()
    external = statement["predicate"]["buildDefinition"]["externalParameters"]
    del external["source"]
    external["config"] = {
        "buildArgs": {
            "TGHARBOR_IMAGE_SOURCE": contract.CANONICAL_SOURCE_URL,
            "TGHARBOR_IMAGE_REVISION": SHA,
        }
    }
    statement["predicate"]["internalParameters"] = {
        "buildkit": {
            "source": {
                "uri": f"{contract.CANONICAL_SOURCE_URL}.git",
                "digest": {"sha1": SHA},
            }
        }
    }
    assert not verify.provenance_binds_source_and_revision(statement, metadata)


def test_provenance_rejects_malformed_structural_bindings() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    cases: list[dict[str, Any]] = []
    for mutate in (
        lambda statement: statement.update({"predicateType": "https://example.test/provenance"}),
        lambda statement: statement.update({"predicate": []}),
        lambda statement: statement["predicate"].update({"buildDefinition": []}),
        lambda statement: statement["predicate"]["buildDefinition"].update(
            {"externalParameters": []}
        ),
        lambda statement: statement["predicate"]["buildDefinition"]["externalParameters"][
            "source"
        ].update({"uri": 123}),
        lambda statement: statement["predicate"]["buildDefinition"]["externalParameters"][
            "source"
        ].update({"digest": {"sha1": 123}}),
        lambda statement: statement["predicate"]["buildDefinition"]["externalParameters"][
            "source"
        ].update({"digest": "sha1"}),
        lambda statement: statement["predicate"]["buildDefinition"]["externalParameters"].update(
            {"source": []}
        ),
    ):
        statement = copy.deepcopy(_provenance_statement())
        mutate(statement)
        cases.append(statement)
    alternate = copy.deepcopy(_provenance_statement())
    alternate_external = alternate["predicate"]["buildDefinition"]["externalParameters"]
    alternate_external["config"] = {"source": alternate_external.pop("source")}
    alternate_external["config"]["source"].update({"uri": 123})
    cases.append(alternate)
    for statement in cases:
        assert not verify.provenance_binds_source_and_revision(statement, metadata)


def test_default_smoke_script_has_no_project_restricted_names() -> None:
    script = verify.build_smoke_script()
    assert "restricted-tree" not in script
    assert "governance-note.md" not in script


def test_smoke_script_can_be_injected_with_restricted_policy() -> None:
    script = verify.build_smoke_script(RESTRICTED_POLICY)
    assert "restricted-tree" in script
    assert "governance-note.md" in script


def test_smoke_command_is_networkless_read_only_and_secretless() -> None:
    command = verify.smoke_test_command("linux/amd64", PLATFORM_DIGESTS["amd64"])
    text = " ".join(command)
    assert command[:2] == ["docker", "run"]
    assert "--network none" in text
    assert "--read-only" in command
    assert "--cap-drop ALL" in text
    assert "--security-opt no-new-privileges:true" in text
    assert "--entrypoint python" in text
    assert f"shareur/tgharbor@{PLATFORM_DIGESTS['amd64']}" in command
    for forbidden in (
        "--mount",
        "--volume",
        "-v",
        "--env",
        "--env-file",
        "--secret",
        "--privileged",
    ):
        assert forbidden not in command
    with pytest.raises(verify.VerificationError):
        verify.smoke_test_command("linux/s390x", PLATFORM_DIGESTS["amd64"])


def test_smoke_payload_requires_python_version_uid_directories_and_no_payload() -> None:
    payload = {
        "python": "3.15.2",
        "platform": "linux",
        "version": "0.2.0",
        "uid": 10001,
        "gid": 10001,
        "cwd": "/app",
        "directories": {
            "/app": True,
            "/app/src/tgharbor": True,
            "/config": True,
            "/data": True,
            "/downloads": True,
        },
        "forbidden": [],
    }
    assert verify.verify_smoke_payload(json.dumps(payload), expected_version="0.2.0") == payload
    for mutation in (
        {"python": "3.14.0"},
        {"platform": "darwin"},
        {"version": "0.1.0"},
        {"uid": 0},
        {"gid": 0},
        {"cwd": "/"},
        {"directories": {"/app": True, "/config": False}},
        {"forbidden": ["/app/.env"]},
    ):
        changed = copy.deepcopy(payload)
        changed.update(mutation)
        with pytest.raises(verify.VerificationError):
            verify.verify_smoke_payload(json.dumps(changed), expected_version="0.2.0")


def test_run_smoke_tests_executes_both_platform_digests() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    result = _verify_graph(metadata, release_graph(metadata))
    payload = json.dumps(
        {
            "python": "3.15.2",
            "platform": "linux",
            "version": "0.2.0",
            "uid": 10001,
            "gid": 10001,
            "cwd": "/app",
            "directories": {
                "/app": True,
                "/app/src/tgharbor": True,
                "/config": True,
                "/data": True,
                "/downloads": True,
            },
            "forbidden": [],
        }
    )
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    smoke = verify.run_smoke_tests(result.images, expected_version="0.2.0", runner=runner)
    assert set(smoke) == {"linux/amd64", "linux/arm64"}
    assert len(calls) == 2
    assert any("linux/amd64" in call for call in calls)
    assert any("linux/arm64" in call for call in calls)

    def failing_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    with pytest.raises(verify.VerificationError):
        verify.run_smoke_tests(result.images, expected_version="0.2.0", runner=failing_runner)


SENTINEL = "NEUTRAL_LAYER_SENTINEL_DO_NOT_LEAK"
RESTRICTED_LAYER_MEMBERS = (
    "app/restricted-tree/neutral.txt",
    "app/governance-note.md",
    "app/nested/governance-note.md",
)
EXISTING_FORBIDDEN_LAYER_MEMBERS = (
    "app/.env",
    "app/config.yaml",
    "app/compose.yaml",
    "app/tests/neutral.py",
    "app/.git/config",
    "app/session.session",
    "app/cache.sqlite3",
    "app/service.log",
)


@pytest.mark.parametrize("media_type", sorted(verify.SUPPORTED_LAYER_MEDIA_TYPES))
def test_layer_scan_accepts_clean_compressed_and_uncompressed_layers(media_type: str) -> None:
    data = layer_archive(
        directory_member("app"),
        regular_member("app/main.py"),
        symlink_member("app/link", "main.py"),
        hardlink_member("app/hard", "app/main.py"),
        compress=media_type in verify.GZIP_LAYER_MEDIA_TYPES,
    )
    assert verify.inspect_layer_archive(layer_blob_digest(data), media_type, data) is None


@pytest.mark.parametrize("member_name", RESTRICTED_LAYER_MEMBERS + EXISTING_FORBIDDEN_LAYER_MEMBERS)
def test_layer_scan_rejects_each_forbidden_path_class(member_name: str) -> None:
    payload = f"{SENTINEL}\n".encode()
    data = layer_archive(regular_member(member_name, payload))
    layer_digest = layer_blob_digest(data)
    with pytest.raises(verify.VerificationError) as caught:
        verify.inspect_layer_archive(
            layer_digest,
            verify.OCI_GZIP_LAYER_MEDIA_TYPE,
            data,
            RESTRICTED_POLICY,
        )
    message = str(caught.value)
    assert layer_digest in message
    assert member_name.lstrip("./") in message
    assert "forbidden" in message
    assert SENTINEL not in message


def test_layer_scan_detects_forbidden_member_deleted_by_later_whiteout() -> None:
    metadata = contract.derive_release_metadata(TAG, SHA, EPOCH)
    forbidden = layer_archive(regular_member("app/restricted-tree/neutral.txt", b"neutral\n"))
    whiteout = layer_archive(regular_member("app/.wh.restricted-tree"))
    graph = release_graph(
        metadata,
        runtime_layers={
            "amd64": [
                (verify.OCI_GZIP_LAYER_MEDIA_TYPE, forbidden),
                (verify.OCI_GZIP_LAYER_MEDIA_TYPE, whiteout),
            ]
        },
    )
    with pytest.raises(verify.VerificationError) as caught:
        _verify_graph(metadata, graph, policy=RESTRICTED_POLICY)
    message = str(caught.value)
    assert layer_blob_digest(forbidden) in message
    assert "app/restricted-tree/neutral.txt" in message


@pytest.mark.parametrize(
    ("member_name", "reason"),
    [
        ("../escape", "traverses"),
        ("app/../../escape", "traverses"),
        ("/etc/passwd", "absolute"),
        ("C:/Windows/system.ini", "absolute"),
        ("app\\restricted-tree\\neutral.txt", "backslash"),
    ],
)
def test_layer_scan_rejects_traversal_absolute_and_backslash_members(
    member_name: str, reason: str
) -> None:
    data = layer_archive(regular_member(member_name))
    layer_digest = layer_blob_digest(data)
    with pytest.raises(verify.VerificationError) as caught:
        verify.inspect_layer_archive(layer_digest, verify.OCI_GZIP_LAYER_MEDIA_TYPE, data)
    message = str(caught.value)
    assert layer_digest in message
    assert reason in message


@pytest.mark.parametrize(
    ("member_factory", "target", "reason"),
    [
        (symlink_member, "../../etc/passwd", "escapes"),
        (symlink_member, "/etc/passwd", "absolute"),
        (hardlink_member, "../outside", "escapes"),
        (hardlink_member, "/etc/passwd", "absolute"),
    ],
)
def test_layer_scan_rejects_unsafe_symlink_and_hardlink_targets(
    member_factory: Any, target: str, reason: str
) -> None:
    data = layer_archive(member_factory("app/link", target))
    layer_digest = layer_blob_digest(data)
    with pytest.raises(verify.VerificationError) as caught:
        verify.inspect_layer_archive(layer_digest, verify.OCI_GZIP_LAYER_MEDIA_TYPE, data)
    message = str(caught.value)
    assert layer_digest in message
    assert "app/link" in message
    assert reason in message


@pytest.mark.parametrize(
    ("member_factory", "member_name", "target"),
    [
        (symlink_member, "app/.env", "main.py"),
        (hardlink_member, "app/.env", "app/main.py"),
        (symlink_member, "app/config.yaml", "main.py"),
        (hardlink_member, "app/config.yaml", "app/main.py"),
        (symlink_member, "app/restricted-tree/link", "../main.py"),
        (hardlink_member, "app/restricted-tree/link", "app/main.py"),
    ],
)
def test_layer_scan_rejects_forbidden_link_member_paths(
    member_factory: Any, member_name: str, target: str
) -> None:
    data = layer_archive(
        regular_member("app/main.py"),
        member_factory(member_name, target),
    )
    layer_digest = layer_blob_digest(data)
    with pytest.raises(verify.VerificationError) as caught:
        verify.inspect_layer_archive(
            layer_digest,
            verify.OCI_GZIP_LAYER_MEDIA_TYPE,
            data,
            RESTRICTED_POLICY,
        )
    message = str(caught.value)
    assert layer_digest in message
    assert member_name in message
    assert "forbidden" in message


@pytest.mark.parametrize("member_factory", [symlink_member, hardlink_member])
def test_layer_scan_accepts_benign_link_member_paths(member_factory: Any) -> None:
    target = "main.py" if member_factory is symlink_member else "app/main.py"
    data = layer_archive(
        regular_member("app/main.py"),
        member_factory("app/link", target),
    )
    assert (
        verify.inspect_layer_archive(
            layer_blob_digest(data),
            verify.OCI_GZIP_LAYER_MEDIA_TYPE,
            data,
            RESTRICTED_POLICY,
        )
        is None
    )


@pytest.mark.parametrize(
    ("member_type", "reason"),
    [
        (tarfile.CHRTYPE, "device"),
        (tarfile.BLKTYPE, "device"),
        (tarfile.FIFOTYPE, "FIFO"),
    ],
)
def test_layer_scan_rejects_device_and_fifo_members(member_type: bytes, reason: str) -> None:
    data = layer_archive(special_member("dev/node", member_type))
    layer_digest = layer_blob_digest(data)
    with pytest.raises(verify.VerificationError) as caught:
        verify.inspect_layer_archive(layer_digest, verify.OCI_GZIP_LAYER_MEDIA_TYPE, data)
    message = str(caught.value)
    assert layer_digest in message
    assert "dev/node" in message
    assert reason in message


def test_layer_scan_rejects_unsupported_and_malformed_archives() -> None:
    compressed = layer_archive(regular_member("app/main.py"))
    compressed_digest = layer_blob_digest(compressed)
    with pytest.raises(verify.VerificationError) as unsupported:
        verify.inspect_layer_archive(
            compressed_digest, "application/vnd.oci.image.layer.v1.tar+zstd", compressed
        )
    assert "unsupported media type" in str(unsupported.value)

    with pytest.raises(verify.VerificationError) as not_gzip:
        verify.inspect_layer_archive(
            compressed_digest, verify.OCI_GZIP_LAYER_MEDIA_TYPE, b"not a gzip stream"
        )
    assert "gzip stream" in str(not_gzip.value)

    garbage_tar = b"not a tar archive"
    with pytest.raises(verify.VerificationError) as malformed_tar:
        verify.inspect_layer_archive(
            layer_blob_digest(garbage_tar), verify.OCI_TAR_LAYER_MEDIA_TYPE, garbage_tar
        )
    assert "readable tar" in str(malformed_tar.value)

    garbage_gzip = gzip.compress(b"not a tar archive")
    with pytest.raises(verify.VerificationError) as malformed_gzip:
        verify.inspect_layer_archive(
            layer_blob_digest(garbage_gzip), verify.OCI_GZIP_LAYER_MEDIA_TYPE, garbage_gzip
        )
    assert "readable gzip" in str(malformed_gzip.value)


def test_layer_diagnostics_contain_only_layer_path_and_reason() -> None:
    payload = f"{SENTINEL}\n".encode()
    data = layer_archive(regular_member("app/restricted-tree/neutral.txt", payload))
    layer_digest = layer_blob_digest(data)
    with pytest.raises(verify.VerificationError) as caught:
        verify.inspect_layer_archive(
            layer_digest,
            verify.OCI_GZIP_LAYER_MEDIA_TYPE,
            data,
            RESTRICTED_POLICY,
        )
    message = str(caught.value)
    assert message == (
        f"runtime layer {layer_digest} member app/restricted-tree/neutral.txt: "
        "forbidden restricted path"
    )
    assert SENTINEL not in message
    assert payload.decode("utf-8").strip() not in message
