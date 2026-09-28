#!/bin/bash
# sc_cycle.sh - one full autonomous cycle: decide, work, verify, settle.
#
# This is the step that actually spends money and edits files, so it is a shell
# script you read and run deliberately rather than a timer that starts itself.
# Everything it can do is bounded by ~/.sidecar/config.json.
#
#   ./sc_cycle.sh                  # default agent (claude)
#   AGENT=codex ./sc_cycle.sh      # OpenAI Codex
#   DRY_RUN=1 ./sc_cycle.sh        # decide and print the directive, do nothing
#
# Exit codes: 0 worked, 1 error, 2 nothing to do right now.

set -uo pipefail

SIDECAR_HOME="${SIDECAR_HOME:-$HOME/.sidecar}"
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
AGENT="${AGENT:-claude}"
DRY_RUN="${DRY_RUN:-0}"
WORK_ROOT="${WORK_ROOT:-$SIDECAR_HOME/work}"

mkdir -p "$WORK_ROOT" "$SIDECAR_HOME/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$WORK_ROOT/$STAMP"
mkdir -p "$RUN_DIR"

log() { echo "[$(date '+%F %T')] $*"; }

# ---------------------------------------------------------------- 1. decide
log "planning"
PLAN="$RUN_DIR/plan.json"
if ! "$PY" "$SCRIPTS/sc_run.py" plan > "$PLAN" 2> "$RUN_DIR/plan.err"; then
  log "plan failed"; cat "$RUN_DIR/plan.err" >&2; exit 1
fi

# One field per call. A single read would split on spaces, and both paths and
# verification commands contain them.
jget() { "$PY" -c "import json,sys
d = json.load(open(sys.argv[1]))
k = sys.argv[2]
v = (d.get('task') or {}).get(k[5:]) if k.startswith('task.') else d.get(k)
print('' if v is None else ('True' if v is True else 'False' if v is False else v))" "$1" "$2"; }

GO="$(jget "$PLAN" go)"
TASK="$(jget "$PLAN" task.id)"
TARGET="$(jget "$PLAN" task.target)"

if [ "$GO" != "True" ]; then
  log "not working now:"
  "$PY" -c "import json,sys;print(chr(10).join('  - '+r for r in json.load(open(sys.argv[1]))['reasons']))" "$PLAN"
  exit 2
fi

log "go: $TASK"

# ------------------------------------------------------------------ 2. guard
DIRECTIVE="$RUN_DIR/directive.json"
"$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));json.dump(d['directive'],open(sys.argv[2],'w'),indent=2)" "$PLAN" "$DIRECTIVE"

POLICY_ARGS=()
if [ -n "$TARGET" ]; then
  POLICY_ARGS=(--target "$TARGET")
fi
if ! "$PY" "$SCRIPTS/sc_guard.py" policy "${POLICY_ARGS[@]}" > "$RUN_DIR/policy.json"; then
  log "policy refused this target:"
  cat "$RUN_DIR/policy.json"
  exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
  log "dry run - directive written to $DIRECTIVE, nothing executed"
  exit 0
fi

# ------------------------------------------------------------------- 3. work
PROMPT_FILE="$RUN_DIR/prompt.txt"
cat > "$PROMPT_FILE" <<PROMPT
You are running unattended as a background worker. Nobody is watching. Follow the
directive below exactly, and stop when it is satisfied.

Rules:
- Make the smallest change that resolves the finding. Do not refactor around it.
- Never force push, hard reset, git clean, or delete anything outside the target.
- Do not install global dependencies or publish anything.
- If the finding is wrong, already fixed, or needs a human decision, stop and say so.
- When finished, write a JSON result to $RUN_DIR/result.json with exactly:
    files_changed (int), lines_changed (int), verify_command (string),
    verify_exit_code (int), blocked_on (string or null),
    usd_estimate (number), summary (string, one line)

DIRECTIVE:
$(cat "$DIRECTIVE")
PROMPT

# Resolve the agent binary through PATH. Hardcoding ~/.local/bin assumed one
# particular installer; the point of this script is to drive whichever agent
# the operator actually has.
case "$AGENT" in
  claude) CLAUDE_BIN="$(command -v claude || true)"
          if [ -z "$CLAUDE_BIN" ]; then log "claude not found on PATH"; exit 1; fi ;;
  codex)  CODEX_BIN="$(command -v codex || true)"
          if [ -z "$CODEX_BIN" ]; then log "codex not found on PATH"; exit 1; fi ;;
