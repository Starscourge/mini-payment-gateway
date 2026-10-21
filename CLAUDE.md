# CLAUDE.md

A FastAPI backend that simulates a small payment gateway (INR only, no real money): merchants create payment orders, a mock bank settles them, and money records stay correct under retries, failures and concurrency. It is a portfolio project for backend/fintech SDE roles, so the owner has to be able to explain every design choice in an interview. Prefer clear code over clever code, and briefly explain non-obvious choices when implementing them.

**Source of truth:** `Mini Payment Gateway — Project Spec.md` (quote the path; it has spaces and an em dash). It is deliberately kept out of git (excluded via `.git/info/exclude`) because it holds personal notes, so it exists only on the owner's machine. Never commit it. It holds the full schema SQL, API table, error codes, core flows, security controls, testing matrix and milestones. Read the relevant section before building a feature, and follow it unless the owner agrees to a change. The `.docx` is the same document with the two diagrams embedded. `*:Zone.Identifier` files are Windows download artifacts; ignore them.

## Current status

**Milestones 1 (Foundation), 2 (Money movement) and 3 (Correctness under pressure) are built.** Milestone 1 covers all 10 tables, merchants and API keys, create/get/list payments with the state machine, health checks, Docker Compose and CI. Milestone 2 adds `POST /confirm` with the outbox, the worker (`bank_submit` handler and expiry sweep), the mock bank, signed bank callbacks, ledger postings and `GET /v1/balance`, and runs end to end in `docker compose up`. Both are merged into `main`, and CI is green on GitHub (`Starscourge/mini-payment-gateway`, a public repo). Milestone 3 (branch `milestone-3-correctness`) adds required `Idempotency-Key`s on create, confirm and refunds; refunds (`POST`/`GET /v1/payments/{id}/refunds`) under row locks; and concurrency, duplicate-callback and tenant-isolation tests that pass 50 runs in a row.

