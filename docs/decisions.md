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

## Milestone 2: Money movement (2026-10-21)

### 11. Bank callbacks find the payment by `payment_id`, not by `bank_reference`

The spec looks callbacks up by `bank_reference`, but the gateway learns that reference only
from the bank's 202 reply, which the worker saves afterwards. A fast bank, or a 202 lost to a
timeout, can deliver the callback first; a lookup by reference would reject it, and the payment
could stay `processing` for good. So the callback carries `payment_id` and `bank_reference`,
and the handler locks the payment by ID. If no reference is stored yet, it stores the one from
the callback, which is safe because the HMAC signature proves the bank sent it. A different
stored reference is rejected with 409. The worker writes its reference only
`WHERE bank_reference IS NULL`, so whichever side arrives first wins and the other agrees.
(Agreed with the owner as a change to the spec.)

### 12. The worker claims one outbox event per transaction

The spec's claim query takes `LIMIT 10`. The worker takes one row at a time, with
`FOR UPDATE SKIP LOCKED`, and handles it, records the outcome and commits in that row's own
transaction. A row lock is then held only for its own HTTP call (5 s at most), not for up to
ten sequential calls. A crash loses at most one event's bookkeeping. The cost is one extra
`SELECT` per event, which is negligible here. A `locked_until` lease column remains the upgrade
if calls get slow.

### 13. A savepoint around each handler

If a handler fails part-way (a bug, or a constraint error), its writes must be undone, but the
failure still has to be recorded. Rolling back the whole transaction would release the row
lock, and another worker could grab the event before the failure was written. So the handler
runs inside `begin_nested()`: a failure rolls back to the savepoint, and the outer transaction
keeps the lock, records `attempts` and `last_error`, and schedules the retry. Unexpected errors
back off like expected ones, so a poison event can't spin the worker.

### 14. Retries: the spec's webhook backoff for every outbox kind

Every failed attempt waits min(30 s × 2^(attempts−1), 6 h) with ±20% jitter, and the event
becomes `dead` after 10 attempts. Any non-2xx reply, timeout or malformed reply counts as a
failure. One policy for every kind is simpler to reason about, and milestone 4's webhooks
reuse it unchanged.

### 15. A dead bank submission leaves the payment `processing`

After 10 failed submissions the worker gives up, but it doesn't mark the payment `failed`. A
timeout means the bank may have received the payment, and a `failed` payment can't later become
`succeeded`, so guessing could contradict money that actually moved. The payment stays
`processing` until a callback arrives or someone reconciles it, and the event shows as `dead`.

### 16. Confirm refuses a payment past `expires_at` even before the sweep

The sweep runs every 10 seconds, so a payment can be past `expires_at` while still `created`.
`POST /confirm` checks the time itself and returns 409 `payment_expired`, without changing the
status: a failed request commits nothing, and the next sweep expires it. An already expired
payment gets the same error.

### 17. The expiry sweep uses `SKIP LOCKED` and a partial index

The sweep locks up to 500 overdue `created` payments with `FOR UPDATE SKIP LOCKED` and moves
each through the state machine. A payment that a confirm has locked is skipped rather than
waited for; the confirm either moves it to `processing` or refuses it, and a later sweep picks
it up if needed. The partial index `payments_expiry_due ON payments (expires_at) WHERE status =
'created'` holds only unconfirmed payments, so it stays small however many settled payments
accumulate.

### 18. Webhook events are queued now and delivered in milestone 4

The callback and the sweep enqueue `payment.succeeded`, `payment.failed` and `payment.expired`
events in the same transaction as the status change, as the spec's flow says. The spec doesn't
mention an expiry event, but a merchant waiting on a final status needs one. The worker claims
only the kinds it has handlers for, so these rows wait as `pending` until milestone 4 adds
delivery. Then the backlog drains, and none are lost, which is the outbox doing its job. Each
payload is a snapshot of the payment when the event happened, with an `evt_` ID that stays the
same across retries so merchants can drop duplicates. Events are queued whether or not the
merchant has a `webhook_url`; delivery decides what to do without one.

### 19. Ledger accounts are created on first use

`bank_clearing` (a system account, `merchant_id` NULL) and each merchant's `merchant_balance`
are created by `INSERT ... ON CONFLICT DO NOTHING` the first time a posting needs them, then
read back. Merchants that existed before the ledger need no backfill, and two transactions
creating the same account at once can't produce duplicates: the second waits on the unique
index, then finds the first's row. `UNIQUE NULLS NOT DISTINCT` is what makes this work for the
system account.

### 20. A unique index as the backstop against double crediting

The duplicate-callback guard is the final-status check under the row lock. As a second line,
like the `refunded_paise` CHECK constraint, the partial unique index
`ledger_transactions_one_capture ON ledger_transactions (payment_id) WHERE refund_id IS NULL`
lets the database itself refuse a second capture posting for one payment.

### 21. Balances are summed in SQL and cast back to an integer

`GET /v1/balance` sums the merchant's `merchant_balance` entries (credits shown positive). In
Postgres, `SUM(bigint)` returns `NUMERIC`, which asyncpg returns as `Decimal`, so the query
casts the result back to `BIGINT` to keep money as `int` end to end. Summing all entries is
fine at this scale (and `ledger_entries.account_id` is now indexed). A large system would keep
periodic balance snapshots.

### 22. Row locks re-read the row (`populate_existing`)

Every `SELECT ... FOR UPDATE` on a payment also sets `populate_existing=True`. SQLAlchemy
otherwise keeps an object that is already in the session's identity map unchanged, so code
could take the lock and still read values from before it waited.

### 23. The mock bank simulates the hard cases

Besides a decline rate and a random delay, the mock bank can send a callback twice at the same
moment (exercising the row lock), and can accept a submission but answer 503 ("lost response").
That leaves the gateway unsure whether the bank got the payment, and the callback can beat the
reference into the database. It retries callbacks the gateway doesn't accept with doubling
backoff, signs each attempt with a fresh timestamp, and treats `payment_id` as an idempotency
key. It shares only the signing helper with the gateway. In tests, a scripted fake bank
(`httpx.MockTransport`) replaces it for the worker, and the real mock bank runs in-process for
the end-to-end tests.
