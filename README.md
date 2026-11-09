# Aegis Spend Guard

**Owner-set spend policy for Hermes Agent, enforced before any spend executes.**

AI agents with paid tools can spend real money on their own initiative. Hermes
v0.23 added price tags and built-in spend limits for paid MCP tools — Aegis
Spend Guard takes the next step: one owner-written policy that vets *every*
spend-shaped tool call, from any tool, before it runs.

The owner sets a per-transaction cap, a daily limit, recipient allow/block
lists and a tool scope. Every spend is checked in `pre_tool_call`:

- **Within policy** → the call proceeds untouched.
- **Borderline** (near a cap, unknown recipient, unlisted tool, or a spend
  with no readable price) → routed to the owner through Hermes's own
  human-approval gate.
- **Over policy** (over the cap, over the daily limit, blocklisted
  recipient, or the guard is frozen) → **blocked outright**, with the reason
  shown to the agent.

The verdict logic is the same worst-wins policy engine the author already
ships as [Aegis](https://github.com/georgeonweb3/aegis-base) (spend control
for AI agents on Base) and in the tgp-guard/NearGuard pre-flight guard:
BLOCK > WARN > ALLOW across frozen / amount / blocklist / per-tx cap /
daily limit / allowlist / tool-scope checks, failing closed whenever a
check cannot clear.

## Install

From the Hermes plugin catalog (once listed):

```bash
hermes plugins install aegis-spend-guard
```

Or from source, into your plugins directory:

```bash
git clone https://github.com/georgeonweb3/hermes-aegis-spend-guard ~/.hermes/plugins/aegis-spend-guard
```

No Python dependencies. Works on Hermes **>= 0.23.0**.

## Configure

Settings live under `plugins.entries.aegis-spend-guard.settings` in
`config.yaml` (or the Desktop Plugins tab):

```yaml
plugins:
  entries:
    aegis-spend-guard:
      settings:
        per_tx_cap_usd: 25.0        # hard cap for a single spend
        daily_limit_usd: 100.0      # hard cap per day (UTC)
        warn_bps: 8000              # route to owner at 80% of a limit
        allowlist:                  # known-good recipients
          - api.openai.com
          - api.anthropic.com
        blocklist:                  # never pay these
          - suspicious-vendor.example
        allowed_tools: []           # empty = any tool may spend
        paid_tools:                 # fixed per-call prices (USD)
          some_paid_search: 0.50
        unknown_recipient: warn     # warn = owner approves; block = strict
        frozen: false               # kill switch
```

### How a call counts as a spend

A tool call is a spend when any of these is true:

1. Its arguments carry an amount — `amount_cents` / `amount_usd` /
   `amount` / `price` / `cost` / `total` / `value` (USD).
2. The tool is in your `paid_tools` map (fixed price per call).
3. The tool name is spend-shaped (`pay`, `transfer`, `purchase`, `buy`,
   `checkout`, `send`, `swap`, `withdraw`, `charge`, ...) but no amount can
   be read — an *unpriced* spend, always routed to the owner. Fail closed.

The recipient is read from `recipient` / `counterparty` / `payee` / `to` /
`address` / `destination` / `merchant` / `vendor` / `seller` arguments.

Executed spends are recorded in a daily ledger (via `ctx.state`, per
profile). Blocked calls never execute, so they never reach the ledger.

## Use

- `/spendguard status` — current policy, today's spend, freeze state.
- `/spendguard freeze` — block **all** agent spends immediately (persists
  across restarts). `/spendguard unfreeze` lifts it.
- Tool `spend_guard_check` — let the agent dry-run a planned spend
  (`tool_name`, `amount_usd`, `recipient`) and see the verdict + per-check
  breakdown before attempting it.
- Tool `spend_guard_status` — the same state as JSON.

## Example policy in action

With the config above:

| Agent tries | Verdict |
|---|---|
| Pay `api.openai.com` $5.00 | ✅ allowed |
| Pay `api.openai.com` $21.00 (84% of the $25 cap) | 🟡 routed to owner |
| Pay `api.openai.com` $30.00 | ⛔ blocked — over per-tx cap |
| Pay `new-vendor.example` $5.00 | 🟡 routed to owner — not on allowlist |
| Pay `suspicious-vendor.example` $1.00 | ⛔ blocked — blocklist |
| Spends reach $96 today, then tries $10 more | ⛔ blocked — daily limit |
| Anything, while frozen | ⛔ blocked |

## Disclosure

- **Network calls:** none. **Shell commands:** none. **Credentials:** none
  read, none stored.
- **Storage:** only the plugin's own state via `ctx.state` — the freeze
  flag and a daily spend total. No telemetry.
- **Approvals:** the guard never approves anything itself. Borderline
  spends are escalated through Hermes's built-in approval gate; under
  unattended runs (cron, gateway) that gate's own timeout behaviour
  applies, so nothing hangs.
- **Known limit:** two spend calls issued in parallel are each checked
  against the ledger as it stands before either executes, so a burst could
  overshoot the daily limit by one call's amount. Sequential agent turns
  (the normal case) are exact.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

24 tests: the pure policy engine (cap, daily limit, freeze, blocklist,
allowlist routing/strict mode, warn zone), the Hermes adapter (directive
translation, ledger, fixed-price tools, unpriced spends), and a wiring
test that loads the plugin the way Hermes does and drives its registered
hooks and commands end-to-end.

## License

MIT. By George Agbe ([@georgebuilding2](https://x.com/georgebuilding2)).
Policy semantics ported from Aegis (Base) and tgp-guard/NearGuard — see
`policy.py` for the lineage notes.
