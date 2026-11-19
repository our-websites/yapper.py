"""Login for the telemetry panel: signed session cookies, CSRF tokens and the "Log in with Yapin'" OAuth2 flow.

Standard library only. Nothing is kept on the server for a session: the cookie itself is signed with TELEMETRY_SECRET.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

SESSION_COOKIE = "yapper_session"
STATE_COOKIE = "yapper_oauth"
SESSION_TTL = 7 * 24 * 3600
STATE_TTL = 600


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
    """obj["a"]["b"] for the path "a.b"; None when any step is missing."""
    for part in str(path).split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


class OAuth:
    """The authorization-code flow. Every endpoint comes from the environment so it can be pointed at Yapin's."""

    def __init__(self, client_id="", client_secret="", authorize_url="", token_url="", userinfo_url="", redirect_uri="",
                 scope="identify", id_field="id", name_field="username"):
        self.client_id, self.client_secret = client_id, client_secret
        self.authorize_url, self.token_url, self.userinfo_url = authorize_url, token_url, userinfo_url
        self.redirect_uri, self.scope = redirect_uri, scope
        self.id_field, self.name_field = id_field, name_field

    @classmethod
    def from_env(cls, env, redirect_uri=""):
        g = lambda k, d="": (env.get(k) or d).strip()
        return cls(g("YAPIN_CLIENT_ID"), g("YAPIN_CLIENT_SECRET"), g("YAPIN_AUTHORIZE_URL"), g("YAPIN_TOKEN_URL"),
                   g("YAPIN_USERINFO_URL"), g("YAPIN_REDIRECT_URI") or redirect_uri, g("YAPIN_SCOPE", "identify"),
                   g("YAPIN_USER_ID_FIELD", "id"), g("YAPIN_USERNAME_FIELD", "username"))

    @property
    def configured(self):
        return all((self.client_id, self.client_secret, self.authorize_url, self.token_url, self.userinfo_url, self.redirect_uri))

    def missing(self):
        names = {"YAPIN_CLIENT_ID": self.client_id, "YAPIN_CLIENT_SECRET": self.client_secret, "YAPIN_AUTHORIZE_URL": self.authorize_url,
                 "YAPIN_TOKEN_URL": self.token_url, "YAPIN_USERINFO_URL": self.userinfo_url, "YAPIN_REDIRECT_URI": self.redirect_uri}
        return [k for k, v in names.items() if not v]

    def login_url(self, state):
        query = urllib.parse.urlencode({"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
                                        "scope": self.scope, "state": state})
        sep = "&" if "?" in self.authorize_url else "?"
        return self.authorize_url + sep + query

    def _request(self, req):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read(65536))
        except (urllib.error.URLError, ValueError, OSError) as e:
            raise LoginError("Yapin' did not answer the login request.") from e

    def identify(self, code):
        """Swap the code from the callback for the person's (user_id, username). Raises LoginError."""
        form = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                                       "client_id": self.client_id, "client_secret": self.client_secret}).encode()
        token = self._request(urllib.request.Request(self.token_url, data=form, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": "yapper-telemetry-panel"}))
        access = token.get("access_token") if isinstance(token, dict) else None
        if not isinstance(access, str):
            raise LoginError("Yapin' did not accept the login.")
        info = self._request(urllib.request.Request(self.userinfo_url, headers={
            "Authorization": f"Bearer {access}", "Accept": "application/json", "User-Agent": "yapper-telemetry-panel"}))
        user_id = dig(info, self.id_field)
        if user_id is None or not str(user_id).strip() or isinstance(user_id, (dict, list)):
            raise LoginError("Yapin' did not say who you are.")
        name = dig(info, self.name_field)
        return str(user_id).strip(), str(name) if isinstance(name, (str, int)) else ""


class LoginError(Exception):
    pass
