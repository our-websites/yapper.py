import importlib.util
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("yapper_main", ROOT / "src" / "yapper" / "main.py")
y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(y)


def make_bot():
    bot = y.Bot("yb_test", log=False)
    bot.me = y.Data({"id": "bot-id", "bot": {"owner_id": "owner-id"}})
    bot.sent = []
    bot.send = lambda channel_id, content="", **kwargs: bot.sent.append((channel_id, content, kwargs)) or y.Data({"id": "sent"})
    return bot


def message(content, **overrides):
    data = {
        "id": "message-id",
        "channel_id": "channel-id",
        "server_id": "server-id",
        "author_id": "user-id",
        "author": "river",
        "author_bot": False,
        "content": content,
    }
    data.update(overrides)
    return {"id": 1, "type": "message", "data": data}


class CommandRoutingTests(unittest.TestCase):
    def test_legacy_handlers_still_work_and_argv_respects_quotes(self):
        bot = make_bot()
        got = []
        bot.command("ping")(lambda msg: got.append(msg))
        bot.handle(message('!ping "two words" plain\\ value'))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].argv, ["two words", "plain value"])
        self.assertEqual(got[0].args, '"two words" plain\\ value')

    def test_malformed_quotes_fall_back_to_whitespace_for_legacy_handlers(self):
        bot = make_bot()
        got = []
        bot.command("ping")(lambda msg: got.append(msg.argv))
        bot.handle(message('!ping "unfinished value'))
        self.assertEqual(got, [['"unfinished', 'value']])

    def test_aliases_route_to_the_same_handler(self):
        bot = make_bot()
        got = []
        bot.command("roll", aliases=["dice"])(lambda msg: got.append(msg.args))
        bot.handle(message("!dice 12"))
        self.assertEqual(got, ["12"])

    def test_subcommands_use_the_longest_route_and_preserve_arguments(self):
        bot = make_bot()
        got = []
        bot.command("dice")(lambda msg: got.append(("root", msg.args)))
        bot.command("dice roll")(lambda msg: got.append(("roll", msg.args)))
        bot.handle(message('!dice roll "lucky die"'))
        self.assertEqual(got, [("roll", '"lucky die"')])

    def test_aliases_can_name_a_subcommand(self):
        bot = make_bot()
        got = []
        bot.command("dice roll", aliases=["roll", "dice r"])(lambda msg: got.append(msg.args))
        bot.handle(message("!roll 20"))
        bot.handle(message("!dice r 8"))
        self.assertEqual(got, ["20", "8"])

    def test_slash_delivery_resolves_subcommands(self):
        bot = make_bot()
        got = []
        bot.command("dice roll")(lambda msg: got.append(msg.args))
        bot.handle({"id": 1, "type": "command", "data": {"command": "dice", "args": "roll 6"}})
        self.assertEqual(got, ["6"])

    def test_only_root_commands_are_advertised(self):
        bot = make_bot()
        bot.command("dice", aliases=["die"])(lambda msg: None)
        bot.command("dice roll", aliases=["roll"])(lambda msg: None)
        bot.api = lambda *args, **kwargs: {}
        advertised = bot.sync_commands()
        self.assertEqual([c["name"] for c in advertised], ["dice"])

    def test_duplicate_aliases_are_rejected(self):
        bot = make_bot()
        bot.command("roll")(lambda msg: None)
        with self.assertRaisesRegex(ValueError, "alias"):
            bot.command("dice", aliases=["roll"])(lambda msg: None)


class TypedArgumentTests(unittest.TestCase):
    def test_annotations_convert_arguments_and_defaults(self):
        bot = make_bot()
        got = []

        @bot.command("roll")
        def roll(msg, sides: int = 6, label: str = "plain"):
            got.append((sides, label))

        bot.handle(message('!roll 12 "lucky die"'))
        bot.handle(message("!roll"))
        self.assertEqual(got, [(12, "lucky die"), (6, "plain")])

    def test_boolean_and_float_arguments(self):
        bot = make_bot()
        got = []

        @bot.command("configure")
        def configure(msg, enabled: bool, level: float):
            got.append((enabled, level))

        bot.handle(message("!configure yes 2.5"))
        self.assertEqual(got, [(True, 2.5)])

    def test_defaults_are_not_converted_as_input_strings(self):
        bot = make_bot()
        got = []

        @bot.command("configure")
        def configure(msg, enabled: bool = True, note: y.Optional[str] = None):
            got.append((enabled, note))

        bot.handle(message("!configure"))
        self.assertEqual(got, [(True, None)])

    def test_string_annotations_are_resolved(self):
        bot = make_bot()
        got = []

        @bot.command("roll")
        def roll(msg, sides: "int"):
            got.append(sides)

        bot.handle(message("!roll 4"))
        self.assertEqual(got, [4])

    def test_invalid_typed_argument_sends_usage_without_calling_handler(self):
        bot = make_bot()
        called = []

        @bot.command("roll")
        def roll(msg, sides: int):
            called.append(sides)

        bot.handle(message("!roll lots"))
        self.assertEqual(called, [])
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("sides", bot.sent[0][1])
        self.assertIn("Usage: /roll <sides>", bot.sent[0][1])

    def test_user_and_channel_mentions_convert_to_ids(self):
        bot = make_bot()
        got = []

        @bot.command("move")
        def move(msg, user: y.UserId, channel: y.ChannelId):
            got.append((user, channel))

        bot.handle(message("!move <@!user-123> <#channel-456>"))
        self.assertEqual(got, [("user-123", "channel-456")])


