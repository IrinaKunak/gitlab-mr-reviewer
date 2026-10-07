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

`w-server.py` is a thin shim so `uvicorn w-server:app` keeps working.

- **reviewer/server.py** — FastAPI app: `POST /webhook` (MR events, contract unchanged
  from v1, **plus Note Hook** → dialogue jobs on the same queue, deduped by note id and
  exempt from the burst window), `GET /` (health + flags), `GET /stats` (token/cost
  aggregates); asyncio queue with N workers; webhook dedupe (exact-SHA TTL + per-MR
  burst window); lifespan starts the bridge listener and GitLab startup checks (which
  also capture each instance's `bot_username` so the bot's own notes are dropped at
  the door)
- **reviewer/pipeline.py** — stage orchestrator: triage (fast) → review (main,
  **agentic with repo tools** — see below) → investigator (smart, agentic, complex MRs
  only) → translate EN→RU → deliver review (+impact analysis) and tester report; one
  repo checkout is shared by the tool-assisted review and the investigator;
  `process_note` answers developer replies in MR discussion threads (main tier + repo
  tools, `NO_REPLY` sentinel, per-MR daily reply budget); per-review usage tracking
  (`kind: review|dialogue` in usage.jsonl); v1-parity path when `PIPELINE_V2=off`;
  legacy `AI_PROVIDER=gemini` subprocess path as rollback hatch
- **reviewer/ai_client.py** — Anthropic SDK via Cloudflare AI Gateway with OpenRouter
  fallback (Anthropic-compatible `/api/v1/messages`, `models` array failover); response
  cache, rate limiting, agent tool loop, empty-response retry, usage recording
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
- **reviewer/bridge.py** — Review Bridge: exclusive `getUpdates` long-polling, asks
  AIManager questions in the bridge group, strips usage footers from answers
- **reviewer/usage.py** — per-review token/cost accounting (contextvar tracker), model
  price table (`MODEL_PRICES` override), `logs/usage.jsonl`, `/stats` aggregation,
  Telegram usage footer
- **reviewer/gitlab_io.py / telegram_io.py** — GitLab and Telegram I/O (SOCKS via proxies
  dict); **reviewer/prompts.py** — English-only prompts (translation is a stage);
  **reviewer/config.py** — env/flags

## Model & thinking policy (hard-won, do not regress)

| Tier | Model (env) | Thinking | max_tokens |
|------|-------------|----------|------------|
| fast (triage, trivial review, translation) | `ANTHROPIC_FAST_MODEL`=claude-haiku-4-5 | none (param omitted) | ≤2048 |
| main (standard review) | `ANTHROPIC_MAIN_MODEL`=claude-sonnet-5 | **explicitly `{"type": "disabled"}`** | 16000 |
| smart (investigator) | `ANTHROPIC_SMART_MODEL`=claude-opus-5 | adaptive + effort | 32000/turn |

- **Sonnet 5 runs ADAPTIVE thinking when the `thinking` param is omitted** (changed from
  Sonnet 4.6). Disabling must be explicit, else thinking silently consumes the whole
  output budget on big diffs (zero visible text). See `_primary_params` in ai_client.
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
  tier. `_primary_params` picks per model; new models need a branch there.
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
  catalog (`usage.price_of`: curated MODEL_PRICES win → live catalog → $0). Handy: the
  smart tier on `openai/gpt-5.6-terra` runs a full investigation for ~$0.54 vs Opus
  ~$1.00 and Sonnet-as-smart ~$2.00 (no CF caching), and OpenRouter auto-caches repo
  context (0.1× reads), so agentic loops are far cheaper there than list price implies.

## Operational gotchas (each cost real money/debugging to learn)

