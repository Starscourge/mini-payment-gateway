# Design decisions

Short records of choices that the spec leaves open or that are worth defending in a review.
Newest at the bottom. Each one says what was decided and why.

## Milestone 1: Foundation (2026-10-19)

### 1. Prefixed public IDs over raw UUIDs

The API shows IDs like `pay_8c1f…`, `mer_…` and `key_…`, while the database stores plain
UUIDs. The prefix is added in the response schemas and stripped when parsing path parameters
(`app/ids.py`). Prefixed IDs are self-describing in logs and support tickets, and an ID of the
wrong type (a key ID where a payment ID belongs) is rejected as not found instead of being
looked up. A malformed ID returns the same 404 as an unknown one.

### 2. A `description` column on payments

The spec's create-payment example sends `"description": "Order #1042"`, but its `payments`
table has no column for it. Silently dropping data the merchant sent would be worse than a
small schema addition, so `payments.description` is a nullable `TEXT` (at most 255 characters
through the API).

### 3. API keys hashed with SHA-256, looked up by prefix

Passwords need slow hashes (bcrypt, argon2) because people choose guessable ones. An API key
has 256 random bits, so there is nothing to brute-force, and a fast hash keeps per-request
authentication cheap. The key's first 12 characters are stored in clear for lookup, and the
full hash is compared with `hmac.compare_digest`. That prefix holds only 4 random characters
(24 bits), so issuing a key draws again if its prefix is already taken. The `UNIQUE`
constraint backs this up.

### 4. Routers commit; services only flush

Services add rows and `flush()` (which assigns IDs and surfaces constraint errors) but never
commit. The router decides when the unit of work ends. This keeps services composable: in
milestone 3, the idempotency layer will own the transaction so that the stored response
commits together with the business writes.

### 5. Keyset (cursor) pagination on `(created_at, id)`

`next_cursor` is the base64 of the last row's `(created_at, id)`, and the next page asks for
rows strictly older (`WHERE (created_at, id) < (…)`), served by the
`(merchant_id, created_at DESC, id DESC)` index. Unlike `OFFSET`, payments created between
page requests never cause duplicates or skipped rows. `id` breaks ties, because rows created in
one transaction share a `now()` timestamp. Cursors aren't signed: tampering can only move the
starting point, since the merchant filter still applies.

### 6. Amounts are strict integers

`amount_paise` rejects `500.0`, `"500"` and `true` instead of coercing them. Coercion is
convenient, but in a payment API a float or string amount points to a client bug, and the
safest response is a 422.

### 7. Admin token required in production, enforced at startup

`POST /v1/merchants` has no merchant to authenticate as, so outside local development it
needs `X-Admin-Token`. Settings validation refuses to start with `ENVIRONMENT=production` and
no `ADMIN_TOKEN`, so a misconfigured deployment fails loudly instead of running open.

### 8. Request IDs and 500s in one plain-ASGI middleware

`RequestContextMiddleware` assigns or accepts an `X-Request-ID`, binds it to the structlog
context and returns it on every response. It also catches unhandled exceptions, so even a crash
returns the standard error body with a request ID and no internal details. It is plain ASGI
rather than `BaseHTTPMiddleware`, which runs each request in an extra task.

### 9. Tests: real Postgres, savepoint rollback, migrations every run

Each test runs in a transaction that is rolled back. `join_transaction_mode="create_savepoint"`
turns the app's own `commit()` into a savepoint release, so the code under test is unchanged.
The test database schema is rebuilt from the Alembic migrations every run, so the migrations
are tested too, and CI runs `alembic check` to catch models that drift from them.

### 10. No ORM relationships (for now)

Related rows are loaded with explicit queries. Under `AsyncSession`, an accidental lazy load
raises `MissingGreenlet` at runtime, and explicit queries make every round trip visible.
