from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import venv
import zipfile
from collections.abc import Iterable, Sequence
from email.message import Message
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_ROOT = ROOT / "src" / "tgharbor"
PROJECT = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
VERSION = PROJECT["version"]
DIST_NAME = f"{PROJECT['name']}-{VERSION}"
DIST_INFO = f"{DIST_NAME}.dist-info"

# This reviewed manifest is intentionally independent of directory enumeration and
# build-tool selection. Adding a runtime module requires an explicit contract update.
APPROVED_RUNTIME_MEMBERS = frozenset(
    {
        "tgharbor/__init__.py",
        "tgharbor/main.py",
        "tgharbor/app/__init__.py",
        "tgharbor/app/bootstrap.py",
        "tgharbor/app/instance_lock.py",
        "tgharbor/config/__init__.py",
        "tgharbor/config/settings.py",
        "tgharbor/core/__init__.py",
        "tgharbor/core/errors.py",
        "tgharbor/core/interfaces.py",
        "tgharbor/core/job_api.py",
        "tgharbor/core/jobs.py",
        "tgharbor/core/library_api.py",
        "tgharbor/core/reconciliation.py",
        "tgharbor/core/worker.py",
        "tgharbor/persistence/__init__.py",
        "tgharbor/persistence/sqlite_queue.py",
        "tgharbor/providers/__init__.py",
        "tgharbor/storage/__init__.py",
        "tgharbor/storage/local.py",
        "tgharbor/telegram/__init__.py",
        "tgharbor/telegram/bot.py",
        "tgharbor/telegram/links.py",
        "tgharbor/telegram/provider.py",
        "tgharbor/telegram/sessions.py",
    }
)
SDIST_RUNTIME_MEMBERS = frozenset(f"src/{name}" for name in APPROVED_RUNTIME_MEMBERS)
SDIST_ROOT_INPUTS = frozenset(
    {"pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "hatch_build.py"}
)
SDIST_INCLUDE = frozenset(
    {
        "/src/tgharbor/**/*.py",
        *(f"/{name}" for name in SDIST_ROOT_INPUTS),
    }
)
SDIST_EXPECTED_MEMBERS = frozenset({*SDIST_ROOT_INPUTS, *SDIST_RUNTIME_MEMBERS, "PKG-INFO"})
WHEEL_EXPECTED_DIST_INFO = frozenset(
    {
        f"{DIST_INFO}/METADATA",
        f"{DIST_INFO}/WHEEL",
        f"{DIST_INFO}/entry_points.txt",
        f"{DIST_INFO}/licenses/LICENSE",
        f"{DIST_INFO}/RECORD",
    }
)

FORBIDDEN_BASENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "config.yaml",
    "config.yml",
    "config.local.yaml",
    "config.local.yml",
    "config.example.yaml",
    "compose.yaml",
    "compose.override.yaml",
    "compose.local.yaml",
    "compose.example.yaml",
    "agents.md",
    "agent.md",
    "handoff.md",
}
FORBIDDEN_DIRECTORY_PARTS = {
    ".git",
    ".github",
    "tests",
    "docs",
    "docs-local",
    "archive",
    "reference",
    "downloads",
    "download",
    "data",
    "dist",
    "build",
}
FORBIDDEN_SUFFIXES = (
    ".session",
    ".session-journal",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pyc",
    ".pyo",
)


class Artifacts(NamedTuple):
    source_sdist: Path
    rebuilt_sdist: Path
    direct_wheel: Path
    sdist_derived_wheel: Path


def _assert_unique_names(names: Sequence[str]) -> None:
    assert len(names) == len(set(names)), "archive contains duplicate members"


def _assert_safe_member_names(names: Iterable[str]) -> None:
    for name in names:
        assert name
        assert "\\" not in name, name
        assert not name.startswith("/"), name
        path = PurePosixPath(name)
        assert not path.is_absolute(), name
        assert ".." not in path.parts, name
        assert "." not in path.parts, name


def _assert_regular_sdist_members(members: Sequence[tarfile.TarInfo]) -> None:
    assert not any(member.issym() or member.islnk() for member in members)
    assert all(member.isfile() for member in members)


def _assert_no_forbidden_payload_paths(names: Iterable[str]) -> None:
    for name in names:
        parts = name.split("/")
        basename = parts[-1].lower()

        assert basename not in FORBIDDEN_BASENAMES, name
        assert not basename.startswith((".env", "config.", "compose.")), name
        assert "handoff" not in basename, name
        assert "agent" not in basename, name
        assert not any(
            basename.endswith(suffix) or f"{suffix}-" in basename or f"{suffix}." in basename
            for suffix in FORBIDDEN_SUFFIXES
        ), name
        assert not any(part.lower() in FORBIDDEN_DIRECTORY_PARTS for part in parts[:-1]), name


