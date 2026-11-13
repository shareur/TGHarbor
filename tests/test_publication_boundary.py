from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
NEUTRAL_MARKER = "NEUTRAL_CONTENT_MARKER_XYZ"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"tgharbor_boundary_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


policy_module = _load_script("publication_policy")
sys.modules["publication_policy"] = policy_module
boundary = _load_script("verify_publication_boundary")

SYNTHETIC_POLICY = policy_module.ForbiddenPathPolicy(
    directory_names=frozenset({"restricted-tree"}),
    basenames=frozenset({"governance-note.md"}),
    relative_paths=frozenset({"notes/governance-note.md"}),
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _write_required_ignores(root: Path) -> None:
    gitignore = "\n".join(sorted(boundary.PUBLIC_GITIGNORE_REQUIRED_RULES)) + "\n"
    dockerignore = (
        boundary.DOCKERIGNORE_DENY_ALL_RULE
        + "\n"
        + "\n".join(sorted(boundary.DOCKERIGNORE_REQUIRED_INCLUDE_RULES))
        + "\n"
    )
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    (root / ".dockerignore").write_text(dockerignore, encoding="utf-8")


def _write_policy_file(path: Path, *, directories: list[str]) -> Path:
    path.write_text(json.dumps({"directories": directories}), encoding="utf-8")
    return path


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, check=True)
    _git(root, "config", "user.email", "boundary@example.invalid")
    _git(root, "config", "user.name", "Boundary Test")
    _write_required_ignores(root)
    (root / "README.md").write_text("# Neutral fixture\n", encoding="utf-8")


def _commit(root: Path, message: str = "neutral fixture") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def test_clean_temporary_repository_passes_with_and_without_policy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root)

    assert boundary.verify_repository(root) == []
    assert boundary.verify_repository(root, SYNTHETIC_POLICY) == []


def test_force_added_restricted_path_fails_under_injected_policy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root)

    secret = root / "restricted-tree" / "neutral-note.txt"
    secret.parent.mkdir()
    secret.write_text("neutral fixture\n", encoding="utf-8")
    _git(root, "add", "-f", "restricted-tree/neutral-note.txt")
    _git(root, "commit", "-q", "-m", "force add")

    assert boundary.verify_repository(root) == []
    violations = boundary.verify_repository(root, SYNTHETIC_POLICY)
    assert any(violation.path == "restricted-tree/neutral-note.txt" for violation in violations)


def test_removed_path_in_reachable_history_fails_under_injected_policy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root)

    secret = root / "restricted-tree" / "neutral-note.txt"
    secret.parent.mkdir()
    secret.write_text("neutral fixture\n", encoding="utf-8")
    _git(root, "add", "-f", "restricted-tree/neutral-note.txt")
    _git(root, "commit", "-q", "-m", "add")
    _git(root, "rm", "-q", "restricted-tree/neutral-note.txt")
    _git(root, "commit", "-q", "-m", "remove")

    violations = boundary.verify_repository(root, SYNTHETIC_POLICY)
    assert any(violation.path == "restricted-tree/neutral-note.txt" for violation in violations)


def test_missing_ignore_documents_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, check=True)
    _git(root, "config", "user.email", "boundary@example.invalid")
    _git(root, "config", "user.name", "Boundary Test")
    (root / "README.md").write_text("# Neutral fixture\n", encoding="utf-8")
    _commit(root)

    violations = boundary.verify_repository(root)
    assert any(violation.path == ".gitignore" for violation in violations)
    assert any(violation.path == ".dockerignore" for violation in violations)


def test_untracked_public_file_is_not_scanned(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root)
    (root / "untracked.svg").write_text(
        '<image href="../restricted-tree/neutral.svg" />\n', encoding="utf-8"
    )

    assert boundary.verify_repository(root, SYNTHETIC_POLICY) == []


