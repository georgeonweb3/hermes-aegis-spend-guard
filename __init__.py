"""Aegis Spend Guard — owner-set spend policy for Hermes Agent tool calls.

Wiring only: the policy engine lives in :mod:`.policy` (pure logic, ported
from Aegis on Base and tgp-guard/NearGuard) and the stateful adapter in
:mod:`.guard`. This module registers, through Hermes's public plugin API:

* a ``pre_tool_call`` hook that evaluates every spend-shaped tool call
  before it executes and returns Hermes directives — ``block`` vetoes the
  call, ``approve`` routes it to the owner's human-approval gate;
* a ``post_tool_call`` hook that records executed spends in the daily
  ledger (blocked calls never execute, so they never reach the ledger);
* two tools (``spend_guard_check``, ``spend_guard_status``) so the agent
  can dry-run a spend against policy and report the guard's state;
* a ``/spendguard`` slash command (status / freeze / unfreeze) so the
  owner can work the kill switch from chat.

Disclosure: the guard makes no network calls, runs no shell commands, and
stores only its own policy state and daily totals via ``ctx.state``.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

try:  # package import (Hermes loads plugins as packages)
    from .guard import DictStorage, SpendGuard
    from .policy import GuardPolicy
except ImportError:  # top-level import (tests, direct execution)
    from guard import DictStorage, SpendGuard
    from policy import GuardPolicy

logger = logging.getLogger(__name__)

_guard: SpendGuard | None = None


class _CtxStateStorage:
    """Adapt ctx.state (keyword ``default=``) to the guard's storage API."""

    def __init__(self, state):
        self._state = state

    def get(self, key, default=None):
        try:
            return self._state.get(key, default=default)
        except TypeError:
            value = self._state.get(key)
            return default if value is None else value

    def set(self, key, value):
        self._state.set(key, value)


def _usd_to_cents(value, default_cents: int) -> int:
    try:
        return int((Decimal(str(value)) * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    except (InvalidOperation, ValueError, TypeError):
        return default_cents


def _build_guard(ctx) -> SpendGuard:
    get = ctx.get_config
    policy = GuardPolicy(
        per_tx_cap_cents=_usd_to_cents(get("per_tx_cap_usd", default=25.0), 2500),
        daily_limit_cents=_usd_to_cents(get("daily_limit_usd", default=100.0), 10000),
        warn_bps=int(get("warn_bps", default=8000)),
        frozen=bool(get("frozen", default=False)),
        allowlist=list(get("allowlist", default=[]) or []),
        blocklist=list(get("blocklist", default=[]) or []),
        allowed_tools=list(get("allowed_tools", default=[]) or []),
        unknown_recipient=str(get("unknown_recipient", default="warn")),
    )
    paid_tools_raw = get("paid_tools", default={}) or {}
    paid_tools = {
        str(name): _usd_to_cents(price, 0) for name, price in dict(paid_tools_raw).items()
    }
    try:
        storage = _CtxStateStorage(ctx.state)
    except Exception:
        storage = DictStorage()
    return SpendGuard(policy, storage, paid_tools=paid_tools)


# ---------------------------------------------------------------- hooks
def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Evaluate a tool call before it executes. Fail closed on spends."""
    if _guard is None or not tool_name:
        return None
    try:
        directive, _ev = _guard.evaluate_call(tool_name, args or {})
        return directive
    except Exception:
        logger.exception("aegis-spend-guard: evaluation failed for %s", tool_name)
        # A policy hook that cannot decide must not wave a spend through.
        try:
            from .guard import extract_spend
        except ImportError:
            from guard import extract_spend

        try:
            if extract_spend(tool_name, args or {}, _guard.paid_tools) is not None:
                return {
                    "action": "block",
                    "message": "Aegis Spend Guard could not evaluate this spend; "
                    "blocked (fail closed).",
                }
        except Exception:
            pass
        return None


def _on_post_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Record executed spends in the daily ledger."""
    if _guard is None or not tool_name:
        return None
    try:
        _guard.record_execution(tool_name, args or {})
    except Exception:
        logger.exception("aegis-spend-guard: ledger write failed for %s", tool_name)
    return None


# ---------------------------------------------------------------- tools
def _tool_spend_guard_check(args: dict | None = None, **kwargs) -> str:
    """Dry-run: would this spend be allowed, routed to the owner, or blocked?"""
    merged = dict(args or {})
    merged.update(kwargs)
    if _guard is None:
        return json.dumps({"error": "guard not initialised"})
    tool_name = str(merged.get("tool_name", "hypothetical_spend"))
    call_args = {}
    if merged.get("amount_usd") is not None:
        call_args["amount_usd"] = merged["amount_usd"]
    if merged.get("recipient"):
        call_args["recipient"] = merged["recipient"]
    directive, ev = _guard.evaluate_call(tool_name, call_args)
    if ev is None:
        return json.dumps({"verdict": "not-a-spend", "tool": tool_name})
    return json.dumps(
        {
            "verdict": ev.verdict,
            "reason": ev.reason,
            "directive": directive,
            "checks": [
                {"name": c.name, "verdict": c.verdict, "reason": c.reason} for c in ev.checks
            ],
        }
    )


def _tool_spend_guard_status(args: dict | None = None, **kwargs) -> str:
    if _guard is None:
        return json.dumps({"error": "guard not initialised"})
    p = _guard.policy
    return json.dumps(
        {
            "frozen": p.frozen,
            "per_tx_cap_usd": p.per_tx_cap_cents / 100,
            "daily_limit_usd": p.daily_limit_cents / 100,
            "spent_today_usd": _guard.spent_today_cents() / 100,
            "allowlist": p.allowlist,
            "blocklist": p.blocklist,
            "allowed_tools": p.allowed_tools,
            "unknown_recipient": p.unknown_recipient,
        }
    )


_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_name": {"type": "string", "description": "Tool that would make the spend"},
        "amount_usd": {"type": "number", "description": "Amount in USD"},
        "recipient": {"type": "string", "description": "Who gets paid"},
    },
    "required": ["amount_usd"],
}
_STATUS_SCHEMA = {"type": "object", "properties": {}}


