# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GitLab MR Reviewer — a FastAPI webhook service that reviews GitLab merge requests with a
tiered Claude pipeline: Haiku triage → Sonnet review → Opus investigator (whole-repo agentic
analysis + Jira context via a Telegram bridge to AIManager) → Russian delivery, plus tester
reports. Runs against multiple GitLab instances, notifies Telegram, accounts every token.

**Status: v2 fully rolled out in production since 2026-07-23** (all four feature flags ON).
Design docs: `plans/2026-06-11-v2-architecture.md`, `plans/2026-06-10-review-bridge.md`.

## Architecture (`reviewer/` package)

Entry point: `python -m reviewer` (`reviewer/__main__.py`, the Dockerfile CMD): loads
and validates the config BEFORE uvicorn starts. `w-server.py` is only an import shim
(`bootstrap.app`, config loaded in the lifespan) so `uvicorn w-server:app` keeps working.

- **reviewer/bootstrap.py** — composition root, the ONLY place that reads `.env`
  (`load_config`), configures logging and builds objects: `build_services(cfg, **fakes)`
  → `Services` (settings, Notifier, ReviewBridge, AIClient, ReviewMergeRequest, AnswerNote, ReviewQueue,
  ModelOverrides, OpenRouterCatalog, Pricing, UsageLog, ReviewStateStore, gitlab client
  factory, `bot_usernames`) with `start()`/`stop()` (startup logs, state migration,
  workers, bridge listener, instance check). No module in `reviewer/` holds a
  config or service singleton; importing anything has no side effects.

- **reviewer/server.py** — `create_app(services)`; handlers read
  `request.app.state.services`. `POST /webhook` (MR events, contract unchanged
  from v1, **plus Note Hook** → dialogue jobs on the same queue, deduped by note id and
  exempt from the burst window), `GET /` (health + flags), `GET /stats` (token/cost
  aggregates); `ReviewQueue`: N workers over the durable `JobQueue` port
  (`adapters/storage/jobs.JobStore`, table `jobs`: queued → running → done/failed,
  ≤`JOB_MAX_ATTEMPTS` (2) tries, `recover()` at start re-queues jobs a dead process
  left running, graceful stop waits `SHUTDOWN_TIMEOUT` then hands jobs back; payloads
  store the instance by name, never its token); webhook dedupe via
  `domain.dedupe.DedupePolicy` (exact-SHA TTL + per-MR burst window, injected clock);
  lifespan runs `Services.start()` — the GitLab startup check fills
  `services.bot_usernames` (instance name → bot login, so the bot's own notes are
  dropped at the door; the instance config itself is immutable)
