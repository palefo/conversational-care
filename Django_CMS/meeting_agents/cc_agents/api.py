"""The platform's internal API, as the agents see it.

Every call carries two credentials: ``X-CC-Service-Key`` (a secret only these
containers hold, from MEETINGS_SERVICE_KEY) and the run token the platform put
in this job's dispatch metadata. The platform also checks, on every call, that
the run is still live — so an agent stopped from the room UI is refused on its
next call, whatever it thinks it is doing.

Calls are short and retried once: an interviewer's tool call blocks the voice
model until it returns, so a slow platform must fail fast rather than leave
someone listening to silence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import aiohttp

logger = logging.getLogger("cc_agents.api")

TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3)


class RunGone(Exception):
    """The platform says this run is over (stopped, room closed). Wind down."""


class PlatformAPI:
    def __init__(self, base: str, token: str, service_key: str | None = None):
        self.base = (os.getenv("MEETINGS_INTERNAL_API_URL") or base or "http://web:8000").rstrip("/")
        self.token = token
        self.service_key = service_key if service_key is not None else os.getenv("MEETINGS_SERVICE_KEY", "")
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_metadata(cls, raw: str) -> tuple["PlatformAPI", dict]:
        meta = json.loads(raw or "{}")
        return cls(meta.get("api_base", ""), meta.get("token", "")), meta

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, headers={
                "Authorization": f"Bearer {self.token}",
                "X-CC-Service-Key": self.service_key,
                "Accept": "application/json",
            })
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, path: str, body: dict | None = None, *, retries: int = 1,
                    backoff: float = 0.6):
        url = f"{self.base}/meetings/internal/v1/{path.lstrip('/')}"
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                http = await self._http()
                async with http.request(method, url, json=body) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 410:
                        raise RunGone(data.get("error", "run is no longer live") if isinstance(data, dict) else "")
                    if resp.status >= 400:
                        raise RuntimeError(f"{method} {path} → {resp.status}: {data}")
                    return data
            except RunGone:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last = exc
                if attempt < retries:
                    await asyncio.sleep(backoff * (2 ** attempt))
        raise last  # type: ignore[misc]

    # ── Calls ───────────────────────────────────────────────────────────────
    async def run_config(self) -> dict:
        return await self._call("GET", "run/")

    async def status(self, state: str = "", *, identity: str = "", error: str = ""):
        body = {"state": state}
        if identity:
            body["identity"] = identity
        if error:
            body["error"] = error[:300]
        try:
            return await self._call("POST", "run/status/", body)
        except RunGone:
            return None
        except Exception as exc:  # status is best-effort; the work matters more
            logger.warning("Could not report status %s: %s", state, exc)
            return None

    async def questions(self) -> dict:
        return await self._call("GET", "protocol/questions/")

    async def save_answer(self, question_id: int, response: str) -> dict:
        return await self._call("POST", "protocol/answers/",
                                {"question_id": int(question_id), "response": response})

    async def search(self, query: str) -> dict:
        return await self._call("POST", "search/", {"query": query})

    async def segment(self, **fields) -> dict:
        # Not latency-critical, and losing one is losing someone's words: ride
        # out a web restart (≈ 1 + 2 + 4 + 8 + 16 s of backoff).
        return await self._call("POST", "recording/segments/", fields, retries=5, backoff=1.0)
