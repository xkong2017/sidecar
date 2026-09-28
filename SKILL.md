---
name: sidecar
description: Run durable work on this machine during idle time, funded by unused subscription quota. Mines local agent transcripts and git state into a reviewable backlog, gates every run on machine load, budget and approval, and verifies results with a real command. Use when the user wants background or overnight autonomous work, asks what to do with idle GPU/hardware, wants to mine their own session history for parked projects, wants to know how much of a subscription window is left, or asks why autonomous work did or did not run. Triggers on "run work in the background", "use my idle machine", "what should I work on next", "mine my history", "overnight agent", "sidecar".
---

# Sidecar

Durable work that happens when you are not driving.

The machine is idle most of the day; a subscription window resets whether or not
you used it. Sidecar spends that capacity on work you already implied you wanted,
and proves each result with a command rather than a summary.

## The loop

Four stages. Each writes a file in `$SIDECAR_HOME` (default `~/.sidecar`), and
each can be run alone.

1. **Mine** `sc_mine.py` — reads local transcripts and git state, writes
   `state/backlog.json` and `state/profile.json`. Proposals only; starts nothing.
2. **Decide** `sc_run.py plan` — asks whether the machine is free, whether budget
   remains, and which candidate is runnable. Read-only; writes `state/last_probe.json`.
3. **Work** — you or an agent executes `directive` from the plan, inside the
   stated constraints.
4. **Settle** `sc_run.py dispatch` — re-runs the verification command, charges the
   ledger, and applies the failure breaker.

Run `python3 scripts/sc_selftest.py` after changing any script. It asserts the
invariants that keep unattended work safe, in a temp sandbox.

**Before trusting the miner, calibrate it.** The keyword heuristics ship as a
starting point tuned on one small corpus, not measured optima. `sc_eval.py` turns
that into a number:

```sh
python3 scripts/sc_eval.py sample --n 20    # label leads real / noise
python3 scripts/sc_eval.py score            # precision, recall, F1
python3 scripts/sc_eval.py sweep            # best lead threshold
python3 scripts/sc_eval.py words            # which terms are noise
```

Then tune `mining_tuning` in config.json rather than editing code. Below about
70% precision, treat the backlog as unvalidated.

## First run

```sh
python3 scripts/sc_init.py          # probe the machine, write config.json
python3 scripts/sc_mine.py          # build the first backlog
python3 scripts/sc_run.py plan      # see the current verdict
python3 scripts/sc_render.py --open # open the dashboard
```

`sc_init.py` sets core count from the real machine and discovers local inference
endpoints by probing common ports, so GPU gating works without configuration. It
never overwrites an existing config without `--force`.

## Reviewing proposals

Mining never widens its own authority. A finding covering 23 repositories is a
prompt to look, not a licence to edit 23 repositories.

```sh
python3 scripts/sc_backlog.py list                    # everything awaiting a decision
python3 scripts/sc_backlog.py show <id>               # evidence for one item
python3 scripts/sc_backlog.py approve <id> --target <path>   # make one target runnable
python3 scripts/sc_backlog.py reject <id> --reason "..."
python3 scripts/sc_backlog.py clear-breaker <id>      # allow a repeatedly-failing task to retry
```

Approving a bulk item without `--target` is refused, and approving one target
creates a separate single-target item so the bulk finding keeps its full list.
Rejected items are never resurrected by a later mining run.

## Working a directive

When `sc_run.py plan` reports `go: true` it returns a `directive`. Hand that
to an agent (Claude Code, Codex, or a local vLLM endpoint) and require the
`must_report` fields back. Then:

```sh
python3 scripts/sc_run.py dispatch \
  --task-id <id> --outcome ok \
  --verify-cmd "<the task's verify_with>" --cwd <target> \
  --minutes <wall clock> --usd <spend, 0 for a local model> \
  --files <n> --lines <n> --summary "<one line>"
```

`dispatch` re-runs the verification command itself. An agent that reports
success while the command fails is recorded as a **failure**, not a success.
That is the single most important behaviour here: without it the system
accumulates confident, wrong work.

## Scheduling

