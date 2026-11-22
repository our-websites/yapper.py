"""The pages of the telemetry server: the landing page everyone sees and the panel people log in to. Dark mode only.

Everything that is not fixed text goes through esc(). The page uses a per-response nonce for its one <style> and one
<script>, so the Content-Security-Policy can forbid everything else (no inline styles on elements, no inline handlers).
"""
import contextvars
import html
import json
import re
import time
import urllib.parse
import zoneinfo

from telemetry_store import assignable_roles, bot_state, can_approve, can_change_owner, can_remove_roles, can_revoke

# What a report looks like (tests check this has the same shape as yapper.telemetry.collect()).
EXAMPLE_REPORT = {
    "schema": 1,
    "event": "heartbeat",
    "sent_at": "2026-03-14T18:30:12+00:00",
    "sdk": {"version": "1.8.4", "package_version": "1.8.4"},
    "runtime": {"python": "3.12.3", "os": "Linux"},
    "bot": {"id": "123456789", "username": "Cool Bot", "avatar": "https://cdn.example/avatars/123456789.png", "description": "Rolls dice for your server.", "uptime_seconds": 86412},
    "servers": {"count": 14, "members_total": 2310, "online_total": 412},
    "commands": [
        {"name": "ping", "description": "Check if the bot is alive", "options": []},
        {"name": "vote", "description": "Start a pizza vote", "options": [{"name": "topping", "type": "string", "required": False}]},
    ],
    "cogs": ["Fun", "Moderation"],
    "extensions": ["yapper.telemetry"],
    "listeners": 3,
    "buttons": 2,
    "extra": {"games_played": 1280, "premium_servers": 3},
}

MESSAGES = {
    "approved": ("ok", "Bot approved. Its owner can now log in and copy the private key."),
    "applied": ("ok", "Application saved. Ask in the support server to get it approved; its status shows here."),
    "rotated": ("ok", "New key made. Copy it into TELEMETRY_KEY and restart your bot: the old one no longer works."),
    "shared": ("ok", "Access saved."),
    "unshared": ("ok", "Access removed."),
    "need_login": ("bad", "That username can't be found. They need to log in to the panel first, or use their user ID."),
    "ambiguous": ("bad", "More than one person has that username. Use their user ID."),
    "announced": ("ok", "Announcement sent. It waits for anyone who is offline until they next open the page."),
    "saved": ("ok", "Saved."),
    "support_saved": ("ok", "Support server link saved."),
    "invite_saved": ("ok", "Invite link saved. Copy the promo link from the bot\'s Invite link section."),
    "key_removed": ("ok", "API key removed. It stops working right away."),
    "login_needed": ("bad", "Log in to open the panel."),
    "bot_denied": ("ok", "Bot denied. Its owner can see your reason in the panel, and the bot is told the next time it asks."),
    "reason_needed": ("bad", "Write a reason for denying it: the bot's owner sees it."),
    "revoked": ("ok", "Access removed. The bot will be told on its next report."),
    "owner": ("ok", "Owner changed."),
    "role_added": ("ok", "Role saved."),
    "role_removed": ("ok", "Role removed."),
    "denied": ("bad", "Your role can't do that."),
    "invalid": ("bad", "That didn't look right. Check the IDs and try again."),
    "failed": ("bad", "That didn't work."),
    "csrf": ("bad", "The page was out of date. Try again."),
    "login_failed": ("bad", "Login didn't work. Try again."),
    "login_off": ("bad", "Login with Yapin' isn't set up on this server yet."),
}

CSS = """
:root{color-scheme:dark;--bg:#0e1220;--surface:#151a2c;--raised:#1c2340;--line:#283054;--text:#e9ecf6;--muted:#9aa3c0;
--accent:#a393ff;--accent-ink:#14102e;--ok:#62d8a8;--wait:#f3b95f;--bad:#f2808f;
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 var(--sans)}
a{color:var(--accent)}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:64rem;margin:0 auto;padding:0 1.25rem}
header.top{border-bottom:1px solid var(--line)}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;gap:1rem;min-height:3.5rem;flex-wrap:wrap}
.brand{font:600 1.05rem var(--serif);color:var(--text);text-decoration:none}
.who{display:flex;gap:.75rem;align-items:center;color:var(--muted);font-size:.9rem}
.who form{margin:0}
h1,h2,h3{font-family:var(--serif);font-weight:600;line-height:1.2;margin:0}
h1{font-size:clamp(1.9rem,4vw,2.7rem);max-width:22ch}
h2{font-size:1.45rem;margin-bottom:.6rem}
h3{font-size:1.1rem}
p{margin:.5rem 0;max-width:68ch}
.muted{color:var(--muted)}
.small{font-size:.88rem}
main{padding:2.5rem 0 4rem}
section{margin-top:2.75rem}
.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:2.5rem;align-items:start}
@media(max-width:52rem){.hero{grid-template-columns:1fr}}
.lede{font-size:1.1rem;color:var(--muted)}
pre,code{font-family:var(--mono)}
pre{background:var(--surface);border:1px solid var(--line);border-radius:.6rem;padding:1rem;overflow:auto;font-size:.8rem;line-height:1.5;margin:0}
code{background:var(--raised);padding:.1rem .35rem;border-radius:.3rem;font-size:.88em}
pre code{background:none;padding:0}
.btn,button{font:600 .95rem var(--sans);border-radius:.5rem;border:1px solid var(--line);background:var(--raised);color:var(--text);padding:.5rem .95rem;cursor:pointer;text-decoration:none;display:inline-block}
.btn:hover,button:hover{border-color:var(--accent)}
.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.danger{color:var(--bad)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:2rem}
@media(max-width:44rem){.two{grid-template-columns:1fr}}
ul.plain{padding-left:1.1rem;margin:.4rem 0}
ul.plain li{margin:.25rem 0}
ol.steps{padding-left:1.2rem}
ol.steps li{margin:.7rem 0;padding-left:.3rem;max-width:68ch}
.tabs{display:flex;gap:.25rem;border-bottom:1px solid var(--line);margin-bottom:1.5rem;flex-wrap:wrap}
.tabs a{padding:.6rem 1rem;color:var(--muted);text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a[aria-current=page]{color:var(--text);border-color:var(--accent)}
.flash{padding:.7rem 1rem;border-radius:.5rem;border:1px solid var(--line);background:var(--surface);margin-bottom:1.25rem}
.flash.ok{border-color:var(--ok)}.flash.bad{border-color:var(--bad)}
.row{display:grid;grid-template-columns:.3rem minmax(0,1fr);background:var(--surface);border:1px solid var(--line);border-radius:.6rem;margin-bottom:.9rem;overflow:hidden}
.row>.edge{background:var(--line)}
.row.active>.edge,.row.approved>.edge{background:var(--ok)}.row.pending>.edge,.row.waiting>.edge{background:var(--wait)}
.row.quiet>.edge{background:var(--wait)}.row.removed>.edge,.row.denied>.edge{background:var(--bad)}
.row .in{padding:1rem 1.15rem}
.head{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;align-items:baseline}
.status{font-size:.9rem;display:inline-flex;gap:.45rem;align-items:center}
.dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--muted);display:inline-block}
.active .dot{background:var(--ok);animation:pulse 2.4s ease-in-out infinite}
.pending .dot,.waiting .dot,.quiet .dot{background:var(--wait)}.removed .dot,.denied .dot{background:var(--bad)}
@keyframes pulse{50%{opacity:.35}}
@media(prefers-reduced-motion:reduce){.active .dot{animation:none}}
.id{font-family:var(--mono);font-size:.82rem;color:var(--muted)}
.track{margin:.9rem 0 .2rem}
.track svg{display:block;width:100%;height:1.1rem}
.track .labels{display:flex;justify-content:space-between;font-size:.82rem;color:var(--muted);margin-top:.15rem;gap:1rem;flex-wrap:wrap}
dl.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.8rem 1.5rem;margin:1rem 0 0}
dl.facts dt{font-size:.82rem;color:var(--muted)}
dl.facts dd{margin:0;font-size:1.05rem}
form.inline{display:flex;gap:.5rem;flex-wrap:wrap;align-items:end;margin:.6rem 0 0}
label.check{flex-direction:row;align-items:center;gap:.4rem;padding-bottom:.55rem}
label.check input{min-width:0}
label{display:flex;flex-direction:column;gap:.25rem;font-size:.85rem;color:var(--muted)}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:.45rem;padding:.5rem .65rem;font:inherit;min-width:12rem}
details{margin-top:.9rem}
summary{cursor:pointer;color:var(--accent)}
.key{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-top:.9rem}
.key output{font-family:var(--mono);background:var(--bg);border:1px solid var(--line);border-radius:.45rem;padding:.4rem .7rem;min-width:16rem;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.55rem .6rem;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-weight:600;color:var(--muted);font-size:.85rem}
.avatar{width:2rem;height:2rem;border-radius:50%;background:var(--raised);color:var(--accent);display:inline-flex;align-items:center;justify-content:center;font:600 .9rem var(--serif);object-fit:cover;flex:none}
.who-is{display:inline-flex;gap:.5rem;align-items:center}.who-is .avatar{width:1.5rem;height:1.5rem;font-size:.75rem}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(17rem,1fr));gap:.9rem;margin-bottom:.9rem}
.card{display:flex;flex-direction:column;gap:.35rem;background:var(--surface);border:1px solid var(--line);border-radius:.6rem;padding:1rem 1.1rem;color:var(--text);text-decoration:none}
.card:hover,.card:focus-visible{border-color:var(--accent)}
.card h3{font-size:1.05rem}.card .desc{margin:0;color:var(--muted);font-size:.9rem;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .small{margin:auto 0 0}.card.active .dot{background:var(--ok)}.card.waiting .dot,.card.quiet .dot{background:var(--wait)}
.botname{display:flex;gap:.7rem;align-items:center}.botname .avatar{width:2.4rem;height:2.4rem}
.suggest{position:absolute;z-index:5;background:var(--raised);border:1px solid var(--line);border-radius:.5rem;margin-top:.2rem;min-width:16rem;overflow:hidden}
.suggest button{display:flex;width:100%;gap:.5rem;border:0;border-radius:0;background:none;text-align:left;align-items:center}
.suggest button:hover{background:var(--surface)}
.field{position:relative}
.banner{border:1px solid var(--accent);background:var(--surface);border-radius:.6rem;padding:1rem 1.15rem;margin-bottom:1rem}
.banner h3{margin-bottom:.3rem}.banner form{margin-top:.6rem}
.spark{display:grid;grid-template-columns:repeat(auto-fit,minmax(12rem,1fr));gap:1rem;margin-top:1rem}
.spark svg{width:100%;height:3.2rem;display:block}.spark .label{font-size:.82rem;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));gap:1rem;margin:1rem 0}
.stat{border-left:3px solid var(--line);padding-left:.8rem}.stat b{display:block;font:600 1.6rem var(--serif)}.stat span{color:var(--muted);font-size:.85rem}
textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:.45rem;padding:.5rem .65rem;font:inherit;min-width:min(30rem,100%);min-height:6rem}
.newkey{font-family:var(--mono);background:var(--bg);border:1px solid var(--ok);border-radius:.45rem;padding:.6rem .8rem;overflow-wrap:anywhere;display:block;margin:.5rem 0}
.empty{border:1px dashed var(--line);border-radius:.6rem;padding:1.5rem;color:var(--muted)}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:.85rem;padding:1.5rem 0}
"""

