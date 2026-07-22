# GitLab MR Reviewer v2 — AI Gateway + Tiered Intelligence + Review Bridge (2026-06-11)

> Status: APPROVED 2026-06-11 — all owner decisions made (see § Decisions at the bottom).
> Model refresh 2026-07-23 (owner): main → `claude-sonnet-5` ($2/$10, cheaper than 4.6);
> fallbacks → `gemini-3.5-flash-lite` (fast), `gemini-3.6-flash` (main),
> `gpt-5.6-terra` + `kimi-k3` replacing `gpt-5.5` + `gemini-3.1-pro-preview` (smart).
> Companion doc: `plans/2026-06-10-review-bridge.md` (AIManager side, already designed).

## Goals

1. **Replace `gemini-wrapper.sh`** (Gemini CLI subprocess) with a Python AI client that talks to
   **Anthropic via Cloudflare AI Gateway**, with **OpenRouter as fallback**.
2. **Tiered intelligence**: cheap/fast models for simple steps, the expensive model only for the
   decider/investigator role.
3. **New feature — tester reports**: for an MR, produce a verification guide for a human tester.
   This requires whole-project understanding (not just the diff) and Jira/business context
   (via the Review Bridge → AIManager).
4. **Language policy**: all prompts and internal LLM traffic in **English**; final deliverables
   (GitLab review comment, tester report) translated to **Russian**.
5. **Delivery policy**: code-review reports → GitLab MR comments (as today). Tester reports →
   `.md` document attached to the MR **and** sent to the Review Bridge Telegram chat
   (AIManager/owner triages and forwards to testers/managers).

Non-goals (this iteration): web UI, multi-VCS support, Jira write-back, fine-tuning.
`future.md` (July 2025, VseGPT-based multi-agent consensus) is superseded by this plan; its
KPI "100% feature parity with the current system" is retained.

---

## 1. Model strategy (verified June 2026)

### Primary — Anthropic via Cloudflare AI Gateway

| Tier (env var) | Model | Price in/out per MTok | Used for |
|---|---|---|---|
| `ANTHROPIC_FAST_MODEL` | `claude-haiku-4-5` | $1 / $5 | Triage/classification, issue-key extraction, short-string translation (Telegram texts, comment headers), formatting |
| `ANTHROPIC_MAIN_MODEL` | `claude-sonnet-5` | $2 / $10 | Standard code review (diff + file context), tester-report translation EN→RU |
| `ANTHROPIC_SMART_MODEL` | `claude-opus-4-8` | $5 / $25 | **Decider + Investigator**: agentic loop over repo + bridge Q&A, tester-report authoring, deep review of complex MRs |

Notes:
- Opus 4.8 / Sonnet 4.6: use `thinking: {"type": "adaptive"}`; **no** `temperature`/`top_p`/`budget_tokens`
  (removed on Opus 4.8 — they 400). Effort via `output_config: {"effort": "high"}` for the investigator.
- **Claude Fable 5 deliberately excluded**: 2× price ($10/$50), new tokenizer (+~30% tokens),
  30-day retention requirement, and its cybersecurity safety classifiers can refuse
  security-analysis content — which is literally item 3 of our review checklist.
- **Prompt caching**: stable system prompt first, volatile MR content last; inside the
  investigator loop add `cache_control` breakpoints so repeated repo context is billed at ~0.1×.
  Cache passes through the CF gateway unchanged (it proxies the native Messages API).

### Gateway wiring (verified against developers.cloudflare.com, 2026-06-11)

- Endpoint: `https://gateway.ai.cloudflare.com/v1/{account}/mr-reviewer/anthropic`
  proxies the native Anthropic Messages API. Official `anthropic` Python SDK works with
  `base_url=` set to exactly that URL (SDK appends `/v1/messages`).
- **Auth with the `cfut_` token** (our `ANTHROPIC_API_KEY_GATEWAY`): it is a gateway
  authentication token, passed as header `cf-aig-authorization: Bearer cfut_...`
  (BYOK / Unified Billing mode — the gateway injects/settles the provider credential).
  The SDK still requires a non-empty `api_key` → pass a dummy. If we instead store the real
  Anthropic key (BYOK) or keep direct billing, the same client shape works.
- Free gateway features we get per request: caching (`cf-aig-cache-ttl`), retries
  (`cf-aig-max-attempts`, `cf-aig-backoff`), request timeout (`cf-aig-request-timeout`),
  full request logs/analytics in the CF dashboard.
- Limits to respect: Unified Billing = 200 req/60s per gateway (far above our volume);
  requests >25 MB bypass gateway cache; >10 MB not stored in logs.

