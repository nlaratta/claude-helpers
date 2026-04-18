---
name: claude-code-recovery
description: Recover lost work after a Claude Code Desktop worktree is archived, deleted, or the session is otherwise wiped. Rebuilds files by replaying tool_use events from session JSONL transcripts, with special handling for dedup across compacted sessions, split transcripts when a session migrated between directories, and dangling git stashes. Use whenever the user reports lost work, a missing worktree, an archived Claude Code session they want back, files that "disappeared" from a project, uncommitted changes that vanished when a Desktop app session ended, or anything involving recovering state from Claude Code transcripts — even if they don't mention "transcripts" or "JSONL" by name.
---

# Claude Code session recovery

Claude Code archives and worktree deletions can take hours or days of work with them. The session's JSONL transcripts are usually still on disk, and they capture every `Write` and `Edit` tool call in enough fidelity to reconstruct files. This skill is the tool + playbook for doing that reconstruction.

## When this applies

Use this skill when:
- A Claude Code Desktop session was archived and its worktree vanished.
- A worktree directory was deleted (by archive, by `git worktree remove`, by a stale cleanup script).
- Uncommitted changes from a Claude session disappeared and aren't in any stash or branch.
- A user says some variant of "I just lost all my work" in the context of Claude Code.

Do NOT use this skill for:
- Regular git recovery (reflog alone is enough — no need to replay transcripts).
- Time Machine / filesystem-level restore (out of scope; try `tmutil` first).
- Recovering a whole _committed_ branch that got force-pushed over (regular `git reflog` or GitHub's branch protection backups).

## Quick triage

Before replaying transcripts, try the cheap things first:

1. **Git reflog + fsck.** If the work was committed (even briefly) or stashed, it's probably still in git:
   ```bash
   git -C <repo> reflog --all
   git -C <repo> fsck --lost-found --unreachable
   ```
   Look for dangling commits and stash-shaped commits (merge commits titled `WIP on <branch>`).
2. **Session JSON.** On macOS, Claude Code Desktop stores session metadata at `~/Library/Application Support/Claude/claude-code-sessions/<org-id>/<user-id>/local_*.json`. The `isArchived:true` flag is what hides a session from the UI. Flipping it to `false` (with Claude Desktop quit) brings the session back into the active list.
3. **Local snapshots.** `tmutil listlocalsnapshots <volume>` and `diskutil apfs listSnapshots <volume>` — APFS sometimes has a snapshot from minutes ago that's faster than any transcript replay.

If the work was never committed/stashed and no snapshot helps, move on to transcript replay.

## Workflow

Work through these steps in order. Most of the intelligence is in the bundled `scripts/recover.py` — the steps below orient you around when to call it and how to verify.

### 1. Find the transcripts

Claude Code's CLI stores transcripts at `~/.claude/projects/<encoded-cwd>/*.jsonl`, where `<encoded-cwd>` is the working directory with `/` replaced by `-`. **The non-obvious trap is that one logical session can span MULTIPLE encoded-cwd directories** — e.g., if work started in a main repo and migrated to a worktree, each location has its own transcript root. Ingesting only one of them leaves half the history unseen. The script takes multiple `--transcript-root` arguments for this reason.

Also check `subagents/` subdirectories under each root — subagent work produces independent JSONLs nested there.

```bash
# Identify candidate roots. The encoded-cwd slug is the project path
# with '/' -> '-'. For /Volumes/DevDrive1/Projects/Foo:
ls ~/.claude/projects/ | grep Foo

# If the work moved between main repo and a worktree, both will appear.
# The worktree slug typically has `--claude-worktrees-<name>` as a suffix.
```

### 2. Run the replay

Reset the target workspace to a clean base (HEAD of the branch you want to reconstruct onto), then run `scripts/recover.py`:

```bash
python3 scripts/recover.py \
  --transcript-root ~/.claude/projects/-Users-you-Projects-Foo \
  --transcript-root ~/.claude/projects/-Users-you-Projects-Foo--claude-worktrees-session-name \
  --workspace /Users/you/Projects/Foo/.claude/worktrees/session-name \
  --repo /Users/you/Projects/Foo
```

The script:
- Walks every `*.jsonl` under each transcript root (including `subagents/` subdirs).
- Pairs `tool_use` with its matching `tool_result` by `tool_use_id`.
- **Deduplicates by `tool_use_id` globally.** Claude Code's compacted-session resume re-emits identical tool events up to 5× across rolling transcript files. Without dedup, insertion-pattern Edits (where `new_string` contains `old_string`) apply repeatedly and duplicate their inserts each time. This one fix was the difference between 60 dirty files with ~800 missed edits and 3 dirty files with 3 misses on the recovery that inspired this skill.
- For each target file: finds the latest "snapshot" (a `Write`, or a full `Read` with no `offset`/`limit`) and replays subsequent `Edit`/`MultiEdit` calls in timestamp order. Every `Read` along the way re-anchors state, so intermediate edits that fail quietly get overwritten when the next Read lands.
- Falls back to `git show HEAD:<path>` when there's no transcript snapshot.
- Writes a `.recovery_report.json` in the workspace listing each file's classification: `clean`, `clean_after_snap` (misses before last Read — harmless), `dirty` (misses after last Read — real gaps), or `skipped` (no snapshot, file not in HEAD).

Read `references/algorithm.md` for the full algorithm and the reasoning behind each design choice.

### 3. Overlay a dangling stash, if any

If `git fsck` turned up a stash-format commit (`WIP on <branch>` with two parents), it may contain files that the transcript never touched — tool-less edits made via your editor or bash heredocs. Files in the stash but absent from the replay report should be pulled from the stash directly:

```bash
# Identify stash files not touched by the transcript.
# The recover.py report's `ok`/`clean_after_snap`/`dirty` lists are the ones
# that WERE touched; diff that set against the stash's file list.
git -C <repo> diff --name-only <stash-sha>^1 <stash-sha>

# For each file only in the stash, overlay it:
git -C <workspace> show <stash-sha>:<path> > <path>
```

The stash is authoritative for any tool-invisible edit (IDE saves, `sed -i`, Python scripts that called `.write()`, etc.).

### 4. Verify the reconstruction builds

Don't trust replay silently. The build/typecheck step catches subtle miscompositions that a pass-rate metric won't:

- **Python:** `ast.parse` every `.py` file to catch syntax errors from mid-replay divergence.
- **Frontend (Vite / Svelte / etc.):** run `npm install` and `vite build`. `svelte-check`/`tsc` errors are diagnostic but rarely build-blocking; the actual `vite build` is what confirms the bundle compiles.
- **Backend:** instantiate the app with `TestClient` (FastAPI) or equivalent and hit one known-good endpoint (`/health`, etc.) before trusting it.

### 5. Commit what you have

Commit the reconstruction before you start poking at it. This preserves a known checkpoint you can always return to, and converts what was dangling git state into reachable refs so `git gc` can't reap the blobs.

```bash
cd <workspace>
git add -A
git commit -m "Recover session state from transcript replay"
git push -u origin <branch>   # belt-and-suspenders — survive a disk failure
```

### 6. Unarchive the session (optional)

If the user wants the Claude Code Desktop UI to show the recovered session again, edit the session JSON with Claude Desktop quit:

```bash
# macOS
SESSION_DIR="$HOME/Library/Application Support/Claude/claude-code-sessions"
# Find the archived session's JSON and flip the flag.
grep -rl '"isArchived":true' "$SESSION_DIR" | head
# For each match, verify it's the right session (check `title`, `worktreePath`)
# and change isArchived to false.
```

Relaunch Claude Desktop — the session reappears in the active list, pointing at the recovered worktree path.

## Handling the remaining misses

After replay, a small number of files may still have post-snapshot misses (classified as `dirty` in the report). For each:

1. Run `scripts/inspect_misses.py <workspace>` to see the exact `old_string` / `new_string` pairs that failed to apply.
2. Classify the miss:
   - **`first_line_present` but not full match** — the file evolved since that Edit (a later Edit rewrote the region). The later state is already in the reconstruction; the missed Edit is almost always redundant noise. Skim it and move on unless its intent is obviously not in the final file.
   - **Prerequisite missing** — the Edit's `old_string` refers to content (function names, blocks, imports) that never existed in the reconstruction, because an earlier Edit that would have introduced it was itself missed. In this case, you need to find the introducing Edit. Grep every Edit's `new_string` across all transcripts for a distinctive token from the missing content; you'll usually find it.
   - **Genuinely lost** — off-tool modification that the transcripts never captured. The user will need to re-implement from scratch.

## What belongs in the bundled scripts

- `scripts/recover.py` — the replay engine. Parameterized by transcript roots, workspace, repo. Writes `.recovery_report.json`.
- `scripts/inspect_misses.py` — diagnostic helper that prints the old/new strings of every post-snapshot miss the replay couldn't apply, so a human can decide whether each is a worrying gap or a superseded edit.

Treat them as a starting template. Specific projects will want to adapt (e.g., stitch partial `Read` results for config files that were never fully Read).

## Reading order for future improvements

If you're working on this skill itself and want to improve it:
1. `references/algorithm.md` — full replay algorithm + design rationale + dedup explanation.
2. `scripts/recover.py` — the implementation.
3. This `SKILL.md` — the workflow-level glue.

Changes to the algorithm should flow in that order: update the reference doc's reasoning first, then the script, then surface the workflow change here.
