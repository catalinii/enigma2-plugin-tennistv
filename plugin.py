# -*- coding: utf-8 -*-
"""Tennis TV plugin for Enigma2 (OpenATV 7.x / Python 3).

Lists live ATP matches (mapped to their court feeds) and the upcoming
schedule, and plays the HLS streams via the box's media player.

Authentication uses the same Keycloak OIDC flow as the web player (see
api.py). Credentials are entered in the plugin Settings screen and persisted
to /etc/enigma2/ttv_credentials.json so they survive a reboot. No credentials
are stored in code.
"""

import functools
import json
import os
import sys
import threading
from urllib.parse import quote

# Enigma2 does not put the plugin directory on sys.path when it imports
# plugin.py, so make the sibling "api" module importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Plugins.Plugin import PluginDescriptor
from Screens.Screen import Screen
from Screens.MessageBox import MessageBox
from Components.MenuList import MenuList
from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.config import (
    config,
    ConfigSubsection,
    ConfigText,
    ConfigPassword,
    getConfigListEntry,
)
from Components.ConfigList import ConfigListScreen
from enigma import eServiceReference, eTimer
from Tools.Directories import resolveFilename, SCOPE_CONFIG

from api import (
    TennisTV,
    TennisTVAuthError,
    match_title,
    match_scores,
    match_players,
    find_video_for_match,
)
import proxy

PLUGIN_NAME = "Tennis TV"
PLUGIN_DESC = "Watch live ATP tennis from Tennis TV"

# Service reference type 4097 = GStreamer/IPTV playback.
GST_SERVICE_TYPE = 4097

config.plugins.tennistv = ConfigSubsection()
config.plugins.tennistv.username = ConfigText(default="")
config.plugins.tennistv.password = ConfigPassword(default="")

CRED_FILE = resolveFilename(SCOPE_CONFIG, "ttv_credentials.json")
TOKEN_FILE = resolveFilename(SCOPE_CONFIG, "ttv_tokens.json")


def _save_credentials():
    try:
        with open(CRED_FILE, "w") as fh:
            json.dump(
                {
                    "username": config.plugins.tennistv.username.value,
                    "password": config.plugins.tennistv.password.value,
                },
                fh,
            )
    except Exception:
        pass


def _load_credentials():
    try:
        with open(CRED_FILE, "r") as fh:
            data = json.load(fh)
            config.plugins.tennistv.username.value = data.get("username", "")
            config.plugins.tennistv.password.value = data.get("password", "")
    except Exception:
        pass


def _on_credentials_changed(*args):
    _save_credentials()


def get_api():
    return TennisTV(
        username=config.plugins.tennistv.username.value,
        password=config.plugins.tennistv.password.value,
        token_file=TOKEN_FILE,
    )


_global_proxy = None


def _get_proxy():
    global _global_proxy
    if _global_proxy is None:
        _global_proxy = proxy.build_proxy()
    return _global_proxy


_load_credentials()


def main(session, **kwargs):
    session.open(TennisTVMenu)


def Plugins(**kwargs):
    return PluginDescriptor(
        name=PLUGIN_NAME,
        description=PLUGIN_DESC,
        where=PluginDescriptor.WHERE_PLUGINMENU,
        fnc=main,
    )


# ---------------------------------------------------------------------- #
# Main menu
# ---------------------------------------------------------------------- #
class TennisTVMenu(Screen):
    skin = """
        <screen name="TennisTVMenu" position="center,center" size="620,440" title="Tennis TV">
            <widget name="menu" position="10,10" size="600,420" itemHeight="40"
                    font="Regular;22" scrollbarMode="showOnDemand" />
        </screen>
        """

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self["menu"] = MenuList([])
        self["menu"].setList(
            [
                ("Live Now", self.open_live),
                ("Upcoming", self.open_upcoming),
                ("Settings", self.open_settings),
            ]
        )
        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions"],
            {"ok": self.ok, "cancel": self.close, "red": self.close},
        )

    def ok(self):
        current = self["menu"].getCurrent()
        if current and len(current) > 1:
            current[1]()

    def open_live(self):
        self.session.open(TennisTVLive)

    def open_upcoming(self):
        self.session.open(TennisTVUpcoming)

    def open_settings(self):
        self.session.open(TennisTVSettings)