def test_tracked_public_link_to_restricted_path_fails_under_injected_policy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "docs").mkdir()
    (root / "docs" / "README.md").write_text(
        "[neutral](../restricted-tree/neutral.md)\n", encoding="utf-8"
    )
    _commit(root)

    assert boundary.verify_repository(root) == []
    violations = boundary.verify_repository(root, SYNTHETIC_POLICY)
    assert any(violation.path == "docs/README.md" for violation in violations)


def test_tracked_upload_artifact_workflow_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    workflow = root / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text("uses: actions/upload-artifact@v4\n", encoding="utf-8")
    _commit(root)

    violations = boundary.verify_repository(root)
    assert any(violation.path == ".github/workflows/ci.yml" for violation in violations)


@pytest.mark.parametrize(
    "path",
    [
        "restricted-tree/neutral.txt",
        "governance-note.md",
        "nested/governance-note.md",
        "notes/governance-note.md",
    ],
)
def test_injected_policy_covers_restricted_paths(path: str) -> None:
    assert boundary.is_forbidden_path(path, SYNTHETIC_POLICY)


def test_public_mode_has_no_builtin_restricted_path_names() -> None:
    assert not boundary.is_forbidden_path("restricted-tree/neutral.txt")
    assert not boundary.is_forbidden_path("governance-note.md")


@pytest.mark.parametrize(
    "path",
    [
        "docs/README.md",
        "src/tgharbor/main.py",
        "docs/reference-guide.md",
        "docs/reference/neutral.md",
        "restricted-tree-notes.md",
        "nested/archive/neutral.txt",
        "archives/neutral.txt",
        "governance-note.md.example",
    ],
)
def test_policy_allows_public_paths(path: str) -> None:
    assert not boundary.is_forbidden_path(path, SYNTHETIC_POLICY)


@pytest.mark.parametrize(
    "path",
    [
        "restricted-tree\\neutral.txt",
        "./restricted-tree/neutral.txt",
        "/restricted-tree/neutral.txt",
        "governance-note.md",
    ],
)
def test_policy_normalizes_posix_and_backslash_paths(path: str) -> None:
    assert boundary.is_forbidden_path(path, SYNTHETIC_POLICY)


def test_history_helper_reports_reachable_restricted_path() -> None:
    entries = [
        "a" * 40 + " restricted-tree/neutral.txt",
        "b" * 40 + " README.md",
        "c" * 40,
    ]

    assert boundary.check_history_paths(entries) == []
    violations = boundary.check_history_paths(entries, SYNTHETIC_POLICY)
    assert [violation.path for violation in violations] == ["restricted-tree/neutral.txt"]


def test_history_helper_still_rejects_unsafe_path_shape() -> None:
    violations = boundary.check_history_paths(["a" * 40 + " ../escape"])
    assert [violation.path for violation in violations] == ["../escape"]


@pytest.mark.parametrize(
    "name",
    [
        "restricted-tree/neutral.txt",
        "governance-note.md",
        "notes/governance-note.md",
    ],
)
def test_archive_restricted_members_are_rejected_under_injected_policy(name: str) -> None:
    assert boundary.check_archive_members([name]) == []
    violations = boundary.check_archive_members([name], SYNTHETIC_POLICY)
    assert violations
    assert violations[0].path == name


@pytest.mark.parametrize(
    "name",
    [
        "/absolute",
        "../escape",
        "a/../../escape",
        "C:\\escape",
        "a\\b",
        "a//b",
        "",
        ".",
        "./relative",
    ],
)
def test_archive_unsafe_members_are_rejected(name: str) -> None:
    assert boundary.check_archive_members([name])


