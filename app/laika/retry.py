"""Retries for calls to the local model services (SB0, docs/PLAN-second-brain.md).

Only transient errors are retried: the service is restarting or loading (connection refused, 503), too
busy (429), or slow (a timeout, when the caller allows it). Each retry waits a random time up to an
exponentially growing cap ("full jitter"), so a restarting server isn't hit in lockstep.
Anything else (a 400 for one bad input, a 500) is the input's problem, not the service's, and is raised
at once so the caller can count it against that input.
"""
import asyncio
import logging
import random

import httpx

log = logging.getLogger(__name__)

TRANSIENT_STATUS = {429, 503}


def is_transient(exc, timeouts=True):
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.TimeoutException):
        return timeouts
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUS
    return False


async def retrying(call, *, attempts=3, base=1.0, cap=20.0, timeouts=True, what="call"):
    """Run `call()` (a coroutine factory), retrying transient errors up to `attempts` times in all."""
    for i in range(attempts):
        try:
            return await call()
        except httpx.HTTPError as e:
            if i == attempts - 1 or not is_transient(e, timeouts):
                raise
            wait = random.uniform(0, min(cap, base * 2 ** i))
            log.warning("%s failed (%s), retry %d of %d in %.1f s", what, type(e).__name__, i + 1, attempts - 1, wait)
            await asyncio.sleep(wait)
