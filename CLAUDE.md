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

- **reviewer/server.py** — FastAPI app: `POST /webhook` (contract unchanged from v1),
  `GET /` (health + flags), `GET /stats` (token/cost aggregates); asyncio queue with
  N workers; webhook dedupe (exact-SHA TTL + per-MR burst window); lifespan starts the
  bridge listener and GitLab startup checks
- **reviewer/pipeline.py** — stage orchestrator: triage (fast) → review (main) →
  investigator (smart, agentic, complex MRs only) → translate EN→RU → deliver review
  (+impact analysis) and tester report; per-review usage tracking; v1-parity path when
  `PIPELINE_V2=off`; legacy `AI_PROVIDER=gemini` subprocess path as rollback hatch
- **reviewer/ai_client.py** — Anthropic SDK via Cloudflare AI Gateway with OpenRouter
  fallback (Anthropic-compatible `/api/v1/messages`, `models` array failover); response
  cache, rate limiting, agent tool loop, empty-response retry, usage recording
- **reviewer/repo_cache.py** — lazy bare-clone cache (`refs/merge-requests/<iid>/head`,
  detached worktrees, LRU eviction via `REPO_CACHE_MAX_GB`; `REPO_CACHE_EPHEMERAL=true`
  = clone→investigate→remove) + sandboxed read-only repo tools for the investigator
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
| smart (investigator) | `ANTHROPIC_SMART_MODEL`=claude-opus-4-8 | adaptive + effort | 32000/turn |

- **Sonnet 5 runs ADAPTIVE thinking when the `thinking` param is omitted** (changed from
  Sonnet 4.6). Disabling must be explicit, else thinking silently consumes the whole
  output budget on big diffs (zero visible text). See `_primary_params` in ai_client.
- Thinking tokens bill against `max_tokens` — that's why smart tier gets 32k.
- A response that is only the truncation marker is retried once at 4× budget and never cached.
- Fallback chains (OpenRouter, env-overridable): same-class Claude first, then
  cross-vendor: gemini-3.5-flash-lite / gemini-3.6-flash / gpt-5.6-terra / kimi-k3 /
  deepseek. `anthropic/*` via OpenRouter hits the same upstream — cross-vendor entries
  are the real availability hedge.

## Operational gotchas (each cost real money/debugging to learn)

- **GitLab collapses large per-file diffs to empty strings** — changes are fetched with
  `access_raw_diffs=true`; still-collapsed files fall back to current file content with a
  marker. Never silently skip empty diffs.
- **Timed-out Anthropic calls still bill server-side** — `AI_TIMEOUT` default is 300s and
  primary SDK retries are 1; don't lower/raise casually.
- **One user action can emit several webhooks** (reopen → `reopen` + `update` with
  different SHAs) — the per-MR burst window (`DEDUPE_BURST_SECONDS`, default 30) collapses
  them. The queued review reads live MR state, so nothing is lost.
- **Re-reviews are incremental** (dev feedback 2026-07-23: full re-reviews rehashed
  old remarks every push): first review = whole diff; later pushes review only the
  `repository_compare(prev_sha, head_sha)` delta with `INCREMENTAL_REVIEW_NOTE`
  (unfixed earlier findings = author's decision); same-sha events (title/label edits)
  are skipped entirely. State: `cache/reviewed_shas.json` (`review_state.py`,
  bounded, fail-open → full review). A `re-review` label / `[re-review]` title marker
  forces a full fresh review (bypasses dedupe too — the label event's sha is one the
  TTL window would swallow). Infra MRs still need `[no-review]` in the title
  (or a `no-review` label) — e.g. the standing v2→master MR.
- **Reviews are verdict-first and anti-pedantic** (`REVIEW_SYSTEM` noise rules:
  intentional changes are intentional, no hypotheticals, no "confirm/verify" asks,
  empty review is valid). Teams can extend focus via `.ai-review.md` in the repo root
  (target branch, capped 4000 chars, any language).
- **Translation tier is length-routed**: >3500 chars goes to main tier — Haiku left
  long reviews half-English mid-sentence.
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
- Volumes `./logs ./cache ./repos` must be writable by the container user (`useradd -r`
  → UID 999): `sudo chown -R 999:999 logs cache repos` on first deploy.
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
