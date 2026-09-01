# pipeline

Email → GitHub issue automation. A Gmail push notification (via Google
Pub/Sub) arrives at a FastAPI webhook, and a matching, allowlisted, `[TASK]`-
prefixed email gets classified by an LLM and turned into a GitHub issue.

This is a separate service from the Flask voice-interview app in the rest of
the repo — it has its own entry point, its own state (SQLite), and its own
deploy.

## Flow

```
Gmail inbox
   │  users.watch() registered at boot (pipeline/watch.py)
   ▼
Pub/Sub topic --push--> POST /gmail/webhook (pipeline/app.py)
   │  verify Google OIDC JWT
   │  decode envelope -> (emailAddress, historyId)
   ▼
Bridge.handle_notification (pipeline/bridge.py)
   │  list new message ids since the last cursor (history.list)
   │  for each message, in isolation:
   │    fetch -> allowlist gate -> [TASK] subject gate -> LLM classify -> create issue
   ▼
GitHub issue (pipeline/github_client.py)
```

State lives in SQLite (`pipeline/storage/db.py`): the history cursor, a
dedup/claim table per message, and an events log for auditing what happened
to each message.

## Files

| File | Responsibility |
|---|---|
| `app.py` | FastAPI app: `/health`, `/gmail/webhook`. JWT verification, envelope decoding, lifespan startup (db init + watch registration). |
| `bridge.py` | Core logic: `Bridge.handle_notification` walks new messages, `Bridge.process_message` runs the gates/LLM/issue-creation pipeline for one message. `build_bridge()` is the only impure factory (real Gmail/GitHub/Anthropic clients); everything else takes them injected, so tests construct a `Bridge` with fakes. |
| `watch.py` | Registers Gmail's `users.watch()` and renews it daily (Gmail expires a watch after 7 days). Boot registration is non-fatal — a failure (e.g. rate limit) is retried in the background instead of crashing the process. |
| `gates.py` | The two cheap, pure pre-LLM filters: sender allowlist, `[TASK]` subject prefix. Run before the LLM so a rejected email never costs a token. |
| `llm.py` | Wraps the Anthropic call that classifies a parsed email into project/title/description/acceptance/actionable. |
| `github_client.py` | GitHub issue creation, dedup lookup by message id, issue body formatting. |
| `config.py` | Lazy `Settings` from env vars (`get_settings()`, cached). Never raises on import; callers that need a secret call `settings.require(...)`. |
| `models.py` | Shared dataclasses/pydantic models. |
| `gmail/parser.py` | Turns a raw Gmail API message into `{message_id, sender, subject, body, ...}`. |
| `auth/gmail_auth.py` | Builds the authenticated Gmail API client from stored OAuth credentials/token. |
| `storage/db.py` | SQLite access: history cursor, `processed_messages` claim/dedup table, `events` log. Fresh connection per call (webhook runs on a threadpool; SQLite connections aren't thread-portable). |
| `scripts/list_recent.py` | Manual/debug script to list recent Gmail messages. |

## Running

```bash
uvicorn pipeline.app:app --reload
```

Health check: `GET /health`.

## Configuration

All via environment variables (see `config.py`), loaded through `.env` if
present:

| Var | Purpose |
|---|---|
| `GMAIL_CREDENTIALS` | Path to Gmail OAuth client credentials (default `credentials.json`). |
| `GMAIL_TOKEN` | Path to the stored Gmail OAuth token (default `pipeline/token.json`). |
| `GMAIL_TOKEN_B64` | Base64 token as an alternative to a token file (e.g. for deploys without a writable/pre-seeded filesystem). |
| `DB_PATH` | SQLite file path (default `pipeline/pipeline.db`). |
| `PUBSUB_TOPIC` | Full Pub/Sub topic name Gmail should push to. Required for `users.watch()`. |
| `WEBHOOK_AUDIENCE` | Expected `aud` claim on the Pub/Sub push JWT. Unset means every webhook call 401s (logged loudly at boot). |
| `PUBSUB_SA_EMAIL` | Expected service-account email on the JWT, if you want to pin it. |
| `ALLOWLIST` | Comma-separated sender addresses allowed to open issues. |
| `GITHUB_TOKEN` / `GITHUB_REPO` | Issue creation target. |
| `ANTHROPIC_API_KEY` | LLM classification. |
| `PIPELINE_AUTOWATCH` | Set to `1` to register/renew the Gmail watch at boot. Off by default so importing the module in tests never calls Google. |

## Resilience notes

A few failure modes bit this service in production and were fixed
deliberately — worth knowing before touching `bridge.py` or `watch.py`:

- **Boot must never crash on `users.watch()`.** Gmail rate-limits watch
  registration; a 429 at startup used to propagate out of the FastAPI
  `lifespan` and kill the container, which Railway then restarted straight
  into the same rate limit. `register_watch()` now logs and retries every 5
  minutes in the background instead of raising.
- **A bad message must not block the batch or wedge the cursor.**
  `handle_notification` processes each message id in its own try/except; any
  failure drops that one message and moves on. The history cursor always
  advances at the end of the batch, so a single poisoned message (an LLM call
  failing because Anthropic credits ran out, say) can't turn into an infinite
  Pub/Sub retry loop by permanently stalling the cursor.
- **Dropped messages are marked terminal.** `processed_messages.issue_number`
  is `NULL` (claimed, in flight), `0`/`DROPPED` (decided, no issue — gate
  reject, LLM reject, deleted message, or failure), or the real issue number.
  Marking rejects and failures `DROPPED` means an overlapping Gmail history
  window (they overlap by design) skips the message on sight instead of
  re-fetching it and re-scanning GitHub for it every time.
- **A deleted message is not an error.** Gmail 404s a `messages.get` for a
  message that's since been deleted; that's expected and is logged at INFO
  and marked dropped, not logged as an exception traceback.
- **Trade-off to know:** message-level isolation means a *transient* failure
  (a one-off GitHub 5xx, a momentary Gmail hiccup) also drops the message
  permanently — there's no per-message retry. It's auditable via the
  `events` table (`process_error`, `message_gone`, etc.), but not
  auto-recovered.

## Testing

```bash
pytest tests/test_pipeline_gates.py
```

Tests construct a `Bridge` directly with fakes (`FakeGmail`, `FakeRepo`,
`FakeLLM` in `tests/conftest.py`) rather than patching — no network calls.
