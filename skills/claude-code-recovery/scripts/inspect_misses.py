#!/usr/bin/env python3
"""Inspect post-snapshot Edit misses from a recovery run.

After ``recover.py`` writes its report, run this to see the exact
``old_string`` / ``new_string`` of every Edit that couldn't be replayed.

Usage::

    python3 inspect_misses.py \\
        --transcript-root ~/.claude/projects/-slug-1 \\
        --transcript-root ~/.claude/projects/-slug-2 \\
        --workspace /path/to/workspace \\
        [--file path/relative/to/workspace]

Without ``--file``, prints misses for every dirty file in the recovery report.
With ``--file``, restricts to just that one.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Import the parse_events + helpers from the sibling recover.py.
sys.path.insert(0, str(Path(__file__).parent))
from recover import parse_events, strip_read_output, apply_edit  # type: ignore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript-root", dest="transcript_roots", action="append", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--file", default=None, help="Optional: restrict to a single relative path.")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    prefix = str(workspace) + "/"
    roots = [Path(r).expanduser().resolve() for r in args.transcript_roots]

    targets: set[str] | None = None
    if args.file:
        targets = {args.file}
    else:
        report = workspace / ".recovery_report.json"
        if not report.is_file():
            raise SystemExit(f"no recovery report at {report} — run recover.py first")
        data = json.loads(report.read_text())
        targets = {d["file"] for d in data.get("dirty", [])}
        if not targets:
            print("No dirty files in the recovery report — nothing to inspect.")
            return

    events = parse_events(roots)
    by_file = defaultdict(list)
    for ev in events:
        if ev[2].startswith(prefix):
            rel = ev[2][len(prefix):]
            if rel in targets:
                by_file[rel].append(ev)

    for rel, evs in by_file.items():
        # Re-run replay and capture misses per file, walking through every
        # event in order. This mirrors recover.py's drift-corrected replay.
        last_snap = -1
        for i, (_ts, kind, _fp, inp, is_err, text) in enumerate(evs):
            if is_err:
                continue
            if kind == "Write":
                last_snap = i
            elif kind == "Read" and not inp.get("offset") and not inp.get("limit"):
                if strip_read_output(text) is not None:
                    last_snap = i

        content: str | None = None
        misses: list[tuple[str, str, str, str]] = []
        for i, (ts, kind, _fp, inp, is_err, text) in enumerate(evs):
            if is_err:
                continue
            is_post = i > last_snap
            if kind == "Read":
                if inp.get("offset") or inp.get("limit"):
                    continue
                s = strip_read_output(text)
                if s is not None:
                    content = s
            elif kind == "Write":
                content = inp.get("content", "") or ""
            elif kind == "Edit":
                if content is None:
                    continue
                old = inp.get("old_string", "") or ""
                new = inp.get("new_string", "") or ""
                nc = apply_edit(content, old, new, bool(inp.get("replace_all", False)))
                if nc is None:
                    if is_post:
                        misses.append(("Edit", ts, old, new))
                else:
                    content = nc
            elif kind == "MultiEdit":
                if content is None:
                    continue
                working = content
                ok = True
                fail: tuple | None = None
                for j, ed in enumerate(inp.get("edits", []) or []):
                    old = ed.get("old_string", "") or ""
                    new = ed.get("new_string", "") or ""
                    nc = apply_edit(working, old, new, bool(ed.get("replace_all", False)))
                    if nc is None:
                        ok = False
                        fail = (j, old, new)
                        break
                    working = nc
                if ok:
                    content = working
                elif is_post and fail is not None:
                    misses.append((f"MultiEdit#{fail[0]}", ts, fail[1], fail[2]))

        header = f"=== {rel} — {len(misses)} post-snapshot miss(es) ==="
        print("\n" + "=" * len(header))
        print(header)
        print("=" * len(header))
        for kind, ts, old, new in misses:
            print(f"\n--- {kind} @ {ts} ---")
            print(f"OLD_STRING (len={len(old)}):")
            for l in old.split("\n")[:20]:
                print(f"    {l}")
            if len(old.split("\n")) > 20:
                print("    ...")
            print(f"\nNEW_STRING (len={len(new)}):")
            for l in new.split("\n")[:20]:
                print(f"    {l}")
            if len(new.split("\n")) > 20:
                print("    ...")
            # Diagnostic: is the first line of old_string present on disk?
            disk = (workspace / rel).read_text() if (workspace / rel).is_file() else ""
            first = old.split("\n")[0].strip()
            present = first in disk if first else False
            verbatim = old in disk
            print(f"\n    old_string verbatim in on-disk? {verbatim}")
            print(f"    first line of old_string in on-disk? {present}")


if __name__ == "__main__":
    main()
