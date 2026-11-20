# yapper.py 
[![yapper.py Total Downloads](https://static.pepy.tech/personalized-badge/yapper.py?period=total&units=ABBREVIATION&left_color=BLACK&right_color=GREEN&left_text=Total+Downloads)](https://pepy.tech/projects/yapper.py)

[![Join Our Server](https://our-websites.github.io/cdn.shapes.lol/yapin/join_server.svg)](https://www.yapin.gg/invite/pythonlibrary)

Python library for building bots on [Yapin'](https://yapin.gg/developers).

## Install

```bash
pip install yapper.py
```

Making a voice or music bot? Install the voice extras too (they add `livekit` and `cryptography`):

```bash
pip install "yapper.py[voice]"
```

Python 3.10 or newer. Prefer a single file? Download <https://yapin.gg/sdk/yapper.py> next to your bot and `import yapper`; it works without installing anything (a `.env` file is only read when `python-dotenv` is installed, which `pip install yapper.py` does for you).

## Quick start

Make a bot token in **Settings -> Developer** (it starts with `yb_`) and put it in a `.env` file next to your bot:

```
YAPPER_BOT_TOKEN=yb_your_token_here
```

Then write `bot.py`:

```python
import yapper

bot = yapper.Bot()  # reads YAPPER_BOT_TOKEN from the environment or your .env file

@bot.command("ping", "Check if the bot is alive")
def ping(msg):
    msg.reply("pong")

@bot.command("vote", "Start a pizza vote")
def vote(msg):
    msg.reply(
        embeds=yapper.Embed().title("Pizza tonight?").color("blue"),
        components=yapper.Row(
            yapper.Button.success("Yes", "vote:yes"),
            yapper.Button.danger("No", "vote:no"),
        ),
    )

@bot.button("vote:")  # clicks on buttons whose custom_id starts with "vote:"
def voted(i):
    i.ephemeral("You picked " + i.custom_id[5:])

@bot.on("member_join")
def welcome(ev):
    print(f"{ev.username} joined")

bot.run()
```

Run it with `python bot.py`. People can now type `/ping` or `!ping`. Nothing to host: the bot connects out to Yapin' and gets its events pushed (it falls back to long polling by itself).

### The `.env` file

`YAPPER_BOT_TOKEN` is read from your environment first, then from a `.env` file. The library looks for `.env` in the folder of the script you ran, then in the current folder, and in each parent folder. A variable that is already set in the environment always wins over the file. You can also pass the token directly: `yapper.Bot("yb_...")`.

## Main concepts

- **Commands**: `@bot.command("name", "description")`. People type `/name` or `!name`; `msg.args` is the rest of the line and `msg.argv` is it split on spaces.
- **Events**: `@bot.on("member_join")`, or `@bot.event` on a function named `on_member_join`. Also `@bot.on_dm` for direct messages and `@bot.on_reaction_on_mine` for reactions on the bot's own messages.
- **Buttons**: `@bot.button("prefix:")` for every `custom_id` starting with a prefix, or `@bot.on_button("ping")` for one exact id (a `re.compile(...)` pattern works too). Answer with `i.reply`, `i.ephemeral`, `i.update` or `i.ack` within 3 seconds.
- **Embeds and buttons**: `yapper.Embed()`, `yapper.Button`, `yapper.Row`. Every setter returns the object, so they chain: `yapper.Embed().title("Score").field("Kills", "12", True).color("green")`. The plain `yapper.row(...)`, `yapper.btn(...)` and `yapper.link_button(...)` helpers still work.
- **Components V2 layouts**: build a message out of `yapper.Container`, `Section`, `TextDisplay`, `MediaGallery`, `Separator` and `FileComponent`, then send it with `bot.send_layout(channel_id, [...])`:

  ```python
  bot.send_layout(channel_id, [
      yapper.Container(
          "# Ready?",
          yapper.Section("Press go to start").accessory(yapper.Button.primary("Go", "go")),
      ).accent("blue"),
  ])
  ```

- **Cogs and extensions**: group commands, listeners and buttons into a class, usually one file per topic, and load a whole folder with `bot.load_cogs("cogs")`:

  ```python
  # cogs/fun.py
  import yapper

  class Fun(yapper.Cog):
      @yapper.command("joke", "Tell a joke")
      def joke(self, msg):
          msg.reply("I know a UDP joke, but you might not get it")

      @yapper.listener("member_join")
      def hello(self, ev):
          print(ev.username, "joined")
  ```

  Use `bot.add_cog(...)`, `bot.load_extension(...)`, `bot.unload_extension(...)` and `bot.reload_extension(...)` to manage them while the bot runs.
- **Errors**: failed API calls raise `yapper.YapperError` (`.status`, `.retry_after`). Rate limits are waited out for you.

## Useful bot methods

- **Messages**: `send`, `send_embed`, `send_layout`, `edit`, `edit_layout`, `delete`, `bulk_delete`, `pin`, `unpin`, `react`, `unreact`, `reactions`, `message`, `messages`, `messages_after`, `dm`, `dm_send`
- **Interactions**: `respond` (or `i.reply`, `i.ephemeral`, `i.update`, `i.ack` on the click itself)
- **Servers and members**: `servers`, `server`, `member_counts`, `channels`, `members`, `member`, `roles`, `user`, `emojis`, `invites`, `events`
- **Roles, channels and permissions**: `add_role`, `remove_role`, `create_role`, `edit_role`, `delete_role`, `create_channel`, `edit_channel`, `delete_channel`, `create_category`, `channel_permissions`, `set_channel_permissions`, `set_everyone_perms`
- **Moderation**: `kick`, `ban`, `unban`, `bans`, `timeout`, `set_nickname`
- **Voice**: `voice`, `move_member`, `disconnect_member`, `join_voice`, `leave_voice`, and `await bot.connect_voice(...)` for music bots (install with `pip install "yapper.py[voice]"`)
- **Webhooks**: `create_webhook`, `webhooks`, `my_webhooks`, `send_webhook`, `delete_webhook`
- **Presence and profile**: `set_status`, `set_profile`, `set_avatar`, `set_banner`

Every method has a docstring: `help(yapper.Bot.send)`.

## Telemetry (optional, off by default)

yapper.py can send a small usage report so we can see which bots use the library. It is **off unless you turn it on**:
`import yapper` never loads it, a normal install never sends anything, and it is a separate file the main library does not
use. To turn it on:

```python
from yapper import telemetry

telemetry.enable(bot)   # or: bot.load_extension("yapper.telemetry")
```

Once the bot is online, and then every 30 minutes, it sends this as JSON:

- the bot's ID, username, profile picture link and description (its bio, read from its own Yapin' profile, at most 300 characters)
- command names, descriptions and options, cog and extension names, and how many listeners and buttons it has
- how many servers the bot is in and their total member (and online) count, just the numbers
- the yapper version, the Python version and the OS name

It **never** sends your bot token, message content, the names or IDs of members, servers or channels, or anything else.
`telemetry.preview(bot)` returns exactly what would be sent and sends nothing. To turn it off, remove the line above, call
`telemetry.disable(bot)`, or set `YAPPER_TELEMETRY=0` (or `DO_NOT_TRACK=1`), which wins even when telemetry is loaded.

### Getting approved

Reports are only accepted from bots on an approved list. If yours is not on it yet, the bot prints a message the first time
and sends nothing. To get added, [join the support server](https://www.yapin.gg/invite/pythonlibrary), ask someone to add
your bot to the approved list, and bring:

1. **your bot's ID** (the message the bot printed includes it), and
2. **proof that yapper.py is your main SDK**: a link to your bot's source code that uses `import yapper`, or the output of
   `telemetry.preview(bot)`, which shows the yapper version and your commands.

Add your Yapin' user ID to your `.env` as `TELEMETRY_OWNER_ID=...` (or pass `telemetry.enable(bot, owner_id="...")`) and you
can log in to the telemetry panel right away to follow your bot's status: pending, then approved. Nothing about your bot is
visible to anyone but you until it is approved, and then only the people who approve bots can see that it exists.

Once you are approved, log in to the panel with Yapin', open your bot, click **Show key** and copy its private key into your
`.env` as `TELEMETRY_KEY=...`, then restart your bot. The key proves reports really come from your bot, so keep it private
like a token (you can make a new one in the panel if it leaks). The panel shows what your bot has sent and when the next report is due.
You can have several bots: apply for each one in the panel's **My bots** tab, and manage them all from there. You can also share a bot with
other people there, as viewers (see the data) or managers (also the key). If you move to a new Yapin' account, ask in the support server
and an approver can switch your bots to it.

### How we use the data

We use it to see which bots are running on yapper.py and how (commands, server and member counts, library and Python
versions), so we know what to improve and which versions still need support. It is also how bots get picked to be
**featured**: if you want your bot featured in our Yapin' server and in this README, turn telemetry on and get approved,
and we choose from the approved bots using the details listed above. If you do not turn it on, nothing is sent and nothing
changes for you.

### The telemetry page

Bot owners log in with Yapin' at **<https://yapper-py.rustdactyl.tech>** to follow their bot's status, see what it reports, and
save its invite and support links. The page also draws live badges that update themselves, so nothing here needs editing:

[![Telemetry Stats](https://yapper-py.rustdactyl.tech/badge/stats.svg)](https://yapper-py.rustdactyl.tech)

### Featured bots

Bots built on yapper.py that opted in to telemetry and were picked by us:

[![Featured Bots Using Telemetry](https://yapper-py.rustdactyl.tech/badge/featured.svg)](https://yapper-py.rustdactyl.tech)

Running the receiving end yourself? The server and its setup guide are in [`server/`](server/README.md).

## Support

Questions, bug reports, getting a bot approved for telemetry: [join the yapper.py support server](https://www.yapin.gg/invite/pythonlibrary).
Bugs and feature requests can also go on [GitHub](https://github.com/our-websites/yapper.py/issues).

## API reference

For payload details and limits, see the Yapin' developer docs: <https://yapin.gg/developers>

## License

GPL-3.0-only
