"""A guided tour of the gateway, run against the Docker Compose stack.

    docker compose up --build -d
    docker compose exec api python -m scripts.demo

It plays a merchant from signup to refund against the real API, worker and mock bank, and says
what each step shows: idempotent retries, settlement through the outbox and the bank's signed
callback, a signed webhook that fails, is logged and is redelivered, refunds, the ledger
balance and tenant isolation. It runs its own webhook receiver and checks every signature with
`verify_signature`, the helper a merchant would use.

It runs inside the api container so the worker can reach the receiver over the Compose network
(`http://api:9000`). The local stack allows that private address with WEBHOOK_ALLOW_PRIVATE_URLS.
"""

import itertools
import json
import os
import queue
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

from app.security.signing import SIGNATURE_HEADER, verify_signature

API_URL = os.environ.get("DEMO_API_URL", "http://localhost:8000")
RECEIVER_PORT = 9000
WEBHOOK_URL = os.environ.get("DEMO_WEBHOOK_URL", f"http://api:{RECEIVER_PORT}/webhooks")
TIMEOUT_SECONDS = 30
PAYMENT_PAISE = 49_900
REFUND_PAISE = 10_000

type Json = dict[str, Any]


class DemoError(Exception):
    pass


class WebhookReceiver:
    """The merchant's webhook endpoint: checks each signature and records what arrived.

    It answers the first `payment.succeeded` delivery with 503, like a server that is briefly
    down, so the gateway has a failed attempt to log and retry. A plain (single-threaded)
    HTTPServer handles one delivery at a time, so the handler needs no locking.
    """

    def __init__(self) -> None:
        self.secret = ""  # set once the merchant has signed up
        self.refuse_first = "payment.succeeded"
        self.arrivals: queue.Queue[tuple[Json, bool, int]] = queue.Queue()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                header = self.headers.get(SIGNATURE_HEADER, "")
                valid = verify_signature(receiver.secret, body, header)
                event: Json = json.loads(body)
                status = 200 if valid else 400
                if valid and event["type"] == receiver.refuse_first:
                    receiver.refuse_first = ""
                    status = 503
                self.send_response(status)
                self.end_headers()
                receiver.arrivals.put((event, valid, status))

            def log_message(self, format: str, *args: Any) -> None:
                pass  # keep the demo's output readable

        # Every interface, so the worker's container can reach it over the Compose network.
        self._server = HTTPServer(("0.0.0.0", RECEIVER_PORT), Handler)  # noqa: S104 (see above)

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def wait_for(self, event_type: str, object_id: str) -> tuple[Json, bool, int]:
        """The next delivery of `event_type` about `object_id`; other deliveries are skipped."""
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                event, valid, status = self.arrivals.get(timeout=remaining)
            except queue.Empty:
                break
            if event["type"] == event_type and event["data"]["id"] == object_id:
                return event, valid, status
        raise DemoError(f"no {event_type} webhook for {object_id} within {TIMEOUT_SECONDS} s")


_steps = itertools.count(1)


def step(title: str) -> None:
    print(f"\n{next(_steps)}. {title}")


def say(text: str) -> None:
    print(f"   {text}")


def rupees(paise: int) -> str:
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def expect(response: httpx.Response, status: int) -> Json:
    if response.status_code != status:
        raise DemoError(
            f"{response.request.method} {response.request.url.path} returned "
            f"{response.status_code}, expected {status}: {response.text}"
        )
    body: Json = response.json()
    return body


def new_key() -> dict[str, str]:
    return {"Idempotency-Key": f"demo-{uuid.uuid4()}"}


def wait_until_ready(client: httpx.Client) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if client.get("/ready").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)
    raise DemoError(f"the API at {API_URL} isn't ready; is `docker compose up` running?")


def sign_up(client: httpx.Client, name: str, webhook_url: str | None = None) -> Json:
    return expect(
        client.post("/v1/merchants", json={"name": name, "webhook_url": webhook_url}), 201
    )


def settle(client: httpx.Client, auth: dict[str, str], payment_id: str) -> Json:
    """Confirm the payment, then poll until the bank's callback has settled it."""
    confirmed = expect(
        client.post(f"/v1/payments/{payment_id}/confirm", headers=auth | new_key()), 200
    )
    say(f"POST /confirm -> {confirmed['status']}")
    started = time.monotonic()
    while time.monotonic() - started < TIMEOUT_SECONDS:
        payment = expect(client.get(f"/v1/payments/{payment_id}", headers=auth), 200)
        if payment["status"] != "processing":
            say(f"{payment['status']} after {time.monotonic() - started:.1f} s")
            return payment
        time.sleep(0.25)
    raise DemoError(f"{payment_id} was still processing after {TIMEOUT_SECONDS} s")