JS = """
document.addEventListener("error", function (e) {      // a bot picture that can't load becomes its first letter
  var img = e.target;
  if (img && img.tagName === "IMG" && img.dataset.letter) {
    var span = document.createElement("span"); span.className = "avatar"; span.textContent = img.dataset.letter; img.replaceWith(span);
  }
}, true);
function localTimes(root) {
  for (const el of root.querySelectorAll('time[datetime]')) {
    const d = new Date(el.getAttribute('datetime'));
    if (!isNaN(d)) el.textContent = d.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
  }
}
localTimes(document);
for (const box of document.querySelectorAll('[data-live]')) {
  const every = Math.max(10, Number(box.dataset.every) || 30) * 1000;
  let failures = 0, busy = false;
  const refresh = async () => {
    if (!document.hidden && !busy) {
    busy = true;
    try {
      const open = Array.from(box.querySelectorAll('details')).map(d => d.open);
      const r = await fetch(box.dataset.live, {credentials: 'same-origin', headers: {'Accept': 'text/html'}});
      if (!r.ok) throw new Error(r.status);
      const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
      const fresh = doc.querySelector('.live');
      if (fresh) {
        box.replaceChildren(...Array.from(fresh.childNodes).map(n => document.importNode(n, true)));
        Array.from(box.querySelectorAll('details')).forEach((d, i) => { d.open = !!open[i]; });
        localTimes(box);
      }
      failures = 0;
    } catch (e) { failures += 1; }
    busy = false;
    }
    setTimeout(refresh, every * Math.min(1 + failures, 6));
  };
  setTimeout(refresh, every);
}
for (const input of document.querySelectorAll('input[data-people]')) {
  const wrap = input.parentElement; wrap.classList.add('field');
  const box = document.createElement('div'); box.className = 'suggest'; box.hidden = true; wrap.appendChild(box);
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) { box.hidden = true; return; }
      try {
        const r = await fetch('/panel/search?q=' + encodeURIComponent(q), {credentials: 'same-origin'});
        const list = r.ok ? await r.json() : [];
        box.textContent = '';
        for (const u of list) {
          const b = document.createElement('button'); b.type = 'button';
          const name = document.createElement('span'); name.textContent = (u.username || 'User') + ' ';
          const id = document.createElement('span'); id.className = 'id'; id.textContent = u.user_id;
          b.append(name, id);
          b.addEventListener('click', () => { input.value = u.user_id; box.hidden = true; });
          box.appendChild(b);
        }
        box.hidden = list.length === 0;
      } catch (e) { box.hidden = true; }
    }, 200);
  });
}
for (const box of document.querySelectorAll('[data-key-for]')) {
  const out = box.querySelector('output'), btn = box.querySelector('button');
  const csrf = document.body.dataset.csrf;
  let shown = false;
  btn.addEventListener('click', async () => {
    if (shown) { out.textContent = '\\u2022'.repeat(24); btn.textContent = 'Show key'; shown = false; return; }
    btn.disabled = true;
    try {
      const r = await fetch('/panel/key', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({bot_id: box.dataset.keyFor})});
      const j = await r.json();
      if (!r.ok || !j.key) throw new Error();
      out.textContent = j.key; btn.textContent = 'Hide key'; shown = true;
    } catch (e) { out.textContent = 'Could not load the key. Reload the page and try again.'; }
    btn.disabled = false;
  });
}
"""


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def ago(seconds):
    seconds = int(abs(seconds))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return "under a minute"


class Context:
    """Per-request facts the page helpers need: who is looking (timezone, role), the store, and which image hosts are allowed."""

    def __init__(self, store=None, tz="auto", avatar_hosts=(), staff=False, base=""):
        self.store, self.tz, self.avatar_hosts, self.staff, self.base = store, tz, frozenset(avatar_hosts), staff, base