# ------------------------------------------------------------- commands
def _handle_spendguard_command(raw_args: str) -> str:
    if _guard is None:
        return "Aegis Spend Guard is not initialised."
    parts = (raw_args or "").strip().split()
    sub = parts[0].lower() if parts else "status"
    if sub == "freeze":
        _guard.set_frozen(True)
        return "Aegis Spend Guard FROZEN. All agent spends are now blocked."
    if sub == "unfreeze":
        _guard.set_frozen(False)
        return "Aegis Spend Guard unfrozen. Policy checks apply as configured."
    if sub == "status":
        status = json.loads(_tool_spend_guard_status())
        return (
            "Aegis Spend Guard — "
            f"frozen: {status['frozen']}, "
            f"per-tx cap: ${status['per_tx_cap_usd']:.2f}, "
            f"daily limit: ${status['daily_limit_usd']:.2f}, "
            f"spent today: ${status['spent_today_usd']:.2f}, "
            f"allowlist: {status['allowlist'] or 'not enforced'}, "
            f"blocklist: {status['blocklist'] or 'empty'}"
        )
    return "Usage: /spendguard [status|freeze|unfreeze]"


# ------------------------------------------------------------- register
def register(ctx):
    global _guard
    _guard = _build_guard(ctx)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_tool(
        name="spend_guard_check",
        toolset="aegis_spend_guard",
        schema=_CHECK_SCHEMA,
        handler=_tool_spend_guard_check,
    )
    ctx.register_tool(
        name="spend_guard_status",
        toolset="aegis_spend_guard",
        schema=_STATUS_SCHEMA,
        handler=_tool_spend_guard_status,
    )
    ctx.register_command(
        "spendguard",
        _handle_spendguard_command,
        description="Aegis Spend Guard: status, freeze or unfreeze agent spending",
        args_hint="[status|freeze|unfreeze]",
    )