### Fallback — OpenRouter (client-side failover)

CF Dynamic Routing cannot target OpenRouter (not in its provider list), so failover lives in
**our code**, triggered on: 429/5xx after SDK retries, gateway timeout, or Anthropic outage.

OpenRouter now exposes an **Anthropic-compatible** `POST https://openrouter.ai/api/v1/messages`
(full Messages API incl. tools, adaptive thinking, cache_control, streaming) → the **same
`anthropic` SDK** is reused with `base_url="https://openrouter.ai/api"` and
`auth Bearer OPENROUTER_API_TOKEN`. It also accepts a `models: [...]` array — one request
auto-degrades down the chain; billed only for the model that served.

Fallback chains (verified slugs + prices, June 2026):

| Tier | Chain (first = same-class Claude, then cross-vendor) |
|---|---|
| fast | `anthropic/claude-haiku-4.5` → `google/gemini-3.5-flash-lite` ($0.30/$2.50) → `deepseek/deepseek-v4-flash` ($0.10/$0.20) |
| main | `anthropic/claude-sonnet-5` → `google/gemini-3.6-flash` ($1.50/$7.50) → `deepseek/deepseek-v4-pro` ($0.44/$0.87) |
| smart | `anthropic/claude-opus-4.8` → `openai/gpt-5.6-terra` ($2.50/$15) → `moonshotai/kimi-k3` ($3/$15) |

Caveat: `anthropic/*` via OpenRouter hits the same Anthropic upstream — the cross-vendor
entries are the real availability hedge. Chains are env-configurable
(`OPENROUTER_FALLBACK_FAST|MAIN|SMART`, comma-separated).

---

## 2. New component: `ai_client.py` (replaces gemini-wrapper.sh)

Single module, async, used by every pipeline stage.

```
AIClient
├── complete(tier, system, messages, tools=None, max_tokens, effort) -> AIResult
├── primary: anthropic.AsyncAnthropic(base_url=ANTHROPIC_API_URL,
│            api_key="dummy", default_headers={"cf-aig-authorization": f"Bearer {cfut}"})
├── fallback: anthropic.AsyncAnthropic(base_url="https://openrouter.ai/api",
│            auth_token=OPENROUTER_API_TOKEN)  + models=[chain] per tier
├── failover policy: RateLimitError/5xx/timeout after SDK retries → fallback; both fail → raise AIError
├── proxy: honors HTTP_PROXY / SOCKS_PROXY via httpx client (replaces global socket monkey-patch)
└── observability: per-call log line (tier, model-served, tokens, cache hits, latency, fallback used)
```

Behaviors preserved from the wrapper (per wrapper analysis):
- Response cache: sha256(prompt+content) key, TTL `GEMINI_CACHE_TTL`→`AI_CACHE_TTL` (default 1 h),
  only successful responses cached, cleanup on access. Now in addition to gateway-side cache.
- Rate limiting: `asyncio.Semaphore` + min-interval (process-safe; replaces the racy `/tmp` file).
- Timeout: per-tier (`AI_TIMEOUT`, default 120 s for main, 600 s for investigator runs), distinct
  timeout error path with the existing RU/EN user-facing messages (too large / timed out / failed).
- Size guard: replace the 1 MB byte cap with a token-aware cap (count via `count_tokens`,
  truncate file context first, never the diff; keep the graceful "split the MR" refusal message).
- Debug logging: `AI_DEBUG` request/response dumps to `logs/` (with rotation — the old log grew
  unbounded and contained full source code).

Obsolete and dropped: stdin/ARG_MAX tricks, temp files, exit-code protocol, stat/sha portability
shims, Node.js + Gemini CLI (Docker base becomes `python:3.12-slim`), `GEMINI_API_KEY`.

Env migration: `GEMINI_*` knobs get `AI_*` equivalents; old names read as aliases for one release.
`GEMINI_PROMPT(_RU)` is replaced by English-only internal prompts (see § 6); the env override stays
supported for the review checklist content but is translated into the pipeline at the translation step.

---

## 3. Pipeline v2 (tiered)

