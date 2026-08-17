# -*- coding: utf-8 -*-
"""Local HTTP proxy for Tennis TV HLS streams.

GStreamer's souphttpsrc normalises percent-encoding in URLs (decoding
``%2f`` -> ``/`` in the path), which breaks Akamai's HDN token HMAC
validation and causes 403 "Forbidden / Access Denied" for the variant
playlists.

This proxy listens on 127.0.0.1 and handles every HTTP request GStreamer's
hlsdemux needs -- variant playlist, AES-128 key and TS segments -- by
fetching the real URLs with Python's urllib, which preserves the
percent-encoding.  Playlist responses are rewritten so GStreamer routes all
subsequent requests back through the proxy and never touches the raw
tokenised Akamai URLs.
"""

import base64
import re
import threading

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urljoin
import urllib.request
import urllib.error

# Akamai rejects non-browser User-Agents (403). A full, realistic browser UA
# is required for the stream requests.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


class StreamProxy(object):
    def __init__(self, port=0):
        self._server = HTTPServer(("127.0.0.1", port), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()

    def wrap(self, url):
        """Return a proxy URL that GStreamer can play."""
        encoded = base64.urlsafe_b64encode(url.encode()).decode()
        return "http://127.0.0.1:%d/proxy?url=%s" % (self.port, encoded)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.strip("/") != "proxy":
            self.send_error(404)
            return
        params = parse_qs(parsed.query)
        raw = params.get("url", [""])[0]
        if not raw:
            self.send_error(400)
            return
        url = base64.urlsafe_b64decode(raw).decode()

        req = urllib.request.Request(url)
        req.add_header("User-Agent", BROWSER_UA)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            self.send_response(exc.code)
            self.end_headers()
            self.wfile.write(exc.read())
            return
        except Exception:
            self.send_error(502)
            return

        body = resp.read()
        ctype = resp.headers.get("Content-Type", "application/octet-stream")

        if body.lstrip().startswith(b"#EXTM3U") or "mpegurl" in ctype:
            text = self._rewrite_playlist(body.decode("utf-8", "replace"), url)
            body = text.encode("utf-8", "replace")
            ctype = "application/vnd.apple.mpegurl"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _rewrite_playlist(self, text, base_url):
        port = self.server.server_address[1]
        out = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                abs_url = stripped if "://" in stripped else urljoin(base_url, stripped)
                encoded = base64.urlsafe_b64encode(abs_url.encode()).decode()
                line = "http://127.0.0.1:%d/proxy?url=%s" % (port, encoded)
            elif stripped.startswith("#"):
                m = re.search(r'URI="([^"]+)"', stripped)
                if m:
                    key = m.group(1)
                    if "://" not in key:
                        key = urljoin(base_url, key)
                    encoded = base64.urlsafe_b64encode(key.encode()).decode()
                    new_uri = "http://127.0.0.1:%d/proxy?url=%s" % (port, encoded)
                    line = re.sub(r'URI="([^"]+)"', 'URI="%s"' % new_uri, stripped)
            out.append(line)
        return "\n".join(out)


def build_proxy():
    proxy = StreamProxy()
    proxy.start()
    return proxy