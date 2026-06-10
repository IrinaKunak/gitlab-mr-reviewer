# Review Bridge — git-reviewer bot ⇄ AIManager (2026-06-10)

## Goal

The company's git-reviewer bot (separate project, already exists) needs Jira
and project context while reviewing commits/MRs: what issue is being fixed,
how the change affects the project, and how a tester can verify it on
production. It gets that by **asking AIManager questions in a dedicated
Telegram group** ("review bridge"). AIManager answers from the synced Jira
corpus + chat history via the existing agentic query engine.

Hard requirement from the owner: **only AIManager answers — the private
Scribe bot must never respond to the reviewer.**

## Why Telegram as the transport (and not an internal API)

- Bot API 10.0 (May 2026) added sanctioned bot-to-bot messaging:
  `guest_message` updates + `answerGuestQuery`. We already wired it for the
  Scribe Bridge group (commits `4f0b7a8`, `ccb30c5`, `954b642`, aiogram 3.28).
- The reviewer bot can be hosted anywhere — the bot host sits on a home LAN
  behind proxies, so an internal HTTP API would need tunneling; Telegram
  solves reachability and identity (Telegram sets `from_user` server-side,
  another bot cannot spoof an allowlisted bot id).
- Every question and answer is visible to the owner in the group — built-in
  audit log.

## Topology

```
┌─────────────────────── Review Bridge (new group) ──────────────────────┐
│  Maxim (owner) · AIManager (group bot) · git-reviewer bot              │
│                                                                        │
│  reviewer ──"Что за задача PBV-123? Как тестировать на проде?"──▶      │
│  AIManager ──(agentic search over jira_*, messages, docs)──▶ answer    │
└────────────────────────────────────────────────────────────────────────┘
```

Scribe (private bot) is **not a member** of this group, so it physically
never receives the traffic. Defense in depth: even if it were added, the
designated-answerer gate (below) keeps it on the passive path.

## How a question is answered

1. Reviewer bot posts a **text** message in the review bridge.
2. AIManager receives it as a `guest_message` update (or a plain bot-authored
   `message`). `AuthMiddleware` authorizes the review bridge chat id.
3. `_is_active_guest()` gates (ALL must hold, else passive ingest path as
   today):
   - `GUEST_ANSWER_ENABLED=true` and `REVIEW_BRIDGE_CHAT_ID` configured;
   - message is text (bot-posted documents/photos stay passive);
   - chat is the review bridge;
   - sender bot id ∈ `GUEST_ANSWER_BOT_IDS` allowlist;
   - **receiving bot id == group bot id** (derived from the token prefix —
     this is the "Scribe never answers" guarantee in code).
4. Rate limit: sliding window per caller bot (`GUEST_ANSWER_RATE_PER_HOUR`,
   default 30). Exceeded → cheap fixed reply `⏳ Rate limit exceeded`, no LLM
   call.
5. Full pipeline runs with `bot_caller=True`:
   - router context gains a machine-caller block (structured, self-contained
     answers, no pleasantries, no counter-questions);
   - **action whitelist**: only `search` and `just_respond`. Anything else the
     router picks (reminders, exports, manage_chats, schedules) → fixed
     refusal, executor never runs;
   - persona voice layer is skipped (machine consumes the answer);
   - per-chat conversation history works as usual → follow-up questions OK.
6. Reply: first chunk via `answerGuestQuery` (links the answer to the query
   for the asking bot; exactly one result per query id), overflow chunks and
   fallback via plain `reply()` — AIManager is a group member, plain replies
   deliver (the passive ingest path already proves this in prod).

## Config (all off by default — prod-safe deploy)

| Variable | Meaning |
|----------|---------|
| `GUEST_ANSWER_ENABLED` | Master switch (default `false`) |
| `REVIEW_BRIDGE_CHAT_ID` | The dedicated group's chat id |
| `GUEST_ANSWER_BOT_IDS` | Comma-separated bot user ids allowed to ask |
| `GUEST_ANSWER_RATE_PER_HOUR` | Per-caller sliding window (default 30) |

The designated answerer needs no config: it is derived from
`TELEGRAM_BOT_TOKEN_GROUP` (`<bot_id>:` prefix).

## Security posture

- Allowlist by bot id; group membership controlled by the owner.
- Action whitelist = read-only surface (`search` uses the read-only
  `bot_reader` SQL role; no write tools on this path).
- Prompt injection via MR text relayed by the reviewer: worst case is a wrong
  search answer; there is no write/action surface to abuse.
- Cost capped by the rate limit; usage footer on every answer keeps spend
  visible in-group.

## Question protocol (for the reviewer-bot developer)

- One focused question per message, **plain text**.
- Include the Jira issue key when known (branch names / MR titles usually
  carry it): `"Что за задача PBV-123? Статус, критерии приёмки, ссылки."`
- Good question set for a review:
  1. What is issue X about (status, summary, acceptance criteria, links)?
  2. What parts of the project does X touch / relate to (linked discussion)?
  3. How can a tester verify X on production — concrete steps?
- Russian or English — the engine answers in the question's language.
- Follow-ups are fine (history-aware, last 3–5 turns per chat).
- Expect 15–60 s latency (agentic search) and possible "не нашёл" answers.
- The final `<code>sonnet: …</code>` line of an answer is a usage footer —
  metadata, strip it before parsing.
- Long answers arrive as several messages: the first is the linked
  `answerGuestQuery` reply, the rest are plain replies from AIManager.

## Rollout

1. Create the group, add **AIManager + reviewer bot + owner only**.
2. Get the chat id: send any message there and read the bot log — rejected
   chats are logged by `AuthMiddleware` at debug level (`DEBUG=true`), or
   forward a message to @RawDataBot.
3. Get the reviewer's bot id (its token prefix, or @RawDataBot on any of its
   messages).
4. `.env`: `REVIEW_BRIDGE_CHAT_ID=…`, `GUEST_ANSWER_BOT_IDS=…`,
   `GUEST_ANSWER_ENABLED=true` → `docker compose build bot && docker compose
   up -d bot`.
5. Rollback: `GUEST_ANSWER_ENABLED=false` + restart — behavior reverts to
   passive (bit-for-bit pre-feature).

## Files

| File | Change |
|------|--------|
| `config/settings.py` | 4 new settings + `guest_answer_bot_id_set`, `group_bot_id` properties |
| `src/middlewares/auth.py` | authorize `review_bridge_chat_id` alongside the Scribe Bridge |
| `src/core/router.py` | `bot_caller` flag → machine-caller context block |
| `src/handlers/message_handler.py` | `_is_active_guest`, rate limiter, `_send_guest_answer`, action whitelist, voice skip |
| `tests/test_review_bridge.py` | 19 tests pinning the gates, rate limit, reply mechanism |

## Out of scope (this PR)

- The reviewer bot itself (separate project).
- Write-back to Jira from review context (Phase 2 design:
  `2026-06-04-jira-phase2-write-back.md`).
- Multi-result answers via `answerGuestQuery` media types — text only.