```
webhook (unchanged contract: POST /webhook, X-Gitlab-Token routing, open/update/reopen)
   │
   ▼
queue worker (asyncio.Queue + N workers; replaces fire-and-forget BackgroundTasks;
   dedupe key = (instance, project, mr_iid, last_commit_sha) — kills duplicate webhook retries)
   │
   ▼
[STAGE 0 — context]   fetch MR, conflicts check, mr.changes(), file contents   (no LLM)
   │
   ▼
[STAGE 1 — triage]    FAST (Haiku): classify the MR                            (~$0.005)
   │   outputs JSON: {complexity: trivial|normal|complex, risk_areas[],
   │                  jira_keys[] (from branch/title/description),
   │                  needs_investigation: bool, summary}
   │   structured outputs (output_config.format) → guaranteed parseable
   ▼
[STAGE 2 — review]    MAIN (Sonnet): code review in English                    (~$0.05–0.2)
   │   diff + file context (as today) + triage hints
   │   trivial MRs (typo/docs/lockfile): Haiku writes a 3-line review instead   (~$0.01)
   ▼
[STAGE 3 — decide]    needs_investigation && feature-flag → STAGE 4, else skip
   │   (triage decides; Opus is NOT spent on trivial/normal MRs)
   ▼
[STAGE 4 — investigate] SMART (Opus, agentic tool loop, effort=high)           (~$0.5–3)
   │   tools: repo_grep, repo_read_file, repo_list_tree, mr_data,
   │          ask_aimanager (Review Bridge), gitlab_search_related
   │   produces: impact analysis + TESTER REPORT (English)
   ▼
[STAGE 5 — translate] MAIN (Sonnet): EN→RU for tester report;                  (~$0.02–0.1)
   │                  FAST (Haiku): EN→RU for the review comment
   ▼
[STAGE 6 — deliver]
      • review comment → mr.notes.create (RU)                      [as today]
      • tester report  → POST /projects/:id/uploads (.md) + MR comment linking it (RU)
      • tester report  → Telegram sendDocument to REVIEW_BRIDGE_CHAT_ID (RU)
      • notifications  → existing Telegram channels (RU strings preserved)
      • errors         → existing send_error_notification taxonomy
        (rename `gemini_failure` → `ai_failure`, keep all other types)
```

Integration points in `w-server.py` (from the architecture map): the subprocess block
L630–657 + result handling L659–727 collapse into a call to the orchestrator; the dispatch
seam at L456 (`background_tasks.add_task`) is where the queue goes; `extract_review_content`
(L808–877) becomes Stage 0; dead code `extract_diff_content` (L794–805) is removed.

Estimated cost per MR: trivial ≈ $0.01–0.02, normal ≈ $0.1–0.3, complex with investigation
≈ $1–3.5 (dominated by Opus; prompt caching cuts the loop cost substantially).

---

## 4. Repo context for the investigator (whole-project understanding)

The tester report requires reasoning over the whole project, not the diff. Options considered:

| Option | Verdict |
|---|---|
| A. GitLab API on-demand file fetches | Works for review (today), too slow/chatty for an agentic loop (every grep = N API calls) |
| B. Owner mounts all lab repos as a volume | Works, but manual upkeep for 378 projects |
| C. **Service-managed lazy clone cache** (recommended) | First MR for a project → `git clone` via HTTPS using the existing `GITLAB_TOKEN` (scope `api` covers `read_repository`); subsequent MRs → `git fetch`; checkout MR head SHA into a worktree; LRU eviction by disk budget |

With option C, **no manual repo placement is needed** — the existing tokens already allow
cloning. What the owner provides instead: a disk budget (`REPO_CACHE_DIR`, `REPO_CACHE_MAX_GB`,
suggest 20–50 GB; only projects that actually receive MRs get cloned). Option B remains a
supported override: if a volume of repos is mounted, the cache layer uses it read-only and
skips cloning. Clones happen over the existing HTTP/SOCKS proxy config.

Investigator tools over the worktree (read-only, sandboxed to the worktree path):
`repo_grep(pattern, glob)`, `repo_read_file(path, range)`, `repo_list_tree(path, depth)`.
No shell access — these are Python-implemented tools, immune to prompt-injection-driven
command execution. Diff-touched files are pre-seeded into context to anchor the loop.

---

## 5. Review Bridge client (reviewer side of `2026-06-10-review-bridge.md`)

AIManager's side is done (gates, rate limit 30/h, `answerGuestQuery` replies). Our side:

- **Identity**: our `TELEGRAM_BOT_TOKEN` bot (id `7745319318`) is already allowlisted in
  `GUEST_ANSWER_BOT_IDS` and the group `REVIEW_BRIDGE_CHAT_ID=-5288630456` exists. ✔
