"""Per-API-key rate limiting: a fixed one-minute window counted in Redis.

Each request runs INCR on rl:{key_prefix}:{minute}, which also sets a 60-second expiry the first
time; past RATE_LIMIT_PER_MINUTE the answer is 429 with Retry-After. A fixed window is the
simplest correct limiter: its known weakness is a burst of up to twice the limit straddling a
minute boundary, which is acceptable here (a sliding window fixes it at more cost).

Redis holds only these counters, so if it is unreachable the limiter fails open: requests go
through and a warning is logged. Losing rate limiting briefly is better than losing the API.
"""

import math
import time

import structlog
from fastapi import Depends, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.deps import CurrentMerchant, SettingsDep
from app.errors import TooManyRequests

log = structlog.get_logger()

WINDOW_SECONDS = 60
clock = time.time  # replaced in tests to pin the window


async def enforce_rate_limit(
    request: Request, response: Response, merchant: CurrentMerchant, settings: SettingsDep
) -> None:
    limit = settings.rate_limit_per_minute
    now = clock()
    key = f"rl:{merchant.key_prefix}:{int(now // WINDOW_SECONDS)}"
    redis: Redis = request.app.state.redis
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS, nx=True)
            count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        log.warning("rate_limit_unavailable", error=repr(exc))
        return

    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(max(limit - count, 0))
    if count > limit:
        retry_after = math.ceil(WINDOW_SECONDS - now % WINDOW_SECONDS)
        raise TooManyRequests(
            "rate_limited",
            f"More than {limit} requests in a minute. Retry in {retry_after} s.",
            headers={"Retry-After": str(retry_after)},
        )


# For a router's or route's `dependencies=[...]`. It runs after authentication, which it needs.
RateLimited = Depends(enforce_rate_limit)
