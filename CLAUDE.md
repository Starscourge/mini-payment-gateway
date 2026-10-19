# CLAUDE.md

A FastAPI backend that simulates a small payment gateway (INR only, no real money): merchants create payment orders, a mock bank settles them, and money records stay correct under retries, failures and concurrency. It is a portfolio project for backend/fintech SDE roles, so the owner has to be able to explain every design choice in an interview. Prefer clear code over clever code, and briefly explain non-obvious choices when implementing them.

**Source of truth:** `Mini Payment Gateway — Project Spec.md` (quote the path; it has spaces and an em dash). It is deliberately kept out of git (excluded via `.git/info/exclude`) because it holds personal notes, so it exists only on the owner's machine. Never commit it. It holds the full schema SQL, API table, error codes, core flows, security controls, testing matrix and milestones. Read the relevant section before building a feature, and follow it unless the owner agrees to a change. The `.docx` is the same document with the two diagrams embedded. `*:Zone.Identifier` files are Windows download artifacts; ignore them.

## Current status

**Milestone 1 (Foundation) is built.** It covers all 10 tables and the first migration, merchants and API keys, create/get/list payments with the state machine, health checks, Docker Compose (api, postgres, redis) and CI. Not yet verified: CI on GitHub (nothing has been pushed).

**Next is milestone 2 (Money movement):**
- `mock_bank/` and the worker, with their Compose services
- the outbox and `POST /confirm`
- signed bank callbacks
- ledger postings and `GET /v1/balance`
- the expiry sweep

`POST /v1/payments` doesn't require an `Idempotency-Key` yet; that arrives with milestone 3. Follow the spec's "Project structure"; design choices so far are in `docs/decisions.md`. Update this section as milestones complete.

## Commands

Use `uv` for everything. Never `pip install`.

```bash
uv sync                                  # install from uv.lock
uv add <pkg>  /  uv add --dev <pkg>      # runtime / dev dependency (updates the lockfile)
docker compose up -d postgres redis      # needed by integration tests, Alembic and the dev server
uv run uvicorn app.main:create_app --factory --reload   # dev server on :8000, Swagger at /docs
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
for i in $(seq 50); do uv run pytest tests/integration/test_refunds.py -q -x || break; done
```

The loop above is how concurrency tests are vetted: they must pass 50 runs in a row before they are trusted.

## Architecture

Three processes we write (API, worker, mock bank) plus PostgreSQL 16 and Redis 7.

- **The API never calls the bank or a merchant directly.** It writes the state change and an `outbox` row in one transaction; the worker polls the outbox with `FOR UPDATE SKIP LOCKED` and makes the HTTP call (5 s timeout).
- **Postgres is the only source of truth.** Redis holds rate-limit counters only, which can be lost without harm. Never store business state in Redis.
- **No job-queue library** (Celery, RQ, arq, Dramatiq). The Postgres outbox is a deliberate design choice.
- **The mock bank is a separate FastAPI app** (`mock_bank/`) so failures and duplicate callbacks happen over a real network. In tests, replace it with a deterministic fake.
- **Layering:** routers in `app/api/` stay thin. Business logic lives in `app/services/`, which must never import FastAPI, so the worker can reuse it and tests can call it directly. Services `flush()`, routers `commit()` (milestone 3 hands the transaction to the idempotency layer).
- **Map of `app/`:**
  - `main.py`: `create_app()` factory; run with `--factory`
  - `config.py`: Settings
  - `db.py`: engine and session factory
  - `logging.py`: structlog setup, plus the request-ID middleware that also turns crashes into 500s
  - `errors.py`: every error code the API returns
  - `ids.py`: prefixed public IDs
  - `api/deps.py`: session and settings dependencies, `CurrentMerchant` auth
  - `api/error_handlers.py`: renders errors in the one shape

Payment state machine (status only moves forward; refunds never change it):
`created --confirm--> processing --bank success--> succeeded`, `processing --bank failure--> failed`, `created --past expires_at--> expired`.

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
- **Rolled-back transactions:** most tests run inside a transaction that rolls back, and `create_savepoint` turns the app's `commit()` into a savepoint release (fixtures are in `tests/conftest.py`). Concurrency tests need real commits, so they will need their own engine-level fixture that truncates tables afterwards.
- **`now()` is fixed within a test:** it doesn't change inside one transaction, so rows created through the API in one test share a `created_at`. When order matters, insert rows through `db_session` with explicit timestamps (see `insert_payments` in `tests/integration/test_payments.py`).
- **Fixtures:**
  - `client` calls the app
  - `merchant` / `make_merchant()` create merchants through the API (`.headers` holds their bearer auth)
  - `db_session` is the same session the app uses
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
- **Server-updated columns** (`updated_at` via `onupdate=func.now()`) need `__mapper_args__ = {"eager_defaults": True}` (see `Payment`). Without it, the value expires after flush and reading it under `AsyncSession` raises `MissingGreenlet`.
- **Coverage needs `concurrency = ["thread", "greenlet"]`** (set in `pyproject.toml`). Without it, lines after a database `await` are falsely reported as never run.
- **No FK indexes yet:** the spec's schema has none on foreign-key columns (e.g. `refunds.payment_id`, `ledger_entries.account_id`). Add each one in a migration alongside the first query that filters by that column.
- **`astral-sh/setup-uv` has no floating major tags** (no `@v10`, unlike `actions/checkout@v7`). Pin an exact release such as `@v10.3.0`, and check that a tag exists before using it in CI.
- **Listing routes:** in this FastAPI version, `app.routes` holds nested `_IncludedRouter` objects. Use `create_app().openapi()["paths"]` to list endpoints.
- **`POST /v1/merchants`** is open when `ADMIN_TOKEN` is unset. Settings refuses to start with `ENVIRONMENT=production` and no token.
