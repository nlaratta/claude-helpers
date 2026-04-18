# Recovery algorithm: design rationale

Background for the replay engine in `scripts/recover.py`. Read this before changing the algorithm — every piece is a response to a specific failure mode that showed up in practice.

## Why transcripts are usable at all

Claude Code's CLI writes one JSONL file per conversation to `~/.claude/projects/<encoded-cwd>/`. Every tool invocation produces two entries:

- A `tool_use` event with a unique `id`, the tool `name`, and the `input` (for `Write`: full file content; for `Edit`: `old_string` + `new_string` + `replace_all`; for `Read`: `file_path` + optional `offset`/`limit`).
- A matching `tool_result` event keyed by `tool_use_id` that carries the tool's output and an `is_error` flag.

Because `Write` captures full content and `Edit` captures exact old/new strings, replaying these events in order is — in theory — sufficient to reconstruct any file the session edited. In practice, three things break that theory:

1. **Events get duplicated** across compacted-session resume.
2. **One logical session spans multiple transcript roots** when the working directory migrates.
3. **State drifts between replay and reality** when edits are made by tools other than `Write`/`Edit` (bash heredocs, `sed -i`, IDE saves).

The algorithm below is essentially three fixes for those three problems.

## Fix 1: Deduplicate by `tool_use.id` globally

This is the most important correctness property.

When a Claude Code session runs long enough to trigger compaction, subsequent transcript files re-emit prior tool events. The same `tool_use_id` can appear up to 5× across the JSONL corpus. If you naively replay every event, each duplicate fires the same `Edit` again — and that's fine for most edits, because the second attempt hits an `old_string` that's already been replaced and just no-ops.

The failure mode is **insertion-pattern edits**, where `new_string` contains `old_string` as a prefix or suffix. For example:

```
old_string = "function foo() {"
new_string = "function foo() {\n  const x = 1;"
```

After the first replay, content contains `function foo() {\n  const x = 1;`. The second replay searches for `function foo() {` and still finds it — because it's at the start of the new content. It replaces that with `function foo() {\n  const x = 1;` again, producing `function foo() {\n  const x = 1;\n  const x = 1;`. Every duplicate inserts again.

On the recovery that motivated this skill, ignoring dedup produced 60 "dirty" files with ~800 missed or corrupted edits. Adding a single `seen_use.add(tool_id)` guard reduced that to 2 dirty files with 2 misses each. Don't skip this.

## Fix 2: Ingest multiple transcript roots

One Claude Code session doesn't necessarily correspond to one `~/.claude/projects/<slug>/` directory. If a user starts a Claude Code CLI session in a main repo, then opens Claude Desktop on a worktree of that same repo, the two halves of the logical project live under two different encoded-cwd slugs:

- `/Users/you/Projects/Foo` → `~/.claude/projects/-Users-you-Projects-Foo/`
- `/Users/you/Projects/Foo/.claude/worktrees/session-a` → `~/.claude/projects/-Users-you-Projects-Foo--claude-worktrees-session-a/`

Each slug has its own JSONLs, and file paths in the tool events are absolute, so events targeting the worktree can appear in either location's transcripts.

If you ingest only one slug, you'll see half the edit history and end up with the other half as "new files" that the replay has to synthesize from scratch — which it can't, since it lacks the intermediate state.

The recover script takes multiple `--transcript-root` arguments for this reason. Also walk `subagents/` subdirectories under each root — subagent invocations produce independent JSONLs that can and do edit files in the same workspace.

## Fix 3: Drift-corrected replay with Read as re-anchor

Not every modification to a file comes through `Write`/`Edit`. Users run `sed -i`, they save in their editor, they run Python scripts that call `.write()`. These modifications change the on-disk state but leave no tool event behind.

