"""Tests for the Aegis Spend Guard policy engine and Hermes adapter.

Run:  python3 -m unittest discover -s tests -v   (from the plugin root)
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guard import DictStorage, SpendGuard, extract_spend  # noqa: E402
from policy import BLOCK, WARN, GuardPolicy, SpendAction, evaluate  # noqa: E402

FIXED_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def make_guard(policy=None, storage=None, paid_tools=None, gate_scripts=True, spend_tools=None):
    return SpendGuard(
        policy or GuardPolicy(),
        storage if storage is not None else DictStorage(),
        paid_tools=paid_tools or {},
        clock=lambda: FIXED_NOW,
        gate_scripts=gate_scripts,
        spend_tools=spend_tools or [],
    )


class PolicyEngineTests(unittest.TestCase):
    def test_allow_under_cap(self):
        ev = evaluate(GuardPolicy(), SpendAction("pay_api", "vendor.example", 500), 0)
        self.assertEqual(ev.verdict, "allow")
        self.assertEqual(ev.reason, "all checks passed")

    def test_block_over_per_tx_cap(self):
        ev = evaluate(GuardPolicy(), SpendAction("pay_api", "vendor.example", 2600), 0)
        self.assertEqual(ev.verdict, BLOCK)
        self.assertIn("per-transaction cap", ev.reason)

    def test_daily_limit_block(self):
        # Already spent $95 of the $100 daily limit; a $10 spend overflows.
        ev = evaluate(GuardPolicy(), SpendAction("pay_api", "vendor.example", 1000), 9500)
        self.assertEqual(ev.verdict, BLOCK)
        self.assertIn("daily limit", ev.reason)

    def test_frozen_blocks_everything(self):
        ev = evaluate(
            GuardPolicy(frozen=True), SpendAction("pay_api", "vendor.example", 100), 0
        )
        self.assertEqual(ev.verdict, BLOCK)
        self.assertIn("frozen", ev.reason)

    def test_blocklist_blocks(self):
        policy = GuardPolicy(blocklist=["evil.example"])
        ev = evaluate(policy, SpendAction("pay_api", "evil.example", 100), 0)
        self.assertEqual(ev.verdict, BLOCK)
        self.assertIn("blocklist", ev.reason)

    def test_unknown_recipient_routes_to_owner_by_default(self):
        policy = GuardPolicy(allowlist=["known.example"])
        ev = evaluate(policy, SpendAction("pay_api", "stranger.example", 100), 0)
        self.assertEqual(ev.verdict, WARN)
        self.assertIn("owner must confirm", ev.reason)

    def test_unknown_recipient_blocked_in_strict_mode(self):
        policy = GuardPolicy(allowlist=["known.example"], unknown_recipient="block")
        ev = evaluate(policy, SpendAction("pay_api", "stranger.example", 100), 0)
        self.assertEqual(ev.verdict, BLOCK)

    def test_known_recipient_on_allowlist_allows(self):
        policy = GuardPolicy(allowlist=["known.example"])
        ev = evaluate(policy, SpendAction("pay_api", "known.example", 100), 0)
        self.assertEqual(ev.verdict, "allow")

    def test_warn_zone_near_cap(self):
        # $21 of a $25 cap = 84% >= 80% warn threshold.
        ev = evaluate(GuardPolicy(), SpendAction("pay_api", "vendor.example", 2100), 0)
        self.assertEqual(ev.verdict, WARN)


class GuardAdapterTests(unittest.TestCase):
    def test_allow_under_cap_returns_no_directive(self):
        guard = make_guard()
        directive, ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 5.00, "recipient": "vendor.example"}
        )
        self.assertIsNone(directive)
        self.assertEqual(ev.verdict, "allow")

    def test_block_over_cap_returns_block_directive(self):
        guard = make_guard()
        directive, ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 30.00, "recipient": "vendor.example"}
        )
        self.assertEqual(directive["action"], "block")
        self.assertEqual(ev.verdict, BLOCK)

    def test_unknown_recipient_returns_approve_directive(self):
        guard = make_guard(GuardPolicy(allowlist=["known.example"]))
        directive, ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 5.00, "recipient": "stranger.example"}
        )
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(ev.verdict, WARN)

    def test_daily_limit_block_via_ledger(self):
        guard = make_guard()
        # Four executed $24 spends = $96 of the $100 daily limit.
        for _ in range(4):
            guard.record_execution("pay_api", {"amount_usd": 24.00, "recipient": "v"})
        self.assertEqual(guard.spent_today_cents(), 9600)
        directive, ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 10.00, "recipient": "v"}
        )
        self.assertEqual(directive["action"], "block")
        self.assertIn("daily limit", ev.reason)

    def test_frozen_blocks_via_hook_path(self):
        guard = make_guard()
        guard.set_frozen(True)
        directive, _ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 1.00, "recipient": "v"}
        )
        self.assertEqual(directive["action"], "block")
        guard.set_frozen(False)
        directive, _ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 1.00, "recipient": "v"}
        )
        self.assertIsNone(directive)

    def test_non_spend_call_is_ignored(self):
        guard = make_guard()
        directive, ev = guard.evaluate_call("read_file", {"path": "/tmp/x"})
        self.assertIsNone(directive)
        self.assertIsNone(ev)

    def test_unpriced_spend_routes_to_owner(self):
        guard = make_guard()
        directive, ev = guard.evaluate_call("transfer_tokens", {"to": "someone"})
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(ev.verdict, WARN)

    def test_paid_tool_fixed_price(self):
        guard = make_guard(paid_tools={"fancy_search": 5000})  # $50/call > $25 cap
        action = extract_spend("fancy_search", {}, guard.paid_tools)
        self.assertEqual(action.amount_cents, 5000)
        directive, _ev = guard.evaluate_call("fancy_search", {})
        self.assertEqual(directive["action"], "block")

    def test_amount_cents_key_wins(self):
        action = extract_spend("pay_api", {"amount_cents": 1234, "amount_usd": 99}, {})
        self.assertEqual(action.amount_cents, 1234)

    def test_blocked_spend_never_reaches_ledger(self):
        guard = make_guard()
        directive, _ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 500.00, "recipient": "v"}
        )
        self.assertEqual(directive["action"], "block")
        # post_tool_call never fires for a vetoed call, so nothing records.
        self.assertEqual(guard.spent_today_cents(), 0)


class ScriptGateTests(unittest.TestCase):
    """Spends written into shell commands / scripts (the `terminal` tool
    passes its command as {"command": ...}) are inspected too."""

    def test_script_priced_spend_over_cap_blocked(self):
        guard = make_guard()
        args = {
            "command": "curl -s https://api.stripe.com/v1/charges "
            "-d '{\"amount_usd\": 500}'"
        }
        action = extract_spend("terminal", args, {})
        self.assertEqual(action.amount_cents, 50000)
        self.assertEqual(action.recipient, "api.stripe.com")
        directive, ev = guard.evaluate_call("terminal", args)
        self.assertEqual(directive["action"], "block")
        self.assertEqual(ev.verdict, BLOCK)

    def test_script_dollar_literal_priced(self):
        action = extract_spend("terminal", {"command": "bash pay.sh $500"}, {})
        self.assertEqual(action.amount_cents, 50000)

    def test_script_currency_suffix_priced(self):
        action = extract_spend(
            "terminal", {"command": "python swap_tokens.py 100 USDT"}, {}
        )
        self.assertEqual(action.amount_cents, 10000)

    def test_script_spend_verb_no_amount_routes_to_owner(self):
        guard = make_guard()
        args = {
            "command": "curl -X POST https://api.stripe.com/v1/charges "
            "-d customer=cus_123"
        }
        directive, ev = guard.evaluate_call("terminal", args)
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(ev.verdict, WARN)
        # The owner is told WHY the gate fired: detection was in the text.
        self.assertIn("command/script text", directive["message"])

    def test_frozen_blocks_script_spends(self):
        guard = make_guard(GuardPolicy(frozen=True))
        directive, _ev = guard.evaluate_call(
            "terminal", {"command": "curl -X POST https://api.stripe.com/v1/charges"}
        )
        self.assertEqual(directive["action"], "block")
        directive, _ev = guard.evaluate_call(
            "terminal", {"command": "bash pay.sh $500"}
        )
        self.assertEqual(directive["action"], "block")

    def test_script_priced_spend_within_caps_allowed_and_ledgered(self):
        guard = make_guard()
        args = {"command": "python3 send_invoice.py --amount 5"}
        directive, ev = guard.evaluate_call("terminal", args)
        self.assertIsNone(directive)
        self.assertEqual(ev.verdict, "allow")
        guard.record_execution("terminal", args)
        self.assertEqual(guard.spent_today_cents(), 500)

    def test_benign_commands_have_no_opinion(self):
        guard = make_guard()
        benign = [
            "ls -la",
            "git status",
            "npm test",
            "cat README.md",
            "python train.py",
            "echo hello",
            "awk '{print $1}' data.csv",  # $1 is a field ref, not money
            "grep -r payload src/",  # 'pay' inside a word is not a verb
            "curl https://docs.example.com/guide",  # plain docs fetch
        ]
        for cmd in benign:
            with self.subTest(cmd=cmd):
                self.assertIsNone(extract_spend("terminal", {"command": cmd}, {}))
                directive, ev = guard.evaluate_call("terminal", {"command": cmd})
                self.assertIsNone(directive)
                self.assertIsNone(ev)

    def test_gate_scripts_disabled_passes_script_spends(self):
        guard = make_guard(gate_scripts=False)
        args = {"command": "bash pay.sh $500"}
        self.assertIsNone(extract_spend("terminal", args, {}, gate_scripts=False))
        directive, ev = guard.evaluate_call("terminal", args)
        self.assertIsNone(directive)
        self.assertIsNone(ev)

    def test_script_gate_applies_regardless_of_tool_name(self):
        # Any tool that smuggles a command string gets inspected, and the
        # other command-text keys are recognised too.
        action = extract_spend(
            "run_notebook_cell", {"code": "charge_card(amount=30)"}, {}
        )
        self.assertEqual(action.amount_cents, 3000)


class SpendDetectionScopingTests(unittest.TestCase):
    """Arg-key amounts are money ONLY on spend tools (owner-listed or
    spend-named). Core tools that happen to carry amount/value-shaped
    arguments — the maintainer's review cases — must pass untouched:
    no directive, no evaluation, nothing in the daily ledger."""

    def assert_not_a_spend(self, guard, tool_name, args):
        self.assertIsNone(
            extract_spend(
                tool_name,
                args,
                guard.paid_tools,
                spend_tools=guard.spend_tools,
                allowed_tools=guard.policy.allowed_tools,
            )
        )
        directive, ev = guard.evaluate_call(tool_name, args)
        self.assertIsNone(directive)
        self.assertIsNone(ev)
        guard.record_execution(tool_name, args)
        self.assertEqual(guard.spent_today_cents(), 0)

    def test_computer_use_set_value_text_is_not_a_spend(self):
        # computer_use setting a form field to "Blue": the value parses
        # to 0 and used to be BLOCKED as a non-positive spend.
        guard = make_guard()
        self.assert_not_a_spend(
            guard, "computer_use", {"action": "set_value", "value": "Blue"}
        )

    def test_scroll_amount_is_not_a_spend(self):
        # A scroll amount is a distance (3 lines/clicks), not $3.
        guard = make_guard()
        self.assert_not_a_spend(guard, "computer_use", {"action": "scroll", "amount": 3})
        self.assert_not_a_spend(guard, "scroll", {"amount": 3})

    def test_drive_preview_negative_scroll_amount_is_not_a_spend(self):
        # drive_preview scroll amounts can be negative; they used to
        # enter the ledger path as negative spends.
        guard = make_guard()
        self.assert_not_a_spend(guard, "drive_preview", {"amount": -240})

    def test_unlisted_tool_amount_keys_are_ignored(self):
        guard = make_guard()
        self.assert_not_a_spend(guard, "weather_lookup", {"price": 99999, "total": 5})

    def test_show_tip_is_not_routed_to_approval(self):
        # "tip" sits in noun position: the tool displays a tip, it does
        # not send one. Segment matching must not gate it.
        guard = make_guard()
        directive, ev = guard.evaluate_call("show_tip", {})
        self.assertIsNone(directive)
        self.assertIsNone(ev)
        self.assert_not_a_spend(guard, "show_tip", {"value": "Thanks for reading"})

    def test_hint_substrings_do_not_match(self):
        guard = make_guard()
        self.assert_not_a_spend(guard, "tooltips", {})

    def test_spend_named_tool_amounts_still_count(self):
        # Genuine spend tools keep the old behaviour end to end.
        guard = make_guard()
        directive, ev = guard.evaluate_call(
            "pay_api", {"amount_usd": 30.00, "recipient": "vendor.example"}
        )
        self.assertEqual(directive["action"], "block")
        self.assertEqual(ev.verdict, BLOCK)

    def test_owner_spend_tools_list_scopes_detection(self):
        # A tool with no spend verb in its name counts once the owner
        # lists it in spend_tools...
        guard = make_guard(spend_tools=["invoice_runner"])
        directive, ev = guard.evaluate_call(
            "invoice_runner", {"amount_usd": 30.00, "recipient": "vendor.example"}
        )
        self.assertEqual(directive["action"], "block")
        self.assertEqual(ev.verdict, BLOCK)
        # ...and an amount-less call to it is an unpriced spend, gated.
        directive, ev = guard.evaluate_call("invoice_runner", {})
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(ev.verdict, WARN)
        # The same tool is invisible to a guard whose owner did not list it.
        self.assert_not_a_spend(
            make_guard(), "invoice_runner", {"amount_usd": 30.00}
        )

    def test_allowed_tools_scope_detection(self):
        # allowed_tools membership also qualifies a tool for arg-based
        # detection (its amounts are then policy-checked as spends).
        guard = make_guard(GuardPolicy(allowed_tools=["ledger_entry"]))
        directive, ev = guard.evaluate_call(
            "ledger_entry", {"amount_usd": 5.00, "recipient": "vendor.example"}
        )
        self.assertIsNone(directive)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.verdict, "allow")


if __name__ == "__main__":
    unittest.main()
