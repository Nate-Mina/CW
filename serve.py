#!/usr/bin/env python3
"""
Self-contained server for the CW (Clark & Wolcott) Wix export.

Two capabilities:
  1. Serves the local site files (index.html, pages/, scripts/, ...).
  2. Rewrites every Wix CDN reference (static.wixstatic.com, *.parastorage.com,
     www.clarkandwolcott.net, fallback.wix.com, ...) inside served HTML/JS to a
     local /cdn/<host>/... path.

     On a request for /cdn/<host>/<path>, it serves the on-disk cached copy if
     present, otherwise fetches it from the real CDN, caches it under ./cdn/,
     and returns it. This makes the whole site work OFFLINE after a one-time
     warm-up (run:  python3 serve.py --warm  while online).

  Also keeps the bracket->dash fix from the original serve.js:
     rb_wixui.thunderbolt[NAME].HASH.js  ->  rb_wixui.thunderbolt-NAME-.HASH.js

Usage:
  python3 serve.py                 # serve (proxy+cache on demand)
  python3 serve.py --warm          # pre-download all statically-referenced CDN
                                    #   assets into ./cdn, then exit
  PORT=8080 python3 serve.py       # custom port
"""

import os
import re
import sys
import shutil
import hashlib
import urllib.parse
import urllib.request
import http.server
import socketserver

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
HOST = "127.0.0.1"
CDN_DIR = os.path.join(ROOT, "cdn")
IMAGES_DIR = os.path.join(ROOT, "images")  # local mirror of wixstatic media
CACHE_DIR = os.path.join(ROOT, ".cdncache")  # rewritten-HTML cache

# Wix serves images from static.wixstatic.com, which is hotlink-protected
# (returns 403 on direct fetches). We mirror those exact media files in
# ./images (<id>-mv2.<ext>), so intercept those CDN requests and serve the
# local copy instead of proxying to the dead/403 host.
WIXSTATIC_MEDIA_RE = re.compile(
    r"static\.wixstatic\.com/media/([0-9a-f_]+)~mv2\.(png|jpe?g|webp|gif)",
    re.IGNORECASE,
)


def try_local_wixstatic(url_path):
    """If url_path is a wixstatic media URL whose base file exists in
    ./images, return the local absolute path; else None."""
    m = WIXSTATIC_MEDIA_RE.search(url_path)
    if not m:
        return None
    img_id, ext = m.group(1), m.group(2).lower()
    if not os.path.isdir(IMAGES_DIR):
        return None
    exact = os.path.join(IMAGES_DIR, f"{img_id}-mv2.{ext}")
    if os.path.isfile(exact):
        return exact
    # fall back to any <id>-mv2*.<ext> variant (e.g. ..._d_4608_..._s_4_2.jpg)
    for fn in os.listdir(IMAGES_DIR):
        if fn.startswith(f"{img_id}-mv2") and fn.lower().endswith(f".{ext}"):
            return os.path.join(IMAGES_DIR, fn)
    return None

# Hosts whose assets we route through /cdn/ and proxy/cache.
# Catch-all for the whole Wix ecosystem:
#   *.wixstatic.com, *.parastorage.com, *.wix.com, clarkandwolcott.net
# We capture (scheme)(host)(slash)(rest-of-path) and rebuild as
#   /cdn/<host><slash><rest>
# so the scheme is dropped (no duplicate https://) and both normal
# (https://host/path) and JSON-escaped (https:\/\/host\/path) forms work.
_HOSTS = (
    r"(?:[a-z0-9-]+\.)*(?:wixstatic\.com|parastorage\.com|wix\.com)|"
    r"(?:[a-z0-9-]+\.)*clarkandwolcott\.net"
)
# Hosts we intentionally leave alone. `frog.wix.com` is an analytics/perf beacon
# whose query string embeds the page URL (e.g. url=%2Fwww.clarkandwolcott.net%2Fgallery).
# Rewriting the inner host there produced `url=%2Fcdn%2F...` (an invalid, double-rewritten
# URL) which throws "Failed to construct 'URL'" and aborts SPA hydration -> blank page.
_SKIP_HOSTS = re.compile(r"frog\.wix\.com$")
# rest = path/query, allowing escaped slashes (\/) and normal chars, stopping
# at a quote / space / close-paren / angle-bracket / end. Optional, so bare
# "https://host" (config strings in the viewer-model) are rewritten too.
_HOSTS_RE = re.compile(_HOSTS)
URL_RE = re.compile(
    r"(?P<pre>https?://|https?:\\/\\/|//)"
    r"(?P<host>" + _HOSTS + r")"
    r"(?P<slash>/|\\/)?"
    r"(?P<rest>(?:[^\"'\\s)\\<>]|\\/)+)?"
)


