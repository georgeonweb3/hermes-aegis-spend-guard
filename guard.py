"""Aegis Spend Guard — stateful adapter between Hermes tool calls and the
pure policy engine in :mod:`policy`.

Responsibilities:

* Decide whether a tool call is a *spend* and extract (tool, recipient,
  amount) from its arguments:
    - an explicit amount in the args (``amount_cents`` / ``amount_usd`` /
      ``amount`` / ``price`` / ``cost`` / ``total`` ...), or
    - a fixed per-call price from the owner's ``paid_tools`` map, or
    - a spend written into command/script text (a ``command`` / ``cmd`` /
      ``script`` / ``code`` argument — e.g. a ``terminal`` call): a money
      literal in the text prices the spend; a payment verb with no
      readable amount makes it an *unpriced* spend, routed to the owner.
      This inspection is the ``gate_scripts`` option (default on), or
    - a spend-shaped tool name (pay/transfer/purchase/...) with no readable
      amount — treated as an *unpriced spend* and routed to the owner,
      fail-closed, exactly like tgp-guard's HOLD.
* Keep the daily spend ledger (executed spends only) in injected storage —
  ``ctx.state`` in Hermes, a plain dict in tests.
* Translate verdicts into Hermes ``pre_tool_call`` directives:
    allow -> None, warn -> {"action": "approve", ...} (Hermes's own
    human-approval gate = route to owner), block -> {"action": "block"}.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

try:  # package import (Hermes loads plugins as packages)
    from .policy import BLOCK, WARN, Evaluation, GuardPolicy, SpendAction, evaluate
except ImportError:  # top-level import (tests, direct execution)
    from policy import BLOCK, WARN, Evaluation, GuardPolicy, SpendAction, evaluate

AMOUNT_CENTS_KEYS = ("amount_cents", "cost_cents", "price_cents")
AMOUNT_USD_KEYS = (
    "amount_usd",
    "cost_usd",
    "price_usd",
    "total_usd",
    "amount",
    "cost",
    "price",
    "total",
    "value",
)
RECIPIENT_KEYS = (
    "recipient",
    "counterparty",
    "payee",
    "to",
    "address",
    "destination",
    "merchant",
    "vendor",
    "seller",
)
SPEND_TOOL_HINTS = (
    "pay",
    "transfer",
    "purchase",
    "buy",
    "checkout",
    "send",
    "swap",
    "withdraw",
    "tip",
    "donate",
    "subscribe",
    "charge",
)

# ---------------------------------------------------------------------------
# Script / command inspection ("script gate")
#
# Hermes's code-execution tool is ``terminal`` and it receives the shell
# command as a ``command`` string; other runtimes use ``cmd`` / ``script`` /
# ``code``. A spend smuggled into such a call is invisible to the arg-key
# checks above, so the command text itself is scanned:
#
# * a money literal prices the spend (normal caps/lists/ledger apply);
# * a payment verb with no readable amount is an *unpriced* spend and is
#   routed to the owner, exactly like an unpriced spend-shaped tool call.
#
# Matching is word-boundary based so everyday text does not trip it
# ("payload" is not "pay"). Posture is fail-closed: a verb found inside a
# quoted grep pattern will gate to the owner rather than sail through —
# see the README's "Script and command inspection" section. One deliberate
# exception: a bare $1-$9 integer is NOT read as a money literal, because
# $1..$9 are shell field/positional references (awk '{print $1}'); a spend
# verb in the same command still gates it to the owner.

COMMAND_TEXT_KEYS = ("command", "cmd", "script", "code")

_SPEND_TEXT_RE = re.compile(
    r"\b(?:pay|paid|paying|payments?|transfers?|purchases?|buy|buying|"
    r"checkout|charges?|charged|sends?|sending|swaps?|withdraws?|"
    r"withdrawals?|tips?|donates?|donations?|subscribes?|subscriptions?)\b",
    re.IGNORECASE,
)
# Explicit amount semantics in text: amount=500, amount: 500, --amount 500,
# "amount_usd": 500. Any value counts — the key itself declares the amount.
_MONEY_KV_RE = re.compile(
    r"(?:amount_usd|amount|cost|price|total)\b[\"']?\s*[:=]?\s*[\"']?"
    r"\$?\s*(\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_MONEY_DOLLAR_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)")
_MONEY_SUFFIX_RE = re.compile(
    r"(\d[\d,]*(?:\.\d{1,2})?)\s*(?:USD|USDT|USDC)\b", re.IGNORECASE
)
_RECIPIENT_KV_RE = re.compile(
    r"(?:recipient|counterparty|payee|destination|merchant|vendor|seller)"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9.@_-]*)",
    re.IGNORECASE,
)
_URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _plausible_money_literal(raw: str) -> bool:
    """True if a $/suffix literal reads as money rather than shell syntax.

    Bare integers under $10 are ignored: ``$1`` .. ``$9`` are awk field
    references and positional parameters, not prices. A decimal point or
    a thousands separator always reads as money.
    """
    if "." in raw or "," in raw:
        return True
    try:
        return int(raw) >= 10
    except ValueError:
        return False


def _command_text(args: dict) -> str:
    """The command/script text a call carries, or "" when it has none."""
    for key in COMMAND_TEXT_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _money_literal_cents(text: str) -> int | None:
    """First/clearest money literal in command text, in USD cents.

    USDT/USDC literals are treated at par with USD. Explicit key=value
    amounts win over bare literals (they declare their own semantics).
    """
    m = _MONEY_KV_RE.search(text)
    if m:
        cents = _to_cents(m.group(1).replace(",", ""))
        if cents is not None:
            return cents
    for pattern in (_MONEY_DOLLAR_RE, _MONEY_SUFFIX_RE):
        for m in pattern.finditer(text):
            if _plausible_money_literal(m.group(1)):
                cents = _to_cents(m.group(1).replace(",", ""))
                if cents is not None:
                    return cents
    return None


def _recipient_in_text(text: str) -> str:
    """A recipient named inside command text, or "" when none is obvious."""
    m = _RECIPIENT_KV_RE.search(text)
    if m:
        return m.group(1)
    m = _URL_HOST_RE.search(text)
    if m:
        return m.group(1)
    return ""


def _extract_command_spend(
    tool_name: str, args: dict, recipient: str
) -> SpendAction | None:
    """Spend detection over command/script text (the script gate).

    Priced when a money literal is readable; unpriced (owner gate) when a
    payment verb appears without one; None when the text shows no spend
    signals at all — benign shell use passes completely untouched.
    """
    text = _command_text(args)
    if not text:
        return None
    cents = _money_literal_cents(text)
    if cents is not None:
        action = SpendAction(
            tool_name, recipient or _recipient_in_text(text), cents
        )
        action.in_script = True  # type: ignore[attr-defined]
        return action
    if _SPEND_TEXT_RE.search(text):
        action = SpendAction(
            tool_name, recipient or _recipient_in_text(text), 0
        )
        action.unpriced = True  # type: ignore[attr-defined]
        action.in_script = True  # type: ignore[attr-defined]
        return action
    return None


def _to_cents(value) -> int | None:
    """Parse a dollar amount (int/float/str) into integer cents, exactly."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if d.is_nan() or d.is_infinite():
        return None
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def extract_spend(
    tool_name: str, args: dict, paid_tools: dict, gate_scripts: bool = True
) -> SpendAction | None:
    """Return a SpendAction if this tool call spends money, else None."""
    if not isinstance(args, dict):
        args = {}

    recipient = ""
    for key in RECIPIENT_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            recipient = v.strip()
            break

    for key in AMOUNT_CENTS_KEYS:
        if key in args and args[key] is not None:
            try:
                return SpendAction(tool_name, recipient, int(args[key]))
            except (TypeError, ValueError):
                return SpendAction(tool_name, recipient, 0)

    for key in AMOUNT_USD_KEYS:
        if key in args and args[key] is not None:
            cents = _to_cents(args[key])
            return SpendAction(tool_name, recipient, cents if cents is not None else 0)

    if tool_name in paid_tools:
        # Fixed-price tool: the tool itself is the counterparty when the
        # call names no recipient.
        return SpendAction(tool_name, recipient or tool_name, int(paid_tools[tool_name]))

    if gate_scripts:
        # A spend written into a shell command / script the agent runs:
        # priced from a money literal in the text, or unpriced when only
        # a payment verb shows. No spend signals -> no opinion, and the
        # tool-name check below still applies.
        command_action = _extract_command_spend(tool_name, args, recipient)
        if command_action is not None:
            return command_action

    lowered = tool_name.lower()
    if any(hint in lowered for hint in SPEND_TOOL_HINTS):
        # Spend-shaped but unpriced: the guard cannot clear it, so it is
        # evaluated as a zero-amount action whose allowlist/amount checks
        # fail closed in evaluate_call below (see _unpriced flag handling).
        action = SpendAction(tool_name, recipient, 0)
        action.unpriced = True  # type: ignore[attr-defined]
        return action

    return None


