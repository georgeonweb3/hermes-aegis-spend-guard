# Aegis Spend Guard

> **Status:** live and merged, including the script gate (Nov 10, 2026) — spends hidden inside shell commands are scanned, priced and capped too. Catalog PR: NousResearch/hermes-agent#151917.

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
        gate_scripts: true          # inspect shell commands/scripts for spends
```

### How a call counts as a spend

A tool call is a spend when any of these is true:

1. Its arguments carry an amount — `amount_cents` / `amount_usd` /
   `amount` / `price` / `cost` / `total` / `value` (USD).
2. The tool is in your `paid_tools` map (fixed price per call).
3. The tool name is spend-shaped (`pay`, `transfer`, `purchase`, `buy`,
   `checkout`, `send`, `swap`, `withdraw`, `charge`, ...) but no amount can
   be read — an *unpriced* spend, always routed to the owner. Fail closed.
4. The call carries command/script text (a `command` / `cmd` / `script` /
   `code` argument — how Hermes's `terminal` tool and code runners take
   their input) and the text itself shows a spend: a money literal prices
   it, a payment verb without one makes it unpriced. See below. This is
   the **script gate** (`gate_scripts`, on by default).

The recipient is read from `recipient` / `counterparty` / `payee` / `to` /
`address` / `destination` / `merchant` / `vendor` / `seller` arguments.

Executed spends are recorded in a daily ledger (via `ctx.state`, per
profile). Blocked calls never execute, so they never reach the ledger.

### Script and command inspection

Direct tool calls are not the only way an agent spends. It can also write
the payment into a shell command or a script and run that — the launch call
(`terminal` with a `command` string, a code cell, any tool that smuggles
command text) carries no amount argument, so arg-key checks alone would
wave it through. With `gate_scripts` on (the default), the guard scans the
command text of every call, whatever the tool is called:

- **A money literal prices the spend.** `$500`, `$24.99`, `500 USD`,
  `100 USDT`, `amount=500`, `--amount 500`, `"amount_usd": 500` — the
  first/clearest literal becomes the amount (USDT/USDC at par with USD),
  and the recipient is read from the text when one is obvious (a
  `recipient=` style key, else the URL's host). Priced script spends go
  through the full policy: per-tx cap, daily limit, allow/blocklists,
  freeze — and executed ones land in the daily ledger like any spend.
- **A payment verb with no readable amount routes to the owner.** `pay`,
  `transfer`, `charge`, `checkout`, `withdraw`, ... as whole words: the
  guard cannot price the spend, so — same fail-closed rule as unpriced
  tool calls — the launch goes to the owner's approval gate with a
  message saying the spend was detected in the command/script text.
  While frozen, script spends are blocked outright, priced or not.
- **No spend signals, no opinion.** `ls`, `git status`, `npm test`,
  builds, doc fetches and the rest of everyday shell use pass completely
  untouched. Verb matching is word-boundary based (`payload` is not
  `pay`), and a bare `$1`–`$9` is not read as money — those are shell
  field/positional references (`awk '{print $1}'`).

The posture is deliberately fail-closed: a payment verb inside a quoted
grep pattern (`grep -r "charge" src/`) will gate to the owner rather than
pass silently. Owners who would rather never gate shell use can set
`gate_scripts: false`.

**The honest boundary:** the guard prices what the *launch text* shows.
A spend constructed fully dynamically inside a script — amount computed
at runtime, fetched from a file, or assembled so no payment verb or money
literal appears in the command that launches it — cannot be priced from
the launch call. What the owner approves in that case is the launch
itself. Closing that gap would require instrumenting the script's
runtime, which a `pre_tool_call` plugin cannot do; the script gate covers
the spends that are visible in the text the agent actually submits.

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
| Runs `curl api.stripe.com/v1/charges -d '{"amount_usd": 500}'` | ⛔ blocked — $500 priced from the command text, over cap |
| Runs `curl api.stripe.com/v1/charges` with no amount anywhere | 🟡 routed to owner — payment verb in script, unpriced |
| Runs `git status`, `npm test`, `awk '{print $1}'` | ✅ allowed — no spend signals |
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

34 tests: the pure policy engine (cap, daily limit, freeze, blocklist,
allowlist routing/strict mode, warn zone), the Hermes adapter (directive
translation, ledger, fixed-price tools, unpriced spends), the script gate
(priced literals in command text, verb-only owner routing, frozen blocks,
benign commands untouched, `gate_scripts` off switch), and a wiring test
that loads the plugin the way Hermes does and drives its registered hooks
and commands end-to-end.

## License

MIT. By George Agbe ([@georgebuilding2](https://x.com/georgebuilding2)).
Policy semantics ported from Aegis (Base) and tgp-guard/NearGuard — see
`policy.py` for the lineage notes.