- **GitLab collapses large per-file diffs to empty strings** — changes are fetched with
  `access_raw_diffs=true`; still-collapsed files fall back to current file content with a
  marker. Never silently skip empty diffs.
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
  repo before answering ("Пусть сам подтверждает"). Requires `note_events` on project
  webhooks — `add_webhooks_to_all_projects.py` enables it and also UPDATES existing
  hooks (re-run it once after deploying). Guards: own-note drop (startup-captured
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
  `STATE_DIR` (`state/`) is durable. `state_layout.migrate` moves legacy files at
  startup (idempotent, an existing file in `state/` wins).
- **Zero prompt-cache reads are alerted**: an agent loop of >1 turn reading
  ≥`AI_CACHE_ALERT_MIN_INPUT` (100k) input with 0 cache reads logs a WARNING and sends
  one Telegram alert per review (prod !493: 677k in, 0 cached on the tool review).
- **Exception text never reaches GitLab** (MR notes are visible to every project member,
  the hook log to maintainers): error paths post only «Ревью не выполнено, id задачи: …»,
  a webhook 500 returns `{"detail": "internal error", "job_id": …}`. `str(exc)` goes to
  the log and the internal Telegram alert only. Every queued job gets a short `job_id`
  (`ReviewQueue.submit`) that appears in its log lines (`job <id>: …`), TG alerts and
  the error note — grep the log for the id from a user's report.
- **Debug/usage logging must never break a review** — logs dir can be unwritable
  (bind-mount ownership); all accounting is fail-open.
- **Translator input is wrapped in `<document>` tags** and output must contain Cyrillic,
  otherwise the English original is delivered — Haiku answers instead of translating
  otherwise.
- **Triage must not extract Jira keys from diff content** (docs/fixtures contain example
  keys) — only branch name, title, description.

## Configuration

`.env` (mounted by compose, never baked into the image). Multi-instance GitLab via
`GITLAB_URL[_2.._10]` / `GITLAB_TOKEN[_N]` / `XGITLABTOKEN[_N]` — webhook routing by
`X-Gitlab-Token` header match.

Key groups (see `.env.example` for the full annotated list):

- **AI**: `AI_PROVIDER=anthropic`, `ANTHROPIC_API_URL` (CF gateway `/anthropic` route),
  `ANTHROPIC_API_KEY` (real key, x-api-key) + `ANTHROPIC_API_KEY_GATEWAY` (cfut_, sent as
  `cf-aig-authorization: Bearer`), model tiers, `OPENROUTER_API_TOKEN` + fallback chains
- **Flags**: `PIPELINE_V2`, `INVESTIGATOR`, `BRIDGE`, `TESTER_REPORT` — all ON in prod;
  all off = v1-parity. Rollback = flip a flag + `docker compose up -d`.
  `REVIEW_REPO_TOOLS` / `MR_DIALOGUE` default ON (env `off` to disable);
  `REVIEW_MAX_TOOL_CALLS` (8) budgets both the review's checks and dialogue replies.
- **Bridge**: `REVIEW_BRIDGE_CHAT_ID`, `BRIDGE_QUESTION_TIMEOUT`, `BRIDGE_MAX_QUESTIONS_PER_MR`
- **Repo cache**: `REPO_CACHE_DIR`, `REPO_CACHE_MAX_GB` (LRU) or `REPO_CACHE_EPHEMERAL=true`
- **Stats**: `MODEL_PRICES="model=in/out,..."` ($/MTok override; sonnet-5 intro pricing
  ends 2026-08-31), `TESTER_REPORT_CHAT_IDS` (defaults to all `TELEGRAM_CHAT_ID*`)
- **Dedupe**: `DEDUPE_TTL` (600), `DEDUPE_BURST_SECONDS` (30)

## Development

```bash
source .venv/bin/activate
.venv/bin/python -m pytest tests/ -q     # offline, no API keys needed — keep it green
DEBUG=true uvicorn w-server:app --host 0.0.0.0 --port 5000
```

Every bug fix gets a regression test in `tests/test_unit.py`. When checking pytest results
in a shell chain, test `${PIPESTATUS[0]}`, not the pipe's exit code.

## Deployment (production: r.smysl.pro)

```bash
git pull && docker compose up -d --build
```

- Image is `python:3.12-slim` based — **no Node/Gemini CLI**. Full v1 rollback = deploy master.
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

- `GET /stats` — overall totals, per-model aggregates, last 20 reviews (localhost/Caddy)
- `logs/usage.jsonl` — one JSON entry per review (tokens, cost, per-model breakdown)
- `logs/ai-debug.log` — request/response dumps when `AI_DEBUG=true` (rotating)
- Telegram review notifications end with a usage footer:
  `haiku-4-5: →19448 ←446 | sonnet-5: →104634 ←7457 | 💰$0.63`
- Costs are list-price ceilings. Prompt-cache tokens ARE counted: wire-format
  `input_tokens` excludes them (auto-caching models via OpenRouter report 9-token
  inputs on 100k prompts), so cache read/creation tokens are added to input counts
  and priced at 0.1×/1.25× of the input rate.

## Testing utilities

- `python test_webhooks.py` — create test MRs in the configured test repos
  (`spikerwork/test-repo` on primary, `gitlab-instance-0d55f60d/max-test` on instance_2)
- `python add_webhooks_to_all_projects.py [--dry-run|--instance N|--test-endpoint]` —
  bulk webhook management across all projects/instances
- To exercise the investigator/bridge: MR with multi-file auth/payment-ish logic and a
  Jira key in the branch name; trivial one-file MRs stop at the Haiku tier by design.

## Backlog

- `RepoCache._locks` defaultdict never evicts (unbounded per-repo growth) — flagged by
  the reviewer itself on MR !20
- CF Unified Billing credits top-up (only if switching billing off the direct key)
- AIManager `GUEST_ANSWER_RATE_PER_HOUR` 30→60 once tester reports ramp up
- Merge MR !20 (v2→master) — keep `[no-review]` in its title
- Occasional RU translation artifacts on long reviews
