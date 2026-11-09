"""Wiring test: load the plugin the way Hermes does (as a package with a
synthetic name, since the directory is hyphenated), call register(ctx)
against a ctx-shaped double, and verify:

* exactly the tools/hooks declared in plugin.yaml get registered;
* the registered pre_tool_call hook blocks an over-cap spend end-to-end
  and lets a clean spend through;
* the /spendguard command freezes and unfreezes the guard.

Run:  python3 -m unittest discover -s tests -v   (from the plugin root)
"""

import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "aegis_spend_guard",
        PLUGIN_DIR / "__init__.py",
    )
    spec.submodule_search_paths = [str(PLUGIN_DIR)]
    module = importlib.util.module_from_spec(spec)
    sys.modules["aegis_spend_guard"] = module
    spec.loader.exec_module(module)
    return module


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.state = FakeState()
        self.tools = {}
        self.hooks = {}
        self.commands = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = (toolset, schema, handler)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler


class HermesWiringTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.ctx = FakeCtx()
        self.plugin.register(self.ctx)

    def test_registers_declared_surface(self):
        self.assertEqual(sorted(self.ctx.tools), ["spend_guard_check", "spend_guard_status"])
        self.assertEqual(sorted(self.ctx.hooks), ["post_tool_call", "pre_tool_call"])
        self.assertIn("spendguard", self.ctx.commands)

    def test_hook_blocks_over_cap_spend(self):
        pre = self.ctx.hooks["pre_tool_call"]
        directive = pre(
            tool_name="pay_api",
            args={"amount_usd": 500.0, "recipient": "vendor.example"},
            task_id="t1",
        )
        self.assertEqual(directive["action"], "block")

    def test_hook_allows_clean_spend_and_records_it(self):
        pre = self.ctx.hooks["pre_tool_call"]
        post = self.ctx.hooks["post_tool_call"]
        args = {"amount_usd": 5.0, "recipient": "vendor.example"}
        self.assertIsNone(pre(tool_name="pay_api", args=args, task_id="t1"))
        post(tool_name="pay_api", args=args, result="ok", task_id="t1", duration_ms=3)
        status = self.plugin._tool_spend_guard_status()
        self.assertIn('"spent_today_usd": 5.0', status)

    def test_slash_command_freeze_unfreeze(self):
        cmd = self.ctx.commands["spendguard"]
        self.assertIn("FROZEN", cmd("freeze"))
        pre = self.ctx.hooks["pre_tool_call"]
        directive = pre(
            tool_name="pay_api",
            args={"amount_usd": 1.0, "recipient": "vendor.example"},
            task_id="t1",
        )
        self.assertEqual(directive["action"], "block")
        self.assertIn("unfrozen", cmd("unfreeze"))

    def test_check_tool_dry_run(self):
        _toolset, _schema, handler = self.ctx.tools["spend_guard_check"]
        out = handler({"tool_name": "pay_api", "amount_usd": 30.0, "recipient": "v"})
        self.assertIn('"verdict": "block"', out)

    def test_script_gate_blocks_and_can_be_disabled(self):
        # A $500 spend written into a terminal command is blocked...
        args = {
            "command": "curl -s https://api.stripe.com/v1/charges "
            "-d '{\"amount_usd\": 500}'"
        }
        pre = self.ctx.hooks["pre_tool_call"]
        directive = pre(tool_name="terminal", args=args, task_id="t1")
        self.assertEqual(directive["action"], "block")
        # ...unless the owner turned the script gate off in settings.
        ctx2 = FakeCtx({"gate_scripts": False})
        self.plugin.register(ctx2)
        pre2 = ctx2.hooks["pre_tool_call"]
        self.assertIsNone(pre2(tool_name="terminal", args=args, task_id="t2"))


if __name__ == "__main__":
    unittest.main()