def _assert_metadata(metadata: Message) -> None:
    assert metadata["Name"] == PROJECT["name"]
    assert metadata["Version"] == VERSION
    assert metadata["Requires-Python"] == PROJECT["requires-python"]


def _relative_sdist_members(archive: tarfile.TarFile) -> tuple[list[tarfile.TarInfo], list[str]]:
    members = archive.getmembers()
    names = [member.name for member in members]
    _assert_unique_names(names)
    _assert_safe_member_names(names)
    _assert_regular_sdist_members(members)

    root_prefix = f"{DIST_NAME}/"
    assert all(name.startswith(root_prefix) for name in names)
    return members, [name.removeprefix(root_prefix) for name in names]


def _assert_sdist_contract(path: Path) -> None:
    with tarfile.open(path) as archive:
        _members, relative_names = _relative_sdist_members(archive)
        _assert_no_forbidden_payload_paths(relative_names)
        assert set(relative_names) == SDIST_EXPECTED_MEMBERS

        pkg_info = archive.extractfile(f"{DIST_NAME}/PKG-INFO")
        assert pkg_info is not None
        _assert_metadata(Parser().parsestr(pkg_info.read().decode("utf-8")))


def _assert_wheel_contract(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        _assert_unique_names(names)
        _assert_safe_member_names(names)
        assert not any(stat.S_ISLNK(info.external_attr >> 16) for info in infos)
        _assert_no_forbidden_payload_paths(names)

        package_members = {name for name in names if name.startswith("tgharbor/")}
        dist_info_members = {name for name in names if name.startswith(f"{DIST_INFO}/")}
        assert set(names) == package_members | dist_info_members
        assert package_members == APPROVED_RUNTIME_MEMBERS
        assert dist_info_members == WHEEL_EXPECTED_DIST_INFO
        _assert_metadata(Parser().parsestr(archive.read(f"{DIST_INFO}/METADATA").decode("utf-8")))


def _build_environment(cache_root: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONPYCACHEPREFIX"] = str(cache_root / "pycache")
    env.update(overrides)
    return env


def _run_build(
    project_root: Path,
    output: Path,
    distribution: str,
    *,
    env: dict[str, str],
) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            f"--{distribution}",
            "--outdir",
            str(output),
            str(project_root),
        ],
        cwd=output.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"artifact build failed:\n{result.stderr[-2000:]}"
    suffix = ".tar.gz" if distribution == "sdist" else ".whl"
    artifacts = sorted(output.glob(f"{DIST_NAME}*{suffix}"))
    assert len(artifacts) == 1, [path.name for path in output.iterdir()]
    return artifacts[0]


def _copy_approved_build_tree(destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in SDIST_ROOT_INPUTS:
        shutil.copy2(ROOT / name, destination / name)
    for member in APPROVED_RUNTIME_MEMBERS:
        relative = Path(member).relative_to("tgharbor")
        target = destination / "src" / "tgharbor" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGE_ROOT / relative, target)


def _build_complete_artifact_set(project_root: Path, work_root: Path) -> Artifacts:
    env = _build_environment(work_root / "cache")
    source_sdist = _run_build(project_root, work_root / "source-sdist", "sdist", env=env)
    direct_wheel = _run_build(project_root, work_root / "direct-wheel", "wheel", env=env)

    unpacked = work_root / "unpacked"
    unpacked.mkdir()
    with tarfile.open(source_sdist) as archive:
        archive.extractall(unpacked, filter="data")
    unpacked_project = unpacked / DIST_NAME
    assert unpacked_project.is_dir()

    rebuilt_sdist = _run_build(unpacked_project, work_root / "rebuilt-sdist", "sdist", env=env)
    sdist_derived_wheel = _run_build(
        unpacked_project, work_root / "sdist-derived-wheel", "wheel", env=env
    )
    return Artifacts(source_sdist, rebuilt_sdist, direct_wheel, sdist_derived_wheel)


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> Artifacts:
    work_root = tmp_path_factory.mktemp("distribution-work")
    project_root = work_root / "project"
    _copy_approved_build_tree(project_root)

    # These adversarial local files must not be selected. The root .gitignore also
    # exercises the public sdist hook that removes Hatchling's forced VCS metadata.
    (project_root / ".gitignore").write_text("*.local\n", encoding="utf-8")
    (project_root / ".env").write_text("NEUTRAL_TEST_VALUE=1\n", encoding="utf-8")
    (project_root / "local-root-note.txt").write_text("neutral\n", encoding="utf-8")
    (project_root / "src" / "tgharbor" / "local-note.txt").write_text("neutral\n", encoding="utf-8")
    return _build_complete_artifact_set(project_root, work_root / "artifacts")


def _install_wheel_in_fresh_environment(wheel: Path, tmp_path: Path) -> str:
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    install = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, f"wheel install failed:\n{install.stderr[-2000:]}"

    imported = subprocess.run(
        [str(python), "-c", "import tgharbor; print(tgharbor.__version__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, f"wheel import failed:\n{imported.stderr[-2000:]}"
    return imported.stdout.strip()


def test_build_targets_declare_python_only_default_deny_selection() -> None:
    document = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    targets = document["tool"]["hatch"]["build"]["targets"]

    sdist = targets["sdist"]
    assert set(sdist["include"]) == SDIST_INCLUDE
    assert sdist.get("ignore-vcs") is True
    assert "only-include" not in sdist
    assert "exclude" not in sdist
    assert sdist["hooks"]["custom"] == {"path": "hatch_build.py"}

    wheel = targets["wheel"]
    assert wheel["include"] == ["/src/tgharbor/**/*.py"]
    assert wheel["sources"] == ["src"]
    assert "only-include" not in wheel
    assert "packages" not in wheel
    assert "exclude" not in wheel


def test_reviewed_runtime_manifest_matches_current_python_sources() -> None:
    actual_python_members = {
        f"tgharbor/{path.relative_to(PACKAGE_ROOT).as_posix()}"
        for path in PACKAGE_ROOT.rglob("*.py")
    }
    assert actual_python_members == APPROVED_RUNTIME_MEMBERS
    assert not any(path.is_symlink() for path in PACKAGE_ROOT.rglob("*"))
    assert not any((ROOT / name).is_symlink() for name in SDIST_ROOT_INPUTS)


@pytest.mark.parametrize("attribute", ["source_sdist", "rebuilt_sdist"])
def test_sdist_exact_contract(artifacts: Artifacts, attribute: str) -> None:
    _assert_sdist_contract(getattr(artifacts, attribute))


@pytest.mark.parametrize("attribute", ["direct_wheel", "sdist_derived_wheel"])
def test_wheel_exact_contract(artifacts: Artifacts, attribute: str) -> None:
    _assert_wheel_contract(getattr(artifacts, attribute))


@pytest.mark.parametrize("attribute", ["direct_wheel", "sdist_derived_wheel"])
def test_wheel_installs_and_imports_in_a_fresh_environment(
    artifacts: Artifacts, attribute: str, tmp_path: Path
) -> None:
    assert _install_wheel_in_fresh_environment(getattr(artifacts, attribute), tmp_path) == VERSION


def test_unexpected_python_module_fails_reviewed_manifest_gate(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _copy_approved_build_tree(project_root)
    unexpected = project_root / "src" / "tgharbor" / "unexpected_module.py"
    unexpected.write_text("VALUE = 'neutral'\n", encoding="utf-8")
    wheel = _run_build(
        project_root,
        tmp_path / "wheel",
        "wheel",
        env=_build_environment(tmp_path / "cache"),
    )
    with pytest.raises(AssertionError):
        _assert_wheel_contract(wheel)


def test_disabled_required_hook_is_rejected_by_exact_member_gate(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _copy_approved_build_tree(project_root)
    (project_root / ".gitignore").write_text("*.local\n", encoding="utf-8")
    sdist = _run_build(
        project_root,
        tmp_path / "sdist",
        "sdist",
        env=_build_environment(tmp_path / "cache", HATCH_BUILD_NO_HOOKS="true"),
    )
    with tarfile.open(sdist) as archive:
        _members, relative_names = _relative_sdist_members(archive)
        assert ".gitignore" in relative_names
    with pytest.raises(AssertionError):
        _assert_sdist_contract(sdist)


def test_duplicate_archive_members_are_rejected(tmp_path: Path) -> None:
    duplicate_zip = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate_zip, "w") as archive:
        archive.writestr("member", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("member", b"second")
    with zipfile.ZipFile(duplicate_zip) as archive, pytest.raises(AssertionError):
        _assert_unique_names([info.filename for info in archive.infolist()])

    duplicate_tar = tmp_path / "duplicate.tar"
    with tarfile.open(duplicate_tar, "w") as archive:
        for payload in (b"first", b"second"):
            info = tarfile.TarInfo("member")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    with tarfile.open(duplicate_tar) as archive, pytest.raises(AssertionError):
        _assert_unique_names([member.name for member in archive.getmembers()])


@pytest.mark.parametrize("name", ["/absolute", "../escape", "safe/../../escape", "C:\\escape"])
def test_absolute_and_traversal_archive_members_are_rejected(name: str) -> None:
    with pytest.raises(AssertionError):
        _assert_safe_member_names([name])


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_sdist_symbolic_and_hard_links_are_rejected(member_type: bytes) -> None:
    member = tarfile.TarInfo(f"{DIST_NAME}/link")
    member.type = member_type
    member.linkname = "target"
    with pytest.raises(AssertionError):
        _assert_regular_sdist_members([member])