@pytest.mark.parametrize(
    "text",
    [
        "[neutral](../restricted-tree/neutral.md)",
        "![neutral](../restricted-tree/neutral.svg)",
        '<a href="../restricted-tree/neutral.html">neutral</a>',
        '<image xlink:href="../restricted-tree/neutral.svg" />',
        "[neutral](..\\restricted-tree\\neutral.md)",
        "[neutral](/restricted-tree/neutral.md)",
        "[neutral](..%2Frestricted-tree%2Fneutral.md)",
        "[neutral](../governance-note.md)",
        "[neutral](../notes/governance-note.md)",
    ],
)
def test_public_link_targets_to_restricted_paths_fail(text: str) -> None:
    assert boundary.check_public_document("docs/README.md", text) == []
    assert boundary.check_public_document("docs/README.md", text, SYNTHETIC_POLICY)


def test_ordinary_prose_naming_synthetic_concepts_is_not_a_link() -> None:
    text = (
        "The restricted-tree, archive, and reference concepts are fixture names. "
        "Use [setup](001-development-setup.md).\n"
    )

    assert boundary.check_public_document("docs/README.md", text, SYNTHETIC_POLICY) == []


def test_clean_exact_public_links_pass() -> None:
    text = (
        "[setup](001-development-setup.md)\n"
        "[translated](../zh-CN/README.md)\n"
        "[source](https://github.com/shareur/TGHarbor)\n"
        "[archive](https://github.com/shareur/TGHarbor/archive/refs/tags/v0.2.1.tar.gz)\n"
        '<a href="https://hub.docker.com/r/shareur/tgharbor">image</a>\n'
        "[mail](mailto:maintainer@example.invalid)\n"
    )

    assert boundary.check_public_document("docs/README.md", text, SYNTHETIC_POLICY) == []


@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/example-org/example-repo/blob/main/restricted-tree/neutral.txt",
        "git@github.com:example-org/example-repo.git",
        "ssh://git@github.com/example-org/example-repo.git",
        "https://token@github.com/example-org/example-repo",
    ],
)
def test_unsafe_repository_targets_fail(target: str) -> None:
    assert boundary.check_public_document(
        "docs/README.md", f"[neutral]({target})", SYNTHETIC_POLICY
    )


def test_foreign_repository_link_is_public_mode_safe() -> None:
    text = "[neutral](https://github.com/example-org/example-repo)"
    assert boundary.check_public_document("docs/README.md", text, SYNTHETIC_POLICY) == []


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "data:text/html;base64,AAAA",
        "file:///home/neutral/file",
        "vbscript:msgbox(1)",
        "ftp://example.invalid/file",
    ],
)
def test_unsafe_schemes_fail(target: str) -> None:
    assert boundary.check_public_document("docs/README.md", f"[neutral]({target})")


@pytest.mark.parametrize(
    "target",
    [
        "/home/neutral/file.md",
        "/Users/neutral/file.md",
        "C:\\Users\\neutral\\file.md",
        "~/neutral/file.md",
        "\\\\server\\share\\file.md",
    ],
)
def test_absolute_workstation_targets_fail(target: str) -> None:
    assert boundary.check_public_document("docs/README.md", f"[neutral]({target})")


@pytest.mark.parametrize(
    "target",
    [
        "../../../outside.md",
        "..%2f..%2foutside.md",
        "a/../../../outside.md",
        "/../outside.md",
    ],
)
def test_escaping_relative_targets_fail(target: str) -> None:
    assert boundary.check_public_document("docs/README.md", f"[neutral]({target})")


def test_upload_artifact_workflow_fails_closed() -> None:
    violations = boundary.check_workflow_documents(
        {".github/workflows/ci.yml": "uses: actions/upload-artifact@v4\n"}
    )

    assert violations
    assert violations[0].path == ".github/workflows/ci.yml"


def test_non_upload_workflow_passes() -> None:
    assert (
        boundary.check_workflow_documents(
            {".github/workflows/ci.yml": "uses: actions/checkout@v4\n"}
        )
        == []
    )


def test_complete_public_ignore_rules_pass() -> None:
    gitignore = "\n".join(sorted(boundary.PUBLIC_GITIGNORE_REQUIRED_RULES))
    dockerignore = (
        boundary.DOCKERIGNORE_DENY_ALL_RULE
        + "\n"
        + "\n".join(sorted(boundary.DOCKERIGNORE_REQUIRED_INCLUDE_RULES))
    )

    assert boundary.check_ignore_documents(gitignore, dockerignore) == []


