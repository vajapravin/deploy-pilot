"""Unit tests for Git operations and revision integrity service."""

import contextlib
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.services.git_service import (
    CommitNotReachableError,
    DirtyWorkingTreeError,
    GitService,
    GitTimeoutError,
    NotAGitRepositoryError,
    UnexpectedRemoteError,
)


def _run_cmd(cmd: list[str], cwd: Path) -> str:
    """Helper to run synchronous git command in setup fixtures."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _init_repo(path: Path) -> None:
    """Initialize a git repository with initial commit and identity."""
    path.mkdir(parents=True, exist_ok=True)
    _run_cmd(["git", "init", "-b", "main"], cwd=path)
    _run_cmd(["git", "config", "user.name", "Test User"], cwd=path)
    _run_cmd(["git", "config", "user.email", "test@example.com"], cwd=path)
    (path / "README.md").write_text("# Test Repo\n")
    _run_cmd(["git", "add", "README.md"], cwd=path)
    _run_cmd(["git", "commit", "-m", "initial commit"], cwd=path)


@pytest.mark.asyncio
async def test_verify_repository_success(tmp_path: Path) -> None:
    """Ensure verify_repository succeeds on a valid git directory."""
    repo_dir = tmp_path / "valid-repo"
    _init_repo(repo_dir)

    service = GitService()
    await service.verify_repository(repo_dir)


@pytest.mark.asyncio
async def test_verify_repository_failure(tmp_path: Path) -> None:
    """Ensure verify_repository fails on non-git or missing paths."""
    service = GitService()
    with pytest.raises(NotAGitRepositoryError):
        await service.verify_repository(tmp_path / "missing-dir")

    not_git = tmp_path / "not-git"
    not_git.mkdir()
    with pytest.raises(NotAGitRepositoryError):
        await service.verify_repository(not_git)


@pytest.mark.asyncio
async def test_check_clean_working_tree_passes(tmp_path: Path) -> None:
    """Ensure clean working tree passes without error."""
    repo_dir = tmp_path / "clean-repo"
    _init_repo(repo_dir)

    service = GitService()
    await service.check_clean_working_tree(repo_dir)


@pytest.mark.asyncio
async def test_check_clean_working_tree_detects_dirty_modification(tmp_path: Path) -> None:
    """Ensure uncommitted modifications trigger DirtyWorkingTreeError."""
    repo_dir = tmp_path / "dirty-repo"
    _init_repo(repo_dir)
    (repo_dir / "README.md").write_text("modified content\n")

    service = GitService()
    with pytest.raises(DirtyWorkingTreeError) as exc_info:
        await service.check_clean_working_tree(repo_dir)

    assert "M README.md" in str(exc_info.value)


@pytest.mark.asyncio
async def test_check_clean_working_tree_detects_untracked_files(tmp_path: Path) -> None:
    """Ensure untracked files trigger DirtyWorkingTreeError."""
    repo_dir = tmp_path / "untracked-repo"
    _init_repo(repo_dir)
    (repo_dir / "new_secret.txt").write_text("secret\n")

    service = GitService()
    with pytest.raises(DirtyWorkingTreeError) as exc_info:
        await service.check_clean_working_tree(repo_dir)

    assert "?? new_secret.txt" in str(exc_info.value)


@pytest.mark.asyncio
async def test_verify_remote_matching(tmp_path: Path) -> None:
    """Ensure verify_remote succeeds when remote matches expected URL."""
    repo_dir = tmp_path / "remote-repo"
    _init_repo(repo_dir)
    _run_cmd(
        ["git", "remote", "add", "origin", "https://github.com/vajapravin/test-app.git"],
        cwd=repo_dir,
    )

    service = GitService()
    # Matching with exact URL
    await service.verify_remote(repo_dir, "https://github.com/vajapravin/test-app.git")
    # Matching without .git suffix
    await service.verify_remote(repo_dir, "https://github.com/vajapravin/test-app")


@pytest.mark.asyncio
async def test_verify_remote_mismatch_raises_error(tmp_path: Path) -> None:
    """Ensure unexpected remote raises UnexpectedRemoteError."""
    repo_dir = tmp_path / "remote-repo-mismatch"
    _init_repo(repo_dir)
    _run_cmd(
        ["git", "remote", "add", "origin", "https://github.com/attacker/malicious.git"],
        cwd=repo_dir,
    )

    service = GitService()
    with pytest.raises(UnexpectedRemoteError, match="remote mismatch"):
        await service.verify_remote(repo_dir, "https://github.com/vajapravin/test-app.git")


@pytest.mark.asyncio
async def test_verify_commit_in_branch(tmp_path: Path) -> None:
    """Ensure commit reachable in branch history verifies successfully."""
    origin_dir = tmp_path / "origin"
    _init_repo(origin_dir)

    # Local clone
    local_dir = tmp_path / "local"
    _run_cmd(["git", "clone", origin_dir.as_posix(), local_dir.as_posix()], cwd=tmp_path)
    _run_cmd(["git", "config", "user.name", "Test User"], cwd=local_dir)
    _run_cmd(["git", "config", "user.email", "test@example.com"], cwd=local_dir)

    service = GitService()
    head_sha = await service.get_current_head(local_dir)

    verified_sha = await service.verify_commit_in_branch(local_dir, "main", head_sha)
    assert verified_sha == head_sha


@pytest.mark.asyncio
async def test_verify_commit_not_in_branch_raises_error(tmp_path: Path) -> None:
    """Ensure commit not in branch history raises CommitNotReachableError."""
    origin_dir = tmp_path / "origin"
    _init_repo(origin_dir)

    local_dir = tmp_path / "local"
    _run_cmd(["git", "clone", origin_dir.as_posix(), local_dir.as_posix()], cwd=tmp_path)

    service = GitService()
    # Non-existent commit SHA
    fake_sha = "a" * 40
    with pytest.raises(CommitNotReachableError):
        await service.verify_commit_in_branch(local_dir, "main", fake_sha)


@pytest.mark.asyncio
async def test_checkout_and_fast_forward_flow(tmp_path: Path) -> None:
    """Test full cycle: upstream commits -> fetch -> verify -> checkout & fast forward."""
    origin_dir = tmp_path / "origin"
    _init_repo(origin_dir)

    local_dir = tmp_path / "local"
    _run_cmd(["git", "clone", origin_dir.as_posix(), local_dir.as_posix()], cwd=tmp_path)
    _run_cmd(["git", "config", "user.name", "Test User"], cwd=local_dir)
    _run_cmd(["git", "config", "user.email", "test@example.com"], cwd=local_dir)

    # Add a new commit to origin
    (origin_dir / "app.py").write_text("print('hello world')\n")
    _run_cmd(["git", "add", "app.py"], cwd=origin_dir)
    _run_cmd(["git", "commit", "-m", "feat: new upstream commit"], cwd=origin_dir)
    new_sha = _run_cmd(["git", "rev-parse", "HEAD"], cwd=origin_dir)

    service = GitService()
    # 1. Fetch
    await service.fetch_branch(local_dir, "main")
    # 2. Verify commit on branch
    verified_sha = await service.verify_commit_in_branch(local_dir, "main", new_sha)
    assert verified_sha == new_sha
    # 3. Checkout & fast-forward
    deployed_sha = await service.checkout_and_fast_forward(local_dir, "main", new_sha)
    assert deployed_sha == new_sha
    assert (local_dir / "app.py").exists()


@pytest.mark.asyncio
async def test_git_timeout_handling(tmp_path: Path) -> None:
    """Ensure GitTimeoutError is raised when command exceeds execution timeout."""
    repo_dir = tmp_path / "timeout-repo"
    _init_repo(repo_dir)

    async def _mock_wait_for(coro: Any, timeout: float) -> Any:
        with contextlib.suppress(Exception):
            coro.close()
        raise TimeoutError("timed out")

    service = GitService(default_timeout_seconds=5)
    with (
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
        pytest.raises(GitTimeoutError, match="timed out"),
    ):
        await service._run_git_command(repo_dir, ["status"], timeout_seconds=1.0)
