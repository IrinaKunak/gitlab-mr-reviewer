# GitLab MR Reviewer 🚀

Automated merge-request reviews for GitLab, powered by a tiered Claude pipeline via
Cloudflare AI Gateway (OpenRouter fallback): **Haiku triage → Sonnet review → Opus
investigator** (whole-repo agentic analysis + Jira context through a Telegram bridge to
AIManager) → **Russian delivery**, plus **tester reports** for complex MRs.

Design docs: `plans/2026-06-11-v2-architecture.md`, `plans/2026-06-10-review-bridge.md`.
Operational guidance for AI-assisted development: `CLAUDE.md`.

## ✨ Features

- 🔗 **Multi-instance GitLab** — up to 10 instances, routed by webhook secret token
- 🤖 **Tiered AI reviews** — cheap triage routes trivial MRs away from expensive models;
  complex MRs get a full agentic investigation over a local clone of the project
- 🧠 **Business context** — the investigator asks AIManager about Jira issues in a
  dedicated Telegram group (Review Bridge) and folds answers into the review
- 🧪 **Tester reports** — verification guides delivered to the MR and Telegram
- 💰 **Cost accounting** — per-review usage footer in Telegram, `logs/usage.jsonl`
  ledger, `GET /stats` totals; prices overridable via `MODEL_PRICES`
- 📱 **Telegram notifications** — up to 10 channels, error alerts included
- 🚦 **Sane webhook handling** — retry dedupe, burst collapsing (one user action = one
  review), `[no-review]` title marker / `no-review` label opt-out
- 🔁 **Incremental re-reviews** — the first review covers the whole MR; each next push
  reviews only the new delta (unaddressed earlier remarks are treated as the author's
  decision, not repeated); metadata-only updates don't re-review at all
- 📋 **Verdict-first reviews** — every review opens with SHIP / SHIP WITH FIXES /
  DO NOT MERGE and only reports demonstrable defects (no "confirm your own change",
  no hypotheticals)
- ⚙️ **Per-project guidelines** — an `.ai-review.md` at the repo root (target branch)
  is added to the review prompt: write what to focus on / what to skip, in any language
- 🌍 **English/Russian** — prompts run in English, final output translated to Russian
- 🐳 **Docker deployment**, HTTP/SOCKS proxy support, bulk webhook management scripts

## 🚀 Quick Start

```bash
git clone <your-repo-url> && cd gitlab-mr-reviewer
cp .env.example .env      # fill in GitLab tokens, Anthropic/gateway keys, Telegram
mkdir -p logs cache repos
docker compose up -d --build
sudo chown -R 999:999 logs cache repos   # container runs as uid 999 (appuser)
curl http://localhost:5000/              # health + feature flags
```

The service binds to `127.0.0.1:5000` — put a TLS reverse proxy (e.g. Caddy) in front
for the public webhook URL.

### GitLab webhook

Per project (or bulk via `add_webhooks_to_all_projects.py`):

- URL: `https://<your-domain>/webhook`
- Secret Token: the matching `XGITLABTOKEN[_N]` value (this is how instances are routed)
- Trigger: Merge request events

### Feature flags

| Flag | What it enables |
|------|-----------------|
| `PIPELINE_V2` | tiered pipeline (triage → review → translate); off = v1-parity single pass |
| `INVESTIGATOR` | Opus agentic analysis for complex MRs (clones the repo, read-only tools) |
| `BRIDGE` | AIManager Q&A in the Review Bridge Telegram group |
| `TESTER_REPORT` | tester verification guides (.md on the MR + Telegram) |

Any flag can be turned off and the container restarted for instant rollback.

## 🔍 Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /webhook` | GitLab merge request events (multi-instance via X-Gitlab-Token) |
| `GET /` | health, version, active flags |
| `GET /stats` | overall token/cost totals, per-model breakdown, last 20 reviews |

## 💰 Cost visibility

Every review-completion Telegram message ends with a usage footer:

```
haiku-4-5: →19448 ←446 | sonnet-5: →104634 ←7457 | 💰$0.30
```

Per-review entries append to `logs/usage.jsonl`; `GET /stats` aggregates them. Prices
are list-price ceilings ($/MTok) — override via `MODEL_PRICES=model=in/out,...` when
pricing changes.

## 🛠️ Development

```bash
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q          # offline unit tests, no API keys needed
DEBUG=true uvicorn w-server:app --host 0.0.0.0 --port 5000
```

Testing utilities: `test_webhooks.py` (create test MRs in the configured test repos),
`add_webhooks_to_all_projects.py --dry-run` (bulk webhook management),
`test_gitlab_connection.py`, `test_webhook_local.py`.

## 🧯 Troubleshooting

- **`Errno 13 Permission denied` on logs/cache/repos** — the bind-mounted volumes must
  be writable by uid 999: `sudo chown -R 999:999 logs cache repos`
- **Review posted but empty / marker only** — check `logs/ai-debug.log` with
  `AI_DEBUG=true`; the client retries no-text responses automatically
- **Bridge questions unanswered** — the bot must be a member of the bridge group with
  privacy mode disabled, AIManager must allowlist the bot id (`GUEST_ANSWER_BOT_IDS`),
  and nothing else may call `getUpdates` on the bot token
- **Duplicate reviews** — one user action can emit several webhook events; covered by
  `DEDUPE_TTL` / `DEDUPE_BURST_SECONDS`. Same-SHA retries are suppressed for 10 min.
- **An MR you never want reviewed** (huge infra branches): add `[no-review]` to its
  title or a `no-review` label
- **Full rollback to v1** — deploy the `master` branch (the v2 image has no Gemini CLI;
  `AI_PROVIDER=gemini` works only with the v1 image)

## 📊 Production status

Deployed on `r.smysl.pro` since 2026-07-23 with all flags enabled, serving two GitLab
instances (~380 projects). Verified end-to-end: tiered reviews, repo-clone
investigations, bridge Q&A with AIManager, tester reports, cost accounting.