class DictStorage:
    """Minimal get/set storage (tests and fallback)."""

    def __init__(self, backing: dict | None = None):
        self.backing = backing if backing is not None else {}

    def get(self, key, default=None):
        return self.backing.get(key, default)

    def set(self, key, value):
        self.backing[key] = value


class SpendGuard:
    """The guard: policy + daily ledger + Hermes directive translation."""

    def __init__(
        self,
        policy: GuardPolicy,
        storage,
        paid_tools: dict | None = None,
        clock=None,
        gate_scripts: bool = True,
    ):
        self.policy = policy
        self.storage = storage
        self.paid_tools = paid_tools or {}
        # Script gate: inspect command/script text for spends (see
        # _extract_command_spend). Owners can turn it off in config.
        self.gate_scripts = bool(gate_scripts)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # A freeze set from the slash command persists across restarts and
        # overrides the config default.
        stored_frozen = self.storage.get("frozen", None)
        if stored_frozen is not None:
            self.policy.frozen = bool(stored_frozen)

    # -- ledger ---------------------------------------------------------
    def _today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def spent_today_cents(self) -> int:
        ledger = self.storage.get("ledger", None) or {}
        if ledger.get("day") != self._today():
            return 0
        return int(ledger.get("spent_cents", 0))

    def record_execution(self, tool_name: str, args: dict) -> None:
        """Record an executed spend (called from post_tool_call: the tool
        actually ran, so the money actually moved)."""
        action = extract_spend(
            tool_name, args, self.paid_tools, gate_scripts=self.gate_scripts
        )
        if action is None or action.amount_cents <= 0:
            return
        self.storage.set(
            "ledger",
            {"day": self._today(), "spent_cents": self.spent_today_cents() + action.amount_cents},
        )

    # -- freeze ---------------------------------------------------------
    def set_frozen(self, frozen: bool) -> None:
        self.policy.frozen = frozen
        self.storage.set("frozen", frozen)

    # -- evaluation -----------------------------------------------------
    def evaluate_call(self, tool_name: str, args: dict) -> tuple[dict | None, Evaluation | None]:
        """Return (pre_tool_call directive or None, evaluation or None).

        None directive = Hermes proceeds. A None evaluation means the call
        is not a spend and the guard has no opinion on it.
        """
        action = extract_spend(
            tool_name, args, self.paid_tools, gate_scripts=self.gate_scripts
        )
        if action is None:
            return None, None

        if getattr(action, "unpriced", False):
            # Fail closed: an unpriced spend always routes to the owner,
            # and a frozen guard blocks even that.
            if self.policy.frozen:
                ev = Evaluation(BLOCK, "guard is frozen by the owner; all spends are refused", [])
                return {"action": "block", "message": f"Aegis Spend Guard BLOCKED {tool_name}: {ev.reason}"}, ev
            if getattr(action, "in_script", False):
                ev = Evaluation(
                    WARN,
                    f"spend detected in the command/script text passed to "
                    f"{tool_name}, with no readable amount; the guard cannot "
                    "clear it, so the owner must confirm the launch",
                    [],
                )
            else:
                ev = Evaluation(
                    WARN,
                    f"spend-shaped tool {tool_name} has no readable amount; "
                    "the guard cannot clear it, so the owner must confirm",
                    [],
                )
            return {"action": "approve", "message": f"Aegis Spend Guard: {ev.reason}"}, ev

        ev = evaluate(self.policy, action, self.spent_today_cents())
        if ev.verdict == BLOCK:
            return {
                "action": "block",
                "message": f"Aegis Spend Guard BLOCKED {tool_name} "
                f"({action.amount_cents}c to {action.recipient or 'unknown'}): {ev.reason}",
            }, ev
        if ev.verdict == WARN:
            return {
                "action": "approve",
                "message": f"Aegis Spend Guard needs owner approval for {tool_name} "
                f"({action.amount_cents}c to {action.recipient or 'unknown'}): {ev.reason}",
            }, ev
        return None, ev
