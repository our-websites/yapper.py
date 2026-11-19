"""The pages of the telemetry server: the landing page everyone sees and the panel people log in to. Dark mode only.

Everything that is not fixed text goes through esc(). The page uses a per-response nonce for its one <style> and one
<script>, so the Content-Security-Policy can forbid everything else (no inline styles on elements, no inline handlers).
"""
import html
import json
import time

from telemetry_store import assignable_roles, bot_state, can_approve, can_change_owner, can_remove_roles, can_revoke

# What a report looks like (tests check this has the same shape as yapper.telemetry.collect()).
EXAMPLE_REPORT = {
    "schema": 1,
    "event": "heartbeat",
    "sent_at": "2026-03-14T18:30:12+00:00",
    "sdk": {"version": "1.8.4", "package_version": "1.8.4", "yapin_version": None},
    "runtime": {"python": "3.12.3", "os": "Linux"},
    "bot": {"id": "123456789", "username": "Cool Bot", "uptime_seconds": 86412},
    "servers": {"count": 14, "members_total": 2310, "online_total": 412},
    "commands": [
        {"name": "ping", "description": "Check if the bot is alive", "options": []},
        {"name": "vote", "description": "Start a pizza vote", "options": [{"name": "topping", "type": "string", "required": False}]},
    ],
    "cogs": ["Fun", "Moderation"],
    "extensions": ["yapper.telemetry"],
    "listeners": 3,
    "buttons": 2,
}

MESSAGES = {
    "approved": ("ok", "Bot approved. Its owner can now log in and copy the private key."),
    "applied": ("ok", "Application saved. Ask in the support server to get it approved; its status shows here."),
    "rotated": ("ok", "New key made. Copy it into TELEMETRY_KEY and restart your bot: the old one no longer works."),
    "shared": ("ok", "Access saved."),
    "unshared": ("ok", "Access removed."),
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
.row.quiet>.edge{background:var(--wait)}.row.removed>.edge{background:var(--bad)}
.row .in{padding:1rem 1.15rem}
.head{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;align-items:baseline}
.status{font-size:.9rem;display:inline-flex;gap:.45rem;align-items:center}
.dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--muted);display:inline-block}
.active .dot{background:var(--ok);animation:pulse 2.4s ease-in-out infinite}
.pending .dot,.waiting .dot,.quiet .dot{background:var(--wait)}.removed .dot{background:var(--bad)}
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
.empty{border:1px dashed var(--line);border-radius:.6rem;padding:1.5rem;color:var(--muted)}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:.85rem;padding:1.5rem 0}
"""

JS = """
for (const el of document.querySelectorAll('time[datetime]')) {
  const d = new Date(el.getAttribute('datetime'));
  if (!isNaN(d)) el.textContent = d.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
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


def when(ts):
    """A <time> element (the browser swaps in local time) with a UTC fallback."""
    if not ts:
        return "never"
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return f'<time datetime="{iso}">{time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))}</time>'


def page(title, body, nonce, user=None, csrf="", login_url=None):
    who = ""
    if user:
        who = (f'<div class="who"><span>{esc(user.username or "User")} <span class="id">{esc(user.user_id)}</span></span>'
               f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}">'
               f'<button type="submit">Log out</button></form></div>')
    elif login_url:
        who = f'<div class="who"><a class="btn" href="{esc(login_url)}">Log in with Yapin\'</a></div>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="dark"><meta name="robots" content="noindex"><title>{esc(title)}</title>'
            f'<style nonce="{nonce}">{CSS}</style></head><body data-csrf="{esc(csrf)}">'
            f'<header class="top"><div class="wrap"><a class="brand" href="/">yapper.py telemetry</a>{who}</div></header>'
            f'<main><div class="wrap">{body}</div></main>'
            f'<footer><div class="wrap">Opt-in telemetry for <a href="https://github.com/our-websites/yapper.py">yapper.py</a> bots. '
            f'Nothing is sent unless a bot owner turns it on.</div></footer>'
            f'<script nonce="{nonce}">{JS}</script></body></html>').encode()


def flash(code):
    kind, text = MESSAGES.get(code, (None, None))
    return f'<div class="flash {kind}" role="status">{esc(text)}</div>' if text else ""


# ---- the landing page ----

