export const meta = {
  name: 'v2-code-review',
  description: 'Multi-dimension review of the v2 branch implementation, with adversarial verification',
  phases: [{ title: 'Review' }, { title: 'Verify' }],
}

const ROOT = '/home/spikerwork/develop/lab.smysl.pro/gitlab-mr-reviwer'
const FILES = 'reviewer/{config,prompts,ai_client,gitlab_io,telegram_io,repo_cache,bridge,pipeline,server}.py, w-server.py, Dockerfile, docker-compose.yml, tests/test_unit.py'

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line_hint: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          description: { type: 'string' },
          suggested_fix: { type: 'string' },
        },
        required: ['title', 'file', 'severity', 'description'],
        additionalProperties: false,
      },
    },
  },
  required: ['findings'],
  additionalProperties: false,
}

const VERDICT = {
  type: 'object',
  properties: {
    is_real: { type: 'boolean' },
    reasoning: { type: 'string' },
    corrected_fix: { type: 'string' },
  },
  required: ['is_real', 'reasoning'],
  additionalProperties: false,
}

const COMMON = `Repo: ${ROOT} (branch v2, commit ef267ab — the whole reviewer/ package is new; v1 reference is "git show master:w-server.py").
Files in scope: ${FILES}.
Context: design doc at plans/2026-06-11-v2-architecture.md. This is a FastAPI webhook service reviewing GitLab MRs with Claude via Cloudflare AI Gateway (anthropic SDK base_url override, cfut_ token in cf-aig-authorization header) and OpenRouter fallback (anthropic-compatible /api/v1/messages, extra_body models array). asyncio queue workers, Telegram getUpdates long-poll bridge, git bare-clone cache with worktrees.
Report EVERY issue you find, including uncertain ones — a separate verification step filters. Include confidence via severity. Do NOT report style nits, naming, or missing type hints. Do not report that Pyright can't resolve imports (venv issue).`

const DIMENSIONS = [
  { key: 'correctness-concurrency', prompt: `${COMMON}
Dimension: correctness and concurrency bugs. Hunt for: race conditions (queue, dedupe dict, bridge inbox/ask lock, repo cache locks defaultdict, rate-limit lock), asyncio misuse (blocking calls in async context, fire-and-forget tasks, run_in_executor misuse), logic errors in the pipeline stage flow and error paths (does every v1 error path still post the right MR note?), off-by-one/None handling, exception flow (what is caught where, what escapes, finally blocks), the agent_loop message accumulation (pause_turn handling, tool_result pairing).` },
  { key: 'api-usage', prompt: `${COMMON}
Dimension: third-party API usage correctness. Verify against the actual installed SDK (.venv/bin/python -c "import anthropic; ..." is available — exercise real signatures where unsure): anthropic SDK params (thinking adaptive on sonnet-4-6/opus-4-8 but NOT haiku, output_config format/effort combos, extra_body, with_options, auth_token vs api_key), whether structured-output json_schema + effort merge is well-formed, Telegram Bot API payloads (sendMessage/sendDocument/getUpdates params), python-gitlab usage (mr.changes(), project.files.get, notes.create, project.upload signature!), git CLI flags used in repo_cache (clone --bare --filter, fetch refspec refs/merge-requests/N/head on bare repo, worktree add --detach on bare repo, worktree remove), httpx proxy kwarg.` },
  { key: 'security', prompt: `${COMMON}
Dimension: security. Hunt for: injection paths (the old code had a command-injection CVE in the wrapper — check the legacy path and any subprocess usage; tokens embedded in git remote URLs appearing in logs/error messages; RepoCacheError includes stderr — does git stderr leak the oauth2:TOKEN URL?), path traversal in repo tools and upload filenames, prompt-injection blast radius (repo content / MR text flows into the investigator which has tools — are tools truly read-only and sandboxed? can ask_aimanager be abused to spam the bridge?), secrets in debug logs (ai-debug.log dumps requests — MR source code retention), webhook auth (timing-safe compare? 401 path), Telegram chat-id spoofing in the bridge dispatch.` },
  { key: 'parity-contract', prompt: `${COMMON}
Dimension: v1 feature-parity and contract drift. Compare against "git show master:w-server.py" carefully. The webhook contract (paths, headers, response JSON shapes, status codes for: unknown token, bad JSON, non-MR event, unsupported action), the URL typo fix, conflict gate REVIEW_FOR_CONFLICT, initial comment posting order, Telegram message formats, error-notification taxonomy and call sites, RU/EN strings, env var compatibility (GEMINI_* aliases actually honored where promised), Docker/compose parity (volumes, healthcheck, port, .env mount), the parity-mode review (PIPELINE_V2=off) matching v1 behavior, the legacy gemini path correctness (cwd, file format it writes vs what wrapper expects).` },
]

phase('Review')
const results = await pipeline(
  DIMENSIONS,
  d => agent(d.prompt, { label: `review:${d.key}`, phase: 'Review', schema: FINDINGS }),
  (review, d) => {
    const findings = (review?.findings || []).slice(0, 12)
    log(`${d.key}: ${findings.length} findings`)
    return parallel(findings.map(f => () =>
      agent(`${COMMON}
Adversarially verify this code-review finding. Read the actual code at the cited location plus enough surrounding context. Default to is_real=false unless the issue demonstrably exists and matters in production (a real bug, leak, contract break — not a theoretical nit). If real but the suggested fix is wrong, give corrected_fix.

FINDING [${f.severity}] ${f.title}
File: ${f.file} ${f.line_hint || ''}
${f.description}
Suggested fix: ${f.suggested_fix || 'none given'}`,
        { label: `verify:${f.title.slice(0, 40)}`, phase: 'Verify', schema: VERDICT })
        .then(v => ({ ...f, dimension: d.key, verdict: v }))
    ))
  }
)

const all = results.filter(Boolean).flat().filter(Boolean)
const confirmed = all.filter(f => f.verdict?.is_real)
log(`${confirmed.length}/${all.length} findings confirmed`)
return confirmed.map(f => ({
  severity: f.severity, dimension: f.dimension, title: f.title, file: f.file,
  line_hint: f.line_hint || '', description: f.description,
  fix: f.verdict.corrected_fix || f.suggested_fix || '',
  reasoning: f.verdict.reasoning,
}))