- **New capability — receiving**: today the bot is send-only (`requests.post sendMessage`).
  The bridge requires reading AIManager's answers → add a **long-polling listener**
  (`getUpdates` with `allowed_updates=["message"]`, offset-tracked, runs as an asyncio task
  in the same process). ⚠ `getUpdates` is exclusive per bot token — nothing else may poll
  this token (verify; see Open Questions). Webhook mode is the alternative but needs a public
  HTTPS route for Telegram → polling is simpler behind our proxies.
- **`ask_aimanager(question)` tool semantics** (per the bridge protocol):
  - one focused question per message, plain text, posted to the bridge chat;
  - include the Jira key when known (triage extracts it from branch/MR title);
  - await reply linked via `answerGuestQuery` + collect subsequent plain replies from
    AIManager for a grace window (answers can span several messages);
  - timeout 90 s (engine latency is 15–60 s); on timeout/`"не нашёл"` → tool returns
    "no answer", investigator proceeds without blocking the pipeline;
  - strip the trailing `<code>sonnet: …</code>` usage-footer line before returning;
  - respect AIManager's rate limit: client-side cap (default 10 questions/MR, sliding
    window shared across MRs kept under `GUEST_ANSWER_RATE_PER_HOUR`).
- **Question language**: English (policy: all LLM-to-LLM traffic in English; the engine
  answers in the question's language per the bridge doc — answers may quote Russian Jira
  text, which the investigator handles fine).
- Typical question set per investigation: what is issue X (status/AC/links); what parts of
  the project does X touch; how is this verified on production today.

---

## 6. Language policy implementation

- **All prompts English-only**, stored in `prompts.py` (versioned in git, env-overridable):
  triage, review, investigator, tester-report, translation. `GEMINI_PROMPT_RU` retires.