def _rewrite_sub(m):
    host = m.group("host")
    if _SKIP_HOSTS.search(host):
        return m.group(0)  # leave analytics beacons untouched
    slash = m.group("slash") or ""
    rest = m.group("rest") or ""
    return "/cdn/" + host + slash + rest


def rewrite_html(text):
    """Rewrite Wix CDN absolute URLs to local /cdn/ paths.

    Idempotent: already-rewritten '/cdn/<host>/...' URLs are left alone so we
    never double-rewrite (which produced invalid URLs in query strings).
    """
    if "/cdn/" in text:
        # Only rewrite the still-absolute ones; skip anything already /cdn/.
        def _sub(m):
            if m.group(0).startswith("/cdn/"):
                return m.group(0)
            return _rewrite_sub(m)
        return URL_RE.sub(_sub, text)
    return URL_RE.sub(_rewrite_sub, text)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REF = "https://www.clarkandwolcott.net/"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".bin": "application/octet-stream",
    ".map": "application/json; charset=utf-8",
}

BRACKET_RE = re.compile(r"\[([^\]]+)\]")


# --------------------------------------------------------------------------- #
# CDN path helpers
# --------------------------------------------------------------------------- #
def cdn_local_path(rest):
    """Map '/cdn/<host>/<path>[?query]' -> (abs_file_path, full_remote_url)."""
    rest = rest[len("/cdn/"):] if rest.startswith("/cdn/") else rest
    host, _, pathq = rest.partition("/")
    path_part, sep, query = pathq.partition("?")
    path_part = urllib.parse.unquote(path_part)
    remote = "https://" + host + "/" + path_part + (sep + query if query else "")
    # local file (query -> short hash suffix to keep filenames valid on Windows)
    # Sanitize each path segment against Windows-illegal characters.
    illegal = '<>:"|?*'
    def clean(seg):
        seg = seg.strip("/")
        for ch in illegal:
            seg = seg.replace(ch, "_")
        return seg
    segs = [clean(host)] + [clean(s) for s in path_part.split("/") if s != ""]
    local_rel = os.path.join("cdn", *segs)
    if query:
        local_rel += "_q" + hashlib.sha1(query.encode()).hexdigest()[:8]
    return os.path.normpath(os.path.join(ROOT, local_rel)), remote


def proxy_fetch(remote, local_file):
    """Fetch remote URL, cache to local_file, return bytes or None."""
    os.makedirs(os.path.dirname(local_file), exist_ok=True)
    req = urllib.request.Request(remote, headers={"User-Agent": UA, "Referer": REF})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
    except Exception as e:
        print(f"  [proxy FAIL] {remote} -> {e}")
        return None, None
    with open(local_file, "wb") as f:
        f.write(data)
    return data, ctype


# --------------------------------------------------------------------------- #
# HTML rewriting (rewrite_html is defined above, near the URL_RE definition)
# --------------------------------------------------------------------------- #
_html_cache = {}  # (abspath, mtime) -> rewritten bytes


def serve_rewritten_html(abs_path):
    mtime = os.path.getmtime(abs_path)
    key = (abs_path, mtime)
    if key in _html_cache:
        return _html_cache[key]
    with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    out = rewrite_html(text).encode("utf-8")
    _html_cache[key] = out
    return out