_ctx = contextvars.ContextVar("panel_ctx", default=Context())


def set_context(ctx):
    _ctx.set(ctx)


_ZONES = None


def timezones():
    global _ZONES
    if _ZONES is None:
        _ZONES = sorted(zoneinfo.available_timezones() - {"UTC"})
    return _ZONES


def when(ts):
    """A time in the viewer's timezone. Stored times are UTC. For UTC nothing is converted; for a chosen zone the server
    converts; for "auto" the browser swaps in local time (UTC text until then)."""
    if not ts:
        return "never"
    tz = _ctx.get().tz
    utc = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
    if tz == "UTC":
        return f'<span class="time">{utc}</span>'
    if tz != "auto":
        try:
            from datetime import datetime, timezone as _tz
            local = datetime.fromtimestamp(ts, _tz.utc).astimezone(zoneinfo.ZoneInfo(tz))
            return f'<span class="time" title="{utc}">{local:%Y-%m-%d %H:%M} {esc(local.tzname())}</span>'
        except Exception:
            pass
    return f'<time datetime="{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))}">{utc}</time>'


def avatar(url, name, size=32, bot_id=None):
    """A profile picture, only from allowed https hosts (so a bot can't make staff browsers fetch from anywhere);
    otherwise the first letter of the name."""
    host = urllib.parse.urlparse(url or "").hostname if url and str(url).startswith("https://") else None
    letter = esc((str(name or "?").strip() or "?")[0].upper())
    if bot_id and host:        # bot pictures come through this server (/avatar/<id>.png): it fetches and shrinks them
        return (f'<img class="avatar" src="/avatar/{esc(urllib.parse.quote(bot_id, safe=""))}.png" alt="" width="{size}" height="{size}" loading="lazy" data-letter="{letter}">')
    if host and host in _ctx.get().avatar_hosts:
        return f'<img class="avatar" src="{esc(url)}" alt="" width="{size}" height="{size}" loading="lazy" referrerpolicy="no-referrer">'
    return f'<span class="avatar" aria-hidden="true">{letter}</span>'


def person(user_id, store=None):
    """Avatar, username and ID of someone, from what Yapin' told us when they logged in."""
    store = store or _ctx.get().store
    user = store.get_user(user_id) if store else None
    if user:
        name = user.get("display") or user.get("username") or "User"
        return f'<span class="who-is">{avatar(user.get("avatar"), name, 24)}<span>{esc(name)} <span class="id">{esc(user_id)}</span></span></span>'
    return f'<span class="who-is">{avatar(None, user_id, 24)}<span class="id">{esc(user_id)}</span></span>'


def page(title, body, nonce, user=None, csrf="", login_url=None, banners=""):
    """Every page: the same header (Panel and Settings when logged in, Log in otherwise), announcements, then the body."""
    who = ""
    if user:
        record = _ctx.get().store.get_user(user.user_id) if _ctx.get().store else None
        name = (record or {}).get("display") or (record or {}).get("username") or user.username or "User"
        who = (f'<div class="who"><a class="btn primary" href="/panel">Panel</a><a href="/settings" class="who-is">'
               f'{avatar((record or {}).get("avatar"), name, 24)}<span>{esc(name)}</span></a>'
               f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}">'
               f'<button type="submit">Log out</button></form></div>')
    elif login_url:
        who = f'<div class="who"><a class="btn primary" href="{esc(login_url)}">Log in with Yapin\'</a></div>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="dark"><meta name="robots" content="noindex"><title>{esc(title)}</title>'
            f'<style nonce="{nonce}">{CSS}</style></head><body data-csrf="{esc(csrf)}">'
            f'<header class="top"><div class="wrap"><a class="brand" href="/">yapper.py telemetry</a>{who}</div></header>'
            f'<main><div class="wrap">{banners}{body}</div></main>'
            f'<footer><div class="wrap">Opt-in telemetry for <a href="https://github.com/our-websites/yapper.py">yapper.py</a> bots. '
            f'Nothing is sent unless a bot owner turns it on.</div></footer>'
            f'<script nonce="{nonce}">{JS}</script></body></html>').encode()


def banners(announcements, csrf, back="/panel"):
    """Announcements nobody has dismissed yet. They stay until the person presses Dismiss."""
    if not announcements:
        return ""
    top = max(a["id"] for a in announcements)
    items = "".join(f'<div class="banner"><h3>{esc(a["title"])}</h3><p>{esc(a["body"])}</p>'
                    f'<p class="muted small">{"For everyone" if a["target"] == "all" else "Just for you"}, {when(a["created_at"])}</p></div>' for a in announcements)
    return (f'{items}<form method="post" action="/panel/dismiss" class="inline"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="upto" value="{top}"><input type="hidden" name="back" value="{esc(back)}">'
            f'<button type="submit">Dismiss {"all" if len(announcements) > 1 else ""}</button></form><br>')


def flash(code):
    kind, text = MESSAGES.get(code, (None, None))
    return f'<div class="flash {kind}" role="status">{esc(text)}</div>' if text else ""


# ---- the landing page ----

def landing(nonce, support_url, login_url, message=None, user=None, csrf="", banner_html=""):
    example = esc(json.dumps(EXAMPLE_REPORT, indent=2))
    cta = ('<a class="btn primary" href="/panel">Open the panel</a>' if user else
           f'<a class="btn primary" href="{esc(login_url)}">Log in with Yapin\'</a>' if login_url
           else '<span class="muted small">Login is not set up on this server yet.</span>')
    body = f"""
{flash(message)}
<div class="hero">
  <div>
    <h1>A small, optional report from yapper.py bots</h1>
    <p class="lede">If you turn telemetry on in your bot, it sends a short JSON report about itself to this server
    every 30 minutes. Normal installs send nothing: it only runs when a bot owner loads it on purpose.</p>
    <p>{cta} <a class="btn" href="{esc(support_url)}">Join the support server</a></p>
  </div>
  <div>
    <p class="muted small">An example report, exactly the shape a bot sends:</p>
    <pre><code>{example}</code></pre>
  </div>
</div>

<section class="two">
  <div>
    <h2>What is sent</h2>
    <ul class="plain">
      <li>The bot's ID, name and profile picture link</li>
      <li>Command names, descriptions and options; cog and extension names</li>
      <li>How many listeners and buttons it has</li>
      <li>How many servers it is in, with total and online member counts</li>
      <li>The yapper.py, Python and OS versions, and how long the bot has been up</li>
      <li>Anything extra the bot's owner chooses to add themselves (<code>telemetry.add_data</code>)</li>
    </ul>
  </div>
  <div>
    <h2>What is never sent</h2>
    <ul class="plain">
      <li>The bot token</li>
      <li>Messages or anything people wrote</li>
      <li>User, server or channel names and IDs</li>
      <li>IP addresses are not saved</li>
    </ul>
    <p class="muted small">Bots can see their own report any time with <code>telemetry.preview(bot)</code>.</p>
  </div>
</section>

<section>
  <h2>How the data is used</h2>
  <p>Reports show which bots really use yapper.py and what they do with it. We use that to pick bots to feature in the
  support server and in the yapper.py README, and to decide what to improve in the library. Bot owners can see their own
  data here, and only the people who approve bots can see which bots exist. Only a bot's owner can see its private key.</p>
</section>

<section>
  <h2>Getting a bot on the list</h2>
  <ol class="steps">
    <li>Load telemetry in your bot: <code>from yapper import telemetry</code> then <code>telemetry.enable(bot)</code>.
    Add <code>TELEMETRY_OWNER_ID=your Yapin' user ID</code> to your <code>.env</code> to follow your bot's status here before it is approved.</li>
    <li>Ask for approval in the <a href="{esc(support_url)}">support server</a>. Bring your bot's ID and proof that yapper.py is your main SDK.</li>
    <li>Once approved, log in here, open your bot and copy its private key into <code>TELEMETRY_KEY</code> in your <code>.env</code>, then restart the bot.</li>
  </ol>
</section>"""
    return page("yapper.py telemetry", body, nonce, user, csrf, None if user else login_url, banner_html)


