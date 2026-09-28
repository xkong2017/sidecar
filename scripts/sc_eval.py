#!/usr/bin/env python3
"""sc_eval - measure how well mining actually performs against your judgement.

Mining is heuristic, and heuristics drift. This tool closes that loop: label a
handful of leads as real or noise, and it reports precision at the threshold you
are using, per-signal hit rates, and the vocabulary that is pulling its weight.

The premise is that a generator nobody measures is a generator nobody should
trust. This is the smallest tool that makes the mining falsifiable.

  sc_eval.py sample --n 20          # write a labelling sheet to edit by hand
  sc_eval.py score                  # precision/recall against your labels
  sc_eval.py words                  # which keywords track your labels
  sc_eval.py sweep                  # precision at every lead threshold

Labels live in $SIDECAR_HOME/state/labels.json and are never overwritten once
set, so re-running sample only adds new unlabelled leads.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402

BACKLOG = os.path.join(C.STATE_DIR, "backlog.json")
LABELS = os.path.join(C.STATE_DIR, "labels.json")
SHEET = os.path.join(C.SIDECAR_HOME, "label_sheet.md")

VALID = ("real", "noise", "unsure")


def load_labels():
    doc = C.read_json(LABELS, None)
    if not isinstance(doc, dict):
        doc = {"labels": {}}
    doc.setdefault("labels", {})
    return doc


def save_labels(doc):
    C.write_json_atomic(LABELS, doc)


def load_backlog():
    return C.read_json(BACKLOG, {}) or {"candidates": [], "leads": []}


def all_items(backlog):
    """Leads and history-derived candidates are what is worth measuring.

    Repo candidates come from mechanical facts, so they are not a judgement call
    and would flatter the numbers if mixed in.
    """
    items = []
    for lead in backlog.get("leads", []):
        items.append({
            "id": lead["id"], "kind": lead.get("kind", "lead"),
            "quote": lead.get("quote", ""), "count": lead.get("count", 1),
            "project": lead.get("project"), "when": lead.get("when"),
            "transcript": lead.get("transcript"),
        })
    for c in backlog.get("candidates", []):
        if c.get("source") == "history":
            items.append({
                "id": c["id"], "kind": "candidate:T%s" % c.get("tier"),
                "quote": (c.get("evidence") or [""])[0], "count": 1,
                "project": c.get("target"), "when": None,
                "transcript": c.get("transcript"),
            })
    return items


def cmd_sample(args):
    labels = load_labels()
    items = all_items(load_backlog())

    # Stratify by evidence count so the sample is not all one-hit noise, which
    # is where the vocabulary is weakest.
    by_count = collections.defaultdict(list)
    for it in items:
        by_count[min(int(it.get("count", 1)), 4)].append(it)

    picked = []
    per_bucket = max(1, args.n // max(1, len(by_count)))
    for _, bucket in sorted(by_count.items()):
        bucket.sort(key=lambda x: x["id"])
        picked.extend(bucket[:per_bucket])
    if len(picked) < args.n:
        rest = [i for i in items if i not in picked]
        picked.extend(rest[: args.n - len(picked)])

    unlabelled = [i for i in picked if i["id"] not in labels["labels"]][: args.n]
    shown = unlabelled or picked

    lines = [
        "# sidecar labelling sheet", "",
        "Mark each line real (genuinely unfinished work you would act on) or",
        "noise (a keyword accident). Use unsure if it needs reading the transcript.",
        "",
        "Record with:  python3 scripts/sc_eval.py label <id> real|noise|unsure",
        "Then:         python3 scripts/sc_eval.py score",
        "",
        "| # | id | hits | quote | project |",
        "|---|----|------|-------|---------|",
    ]
    for n, it in enumerate(shown, 1):
        quote = (it.get("quote") or "").replace("|", "/").replace("\n", " ")[:110]
        proj = str(it.get("project") or "-").replace("|", "/")[-34:]
        lines.append("| %d | %s | %d | %s | %s |"
                     % (n, it["id"], it.get("count", 1), quote, proj))

    with open(SHEET, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    C.emit({
        "kind": "eval-sample",
        "sheet": SHEET,
        "unlabelled_in_sample": len(unlabelled),
        "total_items": len(items),
        "labelled_so_far": len(labels["labels"]),
        "buckets": {str(k): len(v) for k, v in sorted(by_count.items())},
    })


def cmd_label(args):
    labels = load_labels()
    labels["labels"][args.id] = {"verdict": args.verdict, "at": C.iso()}
    save_labels(labels)
    C.emit({"kind": "eval-label", "id": args.id, "verdict": args.verdict,
            "total_labelled": len(labels["labels"])})


def cmd_score(args):
    labels = load_labels()["labels"]
    items = {i["id"]: i for i in all_items(load_backlog())}

    tp = fp = fn = tn = 0
    per_kind = collections.defaultdict(collections.Counter)
    min_hits = int(C.load_config().get("mining_tuning", {})
                   .get("lead_promotion_threshold", 1))

    for item_id, rec in labels.items():
        verdict = rec.get("verdict")
        if verdict == "unsure":
            continue
        it = items.get(item_id)
        if it is None:
            continue
        # kept mirrors what mining would actually surface at the current
        # threshold, so the numbers describe shipped behaviour.
        kept = int(it.get("count", 1)) >= min_hits
        gold = (verdict == "real")
        if kept and gold:
            tp += 1
            per_kind[it["kind"]]["tp"] += 1
        elif kept and not gold:
            fp += 1
            per_kind[it["kind"]]["fp"] += 1
        elif gold:
            fn += 1
            per_kind[it["kind"]]["fn"] += 1
        else:
            tn += 1
            per_kind[it["kind"]]["tn"] += 1

    kept_total = tp + fp
    labelled = tp + fp + fn + tn
    precision = round(100.0 * tp / kept_total, 1) if kept_total else None
    recall = round(100.0 * tp / (tp + fn), 1) if (tp + fn) else None
    f1 = None
    if precision and recall and (precision + recall):
        f1 = round(2 * precision * recall / (precision + recall), 1)

    if labelled < args.min_labeled:
        verdict_text = ("label at least %d items first (%d so far)"
                        % (args.min_labeled, labelled))
    elif precision is None:
        verdict_text = "nothing kept at this threshold"
    elif precision >= 70:
        verdict_text = "shippable at this threshold"
    elif precision >= 50:
        verdict_text = "marginal: raise lead_promotion_threshold or drop weak words"
    else:
        verdict_text = "too noisy: retune the vocabulary before trusting the backlog"

    C.emit({
        "kind": "eval-score",
        "threshold_used": min_hits,
        "labelled": labelled,
        "kept": kept_total,
        "true_positives": tp, "false_positives": fp,
        "false_negatives": fn, "true_negatives": tn,
        "precision_pct": precision,
        "recall_pct": recall,
        "f1": f1,
        "per_kind": {k: dict(v) for k, v in per_kind.items()},
        "verdict": verdict_text,
    })


def cmd_words(args):
    labels = load_labels()["labels"]
    tune = C.load_config().get("mining_tuning", {})
    words = list(tune.get("parked_words", [])) + list(tune.get("friction_words", []))
    items = {i["id"]: i for i in all_items(load_backlog())}

    real = collections.Counter()
    noise = collections.Counter()
    for item_id, rec in labels.items():
        it = items.get(item_id)
        if not it or rec.get("verdict") == "unsure":
            continue
        low = (it.get("quote") or "").lower()
        bucket = real if rec["verdict"] == "real" else noise
        for w in words:
            if w in low:
                bucket[w] += 1

    rows = []
    for w in sorted(set(real) | set(noise)):
        r, n = real[w], noise[w]
        rows.append({
            "word": w, "real": r, "noise": n,
            "precision_pct": round(100.0 * r / (r + n), 1) if (r + n) else None,
        })
    rows.sort(key=lambda x: (-(x["precision_pct"] or 0), -(x["real"] + x["noise"])))

    C.emit({
        "kind": "eval-words",
        "labelled": len([v for v in labels.values() if v.get("verdict") != "unsure"]),
        "words": rows,
        "note": ("downweight or drop terms with high noise counts, then edit "
                 "config.mining_tuning and re-run score"),
    })


def cmd_sweep(args):
    labels = load_labels()["labels"]
    items = {i["id"]: i for i in all_items(load_backlog())}

    points = []
    for threshold in range(1, 6):
        tp = fp = fn = 0
        for item_id, rec in labels.items():
            if rec.get("verdict") == "unsure":
                continue
            it = items.get(item_id)
            if it is None:
                continue
            kept = int(it.get("count", 1)) >= threshold
            gold = rec["verdict"] == "real"
            if kept and gold:
                tp += 1
            elif kept and not gold:
                fp += 1
            elif gold:
                fn += 1
        kept = tp + fp
        points.append({
            "threshold": threshold,
            "kept": kept,
            "precision_pct": round(100.0 * tp / kept, 1) if kept else None,
            "recall_pct": round(100.0 * tp / (tp + fn), 1) if (tp + fn) else None,
        })

    best = None
    for p in points:
        if p["precision_pct"] is None:
            continue
        score = p["precision_pct"] + (p["recall_pct"] or 0)
        if best is None or score > best[0]:
            best = (score, p)

    C.emit({
        "kind": "eval-sweep",
        "points": points,
        "suggested_threshold": best[1]["threshold"] if best else None,
        "current_threshold": int(C.load_config().get("mining_tuning", {})
                                  .get("lead_promotion_threshold", 1)),
    })


def main():
    ap = argparse.ArgumentParser(description="Measure mining quality against your labels.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sample", help="write a labelling sheet")
    p.add_argument("--n", type=int, default=20)
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("label", help="record one verdict")
    p.add_argument("id")
    p.add_argument("verdict", choices=list(VALID))
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("score", help="precision and recall against your labels")
    p.add_argument("--min-labeled", type=int, default=10)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("words", help="which vocabulary tracks your labels")
    p.set_defaults(func=cmd_words)

    p = sub.add_parser("sweep", help="precision at each lead threshold")
    p.set_defaults(func=cmd_sweep)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