Install the timer so the cycle runs without you:

```sh
python3 scripts/sc_schedule.py install --every 30    # launchd, every 30 minutes
python3 scripts/sc_schedule.py status
python3 scripts/sc_schedule.py uninstall
```

The timer runs `sc_run.py plan` and stops. It does not execute work by itself —
that needs an agent, and an agent needs a decision about which model to spend.
Wire `sc_cycle.sh` to your agent of choice if you want full autonomy.

## Exact quota, not a guess

Claude Code hands its status-line command a JSON payload containing
`rate_limits.five_hour` and `rate_limits.seven_day` with `used_percentage` and
`resets_at`. That is first-party data and it counts every machine, the web UI
and mobile - none of which a transcript estimate can see.

Register the capture once, in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.agents/skills/sidecar/scripts/sc_statusline.py"
  }
}
```

`sc_statusline.py` records each payload to `state/rate_limits.json` and prints a
one-line status. `sc_run.py plan` then prefers that reading over its own estimate,
and ignores it once it is more than six hours old. If the capture time cannot be
read at all, the reading is discarded rather than trusted: a stale percentage
would otherwise unlock work on a window that is already spent.

Without this, quota accounting falls back to summing token usage from local
transcripts. That is a genuine signal, but it only sees this machine.

## Why nothing is happening

`sc_run.py plan` always explains its refusal in `reasons`. The gates are:

| Gate | Fails when |
|---|---|
| `machine_free` | load per core, user activity, memory headroom, thermal, or a busy inference endpoint |
| `budget_available` | daily or weekly spend, wall clock, task count, or the reserve is reached |
| `has_candidate` | nothing is approved, or everything left needs a human decision |
| `quota_ok` | the subscription window is spent (only checked when a ceiling is configured) |

A red verdict is normal and correct while you are at the keyboard. The GPU
utilization number, the memory estimate and the endpoint queue depths are all on
the dashboard, alongside the runs and their verification output.

## Invariants

These hold everywhere in the scripts and should not be relaxed:

- **Verification or it did not happen.** A run without a passing verification
  command is a failure.
- **The reserve is not spendable.** Budget caps are multiplied by
  `(1 - reserve_fraction)` so a runaway cannot consume the whole allowance.
- **Deny-listed paths are never touched.** Credentials, keys, `.env` and shell
  history are matched by suffix, so a copy of a secret in an unexpected place is
  still refused.
- **Destructive commands are blocked by pattern**, including force push, hard
  reset, `git clean`, stash drop, `rm -rf` and piping remote content to a shell.
- **Failure is bounded.** Repeated failures trip a per-task breaker that only a
  human clears.
- **Abandoned runs cost something.** A run that never reports is charged its full
  runtime, so a crashing task trips its breaker instead of retrying forever.
- **Transcripts never leave the machine.** Everything is read locally and
  redacted before it is written to the profile.

## Files

```
scripts/sc_common.py     config, atomic writes, redaction helpers
scripts/sc_probe.py      machine + endpoint availability
scripts/sc_mine.py       transcripts and git state -> backlog + profile
scripts/sc_budget.py     spend ledger, caps, circuit breaker
scripts/sc_quota.py      subscription window reconstruction
scripts/sc_guard.py      policy denials and verification gates
scripts/sc_run.py        plan / dispatch / reconcile
scripts/sc_backlog.py    human approval loop
scripts/sc_render.py     self-contained status dashboard
scripts/sc_schedule.py   launchd timer
scripts/sc_eval.py      miner calibration: labels, precision, threshold sweep
scripts/sc_selftest.py   invariant checks
```

## Limits worth knowing

- Quota numbers are **estimates from local transcripts**, not billing data. Web
  UI, mobile and other machines are invisible to them. Set
  `quota.providers[].window_token_ceiling` to a real plan cap or headroom is
  reported as unknown.
- Transcript keyword mining produces **leads, not tasks**. A single "later" is
  not a parked project. Leads carry their transcript path so an LLM pass can read
  the surrounding session before anything is promoted.
- The sidecar never pushes, never publishes, and never edits a repository on its
  own. It prepares the decision and records the outcome.