- Pipeline runs and reasons in English end-to-end; logs/cache store English originals.
- **Translation is an explicit stage** (not "write in Russian directly" — per owner decision):
  tester report EN→RU via Sonnet (quality matters, it's a human-facing document);
  review comment EN→RU via Haiku (cheap, simple text). Translator prompt forbids
  paraphrasing code identifiers, paths, commands, and markdown structure.
- English originals are kept alongside (`logs/` + optionally a collapsed
  `<details>` block in the GitLab comment) for debugging quality issues.
- Existing RU/EN UI strings (Telegram notifications, error comments) stay as-is
  (`REVIEW_LANGUAGE` keeps working).

## 7. Tester report — content contract

Produced by the investigator (English), translated to Russian. Sections:

1. **Что проверяем** — issue summary (from Jira via bridge) + what the MR changes in product terms.
2. **Затронутые области** — affected screens/endpoints/flows, derived from repo analysis
   (callers/usages of changed code), with risk ranking.
3. **Сценарии проверки** — numbered step-by-step verification scenarios on production/stage:
   preconditions, steps, expected result; includes negative cases for the risk areas.
4. **Регрессия** — adjacent functionality worth a smoke check (from whole-project impact).
5. **Источники** — Jira keys, AIManager answers used, key files inspected (transparency for
   the owner reviewing the report before forwarding).

Delivery: `tester-report-{project}-MR{iid}.md` → GitLab upload attached to MR comment +
`sendDocument` to the bridge chat with a short caption (project, MR link, issue keys).

---

## 8. What stays untouched (feature parity, per codebase survey)

- Webhook contract: `POST /webhook`, port 5000, `X-Gitlab-Token` multi-instance routing
  (378 live project webhooks depend on it), URL-typo fix `/mergerequests/`→`/merge_requests/`.
- Multi-instance env scheme `GITLAB_URL[_N]/GITLAB_TOKEN[_N]/XGITLABTOKEN[_N]` (≤10).
- Conflict detection + `REVIEW_FOR_CONFLICT` gate; initial "review started" MR comment.
- Telegram notification formatting, multi-channel (≤10), error-notification taxonomy.
- Proxy support (HTTP/SOCKS) — but implemented per-client (httpx), removing the global
  `socket.socket` monkey-patch.
- Health endpoint `GET /` (version bumps to 2.x).

## 9. Migration & rollout

Recommended shape: incremental, behind feature flags, w-server.py refactored in place into a
package (`reviewer/` modules: `server.py`, `pipeline.py`, `ai_client.py`, `repo_cache.py`,
`bridge.py`, `gitlab_io.py`, `telegram_io.py`, `prompts.py`) while keeping the `w-server:app`
entrypoint shim so Docker/CMD/docs keep working.

| Phase | Delivers | Flag |
|---|---|---|
| 1 | `ai_client.py` (CF gateway + OpenRouter fallback), wrapper retired, Docker → python-slim, parity review via Sonnet | `AI_PROVIDER=anthropic` (present already; `gemini` = legacy path during transition) |
| 2 | Tiered pipeline: Haiku triage + structured outputs, trivial-MR cheap path, translation stage, queue worker | `PIPELINE_V2=on` |
| 3 | Repo cache + investigator (Opus tool loop) without bridge | `INVESTIGATOR=on` |
| 4 | Bridge client (polling listener, ask_aimanager tool) | `BRIDGE=on` |
| 5 | Tester report generation + delivery (uploads + bridge chat) | `TESTER_REPORT=on` |
| 6 | Hardening: metrics endpoint, cost accounting per MR, unit tests (pytest), eval set of historic MRs | — |

Each phase independently shippable & revertible by flag. Rollback of phase 1 = `AI_PROVIDER=gemini`
(wrapper kept in tree until phase 2 stabilizes).

Testing: existing integration scripts stay valid (webhook contract unchanged); add unit tests
for ai_client failover, triage JSON parsing, bridge footer-stripping/multi-message assembly,
repo cache eviction; replay a corpus of past MR webhooks against a staging gateway.

## 10. New/changed configuration

```env
# present already
AI_PROVIDER=anthropic
ANTHROPIC_API_URL=https://gateway.ai.cloudflare.com/v1/<acct>/mr-reviewer/anthropic
ANTHROPIC_API_KEY_GATEWAY=cfut_...        # sent as cf-aig-authorization: Bearer
ANTHROPIC_MAIN_MODEL=claude-sonnet-5
ANTHROPIC_FAST_MODEL=claude-haiku-4-5
ANTHROPIC_SMART_MODEL=claude-opus-4-8
OPENROUTER_API_TOKEN=sk-or-...
REVIEW_BRIDGE_CHAT_ID=-5288630456

# new
OPENROUTER_FALLBACK_FAST=anthropic/claude-haiku-4.5,google/gemini-3.5-flash-lite,deepseek/deepseek-v4-flash
OPENROUTER_FALLBACK_MAIN=anthropic/claude-sonnet-5,google/gemini-3.6-flash,deepseek/deepseek-v4-pro
OPENROUTER_FALLBACK_SMART=anthropic/claude-opus-4.8,openai/gpt-5.6-terra,moonshotai/kimi-k3
AI_CACHE_TTL=3600            AI_TIMEOUT=120
AI_DEBUG=true                AI_MAX_INPUT_TOKENS=150000
REPO_CACHE_DIR=/app/repos    REPO_CACHE_MAX_GB=30
PIPELINE_V2=off  INVESTIGATOR=off  BRIDGE=off  TESTER_REPORT=off   # flags, per phase
BRIDGE_QUESTION_TIMEOUT=90   BRIDGE_MAX_QUESTIONS_PER_MR=10
```

## 11. Asks for the owner (outside this repo)

**GitLab** — no server-side addons strictly required. Checklist:
- Confirm `GITLAB_TOKEN[_N]` have `api` scope (clone over HTTPS works) — likely already true.
- Process convention for teams: **Jira issue key in branch name or MR title** (e.g.
  `PBV-123-fix-cart`); without it the investigator works from code context only and bridge
  answers are weaker. (Could later be enforced via push rules — optional.)
- Optional, later: add **Note Hook** (comment events) to webhooks if we want on-demand commands
  like `/deep-review` in MR comments — `add_webhooks_to_all_projects.py` can be extended.
- Disk on the host for the repo cache volume.

**AIManager** — already capable per the bridge doc. Nice-to-haves:
- Consider raising `GUEST_ANSWER_RATE_PER_HOUR` (30 → 60) once tester reports go live
  (a complex MR may ask 3–8 questions; several MRs/hour could brush the cap).
- Confirm the machine-caller block makes the engine answer **in English** when asked in English.
- Optional: a compact "issue card" answer template for machine callers (status / AC / links
  as fixed bullets) — easier downstream parsing, not required.

## Decisions (owner, 2026-06-11)

1. **Investigator trigger**: triage decides — only MRs classified `complex / needs_investigation`
   get the Opus loop + tester report. (Manual `/deep-review` trigger may be added later via Note Hook.)
2. **Repo context**: service-managed lazy clone cache using existing `GITLAB_TOKEN`s.
   **No manual repo placement needed** — owner provides only a disk volume (~20–50 GB).
3. **Telegram receiving**: reviewer bot token is free for exclusive `getUpdates` polling —
   the service runs the long-polling listener internally (no public webhook route needed).
4. **Refactor shape**: in-place package refactor behind feature flags, keeping the
   `w-server:app` entrypoint, webhook contract, and env names.