esac

STARTED=$(date +%s)
log "starting agent: $AGENT"
case "$AGENT" in
  claude)
    "$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
      --permission-mode acceptEdits \
      "${POLICY_ARGS[@]/#--target=/--add-dir=}" \
      > "$RUN_DIR/agent.log" 2>&1 || true
    ;;
  codex)
    "$CODEX_BIN" exec "$(cat "$PROMPT_FILE")" \
      > "$RUN_DIR/agent.log" 2>&1 || true
    ;;
  custom)
    # Point AGENT_CMD at any agent CLI, including one backed by a self-hosted
    # vLLM endpoint. The token PROMPT in the command is replaced by the prompt
    # file path; otherwise the path is appended as the final argument.
    if [ -z "${AGENT_CMD:-}" ]; then
      log "AGENT=custom requires AGENT_CMD to be set"; exit 1
    fi
    CMD="$AGENT_CMD"
    case "$CMD" in
      *PROMPT*) CMD="${CMD//PROMPT/$PROMPT_FILE}" ;;
      *)        CMD="$CMD $PROMPT_FILE" ;;
    esac
    log "running custom agent: $CMD"
    # Export the paths so a custom agent never has to parse them out of a
    # prompt string.
    export SIDECAR_RUN_DIR="$RUN_DIR"
    export SIDECAR_DIRECTIVE="$DIRECTIVE"
    export SIDECAR_TARGET="$TARGET"
    export SIDECAR_PLAN="$PLAN"
    /bin/sh -lc "$CMD" > "$RUN_DIR/agent.log" 2>&1 || true
    ;;
  *)
    log "unknown AGENT '$AGENT' (expected claude, codex, or custom)"; exit 1
    ;;
esac
ELAPSED="$(date +%s)"
ELAPSED="$("$PY" -c "print(round(($ELAPSED - $STARTED)/60.0, 1))")"
log "agent finished after $ELAPSED min, see $RUN_DIR/agent.log"

# ------------------------------------------------------------- 4. dispatch
if [ ! -f "$RUN_DIR/result.json" ]; then
  log "agent produced no result.json - recording an abort"
  "$PY" "$SCRIPTS/sc_run.py" dispatch --task-id "$TASK" --outcome abort \
    --minutes "$ELAPSED" --note "agent produced no result.json" | tail -20
  exit 1
fi

rget() { "$PY" -c "import json,sys
r = json.load(open(sys.argv[1]))
v = r.get(sys.argv[2])
v = sys.argv[3] if (v is None or v == '') else v
print(str(v).replace(chr(10), ' '))" "$RUN_DIR/result.json" "$1" "$2"; }

VERIFY_CMD="$(rget verify_command -)"
FILES="$(rget files_changed 0)"
LINES="$(rget lines_changed 0)"
USD="$(rget usd_estimate 0)"
SUMMARY="$(rget summary -)"

log "dispatching; verification command: $VERIFY_CMD"

# dispatch re-runs the verification itself. Claiming outcome=ok here is only a
# claim; dispatch downgrades it to a failure if the command does not pass.
"$PY" "$SCRIPTS/sc_run.py" dispatch \
  --task-id "$TASK" \
  --outcome ok \
  --verify-cmd "$VERIFY_CMD" \
  --cwd "$TARGET" \
  --files "$FILES" --lines "$LINES" \
  --usd "$USD" --minutes "$ELAPSED" \
  --summary "$SUMMARY" \
  --note "cycle $STAMP" \
  | tee "$RUN_DIR/dispatch.json" | tail -30

"$PY" "$SCRIPTS/sc_render.py" > /dev/null 2>&1 || true
log "cycle complete: $RUN_DIR"