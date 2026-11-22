# Telemetry server and panel

The receiving end of the optional [`yapper.telemetry`](../README.md#telemetry-optional-off-by-default) reports, with a web
panel for approving bots and for bot owners. It lives here, outside `src/`, so it is **not** part of the `yapper.py` pip
package. Python 3.10+.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env     # fill it in once (see below)
python3 telemetry_server.py
```

That is the only command. It takes no arguments and no subcommands, so it fits hosts where you can start one process and type
into its console but can't run other scripts. **Everything else is done in the web panel**: approving bots, setting owners,
adding managers and approvers. While it runs you can also type commands into its console (`help` lists them).

It listens on `SERVER_HOST:SERVER_PORT` (default `0.0.0.0:8080`). Bots only talk to `https://` addresses, so put HTTPS in front
of it (your host's proxy, Caddy, nginx, a tunnel) and set the public address as `TELEMETRY_PANEL_URL`.

## What it serves

| Request | Answer |
| --- | --- |
| `GET /` | The landing page (what telemetry is, how it is used, an example report), always: logged in people get a **Panel** button, everyone else a Log in button |
| `GET /panel/<tab>`, `GET /settings` | The panel (tabs `approvals`, `bots`, `people`, `activity`, `team`, `stats`, `announce`; which ones you see depends on your role) and your settings (timezone, API keys) |
| `GET /api/bots`, `/api/bots/<id>`, `/api/bots/<id>/history` | Read-only JSON for bots you own or manage, with `Authorization: Bearer uk_...` (make a key in **Settings**) |
| `POST /` with `{"bot_id", "username", "key", "owner_id", "interval"}` | `200 {"token", "expires_in"}` for an approved bot with the right key. `403` while pending or removed, `401` when the key is missing or wrong; each comes with a message that tells the bot's owner what to do |
| `POST /report` with `Authorization: Bearer <token>` | `204`, the report is stored and the bot's name is set from it |
| `GET /health` | `{"ok": true}` |
| `/login`, `/auth/callback`, `/logout`, `POST /panel/*` | Login with Yapin' and the panel's actions |

## Live badges for READMEs

Public SVGs drawn on request from the current numbers, so a README that shows one never needs editing:

- `/badge/stats.svg`: how many bots use telemetry and how many servers they are in.
- `/badge/featured.svg`: a grid of up to 100 approved bots (small icon, name, description, server and member counts), so a README's bot list fills itself in.
- `/badge/<bot id>.svg`: one bot's icon, name and server count. It exists only for approved bots whose owner saved an invite link.
  Link it to the invite: `[![Name](https://your-panel/badge/<id>.svg)](https://yapin.gg/bot/<id>)`. The staff page for a bot (`/panel/<bot id>`) shows this line ready to copy.

The icon is fetched by the server (only from Yapin's image hosts, small images, cached) and embedded, because an SVG shown in a README can't load other images.

## Roles

Roles are by Yapin' user ID.

| Role | Can |
| --- | --- |
| **Owner** | Everything: approve and remove bots, set bot owners, add managers and approvers, remove any role. Set with `TELEMETRY_OWNERS` |
| **Manager** | Approve and remove bots, switch bot owners, add approvers |
| **Approver** | Approve bots and switch a bot to a new owner account (for people who moved to a new account). Nothing else |
| **Bot owner** | Whoever is set as a bot's owner when it is approved: sees that bot's data, when its next report is due, and its private key, and can share the bot (below) |

Staff see which bots exist, their status and their report data (stats and the latest report), but never a bot's key. Someone who is staff and also owns a bot gets an extra
**My bots** tab. Logging in with no role and no bots shows a message that you don't have any bots with telemetry.

## Sharing a bot

A bot's owner can give other people access from the **My bots** tab ("Who can see this bot"): a **viewer** sees the data and
when the next report is due; a **manager** also gets the private key, can make a new one, and can add viewers. Only the owner can
add or remove managers; anyone can leave. A bot can be shared with up to 20 people. Staff roles don't give access to a bot's data.

## Switching an owner

If someone moves to a new Yapin' account, an approver (or manager, or owner) enters the new user ID under **Switch owner** in the
Approvals tab. Tick **Make a new key too** if the old account may be compromised. Switching also ends any sharing the old owner
set up, so the new owner starts clean. The console does the same with `owner <bot_id> <user_id>`.

## Approving a bot

When someone joins the support server and asks, check the two things the bot's message asks for: their **bot's ID** and **proof
that yapper.py is their main SDK**. Bots that have contacted the server already sit in the **Approvals** tab as pending (with
the owner ID they said, which is not verified: check it). Enter the owner's user ID and press **Approve**. A private key is
made for the bot. The owner logs in, copies it from their bot's page (hidden until they click it, click again to hide) and
sets it as `TELEMETRY_KEY` in the bot's `.env`.

## Login with Yapin'

The panel uses the OAuth2 authorization-code flow. Register this server as an app on Yapin', register
`TELEMETRY_PANEL_URL/auth/callback` as its redirect, and fill in the `YAPIN_*` settings in `.env`. Every endpoint and the names
of the user-ID and username fields are settings, so no code needs changing if Yapin's differ from the defaults.

For trying it on your own machine, `TELEMETRY_DEV_LOGIN=1` adds `/dev-login?user=<id>`, which only works from the same machine.
Never leave it on for a real server.

## Console commands

`status`, `list [pending|approved|removed]`, `approve <bot_id> <owner_id>`, `revoke <bot_id>`, `owner <bot_id> <user_id>`,
`team`, `add <user_id> <manager|approver>`, `remove <user_id>`, `find <name or id>`, `stats`, `announce all|<user> <title> | <message>`, `activity [n]`, `stop`. The console acts as an owner. It never prints keys.

## Settings

All of them are in [`.env.example`](.env.example) with comments. Environment variables set by your host win over `.env`.
If `TELEMETRY_SECRET` is empty one is made on first start and saved next to the database. Data lives in one [PickleDB](https://pypi.org/project/pickledb/) file
(`TELEMETRY_DB`, default `telemetry.json`); it holds the private keys, so keep it private (the server creates it readable by its
owner only) and back it up. Approvals, roles, keys and sharing are saved the moment you make them; incoming reports are batched and
written within a few seconds. Only each bot's latest report is kept. A file the server can't read is never overwritten: it stops
with an error so you can fix or move it. Bots that were approved with the first version
of this server (`approved_bots.json`) are imported once on start; set their owners in the panel.

## Know this

- Cookies are HttpOnly and SameSite=Lax (Secure over https); every panel action needs a CSRF token; the page only allows its
  own inline style and script through a per-response nonce.
- Rate limits: 30 token requests a minute per address, 20 reports an hour per bot, 60 panel requests a minute per address.
- The owner ID a bot sends is not verified. It only lets that person see the bot's status; data and keys go to the owner
  set by whoever approved it, and only after they log in as that user. Once an approver has set an ID as a bot's owner it is
  verified: bots that only *say* they belong to that ID no longer show up for them. A verified owner (or anyone logged in)
  can have as many bots as they like and applies for more in the **My bots** tab, which records the verified ID on the
  application. Owners can also make a new private key for a bot there; the old one stops working at once.
- Bots on older yapper.py versions don't send a key. They are told to update. To let them through while they do, set
  `TELEMETRY_REQUIRE_KEY=0` (a wrong key is still refused).
- IP addresses are not logged or stored.


## People, announcements, activity and settings

- **Profile pictures and people.** Yapin' tells us each person's name and picture when they log in. Staff search people by username or user ID in **People**.
  IDs are what is saved: a username is turned into an ID using logins, and an ID nobody has used yet is accepted if it matches
  `TELEMETRY_USER_ID_PATTERN` (default digits only). A username nobody has logged in with gets "they need to log in to the panel first".
  Pictures are only loaded from `www.yapin.gg` and `files.yapper.website` (`YAPIN_SITE_URL`, `YAPIN_FILES_URL`).
- **Stats** (owners and managers): logins, active and new people over 24 hours, 7 and 30 days, and per day.
- **Announcements** (owners and managers): to everyone or one person. Offline people see them the next time they open a page, until they dismiss them.
- **Activity**: bot owners see what happened on their bot. Approvers see what was done in the approvals panel. Managers and owners also see
  who opened which tab, and who read which bot's data through an API key.
- **Settings**: timezone (times are stored in UTC and shown in yours; UTC does no conversion) and API keys (up to 5, read-only, shown once).
- **Bot pages.** Owners, managers and viewers open a bot's data at `/bots/<bot id>`; the status, numbers and growth on it refresh by themselves
  every 30 seconds while the tab is visible. The team sees the same data on `/panel/<bot id>` (`/bots/<id>` sends them there), along with the owner,
  owner/approval actions, activity on the bot and the README link, but never the key.
- **Bot cards.** In **My bots** and `/panel`, an approved bot is a small card: picture, name, description and when it last reported. Pressing the card opens its page
  with the stats. Bots that are pending, denied or removed stay as full rows, because their message and actions are on the list.
- **Invite link and README link**: owners and managers save their bot's yapin.gg link on its page. The ready-to-paste Markdown link (the bot's icon and name as one
  link that opens the bot to add it) is made for the team only: on `/panel/<bot id>` and under **Promo links** in Approvals. It uses the saved link, or
  `https://yapin.gg/bot/<bot id>` when the ID is a Yapin' bot ID (like shapes-bot's `/add`).
- **Support server and description.** Owners and managers save a yapin.gg support-server invite on the bot's page (`/panel/support`); it is shown to the bot's people on the bot page. The bot's description is reported by the bot itself (its bio from its own profile, or its member info in a server it is in, up to 300 characters, shown as plain text), so owners change it with `set_profile(bio=...)`.
- **Bot pictures.** Bots report their picture as Yapin' gives it (a path under `files.yapper.website/avatars/`); the panel only loads pictures from the allowed hosts.
- **Versions.** Each bot shows two: the SDK's own version (`__version__` in `main.py`) and the PyPI package's.
- **Report size.** `TELEMETRY_MAX_REPORT_BYTES` (default 2 GiB) applies only to bots with a valid token. Reports are read into memory, so lower it on a small host.