# ---- the panel ----

def track(bot, now):
    """The signature element: a line from the last report to the next expected one, filled as time passes."""
    code, _ = bot_state(bot, now)
    if code in ("pending", "removed"):
        return ""
    last, interval = bot["last_report_at"], bot["interval"]
    if not last:
        return f'<div class="track muted small">First report expected within {ago(interval)} of the bot starting with its key set.</div>'
    due = last + interval
    frac = max(0.0, min(1.0, (now - last) / interval))
    overdue = now > due
    x = round(4 + 292 * frac, 1)
    color = "var(--wait)" if overdue else "var(--ok)"
    nxt = f"{ago(now - due)} overdue" if overdue else f"in {ago(due - now)}"
    return (f'<div class="track"><svg viewBox="0 0 300 12" preserveAspectRatio="none" aria-hidden="true">'
            f'<line x1="4" y1="6" x2="296" y2="6" stroke="var(--line)" stroke-width="2"/>'
            f'<line x1="4" y1="6" x2="{x}" y2="6" stroke="{color}" stroke-width="2"/>'
            f'<circle cx="4" cy="6" r="3" fill="{color}"/><circle cx="296" cy="6" r="3" fill="none" stroke="var(--muted)" stroke-width="1.5"/>'
            f'</svg><div class="labels"><span>Last report {when(last)}</span><span>Next expected {when(due)} ({nxt})</span></div></div>')


def _facts(report):
    if not report:
        return ""
    servers, bot, sdk = report.get("servers") or {}, report.get("bot") or {}, report.get("sdk") or {}
    up = bot.get("uptime_seconds")
    items = [("Servers", servers.get("count")), ("Members", servers.get("members_total")), ("Online", servers.get("online_total")),
             ("Commands", len(report.get("commands") or [])),
             ("SDK version (main.py)", sdk.get("version")), ("PyPI version", sdk.get("package_version")),
             ("Uptime", ago(up) if isinstance(up, (int, float)) else None)]
    return '<dl class="facts">' + "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in items if v is not None) + "</dl>"


def _extra(report):
    extra = (report or {}).get("extra")
    if not isinstance(extra, dict) or not extra:
        return ""
    rows = "".join(f"<tr><td><code>{esc(k)}</code></td><td>{esc(json.dumps(v) if not isinstance(v, str) else v)[:400]}</td></tr>" for k, v in list(extra.items())[:50])
    return f"<details open><summary>Your extra data ({len(extra)})</summary><table><tbody>{rows}</tbody></table></details>"


def spark(points, label):
    """A small line of how a number changed over time. Points are (time, value); drawn with SVG attributes only."""
    pts = [(t, v) for t, v in points if isinstance(v, (int, float))]
    if len(pts) < 2:
        return ""
    t0, t1 = pts[0][0], pts[-1][0]
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span_t, span_v = max(t1 - t0, 1), max(hi - lo, 1)
    coords = " ".join(f"{4 + 292 * (t - t0) / span_t:.1f},{34 - 30 * (v - lo) / span_v:.1f}" for t, v in pts[-300:])
    first, last = pts[0][1], pts[-1][1]
    diff = last - first
    return (f'<div><div class="label">{esc(label)}: {first:,} to {last:,} ({"+" if diff >= 0 else ""}{diff:,}) since {when(t0)}</div>'
            f'<svg viewBox="0 0 300 38" preserveAspectRatio="none" aria-hidden="true"><polyline points="{coords}" fill="none" stroke="var(--accent)" stroke-width="2"/></svg></div>')


def growth(bot):
    samples = bot.get("samples") or []
    charts = "".join(spark([(r[0], r[i]) for r in samples], label) for i, label in ((1, "Servers"), (2, "Members"), (3, "Online"), (4, "Commands")))
    return f'<div class="spark">{charts}</div>' if charts else '<p class="muted small">Growth shows here once there are a couple of hours of reports.</p>'


EVENT_TEXT = {
    "bot.approve": "approved", "bot.deny": "denied", "bot.revoke": "removed access for", "bot.owner": "switched the owner of", "bot.apply": "applied for",
    "bot.invite": "set the invite link of", "bot.support": "set the support server of", "bot.share": "gave access to", "bot.unshare": "removed access from", "bot.view": "viewed", "key.reveal": "showed the private key of",
    "key.rotate": "made a new private key for", "role.add": "gave a role to", "role.remove": "removed the role of", "announce.send": "sent an announcement to",
    "page.view": "opened the", "apikey.create": "made an API key", "apikey.remove": "removed an API key", "api.read": "read data through the API for",
    "settings.timezone": "changed their timezone to",
}


def describe(event, show_bot=True):
    who = person(event["actor"])
    text = EVENT_TEXT.get(event["kind"], event["kind"])
    target = f' <b>{person(event["target"])}</b>' if event.get("target") and event["kind"] not in ("announce.send",) else ""
    if event["kind"] == "announce.send":
        target = " everyone" if event.get("target") == "all" else f' {person(event["target"])}'
    bot = ""
    if event.get("bot") and show_bot:
        bot = f' <b>{esc(_ctx.get().store.bot_label(event["bot"]) if _ctx.get().store else event["bot"])}</b> <span class="id">{esc(event["bot"])}</span>'
    detail = f' <span class="muted">{esc(event["detail"])}</span>' if event.get("detail") and event["kind"] != "announce.send" else ""
    if event["kind"] == "page.view":
        return f"{who} opened the {esc(event.get('detail') or 'panel')} tab"
    return f"{who} {esc(text)}{bot}{target}{detail}"


def activity_table(events, show_bot=True):
    if not events:
        return '<p class="muted small">Nothing has happened yet.</p>'
    rows = "".join(f'<tr><td class="small">{when(e["t"])}</td><td>{describe(e, show_bot)}</td></tr>' for e in events)
    return f"<table><tbody>{rows}</tbody></table>"


def _commands(report):
    cmds = (report or {}).get("commands") or []
    if not cmds:
        return ""
    rows = "".join(f"<tr><td><code>/{esc(c.get('name'))}</code></td><td>{esc(c.get('description'))}</td></tr>" for c in cmds[:100] if isinstance(c, dict))
    return f"<details><summary>Commands ({len(cmds)})</summary><table><tbody>{rows}</tbody></table></details>"


def _pretty(name):
    """Usernames end in _0220 (their number): show it as #0220."""
    return re.sub(r"_(\d{4})$", r"#\1", str(name or "").strip())


def bot_title(bot):
    return _pretty(bot.get("name")) or bot["bot_id"]