- **reviewer/domain/** — pure layer, no I/O/settings: `models.py` (frozen
  `InstanceRef`, `MergeRequestRef`, `ReviewJob`/`DialogueJob`, `ChangeSet`/`FileChange`,
  `TriageResult`, `ReviewResult`, `Investigation`; StrEnums `Tier`, `Complexity`,
  `JobKind` that compare equal to the plain strings in config/overrides/usage.jsonl),
  `skip.py` (`resolve_skip`), `budget.py` (the big-MR degradation ladder),
  `dedupe.py`, `investigation.py`. Adapters build models at the edge.
- **reviewer/application/** — `ports.py`: `VcsPort` (async: get MR, changes, compare,
  read file, notes, discussions, post note / thread reply, upload; `VcsError`,
  `VcsNotFound`) — the pipeline talks to GitLab ONLY through it, no SDK object crosses
  it. `content.py`: pure review-input assembly from domain models (diff/manifest/
  file-context text, comments, thread rendering, review comment format, Jira keys);
  file reads go through the port.
- **reviewer/adapters/gitlab/** — `GitLabVcs` (VcsPort over python-gitlab): ONE client
  per instance built by bootstrap, `gl.auth()` once at startup (`connect()`, which
  also yields the bot username; the pipeline retries it lazily if GitLab was down at
  boot), lazy project/MR handles so notes/uploads need no extra GET, conflicts read
  from the MR itself. `webhooks.py`: `parse_*_webhook(payload, instance)` → jobs.
- **reviewer/application/review_mr.py** — use case `ReviewMergeRequest`: preflight
  (live MR state, incremental delta, conflicts) → stages from
  `application/stages/` (each `async run(ctx) -> ctx` over a `ReviewContext`):
  `Triage` (fast) → content assembly → `Review` (main, **agentic with repo tools** —
  see below) → `Investigate` (smart, agentic, complex MRs only) → `Translate` EN→RU →
  `Deliver` (review + impact analysis) → reviewed sha recorded → `DeliverTesterReport`.
  One repo session (`RepoWorkspace` port, `repo_cache.CacheWorkspace`: async context
  manager yielding the repo tools, or None when the checkout failed) is shared by the
  tool-assisted review and the investigator. Per-review usage tracking (`kind:
  review|dialogue` in usage.jsonl). The v1-parity (`PIPELINE_V2=off`) and
  `AI_PROVIDER=gemini` paths were removed in refactoring stage 6 (tag `v2-pre-cleanup`
  still has them)
- **reviewer/application/answer_note.py** — use case `AnswerNote`: answers developer
  replies in MR discussion threads (main tier + repo tools, `NO_REPLY` sentinel);
  guards: own note, not a bot thread and no @mention, already answered, per-MR daily
  reply budget (`dialogue_budget.DialogueBudget`, `state/dialogue_replies.json` —
  survives restarts). `application/jobs.JobRunner` routes a queued job to its use case.
  `pipeline.py` is gone (stage 14).
- **reviewer/ai_client.py** — Anthropic SDK via Cloudflare AI Gateway with OpenRouter
  fallback (Anthropic-compatible `/api/v1/messages`, `models` array failover); response
  cache (key includes `max_tokens` + effort), rate limiting, empty-response retry, agent
  tool loop (`_send_turn` / `_run_tools` / `_advance_cache_breakpoint`, one
  `usage.UsageAccumulator` recorded in `finally` on every exit path)
- **reviewer/llm_requests.py** — `RequestBuilder`: routing (gateway vs OpenRouter chain),
  the per-tier thinking/effort policy driven by the model table, request bodies for
  both routes (cache_control kept/stripped, thinking blocks stripped for OpenRouter)
- **reviewer/repo_cache.py** — lazy bare-clone cache (`refs/merge-requests/<iid>/head`,
  detached worktrees, LRU eviction via `REPO_CACHE_MAX_GB`; `REPO_CACHE_EPHEMERAL=true`
  = clone→investigate→remove) + sandboxed read-only repo tools (review/investigator/
  dialogue): `repo_find_symbol` (universal-ctags index, cached per worktree, cleared on
  release — answers "where is X defined" in one call), `repo_grep` (ripgrep engine:
  linear-time regex, .gitignore-aware, `--hidden` for dotfile parity; falls back to the
  Python engine for lookaround/backref patterns and rg-less deployments),
  `repo_read_file`, `repo_list_tree`. Both binaries come from the Dockerfile
  (`ripgrep`, `universal-ctags`); everything degrades cleanly without them.
  Deliberately NOT embeddings/RAG: review questions are exact-identifier lookups,
  and an embedding index would go stale per MR head and add an API dependency —
  ripgrep+ctags is what Claude Code and aider themselves use.
- **reviewer/adapters/knowledge/telegram_bridge.py** — Review Bridge, the
  `KnowledgeSource` port: exclusive `getUpdates` long-polling, asks AIManager questions
  in the bridge group, strips usage footers from answers, `archive()` posts tester
  reports to the bridge chat (AIManager's corpus). Own bot (`BRIDGE_BOT_TOKEN`,
  default `TELEGRAM_BOT_TOKEN`) and chat — independent of the notification channels.
  No global lock: each question has its own inbox, answers are routed by
  `reply_to_message` (an unlinked one goes to the oldest open question; late answers
  to closed questions are dropped). `BRIDGE_MAX_PARALLEL` (default 1 = serialized,
  as before) — whether AIManager's first chunk is linked is unverified (#16).
- **reviewer/usage.py** — per-review token/cost accounting (contextvar tracker), model
  price table (`MODEL_PRICES` override), `UsageLog` (the `usage` table + `logs/usage.jsonl`
  written in parallel), `/stats` = SQL aggregates over the table;
  `UsageTracker.summary()` → the `UsageSummary` a `ReviewPosted` event carries
- **reviewer/adapters/storage/** — `state/reviewer.db` (SQLite, WAL, numbered
  migrations in `sqlite.MIGRATIONS` tracked by `PRAGMA user_version`; append only):
  `reviewed_shas` (`ReviewStateStore`), `kv` (dashboard overrides, import markers),
  `dialogue_replies` (`DialogueBudget`), `usage`. One `Database` per graph, built by
  bootstrap; stores also accept a dir (tests). Fail-open: an unusable STATE_DIR →
  in-memory db + ERROR; failed writes are logged, never raised into a review.
  `legacy_import.import_legacy` (startup, after `state_layout.migrate`) imports
  `reviewed_shas.json` / `model_overrides.json` / `dialogue_replies.json` /
  `usage.jsonl` once each (kv `imports` markers) and leaves the files for rollback.
  Webhook dedupe stays in memory on purpose (monotonic clock, 10-min window).
- **reviewer/json_store.py** — `JsonStore` (lazy read, lock, fail-open, atomic writes via
  temp file + `os.replace`): now only the OpenRouter catalog cache file;
  `atomic_write_text` is also used by the AI response cache (the cache sweep removes
  orphaned `<sha256>.*.tmp` too)
- **Notifications** — the use cases emit domain events (`domain/events.py`:
  `ReviewStarted`, `ReviewPosted`, `ReviewFailed`, `TesterReportReady`, `SystemAlert`;
  structured fields, no text/markup) to the `Notifier` port (`notify(event)`, never
  raises). `adapters/notify/`: `CompositeNotifier` (one failing channel never stops
  the rest), `NullNotifier`; `adapters/notify/telegram/`: `TelegramClient` (wire),
  `TelegramFormatter` (v1 Markdown verbatim, usage footer), `TelegramNotifier`
  (routing: all `TELEGRAM_CHAT_IDS`; tester reports to `TESTER_REPORT_CHAT_IDS`;
  splits >4096-char messages). Channels: `NOTIFY_CHANNELS` / `notify.channels`
  (default `telegram`), built by the `bootstrap.NOTIFIERS` registry.
  **Adding a channel** (Bitrix24): implement `Notifier` + a formatter in
  `adapters/notify/<name>/`, register it in `bootstrap.NOTIFIERS` (and
  `config.KNOWN_CHANNELS`), add it to `CHANNELS` in `tests/adapters/test_notify.py` — the
  contract tests must pass. `bitrix` is a known name that fails startup until then.
- **reviewer/prompts/** — English-only prompts (translation is a stage): the prose in
  `templates/<name>.md` (`REVIEW_SYSTEM` → `review_system.md`, `str.format` fields
  filled at the call site), the user-message builders in `__init__.py`. A `Prompts`
  object (built-ins + `PROMPTS_DIR` overrides, same file names) is built by bootstrap
  and passed to the stages; `prompts.X` at module level reads the built-ins. System
  prompts are the prompt-cache prefix: `tests/application/test_prompts.py` pins them byte for byte
  (`tests/snapshots/prompts.json`) — an intended prompt change updates the snapshot.
- **reviewer/logging_setup.py** — `configure(settings)` (called by bootstrap: format,
  level, `JobContextFilter` on the root handlers, the rotating `logs/ai-debug.log`
  when `AI_DEBUG=on`, fail-open). `job_context(job, usage=tracker)` sets the
  `JobContext` contextvar for a job — log lines get its label, and
  `usage.current_tracker()` / `usage.record()` read the job's tracker from it (the
  separate usage contextvar is gone).
- **reviewer/i18n/** — message catalog `en.yaml` / `ru.yaml` + `t(key, lang, **kw)`
  (dotted keys, `str.format` fields; a key missing in a language falls back to en
  with a WARNING). Every user-facing text (MR notes, notifications) comes from it —
  no `if lang == …` in code. `tests/application/test_i18n.py` pins key parity and the exact
  texts (`tests/snapshots/messages.json`).
- **reviewer/config.py** — pydantic-settings, nested sections (`settings.gitlab`,
  `.llm.tiers.{fast,main,smart}`, `.notify.telegram`, `.bridge`, `.pipeline.stages`,
  `.repo_cache`, `.dedupe`, `.storage`, `.server`, `.network`). Sources: flat env
  names (`ENV_FIELDS`, unchanged from v1) > optional `config.yaml` (`${VAR}` refs for
  secrets) > defaults. Invalid values exit at startup naming the env var
  (`load_settings`); `python -m reviewer.config` prints the effective config with
  secrets masked. `gitlab.routes` (webhook token → instance dict) is the legacy shape
  the pipeline still consumes until stage 9

## Model & thinking policy (hard-won, do not regress)

| Tier | Model (env) | Thinking | max_tokens |
|------|-------------|----------|------------|
| fast (triage, trivial review, translation) | `ANTHROPIC_FAST_MODEL`=claude-haiku-4-5 | none (param omitted) | ≤2048 |
| main (standard review) | `ANTHROPIC_MAIN_MODEL`=claude-sonnet-5 | **explicitly `{"type": "disabled"}`** | 16000 |
| smart (investigator) | `ANTHROPIC_SMART_MODEL`=claude-opus-5 | adaptive + effort | 32000/turn |

- **Sonnet 5 runs ADAPTIVE thinking when the `thinking` param is omitted** (changed from
  Sonnet 4.6). Disabling must be explicit, else thinking silently consumes the whole
  output budget on big diffs (zero visible text). See `RequestBuilder.thinking_params`.
- Thinking tokens bill against `max_tokens` — that's why smart tier gets 32k.
- **claude-opus-5** (2026-07-24) is the smart default: same $5/$25 as opus-4-8, 1M ctx.
  Two behaviour changes to respect — thinking is ON by default (omitting the param
  runs adaptive, unlike 4.8), and `thinking:{"type":"disabled"}` is a 400 at effort
  `xhigh`/`max`. Our fast/main tiers send disabled with NO effort, which is legal;
  never add `effort` there. Elevated cyber safeguards mean a review of auth/crypto
  code can return `stop_reason:"refusal"` -> AIError (review fails cleanly, the
  investigator just skips); server-side `fallbacks` would fix it if it ever bites.
- **claude-sonnet-5-5 rejects `thinking:{"type":"disabled"}`** (400, prod 2026-09-29).
  Its thinking-off mode is `{"type":"between_tools"}` — no other field alongside it,
  legal only at effort `high` or below (so still no `effort` on main). Models with no
  off mode at all (opus-5-5, fable, mythos) get adaptive + effort `low` on the main
  tier. **Per-model capabilities are config, not code**: `config.DEFAULT_MODELS`
  (`supports_thinking`, `thinking_off: disabled|between_tools|none`, `supports_effort`,
  `max_effort_with_thinking_off`, `price`), keyed by id/prefix (longest match at a `-`
  boundary), extendable via `config.yaml llm.models` — a new model is an entry there.
  Unknown ids get explicit `disabled` on main. `test_thinking_params_per_model` pins
  every known model's request.
- **Anthropic prompt caching is OPT-IN** — the investigator's `agent_loop` sets
  `cache_control` breakpoints on the gateway path (static prefix = tools+system+diff,
  plus one rolling breakpoint on the latest tool-result turn; ≤4 per request is the
  API limit). On OpenRouter the markers are kept for `anthropic/*` (Claude does NOT
  auto-cache there — `AI_PROVIDER=openrouter` once billed !493 1.57M input, 0 cached,
  $6.05) and stripped only for other vendors, which auto-cache. Without it every
  iteration re-billed the whole ~180k prefix — that alone made a Sonnet-as-smart
  investigation ~2x the price of the same loop on terra.
- A response that is only the truncation marker is retried once at 4× budget and never cached.
- Fallback chains (OpenRouter, env-overridable): same-class Claude first, then
  cross-vendor: gemini-3.5-flash-lite / gemini-3.6-flash / gpt-5.6-terra / kimi-k3 /
  deepseek. `anthropic/*` via OpenRouter hits the same upstream — cross-vendor entries
  are the real availability hedge.
- **Runtime per-tier overrides** (dashboard → `state/model_overrides.json`): a plain
  `claude-*` id routes via the CF gateway; anything with a `/` (`openai/…`, `google/…`)
  routes via OpenRouter with the tier's fallback chain behind it. The dashboard offers
  the **entire** OpenRouter catalog (`reviewer/openrouter_models.py`, `GET /models`,
  6h-cached, fail-open) as a free-text combo, and prices unknown models from that
  catalog (`Pricing.price_of`: model-table prices + MODEL_PRICES win → live catalog →
  $0). Handy: the
  smart tier on `openai/gpt-5.6-terra` runs a full investigation for ~$0.54 vs Opus
  ~$1.00 and Sonnet-as-smart ~$2.00 (no CF caching), and OpenRouter auto-caches repo
  context (0.1× reads), so agentic loops are far cheaper there than list price implies.

## Operational gotchas (each cost real money/debugging to learn)

- **GitLab collapses large per-file diffs to empty strings** — changes come from `/diffs`
  (paginated; `/changes` is deprecated since 15.7), which has NO `access_raw_diffs`: only
  files it returns collapsed are re-read via `/changes?access_raw_diffs=true` (Gitaly);
  still-collapsed files fall back to current file content with a marker. Never silently
  skip empty diffs. `/diffs` page size stays at GitLab's default 20: GitLab 17.5 answers
  `per_page=50/100` with a 500 (verified 2026-10-08); if `/diffs` fails anyway the
  adapter reads the whole MR through `/changes` instead.
- **Big MRs degrade, they are never refused** (prod: !779 = 655 files / 235k tokens got
  "MR too large to analyze"). Three levers, in order: (1) **triage returns `skip_globs`** —
  the model sees a path/status/size manifest and picks patterns for files not worth
  reading (on !779: `*.svg`, `public/assets/images/**` → 439 files, 235k→145k tokens);
  patterns not paths, because 439 paths overflowed the fast tier's `max_tokens`.
  `resolve_skip` applies them with guards (catch-alls dropped, a verdict matching
  >98% of files is discarded) so a bad triage can't silence a review; skipped files
  are still *listed*. (2) `AI_MAX_INPUT_TOKENS` 300k — every tier model has 1M
  context; 150k was a Gemini-era holdover. (3) full context → diffs-only →
  whole-file-truncated-to-budget, each with a marker saying what was dropped.
  Triage therefore runs BEFORE content assembly in the v2 path.
- **Timed-out Anthropic calls still bill server-side** — `AI_TIMEOUT` default is 300s and
  primary SDK retries are 1; don't lower/raise casually.
- **One user action can emit several webhooks** (reopen → `reopen` + `update` with
  different SHAs) — the per-MR burst window (`DEDUPE_BURST_SECONDS`, default 30) collapses
  them. The queued review reads live MR state, so nothing is lost.
- **Re-reviews are incremental** (dev feedback 2026-07-23: full re-reviews rehashed
  old remarks every push): first review = whole diff; later pushes review only the
  `repository_compare(prev_sha, head_sha)` delta with `INCREMENTAL_REVIEW_NOTE`
  (unfixed earlier findings = author's decision); same-sha events (title/label edits)
  are skipped entirely. State: `state/reviewed_shas.json` (`review_state.py`,
  bounded, fail-open → full review). A `re-review` label / `[re-review]` title marker
  forces a full fresh review (bypasses dedupe too — the label event's sha is one the
  TTL window would swallow). Infra MRs still need `[no-review]` in the title
  (or a `no-review` label) — e.g. the standing v2→master MR.
- **Reviews are verdict-first and anti-pedantic** (`REVIEW_SYSTEM` noise rules:
  intentional changes are intentional, no hypotheticals, no "confirm/verify" asks,
  empty review is valid). Teams can extend focus via `.ai-review.md` in the repo root
  (target branch, capped 4000 chars, any language).
- **The review VERIFIES instead of hedging** (dev feedback 2026-07-31: prompt rules
  alone still let "нужно подтвердить, что…" through, because a diff-only reviewer
  structurally cannot check anything outside the diff). `REVIEW_REPO_TOOLS=on` gives
  the main-tier review the investigator's read-only repo tools (grep/read/tree at the
  MR head) + `REVIEW_TOOLS_NOTE`: check cross-file concerns yourself, cite file:line,
  or say nothing. Non-trivial MRs now clone into the repo cache (shared with the
  investigator, one checkout per review). Any tool-path failure (checkout, refusal,
  no-verdict output) falls back to the plain single-shot review.
- **MR dialogue** (`MR_DIALOGUE=on`): replying to a bot comment or @mentioning the bot
  in an MR triggers a Note Hook → the bot answers in the same thread, checking the
  repo before answering ("Пусть сам подтверждает"). Requires `note_events` ("Comments")
  on project webhooks — enabled by hand in each project's webhook settings. Guards: own-note drop (startup-captured
  bot_username + worker re-check), only bot-threads/mentions answered,
  already-answered check, `DIALOGUE_MAX_REPLIES_PER_MR` (20/day), `NO_REPLY` sentinel
  for acks. Replies are never posted on failure paths (a broken reply must not spam
  the thread).
- **Translation tier is length-routed**: >3500 chars goes to main tier — Haiku left
  long reviews half-English mid-sentence.
- **Cache and state live in separate dirs** (prod bug 2026-10: the AI cache's 48h sweep
  shared `cache/` with the state files and deleted `model_overrides.json` /
  `reviewed_shas.json` — overrides silently reverted on restart). `AI_CACHE_DIR`
  (`cache/ai`) is disposable and the sweep only touches sha256-named entries;
  `STATE_DIR` (`state/`) is durable (`reviewer.db` + the OpenRouter catalog file).
  `state_layout.migrate` moves legacy files at startup (idempotent, an existing file in
  `state/` wins), then they are imported into SQLite once.
- **Zero prompt-cache reads are alerted**: an agent loop of >1 turn reading
  ≥`AI_CACHE_ALERT_MIN_INPUT` (100k) input with 0 cache reads logs a WARNING and sends
  one Telegram alert per review (prod !493: 677k in, 0 cached on the tool review).
- **Accepted webhooks survive deploys** (#10: the in-memory queue dropped them):
  jobs are in `state/reviewer.db` before the webhook answers. A re-run never posts a
  second review: the review comment ends with a hidden
  `<!-- mr-reviewer:review sha=… -->` marker — `Deliver` checks it before posting and a
  retried job (attempt > 1) checks it before spending AI (force_full re-reviews always
  post). Compose `stop_grace_period: 30s` > `SHUTDOWN_TIMEOUT` 20s.
- **Exception text never reaches GitLab** (MR notes are visible to every project member,
  the hook log to maintainers): error paths post only «Ревью не выполнено, id задачи: …»,
  a webhook 500 returns `{"detail": "internal error", "job_id": …}`. `str(exc)` goes to
  the log and the internal Telegram alert only. Every queued job gets a short `job_id`
  (`ReviewQueue.submit`) that appears in every log line of the job (`[<id> <instance>
  <project>!<iid> <kind>]`, stamped by `logging_setup.JobContextFilter`), TG alerts
  and the error note — grep the log for the id from a user's report.
- **Debug/usage logging must never break a review** — logs dir can be unwritable
  (bind-mount ownership); all accounting is fail-open.
- **Translator input is wrapped in `<document>` tags** and output must contain Cyrillic,
  otherwise the English original is delivered — Haiku answers instead of translating
  otherwise.
- **Triage must not extract Jira keys from diff content** (docs/fixtures contain example
  keys) — only branch name, title, description.

## Configuration

`.env` (mounted by compose, never baked into the image) is the only env source —
compose has no `environment:` block any more (its `${VAR:-default}`s silently beat the
code defaults). Optional `config.yaml` (template `config.example.yaml`, git-ignored,
mount line commented in compose) holds structured data. Multi-instance GitLab:
`config.yaml gitlab.instances[]`, or the deprecated env trios `GITLAB_URL[_2.._10]` /
`GITLAB_TOKEN[_N]` / `XGITLABTOKEN[_N]` (startup WARNING; used only when the yaml
defines none) — webhook routing by `X-Gitlab-Token` header match. Instance names
(`primary`, `instance_N`) are part of the review-state key: keep them when migrating.
Telegram channels: `TELEGRAM_CHAT_IDS=a,b` (deprecated: `TELEGRAM_CHAT_ID[_N]`).

Key groups (see `.env.example` for the full annotated list):

- **AI**: `AI_PROVIDER=anthropic`, `ANTHROPIC_API_URL` (CF gateway `/anthropic` route),
  `ANTHROPIC_API_KEY` (real key, x-api-key) + `ANTHROPIC_API_KEY_GATEWAY` (cfut_, sent as
  `cf-aig-authorization: Bearer`), model tiers, `OPENROUTER_API_TOKEN` + fallback chains
- **Flags**: `INVESTIGATOR`, `BRIDGE`, `TESTER_REPORT` — all ON in prod; turning one
  off = flip it + `docker compose up -d`. The tiered pipeline is not a flag: rollback
  to v1 = deploy `master` (or the `v2-pre-cleanup` tag for v2 with the parity/gemini
  paths). `PIPELINE_V2` and the `GEMINI_*` aliases are ignored; startup logs a WARNING
  per retired variable still set (`config.RETIRED_ENV_VARS`, e.g. `GEMINI_PROMPT` →
  `REVIEW_PROMPT`).
  `REVIEW_REPO_TOOLS` / `MR_DIALOGUE` default ON (env `off` to disable);
  `REVIEW_MAX_TOOL_CALLS` (8) budgets both the review's checks and dialogue replies.
- **Bridge**: `REVIEW_BRIDGE_CHAT_ID`, `BRIDGE_QUESTION_TIMEOUT`, `BRIDGE_MAX_QUESTIONS_PER_MR`
- **Repo cache**: `REPO_CACHE_DIR`, `REPO_CACHE_MAX_GB` (LRU) or `REPO_CACHE_EPHEMERAL=true`
- **Stats**: `MODEL_PRICES="model=in/out,..."` ($/MTok override; sonnet-5 intro pricing
  ends 2026-08-31), `TESTER_REPORT_CHAT_IDS` (defaults to all `TELEGRAM_CHAT_ID*`)
- **Dedupe**: `DEDUPE_TTL` (600), `DEDUPE_BURST_SECONDS` (30)

## Development

```bash
uv sync                                  # runtime + dev deps, exact versions from uv.lock
source .venv/bin/activate
.venv/bin/python -m pytest tests/ -q     # offline, no API keys needed — keep it green
.venv/bin/ruff check                     # lint (rules in pyproject.toml)
.venv/bin/mypy                           # strict for domain/ + application/, zero errors
DEBUG=true python -m reviewer             # 0.0.0.0:5000
```

- Dependencies live in `pyproject.toml` (ranges) + `uv.lock` (exact pins); there is no
  `requirements.txt`. Add a dep with `uv add <pkg>` (dev: `uv add --group dev <pkg>`) and
  commit the lock — the image runs `uv sync --frozen`, so a stale lock fails the build.
- mypy: `domain/` and `application/` are strict (per-module flags in pyproject —
  `strict` cannot be set per module), the rest non-strict; no baseline, zero errors.
  The application layer talks to the LLM through `ports.LLMPort`.
- CI: `.github/workflows/ci.yml` (origin is GitHub) runs ruff, mypy, pytest on
  Python 3.12 for pushes to `v2`/`master` and PRs.

Tests are laid out by layer: `tests/domain/` (pure functions, no fakes),
`tests/application/` (stages, use cases, prompts, i18n), `tests/adapters/` (GitLab, LLM,
storage, notify, bridge, repo cache, config, HTTP, logging), `tests/e2e/` (scenarios,
durable queue restarts, composition root); snapshots in `tests/snapshots/`.

**Scenario tests** (`tests/e2e/test_scenarios.py`) drive the real webhook → queue → use case
on in-memory fakes (`tests/fakes/`: `FakeGitLab` behind the python-gitlab object model,
`ScriptedLLM` with per-method answer queues, `FakeTelegram`, `FakeBridge` = scripted
AIManager answers, `FakeRepoCache` = project files in a temp dir under the real repo
tools). The `world` fixture in `tests/conftest.py` builds the real graph with
`bootstrap.build_services(cfg, telegram=…, ai=…, bridge=…, repo_cache=…,
vcs_for=…, clock=…)` (`telegram` = the Telegram channel's transport, `notifier=` replaces
all channels) — no monkeypatching of modules — on prod-shaped settings
(all v2 flags on incl. investigator/bridge/tester report, RU, Telegram on);
`world.configure(llm__max_input_tokens=...)` overrides nested settings per scenario
(`__` = `.`), `world.settings` is that scenario's config and
`world.clock.advance(s)` drives the queue's dedupe TTL / burst window;
`world.send` posts a webhook and drains the durable queue inline. Scenarios cover
trivial/normal/complex (investigator+bridge+tester report) MRs, incremental re-reviews,
big-MR degradation, dialogue and dedupe; they assert external effects only (note texts,
messages, AI tiers, usage) and never call private methods. An unscripted AI call or
bridge question fails the test. State stores are per-graph objects over the
scenario's temp `state_dir`; a new `ReviewStateStore(dir)` on the same dir is a cold start.
Tests never see a developer's `.env` (only `bootstrap.load_config` reads it).

`tests/factories.py`
builds jobs/refs with defaults (`review_job(mr_iid=7, last_commit="abc")`) and graphs
for unit tests: `make_settings(tmp_path, pipeline__language="ru")`,
`make_services(cfg, ai=StubAI())` / `make_review_mr(...)` / `make_answer_note(...)` —
every edge not passed is
inert and fails the test if touched.
Every bug fix gets a regression test in the layer's test module. When checking pytest results
in a shell chain, test `${PIPESTATUS[0]}`, not the pipe's exit code.

## Deployment (production: r.smysl.pro)

```bash
git pull && docker compose up -d --build
```

- Config changes: validate before restarting —
  `docker compose build && docker compose run --rm --no-deps gitlab-mr-reviewer python -m reviewer.config`
  prints the effective config (secrets masked) or the errors that would stop startup.

- Image is `python:3.12-slim` based — **no Node/Gemini CLI**. Full v1 rollback = deploy master.
  CMD is `python -m reviewer`.
- Compose binds `127.0.0.1:5000` — public access only via the reverse proxy (Caddy, TLS).
- Volumes `./logs ./cache ./state ./repos` must be writable by the container user (`useradd -r`
  → UID 999): `sudo chown -R 999:999 logs cache state repos` on first deploy. `state/`
  is new (2026-10): on an existing host run `mkdir -p state && sudo chown 999:999 state`
  BEFORE the first deploy with it, or docker creates it root-owned and state stops
  persisting (startup logs `STATE_DIR ... is not writable`).
- `.env` is mounted read-only; it is NOT in git (secrets) — transfer it manually.
- The Telegram bot token is polled exclusively by this service (bridge listener) — nothing
  else may call `getUpdates` on it; the bot needs group privacy disabled (done) and must be
  a member of the bridge group alongside AIManager.

## Observability

- `GET /stats` — overall totals, per-model aggregates, last 20 reviews (localhost/Caddy),
  SQL over the `usage` table of `state/reviewer.db`
- `logs/usage.jsonl` — one JSON entry per review (tokens, cost, per-model breakdown),
  still appended in parallel; `sqlite3 state/reviewer.db 'select ...'` for ad-hoc queries
- `logs/ai-debug.log` — request/response dumps when `AI_DEBUG=true` (rotating, job-labelled)
- Telegram review notifications end with a usage footer:
  `haiku-4-5: →19448 ←446 | sonnet-5: →104634 ←7457 | 💰$0.63`
- Costs are list-price ceilings. Prompt-cache tokens ARE counted: wire-format
  `input_tokens` excludes them (auto-caching models via OpenRouter report 9-token
  inputs on 100k prompts), so cache read/creation tokens are added to input counts
  and priced at 0.1×/1.25× of the input rate.

## Testing utilities

- `python scripts/check_webhook_routing.py` — POSTs an MR webhook to a running
  service (`WEBHOOK_ENDPOINT`, default `http://localhost:5000/webhook`) with each
  configured instance's `XGITLABTOKEN[_N]`: checks token routing locally. The
  v1-era MR-creating / bulk-webhook scripts were removed (2026-10-07); webhooks
  (Merge request events + Comments) are configured by hand per project.
- To exercise the investigator/bridge: MR with multi-file auth/payment-ish logic and a
  Jira key in the branch name; trivial one-file MRs stop at the Haiku tier by design.

## Backlog

- `BRIDGE_MAX_PARALLEL` > 1 once the log shows AIManager answers always arrive linked
  (`… chunks, N/N linked by reply`)
- Bitrix24 notification channel (`adapters/notify/bitrix/`, see Notifications)
- CF Unified Billing credits top-up (only if switching billing off the direct key)
- AIManager `GUEST_ANSWER_RATE_PER_HOUR` 30→60 once tester reports ramp up
- Merge MR !20 (v2→master) — keep `[no-review]` in its title
- Occasional RU translation artifacts on long reviews