def logged_event(
    client: httpx.Client, auth: dict[str, str], event_id: str, ready: Callable[[Json], bool]
) -> Json:
    """The event as the delivery log shows it, once `ready` holds.

    The worker records an attempt just after the receiver answers, so this polls. Test the
    event's own fields: the log reads the event before its deliveries, so once the event shows
    an attempt, the deliveries do too.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for event in expect(client.get("/v1/webhook-events", headers=auth), 200)["data"]:
            if event["id"] == event_id and ready(event):
                found: Json = event
                return found
        time.sleep(0.25)
    raise DemoError(f"the delivery log didn't show the expected state of {event_id}")


def run(client: httpx.Client, receiver: WebhookReceiver) -> None:
    wait_until_ready(client)

    step("Sign up a merchant")
    merchant = sign_up(client, "Demo Store", WEBHOOK_URL)
    auth = {"Authorization": f"Bearer {merchant['api_key']['key']}"}
    receiver.secret = merchant["webhook_secret"]
    say(f"merchant {merchant['id']}, webhooks to {WEBHOOK_URL}")
    say(f"API key {merchant['api_key']['prefix']}... is shown once; the gateway keeps its hash")

    step(f"Create a {rupees(PAYMENT_PAISE)} payment, then retry it as a client would")
    body = {"amount_paise": PAYMENT_PAISE, "description": "Demo order"}
    key = new_key()
    payment = expect(client.post("/v1/payments", json=body, headers=auth | key), 201)
    say(f"POST /v1/payments -> {payment['id']} ({payment['status']})")
    retry = client.post("/v1/payments", json=body, headers=auth | key)
    replayed = retry.headers.get("Idempotent-Replayed")
    say(f"same Idempotency-Key and body -> {expect(retry, 201)['id']} again")
    say(f"(Idempotent-Replayed: {replayed}): the stored response, not a second payment")
    changed = client.post("/v1/payments", json=body | {"amount_paise": 1}, headers=auth | key)
    code = expect(changed, 422)["error"]["code"]
    say(f"same key with a different body -> 422 {code}")

    step("Confirm it; the worker submits it to the mock bank, whose signed callback settles it")
    payment = settle(client, auth, payment["id"])
    for _ in range(5):
        if payment["status"] == "succeeded":
            break
        say(f"the mock bank declines about 10% of payments ({payment['failure_reason']})")
        say("failed is final, so take a new payment")
        payment = expect(client.post("/v1/payments", json=body, headers=auth | new_key()), 201)
        payment = settle(client, auth, payment["id"])
    if payment["status"] != "succeeded":
        raise DemoError("the mock bank declined six payments in a row; try again")
    say("the status change, the ledger postings and the webhook event committed together")

    step("Receive the payment.succeeded webhook, after one failed delivery")
    event, valid, status = receiver.wait_for("payment.succeeded", payment["id"])
    say(f"{event['id']} arrived, signature {'valid' if valid else 'INVALID'}")
    say(f"our endpoint answered {status}, as a briefly unavailable server might")
    logged = logged_event(client, auth, event["id"], lambda event: event["attempts"] == 1)
    say(f"the delivery log: {logged['status']}, {logged['last_error']!r}")
    retry_in = datetime.fromisoformat(logged["next_attempt_at"]) - datetime.now(UTC)
    say(f"next attempt in {retry_in.total_seconds():.0f} s (backoff: 30 s doubling, ±20% jitter)")
    expect(client.post(f"/v1/webhook-events/{event['id']}/redeliver", headers=auth), 200)
    say("instead of waiting: POST /v1/webhook-events/{id}/redeliver")
    event, valid, status = receiver.wait_for("payment.succeeded", payment["id"])
    say(f"{event['id']} arrived again, signature {'valid' if valid else 'INVALID'}, -> {status}")
    say("same event ID: delivery is at least once, so merchants drop duplicates by ID")
    logged = logged_event(client, auth, event["id"], lambda event: event["status"] == "done")
    attempts = logged["deliveries"]
    say("attempts: " + ", ".join(f"HTTP {attempt['http_status']}" for attempt in attempts))

    step(f"Refund {rupees(REFUND_PAISE)}, then try to refund more than is left")
    refund = expect(
        client.post(
            f"/v1/payments/{payment['id']}/refunds",
            json={"amount_paise": REFUND_PAISE},
            headers=auth | new_key(),
        ),
        201,
    )
    say(f"POST /refunds -> {refund['id']} ({refund['status']})")
    too_much = client.post(
        f"/v1/payments/{payment['id']}/refunds",
        json={"amount_paise": PAYMENT_PAISE},
        headers=auth | new_key(),
    )
    say(f"refund {rupees(PAYMENT_PAISE)} more -> {too_much.status_code}, in the one error shape:")
    say(json.dumps(expect(too_much, 409)))
    event, valid, status = receiver.wait_for("refund.succeeded", refund["id"])
    say(f"webhook {event['type']} {event['id']}, signature {'valid' if valid else 'INVALID'}")

    step("Check the balance")
    balance = expect(client.get("/v1/balance", headers=auth), 200)["balance_paise"]
    say(f"{rupees(balance)}, summed from the double-entry ledger:")
    say(f"+{rupees(PAYMENT_PAISE)} for the payment, -{rupees(REFUND_PAISE)} for the refund")

    step("Look up the payment as another merchant")
    other = sign_up(client, "Another Store")
    response = client.get(
        f"/v1/payments/{payment['id']}",
        headers={"Authorization": f"Bearer {other['api_key']['key']}"},
    )
    say(f"-> 404 {expect(response, 404)['error']['code']}: not 403, which would confirm it exists")

    print("\nDone. Try it yourself in Swagger at http://localhost:8000/docs")
    print("Watch the worker and the bank with: docker compose logs -f worker mock-bank")


def main() -> int:
    receiver = WebhookReceiver()
    receiver.start()
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        try:
            run(client, receiver)
        except DemoError as exc:
            print(f"\nThe demo stopped: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
