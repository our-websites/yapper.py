"""Login for the telemetry panel: signed session cookies, CSRF tokens and the "Log in with Yapin'" OAuth2 flow.

Standard library only. Nothing is kept on the server for a session: the cookie itself is signed with TELEMETRY_SECRET.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

SESSION_COOKIE = "yapper_session"
STATE_COOKIE = "yapper_oauth"
SESSION_TTL = 7 * 24 * 3600
STATE_TTL = 600
DEFAULT_AUTHORIZE = "https://www.yapin.gg/oauth/authorize"
DEFAULT_TOKEN = "https://www.yapin.gg/api/oauth/token"
DEFAULT_USERINFO = "https://www.yapin.gg/api/oauth/me"
DEFAULT_ID_FIELDS = "id,sub,user_id"
DEFAULT_NAME_FIELDS = "username,preferred_username"
DEFAULT_DISPLAY_FIELDS = "display_name,name"
DEFAULT_AVATAR_FIELDS = "avatar_path,avatar_url,picture"
DEFAULT_SITE = "https://www.yapin.gg"
DEFAULT_FILES = "https://files.yapper.website"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign(secret, purpose, data, ttl, now=None):
    """A tamper-proof string holding `data` that stops working after `ttl` seconds. `purpose` keeps kinds apart."""
    body = _b64(json.dumps(dict(data, e=int((now or time.time()) + ttl)), separators=(",", ":")).encode())
    mac = hmac.new(secret, f"{purpose}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unsign(secret, purpose, value, now=None):
    """The data inside a string made by sign(), or None when it was changed, is for another purpose or has expired."""
    try:
        body, mac = value.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(secret, f"{purpose}.{body}".encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if isinstance(data, dict) and data["e"] > (now or time.time()) else None
    except Exception:
        return None


def make_session(secret, user_id, username, now=None):
    return sign(secret, "session", {"u": str(user_id), "n": str(username or "")[:64], "c": secrets.token_urlsafe(16)}, SESSION_TTL, now)


class Session:
    def __init__(self, user_id, username, csrf):
        self.user_id, self.username, self.csrf = user_id, username, csrf


def read_session(secret, cookie_value, now=None):
    data = unsign(secret, "session", cookie_value or "", now)
    if not data or not data.get("u"):
        return None
    return Session(str(data["u"]), data.get("n", ""), str(data.get("c", "")))


def csrf_ok(session, token):
    return bool(session and token and hmac.compare_digest(session.csrf, str(token)))


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


def cookie(name, value, max_age, secure):
    """A Set-Cookie header value: HttpOnly, SameSite=Lax, Secure when served over HTTPS."""
    return f"{name}={value}; Path=/; Max-Age={int(max_age)}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def dig(obj, path):
    """obj["a"]["b"] for the path "a.b"; None when any step is missing. "a,b" tries a, then b (first one that is set)."""
    for alternative in str(path).split(","):
        found = obj
        for part in alternative.strip().split("."):
            found = found[part] if isinstance(found, dict) and part in found else None
        if found not in (None, ""):
            return found
    return None


class OAuth:
    """Login with Yapin' (OAuth2 authorization code), done the way shapes-bot's dashboard does it. Every endpoint comes from
    the environment so it can be pointed elsewhere."""

    def __init__(self, client_id="", client_secret="", authorize_url="", token_url="", userinfo_url="", redirect_uri="",
                 scope="identify", id_field=DEFAULT_ID_FIELDS, name_field=DEFAULT_NAME_FIELDS, display_field=DEFAULT_DISPLAY_FIELDS,
                 avatar_field=DEFAULT_AVATAR_FIELDS, site_url=DEFAULT_SITE, files_url=DEFAULT_FILES):
        self.client_id, self.client_secret = client_id, client_secret
        self.authorize_url, self.token_url, self.userinfo_url = authorize_url, token_url, userinfo_url
        if userinfo_url.rstrip("/").endswith("/userinfo"):    # Yapin' calls it /me
            self.userinfo_url = userinfo_url.rstrip("/")[:-len("userinfo")] + "me"
        self.redirect_uri, self.scope = redirect_uri, scope
        self.id_field, self.name_field, self.display_field, self.avatar_field = id_field, name_field, display_field, avatar_field
        self.site_url, self.files_url = site_url.rstrip("/"), files_url.rstrip("/")

    @classmethod
    def from_env(cls, env, redirect_uri=""):
        g = lambda k, d="": (env.get(k) or d).strip()
        return cls(g("YAPIN_CLIENT_ID"), g("YAPIN_CLIENT_SECRET"), g("YAPIN_AUTHORIZE_URL", DEFAULT_AUTHORIZE), g("YAPIN_TOKEN_URL", DEFAULT_TOKEN),
                   g("YAPIN_USERINFO_URL", DEFAULT_USERINFO), g("YAPIN_REDIRECT_URI") or redirect_uri, g("YAPIN_SCOPE", "identify"),
                   g("YAPIN_USER_ID_FIELD", DEFAULT_ID_FIELDS), g("YAPIN_USERNAME_FIELD", DEFAULT_NAME_FIELDS),
                   g("YAPIN_DISPLAY_NAME_FIELD", DEFAULT_DISPLAY_FIELDS), g("YAPIN_AVATAR_FIELD", DEFAULT_AVATAR_FIELDS),
                   g("YAPIN_SITE_URL", DEFAULT_SITE), g("YAPIN_FILES_URL", DEFAULT_FILES))

    @property
    def configured(self):
        """The redirect address is optional: when it isn't set it is worked out from the request."""
        return all((self.client_id, self.client_secret, self.authorize_url, self.token_url, self.userinfo_url))

    def missing(self):
        names = {"YAPIN_CLIENT_ID": self.client_id, "YAPIN_CLIENT_SECRET": self.client_secret, "YAPIN_AUTHORIZE_URL": self.authorize_url,
                 "YAPIN_TOKEN_URL": self.token_url, "YAPIN_USERINFO_URL": self.userinfo_url}
        return [k for k, v in names.items() if not v]

    def image_hosts(self):
        """Hosts profile pictures may come from (they are what Yapin' says: its site and its files host)."""
        return {urllib.parse.urlparse(u).hostname for u in (self.site_url, self.files_url) if u} - {None}

    def login_url(self, state, redirect_uri=None):
        query = urllib.parse.urlencode({"client_id": self.client_id, "response_type": "code", "scope": self.scope,
                                        "state": state, "redirect_uri": redirect_uri or self.redirect_uri})
        return self.authorize_url + ("&" if "?" in self.authorize_url else "?") + query

    @staticmethod
    def _unwrap(data):
        """Yapin' sometimes wraps answers as {"data": {...}} (or user / result)."""
        if not isinstance(data, dict):
            return {}
        for key in ("data", "user", "result"):
            if isinstance(data.get(key), dict):
                return {**data, **data[key]}
        return data

    def _request(self, req):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return self._unwrap(json.loads(r.read(65536)))
        except urllib.error.HTTPError as e:
            try:
                return dict(self._unwrap(json.loads(e.read(65536))), _status=e.code)
            except (ValueError, OSError):
                return {"_status": e.code}
        except (urllib.error.URLError, ValueError, OSError) as e:
            raise LoginError("Yapin' did not answer the login request.") from e

    def _token(self, code, redirect_uri):
        """Trade the code for a token: client credentials in the body first, then HTTP Basic, like shapes-bot does."""
        base = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri or self.redirect_uri}
        basic = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        attempts = ((dict(base, client_id=self.client_id, client_secret=self.client_secret), {}),
                    (dict(base, client_id=self.client_id), {"Authorization": basic}))
        for form, extra in attempts:
            answer = self._request(urllib.request.Request(self.token_url, data=urllib.parse.urlencode(form).encode(), method="POST", headers={
                "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": "yapper-telemetry-panel", **extra}))
            token = answer.get("access_token") or answer.get("token") or answer.get("accessToken")
            if isinstance(token, str) and token:
                return token
            if answer.get("_status") not in (400, 401):
                break
        raise LoginError("Yapin' did not accept the login.")

    def identify(self, code, redirect_uri=None):
        """Swap the code from the callback for who the person is: (user_id, username, display name, picture link).
        Raises LoginError."""
        token = self._token(code, redirect_uri)
        info = self._request(urllib.request.Request(self.userinfo_url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "yapper-telemetry-panel"}))
        user_id = dig(info, self.id_field)
        if user_id is None or isinstance(user_id, (dict, list)) or not str(user_id).strip():
            raise LoginError("Yapin' did not say who you are.")
        text = lambda v: str(v)[:64] if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
        return (str(user_id).strip(), text(dig(info, self.name_field)), text(dig(info, self.display_field)),
                self.asset_url(dig(info, self.avatar_field)))

    def asset_url(self, value):
        """Yapin' gives pictures as a path: https://<files host>/avatars/<path>, a path starting with / lives on the site,
        and a full https link is used as it is."""
        if not isinstance(value, str) or not value.strip() or len(value) > 300:
            return None
        value = value.strip()
        if value.startswith("http://"):
            value = "https://" + value[len("http://"):]
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("https://"):
            return value.replace(" ", "%20")
        if value.startswith("/"):
            return (self.site_url + value).replace(" ", "%20")
        return f"{self.files_url}/avatars/{value.lstrip('/')}".replace(" ", "%20")


class LoginError(Exception):
    pass
