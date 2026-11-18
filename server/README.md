# Telemetry server

A small receiver for the optional [`yapper.telemetry`](../README.md#telemetry-optional-off-by-default) reports. It lives
here, outside `src/`, so it is **not** part of the `yapper.py` pip package. It is one file with no dependencies:
`telemetry_server.py` (Python 3.10+).

## Run it

```bash
# 1. a secret that signs the tokens (keep it; changing it logs every bot out until it asks again)
export TELEMETRY_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# 2. approve a bot (this edits approved_bots.json; the server picks it up without a restart)
python3 telemetry_server.py approve 123456789 --name "Cool Bot" --note "repo: github.com/someone/cool-bot"

# 3. serve it
python3 telemetry_server.py serve --host 127.0.0.1 --port 8080
```

Bots only talk to `https://` addresses, so put HTTPS in front of it (Caddy, nginx, a Cloudflare tunnel, ...) and set the
public URL as `ENDPOINT` in `src/yapper/telemetry.py`, for example `https://telemetry.example.com` (no path).

Reports are appended to `telemetry_reports.jsonl`, one JSON object per line: `{"received_at": ..., "report": {...}}`.
IP addresses are not logged or stored.

## The flow

| Request | Answer |
| --- | --- |
| `POST /` with `{"bot_id": "..."}` (or `GET /?bot_id=...`) | `200 {"token", "expires_in"}` if the bot is on the approved list |
| same, bot not approved | `403` with a message telling them to join the support server and ask someone to add their bot, and to bring **their bot's ID** and **proof that yapper.py is their main SDK** |
| same, no bot ID | `400`, which asks for the bot ID (and says the same about approval) |
| `POST /report` with `Authorization: Bearer <token>` | `204`; the report is stored. `401` for a bad or expired token (the bot asks for a new one), `403` if the token is for a different bot than the report or approval was withdrawn |
| `GET /health` | `200` |

Rate limits: 30 token requests a minute per address, 20 reports an hour per bot. Reports are capped at 64 KB.

## Approving a bot

When someone joins the support server and asks, check the two things the message asks for:

1. **Their bot's ID.**
2. **Proof that yapper.py is their main SDK**: a link to their code that uses `import yapper`, or the output of
   `telemetry.preview(bot)` (it shows the yapper version and the bot's commands).

Then `python3 telemetry_server.py approve <bot id> --name "..." --note "..."`. Take a bot off with `revoke <bot id>`; its
token stops working straight away. `list` shows everyone. The file is plain JSON, so editing it by hand works too:
`{"bots": {"123456789": {"name": "Cool Bot"}}}` (a bare list of IDs is accepted as well).

## Settings

| Variable | |
| --- | --- |
| `TELEMETRY_SECRET` | Required to serve, at least 32 characters |
| `TELEMETRY_APPROVED` | Approved bots file, default `./approved_bots.json` |
| `TELEMETRY_DATA` | Reports file, default `./telemetry_reports.jsonl` |
| `TELEMETRY_SUPPORT_URL` | Support server invite shown to bots that are not approved |
| `TELEMETRY_TOKEN_TTL` | Token lifetime in seconds, default `3600` |
| `TELEMETRY_TRUST_PROXY` | `1` if a reverse proxy sets `X-Forwarded-For` (used for the rate limit only) |

## Know this

A bot ID is public, so anyone who knows an approved bot's ID can ask for a token for it and send reports in its name.
That can only skew the numbers you see; it gives them nothing from you. If that matters, give each bot a private key when
you approve it and check it in `handle_token()`.
