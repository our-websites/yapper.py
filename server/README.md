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
| `GET /` | The panel if you are logged in, otherwise the landing page (what telemetry is, how it is used, an example report). The only page that returns HTML |
| `POST /` with `{"bot_id", "username", "key", "owner_id", "interval"}` | `200 {"token", "expires_in"}` for an approved bot with the right key. `403` while pending or removed, `401` when the key is missing or wrong; each comes with a message that tells the bot's owner what to do |
| `POST /report` with `Authorization: Bearer <token>` | `204`, the report is stored and the bot's name is set from it |
| `GET /health` | `{"ok": true}` |
| `/login`, `/auth/callback`, `/logout`, `POST /panel/*` | Login with Yapin' and the panel's actions |

## Roles

Roles are by Yapin' user ID.

| Role | Can |
| --- | --- |
| **Owner** | Everything: approve and remove bots, set bot owners, add managers and approvers, remove any role. Set with `TELEMETRY_OWNERS` |
| **Manager** | Approve and remove bots, set bot owners, add approvers |
| **Approver** | Approve bots, nothing else |
| **Bot owner** | Whoever is set as a bot's owner when it is approved: sees that bot's data, when its next report is due, and its private key |

Staff see which bots exist and their status, never a bot's data or key. Someone who is staff and also owns a bot gets an extra
**My bots** tab. Logging in with no role and no bots shows a message that you don't have any bots with telemetry.

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
`team`, `add <user_id> <manager|approver>`, `remove <user_id>`, `stop`. The console acts as an owner. It never prints keys.

## Settings

All of them are in [`.env.example`](.env.example) with comments. Environment variables set by your host win over `.env`.
If `TELEMETRY_SECRET` is empty one is made on first start and saved next to the database. Data lives in one SQLite file
(`TELEMETRY_DB`); it holds the private keys, so keep it private and back it up. Bots that were approved with the first version
of this server (`approved_bots.json`) are imported once on start; set their owners in the panel.

## Know this

- Cookies are HttpOnly and SameSite=Lax (Secure over https); every panel action needs a CSRF token; the page only allows its
  own inline style and script through a per-response nonce.
- Rate limits: 30 token requests a minute per address, 20 reports an hour per bot, 60 panel requests a minute per address.
- The owner ID a bot sends is not verified. It only lets that person see the bot's status; data and keys go to the owner
  set by whoever approved it.
- Bots on older yapper.py versions don't send a key. They are told to update. To let them through while they do, set
  `TELEMETRY_REQUIRE_KEY=0` (a wrong key is still refused).
- IP addresses are not logged or stored.