class CommandCheckTests(unittest.TestCase):
    def test_owner_only_uses_the_bot_profile_owner(self):
        bot = make_bot()
        got = []

        @y.owner_only()
        @bot.command("admin")
        def admin(msg):
            got.append(msg.author_id)

        bot.handle(message("!admin"))
        bot.handle(message("!admin", author_id="owner-id"))
        self.assertEqual(got, ["owner-id"])
        self.assertEqual(len(bot.sent), 1)

    def test_server_only_rejects_messages_without_a_server(self):
        bot = make_bot()
        got = []

        @y.server_only()
        @bot.command("settings")
        def settings(msg):
            got.append(msg)

        bot.handle(message("!settings", server_id=None))
        self.assertEqual(got, [])
        self.assertIn("server", bot.sent[0][1].lower())

    def test_role_check_accepts_any_required_role(self):
        bot = make_bot()
        bot.member = lambda server_id, user_id: y.Data({"role_ids": ["role-b"]})
        got = []

        @y.has_roles("role-a", "role-b")
        @bot.command("moderate")
        def moderate(msg):
            got.append(msg)

        bot.handle(message("!moderate"))
        self.assertEqual(len(got), 1)

    def test_permission_check_requires_every_permission(self):
        bot = make_bot()
        bot.get_permissions = lambda *args: y.Data({"permissions": {"manage_messages": True, "kick_members": False}})
        got = []

        @y.has_permissions("manage_messages", "kick_members")
        @bot.command("moderate")
        def moderate(msg):
            got.append(msg)

        bot.handle(message("!moderate"))
        self.assertEqual(got, [])
        self.assertIn("permission", bot.sent[0][1].lower())

    def test_server_owner_and_administrators_pass_permission_checks(self):
        bot = make_bot()
        bot.get_permissions = lambda *args: y.Data({"owner": True, "permissions": {}})
        got = []

        @y.has_permissions("manage_messages")
        @bot.command("moderate")
        def moderate(msg):
            got.append(msg)

        bot.handle(message("!moderate"))
        self.assertEqual(len(got), 1)

    def test_custom_check_decorator(self):
        bot = make_bot()
        got = []

        @y.check(lambda msg: msg.author_id == "allowed")
        @bot.command("special")
        def special(msg):
            got.append(msg)

        bot.handle(message("!special"))
        bot.handle(message("!special", author_id="allowed"))
        self.assertEqual(len(got), 1)


class CooldownTests(unittest.TestCase):
    def test_user_cooldown_limits_repeated_invocations(self):
        bot = make_bot()
        got = []

        @y.cooldown(1, 10, bucket="user")
        @bot.command("roll")
        def roll(msg):
            got.append(msg.author_id)

        with mock.patch.object(y.time, "monotonic", return_value=100):
            bot.handle(message("!roll"))
            bot.handle(message("!roll"))
            bot.handle(message("!roll", author_id="other-user"))
        self.assertEqual(got, ["user-id", "other-user"])
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("10", bot.sent[0][1])

    def test_cooldown_expires_and_supports_multiple_uses(self):
        bot = make_bot()
        got = []

        @y.cooldown(2, 10, bucket="channel")
        @bot.command("roll")
        def roll(msg):
            got.append(msg)

        with mock.patch.object(y.time, "monotonic", side_effect=[0, 1, 2, 11]):
            for _ in range(4):
                bot.handle(message("!roll"))
        self.assertEqual(len(got), 3)

    def test_invalid_cooldown_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            y.cooldown(0, 10)
        with self.assertRaises(ValueError):
            y.cooldown(1, 10, bucket="member")


class HelpAndPrefixTests(unittest.TestCase):
    def test_generated_help_contains_paths_aliases_and_typed_usage(self):
        bot = make_bot()

        @bot.command("dice roll", "Roll a die", aliases=["roll"])
        def roll(msg, sides: int = 6):
            pass

        text = bot.help("dice roll")
        self.assertIn("/dice roll [sides]", text)
        self.assertIn("Roll a die", text)
        self.assertIn("roll", text)
        self.assertIn("/dice roll [sides]", bot.help())

    def test_help_command_can_be_installed(self):
        bot = make_bot()
        bot.command("ping", "Check latency")(lambda msg: None)
        bot.help_command()
        bot.handle(message("!help ping"))
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("Check latency", bot.sent[0][1])

    def test_dynamic_prefixes_replace_static_prefixes(self):
        bot = make_bot()
        got = []
        bot.command("ping")(lambda msg: got.append(msg.server_id))
        bot.prefixes_with(lambda msg: "yap!" if msg.server_id == "server-id" else "!")
        bot.handle(message("!ping"))
        bot.handle(message("yap!ping"))
        self.assertEqual(got, ["server-id"])

    def test_mention_prefixes_are_opt_in(self):
        bot = make_bot()
        got = []
        bot.command("ping")(lambda msg: got.append(msg))
        bot.handle(message("<@bot-id> ping"))
        bot.mention_prefix()
        bot.handle(message("<@bot-id> ping"))
        bot.handle(message("<@!bot-id> ping"))
        self.assertEqual(len(got), 2)


class CogCommandTests(unittest.TestCase):
    def test_cog_decorators_carry_command_options(self):
        bot = make_bot()
        got = []

        class Fun(y.Cog):
            @y.cooldown(1, 10)
            @y.command("dice roll", aliases=["roll"])
            def roll(self, msg, sides: int = 6):
                got.append(sides)

        bot.add_cog(Fun(bot))
        bot.handle(message("!roll 4"))
        self.assertEqual(got, [4])
        bot.remove_cog("Fun")
        bot.handle(message("!roll 8"))
        self.assertEqual(got, [4])


if __name__ == "__main__":
    unittest.main()
