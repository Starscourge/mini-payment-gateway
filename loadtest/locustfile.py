"""Load test: create a payment, confirm it, and poll until the bank settles it.

    RATE_LIMIT_PER_MINUTE=1000000 docker compose up --build -d
    uv run locust -f loadtest/locustfile.py --host http://localhost:8000 \
        --headless --users 100 --spawn-rate 20 --run-time 2m

Each simulated user signs up as its own merchant (signup is open in the local stack), so the load
spreads over many API keys. The per-key rate limit is raised for the run: a user polling every
half second sends more than the default 100 requests a minute, and the test measures the
gateway, not the limiter.

Locust's table covers HTTP requests only. Settlement time, from the confirm request until a GET
sees a final status, is logged separately when the run ends: it covers the outbox, the worker, the
mock bank's simulated 0.5-3 s delay and its callback, and it is measured in seconds, so mixing it
into the table would distort the aggregate percentiles. It is precise to the 0.5 s poll interval.
"""

import logging
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from locust import FastHttpUser, constant, events, task

POLL_SECONDS = 0.5
SETTLE_TIMEOUT_SECONDS = 60
FINAL_STATUSES = {"succeeded", "failed", "expired"}

log = logging.getLogger(__name__)


@dataclass
class Settlements:
    seconds: list[float] = field(default_factory=list)
    timeouts: int = 0


settlements = Settlements()  # per process: run without --processes


def idempotency_key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


class Merchant(FastHttpUser):
    wait_time = constant(0)  # the polling interval is the only pause

    def on_start(self) -> None:
        response = self.client.post("/v1/merchants", json={"name": f"load-{uuid.uuid4()}"})
        self.auth = {"Authorization": f"Bearer {response.json()['api_key']['key']}"}

    @task
    def pay(self) -> None:
        created = self.client.post(
            "/v1/payments",
            json={"amount_paise": 49_900, "description": "load test"},
            headers=self.auth | idempotency_key(),
        )
        payment_id = created.json()["id"]
        started = time.perf_counter()
        self.client.post(
            f"/v1/payments/{payment_id}/confirm",
            headers=self.auth | idempotency_key(),
            name="/v1/payments/[id]/confirm",
        )
        while time.perf_counter() - started < SETTLE_TIMEOUT_SECONDS:
            time.sleep(POLL_SECONDS)  # locust patches time.sleep to yield to other users
            payment: dict[str, Any] = self.client.get(
                f"/v1/payments/{payment_id}", headers=self.auth, name="/v1/payments/[id]"
            ).json()
            if payment["status"] in FINAL_STATUSES:
                settlements.seconds.append(time.perf_counter() - started)
                return
        settlements.timeouts += 1


@events.quitting.add_listener  # type: ignore[untyped-decorator]  # locust's hooks are unannotated
def _report_settlement(**_kwargs: Any) -> None:
    seconds = settlements.seconds
    if len(seconds) < 2:
        log.info("settlement: too few payments settled to report")
        return
    cuts = statistics.quantiles(seconds, n=100)
    log.info(
        "settlement (confirm -> final status): %d payments, median %.2f s, p95 %.2f s, "
        "max %.2f s, %d still processing after %d s",
        len(seconds),
        cuts[49],
        cuts[94],
        max(seconds),
        settlements.timeouts,
        SETTLE_TIMEOUT_SECONDS,
    )
