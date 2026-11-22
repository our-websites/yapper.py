"""Live SVG badges for READMEs: they are drawn on request from the current numbers, so a README that shows one never needs editing.

    /badge/stats.svg      how many bots use yapper.py telemetry and how many servers they are in
    /badge/<bot id>.svg   one bot: its icon, name and server count (only bots that are approved and saved an invite link, which is the opt-in)

An SVG shown through <img> can't load other images or be clicked, so the bot's icon is fetched here (only from Yapin's own hosts, small
images only, cached) and embedded, and README Markdown wraps the badge in a link: [![Name](.../badge/ID.svg)](invite link).
"""
import base64
import html
import io
import ipaddress
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 2_000_000       # what is downloaded; it is shrunk before embedding when Pillow is installed
MAX_EMBED_BYTES = 200_000         # what may be embedded as it is when it can't be shrunk
ICON_PX = 48
CACHE_SECONDS, FAILED_SECONDS, CACHE_MAX = 6 * 3600, 600, 256
_cache = {}
_lock = threading.Lock()

BG, LINE, TEXT, MUTED, ACCENT = "#151a2c", "#283054", "#e9ecf6", "#9aa3c0", "#a393ff"


def fetch_image(url, hosts, timeout=4, public=False):
    """The picture at an https URL as a data: URI, or None. The URL's host must be one of `hosts`, or (public=True, for bot pictures,
    whose real host we can't know in advance) any host on the public internet; the server does the fetching, so nobody's browser contacts it."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme != "https" or parsed.username or parsed.password or not (parsed.hostname in hosts or (public and _is_public(parsed.hostname))):
        return None
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1]

    class SameHosts(urllib.request.HTTPRedirectHandler):
        """Yapin' may hand pictures over to a CDN: follow https redirects, but only to hosts on the public internet."""
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urlparse(newurl)
            if target.scheme != "https" or target.username or target.password or not (target.hostname in hosts or _is_public(target.hostname)):
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    result = None
    try:
        opener = urllib.request.build_opener(SameHosts)
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; yapper-telemetry-badge)", "Accept": "image/*"}), timeout=timeout) as r:
            kind = (r.headers.get_content_type() or "").lower()
            data = r.read(MAX_IMAGE_BYTES + 1)
            if kind not in IMAGE_TYPES or not 0 < len(data) <= MAX_IMAGE_BYTES:
                print(f"badge: not using picture {url} (type {kind}, {len(data)} bytes)", file=sys.stderr)
            else:
                result = _embed(data, kind)
    except Exception as e:
        print(f"badge: could not fetch picture {url}: {e}", file=sys.stderr)
        result = None
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[url] = (now + (CACHE_SECONDS if result else FAILED_SECONDS), result)
    return result


def _is_public(host):
    try:
        return bool(host) and all(ipaddress.ip_address(a[4][0]).is_global for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    except Exception:
        return False


def _embed(data, kind):
    """A data: URI for the picture, shrunk to a small PNG when Pillow is installed (avatars can be big or animated); otherwise as it is if small."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 25_000_000
        img = Image.open(io.BytesIO(data))
        img.seek(0)
        img = img.convert("RGBA")
        img.thumbnail((ICON_PX * 2, ICON_PX * 2))
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode()}"
    except ImportError:
        pass
    except Exception as e:
        print(f"badge: could not shrink picture: {e}", file=sys.stderr)
        return None
    return f"data:{kind};base64,{base64.b64encode(data).decode()}" if len(data) <= MAX_EMBED_BYTES else None


def pretty_name(name):
    """Bot usernames end in _0220 (their number): show it as #0220."""
    return re.sub(r"_(\d{4})$", r"#\1", str(name or "").strip())


def _clean(text, limit):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    text = " ".join(text.split())
    return html.escape(text if len(text) <= limit else text[:limit - 1] + "…", quote=True)


def _width(text, per_char):
    return int(len(html.unescape(text)) * per_char)


def _svg(width, height, label, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{label}"><title>{label}</title>'
            f'<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="8" fill="{BG}" stroke="{LINE}"/>{body}</svg>').encode()


FONT = 'font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif"'


def bot_badge(name, servers, icon=None):
    """One bot: icon (or its first letter), name and, when known, how many servers it is in."""
    title = _clean(pretty_name(name) or "Bot", 28)
    sub = f"{int(servers):,} server{'s' if int(servers) != 1 else ''}" if isinstance(servers, int) and not isinstance(servers, bool) and servers >= 0 else ""
    width = max(150, 56 + max(_width(title, 8.4), _width(sub, 6.6)) + 14)
    if icon and icon.startswith("data:image/"):
        pic = (f'<clipPath id="c"><circle cx="30" cy="24" r="14"/></clipPath>'
               f'<image x="16" y="10" width="28" height="28" clip-path="url(#c)" preserveAspectRatio="xMidYMid slice" href="{icon}" xlink:href="{icon}"/>')
    else:
        letter = _clean((pretty_name(name) or "?")[0].upper(), 1)
        pic = (f'<circle cx="30" cy="24" r="14" fill="#1c2340"/><text x="30" y="29.5" text-anchor="middle" {FONT} font-size="15" font-weight="600" fill="{ACCENT}">{letter}</text>')
    text = (f'<text x="54" y="{22 if sub else 29}" {FONT} font-size="14" font-weight="600" fill="{TEXT}">{title}</text>'
            + (f'<text x="54" y="37" {FONT} font-size="11" fill="{MUTED}">{sub}</text>' if sub else ""))
    return _svg(width, 48, f"{title}{', ' + sub if sub else ''}", pic + text)


def stats_badge(bots, servers):
    """Totals over the approved bots that have reported."""
    bots_text, servers_text = f"{int(bots):,} bot{'s' if int(bots) != 1 else ''}", f"{int(servers):,} server{'s' if int(servers) != 1 else ''}"
    label = f"yapper.py telemetry: {bots_text}, {servers_text}"
    left = 150
    right = max(110, _width(bots_text, 7.2) + _width(servers_text, 7.2) + 44)
    width = left + right
    body = (f'<path d="M8.5 .5H{left}V47.5H8.5a8 8 0 0 1-8-8v-31a8 8 0 0 1 8-8z" fill="#1c2340"/>'
            f'<text x="16" y="29" {FONT} font-size="13" font-weight="600" fill="{ACCENT}">yapper.py telemetry</text>'
            f'<text x="{left + 14}" y="29" {FONT} font-size="13" fill="{TEXT}"><tspan font-weight="600">{bots_text}</tspan>'
            f'<tspan fill="{MUTED}">  ·  </tspan><tspan>{servers_text}</tspan></text>')
    return _svg(width, 48, label, body)


def _count(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}" if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else ""


def featured_badge(bots, columns=3, limit=100):
    """A grid of every approved bot: small icon (or first letter), name, description, and server and member counts.
    `bots` is [(name, servers, members, description, icon data URI or None)]."""
    bots = list(bots)[:limit]
    cell_w, cell_h, pad = 236, 58, 6
    if not bots:
        body = f'<text x="16" y="29" {FONT} font-size="13" fill="{MUTED}">No approved bots yet</text>'
        return _svg(190, 48, "No approved bots yet", body)
    cols = min(columns, len(bots))
    rows = (len(bots) + cols - 1) // cols
    width, height = cols * cell_w + (cols + 1) * pad, rows * cell_h + (rows + 1) * pad
    out, names = [], []
    for i, (name, servers, members, desc, icon) in enumerate(bots):
        x, y = pad + (i % cols) * (cell_w + pad), pad + (i // cols) * (cell_h + pad)
        shown = pretty_name(name)
        title = _clean(shown or "Bot", 24)
        names.append(title)
        counts = "  ·  ".join(c for c in (_count(servers, "server"), _count(members, "member")) if c)
        cx, cy = x + 21, y + 21
        if icon and icon.startswith("data:image/"):
            pic = (f'<clipPath id="c{i}"><circle cx="{cx}" cy="{cy}" r="12"/></clipPath>'
                   f'<image x="{cx - 12}" y="{cy - 12}" width="24" height="24" clip-path="url(#c{i})" preserveAspectRatio="xMidYMid slice" href="{icon}" xlink:href="{icon}"/>')
        else:
            letter = _clean((shown or "?")[0].upper(), 1)
            pic = (f'<circle cx="{cx}" cy="{cy}" r="12" fill="#1c2340"/><text x="{cx}" y="{cy + 5}" text-anchor="middle" {FONT} font-size="13" font-weight="600" fill="{ACCENT}">{letter}</text>')
        lines = f'<text x="{x + 40}" y="{y + 18}" {FONT} font-size="12" font-weight="600" fill="{TEXT}">{title}</text>'
        d = _clean(desc, 38) if desc else ""
        if d:
            lines += f'<text x="{x + 40}" y="{y + 32}" {FONT} font-size="10" fill="{TEXT}" fill-opacity=".8">{d}</text>'
        if counts:
            lines += f'<text x="{x + 40}" y="{y + 46}" {FONT} font-size="10" fill="{MUTED}">{counts}</text>'
        out.append(f'<rect x="{x + .5}" y="{y + .5}" width="{cell_w - 1}" height="{cell_h - 1}" rx="7" fill="{BG}" stroke="{LINE}"/>{pic}{lines}')
    label = "yapper.py bots: " + ", ".join(names)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{label}"><title>{label}</title>{"".join(out)}</svg>').encode()
