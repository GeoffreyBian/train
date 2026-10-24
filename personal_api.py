#!/usr/bin/env python3
"""Garmin Connect Personal API client (official, key-based, read-only).

Garmin opened this in Oct 2026: a key made at
connect.garmin.com/app/settings/personalApiToken is exchanged for a 24 h
OAuth2 bearer token. No password, no MFA, no SSO scraping, no per-IP login
throttling -- which is why the core sync runs on it.

It is a strict subset of what the unofficial library reaches. These 404 under
the key and still need the legacy login (see garmin_sync.login): training
readiness, training status, VO2max / heat-acclimation history, endurance
score, per-activity laps, weather and time series.

Key:   GARMIN_PERSONAL_API_KEY in .env (gitignored) or the environment.
Token: cached in ~/.garmin-personal/token (mode 600), re-minted on expiry/401.
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE = "https://personal.connectapi.garmin.com"
TOKEN_URL = BASE + "/personalapitoken-service/personal-api/personal-api-key/token"
ENV = Path(__file__).resolve().parent / ".env"
CACHE = Path.home() / ".garmin-personal" / "token"

# Largest date range each ranged endpoint accepts (inclusive days). Garmin
# answers an empty 400 past these, so callers chunk instead.
MAX_DAYS = {
    "sleep": 31, "steps": 28, "stress": 28, "hydration": 31,
    "body_battery": 31, "dailies": 365, "hrv": 28,
}


class PersonalAPIError(RuntimeError):
    pass


class KeyMissing(PersonalAPIError):
    pass


def api_key():
    k = os.environ.get("GARMIN_PERSONAL_API_KEY")
    if not k and ENV.exists():
        for line in ENV.read_text().splitlines():
            name, _, v = line.strip().partition("=")
            if name == "GARMIN_PERSONAL_API_KEY" and v.strip():
                k = v.strip()
    if not k:
        raise KeyMissing(
            "GARMIN_PERSONAL_API_KEY not set. Create one at "
            "connect.garmin.com/app/settings/personalApiToken and add it to "
            f"{ENV}")
    return k


def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))["exp"]
    except Exception:
        return 0


def chunks(start, end, days):
    """Split [start, end] into inclusive windows of at most `days` days."""
    while start <= end:
        stop = min(end, start + timedelta(days=days - 1))
        yield start, stop
        start = stop + timedelta(days=1)


class PersonalAPI:
    def __init__(self, key=None, cache=CACHE):
        self.key = key or api_key()
        self.cache = cache
        self.token = None

    # ------------------------------------------------------------ auth

    def _mint(self):
        req = urllib.request.Request(
            TOKEN_URL, method="POST", headers={"Authorization": "ApiKey " + self.key})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                tok = r.read().decode().strip()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PersonalAPIError(
                    f"token exchange refused (HTTP {e.code}): the Personal API key "
                    "is wrong, deleted or expired. Make a new one at "
                    "connect.garmin.com/app/settings/personalApiToken") from None
            raise PersonalAPIError(f"token exchange failed: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            # DNS, timeout, outage: a source failure, not a crash
            raise PersonalAPIError(f"token exchange failed: {getattr(e, 'reason', e)}") from None
        # The endpoint returns the bare JWT; tolerate a JSON wrapper in case
        # Garmin ever changes that.
        if tok.startswith("{"):
            d = json.loads(tok)
            tok = d.get("access_token") or d.get("accessToken") or d.get("token") or ""
        if tok.count(".") != 2:
            raise PersonalAPIError("token exchange returned something that is not a token")
        self.token = tok
        try:
            self.cache.parent.mkdir(mode=0o700, exist_ok=True)
            self.cache.touch(mode=0o600)
            self.cache.write_text(self._key_tag() + "\n" + tok)
        except OSError:
            pass
        return tok

    def _key_tag(self):
        # The cache records which key minted it, so a new or mistyped key is
        # never "verified" by a token the old key left behind.
        return hashlib.sha256(self.key.encode()).hexdigest()[:16]

    def _bearer(self):
        if self.token and _jwt_exp(self.token) > time.time() + 300:
            return self.token
        if self.cache.exists():
            tag, _, tok = self.cache.read_text().strip().partition("\n")
            if tag == self._key_tag() and _jwt_exp(tok) > time.time() + 300:
                self.token = tok
                return tok
        return self._mint()

    # ------------------------------------------------------------ transport

    def get(self, path, params=None, raw=False):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={
                    "Authorization": "Bearer " + self._bearer(), "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = r.read()
                return body if raw else (json.loads(body) if body.strip() else None)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.token = None
                    self.cache.unlink(missing_ok=True)
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise PersonalAPIError(f"GET {path}: HTTP {e.code}") from None
            except (urllib.error.URLError, OSError) as e:
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise PersonalAPIError(f"GET {path}: {getattr(e, 'reason', e)}") from None

    # ------------------------------------------------------------ endpoints

    def activities(self, start, end, page=50):
        """Activity summaries with startTimeLocal in [start, end]."""
        out, off = [], 0
        while True:
            batch = self.get("/activitylist-service/activities/search/activities", {
                "startDate": start.isoformat(), "endDate": end.isoformat(),
                "start": off, "limit": page}) or []
            out.extend(batch)
            if len(batch) < page:
                return out
            off += page

    def activity(self, activity_id):
        return self.get(f"/activity-service/activity/{activity_id}") or {}

    def fit(self, activity_id):
        """Original upload (a zip holding the .fit), as bytes."""
        return self.get(f"/download-service/files/activity/{activity_id}", raw=True)

    def sleep(self, start, end):
        out = []
        for a, b in chunks(start, end, MAX_DAYS["sleep"]):
            out += self.get("/sleep-service/sleep/dailySleepSummariesByDate", {
                "startDate": a.isoformat(), "endDate": b.isoformat()}) or []
        return out

    def dailies(self, start, end):
        out = []
        for a, b in chunks(start, end, MAX_DAYS["dailies"]):
            out += (self.get("/usersummary-service/v2/dailysummaries", {
                "startDate": a.isoformat(), "endDate": b.isoformat()}) or {}).get("data") or []
        return out

    def hrv(self, start, end):
        out = []
        for a, b in chunks(start, end, MAX_DAYS["hrv"]):
            out += (self.get(f"/hrv-service/hrv/daily/{a}/{b}") or {}).get("hrvSummaries") or []
        return out

    def weight(self, start, end):
        return self.get(f"/weight-service/weight/range/{start}/{end}",
                        {"includeAll": "true"}) or {}

    def hr_zones(self):
        return self.get("/biometric-service/heartRateZones/v2") or []

    def fitness_age(self, day=None):
        return self.get(f"/fitnessage-service/fitnessage/v2/{day or date.today()}") or {}

    def profile(self):
        return self.get("/userprofile-service/userprofile/settings") or {}


if __name__ == "__main__":
    # Smoke test: ./.venv/bin/python personal_api.py
    api = PersonalAPI()
    api._mint()   # always a fresh exchange, so this really tests the key
    p = api.profile()
    exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(_jwt_exp(api._bearer())))
    print(f"OK: Personal API reachable as {p.get('displayName')}, token valid to {exp}")