def test_missing_ignore_rules_are_reported() -> None:
    violations = boundary.check_ignore_documents("", "")

    assert {violation.path for violation in violations} == {
        ".gitignore",
        ".dockerignore",
    }


def test_dockerignore_must_deny_all_paths_first() -> None:
    gitignore = "\n".join(sorted(boundary.PUBLIC_GITIGNORE_REQUIRED_RULES))
    dockerignore = "\n".join(sorted(boundary.DOCKERIGNORE_REQUIRED_INCLUDE_RULES)) + "\n*"

    violations = boundary.check_ignore_documents(gitignore, dockerignore)
    assert any("deny all paths" in violation.reason for violation in violations)


def test_dockerignore_must_keep_readme_available() -> None:
    gitignore = "\n".join(sorted(boundary.PUBLIC_GITIGNORE_REQUIRED_RULES))
    dockerignore = (
        boundary.DOCKERIGNORE_DENY_ALL_RULE
        + "\n"
        + "\n".join(
            rule
            for rule in sorted(boundary.DOCKERIGNORE_REQUIRED_INCLUDE_RULES)
            if rule != "!README.md"
        )
    )

    violations = boundary.check_ignore_documents(gitignore, dockerignore)
    assert any("README.md" in violation.reason for violation in violations)


def test_diagnostics_report_paths_but_never_file_contents(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root)

    secret = root / "restricted-tree" / "neutral-note.txt"
    secret.parent.mkdir()
    secret.write_text(f"{NEUTRAL_MARKER}\n", encoding="utf-8")
    _git(root, "add", "-f", "restricted-tree/neutral-note.txt")
    _git(root, "commit", "-q", "-m", "force add")

    rendered = "\n".join(
        violation.format() for violation in boundary.verify_repository(root, SYNTHETIC_POLICY)
    )
    assert "restricted-tree/neutral-note.txt" in rendered
    assert NEUTRAL_MARKER not in rendered


def test_tracked_public_symlink_is_rejected_without_reading_its_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)

    target = tmp_path / "external-note.txt"
    target.write_text(f"{NEUTRAL_MARKER}\n", encoding="utf-8")
    (root / "public-note.md").symlink_to(target)
    _commit(root)

    rendered = "\n".join(violation.format() for violation in boundary.verify_repository(root))
    assert "public-note.md" in rendered
    assert NEUTRAL_MARKER not in rendered


def test_main_returns_success_for_clean_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root)

    assert boundary.main(["--root", str(root)]) == boundary.EXIT_OK
    captured = capsys.readouterr()
    assert "publication boundary verified" in captured.out


def test_main_uses_injected_policy_file_without_leaking_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _commit(root)

    secret = root / "restricted-tree" / "neutral-note.txt"
    secret.parent.mkdir()
    secret.write_text(f"{NEUTRAL_MARKER}\n", encoding="utf-8")
    _git(root, "add", "-f", "restricted-tree/neutral-note.txt")
    _git(root, "commit", "-q", "-m", "force add")

    policy_file = _write_policy_file(tmp_path / "policy.json", directories=["restricted-tree"])
    assert (
        boundary.main(["--root", str(root), "--policy", str(policy_file)])
        == boundary.EXIT_VIOLATION
    )
    captured = capsys.readouterr()
    assert "boundary violation" in captured.err
    assert "restricted-tree/neutral-note.txt" in captured.err
    assert NEUTRAL_MARKER not in captured.err
    assert 'restricted-tree"' not in captured.err


def test_main_returns_error_exit_code_outside_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "not-a-repository"
    root.mkdir()

    assert boundary.main(["--root", str(root)]) == boundary.EXIT_ERROR
    captured = capsys.readouterr()
    assert "publication boundary error" in captured.err