**Next is milestone 4 (Notifications and protection):** webhook delivery (a `webhook` handler in the worker: sign with the merchant's secret via `app/security/signing.py`, log each attempt in `webhook_deliveries`, reuse the outbox backoff and dead-lettering), `GET /v1/webhook-events` and `POST .../redeliver`, the SSRF guard on webhook URLs, and Redis rate limiting. `payment.*` and `refund.succeeded` events are already queued and sit `pending` until then. Follow the spec's "Project structure"; design choices so far are in `docs/decisions.md` (callbacks match by `payment_id`, a change agreed with the owner: decision 11). Update this section as milestones complete.

## Commands

Use `uv` for everything. Never `pip install`.

```bash
uv sync                                  # install from uv.lock
uv add <pkg>  /  uv add --dev <pkg>      # runtime / dev dependency (updates the lockfile)
docker compose up -d postgres redis      # needed by integration tests, Alembic and the dev server
uv run uvicorn app.main:create_app --factory --reload   # dev server on :8000, Swagger at /docs
uv run python -m app.worker.main         # worker: drains the outbox, expires payments
uv run uvicorn mock_bank.main:create_app --factory --port 8001   # mock bank (needs BANK_CALLBACK_SECRET)
uv run ruff check . && uv run ruff format .
uv run mypy .                            # strict; tests get relaxed signature rules
uv run pytest                            # tests/unit alone needs no Docker
uv run pytest tests/path/test_x.py::test_name -x
uv run pytest --cov                      # branch coverage of app/
uv run pre-commit run --all-files        # every hook: hygiene, uv lock --check, ruff, mypy
uv run alembic revision --autogenerate -m "msg"   # then review the file (see Gotchas)
uv run alembic upgrade head
uv run alembic check                     # fails if the models drifted from the migrations
docker compose up --build                # full stack; the api container migrates on startup
docker compose exec postgres psql -U gateway gateway   # no psql/redis-cli on the host
```

The test database is `gateway_test`. The test suite creates it and rebuilds its schema from the migrations on every run.

```bash
for i in $(seq 50); do uv run pytest tests/integration/test_concurrency.py -q -x || break; done
```

The loop above is how concurrency tests are vetted: they must pass 50 runs in a row before they are trusted. Don't run two pytest processes at once: each run drops and rebuilds the `gateway_test` schema.

## Architecture

Three processes we write (API, worker, mock bank) plus PostgreSQL 16 and Redis 7.

- **The API never calls the bank or a merchant directly.** It writes the state change and an `outbox` row in one transaction; the worker polls the outbox with `FOR UPDATE SKIP LOCKED` and makes the HTTP call (5 s timeout).
- **Postgres is the only source of truth.** Redis holds rate-limit counters only, which can be lost without harm. Never store business state in Redis.
- **No job-queue library** (Celery, RQ, arq, Dramatiq). The Postgres outbox is a deliberate design choice.
- **The mock bank is a separate FastAPI app** (`mock_bank/`) so failures and duplicate callbacks happen over a real network. It is configured by `BANK_*` env vars (decline, duplicate-callback and lost-response rates, delays) and shares only `app/security/signing.py` with the gateway. In tests, the worker talks to a scripted fake (`httpx.MockTransport`, see `FakeBank` in `tests/integration/test_worker.py`); `tests/integration/test_end_to_end.py` runs the real mock bank in-process.
- **The worker** claims one outbox row per transaction (`FOR UPDATE SKIP LOCKED`), runs its handler inside a savepoint, records the outcome and commits. Handlers are registered per `OutboxKind` in `app/worker/main.py`; it claims only kinds it has handlers for. Failures retry with backoff (30 s doubling to 6 h, ±20% jitter) and become `dead` after 10 attempts.
- **Layering:** routers in `app/api/` stay thin. Business logic lives in `app/services/`, which must never import FastAPI, so the worker can reuse it and tests can call it directly. Services `flush()` and never commit. Endpoints that take an `Idempotency-Key` (`IdempotencyDep`) finish with `return await idempotency.complete(response, status_code=...)`, which stores the response and commits; other endpoints call `session.commit()` themselves.
- **Idempotency** (`app/api/idempotency.py`, `app/services/idempotency.py`): the dependency claims `(merchant_id, key)` with `INSERT ... ON CONFLICT DO NOTHING` in the request's transaction. A concurrent duplicate waits on that row (bounded by a 5 s `lock_timeout`, then 409 `idempotency_key_in_use`). A completed repeat is answered by raising `IdempotentReplay` with the stored response (plus `Idempotent-Replayed: true`); the same key with a different method, path or canonical JSON body is 422. A failed request stores nothing, so its key stays free. The worker purges keys after 24 h.
- **Map of `app/`:**
  - `main.py`: `create_app()` factory; run with `--factory`
  - `config.py`: Settings
  - `db.py`: engine and session factory
  - `logging.py`: structlog setup, plus the request-ID middleware that also turns crashes into 500s
  - `errors.py`: every error code the API returns
  - `ids.py`: prefixed public IDs
  - `api/deps.py`: session and settings dependencies, `CurrentMerchant` auth, and `PaymentPathId` (the parsed `{payment_id}` path parameter; declare it after `CurrentMerchant`, so a request without credentials gets 401 rather than 404)
  - `api/error_handlers.py`: renders errors in the one shape
  - `api/idempotency.py`: `IdempotencyDep` and `Idempotency.complete()`
  - `api/bank_callbacks.py`: `POST /internal/bank/callback`, authenticated by HMAC, not an API key
  - `services/payments.py`: the state machine and every flow that moves it (confirm, bank result, expiry)
  - `services/ledger.py`: balanced postings, lazily created accounts, balances
  - `services/refunds.py`: refunds under the payment's row lock
  - `services/idempotency.py`: claim, replay, save and purge keys
  - `services/outbox.py`: enqueue (bank submissions, `payment.*`/`refund.*` events), `claim_next`, retry bookkeeping
  - `security/signing.py`: `X-Signature` sign and verify (bank callbacks now, webhooks in milestone 4)
  - `worker/main.py`: the polling loop and graceful shutdown; `worker/handlers.py`: what each event does

Payment state machine (status only moves forward; refunds never change it):
`created --confirm--> processing --bank success--> succeeded`, `processing --bank failure--> failed`, `created --past expires_at--> expired`.

Settlement: `POST /confirm` sets `processing` and queues `bank_submit` → the worker POSTs to the bank, which replies 202 with a `bank_reference` → the bank's signed callback `{payment_id, bank_reference, status}` sets `succeeded`/`failed`, posts the ledger on success and queues a `payment.*` webhook event. Callbacks lock the payment **by `payment_id`**; whichever of the callback or the worker arrives first stores the reference, and a different one later is a 409. Ledger accounts: `bank_clearing` (system) is debited and the merchant's `merchant_balance` credited.

## Invariants (never break these)

- **Money is integer paise** (`BIGINT` / `int`). Never `float`, and avoid `Decimal` in the domain. Valid amounts are 1 to 10,000,000 paise.
- **Double-entry ledger:** the entries of every `ledger_transaction` sum to zero (+ debit, − credit). Balances are computed from the ledger. `payments.refunded_paise` is only a guard counter backed by a CHECK constraint.
- **One flow, one transaction:** a state change, its ledger postings and its outbox row commit together or not at all.
- **Row locks for contention:** use `SELECT ... FOR UPDATE` on the payment row for confirm, refund and bank callback. A callback for a payment that is already final returns 200 and changes nothing.
- **Idempotency:** `POST /v1/payments`, `/confirm` and `/refunds` require an `Idempotency-Key`. The same key and body replays the stored response; the same key with a different body returns 422.
- **Tenant isolation:** every query filters by `merchant_id` from the authenticated API key. Another merchant's resource returns **404, not 403**.
- **At-least-once webhooks:** each one carries an event ID and an `X-Signature: t=<unix>,v1=<hmac-sha256 of "{t}.{raw body}">` header.
- **One error shape everywhere:** `{"error": {"code", "message", "request_id"}}`. Every response carries `X-Request-ID`.
- **Secrets:** API keys are stored only as SHA-256 hashes and compared with `hmac.compare_digest`. Config comes from environment variables via pydantic-settings. Commit `.env.example`, never `.env`. Logs redact `Authorization` and webhook secrets.

## Code conventions

- Python 3.12, fully type-annotated, clean under strict mypy and ruff. The ruff rules include security (`S`), blocking calls in async code (`ASYNC`), naive datetimes (`DTZ`) and FastAPI checks (`FAST`, e.g. dependencies must use `Annotated`). Fix findings instead of silencing them; any `# noqa` needs a specific code and a reason.
- Pre-commit hooks are installed. If a hook rewrites files, re-stage and commit again. Never bypass hooks with `--no-verify`.
- SQLAlchemy 2.x style only: `Mapped[...]`, `mapped_column`, `select()`, `AsyncSession`. No legacy `session.query()`.
- Pydantic v2 APIs only: `model_dump`, `model_validate`, `ConfigDict`, `field_validator`.
- Timestamps are timezone-aware (`TIMESTAMPTZ`, `datetime.now(UTC)`).
- Redis access goes through `redis.asyncio`, never the sync client in async code.
- **Errors:** raise `AppError` subclasses from `app/errors.py`, never `HTTPException`. Add reusable ones there (as with `PaymentNotFound`), so it stays the list of every error code.
- **IDs:** public IDs go through `app/ids.py` (`format_id`/`parse_id`) and the `*Id` types in `app/schemas/common.py`. A malformed path ID raises the same 404 as an unknown one.
- **No `relationship()`:** load related rows with explicit queries. A lazy load under `AsyncSession` raises `MissingGreenlet`.
- **Endpoint docs:** declare error responses with `error_responses(...)` from `app/schemas/common.py`, so Swagger shows the real error shape.
- Record significant design decisions in `docs/decisions.md` as they are made.

## Testing

- Tests run against real Postgres and Redis (Docker). **Never SQLite**: row locks and `SKIP LOCKED` behave differently.
- Call the app in-process with `httpx.AsyncClient(transport=ASGITransport(app=app))`.
- **Rolled-back transactions:** most tests run inside a transaction that rolls back, and `create_savepoint` turns the app's `commit()` into a savepoint release (fixtures are in `tests/conftest.py`). Concurrency tests need real commits: they use `live_client` (an app with its own engine) and `committed_sessions` (which truncates every table afterwards), and fire requests together with `asyncio.gather` (see `tests/integration/test_concurrency.py`).
- **`now()` is fixed within a test:** it doesn't change inside one transaction, so rows created through the API in one test share a `created_at`. When order matters, insert rows through `db_session` with explicit timestamps (see `insert_payments` in `tests/integration/test_payments.py`).
- **Fixtures:**
  - `client` calls the app
  - `merchant` / `make_merchant()` create merchants through the API (`.headers` holds their bearer auth)
  - `make_payment(owner=None, confirm=False, **fields)` creates (and optionally confirms) a payment through the API
  - `live_client` calls an app that really commits, for concurrency tests
  - `send_bank_callback(payment_id, status, ...)` posts a callback signed with the test bank secret; overrides make it invalid
  - `db_session` is the same session the app uses
  - `committed_sessions` is a session factory that really commits, for tests that need several connections (row locks, `SKIP LOCKED`); it truncates every table afterwards
  - `tests/helpers.py` holds plain helpers that take the client as an argument (`create_merchant`, `create_payment`, `confirm_payment`, `post_bank_callback`, `settle_payment`), plus `to_uuid("pay_...")` and `idempotency_key()`
- **Every POST to create, confirm or refund needs an `Idempotency-Key`:** use `headers=merchant.headers | idempotency_key()` (a fresh key), or pass a fixed key to test replays.
- **A failed request rolls back the shared test session**, including anything the test inserted through `db_session` without committing. Commit test setup first (`await db_session.commit()`, a savepoint release here) when the request under test is expected to fail.
- The `db_session` fixture asserts the ledger invariant (every transaction sums to zero) after every integration test.
- Coverage target is about 85% on `app/services/`.
- Warnings are errors (`filterwarnings = ["error"]`), which catches un-awaited coroutines. If a third-party deprecation warning breaks the run, add a narrow `ignore:` entry rather than removing `error`.

## Gotchas

- **`sqlalchemy[asyncio]` is required.** SQLAlchemy 2.1 no longer installs `greenlet` by default, and the async engine fails without it. Keep the extra.
- **pytest-asyncio 1.x runs in auto mode on one session-wide event loop** (set in `pyproject.toml`). Don't add `@pytest.mark.asyncio`, and don't define an `event_loop` fixture: 1.x removed it, though older tutorials still use it. The shared loop is what lets one session-scoped async engine (and its asyncpg pool) serve every test.
- **Alembic:** initialise with the async template (`alembic init -t async migrations`). Autogenerate misses or mishandles several things this schema uses, so review every generated migration for:
  - the `payment_status` ENUM, which `downgrade` must drop explicitly
  - CHECK constraint changes, which autogenerate doesn't detect
  - the partial index `outbox_due` (`postgresql_where`)
  - `UNIQUE NULLS NOT DISTINCT` (`postgresql_nulls_not_distinct=True`)
- **Server-set columns that are read after insert or update** (`updated_at` via `onupdate=func.now()`, or a `created_at` returned in a response) need `__mapper_args__ = {"eager_defaults": True}` (see `Payment` and `Refund`). Without it, the value isn't loaded after flush, and reading it under `AsyncSession` raises `MissingGreenlet`.
- **Coverage needs `concurrency = ["thread", "greenlet"]`** (set in `pyproject.toml`). Without it, lines after a database `await` are falsely reported as never run.
- **FK indexes are added as queries need them:** the spec's schema has none on foreign-key columns. So far `ledger_entries.account_id` and `refunds.payment_id` are indexed; add others in a migration alongside the first query that filters by that column.
- **`astral-sh/setup-uv` has no floating major tags** (no `@v10`, unlike `actions/checkout@v7`). Pin an exact release such as `@v10.3.0`, and check that a tag exists before using it in CI.
- **Listing routes:** in this FastAPI version, `app.routes` holds nested `_IncludedRouter` objects. Use `create_app().openapi()["paths"]` to list endpoints.
- **`POST /v1/merchants`** is open when `ADMIN_TOKEN` is unset. Settings refuses to start with `ENVIRONMENT=production` and no token.
- **Bank callbacks** are all rejected (401) while `BANK_CALLBACK_SECRET` is unset; production requires it. The API and the mock bank must share the value (Compose sets a dev one).
- **Row locks need `populate_existing=True`** (see `_locked` in `app/services/payments.py`). `SELECT ... FOR UPDATE` alone doesn't refresh an object already in the session's identity map, so the lock would be held over stale values.
- **`SUM(bigint)` is `NUMERIC`** in Postgres and comes back as `Decimal`. Cast sums of money back to `BigInteger` (see `ledger.merchant_balance`).
- **`OutboxEvent` has no `eager_defaults`:** after inserting one, its server-set columns (`created_at`, `next_attempt_at`) aren't loaded, and reading them raises `MissingGreenlet`. Re-select it instead (with `populate_existing=True` if it's already in the session).
- **Compose names the bank service `mock-bank`** (underscores aren't valid in hostnames), so `BANK_URL` is `http://mock-bank:8001`.
- **Idempotent replays aren't byte-identical:** responses are stored as `JSONB`, which reorders object keys. Compare replays as parsed JSON (`response.json()`), not `response.text`.
- **`lock_timeout` also covers unique-index waits** (SQLSTATE `55P03`, `exc.orig.sqlstate` on a SQLAlchemy `DBAPIError`). That is what bounds an idempotency duplicate's wait.