def bot_live(bot, now, level="owner", link=True, poll=False):
    """The part of a bot's card that changes as reports come in (status, numbers, growth, latest report). The bot page refreshes just this."""
    code, label = bot_state(bot, now)
    name = bot["name"] or "Unnamed bot"
    report = bot.get("last_report")
    title = f'<a href="/bots/{esc(bot["bot_id"])}">{esc(name)}</a>' if link and bot["status"] == "approved" else esc(name)
    parts = [f'<div class="head"><div class="botname">{avatar(bot.get("avatar"), name, 38, bot["bot_id"])}<h3>{title}</h3></div><span class="status"><span class="dot"></span>{esc(label)}</span></div>',
             f'<div class="id">{esc(bot["bot_id"])}</div>']
    if bot.get("description") and code not in ("pending", "denied", "removed"):
        parts.append(f'<p class="desc">{esc(bot["description"])}</p>')
    if bot.get("support_url") and code not in ("pending", "denied", "removed"):
        parts.append(f'<p class="small"><a href="{esc(bot["support_url"])}" rel="noopener">Support server</a></p>')
    if level in ("manager", "viewer") and code not in ("pending", "denied"):
        parts.append(f'<div class="muted small">Shared with you as {"a manager" if level == "manager" else "a viewer"} (owner <span class="id">{esc(bot["owner_id"])}</span>)</div>')
    if code == "pending":
        parts.append('<p class="muted">The bot has asked to send telemetry. It is waiting for someone on the team to approve it. '
                     'Nothing is shared until then, and you will see its data and private key here once it is.</p>')
    elif code == "denied":
        parts.append(f'<div class="banner"><h3>Denied</h3><p>{esc(bot.get("deny_reason"))}</p>'
                     '<p class="muted small">If you have fixed that, ask in the support server and mention the bot\'s ID. The team can approve it again.</p></div>')
    elif code == "removed":
        parts.append('<p class="muted">Telemetry access for this bot was removed. Ask in the support server if you think that is a mistake.</p>')
    else:
        parts.append(track(bot, now))
        if code == "waiting":
            parts.append('<p class="muted">Set the private key below as <code>TELEMETRY_KEY</code> in the bot\'s environment or <code>.env</code> and restart it.</p>')
        parts += [_facts(report), growth(bot), _extra(report), _commands(report)]
        if report:
            parts.append(f"<details><summary>Latest report</summary><pre><code>{esc(json.dumps(report, indent=2))}</code></pre></details>")
    attrs = f' data-live="/bots/{esc(bot["bot_id"])}/live" data-every="30"' if poll else ""
    return f'<div class="live"{attrs}>{"".join(parts)}</div>'


def bot_card(bot, now):
    """A small card for the bot lists: picture, name, description and when it last reported. The whole card opens the bot's page, where the stats are."""
    code, label = bot_state(bot, now)
    name = bot["name"] or "Unnamed bot"
    desc = f'<p class="desc">{esc(bot["description"])}</p>' if bot.get("description") else ""
    last = f'Last report {when(bot["last_report_at"])}' if bot.get("last_report_at") else "No report yet"
    return (f'<a class="card {code}" href="/bots/{esc(bot["bot_id"])}"><div class="botname">{avatar(bot.get("avatar"), name, 38, bot["bot_id"])}'
            f'<h3>{esc(name)}</h3></div>{desc}<p class="muted small"><span class="dot"></span> {esc(last)}</p></a>')


def owner_row(bot, csrf, now, key_box=True, claimed=False, level="owner", members=(), poll=False):
    code, _ = bot_state(bot, now)
    parts = [bot_live(bot, now, level, link=not poll, poll=poll)]
    if code not in ("pending", "removed", "denied"):
        if key_box and bot.get("key") and level in ("owner", "manager"):
            parts.append(f'<div class="key" data-key-for="{esc(bot["bot_id"])}"><span class="muted small">Private key</span>'
                         f'<output aria-live="polite">{"&bull;" * 24}</output><button type="button">Show key</button></div>'
                         f'<form class="inline" method="post" action="/panel/rotate-key"><input type="hidden" name="csrf" value="{esc(csrf)}">'
                         f'<input type="hidden" name="bot_id" value="{esc(bot["bot_id"])}">'
                         f'<button type="submit">Make a new key</button> <span class="muted small">The old key stops working right away.</span></form>')
    if code not in ("pending", "denied") and bot["status"] == "approved" and level in ("owner", "manager"):
        parts.append(invite_section(bot, csrf))
        parts.append(access_section(bot, csrf, level, members))
    if code not in ("pending", "removed", "denied") and _ctx.get().store:
        events = _ctx.get().store.read_audit(("approver", "manager"), bot_id=bot["bot_id"], limit=15)
        parts.append(f'<details><summary>Activity on this bot</summary>{activity_table(events, show_bot=False)}</details>')
    return f'<div class="row {code}"><div class="edge"></div><div class="in">{"".join(parts)}</div></div>'


def bot_page(bot, session_csrf, now, level, members):
    """/bots/<id>: one bot's data, for its owner, managers and viewers. The live part refreshes by itself."""
    row = owner_row(bot, session_csrf, now, level=level, members=members, poll=True)
    return (f'<p class="small"><a href="/panel/bots">&larr; My bots</a></p>{row}'
            '<p class="muted small" data-poll-note>This page refreshes by itself every 30 seconds while it is open.</p>')


def staff_bot_page(bot, role, csrf, now, store):
    """/panel/<bot id>: what the team sees about a bot: status, who owns it, and its report data (stats and the latest report), never its key."""
    levels = ("approver", "manager") if role in ("owner", "manager") else ("approver",)
    events = store.read_audit(levels, bot_id=bot["bot_id"], limit=30)
    promo = promo_line(bot)
    live = badge_line(bot)
    live_box = (f'<p class="muted small">Or a live badge that updates its own server count, so the README never needs editing (the bot also needs its invite link saved):</p>'
                f'<textarea readonly rows="2" aria-label="Live badge">{esc(live)}</textarea>') if live else ""
    box = (f'<section><h3>README link</h3><p class="muted small">Markdown with the bot\'s icon and name, as one link that opens the bot to add it.</p>'
           f'<textarea readonly rows="3" aria-label="README link">{esc(promo)}</textarea>{live_box}</section>') if promo else \
          '<section><h3>README link</h3><p class="muted small">There is no invite link for this bot yet. Its owner can save one on their bot page.</p></section>'
    data = ""
    if bot["status"] == "approved":
        data = f'<section><h3>Report data</h3>{bot_live(bot, now, "staff", link=False)}</section>'
    return (f'<p class="small"><a href="/panel/approvals">&larr; Approvals</a></p>{staff_row(bot, role, csrf, now)}{data}{box}'
            f'<section><h3>Activity on this bot</h3>{activity_table(events, show_bot=False)}</section>')


UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def invite_for(bot):
    """The link that adds the bot: the one saved for it, else https://yapin.gg/bot/<id> when its ID is a Yapin' bot ID (a UUID),
    the same shape shapes-bot's /add uses."""
    if bot.get("invite_url"):
        return bot["invite_url"]
    return f"https://yapin.gg/bot/{bot['bot_id']}" if UUID.match(str(bot["bot_id"])) else ""


def promo_line(bot):
    """The README line for a bot, in Markdown: one link whose text is the bot's icon and name, so pressing either opens the bot to add it.
    The icon is an <img> inside the link text (GitHub allows that, and it keeps the icon small)."""
    url = invite_for(bot)
    if not url:
        return ""
    name = (_pretty(bot.get("name")) or bot["bot_id"]).replace("\\", "\\\\")
    for ch in "[]*_`<>&|":
        name = name.replace(ch, "\\" + ch)
    name = " ".join(name.split())
    icon = bot.get("avatar")
    img = f'<img src="{html.escape(icon, quote=True)}" width="24" height="24" align="absmiddle"> ' if isinstance(icon, str) and icon.startswith("https://") else ""
    return f"[{img}**{name}**]({url.replace(' ', '%20').replace(')', '%29').replace('(', '%28')})"


