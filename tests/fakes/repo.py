"""Repo cache without git: a checkout is the project's files written to a
temp dir, so the real sandboxed repo tools (grep/read/tree/symbol) run on it."""

from __future__ import annotations

from pathlib import Path

from .gitlab import FakeGitLab


class FakeRepoCache:
    def __init__(self, gitlab: FakeGitLab, root: Path):
        self._gitlab = gitlab
        self._root = root
        self.checkouts: list[tuple[str, int, str | None]] = []
        self.released: list[Path] = []

    async def checkout_mr(self, gitlab_config: dict, project_path: str, mr_iid: int,
                          sha: str | None = None) -> Path:
        self.checkouts.append((project_path, mr_iid, sha))
        project = next(p for p in self._gitlab.projects_by_id.values()
                       if p.path_with_namespace == project_path)
        wt = self._root / f"wt-{len(self.checkouts)}"
        for rel, content in project.repo_files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(content)
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    async def release(self, worktree: Path) -> None:
        self.released.append(worktree)
