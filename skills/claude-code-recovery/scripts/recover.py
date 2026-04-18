#!/usr/bin/env python3
"""Claude Code session recovery via transcript replay.

Reconstructs files in a workspace by ingesting all ``*.jsonl`` transcripts under
one or more ``~/.claude/projects/<encoded-cwd>/`` roots, deduplicating tool
events, and replaying ``Write``/``Edit``/``MultiEdit`` operations in timestamp
order.

Usage::

    python3 recover.py \\
        --transcript-root ~/.claude/projects/-path-slug-1 \\
        --transcript-root ~/.claude/projects/-path-slug-2 \\
        --workspace /path/to/target/workspace \\
        --repo /path/to/git/repo

See ../SKILL.md for the workflow and ../references/algorithm.md for the design
rationale (especially why deduplicating by tool_use.id is load-bearing).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

LINE_RE = re.compile(r"^\s*\d+\t")


def parse_events(roots: list[Path]) -> list[tuple]:
    """Walk every JSONL under every root, dedupe by tool_use.id globally, and
    return a timestamp-sorted list of (ts, kind, file_path, input, is_error,
    result_text) tuples for Write/Edit/MultiEdit/Read events.

    Dedup is the critical correctness property: Claude Code's compacted-session
    resume re-emits identical tool events across rolling transcript files. If
    you don't dedup, insertion-pattern Edits (where new_string contains
    old_string) apply multiple times and duplicate their inserts each time.
    """
    seen_use: set[str] = set()
    seen_result: set[str] = set()
    events: list[tuple] = []
    pending: dict[str, dict] = {}

    jsonls: list[Path] = []
    for root in roots:
        jsonls.extend(root.rglob("*.jsonl"))
    jsonls = sorted(set(jsonls))

    for jp in jsonls:
        try:
            text = jp.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = e.get("timestamp", "")
            msg = e.get("message", {}) or {}
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    tid = c.get("id")
                    if not tid or tid in seen_use:
                        continue
                    name = c.get("name")
                    inp = c.get("input", {}) or {}
                    if name in ("Write", "Edit", "MultiEdit", "Read") and inp.get("file_path"):
                        seen_use.add(tid)
                        pending[tid] = {
                            "ts": ts,
                            "kind": name,
                            "file": inp["file_path"],
                            "input": inp,
                        }
                elif c.get("type") == "tool_result":
                    tid = c.get("tool_use_id")
                    if not tid or tid in seen_result:
                        continue
                    p = pending.pop(tid, None)
                    if not p:
                        continue
                    seen_result.add(tid)
                    rc = c.get("content")
                    result_text = ""
                    if isinstance(rc, list):
                        for item in rc:
                            if isinstance(item, dict) and item.get("type") == "text":
                                result_text += item.get("text", "")
                    elif isinstance(rc, str):
                        result_text = rc
                    events.append(
                        (p["ts"], p["kind"], p["file"], p["input"], bool(c.get("is_error", False)), result_text)
                    )

    events.sort(key=lambda x: x[0])
    return events


def strip_read_output(text: str) -> str | None:
    """Convert a ``Read`` tool_result payload back to raw file content.

    Read output is ``N\\t<line>`` per line (cat -n style). Partial/truncated
    reads return None — their content isn't authoritative for the whole file.
    Trailing ``<system-reminder>`` blocks are truncated since they're harness
    injection, not file content.
    """
    if "<system-reminder>" in text:
        text = text.split("<system-reminder>")[0]
    if re.search(r"\[Tool output truncated|only showing the first \d+ lines", text):
        return None
    lines: list[str] = []
    for ln in text.split("\n"):
        m = LINE_RE.match(ln)
        if m:
            lines.append(ln[m.end():])
    return "\n".join(lines) if lines else None


def git_head(repo: Path, rel: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{rel}"],
        capture_output=True,
        text=True,
    )
    return r.stdout if r.returncode == 0 else None


def apply_edit(content: str, old: str, new: str, replace_all: bool) -> str | None:
    """Strict old_string match. Returns None if the match can't be made."""
    if not old:
        return content
    if replace_all:
        return content.replace(old, new) if old in content else None
    return content.replace(old, new, 1) if content.count(old) >= 1 else None


