# Tennis TV for Enigma2 / OpenATV

An unofficial Tennis TV client for Enigma2 receivers (OpenATV 7.x / Python 3).
It lists the live ATP matches (mapped to their court feeds) and the upcoming
schedule, and plays the HLS streams using the box's media player.

> **Disclaimer**: unofficial addon, not affiliated with or endorsed by ATP
> Media. A valid [Tennis TV](https://www.tennistv.com) subscription is
> required. Use at your own risk.

## Features

- Live Now — current matches with live scores, each mapped to its court feed
  (plus the World Feed).
- Upcoming — the next days' order of play.
- Plays via the box's built-in GStreamer player. A local HTTP proxy handles
  the Akamai tokenised/encrypted HLS so playback works on stock images.
- Zero third-party Python dependencies (stdlib only).
- Credentials are stored locally and the auth tokens are refreshed
  automatically.

## Requirements

- Enigma2 image with Python 3 (OpenATV 7.x or similar).
- GStreamer with HLS support (`gstreamer1.0-plugins-bad-hls`) — included in
  stock OpenATV 7.x images.

## Manual installation

Copy the four plugin files to the receiver over SCP or FTP:

```sh
# over SCP
PLUGIN_DIR=/usr/lib/enigma2/python/Plugins/Extensions/TennisTV
ssh root@<receiver-ip> "mkdir -p $PLUGIN_DIR"
scp plugin.py api.py proxy.py __init__.py root@<receiver-ip>:$PLUGIN_DIR/

# restart the GUI
ssh root@<receiver-ip> "killall -9 enigma2"
```

or use the bundled helper (same thing, with a GUI restart):

```sh
RECEIVER=root@192.168.1.100 ./install.sh
```

Then on the box: **Plugins → Tennis TV → Settings**, enter your email and
password, and press **GREEN** to save. Open **Live Now** and select a match.

### Files

| File         | Purpose                                        |
|--------------|------------------------------------------------|
| `plugin.py`  | Plugin entry point, menus and playback logic   |
| `api.py`     | Tennis TV API client (auth + data + playback)  |
| `proxy.py`   | Local HLS proxy (handles Akamai tokenised URLs)|
| `__init__.py`| Empty package marker                           |

## Where things are stored

- Credentials: `/etc/enigma2/ttv_credentials.json` (plain text)
- Auth tokens: `/etc/enigma2/ttv_tokens.json`

## Notes

- The plugin is a thin, read-only client for a service you already subscribe
  to. It uses Tennis TV's private (undocumented) web API, which may change or
  break at any time.
- Content is geo-restricted. If streams return "Access Denied", your egress
  IP is outside the licensed territory.