def landing(nonce, support_url, login_url, message=None, user=None, csrf=""):
    example = esc(json.dumps(EXAMPLE_REPORT, indent=2))
    cta = (f'<a class="btn primary" href="{esc(login_url)}">Log in with Yapin\'</a>' if login_url
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
      <li>The bot's ID and name</li>
      <li>Command names, descriptions and options; cog and extension names</li>
      <li>How many listeners and buttons it has</li>
      <li>How many servers it is in, with total and online member counts</li>
      <li>The yapper.py, Python and OS versions, and how long the bot has been up</li>
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
    return page("yapper.py telemetry", body, nonce, user, csrf, None if user else login_url)


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
             ("Commands", len(report.get("commands") or [])), ("yapper.py", sdk.get("package_version") or sdk.get("version")),
             ("Uptime", ago(up) if isinstance(up, (int, float)) else None)]
    return '<dl class="facts">' + "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in items if v is not None) + "</dl>"


def _commands(report):
    cmds = (report or {}).get("commands") or []
    if not cmds:
        return ""
    rows = "".join(f"<tr><td><code>/{esc(c.get('name'))}</code></td><td>{esc(c.get('description'))}</td></tr>" for c in cmds[:100] if isinstance(c, dict))
    return f"<details><summary>Commands ({len(cmds)})</summary><table><tbody>{rows}</tbody></table></details>"


def owner_row(bot, csrf, now, key_box=True, claimed=False, level="owner", members=()):
    code, label = bot_state(bot, now)
    name = bot["name"] or "Unnamed bot"
    report = bot.get("last_report")
    parts = [f'<div class="head"><h3>{esc(name)}</h3><span class="status"><span class="dot"></span>{esc(label)}</span></div>',
             f'<div class="id">{esc(bot["bot_id"])}</div>']
    if code == "pending":
        parts.append('<p class="muted">The bot has asked to send telemetry. It is waiting for someone on the team to approve it. '
                     'Nothing is shared until then, and you will see its data and private key here once it is.</p>')
    elif code == "removed":
        parts.append('<p class="muted">Telemetry access for this bot was removed. Ask in the support server if you think that is a mistake.</p>')
    else:
        parts.append(track(bot, now))
        if code == "waiting":
            parts.append('<p class="muted">Set the private key below as <code>TELEMETRY_KEY</code> in the bot\'s environment or <code>.env</code> and restart it.</p>')
        parts.append(_facts(report))
        parts.append(_commands(report))
        if report:
            parts.append(f"<details><summary>Latest report</summary><pre><code>{esc(json.dumps(report, indent=2))}</code></pre></details>")
        if key_box and bot.get("key") and level in ("owner", "manager"):
            parts.append(f'<div class="key" data-key-for="{esc(bot["bot_id"])}"><span class="muted small">Private key</span>'
                         f'<output aria-live="polite">{"&bull;" * 24}</output><button type="button">Show key</button></div>'
                         f'<form class="inline" method="post" action="/panel/rotate-key"><input type="hidden" name="csrf" value="{esc(csrf)}">'
                         f'<input type="hidden" name="bot_id" value="{esc(bot["bot_id"])}">'
                         f'<button type="submit">Make a new key</button> <span class="muted small">The old key stops working right away.</span></form>')
    if level != "owner" and code != "pending":
        parts.insert(2, f'<div class="muted small">Shared with you as {"a manager" if level == "manager" else "a viewer"} (owner <span class="id">{esc(bot["owner_id"])}</span>)</div>')
    if code != "pending" and bot["status"] == "approved" and level in ("owner", "manager"):
        parts.append(access_section(bot, csrf, level, members))
    return f'<div class="row {code}"><div class="edge"></div><div class="in">{"".join(parts)}</div></div>'


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
            f'<form class="inline" method="post" action="/panel/share">{hidden}<label>User ID<input name="user_id" required autocomplete="off"></label>'
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
    else:
        meta.append(f'<span class="muted small">owner <span class="id">{esc(bot["owner_id"] or "not set")}</span></span>')
        meta.append(f'<span class="muted small">last report {when(bot["last_report_at"])}</span>')
    forms = ""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="bot_id" value="{esc(bot["bot_id"])}">'
    if bot["status"] == "pending" and can_approve(role):
        forms = (f'<form class="inline" method="post" action="/panel/approve">{hidden}'
                 f'<label>Owner\'s user ID<input name="owner_id" required value="{esc(bot.get("applicant_id") or bot["claimed_owner_id"])}" autocomplete="off"></label>'
                 f'<button class="primary" type="submit">Approve</button></form>')
    elif bot["status"] == "approved" and can_change_owner(role):
        forms = (f'<form class="inline" method="post" action="/panel/owner">{hidden}'
                 f'<label>Owner\'s user ID<input name="owner_id" required value="{esc(bot["owner_id"])}" autocomplete="off"></label>'
                 f'<label class="check"><input type="checkbox" name="new_key" value="1"> Make a new key too</label>'
                 f'<button type="submit">Switch owner</button></form>'
                 f'<p class="muted small">Switching owner is for people who moved to a new account. It also ends any sharing the old owner set up.</p>')
        if can_revoke(role):
            forms += f'<form class="inline" method="post" action="/panel/revoke">{hidden}<button class="danger" type="submit">Remove access</button></form>'
    elif bot["status"] == "revoked" and can_approve(role):
        forms = (f'<form class="inline" method="post" action="/panel/approve">{hidden}'
                 f'<label>Owner\'s user ID<input name="owner_id" required value="{esc(bot["owner_id"])}" autocomplete="off"></label>'
                 f'<button type="submit">Approve again</button></form>')
    return (f'<div class="row {code}"><div class="edge"></div><div class="in"><div class="head"><h3>{esc(name)}</h3>'
            f'<span class="status"><span class="dot"></span>{esc(label)}</span></div><div class="head">{"".join(meta)}</div>{forms}</div></div>')