def badge_line(bot):
    """The README line that never needs editing: a live badge (drawn by this server from the current numbers) linked to the invite."""
    url, base = invite_for(bot), _ctx.get().base
    if not url or not base or bot.get("status") != "approved":
        return ""
    name = " ".join((_pretty(bot.get("name")) or bot["bot_id"]).split())
    for ch in "\\[]":
        name = name.replace(ch, "\\" + ch)
    return f"[![{name}]({base}/badge/{urllib.parse.quote(bot['bot_id'], safe='')}.svg)]({url.replace(' ', '%20').replace(')', '%29').replace('(', '%28')})"


def invite_section(bot, csrf):
    """Owners and managers save the bot's yapin.gg link here. (The ready-to-paste README link is made for the team, in the staff panel.)"""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="bot_id" value="{esc(bot["bot_id"])}">'
    link = invite_for(bot)
    shown = (f'<p class="small">Invite link: <a href="{esc(link)}" rel="noopener">{esc(link)}</a>'
             f'{"" if bot.get("invite_url") else " <span class=muted>(made from the bot ID; save a link below to use another)</span>"}</p>') if link else \
            '<p class="muted small">Add your bot\'s yapin.gg link (https://yapin.gg/bot/...) so people can add it from lists the team features.</p>'
    return (f'<details><summary>Invite link</summary>{shown}<form class="inline" method="post" action="/panel/invite">{hidden}'
            f'<label>yapin.gg invite link<input name="invite_url" value="{esc(bot.get("invite_url"))}" placeholder="https://www.yapin.gg/invite/yourbot" autocomplete="off"></label>'
            f'<button type="submit">Save</button></form></details>'
            f'<details><summary>Support server</summary><p class="muted small">Shown on your bot\'s page so people can get help. Your bot\'s description is read from its own profile; change it in your bot (set_profile).</p>'
            f'<form class="inline" method="post" action="/panel/support">{hidden}'
            f'<label>yapin.gg invite link<input name="support_url" value="{esc(bot.get("support_url"))}" placeholder="https://www.yapin.gg/invite/yourserver" autocomplete="off"></label>'
            f'<button type="submit">Save</button></form></details>')


def access_section(bot, csrf, level, members):
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="bot_id" value="{esc(bot["bot_id"])}">'
    rows = "".join(
        f'<tr><td class="id">{esc(m["user_id"])}</td><td>{"Manager" if m["level"] == "manager" else "Viewer"}</td><td>'
        + (f'<form method="post" action="/panel/unshare" class="inline">{hidden}<input type="hidden" name="user_id" value="{esc(m["user_id"])}">'
           f'<button class="danger" type="submit">Remove</button></form>' if level == "owner" or m["level"] == "viewer" else "")
        + "</td></tr>" for m in members)
    options = '<option value="viewer">Viewer: can see the data</option>' + ('<option value="manager">Manager: also the key, and can add viewers</option>' if level == "owner" else "")
    table = f'<table><tbody>{rows}</tbody></table>' if rows else '<p class="muted small">Not shared with anyone.</p>'
    return (f'<details><summary>Who can see this bot ({len(members)})</summary>{table}'
            f'<form class="inline" method="post" action="/panel/share">{hidden}<label>Username or user ID<input name="user_id" required autocomplete="off"></label>'
            f'<label>Access<select name="level">{options}</select></label><button type="submit">Add</button></form></details>')


def staff_row(bot, role, csrf, now):
    code, label = bot_state(bot, now)
    name = bot["name"] or "Unnamed bot"
    meta = [f'<span class="id">{esc(bot["bot_id"])}</span>']
    if bot["status"] == "pending":
        if bot.get("applicant_id"):
            meta.append(f'<span class="muted small">applied for by <span class="id">{esc(bot["applicant_id"])}</span> (logged in as them)</span>')
        if bot["claimed_owner_id"] and bot["claimed_owner_id"] != bot.get("applicant_id"):
            meta.append(f'<span class="muted small">bot says its owner is <span class="id">{esc(bot["claimed_owner_id"])}</span> (not verified)</span>')
        meta.append(f'<span class="muted small">first seen {when(bot["first_seen"])}</span>')
    elif bot["status"] == "denied":
        meta.append(f'<span class="muted small">denied by <span class="id">{esc(bot["denied_by"])}</span> {when(bot["denied_at"])}</span>')
        meta.append(f'<span class="muted small">reason: {esc(bot["deny_reason"])}</span>')
    else:
        meta.append(f'<span class="muted small">owner <span class="id">{esc(bot["owner_id"] or "not set")}</span></span>')
        meta.append(f'<span class="muted small">last report {when(bot["last_report_at"])}</span>')
    forms = ""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="bot_id" value="{esc(bot["bot_id"])}">'
    if bot["status"] == "pending" and can_approve(role):
        forms = (f'<form class="inline" method="post" action="/panel/approve">{hidden}'
                 f'<label>Owner (username or user ID)<input data-people name="owner_id" required value="{esc(bot.get("applicant_id") or bot["claimed_owner_id"])}" autocomplete="off"></label>'
                 f'<button class="primary" type="submit">Approve</button></form>'
                 f'<form class="inline" method="post" action="/panel/deny">{hidden}'
                 f'<label>Reason for denying (the owner sees it)<input name="reason" required minlength="5" maxlength="500" size="48" autocomplete="off"></label>'
                 f'<button class="danger" type="submit">Deny</button></form>')
    elif bot["status"] == "approved" and can_change_owner(role):
        forms = (f'<form class="inline" method="post" action="/panel/owner">{hidden}'
                 f'<label>Owner (username or user ID)<input data-people name="owner_id" required value="{esc(bot["owner_id"])}" autocomplete="off"></label>'
                 f'<label class="check"><input type="checkbox" name="new_key" value="1"> Make a new key too</label>'
                 f'<button type="submit">Switch owner</button></form>'
                 f'<p class="muted small">Switching owner is for people who moved to a new account. It also ends any sharing the old owner set up.</p>')
        if can_revoke(role):
            forms += f'<form class="inline" method="post" action="/panel/revoke">{hidden}<button class="danger" type="submit">Remove access</button></form>'
    elif bot["status"] in ("revoked", "denied") and can_approve(role):
        owner = bot["owner_id"] or bot.get("applicant_id") or bot["claimed_owner_id"]
        forms = (f'<form class="inline" method="post" action="/panel/approve">{hidden}'
                 f'<label>Owner (username or user ID)<input data-people name="owner_id" required value="{esc(owner)}" autocomplete="off"></label>'
                 f'<button type="submit">Approve{" again" if bot["status"] == "revoked" else " anyway"}</button></form>')
    return (f'<div class="row {code}"><div class="edge"></div><div class="in"><div class="head"><div class="botname">{avatar(bot.get("avatar"), name, 38, bot["bot_id"])}<h3><a href="/panel/{esc(bot["bot_id"])}">{esc(name)}</a></h3></div>'
            f'<span class="status"><span class="dot"></span>{esc(label)}</span></div><div class="head">{"".join(meta)}</div>{forms}</div></div>')


