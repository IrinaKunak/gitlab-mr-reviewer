"""Lazy git clone cache + read-only repo tools for the investigator.

- First MR for a project: bare clone over HTTPS using the instance token.
- Later MRs: fetch. The MR head is fetched via refs/merge-requests/<iid>/head
  on the TARGET project, which works for cross-fork MRs too.
- A detached worktree per investigation (named with the head sha, so concurrent
  investigations of the same MR never collide), removed afterwards.
- LRU eviction by last-use marker when the cache exceeds the disk budget;
  recently-used repos and repos with live worktrees are never evicted.

Security:
- The token is NEVER written to disk: remotes keep a credential-less URL and
  auth rides per-invocation via `http.extraheader` (Basic oauth2:<token>).
- git stderr is scrubbed for embedded credentials before raising.
- Tools are pure Python (no shell), path-sandboxed to the worktree, and do not
  follow symlinks — prompt injection in repo content cannot read outside the
  checkout or execute commands.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .domain.models import InstanceRef

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_500_000  # skip bigger files in grep/read
GREP_TIME_BUDGET = 15.0     # seconds; coarse ReDoS / huge-repo guard
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".idea", ".mypy_cache", ".ruff_cache", "vendor"}

# Better engines when the image ships them (Dockerfile installs both); every
# path degrades cleanly when the binary is absent, so tests and old images work.
_RG = shutil.which("rg")          # ripgrep: linear-time regex, .gitignore-aware
_CTAGS = shutil.which("ctags")    # universal-ctags: definition index
CTAGS_TIME_BUDGET = 90.0
MAX_SYMBOLS = 300_000             # index memory bound (~30 MB worst case)

_CRED_RE = re.compile(r"(https?://)[^/@\s]+@")


class RepoCacheError(Exception):
    pass


def _redact(text: str) -> str:
    return _CRED_RE.sub(r"\1***@", text or "")


def _proxy_args() -> list[str]:
    proxy = settings.network.http_proxy or (
        f"socks5h://{settings.network.socks_proxy}" if settings.network.socks_proxy else "")
    return ["-c", f"http.proxy={proxy}"] if proxy else []


def _auth_args(token: str) -> list[str]:
    """Per-invocation HTTP auth — keeps the token out of .git/config on disk."""
    basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return ["-c", f"http.extraheader=Authorization: Basic {basic}"]


def _run_git(*args: str, token: str | None = None, timeout: int = 600) -> str:
    cmd = ["git", *_proxy_args(), *(_auth_args(token) if token else []), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RepoCacheError(
            f"git {' '.join(args[:2])} failed: {_redact(result.stderr[:500])}")
    return result.stdout


class RepoCache:
    def __init__(self, cache_dir: str | None = None, max_gb: float | None = None):
        self.root = Path(cache_dir or settings.repo_cache.dir)
        self.max_bytes = int((max_gb or settings.repo_cache.max_gb) * 1024**3)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._evict_lock = threading.Lock()

    def _repo_dir(self, instance_url: str, project_path: str) -> Path:
        host = urlsplit(instance_url).netloc or instance_url
        return self.root / host / f"{project_path}.git"

    def _clone_url(self, instance_url: str, project_path: str) -> str:
        parts = urlsplit(instance_url)
        return f"{parts.scheme}://{parts.netloc}/{project_path}.git"

    async def checkout_mr(self, instance: InstanceRef, project_path: str,
                          mr_iid: int, sha: str | None) -> Path:
        """Ensure the repo is cloned/fetched; return a detached worktree at the MR head."""
        repo_dir = self._repo_dir(instance.url, project_path)
        key = str(repo_dir)
        async with self._locks[key]:
            await asyncio.to_thread(
                self._sync_repo, repo_dir, instance, project_path, mr_iid)
            worktree = await asyncio.to_thread(
                self._add_worktree, repo_dir, mr_iid, sha, instance.token)
        asyncio.get_running_loop().run_in_executor(None, self._evict)
        return worktree

    def _sync_repo(self, repo_dir: Path, instance: InstanceRef,
                   project_path: str, mr_iid: int) -> None:
        url = self._clone_url(instance.url, project_path)
        token = instance.token
        if not repo_dir.is_dir():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Cloning %s (first MR for this project)", project_path)
            _run_git("clone", "--bare", "--filter=blob:none", url, str(repo_dir),
                     token=token)
        # credential-less remote; auth is injected per command via extraheader
        _run_git("-C", str(repo_dir), "remote", "set-url", "origin", url)
        _run_git("-C", str(repo_dir), "fetch", "origin",
                 f"+refs/merge-requests/{mr_iid}/head:refs/mr/{mr_iid}", token=token)
        (repo_dir / ".last_used").write_text(str(int(time.time())))

    def _add_worktree(self, repo_dir: Path, mr_iid: int, sha: str | None,
                      token: str) -> Path:
        ref = sha or f"refs/mr/{mr_iid}"
        suffix = (sha or "head")[:12]
        worktree = repo_dir.parent / f"{repo_dir.stem}-mr{mr_iid}-{suffix}-wt"
        if worktree.exists():  # same MR+sha re-queued: previous run is dead (dedupe gates live ones)
            self._remove_worktree_sync(repo_dir, worktree)
        # partial clone fetches blobs during checkout — needs auth + proxy
        _run_git("-C", str(repo_dir), "worktree", "add", "--detach",
                 str(worktree), ref, token=token, timeout=300)
        return worktree

    async def release(self, worktree: Path) -> None:
        clear_symbol_index(worktree)
        repo_dir = worktree.parent / f"{worktree.name.rsplit('-mr', 1)[0]}.git"
        # same lock as checkout_mr: never drop a bare repo mid-checkout of another MR
        async with self._locks[str(repo_dir)]:
            await asyncio.to_thread(self._remove_worktree_sync, repo_dir, worktree)
            if settings.repo_cache.ephemeral:
                await asyncio.to_thread(self._drop_repo, repo_dir)

    def _drop_repo(self, repo_dir: Path) -> None:
        """Ephemeral mode (small disks): clone -> investigate -> remove."""
        if any(repo_dir.parent.glob(f"{repo_dir.stem}-mr*-wt")):
            return  # another investigation of this project is still running
        logger.info("Ephemeral mode: dropping %s after investigation", repo_dir)
        shutil.rmtree(repo_dir, ignore_errors=True)

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
        """Drop least-recently-used repos when over the disk budget.

        Never touches a repo that was used in the last hour or has a live
        worktree next to it (an investigation may be running on it).
        """
        if not self._evict_lock.acquire(blocking=False):
            return  # another eviction pass is running
        try:
            now = time.time()
            sized = []
            total = 0
            for repo in self.root.glob("*/**/*.git"):
                if not repo.is_dir():
                    continue
                size = sum(f.stat().st_size for f in repo.rglob("*") if f.is_file())
                total += size
                marker = repo / ".last_used"
                last_used = marker.stat().st_mtime if marker.exists() else 0
                has_live_worktree = any(repo.parent.glob(f"{repo.stem}-mr*-wt"))
                if now - last_used < 3600 or has_live_worktree:
                    continue  # in use — count its size but never evict
                sized.append((last_used, size, repo))
            if total <= self.max_bytes:
                return
            for _last_used, size, repo in sorted(sized):
                logger.info("Evicting repo cache entry %s (%.1f MB)", repo, size / 1e6)
                shutil.rmtree(repo, ignore_errors=True)
                total -= size
                if total <= self.max_bytes:
                    break
        except Exception as exc:  # noqa: BLE001 — eviction is best-effort housekeeping
            logger.warning("repo cache eviction failed: %s", exc)
        finally:
            self._evict_lock.release()


# --- read-only tools over a worktree (sandboxed, symlink-safe) ---

def _safe_path(worktree: Path, rel: str) -> Path:
    candidate = (worktree / rel.lstrip("/")).resolve()
    if not candidate.is_relative_to(worktree.resolve()):
        raise ValueError(f"path escapes the repository: {rel}")
    return candidate


def _walk_files(worktree: Path):
    """Yield regular files under the worktree, skipping symlinks and junk dirs."""
    for dirpath, dirnames, filenames in os.walk(worktree, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if not path.is_symlink():
                yield path


def repo_grep(worktree: Path, pattern: str, glob: str | None = None,
              max_results: int = 50) -> str:
    if len(pattern) > 256:
        return "Pattern too long (max 256 chars)."
    if _RG:
        out = _grep_ripgrep(worktree, pattern, glob, max_results)
        if out is not None:
            return out
        # rg's Rust regex has no lookarounds/backrefs — those patterns fall
        # through to the Python engine below
    return _grep_python(worktree, pattern, glob, max_results)


def _grep_ripgrep(worktree: Path, pattern: str, glob: str | None,
                  max_results: int) -> str | None:
    """ripgrep-backed search: linear-time regex (no ReDoS), .gitignore-aware,
    orders of magnitude faster than the Python walk on big repos. Returns None
    when rg cannot run the pattern — the caller falls back to Python re."""
    cmd = [_RG, "--no-config", "--line-number", "--no-heading", "--color=never",
           "--sort", "path", "--max-columns", "300", "--max-columns-preview",
           "--hidden",  # the Python engine searched dotfiles (.gitlab-ci.yml)
           "--max-filesize", str(MAX_FILE_BYTES),
           "--max-count", str(max_results)]
    for skip in sorted(SKIP_DIRS):
        cmd += ["--glob", f"!**/{skip}/**"]
    if glob:
        cmd += ["--glob", glob]
    cmd += ["-e", pattern, "."]
    hits: list[str] = []
    started = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, cwd=str(worktree), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("ripgrep unavailable (%s) — python grep fallback", exc)
        return None
    stdout = proc.stdout
    assert stdout is not None  # PIPE above guarantees it
    try:
        for line in stdout:  # streamed: bounded memory even on flood matches
            if len(hits) >= max_results:
                hits.append(f"... (truncated at {max_results} results)")
                break
            if time.monotonic() - started > GREP_TIME_BUDGET:
                hits.append("... (search time budget exceeded — narrow the "
                            "pattern or glob)")
                break
            line = line.rstrip("\n")
            if not line:
                continue
            path, _, rest = line.partition(":")
            line_no, _, text = rest.partition(":")
            if path.startswith("./"):
                path = path[2:]
            hits.append(f"{path}:{line_no}: {text.strip()[:300]}")
    finally:
        proc.kill()
        try:
            stderr = proc.communicate(timeout=5)[1] or ""
        except (subprocess.TimeoutExpired, OSError):
            stderr = ""
    if hits:
        return "\n".join(hits)
    if proc.returncode == 1:  # clean run, zero matches
        return "No matches."
    if proc.returncode not in (0, None):
        # exit 2: pattern rejected (lookaround/backref) or IO error — let the
        # Python engine, which supports the full re syntax, try it
        logger.debug("rg exit %s: %s", proc.returncode, stderr[:200])
        return None
    return "No matches."


def _grep_python(worktree: Path, pattern: str, glob: str | None = None,
                 max_results: int = 50) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Invalid regex: {exc}"
    hits: list[str] = []
    started = time.monotonic()
    for path in _walk_files(worktree):
        if len(hits) >= max_results:
            hits.append(f"... (truncated at {max_results} results)")
            break
        if time.monotonic() - started > GREP_TIME_BUDGET:
            hits.append("... (search time budget exceeded — narrow the pattern or glob)")
            break
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
        if target.is_symlink() or not target.is_file():
            return f"Not a regular file: {path}"
        if target.stat().st_size > MAX_FILE_BYTES:
            return f"File too large to read: {path}"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"
    start_line = max(1, start_line)
    if start_line > len(lines):
        return f"start_line {start_line} is beyond end of file ({len(lines)} lines)."
    end_line = min(end_line or start_line + 399, len(lines))
    if end_line < start_line:
        return f"end_line {end_line} is before start_line {start_line}."
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
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_depth = len(Path(dirpath).parts) - base_depth
        if rel_depth >= depth:
            dirnames[:] = []
        for name in sorted(dirnames):
            entries.append(f"{(Path(dirpath) / name).relative_to(worktree)}/")
        for name in sorted(filenames):
            item = Path(dirpath) / name
            if not item.is_symlink():
                entries.append(str(item.relative_to(worktree)))
        if len(entries) >= 500:
            entries = entries[:500]
            entries.append("... (truncated at 500 entries)")
            break
    return "\n".join(entries) if entries else "(empty)"


# --- symbol index (universal-ctags): definition lookup in one tool call ---
# Grep makes the model GUESS where things are defined and burn its tool budget
# on wrong guesses; a ctags index answers "where is LeadSerializer" exactly.

_SYMBOL_CACHE: dict[str, list[dict] | None] = {}  # worktree path -> index
_SYMBOL_CACHE_MAX = 8


def _run_ctags(worktree: Path) -> str | None:
    if not _CTAGS:
        return None
    cmd = [_CTAGS, "-R", "--output-format=json", "--fields=+n", "-f", "-"]
    cmd += [f"--exclude={skip}" for skip in sorted(SKIP_DIRS)]
    cmd.append(".")
    try:
        result = subprocess.run(cmd, cwd=str(worktree), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=CTAGS_TIME_BUDGET, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ctags indexing failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning("ctags exit %s: %s", result.returncode,
                       (result.stderr or "")[:200])
        return None
    return result.stdout


def _symbol_index(worktree: Path) -> list[dict] | None:
    """Build (once per worktree) and cache the tag list. None = no index
    available (ctags missing/failed) — callers degrade to repo_grep."""
    key = str(worktree)
    if key in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[key]
    raw = _run_ctags(worktree)
    index: list[dict] | None = None
    if raw is not None:
        index = []
        for line in raw.splitlines():
            if len(index) >= MAX_SYMBOLS:
                logger.info("symbol index capped at %d tags", MAX_SYMBOLS)
                break
            try:
                tag = json.loads(line)
            except ValueError:
                continue
            if tag.get("_type") != "tag" or not tag.get("name"):
                continue
            index.append({"name": tag["name"], "path": tag.get("path", ""),
                          "line": tag.get("line", 0), "kind": tag.get("kind", "")})
    while len(_SYMBOL_CACHE) >= _SYMBOL_CACHE_MAX:
        _SYMBOL_CACHE.pop(next(iter(_SYMBOL_CACHE)))
    _SYMBOL_CACHE[key] = index
    return index


def clear_symbol_index(worktree: Path) -> None:
    _SYMBOL_CACHE.pop(str(worktree), None)


def repo_find_symbol(worktree: Path, name: str, max_results: int = 20) -> str:
    name = (name or "").strip()
    if not name:
        return "Empty symbol name."
    index = _symbol_index(worktree)
    if index is None:
        return ("Symbol index unavailable in this deployment — use repo_grep "
                "to locate the definition instead.")
    matches = [t for t in index if t["name"] == name]
    if not matches:
        lowered = name.lower()
        matches = [t for t in index if t["name"].lower() == lowered]
    if not matches:
        lowered = name.lower()
        matches = [t for t in index if lowered in t["name"].lower()]
    if not matches:
        return (f"No symbol matching {name!r} in the index. Try repo_grep — the "
                "definition may be generated, dynamic, or in an unindexed language.")
    lines = [f"{t['kind'] or 'symbol'}\t{t['name']}\t{t['path']}:{t['line']}"
             for t in matches[:max_results]]
    if len(matches) > max_results:
        lines.append(f"... and {len(matches) - max_results} more")
    return "\n".join(lines)


repo_cache = RepoCache()