def approvals_tab(store, role, csrf, now):
    pending, approved, revoked = store.with_status("pending"), store.with_status("approved"), store.with_status("revoked")
    out = ["<h2>Waiting for approval</h2>"]
    out += [staff_row(b, role, csrf, now) for b in pending] or ['<div class="empty">No bots are waiting. When a bot asks for a token it shows up here.</div>']
    out.append('<section><h2>Approve by ID</h2>')
    out.append(f'<p class="muted small">For a bot that has not contacted the server yet.</p><form class="inline" method="post" action="/panel/approve">'
               f'<input type="hidden" name="csrf" value="{esc(csrf)}"><label>Bot ID<input name="bot_id" required autocomplete="off"></label>'
               f'<label>Owner\'s user ID<input name="owner_id" required autocomplete="off"></label>'
               f'<button class="primary" type="submit">Approve</button></form></section>')
    out.append(f'<section><h2>Approved ({len(approved)})</h2>')
    out += [staff_row(b, role, csrf, now) for b in approved] or ['<div class="empty">No bots are approved yet.</div>']
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
               f'<label>User ID<input name="user_id" required autocomplete="off"></label><label>Role<select name="role">{opts}</select></label>'
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
    rows = [owner_row(b, csrf, now, members=store.members(b["bot_id"]) if b["status"] == "approved" else ()) for b in mine]
    rows += [owner_row(b, csrf, now, level=b["level"], members=store.members(b["bot_id"])) for b in shared]
    rows += [owner_row(b, csrf, now, key_box=False, claimed=True) for b in waiting]
    return "".join(rows) + apply_form(csrf)


def dashboard(store, session, role, tab, message, nonce, support_url, now=None):
    now = now or time.time()
    owns = bool(store.owned_by(session.user_id) or store.shared_with(session.user_id) or store.waiting_for(session.user_id))
    tabs = []
    if role:
        tabs.append(("approvals", "Approvals"))
    if owns or not role:
        tabs.append(("bots", "My bots"))
    if role in ("owner", "manager"):
        tabs.append(("team", "Team"))
    ids = [t for t, _ in tabs]
    tab = tab if tab in ids else ids[0]
    nav = "".join(f'<a href="/?tab={t}"{" aria-current=page" if t == tab else ""}>{esc(label)}</a>' for t, label in tabs)
    if tab == "approvals":
        content = approvals_tab(store, role, session.csrf, now)
    elif tab == "team":
        content = team_tab(store, role, session.user_id, session.csrf)
    else:
        content = bots_tab(store, session.user_id, session.csrf, now)
    body = f'{flash(message)}<nav class="tabs" aria-label="Panel">{nav}</nav>{content}'
    if not role and not owns:
        body += f'<p class="muted small">Need your bot on the list? Join the <a href="{esc(support_url)}">support server</a>.</p>'
    return page("Panel | yapper.py telemetry", body, nonce, session, session.csrf)