def approvals_tab(store, role, csrf, now):
    pending, approved, revoked = store.with_status("pending"), store.with_status("approved"), store.with_status("revoked")
    denied = store.with_status("denied")
    out = ["<h2>Waiting for approval</h2>"]
    out += [staff_row(b, role, csrf, now) for b in pending] or ['<div class="empty">No bots are waiting. When a bot asks for a token it shows up here.</div>']
    out.append('<section><h2>Approve by ID</h2>')
    out.append(f'<p class="muted small">For a bot that has not contacted the server yet.</p><form class="inline" method="post" action="/panel/approve">'
               f'<input type="hidden" name="csrf" value="{esc(csrf)}"><label>Bot ID<input name="bot_id" required autocomplete="off"></label>'
               f'<label>Owner (username or user ID)<input data-people name="owner_id" required autocomplete="off"></label>'
               f'<button class="primary" type="submit">Approve</button></form></section>')
    out.append(f'<section><h2>Approved ({len(approved)})</h2>')
    out += [staff_row(b, role, csrf, now) for b in approved] or ['<div class="empty">No bots are approved yet.</div>']
    out.append("</section>")
    promos = [promo_line(b) for b in approved if invite_for(b)]
    if promos:
        out.append('<section><h2>Promo links</h2><p class="muted small">Bots with an invite link, ready to paste into the README or the server.</p>'
                   f'<textarea readonly rows="{min(len(promos), 12)}" aria-label="Promo links">{esc(chr(10).join(promos))}</textarea></section>')
    if denied:
        out.append(f'<section><h2>Denied ({len(denied)})</h2>')
        out += [staff_row(b, role, csrf, now) for b in denied]
        out.append("</section>")
    if revoked:
        out.append(f'<section><h2>Access removed ({len(revoked)})</h2>')
        out += [staff_row(b, role, csrf, now) for b in revoked]
        out.append("</section>")
    return "".join(out)


def team_tab(store, role, user_id, csrf):
    roles = assignable_roles(role)
    rows = []
    for m in store.staff():
        remove = ""
        if can_remove_roles(role) and not m["configured"]:
            remove = (f'<form method="post" action="/panel/remove-role" class="inline"><input type="hidden" name="csrf" value="{esc(csrf)}">'
                      f'<input type="hidden" name="user_id" value="{esc(m["user_id"])}"><button class="danger" type="submit">Remove role</button></form>')
        note = " (you)" if m["user_id"] == user_id else ""
        src = "set in the server settings" if m["configured"] else f'added by <span class="id">{esc(m["added_by"])}</span>'
        rows.append(f'<tr><td class="id">{esc(m["user_id"])}{note}</td><td>{esc(m["role"].capitalize())}</td><td class="muted small">{src}</td><td>{remove}</td></tr>')
    add = ""
    if roles:
        opts = "".join(f'<option value="{r}">{r.capitalize()}</option>' for r in roles)
        add = (f'<form class="inline" method="post" action="/panel/role"><input type="hidden" name="csrf" value="{esc(csrf)}">'
               f'<label>Username or user ID<input data-people name="user_id" required autocomplete="off"></label><label>Role<select name="role">{opts}</select></label>'
               f'<button class="primary" type="submit">Add</button></form>')
    help_text = {"owner": "Owners can add managers and approvers, and remove a role.",
                 "manager": "Managers can add approvers. Approvers can approve bots and nothing else."}.get(role, "")
    return (f'<h2>Team</h2><p class="muted">{esc(help_text)}</p>{add}'
            f'<section><table><thead><tr><th>User ID</th><th>Role</th><th></th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></section>')


def apply_form(csrf):
    return (f'<section><h2>Apply for a bot</h2><p class="muted small">You can have as many bots as you like. Enter the bot\'s ID; '
            f'because you are logged in, we know it is you who asked. Then ask in the support server, with proof that yapper.py is its main SDK.</p>'
            f'<form class="inline" method="post" action="/panel/apply"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<label>Bot ID<input name="bot_id" required autocomplete="off"></label><button class="primary" type="submit">Apply</button></form></section>')


def bots_tab(store, user_id, csrf, now):
    mine = store.owned_by(user_id)
    shared = store.shared_with(user_id)
    waiting = store.waiting_for(user_id)
    if not mine and not waiting and not shared:
        return ('<div class="empty"><h2>No bots with telemetry</h2><p>You don\'t have any bots with telemetry on this account yet. '
                'Turn it on in your bot with <code>telemetry.enable(bot)</code> and set <code>TELEMETRY_OWNER_ID</code> to your user ID '
                'to see its status here before it is approved, or apply for it below.</p></div>' + apply_form(csrf))
    # approved bots are small cards that open the bot's page; the rest (pending, denied, removed) need their message and actions on the list
    cards = [bot_card(b, now) for b in mine + shared if b["status"] == "approved"]
    rows = [owner_row(b, csrf, now) for b in mine + shared if b["status"] != "approved"]
    rows += [owner_row(b, csrf, now, key_box=False, claimed=True) for b in waiting]
    grid = f'<div class="cards">{"".join(cards)}</div>' if cards else ""
    return grid + "".join(rows) + apply_form(csrf)


def people_tab(store, role, query):
    results = store.search_users(query, limit=25) if query else []
    form = ('<form class="inline" method="get" action="/panel/people">'
            f'<label>Username or user ID<input name="q" value="{esc(query)}" autocomplete="off"></label><button class="primary" type="submit">Search</button></form>')
    out = ["<h2>People</h2>", '<p class="muted small">Everyone who has logged in to the panel with Yapin\', as Yapin\' told us. '
           "Anyone you add by username is saved by their user ID.</p>", form]
    if query and not results:
        out.append('<div class="empty">Nobody has logged in with that name or ID. They need to log in to the panel first. '
                   "You can still use their user ID where one is asked for.</div>")
    if results:
        rows = "".join(
            f'<tr><td>{avatar(u.get("avatar"), u.get("display") or u.get("username"), 32)}</td>'
            f'<td>{esc(u.get("display") or u.get("username") or "User")}<div class="muted small">{esc(u.get("username"))}</div></td>'
            f'<td class="id">{esc(u["user_id"])}</td><td>{esc((u.get("role") or "").capitalize() or "")}</td>'
            f'<td>{len(store.bot_ids_owned(u["user_id"]))}</td><td class="small">{when(u.get("last_login"))}</td></tr>' for u in results)
        out.append(f'<table><thead><tr><th></th><th>Name</th><th>User ID</th><th>Role</th><th>Bots</th><th>Last login</th></tr></thead><tbody>{rows}</tbody></table>')
    return "".join(out)


def _bars(values, labels, color):
    top = max(values + [1])
    n = len(values)
    gap, width = 4, (300 - 4 * (n - 1)) / n
    rects = "".join(f'<rect x="{i * (width + gap):.1f}" y="{50 - 46 * v / top:.1f}" width="{width:.1f}" height="{46 * v / top:.1f}" fill="{color}"><title>{esc(labels[i])}: {v}</title></rect>'
                    for i, v in enumerate(values))
    return f'<svg viewBox="0 0 300 52" preserveAspectRatio="none" role="img" aria-label="Bar chart over the last {n} days">{rects}</svg>'


def stats_tab(store):
    st = store.user_stats()
    counts = store.counts()
    tiles = [("People who logged in", st["total"]), ("Active in 24 hours", st["active_24h"]), ("Active in 7 days", st["active_7d"]), ("Active in 30 days", st["active_30d"]),
             ("New in 24 hours", st["new_24h"]), ("New in 7 days", st["new_7d"]), ("New in 30 days", st["new_30d"]), ("Total logins", st["logins"]),
             ("Bot owners", st["bot_owners"]), ("Staff", st["staff"]), ("Approved bots", counts.get("approved", 0)), ("Waiting bots", counts.get("pending", 0))]
    days = [d for d, _, _ in st["series"]]
    recent = "".join(f'<tr><td>{avatar(u.get("avatar"), u.get("display") or u.get("username"), 32)}</td><td>{esc(u.get("display") or u.get("username") or "User")} '
                     f'<span class="id">{esc(u["user_id"])}</span></td><td class="small">{when(u["last_login"])}</td></tr>' for u in st["recent"])
    return ('<h2>Login stats</h2><p class="muted small">From the Yapin\' logins on this panel.</p>'
            + '<div class="stats">' + "".join(f'<div class="stat"><b>{v:,}</b><span>{esc(k)}</span></div>' for k, v in tiles) + "</div>"
            + '<section><h3>Logins per day, last 14 days</h3>' + _bars([l for _, l, _ in st["series"]], days, "var(--accent)") + "</section>"
            + '<section><h3>New people per day, last 14 days</h3>' + _bars([n for _, _, n in st["series"]], days, "var(--ok)") + "</section>"
            + f'<section><h3>Latest logins</h3><table><tbody>{recent}</tbody></table></section>')


