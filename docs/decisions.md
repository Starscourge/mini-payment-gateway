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

## Milestone 3: Correctness under pressure (2026-10-21)

### 24. Idempotency is a dependency, and it owns the transaction

`POST /v1/payments`, `/confirm` and `/refunds` declare `IdempotencyDep`
(`app/api/idempotency.py`). Before the handler runs, it requires the `Idempotency-Key` header
and claims the key in the request's transaction. A repeat of a completed request never reaches
the handler: the dependency raises `IdempotentReplay`, which an exception handler turns into the
stored response with an `Idempotent-Replayed: true` header. The handler finishes with
`idempotency.complete(response, status_code=...)`, which saves the response in the key's row
and commits. So the stored response and the business writes commit together or not at all,
and services still only flush. Raising for a replay is control flow rather than an error, but
it keeps every handler free of replay branches, the same way a failed auth check skips the
handler.

### 25. Only completed requests are remembered

A request that fails (validation, 404, 409 and so on) rolls back everything, including its
key, so the same key can be retried and will run afresh. A failed request had no effect, so
there is nothing to protect from running twice. This is the spec's flow; Stripe instead stores
some errors too. One consequence: a refund rejected because the payment hadn't succeeded yet
can be retried with the same key once it has.

### 26. Concurrent duplicates wait on the key, with a 5-second `lock_timeout`

