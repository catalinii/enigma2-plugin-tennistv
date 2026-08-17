# -*- coding: utf-8 -*-
"""Tennis TV API client.

Handles Keycloak OIDC authentication (authorization code + PKCE), listing of
live/upcoming matches and resolving streams from the StreamAMG backend.

Only uses the Python standard library so the addon has no third-party
dependencies.
"""

import base64
import datetime
import hashlib
import html
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import parse_qs, urlparse

SSO_BASE = "https://sso.tennistv.com/auth/realms/tennistv"
API_BASE = "https://api.tennistv.com"
PLAYBACK_BASE = "https://api.playback.streamamg.com"

CLIENT_ID = "tennis-tv-web"
REDIRECT_URI = "https://www.tennistv.com/"
SCOPE = "openid"

# Public API key embedded in the Tennis TV web player (used for the StreamAMG
# playback API). It is not a user secret.
STREAMAMG_API_KEY = "c5Oe6Cr857oLlsCFY8Bm3djiu4Cw8zo6ckD2ucI7"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

TIMEOUT = 30


class TennisTVError(Exception):
    pass


class TennisTVAuthError(TennisTVError):
    pass


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HttpClient(object):
    """Tiny requests-like wrapper around urllib with cookie persistence."""

    def __init__(self, user_agent):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.no_redirect_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirect()
        )
        self.user_agent = user_agent

    def request(self, method, url, data=None, headers=None, allow_redirects=True):
        if data is not None and not isinstance(data, (bytes, str)):
            data = urllib.parse.urlencode(data).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", self.user_agent)
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        opener = self.opener if allow_redirects else self.no_redirect_opener
        try:
            raw = opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            raw = exc
        return _Response(raw)

    def get(self, url, params=None, headers=None, allow_redirects=True):
        if params:
            sep = "&" if "?" in url else "?"
            url = "%s%s%s" % (url, sep, urllib.parse.urlencode(params))
        return self.request("GET", url, headers=headers, allow_redirects=allow_redirects)

    def post(self, url, data=None, headers=None, allow_redirects=True):
        return self.request(
            "POST", url, data=data, headers=headers, allow_redirects=allow_redirects
        )


class _Response(object):
    def __init__(self, raw):
        self.raw = raw
        self.code = getattr(raw, "code", 200)
        self.headers = getattr(raw, "headers", {})

    def read(self):
        try:
            return self.raw.read()
        except Exception:
            return b""

    def json(self):
        return json.loads(self.read().decode("utf-8", "replace"))

    def text(self):
        return self.read().decode("utf-8", "replace")

    def raise_for_status(self):
        if self.code >= 400:
            raise TennisTVError("HTTP error %s" % self.code)


