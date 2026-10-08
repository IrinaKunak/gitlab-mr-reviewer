"""Repo clone cache and the sandboxed read-only repo tools."""

from __future__ import annotations

import asyncio

import pytest

from reviewer.repo_cache import _safe_path, repo_grep, repo_list_tree, repo_read_file


def test_repo_tools(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def handle_cart():\n    return 42\n")
    (tmp_path / "README.md").write_text("hello")

    hits = repo_grep(tmp_path, r"handle_cart")
    assert "src/app.py:1" in hits

    content = repo_read_file(tmp_path, "src/app.py")
    assert "1: def handle_cart():" in content

    tree = repo_list_tree(tmp_path)
    assert "src/" in tree and "README.md" in tree

    with pytest.raises(ValueError):
        _safe_path(tmp_path, "../../etc/passwd")
    assert "Error" in repo_read_file(tmp_path, "../secret")


def test_safe_path_rejects_sibling_prefix(tmp_path):
    # /x/repo must not authorize /x/repo-evil (startswith-prefix traversal)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    sibling = tmp_path / "repo-evil"
    sibling.mkdir()
    (sibling / "secret").write_text("s")
    with pytest.raises(ValueError):
        _safe_path(worktree, "../repo-evil/secret")


def test_repo_tools_skip_symlinks(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("LEAKED_TOKEN=abc")
    (worktree / "link.txt").symlink_to(secret)
    (worktree / "ok.py").write_text("LEAKED_TOKEN nope, just code")
    hits = repo_grep(worktree, "LEAKED_TOKEN")
    assert "link.txt" not in hits and "ok.py" in hits
    # reading through the symlink must fail (resolve+sandbox check or symlink check)
    assert "LEAKED" not in repo_read_file(worktree, "link.txt")
    assert "link.txt" not in repo_list_tree(worktree)


def test_repo_read_file_edges(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\n")
    assert "beyond end of file" in repo_read_file(tmp_path, "f.txt", start_line=10)
    assert "before start_line" in repo_read_file(tmp_path, "f.txt", start_line=2, end_line=1)


def test_redact_credentials_in_git_errors():
    from reviewer.repo_cache import _redact
    msg = "fatal: unable to access 'https://oauth2:glpat-SECRET@lab.x/p.git/'"
    assert "glpat-SECRET" not in _redact(msg)
    assert "https://***@lab.x" in _redact(msg)


def test_release_modes_keep_vs_ephemeral(tmp_path, monkeypatch):
    """release() keeps the bare repo by default; REPO_CACHE_EPHEMERAL drops it."""
    import shutil as _shutil
    import subprocess as _sp
    if _shutil.which("git") is None:
        pytest.skip("git not available")
    from reviewer import repo_cache as rc

    def git(*args):
        _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                check=True, capture_output=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", str(origin))
    (origin / "f.txt").write_text("x")
    git("-C", str(origin), "add", ".")
    git("-C", str(origin), "commit", "-qm", "c1")

    repo_dir = tmp_path / "cache" / "host" / "grp" / "app.git"
    repo_dir.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", str(origin), str(repo_dir))

    def add_worktree(name):
        wt = repo_dir.parent / name
        git("-C", str(repo_dir), "worktree", "add", "--detach", str(wt))
        return wt

    from reviewer.config import RepoCacheSection
    cache = rc.RepoCache(RepoCacheSection(dir=str(tmp_path / "cache")))

    # default mode: worktree removed, bare repo kept for the next MR
    wt1 = add_worktree("app-mr1-aaa-wt")
    asyncio.run(cache.release(wt1))
    assert not wt1.exists() and repo_dir.exists()

    # ephemeral mode: bare repo dropped too
    cache = rc.RepoCache(RepoCacheSection(dir=str(tmp_path / "cache"), ephemeral=True))
    wt2 = add_worktree("app-mr2-bbb-wt")
    asyncio.run(cache.release(wt2))
    assert not wt2.exists() and not repo_dir.exists()


def test_grep_python_engine_directly(tmp_path):
    # the fallback engine must keep working even where rg is installed
    from reviewer import repo_cache as rc

    (tmp_path / "a.py").write_text("def reconcile():\n    pass\n")
    hits = rc._grep_python(tmp_path, r"reconcile")
    assert "a.py:1" in hits
    assert rc._grep_python(tmp_path, r"nothing_here") == "No matches."
    assert "Invalid regex" in rc._grep_python(tmp_path, r"([")


@pytest.mark.skipif(__import__("shutil").which("rg") is None,
                    reason="ripgrep not installed")
def test_grep_ripgrep_engine(tmp_path):
    from reviewer import repo_cache as rc

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "views.py").write_text(
        "class LeadHistoryView:\n    permission_classes = [IsAuthenticated]\n")
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [reconcile]\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("IsAuthenticated noise\n")

    hits = rc.repo_grep(tmp_path, r"IsAuthenticated")
    assert "src/views.py:2" in hits
    assert "node_modules" not in hits            # SKIP_DIRS respected
    # dotfiles are searched (the Python engine always did — parity matters:
    # CI configs live in dotfiles)
    assert ".gitlab-ci.yml:1" in rc.repo_grep(tmp_path, r"reconcile")
    # glob filter narrows the search
    assert "views.py" not in rc.repo_grep(tmp_path, r"reconcile", glob="*.py")
    # lookahead is not Rust-regex: rg exits 2 and the Python engine takes over
    assert "src/views.py:2" in rc.repo_grep(tmp_path, r"IsAuthenticated(?=\])")
    assert rc.repo_grep(tmp_path, r"absent_symbol_xyz") == "No matches."


def test_symbol_index_lookup(tmp_path, monkeypatch):
    # repo_find_symbol answers "where is X defined" in ONE tool call — grep
    # made the model guess file locations and burn its verification budget
    from reviewer import repo_cache as rc

    canned = "\n".join([
        '{"_type": "tag", "name": "LeadSerializer", "path": "app/serializers.py", '
        '"line": 14, "kind": "class"}',
        '{"_type": "tag", "name": "LeadHistoryView", "path": "app/views.py", '
        '"line": 88, "kind": "class"}',
        '{"_type": "tag", "name": "lead_reconcile_task", "path": "app/tasks.py", '
        '"line": 5, "kind": "function"}',
        '{"_type": "ptag", "name": "!_TAG_PROGRAM"}',   # pseudo-tags are skipped
        "not-json-garbage",
    ])
    calls = {"n": 0}

    def fake_ctags(worktree):
        calls["n"] += 1
        return canned

    monkeypatch.setattr(rc, "_run_ctags", fake_ctags)
    rc.clear_symbol_index(tmp_path)

    out = rc.repo_find_symbol(tmp_path, "LeadSerializer")
    assert "app/serializers.py:14" in out and "class" in out
    # case-insensitive and substring fallbacks
    assert "app/views.py:88" in rc.repo_find_symbol(tmp_path, "leadhistoryview")
    assert "app/tasks.py:5" in rc.repo_find_symbol(tmp_path, "reconcile")
    assert "Try repo_grep" in rc.repo_find_symbol(tmp_path, "NoSuchThing")
    assert calls["n"] == 1                       # index built once, then cached
    rc.clear_symbol_index(tmp_path)
    rc.repo_find_symbol(tmp_path, "LeadSerializer")
    assert calls["n"] == 2                       # release() invalidates

    # no ctags in the deployment -> the tool says so and points at grep
    monkeypatch.setattr(rc, "_run_ctags", lambda wt: None)
    rc.clear_symbol_index(tmp_path)
    assert "unavailable" in rc.repo_find_symbol(tmp_path, "LeadSerializer")
    rc.clear_symbol_index(tmp_path)


def test_repo_locks_are_dropped_after_use():
    # #22: RepoCache._locks was a defaultdict that kept one lock per repo forever
    from reviewer.repo_cache import KeyedLocks

    locks = KeyedLocks()
    order: list[str] = []

    async def user(name, key, hold):
        async with locks.hold(key):
            order.append(f"{name}+")
            await asyncio.sleep(hold)
            order.append(f"{name}-")

    async def scenario():
        await asyncio.gather(user("a", "repo1", 0.02), user("b", "repo1", 0),
                             user("c", "repo2", 0))
        return len(locks)

    assert asyncio.run(scenario()) == 0  # nothing left behind
    assert order.index("a-") < order.index("b+")  # same repo: still exclusive


def test_eviction_runs_tracked_and_its_failure_is_logged(tmp_path, monkeypatch, caplog):
    # #20: run_in_executor(None, self._evict) was fire-and-forget — an exception
    # escaping it was never seen
    from reviewer.config import RepoCacheSection
    from reviewer.repo_cache import RepoCache

    cache = RepoCache(RepoCacheSection(dir=str(tmp_path)))

    def broken_evict():
        raise RuntimeError("disk vanished")
    monkeypatch.setattr(cache, "_evict", broken_evict)

    async def scenario():
        cache._spawn(asyncio.to_thread(cache._evict), "repo-cache-evict")
        assert len(cache._background) == 1  # referenced while it runs
        while cache._background:
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert "repo-cache-evict failed" in caplog.text and "disk vanished" in caplog.text
