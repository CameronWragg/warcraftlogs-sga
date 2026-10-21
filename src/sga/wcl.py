"""Warcraft Logs v2 GraphQL client with client-credentials auth and rate-limit waiting."""

import os
import time

import httpx
from dotenv import load_dotenv

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

RATE_LIMIT_FIELDS = "rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }"


class WclError(RuntimeError):
    pass


class WclClient:
    def __init__(self, reserve_points: float = 20.0, log=print):
        load_dotenv()
        client_id = os.environ.get("WCL_CLIENT_ID")
        client_secret = os.environ.get("WCL_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise WclError("WCL_CLIENT_ID and WCL_CLIENT_SECRET must be set (see .env.example)")
        self._http = httpx.Client(timeout=120)
        self._auth = (client_id, client_secret)
        self._token: str | None = None
        self.reserve_points = reserve_points
        self.log = log
        self.points_spent: float | None = None
        self.limit_per_hour = None

    def _get_token(self) -> str:
        if self._token is None:
            resp = self._http.post(
                TOKEN_URL, auth=self._auth, data={"grant_type": "client_credentials"}
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
        return self._token

    def query(self, body: str) -> dict:
        """Run a query body (the fields inside `query { ... }`).

        Waits for the hourly reset when the remaining points can't cover another request
        like the last one. On HTTP 429 (rateLimitData lags large queries) it retries every
        5 minutes for up to 75; on 5xx it retries 5 times with backoff.
        """
        payload = {"query": f"query {{ {body} {RATE_LIMIT_FIELDS} }}"}
        server_errors = throttled = 0
        while True:
            resp = self._http.post(
                API_URL, json=payload, headers={"Authorization": f"Bearer {self._get_token()}"}
            )
            if resp.status_code == 429:
                throttled += 1
                if throttled > 15:
                    raise WclError("still rate limited after 75 minutes")
                self._sleep(300, "HTTP 429, points exhausted")
                continue
            if resp.status_code >= 500:
                server_errors += 1
                if server_errors > 5:
                    raise WclError(f"HTTP {resp.status_code} after retries")
                self._sleep(10 * server_errors, f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise WclError(str(data["errors"])[:2000])
            self._track(data["data"].pop("rateLimitData"))
            return data["data"]

    def _track(self, rl: dict) -> None:
        self.limit_per_hour = rl["limitPerHour"]
        spent = rl["pointsSpentThisHour"]
        # The first response of a run reports the whole hour so far, not this request's cost
        last = self.points_spent
        cost = 0.0 if last is None else (spent - last if spent >= last else spent)
        self.points_spent = spent
        if rl["limitPerHour"] - spent < max(self.reserve_points, 2 * cost):
            self._sleep(rl["pointsResetIn"] + 5, f"{spent:.0f}/{rl['limitPerHour']} points used")
            self.points_spent = None

    def _sleep(self, seconds: float, why: str) -> None:
        self.log(f"Waiting {seconds:.0f}s ({why})")
        time.sleep(seconds)
