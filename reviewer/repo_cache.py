"""Lazy git clone cache + read-only repo tools for the investigator.

- First MR for a project: bare clone over HTTPS using the instance token.
- Later MRs: fetch. The MR head is fetched via refs/merge-requests/<iid>/head
  on the TARGET project, which works for cross-fork MRs too.
- A detached worktree per investigation, removed afterwards.
- LRU eviction by last-use marker when the cache exceeds the disk budget.

Tools are pure Python (no shell), path-sandboxed to the worktree — prompt
injection in repo content cannot lead to command execution.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, urlsplit

from .config import settings

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_500_000  # skip bigger files in grep/read
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".idea", ".mypy_cache", ".ruff_cache", "vendor"}


class RepoCacheError(Exception):
    pass


def _git_env_args() -> list[str]:
    """Proxy config for git (http.proxy honors socks5h:// URLs)."""
    proxy = settings.http_proxy or (
        f"socks5h://{settings.socks_proxy}" if settings.socks_proxy else "")
    return ["-c", f"http.proxy={proxy}"] if proxy else []


def _run_git(*args: str, timeout: int = 600) -> str:
    cmd = ["git", *_git_env_args(), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RepoCacheError(f"git {' '.join(args[:2])} failed: {result.stderr[:500]}")
    return result.stdout


class RepoCache:
    def __init__(self, cache_dir: str | None = None, max_gb: float | None = None):
        self.root = Path(cache_dir or settings.repo_cache_dir)
        self.max_bytes = int((max_gb or settings.repo_cache_max_gb) * 1024**3)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _repo_dir(self, instance_url: str, project_path: str) -> Path:
        host = urlsplit(instance_url).netloc or instance_url
        return self.root / host / f"{project_path}.git"

    def _clone_url(self, instance_url: str, token: str, project_path: str) -> str:
        parts = urlsplit(instance_url)
        return f"{parts.scheme}://oauth2:{quote(token, safe='')}@{parts.netloc}/{project_path}.git"

    async def checkout_mr(self, gitlab_config: dict, project_path: str,
                          mr_iid: int, sha: str | None) -> Path:
        """Ensure the repo is cloned/fetched; return a detached worktree at the MR head."""
        repo_dir = self._repo_dir(gitlab_config["url"], project_path)
        key = str(repo_dir)
        async with self._locks[key]:
            await asyncio.to_thread(
                self._sync_repo, repo_dir, gitlab_config, project_path, mr_iid)
            worktree = await asyncio.to_thread(
                self._add_worktree, repo_dir, mr_iid, sha)
        asyncio.get_running_loop().run_in_executor(None, self._evict)
        return worktree

    def _sync_repo(self, repo_dir: Path, gitlab_config: dict,
                   project_path: str, mr_iid: int) -> None:
        url = self._clone_url(gitlab_config["url"], gitlab_config["token"], project_path)
        if not repo_dir.is_dir():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Cloning %s (first MR for this project)", project_path)
            _run_git("clone", "--bare", "--filter=blob:none", url, str(repo_dir))
        # remote URL may rotate with the token — refresh it, then fetch the MR head ref
        _run_git("-C", str(repo_dir), "remote", "set-url", "origin", url)
        _run_git("-C", str(repo_dir), "fetch", "origin",
                 f"+refs/merge-requests/{mr_iid}/head:refs/mr/{mr_iid}")
        (repo_dir / ".last_used").write_text(str(int(time.time())))

    def _add_worktree(self, repo_dir: Path, mr_iid: int, sha: str | None) -> Path:
        ref = sha or f"refs/mr/{mr_iid}"
        worktree = repo_dir.parent / f"{repo_dir.stem}-mr{mr_iid}-wt"
        if worktree.exists():
            self._remove_worktree_sync(repo_dir, worktree)
        _run_git("-C", str(repo_dir), "worktree", "add", "--detach",
                 str(worktree), ref, timeout=300)
        return worktree

    async def release(self, worktree: Path) -> None:
        repo_dir = worktree.parent / f"{worktree.name.rsplit('-mr', 1)[0]}.git"
        await asyncio.to_thread(self._remove_worktree_sync, repo_dir, worktree)

    def _remove_worktree_sync(self, repo_dir: Path, worktree: Path) -> None:
        try:
            _run_git("-C", str(repo_dir), "worktree", "remove", "--force", str(worktree),
                     timeout=120)
        except (RepoCacheError, subprocess.TimeoutExpired):
            shutil.rmtree(worktree, ignore_errors=True)
            try:
                _run_git("-C", str(repo_dir), "worktree", "prune")
            except RepoCacheError:
                pass

    def _evict(self) -> None:
        """Drop least-recently-used repos when over the disk budget."""
        try:
            repos = [path for path in self.root.glob("*/**/*.git") if path.is_dir()]
            sized = []
            total = 0
            for repo in repos:
                size = sum(f.stat().st_size for f in repo.rglob("*") if f.is_file())
                marker = repo / ".last_used"
                last_used = marker.stat().st_mtime if marker.exists() else 0
                sized.append((last_used, size, repo))
                total += size
            if total <= self.max_bytes:
                return
            for last_used, size, repo in sorted(sized):
                logger.info("Evicting repo cache entry %s (%.1f MB)", repo, size / 1e6)
                shutil.rmtree(repo, ignore_errors=True)
                total -= size
                if total <= self.max_bytes:
                    break
        except Exception as exc:  # noqa: BLE001 — eviction is best-effort housekeeping
            logger.warning("repo cache eviction failed: %s", exc)


# --- read-only tools over a worktree (sandboxed) ---

def _safe_path(worktree: Path, rel: str) -> Path:
    candidate = (worktree / rel.lstrip("/")).resolve()
    if not str(candidate).startswith(str(worktree.resolve())):
        raise ValueError(f"path escapes the repository: {rel}")
    return candidate


def repo_grep(worktree: Path, pattern: str, glob: str | None = None,
              max_results: int = 50) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Invalid regex: {exc}"
    hits: list[str] = []
    for path in sorted(worktree.rglob("*")):
        if len(hits) >= max_results:
            hits.append(f"... (truncated at {max_results} results)")
            break
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(worktree)
        if glob and not fnmatch.fnmatch(str(rel), glob):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel}:{line_no}: {line.strip()[:300]}")
                if len(hits) >= max_results:
                    break
    return "\n".join(hits) if hits else "No matches."


def repo_read_file(worktree: Path, path: str, start_line: int = 1,
                   end_line: int | None = None) -> str:
    try:
        target = _safe_path(worktree, path)
        if not target.is_file():
            return f"Not a file: {path}"
        if target.stat().st_size > MAX_FILE_BYTES:
            return f"File too large to read: {path}"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"
    end_line = min(end_line or start_line + 399, len(lines))
    start_line = max(1, start_line)
    chunk = [f"{idx}: {lines[idx - 1]}" for idx in range(start_line, end_line + 1)]
    suffix = "" if end_line >= len(lines) else f"\n... ({len(lines) - end_line} more lines)"
    return "\n".join(chunk) + suffix


def repo_list_tree(worktree: Path, path: str = ".", depth: int = 2) -> str:
    try:
        base = _safe_path(worktree, path)
    except ValueError as exc:
        return f"Error: {exc}"
    if not base.is_dir():
        return f"Not a directory: {path}"
    entries: list[str] = []
    base_depth = len(base.parts)
    for item in sorted(base.rglob("*")):
        if any(part in SKIP_DIRS for part in item.parts):
            continue
        rel_depth = len(item.parts) - base_depth
        if rel_depth > depth:
            continue
        rel = item.relative_to(worktree)
        entries.append(f"{rel}/" if item.is_dir() else str(rel))
        if len(entries) >= 500:
            entries.append("... (truncated at 500 entries)")
            break
    return "\n".join(entries) if entries else "(empty)"


repo_cache = RepoCache()
