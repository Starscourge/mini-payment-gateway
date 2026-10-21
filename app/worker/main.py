"""The worker: submits payments to the bank, delivers webhooks, expires stale payments and
purges old idempotency keys. Run with: python -m app.worker.main

Each outbox event is claimed, handled and recorded in its own transaction, so its row lock is
held only for its own HTTP call, and one event's failure can't undo another's bookkeeping.
Several workers can run at once: SKIP LOCKED keeps them off each other's rows.
"""

import asyncio
import contextlib
import signal
import time
from collections.abc import Mapping
from functools import partial

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.logging import configure_logging
from app.models import OutboxKind
from app.security.ssrf import UrlPolicy
from app.services import outbox
from app.worker.handlers import (
    Handler,
    deliver_webhook,
    expire_payments,
    purge_idempotency_keys,
    submit_to_bank,
)

log = structlog.get_logger()

POLL_INTERVAL_SECONDS = 1.0  # how long to wait when no event is due
SWEEP_INTERVAL_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 5.0  # the event's row lock is held for the call, so keep it short


async def process_next_event(session: AsyncSession, handlers: Mapping[str, Handler]) -> bool:
    """Claim one due event, run its handler and record the outcome. False if none was due."""
    event = await outbox.claim_next(session, kinds=handlers.keys())
    if event is None:
        await session.commit()
        return False

    with structlog.contextvars.bound_contextvars(outbox_id=event.id, kind=event.kind):
        try:
            # The savepoint undoes a crashed handler's writes, but the outer transaction (and so
            # the event's row lock) survives, so the failure is recorded before anyone else can
            # claim the event.
            async with session.begin_nested():
                error = await handlers[event.kind](session, event)
        except Exception as exc:
            # A bug or a database error. Back off like any failure, so a bad event can't spin.
            log.exception("outbox_event_crashed", attempt=event.attempts + 1)
            outbox.record_failure(event, repr(exc))
        else:
            if error is None:
                outbox.mark_done(event)
                log.info("outbox_event_done", attempt=event.attempts)
            else:
                log.warning("outbox_event_failed", error=error, attempt=event.attempts + 1)
                outbox.record_failure(event, error)
        await session.commit()
    return True


async def run(
    session_factory: async_sessionmaker[AsyncSession],
    handlers: Mapping[str, Handler],
    stop: asyncio.Event,
) -> None:
    """Poll until `stop` is set. The event in hand is always finished first."""
    next_sweep = 0.0
    while not stop.is_set():
        busy = False
        try:
            if time.monotonic() >= next_sweep:
                async with session_factory() as session:
                    await expire_payments(session)
                    await purge_idempotency_keys(session)
                next_sweep = time.monotonic() + SWEEP_INTERVAL_SECONDS
            async with session_factory() as session:
                busy = await process_next_event(session, handlers)
        except Exception:
            # Usually the database is unreachable. Keep going: it may come back.
            log.exception("worker_iteration_failed")
        if not busy:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    engine = create_engine(settings)
    policy = UrlPolicy.for_settings(settings)
    async with (
        httpx.AsyncClient(base_url=settings.bank_url, timeout=HTTP_TIMEOUT_SECONDS) as bank,
        # follow_redirects is already httpx's default; stated because a redirect could lead the
        # request past the SSRF check to an internal address.
        httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as webhooks,
    ):
        handlers: dict[str, Handler] = {
            OutboxKind.BANK_SUBMIT: partial(submit_to_bank, bank=bank),
            OutboxKind.WEBHOOK: partial(deliver_webhook, http=webhooks, policy=policy),
        }
        log.info("worker_started", kinds=sorted(handlers), bank_url=settings.bank_url)
        try:
            await run(create_session_factory(engine), handlers, stop)
        finally:
            await engine.dispose()
    log.info("worker_stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")
    asyncio.run(_main(settings))


if __name__ == "__main__":
    main()
