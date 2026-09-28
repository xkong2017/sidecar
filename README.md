# sidecar

Run durable work on this machine during idle time, funded by unused subscription quota.

Most of the day a capable machine sits idle, and a flat-rate subscription window resets
whether or not you used it. sidecar spends that capacity on work you already implied you
wanted — and proves each result with a command rather than a summary.

It is an agent skill: a `SKILL.md` plus fifteen standard-library scripts. No dependencies,
Python 3.9+.

## The loop

```
Mine  →  Decide  →  Work  →  Settle
```

1. **Mine** (`sc_mine.py`) — reads local agent transcripts and git state, writes a
   reviewable backlog. Proposals only; starts nothing.
2. **Decide** (`sc_run.py plan`) — is the machine free? is budget left? which candidate is
   runnable? Read-only, and it always explains a refusal.
3. **Work** — an agent executes the returned directive inside stated constraints.
4. **Settle** (`sc_run.py dispatch`) — re-runs the task's verification command, charges the
   ledger, applies the failure breaker.

## What makes it safe to leave alone

- **Verification or it did not happen.** The scheduler re-runs the task's own verification
  command. An agent that reports success while the command fails is recorded as a *failure*.
- **Mining never widens its own authority.** A finding spanning 23 repositories is a prompt
  to look, not a licence to edit 23 repositories. Approving a bulk finding without naming
  exactly one target is refused.
- **The reserve is not spendable.** Caps are multiplied by `1 - reserve_fraction` so a
  runaway cannot consume the whole allowance.
- **Destructive commands are blocked by pattern** — force push, hard reset, `git clean`,
  stash drop, `rm -rf`, piping remote content to a shell.
- **Failure is bounded.** Repeated failures trip a per-task circuit breaker only a human clears.
- **Abandoned runs cost something.** A run that never reports is charged its full runtime, so a
  crashing task trips its breaker instead of retrying invisibly.
- **Transcripts never leave the machine.** Read locally, redacted before anything is written.

## Install

```sh
# symlink into your agent's skills directory
ln -s "$PWD" ~/.claude/skills/sidecar      # Claude Code
ln -s "$PWD" ~/.agents/skills/sidecar      # shared agents dir

python3 scripts/sc_init.py       # probe this machine, write ~/.sidecar/config.json
python3 scripts/sc_mine.py       # build the first backlog
python3 scripts/sc_run.py plan   # see the current verdict
python3 scripts/sc_render.py --open
```

`sc_init.py` reads the real core count and discovers local inference endpoints by probing
common ports, so GPU gating works without configuration.

### Exact quota (recommended)

Claude Code hands its status-line command a JSON payload containing
`rate_limits.five_hour` and `rate_limits.seven_day`. That is first-party data and counts
every machine, the web UI, and mobile. Add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.agents/skills/sidecar/scripts/sc_statusline.py"
  }
}
```

Without it, quota falls back to summing token usage from local transcripts — a real signal,
but one that only sees this machine.

### Scheduling

```sh
python3 scripts/sc_schedule.py install --every 30   # launchd; plan step only
```

The timer refreshes the verdict and stops. It never executes work, because that needs a model
and a target chosen with somebody watching. `scripts/sc_cycle.sh` is the full autonomous
cycle, and it is a script you read and run deliberately.

## Tests

```sh
python3 scripts/sc_selftest.py
```

38 checks covering every invariant above, run in a temp sandbox. Includes regressions for
bugs that mattered: an unparseable timestamp silently unlocking work on a spent quota
window, and re-mining silently deleting human-approved items.

## Layout

| File | Role |
|---|---|
| `sc_common.py` | config, atomic writes, tolerant time parsing, redaction |
| `sc_probe.py` | machine + remote endpoint availability → green/yellow/red |
| `sc_quota.py` | exact rate-limit capture, transcript fallback, reconciliation |
| `sc_statusline.py` | Claude Code status-line hook recording first-party quota |
| `sc_mine.py` | transcripts + git state → backlog, profile, leads |
| `sc_budget.py` | spend ledger, caps with reserve, circuit breaker |
| `sc_guard.py` | policy denials, path allow/deny, verification gate |
| `sc_run.py` | plan / dispatch / reconcile |
| `sc_backlog.py` | human approve / reject / clear-breaker |
| `sc_render.py` | self-contained HTML dashboard |
| `sc_schedule.py` | launchd timer |
| `sc_cycle.sh` | full autonomous cycle (run deliberately) |
| `sc_selftest.py` | 38 invariant checks |

## Honest limits

- **Quota numbers are estimates** unless the status-line hook is installed.
- **Transcript keyword mining is blunt.** It produces *leads* — unverified pointer material
  with a `promote_if` predicate — not tasks. A single "later" is not a parked project. Only
  a finding with a runnable verification command becomes auto-runnable.
- **macOS has no OS-level idle predicate.** `sc_probe.py` synthesises one from load average,
  HID idle time, memory headroom, thermal state and power source. It is a heuristic, and the
  thresholds in `config.json` are defaults, not measured values.
- **The sidecar never pushes, publishes, or edits a repository on its own.** It prepares the
  decision and records the outcome.