Ten requests with one key sent together: the first inserts the key, and the others' `INSERT
... ON CONFLICT DO NOTHING` blocks on the primary key until the first commits. Then each
inserts nothing, re-reads the row (a fresh snapshot under READ COMMITTED) and replays the
stored response. If the first rolls back, a waiter's insert succeeds and it runs instead.
`SET LOCAL lock_timeout` bounds that wait to 5 s, after which the duplicate gets 409
`idempotency_key_in_use` and can retry. The timeout is reset straight after the insert, so
waits on payment row locks later in the request aren't affected. (Verified first: Postgres
applies `lock_timeout` to unique-index waits too, raising SQLSTATE `55P03`.)

### 27. "The same request" is method, path and canonical JSON body

The request hash is SHA-256 over the method, path and body, with a JSON body re-serialised with
sorted keys and no whitespace first. A client that rebuilds an identical request on retry still
matches, while any changed value is a different request (422 `idempotency_key_reused`). The
path is included, so one key can't be reused across endpoints or payments. Keys are scoped to
the merchant (the primary key is `(merchant_id, key)`), and the worker purges keys older than
24 hours, after which a key can be reused.

### 28. Replays are the same JSON, not the same bytes

The stored response lives in a `JSONB` column (as in the spec), and `JSONB` stores object keys
in its own order (by length, then bytes). A replay therefore has the same keys and values as
the original, but possibly in a different order. JSON clients compare parsed objects, so this
is invisible to them; a byte-identical replay would need a `TEXT` column instead.

### 29. Refunds: the spec's row-lock flow, plus "omit the amount for a full refund"

`create_refund` locks the payment (`FOR UPDATE`), checks that it succeeded and how much is
left, raises `refunded_paise`, inserts the refund, posts the reversing ledger entries (debit
`merchant_balance`, credit `bank_clearing`) and queues `refund.succeeded`, all in one
transaction. Parallel refunds take turns on the lock, so each sees the previous one's
`refunded_paise`, and the CHECK constraint backs this up. Omitting `amount_paise` (or the
whole body) refunds everything still refundable, the usual API for "refund all". Refunds
settle immediately; routing them through the bank is a stretch goal.

### 30. Concurrency tests use an app that really commits

Most tests share one rolled-back session, which can't show two transactions racing.
`live_client` builds an app with its own engine, so each request gets its own connection,
locks really block and commits are visible to other requests. Requests are fired together
with `asyncio.gather`, and `committed_sessions` checks the ledger invariant and truncates every
table afterwards. The suite covers parallel duplicate creates, parallel refunds, parallel
duplicate callbacks, parallel confirms, racing first postings that create ledger accounts, and
a duplicate that times out waiting for an in-flight original.

## Milestone 4: Notifications and protection (2026-10-21)

### 31. Webhook delivery: signed bytes, 2xx only, no redirects, body never read

The worker's `webhook` handler serialises the event once, signs exactly those bytes with the
merchant's secret (`X-Signature: t=…,v1=…`, the same scheme the bank's callbacks use), and POSTs
them with a 5-second timeout. Only a 2xx answer counts as delivered. Redirects aren't followed,
because a redirect could send the request to an address the SSRF check never saw, and the
response body is never read, so a hostile endpoint can't make the worker download anything.
Every attempt, failed or not, is a row in `webhook_deliveries` (status code, duration, error).
Delivery is at least once: a timeout after the merchant processed the event means a retry, so
merchants drop duplicates by the event's `evt_` ID, which never changes. Retries use the outbox
backoff (30 s doubling to 6 h, ±20% jitter), and the event is `dead` after 10 attempts.

### 32. Handlers return an error instead of raising

Before, an expected failure raised, rolling back the handler's savepoint. A failed webhook
attempt must still leave its `webhook_deliveries` row, so handlers now return `None` on success
or a short error message on an expected failure, and their writes are kept either way. Only an
exception (a bug, a database error) rolls back to the savepoint (decision 13).

### 33. SSRF: every address must be public, checked at signup and before every attempt

`app/security/ssrf.py` resolves the webhook URL's host and refuses it unless every address is
public (`ipaddress.is_global`, which excludes private, loopback, link-local such as the cloud
metadata address 169.254.169.254, carrier-grade NAT and reserved ranges). IPv4-mapped IPv6
addresses (`::ffff:127.0.0.1`) are judged by the IPv4 address inside. "Every address" matters
because a client may connect to any of them. Production also requires https.

The check runs at signup (422 `invalid_webhook_url`) and again before every delivery, because
DNS can change after signup. To close DNS rebinding, where a name resolves to a public IP for
the check and a private one for the request, the request goes to the exact IP that was checked:
the URL's host is replaced by that IP, and the original name is sent as the `Host` header and as
the TLS server name (httpx's `sni_hostname` extension), so certificates are still verified against
the hostname. I checked this against real HTTPS sites: the pinned request verifies, and the same
request without the SNI extension fails with "IP address mismatch". `WEBHOOK_ALLOW_PRIVATE_URLS`
relaxes the address check for local development, and Settings refuses it in production. Tests
replace DNS with a fake resolver (the autouse `dns` fixture), so none depend on real lookups.

### 34. An event for a merchant without a webhook URL is closed without an attempt

Events are queued whatever the merchant's settings (decision 18). At delivery, an event for a
merchant with no `webhook_url` has nowhere to go, so the worker marks it `done` with no delivery
rows: `done` means "nothing left to do". The delivery log shows it with zero attempts.

### 35. The delivery log and redelivery address events by their `evt_` ID

A webhook event's public ID is the `id` inside its payload, since that is what merchants
receive and deduplicate on. The partial unique expression index `outbox_event_id ON outbox
((payload ->> 'id')) WHERE kind = 'webhook'` makes it addressable without a new column.
`GET /v1/webhook-events` pages newest first by the outbox's increasing `id` (served by
`outbox_webhook_events ON outbox (merchant_id, id DESC) WHERE kind = 'webhook'`), filters by
status, and embeds each event's attempts. `POST /v1/webhook-events/{id}/redeliver` re-queues a
pending or dead event with a fresh set of attempts; a delivered one is a 409, as the spec only
re-queues failed or dead events. It takes the row lock, so it waits out a delivery in progress
instead of racing it.

"Due now" is the database's `now()`, not the app's clock: the worker compares `next_attempt_at`
with the database's `now()`, and a test caught that an app timestamp slightly ahead of it delays
the redelivery. `eager_defaults` on `OutboxEvent` reads the value back after the flush.

### 36. Rate limiting: a fixed window per API key that fails open

Each authenticated request runs `INCR rl:{key_prefix}:{minute}` plus `EXPIRE … 60 NX` in one
Redis `MULTI`. Past `RATE_LIMIT_PER_MINUTE` (100) the answer is 429 `rate_limited` with
`Retry-After` (seconds to the window's end), and every limited response carries
`X-RateLimit-Limit` and `X-RateLimit-Remaining`. A fixed window allows a burst of up to twice the
limit across a minute boundary; a sliding window would fix that at more cost. The limiter runs
after authentication, because it counts per key: unauthenticated requests aren't counted (a
per-IP limit would be the addition). Redis holds nothing but these counters, so if it's
unreachable the limiter lets requests through and logs a warning, and the Redis client has
half-second timeouts so a slow Redis can't stall the API. Tests pin the limiter's clock, so a
real minute boundary can't split a test.

### 37. Outbox rows and delivery attempts read back server-set columns

`OutboxEvent` and `WebhookDelivery` now set `eager_defaults`, like `Payment` and `Refund`. Their
server-set timestamps are shown in the delivery log, and reading them after an insert would
otherwise need a lazy load, which `AsyncSession` can't do.

## Milestone 5: Ship it (2026-10-24)

### 38. No live deployment: the whole stack runs locally in one command

The spec planned a hosted demo with a public Swagger URL. It was dropped, by agreement, in favour
of a polished local setup. The project's readers are recruiters, who read the CV, and engineers,
who read the code; very few would try a live API, and in an interview a laptop shows more (logs,
the database, the worker) than a hosted demo can. A free host also fits poorly. The stack is five
always-on processes, and the worker must keep polling. Free platform tiers sleep idle services
and don't run background workers (Render, as of October 2026), or are trials (Fly.io, Railway),
and the free VM option (Oracle's Always Free) can reclaim a server that looks idle, which a
low-traffic demo does. A link that dies mid job search is worse than none.

So `git clone` and `docker compose up --build` is the whole setup: Compose sets its own
development secrets, and signup is open locally. Host ports are configurable
(`POSTGRES_HOST_PORT=55432 docker compose up --build`), so a local Postgres or Redis already on
the default port doesn't stop the stack. Deploying stays an option: the same Compose file behind
a TLS-terminating proxy, with `ENVIRONMENT=production` and real secrets.

### 39. A demo script, run inside the api container

`scripts/demo.py` walks through signup, an idempotent retry, settlement, a webhook that fails and
is redelivered, a refund, the balance and tenant isolation, narrating each step. It runs its own
webhook receiver and checks signatures with `verify_signature`. It runs inside the api container
(`docker compose exec api python -m scripts.demo`) because the worker must reach the receiver
over the Compose network, and so a reader needs nothing but Docker. The receiver's address is
private, so the local stack sets `WEBHOOK_ALLOW_PRIVATE_URLS=true`, which Settings refuses in
production. To show a retry without waiting out the 30-second backoff, the receiver answers the
first `payment.succeeded` delivery with 503 and the script asks for a redelivery.

### 40. Tracebacks leave out local variables

The load test turned up a leak: when a client disconnected mid-request, the crash log included
every frame's local variables, as structlog's `dict_tracebacks` does by default. The middleware's
locals hold the request headers, so the log line carried the plaintext API key, and it was about
475 KB long. `_redact_secrets` couldn't help, because it checks only top-level keys. Tracebacks
now keep file, line and function but no locals, in JSON (`ExceptionDictTransformer(show_locals=
False)`) and in the console renderer used in development. `tests/unit/test_logging.py` crashes
with a secret in a local variable and checks that it doesn't reach the output in either format.

### 41. Load test: one merchant per user, settlement reported separately

Each Locust user signs up as its own merchant, so the load spreads over many API keys, and the
run raises `RATE_LIMIT_PER_MINUTE`, because it measures the gateway, not the limiter. Settlement
time (confirm until a GET sees a final status) is logged apart from Locust's table: it is
seconds of simulated bank delay, and mixing it in would distort the aggregate HTTP percentiles.

What it showed: one uvicorn process is CPU-bound at about 100 requests a second. The API is
stateless, so `WEB_CONCURRENCY=4` gives 2.5 times the throughput at a lower p95. At 200 users
the single worker became the bottleneck: it handles one outbox event at a time and spends most of
that time waiting on round trips, so the settlement median rose from 3.7 s to 9.3 s.
`docker compose up --scale worker=3` brought it back to 3.5 s, and 2.4 times as many payments
settled per minute, with no code change, because workers claim events with `FOR UPDATE SKIP
LOCKED`. Afterwards every succeeded payment had exactly one ledger posting, and merchant balances
matched payments minus refunds to the paisa. The numbers are in the README.