class TennisTV(object):
    def __init__(self, username="", password="", token_file=None):
        self.username = username
        self.password = password
        self.token_file = token_file
        self.http = _HttpClient(USER_AGENT)
        self._tokens = self._load_tokens()

    # ------------------------------------------------------------------ #
    # Token persistence
    # ------------------------------------------------------------------ #
    def _load_tokens(self):
        if not self.token_file or not os.path.exists(self.token_file):
            return {}
        try:
            with open(self.token_file, "r") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return {}

    def _save_tokens(self):
        if not self.token_file:
            return
        try:
            directory = os.path.dirname(self.token_file)
            if directory and not os.path.exists(directory):
                os.makedirs(directory)
            with open(self.token_file, "w") as fh:
                json.dump(self._tokens, fh)
        except OSError:
            pass

    def _store_tokens(self, response):
        now = time.time()
        self._tokens = {
            "access_token": response.get("access_token"),
            "refresh_token": response.get("refresh_token")
            or self._tokens.get("refresh_token"),
            "access_expires": now + int(response.get("expires_in", 0)) - 60,
            "refresh_expires": now
            + int(
                response.get("refresh_expires_in")
                or self._tokens.get("refresh_expires", 0)
            ),
        }
        self._save_tokens()

    def clear_tokens(self):
        self._tokens = {}
        if self.token_file and os.path.exists(self.token_file):
            try:
                os.remove(self.token_file)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def _token_request(self, data):
        url = "%s/protocol/openid-connect/token" % SSO_BASE
        resp = self.http.post(url, data=data)
        if resp.code != 200:
            raise TennisTVAuthError(
                "Token request failed (%s): %s" % (resp.code, resp.text()[:200])
            )
        return resp.json()

    def login(self):
        if not self.username or not self.password:
            raise TennisTVAuthError(
                "Missing Tennis TV credentials. Set them in the addon settings."
            )

        verifier = _b64url(os.urandom(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(os.urandom(16))
        nonce = _b64url(os.urandom(16))

        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_mode": "fragment",
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        auth_url = "%s/protocol/openid-connect/auth" % SSO_BASE
        resp = self.http.get(auth_url, params=params)
        page = resp.text()

        match = re.search(
            r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page
        )
        if not match:
            raise TennisTVAuthError("Could not locate the login form.")
        action = html.unescape(match.group(1))

        resp = self.http.post(
            action,
            data={
                "username": self.username,
                "password": self.password,
                "credentialId": "",
                "login": "Log In",
            },
            allow_redirects=False,
        )

        if resp.code in (302, 303, 307, 308):
            location = resp.headers.get("Location", "")
        else:
            raise TennisTVAuthError(self._login_error(resp.text()))

        code = self._code_from_location(location)
        if not code:
            raise TennisTVAuthError("Login succeeded but no code was returned.")

        response = self._token_request(
            {
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        )
        self._store_tokens(response)
        return True

    @staticmethod
    def _code_from_location(location):
        fragment = urlparse(location).fragment
        return parse_qs(fragment).get("code", [None])[0]

    @staticmethod
    def _login_error(page_html):
        if "Invalid username or password" in page_html:
            return "Invalid username or password."
        if "Invalid username or email" in page_html:
            return "Invalid username or email."
        message = re.search(
            r'class="[^"]*kc-feedback-text[^"]*"[^>]*>(.*?)</span>', page_html
        )
        if message:
            text = re.sub(r"<[^>]+>", "", message.group(1)).strip()
            if text:
                return text
        return "Login failed. Check your credentials and try again."

    def refresh(self):
        refresh_token = self._tokens.get("refresh_token")
        if not refresh_token:
            raise TennisTVAuthError("Not logged in.")
        response = self._token_request(
            {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._store_tokens(response)
        return True

    def access_token(self):
        token = self._tokens.get("access_token")
        if token and time.time() < self._tokens.get("access_expires", 0):
            return token

        if self._tokens.get("refresh_token"):
            try:
                self.refresh()
                return self._tokens["access_token"]
            except TennisTVAuthError:
                pass

        self.login()
        return self._tokens["access_token"]

    # ------------------------------------------------------------------ #
    # Data endpoints
    # ------------------------------------------------------------------ #
    def _api_get(self, path, params=None, headers=None):
        merged = {"account-id": "35"}
        if headers:
            merged.update(headers)
        resp = self.http.get("%s%s" % (API_BASE, path), params=params, headers=merged)
        resp.raise_for_status()
        return resp.json()

    def live_matches(self):
        data = self._api_get("/tennis/v1/matches", params={"status": "L"})
        return data.get("matches", [])

    def upcoming_matches(self, days=2):
        today = datetime.date.today()
        to_date = today + datetime.timedelta(days=days)
        data = self._api_get(
            "/tennis/v1/matches",
            params={
                "status": "U",
                "from": today.isoformat(),
                "to": to_date.isoformat(),
            },
        )
        return data.get("matches", [])

    def live_videos(self):
        data = self._api_get(
            "/content/atpmedia/VIDEO/EN", params={"tagNames": "live"}
        )
        return data.get("content", [])

    def recorded_videos(self, tag="video-type:replay", page=1, page_size=50):
        params = {"pageSize": page_size, "page": page}
        if tag:
            params["tagNames"] = tag
        data = self._api_get("/content/atpmedia/VIDEO/EN", params=params)
        return data.get("content", [])

    def replay_videos(self, page=1, page_size=50):
        return self.recorded_videos(tag="video-type:replay", page=page, page_size=page_size)

    def match_highlight_videos(self, page=1, page_size=50):
        return self.recorded_videos(tag="video-type:match-highlights", page=page, page_size=page_size)

    def daily_highlight_videos(self, page=1, page_size=50):
        return self.recorded_videos(tag="video-type:daily-highlights", page=page, page_size=page_size)

    def completed_matches(self):
        data = self._api_get("/tennis/v1/matches", params={"status": "C"})
        return data.get("matches", [])

    # ------------------------------------------------------------------ #
    # Playback
    # ------------------------------------------------------------------ #
    def playback_token(self, media_id):
        token = self.access_token()
        resp = self.http.get(
            "%s/entitlementcheck/v1/videoentitlements/%s" % (API_BASE, media_id),
            headers={"Authorization": "Bearer %s" % token, "account": "atpmedia"},
        )
        resp.raise_for_status()
        return resp.json().get("access_token")

    def stream_url(self, media_id):
        token = self.playback_token(media_id)
        resp = self.http.get(
            "%s/v1/entry/%s" % (PLAYBACK_BASE, media_id),
            headers={
                "x-api-key": STREAMAMG_API_KEY,
                "Authorization": "Bearer %s" % token,
            },
        )
        resp.raise_for_status()
        return resp.json()["media"]["hls"]

    def stream_variant_url(self, media_id, max_bandwidth=20000000, quality=None):
        """Return an absolute variant playlist URL from a master HLS playlist.

        The default GStreamer hlsdemux fails to resolve Akamai's tokenized
        relative variant URLs (hdntl=... paths containing %2f), so we resolve
        the relative URL ourselves and hand GStreamer the absolute variant URL
        directly. Segment filenames within the variant are plain filenames and
        resolve correctly.

        Picks the highest variant at or below *max_bandwidth* bps, or an
        exact height if *quality* (e.g. "1080", "720") is given.
        """
        master = self.stream_url(media_id)
        resp = self.http.get(master)
        resp.raise_for_status()
        text = resp.text()
        variants = []
        current = {}
        for line in text.splitlines():
            if line.startswith("#EXT-X-STREAM-INF"):
                bw = re.search(r"BANDWIDTH=(\d+)", line)
                res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                current = {
                    "bandwidth": int(bw.group(1)) if bw else 0,
                    "height": int(res.group(2)) if res else 0,
                }
            elif line.strip() and not line.startswith("#"):
                current["url"] = urllib.parse.urljoin(master, line)
                variants.append(current)
                current = {}

        if not variants:
            return master

        if quality:
            try:
                h = int(quality)
                for v in variants:
                    if v["height"] == h:
                        return v["url"]
            except (ValueError, TypeError):
                pass

        candidates = [v for v in variants if v["bandwidth"] <= max_bandwidth]
        if candidates:
            best = max(candidates, key=lambda v: v["bandwidth"])
        else:
            best = min(variants, key=lambda v: v["bandwidth"])
        return best["url"]


# ---------------------------------------------------------------------- #
# Match helpers
# ---------------------------------------------------------------------- #
def match_players(match):
    """Return (player1, player2) display names for either match format."""
    if "PlayerTeam1" in match:
        p1 = match.get("PlayerTeam1") or {}
        p2 = match.get("PlayerTeam2") or {}
        return _player_name(p1), _player_name(p2)
    teams = match.get("TeamsInMatch") or []
    names = []
    for team in teams:
        players = team.get("Players") or []
        if players:
            p = players[0]
            names.append("%s %s" % (p.get("FirstName", ""), p.get("LastName", "")))
    while len(names) < 2:
        names.append("")
    return names[0], names[1]


def _player_name(player):
    if not player:
        return ""
    first = player.get("PlayerFirstNameFull") or player.get("PlayerFirstName") or ""
    last = player.get("PlayerLastName") or ""
    return ("%s %s" % (first, last)).strip()


def match_scores(match):
    """Return a short live score line for a live match."""
    p1 = match.get("PlayerTeam1") or {}
    p2 = match.get("PlayerTeam2") or {}
    s1 = _set_scores(p1)
    s2 = _set_scores(p2)
    if not s1 and not s2:
        return ""
    return "%s  %s" % (s1, s2)


def _set_scores(player):
    sets = player.get("Sets") or []
    return " ".join(
        str(s.get("SetScore", ""))
        for s in sets
        if s.get("SetScore") is not None
    )


def match_title(match):
    p1, p2 = match_players(match)
    title = "%s vs %s" % (p1 or "TBD", p2 or "TBD")
    if match.get("CourtName"):
        title += " - %s" % match["CourtName"]
    return title


def find_video_for_match(match, videos):
    """Map a live match to its court feed video, if any."""
    tournament_tag = "%s_%s" % (
        match.get("TournamentId"),
        match.get("TournamentYear"),
    )
    court = str(match.get("CourtId", ""))
    for video in videos:
        info = video.get("additionalInfo") or {}
        v_tour = (info.get("tournament_id_year") or "").strip('"')
        v_court = (info.get("court_id") or "").strip('"')
        if v_tour == tournament_tag and v_court == court:
            return video
    return None


def format_duration(seconds):
    """Format duration in seconds to a human-readable string."""
    try:
        s = int(seconds)
        if s <= 0:
            return ""
        m, sec = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return "%dh %02dm" % (h, m)
        return "%dm %02ds" % (m, sec)
    except (ValueError, TypeError):
        return ""
