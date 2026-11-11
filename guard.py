"""Aegis Spend Guard — stateful adapter between Hermes tool calls and the
pure policy engine in :mod:`policy`.

Responsibilities:

* Decide whether a tool call is a *spend* and extract (tool, recipient,
  amount) from its arguments:
    - an explicit amount in the args (``amount_cents`` / ``amount_usd`` /
      ``amount`` / ``price`` / ``cost`` / ``total`` ...) — read ONLY on
      spend tools: tools the owner listed (``allowed_tools`` /
      ``paid_tools`` / ``spend_tools``) or whose name leads with a spend
      verb as a whole segment. The same keys on unrelated tools are not
      money: a scroll ``amount: 3`` is three lines, a form ``value:
      "Blue"`` is text, a preview's negative scroll amount is not a
      negative spend polluting the ledger, or
    - a fixed per-call price from the owner's ``paid_tools`` map, or
    - a spend written into command/script text (a ``command`` / ``cmd`` /
      ``script`` / ``code`` argument — e.g. a ``terminal`` call): a money
      literal prices the spend ONLY when a payment verb also appears in
      the same text; a payment verb with no readable (positive) amount
      makes it an *unpriced* spend, routed to the owner. A bare
      ``$<digits>`` integer is never money (shell field reference).
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


def _name_segments(tool_name: str) -> list[str]:
    """The tool name split into segments on ``_``, ``-`` and ``.``."""
    return [seg for seg in re.split(r"[_\-.]+", str(tool_name).lower()) if seg]


def _has_spend_hint(tool_name: str) -> bool:
    """True when the tool's name *leads* with a spend verb as a whole segment.

    Segment matching, not substring matching: ``pay_api`` and
    ``transfer_tokens`` are spend-shaped, but ``show_tip`` merely displays
    a tip and ``tooltips`` is a UI widget — a hint only counts in the
    leading (verb) segment of the name, never as a substring of another
    word and never in noun position.
    """
    segments = _name_segments(tool_name)
    return bool(segments) and segments[0] in SPEND_TOOL_HINTS


def _is_spend_tool(
    tool_name: str, paid_tools: dict, spend_tools, allowed_tools
) -> bool:
    """True when arg-key amounts on this tool count as money.

    A tool qualifies when the owner listed it — in ``paid_tools``,
    ``spend_tools`` or the policy's ``allowed_tools`` — or its name leads
    with a spend verb (see :func:`_has_spend_hint`). Every other tool's
    ``amount`` / ``value`` / ``price`` / ... arguments are its own
    business (scroll distances, form values, quantities), never a spend.
    """
    return (
        tool_name in paid_tools
        or tool_name in spend_tools
        or tool_name in allowed_tools
        or _has_spend_hint(tool_name)
    )

# ---------------------------------------------------------------------------
# Script / command inspection ("script gate")
#
# Hermes's code-execution tool is ``terminal`` and it receives the shell
# command as a ``command`` string; other runtimes use ``cmd`` / ``script`` /
# ``code``. A spend smuggled into such a call is invisible to the arg-key
# checks above, so the command text itself is scanned:
#
# * a money literal prices the spend, but ONLY when a payment verb
#   also appears in the same text (a bare `total = 0` in code is an
#   assignment, not a spend — the maintainer's Nov 11 review case);
# * a payment verb with no readable amount is an *unpriced* spend and is
#   routed to the owner, exactly like an unpriced spend-shaped tool call.
#   A zero (or negative) parsed amount in command text is never BLOCKED:
#   with a verb it routes to the owner as unpriced, without one it is
#   no opinion at all.
#
# Matching is word-boundary based so everyday text does not trip it
# ("payload" is not "pay"). The verb list is deliberately narrow:
# `send`, `tip`, `charge` and `subscribe` are out — `sock.send(...)`,
# `git send-email` and pub/sub code are ordinary programming, not spends.
# Posture is fail-closed: a kept verb found inside a quoted grep pattern
# will gate to the owner rather than sail through — see the README's
# "Script and command inspection" section. One deliberate exception: a
# bare `$<digits>` integer is NEVER read as a money literal in shell
# text, because `$12` is an awk field reference; only decimals
# (`$12.50`), thousands-separated (`$1,000`) and USD-suffixed literals
# (`100 USDT`) count as money.

COMMAND_TEXT_KEYS = ("command", "cmd", "script", "code")

_SPEND_TEXT_RE = re.compile(
    r"\b(?:pay|paid|paying|payments?|transfers?|purchases?|buy|buying|"
    r"checkout|swaps?|withdraws?|withdrawals?|donates?|donations?)\b",
    re.IGNORECASE,
)
# Explicit amount semantics in text: amount=500, amount: 500, --amount 500,
# "amount_usd": 500. The key must be followed by an explicit separator
# (=, :, or whitespace for a --flag/value form) and a value — a bare
# key with no separator is code, not a spend declaration.
_MONEY_KV_RE = re.compile(
    r"(?:amount_usd|amount|cost|price|total)\b[\"']?\s*(?:[:=]|\s+)\s*[\"']?"
    r"\$?\s*(\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
# A $ literal is money ONLY with a decimal point or a thousands
# separator: $12.50, $1,000. A bare $12 is a shell field reference.
_MONEY_DOLLAR_RE = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d[\d,]*\.\d{1,2})"
)
_MONEY_SUFFIX_RE = re.compile(
    r"(\d[\d,]*(?:\.\d{1,2})?)\s*(?:USD|USDT|USDC)\b", re.IGNORECASE
)
_RECIPIENT_KV_RE = re.compile(
    r"(?:recipient|counterparty|payee|destination|merchant|vendor|seller)"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9.@_-]*)",
    re.IGNORECASE,
)
_URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


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
    A bare `$<digits>` integer is never money (shell field reference),
    so the dollar pattern only matches decimals / thousands-separated
    literals — see ``_MONEY_DOLLAR_RE``.
    """
    m = _MONEY_KV_RE.search(text)
    if m:
        cents = _to_cents(m.group(1).replace(",", ""))
        if cents is not None:
            return cents
    for pattern in (_MONEY_DOLLAR_RE, _MONEY_SUFFIX_RE):
        for m in pattern.finditer(text):
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

    Priced only when a money literal AND a payment verb both appear in
    the text; unpriced (owner gate) when a payment verb appears with no
    readable amount, or with a zero/non-positive amount (a parsed 0 in
    command text is never BLOCKED); None when the text shows no spend
    signals at all — benign shell use passes completely untouched.
    """
    text = _command_text(args)
    if not text:
        return None
    has_verb = bool(_SPEND_TEXT_RE.search(text))
    if not has_verb:
        return None
    cents = _money_literal_cents(text)
    if cents is not None and cents > 0:
        action = SpendAction(
            tool_name, recipient or _recipient_in_text(text), cents
        )
        action.in_script = True  # type: ignore[attr-defined]
        return action
    # Verb present but no positive amount: unpriced, routed to the owner.
    action = SpendAction(
        tool_name, recipient or _recipient_in_text(text), 0
    )
    action.unpriced = True  # type: ignore[attr-defined]
    action.in_script = True  # type: ignore[attr-defined]
    return action


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
    tool_name: str,
    args: dict,
    paid_tools: dict,
    gate_scripts: bool = True,
    spend_tools=None,
    allowed_tools=None,
) -> SpendAction | None:
    """Return a SpendAction if this tool call spends money, else None.

    Arg-key amounts are only read on spend tools (see
    :func:`_is_spend_tool`); on any other tool an ``amount`` / ``value``
    argument — numeric or not — is never treated as money and never
    blocked. The script gate below is separate: it inspects command text
    on any tool, exactly as before.
    """
    if not isinstance(args, dict):
        args = {}
    spend_tools = spend_tools or ()
    allowed_tools = allowed_tools or ()

    recipient = ""
    for key in RECIPIENT_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            recipient = v.strip()
            break

    spend_tool = _is_spend_tool(tool_name, paid_tools, spend_tools, allowed_tools)

    if spend_tool:
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

    if tool_name in spend_tools or _has_spend_hint(tool_name):
        # Spend-shaped but unpriced: the guard cannot clear it, so it is
        # evaluated as a zero-amount action whose allowlist/amount checks
        # fail closed in evaluate_call below (see _unpriced flag handling).
        # The hint test is whole-segment (see _has_spend_hint): display
        # and utility tools whose names merely contain a hint substring
        # or carry it as a noun (show_tip, tooltips) never land here.
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
        spend_tools=None,
    ):
        self.policy = policy
        self.storage = storage
        self.paid_tools = paid_tools or {}
        # Owner-declared spend tools: arg-key amounts on these tools count
        # as money even when the name carries no spend verb (see
        # _is_spend_tool). Together with paid_tools and the policy's
        # allowed_tools, this is the scope of arg-based spend detection.
        self.spend_tools = list(spend_tools or [])
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
            tool_name,
            args,
            self.paid_tools,
            gate_scripts=self.gate_scripts,
            spend_tools=self.spend_tools,
            allowed_tools=self.policy.allowed_tools,
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
            tool_name,
            args,
            self.paid_tools,
            gate_scripts=self.gate_scripts,
            spend_tools=self.spend_tools,
            allowed_tools=self.policy.allowed_tools,
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