# --------------------------------------------------------------------------- #
# File resolution (local, with bracket->dash fix)
# --------------------------------------------------------------------------- #
def resolve_local(url_path):
    p = urllib.parse.unquote(url_path.split("?")[0].split("#")[0])
    if p in ("", "/"):
        p = "/index.html"
    safe = os.path.normpath(p).lstrip("/\\")
    candidate = os.path.normpath(os.path.join(ROOT, safe))
    if not candidate.startswith(ROOT):
        return None
    if os.path.isfile(candidate):
        return candidate
    dashed = BRACKET_RE.sub(r"-\1-", candidate)
    if dashed != candidate and os.path.isfile(dashed):
        return dashed
    return None


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        self.handle_req()

    def do_HEAD(self):
        self.handle_req()

    def handle_req(self):
        url_path = self.path
        # --- Static offline Gallery (self-contained, no Wix Pro Gallery fetch) ---
        # The Wix Pro Gallery renders client-side from a runtime endpoint Wix
        # blocks to offline fetches, leaving the page blank. Serve our local
        # static masonry grid instead for any gallery request.
        norm = url_path.split("?")[0].split("#")[0]
        if norm in ("/gallery", "/pages/gallery.html", "/gallery.html"):
            gpath = os.path.join(ROOT, "gallery-offline.html")
            if os.path.isfile(gpath):
                with open(gpath, "rb") as f:
                    data = f.read()
                self._send(data, "text/html; charset=utf-8")
                return
        # --- CDN proxy/cache route ---
        if url_path.startswith("/cdn/"):
            # Serve hotlink-protected wixstatic media from local ./images.
            local_img = try_local_wixstatic(url_path)
            if local_img:
                with open(local_img, "rb") as f:
                    data = f.read()
                ext = os.path.splitext(local_img)[1].lower()
                self._send(data, MIME.get(ext, "application/octet-stream"))
                return
            local_file, remote = cdn_local_path(url_path)
            if os.path.isfile(local_file):
                with open(local_file, "rb") as f:
                    data = f.read()
                ext = os.path.splitext(local_file)[1].lower()
                self._send(data, MIME.get(ext, "application/octet-stream"))
                return
            data, ctype = proxy_fetch(remote, local_file)
            if data is None:
                self.send_error(404, "CDN asset unavailable (offline): " + remote)
                return
            self._send(data, ctype or "application/octet-stream")
            return

        # --- local file route ---
        f = resolve_local(url_path)
        if not f:
            self.send_error(404, "Not Found: " + url_path)
            return
        ext = os.path.splitext(f)[1].lower()
        if ext in (".html", ".htm"):
            data = serve_rewritten_html(f)
            self._send(data, "text/html; charset=utf-8")
            return
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            self.send_error(404, "Unreadable: " + url_path)
            return
        self._send(data, MIME.get(ext, "application/octet-stream"))

    def log_message(self, fmt, *args):
        print("[CW] %s - %s" % (self.address_string(), fmt % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# Warm-up: download every statically-referenced CDN asset into ./cdn
# --------------------------------------------------------------------------- #
def warm():
    os.makedirs(CDN_DIR, exist_ok=True)
    html_files = [os.path.join(ROOT, f) for f in os.listdir(ROOT) if f.endswith(".html")]
    pages_dir = os.path.join(ROOT, "pages")
    if os.path.isdir(pages_dir):
        html_files += [os.path.join(pages_dir, f)
                       for f in os.listdir(pages_dir) if f.endswith(".html")]

    urls = set()
    for hf in html_files:
        text = open(hf, encoding="utf-8", errors="ignore").read()
        for m in URL_RE.finditer(text):
            # Rebuild the full original URL from the captured groups.
            host = m.group("host")
            slash = m.group("slash") or "/"
            rest = m.group("rest") or ""
            u = "https://" + host + slash + rest
            if u.startswith("https://"):
                urls.add(u)

    print(f"Warm: found {len(urls)} unique CDN URLs across "
          f"{len(html_files)} HTML files.")
    ok = fail = 0
    for u in sorted(urls):
        local_file, _ = cdn_local_path("/cdn/" + u[len("https://"):])
        if os.path.isfile(local_file):
            ok += 1
            continue
        try:
            data, _ = proxy_fetch(u, local_file)
        except Exception as e:
            print(f"  [skip] {u[:90]} -> {e}")
            fail += 1
            continue
        if data is None:
            fail += 1
        else:
            ok += 1
            print(f"  saved {os.path.getsize(local_file):>8} B  {u[:90]}")
    print(f"Warm complete: {ok} cached (incl. pre-existing), {fail} failed.")
    print(f"Cached tree: {CDN_DIR}")


def _watched_paths():
    """All files/dirs whose modification should trigger a server restart."""
    paths = [os.path.abspath(__file__)]
    for name in os.listdir(ROOT):
        p = os.path.join(ROOT, name)
        # Skip heavy/cache dirs we never edit by hand.
        if name in (".cdncache", "__pycache__", ".git"):
            continue
        paths.append(p)
    for sub in ("pages", "images", "styles", "vendor", "scripts"):
        d = os.path.join(ROOT, sub)
        if os.path.isdir(d):
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    paths.append(os.path.join(root, fn))
    return paths


def _autoreload_loop():
    """Restart the server whenever a watched file changes on disk.

    A background watcher thread checks mtimes while serve_forever() runs in the
    main thread; on change it shuts the server down, the main loop re-creates it.
    """
    import time, threading
    print("Autoreload: watching project files for changes (Ctrl+C to stop)...")
    while True:
        mtimes = {}
        for p in _watched_paths():
            try:
                mtimes[p] = os.path.getmtime(p)
            except OSError:
                pass
        httpd = Server((HOST, PORT), Handler)
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                time.sleep(0.6)
                for p in _watched_paths():
                    try:
                        if os.path.getmtime(p) != mtimes.get(p):
                            print("[CW] change detected -> restarting...")
                            stop.set()
                            httpd.shutdown()
                            return
                    except OSError:
                        pass

        w = threading.Thread(target=watch, daemon=True)
        w.start()
        print(f"[CW] serving at  http://{HOST}:{PORT}/   (root: {ROOT})")
        print("[CW] CDN assets proxied+cached under ./cdn. Editing any file auto-restarts.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            stop.set()
            httpd.shutdown()
            print("\nStopped.")
            return
        stop.set()
        w.join(timeout=2)


if __name__ == "__main__":
    if "--warm" in sys.argv:
        warm()
        sys.exit(0)
    os.chdir(ROOT)
    _autoreload_loop()
