"""Aegis policy engine — pure logic, no Hermes imports.

This is the heart of the spend guard. It is a direct port of the policy
semantics George Agbe already ships in two places:

* **Aegis** (spend control for AI agents on Base,
  github.com/georgeonweb3/aegis-base): a hard per-transaction cap, a hard
  daily cap, and an owner freeze that refuses everything.
* **tgp-guard / NearGuard** (Telegraph pre-flight guard, ported to NEAR):
  verdicts ALLOW / WARN / BLOCK where the overall verdict is the WORST of
  the sub-checks (BLOCK > WARN > ALLOW); a blocklist hit is a hard BLOCK;
  a check that cannot clear (unknown counterparty, unlisted method) is a
  WARN — fail closed, never silently pass. Here WARN is surfaced to the
  owner through Hermes's human-approval gate ("route to owner").

All money is integer USD cents. No floats anywhere near money.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALLOW = "allow"
WARN = "warn"  # route to owner for approval
BLOCK = "block"

_RANK = {ALLOW: 0, WARN: 1, BLOCK: 2}


@dataclass
class GuardPolicy:
    """Owner-set spend policy."""

    per_tx_cap_cents: int = 2500  # $25.00 max for a single spend
    daily_limit_cents: int = 10000  # $100.00 max per day
    warn_bps: int = 8000  # warn-zone threshold: 80% of a limit
    frozen: bool = False  # owner kill switch: refuse everything
    allowlist: list = field(default_factory=list)  # known-good recipients
    blocklist: list = field(default_factory=list)  # known-bad recipients
    allowed_tools: list = field(default_factory=list)  # spend-tool scope
    # What to do with a recipient that is NOT on a non-empty allowlist:
    # "warn" (route to owner, the tgp-guard default) or "block" (strict).
    unknown_recipient: str = "warn"


@dataclass
class SpendAction:
    tool: str  # the tool being called, e.g. "stripe_charge"
    recipient: str  # counterparty, "" when unknown
    amount_cents: int  # amount about to be spent


@dataclass
class CheckResult:
    name: str
    verdict: str
    reason: str


@dataclass
class Evaluation:
    verdict: str
    reason: str
    checks: list


def _pct(part: int, whole: int) -> int:
    if whole == 0:
        return 0
    return (part * 10000 // whole) // 100


def evaluate(policy: GuardPolicy, action: SpendAction, spent_today_cents: int) -> Evaluation:
    """Evaluate one proposed spend against the policy.

    Worst-wins aggregation across all sub-checks, exactly like
    tgp-guard's preflight and the NearGuard port.
    """
    checks: list[CheckResult] = []
    amount = action.amount_cents
    warn_bps = int(policy.warn_bps)

    # 1. Freeze: Aegis semantics — a frozen guard refuses everything.
    checks.append(
        CheckResult(
            "frozen",
            BLOCK if policy.frozen else ALLOW,
            "guard is frozen by the owner; all spends are refused"
            if policy.frozen
            else "guard is not frozen",
        )
    )

    # 2. Amount sanity: a non-positive amount never proceeds.
    checks.append(
        CheckResult(
            "amount",
            BLOCK if amount <= 0 else ALLOW,
            "amount must be a positive number of cents"
            if amount <= 0
            else "amount is well-formed",
        )
    )

    # 3. Blocklist: a list hit is a hard BLOCK.
    checks.append(
        CheckResult(
            "blocklist",
            BLOCK if action.recipient and action.recipient in policy.blocklist else ALLOW,
            f"recipient {action.recipient} is on the blocklist (known-bad)"
            if action.recipient and action.recipient in policy.blocklist
            else "recipient is not on the blocklist",
        )
    )

    # 4. Per-transaction cap: Aegis hard cap, with a warn zone below it.
    if amount > 0:
        if amount > policy.per_tx_cap_cents:
            checks.append(
                CheckResult(
                    "per-tx-cap",
                    BLOCK,
                    f"amount {amount}c exceeds the per-transaction cap "
                    f"{policy.per_tx_cap_cents}c",
                )
            )
        elif policy.per_tx_cap_cents > 0 and amount * 10000 >= policy.per_tx_cap_cents * warn_bps:
            checks.append(
                CheckResult(
                    "per-tx-cap",
                    WARN,
                    f"single spend is at {_pct(amount, policy.per_tx_cap_cents)}% "
                    "of the per-transaction cap",
                )
            )
        else:
            checks.append(
                CheckResult("per-tx-cap", ALLOW, "amount is within the per-transaction cap")
            )

    # 5. Daily limit: Aegis hard daily cap over the running day total.
    if amount > 0:
        projected = spent_today_cents + amount
        if projected > policy.daily_limit_cents:
            checks.append(
                CheckResult(
                    "daily-limit",
                    BLOCK,
                    f"spending {amount}c would take today's total to {projected}c, "
                    f"over the daily limit {policy.daily_limit_cents}c "
                    f"(already spent {spent_today_cents}c)",
                )
            )
        elif policy.daily_limit_cents > 0 and projected * 10000 >= policy.daily_limit_cents * warn_bps:
            checks.append(
                CheckResult(
                    "daily-limit",
                    WARN,
                    f"this spend takes today's total to "
                    f"{_pct(projected, policy.daily_limit_cents)}% of the daily limit",
                )
            )
        else:
            checks.append(
                CheckResult("daily-limit", ALLOW, "amount is within the daily limit")
            )

    # 6. Allowlist: fail-closed for unknown recipients, mirroring
    #    tgp-guard's "cannot clear -> HOLD (route to a human)".
    if policy.allowlist:
        if action.recipient and action.recipient in policy.allowlist:
            checks.append(CheckResult("allowlist", ALLOW, "recipient is on the allowlist"))
        else:
            strict = policy.unknown_recipient == "block"
            who = action.recipient or "(unknown recipient)"
            checks.append(
                CheckResult(
                    "allowlist",
                    BLOCK if strict else WARN,
                    f"recipient {who} is not on the allowlist; "
                    + ("blocked by strict policy" if strict else "the owner must confirm"),
                )
            )
    else:
        checks.append(
            CheckResult("allowlist", ALLOW, "allowlist not enforced (empty)")
        )

    # 7. Tool scope: the off-chain twin of a function-call access key's
    #    method whitelist. Unlisted spend tools warn instead of passing.
    if policy.allowed_tools:
        checks.append(
            CheckResult(
                "tool-scope",
                ALLOW if action.tool in policy.allowed_tools else WARN,
                f"tool {action.tool} is in the allowed-tool list"
                if action.tool in policy.allowed_tools
                else f"tool {action.tool} is not in the owner's allowed-tool list; "
                "the owner must confirm",
            )
        )
    else:
        checks.append(
            CheckResult("tool-scope", ALLOW, "tool list not enforced (empty)")
        )

    verdict = ALLOW
    for c in checks:
        if _RANK[c.verdict] > _RANK[verdict]:
            verdict = c.verdict
    reason = (
        "all checks passed"
        if verdict == ALLOW
        else "; ".join(c.reason for c in checks if c.verdict == verdict)
    )
    return Evaluation(verdict=verdict, reason=reason, checks=checks)