When a later `Edit` arrives with an `old_string` that was correct against the post-off-tool-modification state, replay against our reconstructed state (which doesn't have the off-tool mods) fails: `old_string` isn't found, and we either have to abort the file or skip that Edit.

Aborting is wrong — there might be 10 more valid Edits after this one that *would* apply cleanly. Skipping is usually right, but it leaves reconstructed state diverged from reality.

The trick is that `Read` tool_results contain the full file content (formatted as `N\t<line>`) at the moment Claude read the file. If we treat every non-partial `Read` as a _re-anchor_ — overwriting our reconstructed `content` with what the Read actually saw — then any drift that accumulated gets corrected whenever Claude re-reads the file. Subsequent Edits then apply against authoritative state instead of our best-effort reconstruction.

This is why the algorithm:
- Tracks the **last snapshot index** (most recent Write or full Read).
- Walks every event forward, re-anchoring on each full Read.
- Distinguishes pre-snapshot misses (which get overwritten by the next Read anyway — harmless) from post-snapshot misses (the tail of events after the last Read, where we can't re-anchor — these are real gaps in the final state).

Only post-snapshot misses matter for the user. Pre-snapshot misses are noise.

## Detail: parsing `Read` tool_result

Claude Code formats `Read` output as:

```
     1	first line of file
     2	second line
     3	
     4	fifth line (note: blank lines are preserved)
```

Each line starts with a variable amount of leading whitespace, the line number, and a single tab. To recover raw content, strip the `^\s*\d+\t` prefix and join on `\n`.

Two edge cases:

1. **Large files are truncated.** If the Read result contains markers like `[Tool output truncated]` or `only showing the first N lines`, treat the Read as partial — it's not a snapshot. `strip_read_output` returns `None` in this case.
2. **Trailing `<system-reminder>` blocks.** The harness sometimes appends system reminders to tool results. These aren't file content — truncate at the first `<system-reminder>`.

Reads with `offset` or `limit` in the tool_use input are also partial by construction — they show a slice, not the whole file. Skip them for snapshot purposes (but they can be useful for stitching in exotic cases; see below).

## Detail: fallback to `git HEAD`

Not every file the transcripts touch has a `Write` or full `Read`. Some files are created off-tool and only ever had `Edit` operations applied. Others exist on disk before the session starts and only the Edits modify them.

For these files, we prime `content` from `git show HEAD:<relative-path>` as a base before replaying events. If the file isn't at HEAD either, and no transcript event is a snapshot, the file gets `skipped` — the replay can't synthesize content from nothing.

Choose the repo for the HEAD fallback carefully. If the workspace is a worktree, the worktree's HEAD might be empty (fresh branch) while the main repo's HEAD has the file. Pass the main repo as `--repo`.

## Detail: dangling stashes

When Claude Code Desktop archives a session, in some cases it creates a git stash of the worktree's uncommitted changes before removing the worktree directory. The stash's ref gets cleaned up but the object survives. `git fsck --lost-found --unreachable` turns them up as commits titled `WIP on <branch>`.

These files don't appear in transcripts because stash creation isn't a tool event. After running the replay, diff the stash's file list (`git diff --name-only <stash-sha>^1 <stash-sha>`) against the replay's output. For files only in the stash, pull them via `git show <stash-sha>:<path>`.

This is also how off-tool modifications (IDE saves, `sed -i`, etc.) that happened just before archive get recovered — the stash captures them.

## Miss classification (post-recovery)

For each `dirty` file in the recovery report, the missing Edits fall into three patterns:

1. **`first_line_present` but not full-match.** The `old_string`'s first line exists in the on-disk content, but the full multi-line `old_string` doesn't match. This almost always means the file evolved between that Edit and a later one — the region was rewritten, and this Edit would have been superseded anyway. The missing content is usually already in the reconstruction, just arrived by a different path. Low priority; move on.

2. **Prerequisite missing.** `old_string` references function names, variables, or imports that *never appear anywhere* in the current reconstruction. This means an earlier Edit that would have introduced those names was itself missed. Grep every Edit's `new_string` across every transcript for a distinctive token from the missing content — you'll usually find the introducing Edit, and understand why it didn't apply.

3. **Genuinely lost.** Off-tool modification that never left a tool_event trail and isn't in any stash. Reimplement from scratch or regenerate.

`scripts/inspect_misses.py` prints the `old_string` / `new_string` pairs plus whether the first line is present on disk — enough diagnostic info to bucket each miss in a few seconds.

## Edge case: stitching partial Reads

For files that were never Written and only partially Read (e.g., a config file Claude Read in multiple `offset`/`limit` chunks), you can stitch the chunks together by line number. The Read output preserves absolute line numbers, so:

1. Collect every Read's text chunks.
2. For each line, keep the version from the latest Read (by timestamp).
3. Fill gaps with placeholder markers.

This produces a partial reconstruction with known gaps. Useful as a diff reference even if it's not runnable. The tesla-yaml recovery used this to recover ~32% of a 279-line config that was never fully read but had useful metadata in the captured ranges.

## What this algorithm does NOT do

- **Reproduce exact bytes of the on-disk state at archive time.** Replay is deterministic given the same inputs, but the inputs are tool events only. Anything that modified the file outside of tool events is invisible.
- **Detect concurrent edits across subagents.** If two subagents both edit the same file, the algorithm walks events in timestamp order, which is the right thing. But if tool events weren't actually serialized on disk (they should be, but in practice there have been races), the replay order might diverge from on-disk order. Usually not a problem; flagged here for completeness.
- **Automatically patch dirty files.** It reports where the gaps are and surfaces the missing old/new pairs via `inspect_misses.py`, but applying them to a diverged state is a human-judgment call. Don't automate that.