# ---------------------------------------------------------------------- #
# Base list screen (shared loading logic)
# ---------------------------------------------------------------------- #
class _MatchListScreen(Screen):
    skin = """
        <screen name="TennisTVList" position="center,center" size="760,500" title="Tennis TV">
            <widget name="list" position="10,10" size="740,440" itemHeight="36"
                    font="Regular;21" scrollbarMode="showOnDemand" />
            <widget name="status" position="10,455" size="740,32" font="Regular;20"
                    halign="center" />
        </screen>
        """

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self["list"] = MenuList([])
        self["status"] = Label("Loading...")
        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions"],
            {"ok": self.ok, "cancel": self.close, "red": self.close},
        )

        self._error = None
        self._pending = {}
        self._play_timer = eTimer()
        self._play_timer.callback.append(self._do_play)
        self._populate_timer = eTimer()
        self._populate_timer.callback.append(self._populate)

        self.onShown.append(self._start_load)

    def _start_load(self):
        threading.Thread(target=self._load).start()

    def _load(self):
        try:
            self._fetch()
            self._error = None
        except Exception as exc:
            self._error = str(exc)
        self._populate_timer.start(0, True)

    def _fetch(self):
        raise NotImplementedError

    def _populate(self):
        if self._error:
            self["status"].setText("Error: %s" % self._error)
            self["list"].setList([])
            return
        items = self._build_items()
        if not items:
            self["status"].setText(self._empty_text())
        else:
            self["status"].setText("")
        self["list"].setList(items)

    def _build_items(self):
        raise NotImplementedError

    def _empty_text(self):
        return "Nothing to show."

    def ok(self):
        current = self["list"].getCurrent()
        if current and len(current) > 1:
            current[1]()

    # -- playback ------------------------------------------------------ #
    def _start_play(self, media_id, title):
        self["status"].setText("Loading stream...")
        self._pending = {"media_id": media_id, "title": title, "url": None, "error": None}
        threading.Thread(target=self._resolve, args=(media_id, title)).start()

    def _resolve(self, media_id, title):
        try:
            url = get_api().stream_variant_url(media_id)
            self._pending["url"] = url
        except TennisTVAuthError as exc:
            self._pending["error"] = str(exc)
        except Exception as exc:
            self._pending["error"] = "Failed to resolve stream: %s" % exc
        self._play_timer.start(0, True)

    def _do_play(self):
        url = self._pending.get("url")
        if not url:
            self["status"].setText("Error: %s" % (self._pending.get("error") or "unknown"))
            return
        try:
            play_url = _get_proxy().wrap(url)
        except Exception as exc:
            self["status"].setText("Proxy error: %s" % exc)
            return
        encoded_url = quote(play_url, safe="")
        encoded_name = quote(self._pending.get("title", PLUGIN_NAME), safe="")
        ref = "%d:0:1:0:0:0:0:0:0:0:%s:%s" % (GST_SERVICE_TYPE, encoded_url, encoded_name)
        self.session.nav.playService(eServiceReference(ref))
        self.close()

    def _show_message(self, text=""):
        self["status"].setText(text)


# ---------------------------------------------------------------------- #
# Live matches
# ---------------------------------------------------------------------- #
class TennisTVLive(_MatchListScreen):
    def __init__(self, session):
        _MatchListScreen.__init__(self, session)
        self.setTitle(PLUGIN_NAME + " - Live Now")
        self._matches = []
        self._videos = []

    def _fetch(self):
        api = get_api()
        self._matches = api.live_matches()
        self._videos = api.live_videos()

    def _empty_text(self):
        return "No live matches right now."

    def _build_items(self):
        items = []

        world_feed = next(
            (
                v
                for v in self._videos
                if (v.get("additionalInfo") or {}).get("court_id", "").strip('"')
                == "WORLD_FEED"
            ),
            None,
        )
        if world_feed and world_feed.get("mediaId"):
            items.append(
                (
                    "World Feed",
                    functools.partial(self._start_play, world_feed["mediaId"], "World Feed"),
                )
            )

        for match in self._matches:
            title = match_title(match)
            score = match_scores(match)
            label = title
            if score:
                label = "%s  [%s]" % (title, score)

            video = find_video_for_match(match, self._videos)
            if video and video.get("mediaId"):
                items.append(
                    (label, functools.partial(self._start_play, video["mediaId"], title))
                )
            else:
                items.append((label, functools.partial(self._show_message, "No dedicated stream for this match.")))

        return items


# ---------------------------------------------------------------------- #
# Upcoming matches
# ---------------------------------------------------------------------- #
class TennisTVUpcoming(_MatchListScreen):
    def __init__(self, session):
        _MatchListScreen.__init__(self, session)
        self.setTitle(PLUGIN_NAME + " - Upcoming")
        self._matches = []

    def _fetch(self):
        self._matches = get_api().upcoming_matches()

    def _empty_text(self):
        return "No upcoming matches scheduled."

    def _build_items(self):
        items = []
        for match in self._matches:
            p1, p2 = match_players(match)
            label = "%s vs %s" % (p1 or "TBD", p2 or "TBD")

            when = match.get("NotBeforeText") or ""
            if match.get("NotBefore") and match["NotBefore"] not in ("Followed By", ""):
                when = ("%s %s" % (when, match["NotBefore"])).strip()

            details = []
            if match.get("CourtName"):
                details.append(match["CourtName"])
            if isinstance(match.get("Round"), dict):
                details.append(match["Round"].get("RoundName", ""))
            if when:
                details.append(when)
            if details:
                label = "%s - %s" % (label, " | ".join(details))

            label = "%s  [UPCOMING]" % label

            items.append((label, functools.partial(self._show_message, "Match not live yet.")))

        return items


# ---------------------------------------------------------------------- #
# Settings
# ---------------------------------------------------------------------- #
class TennisTVSettings(ConfigListScreen, Screen):
    skin = """
        <screen name="TennisTVSettings" position="center,center" size="620,380" title="Tennis TV Settings">
            <widget name="config" position="10,10" size="600,300" itemHeight="38"
                    font="Regular;21" scrollbarMode="showOnDemand" />
            <widget name="hint" position="10,325" size="600,30" font="Regular;18"
                    halign="center" />
        </screen>
        """

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self["actions"] = ActionMap(
            ["SetupActions", "ColorActions"],
            {"green": self.keySave, "red": self.keyCancel, "cancel": self.keyCancel},
            -2,
        )
        self.list = []
        ConfigListScreen.__init__(self, self.list, session=session)
        self["hint"] = Label("GREEN = save   RED/EXIT = cancel")
        self.createSetup()

    def createSetup(self):
        self.list.append(
            getConfigListEntry("Email", config.plugins.tennistv.username)
        )
        self.list.append(
            getConfigListEntry("Password", config.plugins.tennistv.password)
        )
        self["config"].list = self.list
        self["config"].l.setList(self.list)
        config.plugins.tennistv.username.addNotifier(
            _on_credentials_changed, initial_call=False
        )
        config.plugins.tennistv.password.addNotifier(
            _on_credentials_changed, initial_call=False
        )

    def keySave(self):
        _save_credentials()
        config.plugins.tennistv.save()
        self.close()

    def keyCancel(self):
        self.close()
