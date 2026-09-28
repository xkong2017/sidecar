# sidecar

**Durable work that happens when you are not driving.**

Most of the day a capable machine sits idle. A flat-rate subscription window resets whether
or not you used it. `sidecar` spends that capacity on work you already implied you wanted —
and proves each result with a command rather than a summary.

It is an agent skill: a `SKILL.md` plus fifteen standard-library scripts. No dependencies,
Python 3.9+.

```
Mine  ->  Decide  ->  Work  ->  Settle
```

Nothing is inferred about your intent without a place to point at it, and nothing runs unless
a command can prove it finished.

---

## Contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [The four stages](#the-four-stages)
- [The gate, in detail](#the-gate-in-detail)
- [State and files](#state-and-files)
- [Safety design](#safety-design)
- [Install](#install)
- [Calibrating the miner](#calibrating-the-miner)
- [Command reference](#command-reference)
- [Portability](#portability)
- [Honest limits](#honest-limits)
- [Prior art](#prior-art)

---

## Why this exists

An agent only runs when you prompt it. Between prompts the hardware is idle, and on a
subscription the unused part of a rolling window expires worthless. Two observations follow.

1. **You have already stated most of your backlog.** "Let's do that later", an unchecked box in
   a `PLAN.md`, a repo with an uncommitted tree from three weeks ago — these are stated
   intentions, not inferences. They are recoverable from data already on your disk.
2. **Unattended work is only as valuable as its verification.** An agent that reports success
   without anything checking it produces confident, wrong work at scale. Any system that runs
   without a human present must be able to tell "done" from "believed done".

`sidecar` is built around those two claims. The mining half is deliberately weak and honest
about it; the verification half is strict.

---

## Architecture

```
                       LOCAL SOURCES  (read locally, never uploaded)
  ~/.claude/projects/**/*.jsonl     agent turns, tool calls, token usage
  ~/.dsh/sessions/**/*.jsonl.zstd   DSH session transcripts
  ~/projects/**/.git                repo state: dirty tree, plans, CI, tests
  Claude Code status-line payload   first-party rate limits
                       |
                       v
  +--------------------+---------------------+---------------------+
  |                    |                     |                     |
  v                    v                     v                     v
+-----------------+  +------------------+  +------------------+  +------------------+
| sc_mine.py      |  | sc_statusline.py |  | sc_probe.py      |  | sc_quota.py      |
|                 |  |                  |  |                  |  |                  |
| transcripts ->  |  | reads stdout of  |  | load / core      |  | exact 5h / 7d %  |
| git state   ->  |  | Claude Code on   |  | HID idle time    |  | resets_in        |
|                 |  | stdin, records   |  | memory headroom  |  | idle windows     |
| > candidates    |  | rate_limits.json |  | thermal / power  |  | (transcript      |
| > leads         |  |                  |  | GPU + vLLM queue |  |  fallback)       |
| > profile       |  |                  |  |                  |  |                  |
+--------+--------+  +--------+---------+  +--------+---------+  +--------+---------+
         |                    |                     |                     |
         +--------------------+----------+----------+---------------------+
                                         v
                     +---------------------------------------+
                     |          sc_run.py  plan              |
                     |                                       |
                     |  gate 1  machine_free?     <-- probe   |
                     |  gate 2  budget_available? <-- budget  |
                     |  gate 3  has_candidate?    <-- mine    |
                     |  gate 4  quota_ok?         <-- quota   |
                     |                                       |
                     |  all open -> directive + verify_command|
                     |  else     -> reasons[]  (always explains|
                     |                          the refusal)   |
                     +-------------------+-------------------+
                                         v
                     +---------------------------------------+
                     |  sc_cycle.sh     (run deliberately)    |
                     |                                       |
                     |  sc_guard policy  <-- deny paths,      |
                     |                       command patterns  |
                     |        |                              |
                     |        v                              |
                     |  agent: claude | codex | custom        |
                     |         (custom may be a local vLLM)   |
                     |        |                              |
                     |        v                              |
                     |  writes result.json                   |
                     +-------------------+-------------------+
                                         v
                     +---------------------------------------+
                     |  sc_run.py  dispatch                  |
                     |                                       |
                     |  RE-RUNS verify_command itself        |
                     |    pass -> charge ledger, mark done   |
                     |    fail -> recorded as FAILURE even   |
                     |            when the agent said ok     |
                     |  no report -> charged full runtime    |
                     +-------------------+-------------------+
                                         v
              +--------------------------------------------+
              |  human review surface                      |
              |    sc_backlog.py  approve / reject         |
              |    sc_render.py   status.html dashboard    |
              |    sc_eval.py     measured miner precision |
              +--------------------------------------------+
```

**The loop closes on you, not on the agent.** Anything the sidecar wants to do beyond its
standing authority shows up as a proposal, and anything it claims to have done shows up as a
verification result. Both land where a human reads them.

---

## The four stages

### 1. Mine — what might be worth doing

`sc_mine.py` reads two independent sources and keeps them separate, because their
trustworthiness differs.

**Repo state** produces *candidates*. Every one is a mechanical fact with a command that can
prove it:

| Signal | Tier | Runnable verification |
|---|---|---|
| Failing test suite | 1 | the project's own test command |
| Unchecked boxes in a committed plan | 2 | the referenced change is committed |
| Uncommitted work left to rot | 2 | `test -z "$(git status --porcelain)"` |
| No CI configuration | 3 | a workflow file exists |
| No tests detected | 3 | a test dir or runner config exists |
| Documentation predates the code | 4 | *human read — never auto-runnable* |
| Project has gone cold | 4 | *human decision — never auto-runnable* |

**History** produces *leads*. Keyword matching over transcripts is far too blunt to produce work
on its own — a single "later" is not a parked project — so leads are quarantined from the
backlog entirely and carry the path of the transcript they came from plus a `promote_if`
predicate describing what would have to be true for them to become real:

```json
{
  "kind": "possibly_parked",
  "quote": "we choose to skip these issues and use a different/temporary UX for now",
  "count": 3,
  "project": "~/ios/TennisCoachAI",
  "transcript": ".claude/projects/-Users-.../abc123.jsonl",
  "promote_if": "the surrounding session shows a specific unfinished change, and names a file or command that would close it"
}
```

Three filters keep the history signal from drowning in its own noise:

- **Human-turn filter.** Only ~44% of "user"-role turns are human; the rest are skill bodies,
  system notices and pasted tables. They are longer and more keyword-dense than anything a
  person types, so leaving them in corrupts every statistic.
- **Volume caps.** One signal once fired on 37 projects. "This repo is cold" x 37 is a
  directory listing, not a backlog: signals collapse into one item with the full target list.
- **Materiality gates.** "No commits for 90 days" is true of every scratch repo. A project is
  only called cold if it has real history, a dirty tree, or a plan file.

### 2. Decide — should we work right now

`sc_run.py plan` is read-only and answers four questions. If any gate is shut it says exactly
which one and why — a system that silently does nothing is indistinguishable from a broken one.

```
machine_free?      load/core, user idle time, memory headroom, thermal, power, GPU queues
budget_available?  daily/weekly spend, wall clock, task count, minus the reserve
has_candidate?     something approved, with a runnable verification command
quota_ok?          subscription window has headroom (exact if available)
```

When all four are open it emits a **directive**: objective, target, evidence, constraints, the
exact verification command, and the fields the worker must report back.

### 3. Work — executing the directive

`sc_cycle.sh` is the full autonomous cycle and is deliberately *not* what the timer runs. It
resolves whichever agent you have (`claude`, `codex`, or `custom` — including one backed by a
self-hosted vLLM endpoint), passes the directive with its constraints, and requires a JSON
result.

### 4. Settle — did it actually work

`sc_run.py dispatch` re-runs the task's own verification command **itself** and records the raw
output. Claiming success while the command fails is recorded as a failure:

```
claimed_outcome:      "ok"      <- what the agent said
effective_outcome:    "fail"    <- what the command showed
verified:             false
consecutive_failures: 1
```

This is the single most important behaviour in the system. Three consecutive failures trip a
per-task circuit breaker that only a human clears.

---

## The gate, in detail

```
                        sc_run.py plan
                              |
        +---------------------+---------------------+
        v                     v                     v
  +-------------+       +-------------+       +-------------+
  | MACHINE     |       | BUDGET      |       | QUOTA       |
  |             |       |             |       |             |
  | load / core |       | daily $     |       | exact 5h %  |
  | user idle   |       | weekly $    |       |   (or est.) |
  | memory      |       | wall clock  |       | resets_in   |
  | thermal     |       | task count  |       | 7d %        |
  | power       |       | gpu minutes |       |             |
  | GPU queue   |       | x (1-reserve)|      | stale ->drop|
  +------+------+       +------+------+       +------+------+
         |                     |                     |
         +----------+----------+----------+----------+
                    v                     v
            +---------------+     +------------------+
            |  GREEN        |     |  RED / YELLOW    |
            |  heavy work   |     |  explain exactly |
            |  allowed      |     |  which gate shut |
            +---------------+     +------------------+
```

Every gate distinguishes **measured-and-bad** from **not-measured**. On a platform without
`vm_stat` or `ioreg`, the probe reports the signal as unmeasured and warns, rather than
reporting 0% — which would lock the gate shut forever on a machine that is actually fine.

---

## State and files

Everything lives under `$SIDECAR_HOME` (default `~/.sidecar`). All writes are atomic (temp file
+ rename), so a crash never leaves a torn ledger.

```
~/.sidecar/
  config.json          your thresholds and caps (written by sc_init.py)
  status.html          self-contained dashboard (sc_render.py)
  label_sheet.md       labelling sheet for calibration (sc_eval.py)
  logs/plan.log        timer output
  runs/<timestamp>/    one directory per cycle
      plan.json        the verdict and directive
      directive.json   what the agent was told
      policy.json      guard decision
      prompt.txt       the exact prompt
      agent.log        agent stdout/stderr
      result.json      what the agent reported
      dispatch.json    the settled outcome
  state/
      backlog.json         candidates, leads, decisions (tombstones)
      profile.json         redacted preference signals
      ledger.json          spend and runtime totals
      ledger.jsonl         append-only event log (authoritative)
      runs.json            run records
      rate_limits.json     exact quota captured from the status line
      last_probe.json      most recent machine snapshot
      audit.jsonl          policy and verification decisions
      labels.json          your calibration labels
```

`ledger.jsonl` and `audit.jsonl` are append-only: the totals in `ledger.json` are a cache that
can always be rebuilt and audited from the event log.

---

## Safety design

Each of these exists because of a specific failure — most of which I hit while building it.

**Verification or it did not happen.** Dispatch re-runs the check. A run without a passing
command is a failure regardless of what the agent reported.

**Mining never widens its own authority.** A finding spanning 23 repositories is a prompt to
look, not a licence to edit 23 repositories. Approving a bulk finding without naming exactly one
target is refused; naming one target creates a separate single-target item, so the bulk finding
keeps its full list and the approved work gets its own run history and circuit breaker.

**Human decisions survive re-mining.** *(A bug I shipped and fixed.)* Mining only regenerates
its own ids, so an approved single-target item silently vanished on the next run. Decisions and
approvals are now carried forward explicitly, with a regression test.

**Unmeasurable is not idle.** *(A bug I shipped and fixed.)* On a platform without the macOS
tools the probe reported 0% memory and returned red forever. It now reports unmeasured signals
as warnings and explains that the decision rests on fewer signals than you might think.

**Stale readings fail closed.** *(A bug I shipped and fixed.)* Python 3.9 cannot parse the
`-0400` UTC-offset form, so the quota freshness check silently failed *open* and trusted a
12-hour-old reading — which would unlock work on a window already spent. Timestamps are now
written in the colon form, and an unreadable capture time is treated as stale.

**Deny-listed paths are never touched.** Credentials, keys, `.env`, shell history and keychains
are matched by path suffix, so a copy of a secret in an unexpected place is still caught.

**Destructive commands are blocked by pattern**, including force push, hard reset, `git clean`,
`git stash drop`, `rm -rf`, and piping remote content into a shell.

**The reserve is not spendable.** Caps are multiplied by `1 - reserve_fraction`, so a runaway
cannot consume the whole allowance.

**Abandoned runs cost something.** A run that never reports back is charged its full runtime, so
a crashing task trips its breaker instead of retrying invisibly.

**Transcripts never leave the machine.** Everything is read locally, and text is redacted for API
keys, tokens, private keys and `key=value` secrets before anything is written to the profile.

---

## Install

```sh
git clone https://github.com/xkong2017/sidecar.git
cd sidecar

# make it discoverable to your agent
ln -s "$PWD" ~/.claude/skills/sidecar     # Claude Code
ln -s "$PWD" ~/.agents/skills/sidecar     # shared agents dir

python3 scripts/sc_init.py                # probe this machine, write config.json
python3 scripts/sc_mine.py                # build the first backlog
python3 scripts/sc_run.py plan            # see the current verdict
python3 scripts/sc_render.py --open       # open the dashboard
```

`sc_init.py` reads the real core count and discovers local inference endpoints by probing common
ports, so GPU gating works without configuration. It never overwrites an existing config without
`--force`.

### Exact quota (recommended)

Claude Code hands its status-line command a JSON payload containing `rate_limits.five_hour` and
`rate_limits.seven_day` with `used_percentage` and `resets_at`. That is first-party data, and it
counts every machine, the web UI, and mobile — none of which a transcript estimate can see. Add
to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.agents/skills/sidecar/scripts/sc_statusline.py"
  }
}
```

Without it, quota falls back to summing token usage from local transcripts — a real signal, but
one that only sees this machine. Readings older than six hours are ignored.

### Scheduling

```sh
python3 scripts/sc_schedule.py install --every 30
```

The timer runs the read-only plan step and stops. It does **not** execute work: that needs a
model and a target chosen with somebody watching. `sc_cycle.sh` is the full autonomous cycle,
and it is a script you read and run deliberately.

---

## Calibrating the miner

**The mining heuristics shipped here are a starting point, not measured optima.** They were tuned
against a single small corpus, and a generator nobody measures is a generator nobody should
trust. `sc_eval.py` is the smallest tool that makes them falsifiable.

```sh
python3 scripts/sc_eval.py sample --n 20     # writes ~/.sidecar/label_sheet.md
# read the leads, then record verdicts:
python3 scripts/sc_eval.py label <id> real
python3 scripts/sc_eval.py label <id> noise

python3 scripts/sc_eval.py score             # precision / recall / F1 at your threshold
python3 scripts/sc_eval.py sweep             # precision at every threshold
python3 scripts/sc_eval.py words             # which terms track your labels
```

Then tune it without editing code, in `config.json`:

```json
{
  "mining_tuning": {
    "parked_words": ["later", "defer", "blocked", "..."],
    "friction_words": ["still broken", "..."],
    "lead_promotion_threshold": 2,
    "max_leads": 60,
    "max_candidates": 40
  }
}
```

**Suggested workflow:** run `sample`, label 20 leads honestly, run `score`. If precision is below
about 70%, use `words` to drop the noisy terms and `sweep` to pick a threshold. If it is still
bad, the vocabulary is the wrong approach for your corpus, and the LLM interpretation pass
described under [Honest limits](#honest-limits) is the real fix.

---

## Command reference

| Command | Effect |
|---|---|
| `sc_init.py` | probe machine, discover endpoints, write config |
| `sc_mine.py [--lookback-days N]` | rebuild backlog, profile and leads |
| `sc_probe.py` | machine + endpoint availability (exit 0/3/4 = green/yellow/red) |
| `sc_quota.py` | quota burn, idle windows, exact-vs-estimate reconciliation |
| `sc_statusline.py` | Claude Code hook; records `rate_limits` from stdin |
| `sc_run.py plan` | read-only verdict + directive |
| `sc_run.py dispatch ...` | settle a finished run, re-running verification |
| `sc_run.py reconcile` | close out runs that never reported back |
| `sc_backlog.py list/show/approve/reject/reopen/clear-breaker` | review loop |
| `sc_budget.py check/charge/status` | ledger operations |
| `sc_guard.py policy/verify` | policy check, standalone verification |
| `sc_eval.py sample/label/score/words/sweep` | calibrate the miner |
| `sc_render.py [--open]` | write and open `status.html` |
| `sc_schedule.py install/status/uninstall` | launchd timer (cron line printed elsewhere) |
| `sc_cycle.sh` | one full autonomous cycle (`DRY_RUN=1`, `AGENT=claude|codex|custom`) |
| `sc_selftest.py` | invariant checks in a temp sandbox |

---

## Portability

Built and tested on macOS (Apple Silicon). Written to degrade rather than break elsewhere:

| Signal | macOS | Linux | If absent |
|---|---|---|---|
| CPU load | `os.getloadavg` | `os.getloadavg` | warning; gate uses other signals |
| Core count | `os.cpu_count` | same | falls back to `sysctl` |
| Memory | `vm_stat` + `memory_pressure` | `/proc/meminfo` | reported **unmeasured**, not 0% |
| User idle | `ioreg` HID idle time | not available | warning: cannot confirm you are away |
| Thermal / power | `pmset` | `/sys/class/power_supply` | reported unknown |
| GPU utilisation | `ioreg` IOAccelerator | not available | omitted |
| Timer | launchd | cron line printed, never auto-edited | — |

Agent binaries are resolved via `PATH` rather than assuming an install location. Nothing
hardcodes a user path; everything derives from `$HOME`.

---

## Honest limits

**Quota numbers are estimates unless the status-line hook is installed.** Even then they cover
only what Claude Code reports.

**Transcript keyword mining is blunt.** It produces leads — pointer material with a `promote_if`
predicate — not tasks. The filters that make it usable are tuned from one small sample; they are
now externally configurable and measurable (`sc_eval.py`), but **whether they hold up on your
corpus is untested**. Treat leads as a prompt to look, not findings.

**The highest-value missing piece is the LLM interpretation pass over leads.** Each lead carries
its transcript path and a promotion predicate; a local model could read the surrounding session
and promote only those citing a concrete unfinished change. Until that exists, lead quality
depends on keyword heuristics alone. This is the first thing worth building next.

**macOS has no OS-level idle predicate.** launchd has no "run only when idle" key and systemd has
no `ConditionIdle` (Windows Task Scheduler does have `RunOnlyIfIdle`). `sc_probe.py` synthesises
one from heuristics, and the thresholds in `config.json` are defaults, not measured values.

**The sidecar never pushes, publishes, or edits a repository on its own.** It prepares the
decision and records the outcome, and it never widens its own authority.

**Not yet built:** feeding dispatch outcomes back into the profile to reweight future proposals.
Nothing currently learns from what you approved, rejected, or which runs delivered.

---

## Prior art

The individual capabilities here are largely solved, and it is worth being precise about what is
and is not new.

- **Quota sensing** is first-party and commoditised: Claude Code publishes `rate_limits` to status
  lines, and monitors like [ccusage](https://github.com/ccusage/ccusage) (18.8k stars) and
  [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) cover
  display thoroughly. Anthropic also ships **native auto-continue on rate-limit reset**, which
  obsoletes the reactive half of this space.
- **Predicting a user's next task from history** is an ICLR 2025 paper
  ([thunlp/ProactiveAgent](https://github.com/thunlp/ProactiveAgent),
  [arXiv:2410.12361](https://arxiv.org/abs/2410.12361)), extended by
  [PASK](https://arxiv.org/abs/2604.08000) and [ProAct](https://arxiv.org/abs/2605.25971), which
  explicitly frames idle-time compute for agents.
- **Running jobs only when a machine is idle** is decades old — [BOINC](https://boinc.berkeley.edu/)
  has done it since the 2000s.
- **Overnight autonomous agent execution at scale** is proven by
  [ARIS](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep) (16.7k stars).
- **Budget-bounded loops with policy pinning** are formalised in
  [arXiv:2609.12216](https://arxiv.org/abs/2609.12216).
- The **"queue work into leftover quota"** niche specifically is claimed but not won:
  `claude-nightshift` (35 stars), `redline` (6), `ai-schedule` (3), `ccremote` (48, abandoned).

**What this project claims as distinct** is narrower, and is about design rather than capability:

1. **Verification-gated autonomy** — the scheduler re-runs the task's own verification command and
   records a self-reported success on a failing command as a failure. The nearest research analogue
   is simulation-only; the overnight-agent recipes I found trust a self-reported summary.
2. **Anti-authority-widening as a mining invariant** — a finding spanning 23 repositories is a
   prompt to look, not a licence to edit 23 repositories.
3. **The composition**, on one consumer machine, with the personal transcript as the backlog
   source. Each pillar is covered individually; the conjunction is not.

If you are evaluating whether to adopt this: pillars 1 and 3 are things Anthropic may ship at any
time, and the honest framing is "a careful composition with strong safety design", not a novel
capability.

---

## Tests

```sh
python3 scripts/sc_selftest.py
```

Checks covering every invariant above, run in a temp sandbox with its own config and state.
Includes regressions for the bugs that mattered: a stale quota reading silently unlocking work,
re-mining deleting human-approved items, and unmeasurable signals being read as idle.

## License

MIT