def reconstruct(
    events: list[tuple],
    *,
    workspace: Path,
    repo: Path,
) -> dict[str, Any]:
    """Walk events per file, replay with drift correction, write outputs.

    Drift correction: every full Read re-anchors ``content`` to the authoritative
    snapshot, so intermediate Edits that can't apply get overwritten when the
    next Read lands. We track misses separately as pre-snapshot (harmless —
    overwritten by a later Read) vs post-snapshot (real gaps in the final
    state).
    """
    prefix = str(workspace) + "/"
    by_file: dict[str, list] = defaultdict(list)
    for ev in events:
        if ev[2].startswith(prefix):
            by_file[ev[2][len(prefix):]].append(ev)

    results: dict[str, Any] = {
        "clean": [],
        "clean_after_snap": [],
        "dirty": [],
        "skipped": [],
    }

    for rel, evs in by_file.items():
        # Locate latest snapshot (Write or full Read).
        last_snap = -1
        for i, (_ts, kind, _fp, inp, is_err, text) in enumerate(evs):
            if is_err:
                continue
            if kind == "Write":
                last_snap = i
            elif kind == "Read" and not inp.get("offset") and not inp.get("limit"):
                if strip_read_output(text) is not None:
                    last_snap = i

        content = git_head(repo, rel)  # may be None (new file)
        pre_miss = 0
        post_miss = 0
        applied_post = 0

        for i, (_ts, kind, _fp, inp, is_err, text) in enumerate(evs):
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
                    if is_post:
                        post_miss += 1
                    else:
                        pre_miss += 1
                    continue
                nc = apply_edit(
                    content,
                    inp.get("old_string", "") or "",
                    inp.get("new_string", "") or "",
                    bool(inp.get("replace_all", False)),
                )
                if nc is None:
                    if is_post:
                        post_miss += 1
                    else:
                        pre_miss += 1
                else:
                    content = nc
                    if is_post:
                        applied_post += 1
            elif kind == "MultiEdit":
                if content is None:
                    if is_post:
                        post_miss += 1
                    else:
                        pre_miss += 1
                    continue
                working = content
                ok = True
                for ed in inp.get("edits", []) or []:
                    nc = apply_edit(
                        working,
                        ed.get("old_string", "") or "",
                        ed.get("new_string", "") or "",
                        bool(ed.get("replace_all", False)),
                    )
                    if nc is None:
                        ok = False
                        break
                    working = nc
                if ok:
                    content = working
                    if is_post:
                        applied_post += 1
                elif is_post:
                    post_miss += 1
                else:
                    pre_miss += 1

        if content is None:
            results["skipped"].append(rel)
            continue

        out = workspace / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)

        if post_miss == 0 and pre_miss == 0:
            results["clean"].append(rel)
        elif post_miss == 0:
            results["clean_after_snap"].append({"file": rel, "pre_snapshot_misses": pre_miss})
        else:
            results["dirty"].append(
                {
                    "file": rel,
                    "post_snapshot_misses": post_miss,
                    "post_snapshot_applied": applied_post,
                    "pre_snapshot_misses": pre_miss,
                }
            )

    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay Claude Code transcripts into a workspace")
    ap.add_argument(
        "--transcript-root",
        dest="transcript_roots",
        action="append",
        required=True,
        help="Path to a ~/.claude/projects/<slug>/ directory. Pass multiple times if the "
        "session spanned multiple working directories (e.g., a main repo AND a worktree).",
    )
    ap.add_argument(
        "--workspace",
        required=True,
        help="Target workspace to reconstruct into. Files are written here under their "
        "original relative paths.",
    )
    ap.add_argument(
        "--repo",
        required=True,
        help="Git repo to use for HEAD fallback when a file has no transcript snapshot. "
        "Typically the main repo (not the workspace, if the workspace is a worktree).",
    )
    args = ap.parse_args()

    roots = [Path(r).expanduser().resolve() for r in args.transcript_roots]
    workspace = Path(args.workspace).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve()

    for r in roots:
        if not r.is_dir():
            raise SystemExit(f"transcript root does not exist: {r}")
    if not workspace.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace}")
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise SystemExit(f"repo does not look like a git repository: {repo}")

    print(f"Ingesting {len(roots)} transcript root(s)...")
    events = parse_events(roots)
    print(f"  deduped events: {len(events)}")

    print(f"Reconstructing into {workspace}...")
    results = reconstruct(events, workspace=workspace, repo=repo)

    total = sum(len(v) if isinstance(v, list) else 0 for v in results.values())
    print()
    print(f"=== Summary ({total} files touched) ===")
    print(f"  clean:             {len(results['clean'])}")
    print(f"  clean_after_snap:  {len(results['clean_after_snap'])}")
    print(f"  dirty:             {len(results['dirty'])}")
    print(f"  skipped:           {len(results['skipped'])}")

    report_path = workspace / ".recovery_report.json"
    report_path.write_text(json.dumps(results, indent=2))
    print(f"\nFull report: {report_path}")

    if results["dirty"]:
        print("\nDirty files (post-snapshot gaps) — inspect with scripts/inspect_misses.py:")
        for d in sorted(results["dirty"], key=lambda x: -x["post_snapshot_misses"])[:20]:
            print(f"  {d['file']}: miss={d['post_snapshot_misses']} applied={d['post_snapshot_applied']}")
    if results["skipped"]:
        print("\nSkipped (no snapshot, not in HEAD):")
        for r in results["skipped"]:
            print(f"  {r}")


if __name__ == "__main__":
    main()
