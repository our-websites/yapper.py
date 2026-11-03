# yapper.py

Python library for building bots on [Yapin'](https://yapin.gg/developers).

## Install

```bash
pip install yapper.py
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
- **Voice**: `voice`, `move_member`, `disconnect_member`, `join_voice`, `leave_voice`, and `await bot.connect_voice(...)` for music bots (needs `pip install livekit cryptography`)
- **Webhooks**: `create_webhook`, `webhooks`, `my_webhooks`, `send_webhook`, `delete_webhook`
- **Presence and profile**: `set_status`, `set_profile`, `set_avatar`, `set_banner`

Every method has a docstring: `help(yapper.Bot.send)`.

## API reference

For payload details and limits, see the Yapin' developer docs: <https://yapin.gg/developers>

## License

GPL-3.0-only