def announce_tab(store, csrf):
    sent = store.sent_announcements(30)
    rows = "".join(f'<tr><td class="small">{when(a["created_at"])}</td><td><b>{esc(a["title"])}</b><div class="muted">{esc(a["body"])}</div></td>'
                   f'<td>{"Everyone" if a["target"] == "all" else person(a["target"])}</td><td>{person(a["author"])}</td></tr>' for a in sent)
    form = (f'<form method="post" action="/panel/announce"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            '<label>Send to<select name="audience"><option value="all">Everyone</option><option value="one">One person</option></select></label>'
            '<label>Username or user ID (only for one person)<input data-people name="person" autocomplete="off"></label>'
            '<label>Title<input name="title" maxlength="80" required></label>'
            '<label>Message<textarea name="body" maxlength="1000" rows="4" required></textarea></label>'
            '<button class="primary" type="submit">Send announcement</button></form>')
    return ('<h2>Announcements</h2><p class="muted">Shown at the top of the page when someone opens it. If they are offline, it waits until they do. '
            'Everyone means people who had logged in when you sent it.</p>' + form
            + (f'<section><h3>Sent</h3><table><thead><tr><th>When</th><th>Message</th><th>To</th><th>From</th></tr></thead><tbody>{rows}</tbody></table></section>' if rows else
               '<div class="empty">Nothing sent yet.</div>'))


def activity_tab(store, role):
    levels = ("approver", "manager") if role in ("owner", "manager") else ("approver",)
    events = store.read_audit(levels, limit=200)
    note = ("Everything done in the panel, including who opened which page and which bot's data."
            if role in ("owner", "manager") else "What has been done in the approvals panel.")
    return f'<h2>Activity</h2><p class="muted small">{note} Newest first.</p>{activity_table(events)}'


def settings_page(store, session, nonce, message=None, new_key=None, banner_html=""):
    user = store.get_user(session.user_id) or {}
    tz = user.get("timezone", "auto")
    options = [("auto", "Automatic (your browser's timezone)"), ("UTC", "UTC")] + [(z, z.replace("_", " ")) for z in timezones()]
    select = "".join(f'<option value="{esc(v)}"{" selected" if v == tz else ""}>{esc(t)}</option>' for v, t in options)
    keys = store.api_keys(session.user_id)
    key_rows = "".join(
        f'<tr><td>{esc(k["name"])} <span class="id">{esc(k["prefix"])}...</span></td><td class="small">made {when(k["created_at"])}</td>'
        f'<td class="small">{("used " + when(k["last_used"])) if k["last_used"] else "never used"}</td><td>'
        f'<form method="post" action="/panel/api-key/remove" class="inline"><input type="hidden" name="csrf" value="{esc(session.csrf)}">'
        f'<input type="hidden" name="key_id" value="{k["id"]}"><button class="danger" type="submit">Remove</button></form></td></tr>' for k in keys)
    fresh = (f'<div class="banner"><h3>Your new API key</h3><p>Copy it now: it is shown once and can\'t be looked up later.</p>'
             f'<code class="newkey">{esc(new_key)}</code></div>') if new_key else ""
    body = (flash(message) + fresh +
            '<h2>Settings</h2>'
            f'<section><h3>Your account</h3><p class="who-is">{avatar(user.get("avatar"), user.get("display") or user.get("username"), 32)}'
            f'<span>{esc(user.get("display") or user.get("username") or "User")} <span class="id">{esc(session.user_id)}</span></span></p>'
            '<p class="muted small">From your Yapin\' login. Your user ID is what everything here is saved against.</p></section>'
            '<section><h3>Timezone</h3><p class="muted">Times are stored in UTC and shown in your timezone. With UTC nothing is converted.</p>'
            f'<form class="inline" method="post" action="/panel/settings"><input type="hidden" name="csrf" value="{esc(session.csrf)}">'
            f'<label>Timezone<select name="timezone">{select}</select></label><button class="primary" type="submit">Save</button></form></section>'
            '<section><h3>API keys</h3><p class="muted">Read your bots\' data without writing code. A key can read the bots you own or manage, and nothing else: it can\'t change anything.</p>'
            + (f'<table><tbody>{key_rows}</tbody></table>' if key_rows else '<p class="muted small">No keys yet.</p>')
            + f'<form class="inline" method="post" action="/panel/api-key"><input type="hidden" name="csrf" value="{esc(session.csrf)}">'
              '<label>Name<input name="name" maxlength="40" placeholder="My dashboard"></label><button type="submit">Make a key</button></form>'
              '<details><summary>How to use it</summary><pre><code>curl -H "Authorization: Bearer uk_your_key" https://YOUR-SERVER/api/bots\n'
              'curl -H "Authorization: Bearer uk_your_key" https://YOUR-SERVER/api/bots/BOT_ID\n'
              'curl -H "Authorization: Bearer uk_your_key" https://YOUR-SERVER/api/bots/BOT_ID/history</code></pre>'
              '<p class="muted small">Answers are JSON: your bots, one bot\'s latest report and when the next one is due, and its growth over time.</p></details></section>')
    return page("Settings | yapper.py telemetry", body, nonce, session, session.csrf, banners=banner_html)


TAB_LABELS = {"approvals": "Approvals", "bots": "My bots", "team": "Team", "people": "People", "stats": "Stats",
              "announce": "Announcements", "activity": "Activity"}


def tabs_for(store, user_id, role):
    owns = bool(store.owned_by(user_id) or store.shared_with(user_id) or store.waiting_for(user_id))
    tabs = []
    if role:
        tabs.append("approvals")
    if owns or not role:
        tabs.append("bots")
    if role:
        tabs += ["people", "activity"]
    if role in ("owner", "manager"):
        tabs += ["team", "stats", "announce"]
    return tabs


def dashboard(store, session, role, tab, message, nonce, support_url, now=None, query="", banner_html=""):
    now = now or time.time()
    ids = tabs_for(store, session.user_id, role)
    tab = tab if tab in ids else ids[0]
    nav = "".join(f'<a href="/panel/{t}"{" aria-current=page" if t == tab else ""}>{esc(TAB_LABELS[t])}</a>' for t in ids)
    if tab == "approvals":
        content = approvals_tab(store, role, session.csrf, now)
    elif tab == "team":
        content = team_tab(store, role, session.user_id, session.csrf)
    elif tab == "people":
        content = people_tab(store, role, query)
    elif tab == "stats":
        content = stats_tab(store)
    elif tab == "announce":
        content = announce_tab(store, session.csrf)
    elif tab == "activity":
        content = activity_tab(store, role)
    else:
        content = bots_tab(store, session.user_id, session.csrf, now)
    body = f'{flash(message)}<nav class="tabs" aria-label="Panel">{nav}</nav>{content}'
    if not role and "bots" in ids and len(ids) == 1:
        body += f'<p class="muted small">Need your bot on the list? Join the <a href="{esc(support_url)}">support server</a>.</p>'
    return page("Panel | yapper.py telemetry", body, nonce, session, session.csrf, banners=banner_html), tab
