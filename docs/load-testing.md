# Load testing the stenographer

This document is written for an AI agent that has to run the load test and report on it. Follow it step by step; do not improvise a different test.

## What it is for

The unit tests (`skills/stenographer/tests/test_stenographer.py`) check behaviour on a handful of events. The load test checks what only shows up on a long task: a transcript with 1,000+ events that moves through several branches, with parallel writers and awkward text. It has already found three real defects (an unbalanced code fence that hid later events, a pasted event block that made the whole transcript unreadable, and a digest that was not bounded), so run it after any change that touches how events are written, parsed, or summarized.

Run it after changes to: `parse_events`, `event_entry`, `append`, `build_digest`, `build_context`, `list`, `init`, locking, or the transcript format. Skip it for documentation-only changes.

## Rules

- Run from the repository root. The test creates its own temporary git repository and its own `STENOGRAPHER_ROOT`, so it never touches real transcripts under `transcripts/`.
- Do not edit the code under test while the test runs, and do not commit or push as part of the test.
- Do not run it as part of `unittest discover` (it takes about two minutes); it lives next to the unit tests but its name doesn't match `test*.py` on purpose.
- Temporary files are removed at the end. Use `--keep` only to inspect a failure, and delete the printed directory afterwards.
- Do not write the transcript of this work with the stenographer unless the user asked for it.

## How to run

```bash
python3 skills/stenographer/tests/load_test.py --seed 7
python3 skills/stenographer/tests/load_test.py --seed 21
python3 skills/stenographer/tests/load_test.py --seed 99
```

Each run takes about 2 minutes (1,255 events). Use a command timeout of at least 10 minutes or run it in the background and wait for it; running the three seeds one after another needs about 6 minutes. A different seed gives a different mix of events, which is what makes three runs more convincing than one. For a quick check while developing, use `--events 100` (about 10 seconds); the quick run is not a substitute for the full one.

Other options: `--script <path>` tests another copy of `stenographer.py`, `--digest-max-chars <n>` changes the size alarm (default 20,000).

The exit code is 0 on `RESULT: PASS` and 1 on `RESULT: FAIL`; the problems are listed under the result.

## What the test does

1. Creates a throwaway git repository and a transcript named `load-task`.
2. Appends events in growing phases (50, 150, 300, 600, 1,200) through the real CLI, switching between four branches every 40 events. The mix of event types is weighted like a real task (many `code-change`, `decision`, `verification`; few plans and blockers). Bodies are drawn from a list of awkward texts: Cyrillic, emoji and CJK, backticks and `$`, balanced and unbalanced code fences (including a diff hunk that shows only an opening fence), a fake event header inside a fence, a pasted event block with a reused ID, a pasted block with a far-ahead ID, lines that look like metadata, and a 5,000-character line.
3. Adds a chain of 6 events where each one supersedes the previous.
4. Adds 48 appends from 8 parallel workers.
5. Switches to detached HEAD, appends with `--name`, and checks that `context` without `--name` fails.
6. Checks the results (see below), measures the commands, then runs `finish`, `resume`, and a repeated `init` with a different task.

## What is checked (each failure is a problem)

- Event IDs are contiguous (`E001`…); no event was lost or duplicated; the 48 parallel appends got 48 distinct IDs.
- The transcript doesn't end with a newline.
- Every parallel event and 40 random others come back from `show --event` verbatim, except that header-like lines are stored with a leading backslash (counted separately as "differ only by the header escape").
- The digest lists no superseded event, keeps every `user-request`, `user-decision`, `plan`, and `blocker` that is not superseded, collapses the supersede chain to its last step, has folded lines for a long transcript, has no event line over 220 characters, and stays under the size alarm.
- `list` shows every branch that was used; `finish` and `resume` refresh the digest status; `init` with another task on an existing name is rejected.

## What is measured (reported, not pass/fail)

Lines starting with `METRIC` in the output: average append latency per phase, transcript size, digest size in characters and approximate tokens (characters / 4), number of event lines, folded lines and branch markers in the digest, `context` size and the five largest `context` sections, `list` and digest-rebuild time, and the digest and `context` as a share of the transcript.

## Reference numbers (after the `context` cap, seeds 7 and 21, 1,255 events)

| Metric | Value |
| --- | --- |
| Append latency | 55 ms at 50 events, 68–72 ms at 300, 107–109 ms at 1,200 (grows slowly and linearly) |
| Transcript | 0.8–0.9 MB |
| Digest | 15,400–16,800 chars (about 3,900–4,200 tokens); 29 folded lines |
| `context` | 29,900–33,900 chars (about 7,500–8,500 tokens); the "Decisions" section is about 14,600 chars (it was about 90,000 before the cap) |
| `list`, digest rebuild | about 80 ms each |
| Whole run | about 2 minutes |

Seed 99 was not re-run after the `context` cap.

Treat a move of more than about 30% from these numbers as a regression to investigate, even when the run passes. Update this table in the same commit as a change that moves the numbers on purpose.

## If the run fails

1. Read the problem text and decide whether the product or the test is wrong. Check the test first when the failure looks odd: for example, a body mismatch that appeared only after the test began writing every event from a shared file turned out to be a race inside the test, not a stenographer bug.
2. Reproduce with a smaller run (`--events 100 --seed <same seed>`) or with `--keep` and look at the transcript and digest files in the printed directory.
3. A digest over the size alarm is not automatically a bug: its size depends on how many `user-request`, `user-decision`, `plan`, and `blocker` events the seed produced. Compare with the table; raise the alarm only with the user's agreement.
4. Fix the cause, add a unit test in `test_stenographer.py` that fails without the fix, and run both the unit tests and the load test again.

## Report format

Report to the user, in this order:

1. The commit or working-tree state that was tested and the seeds that were run.
2. `PASS` or `FAIL` per seed, with the problem text for every failure.
3. A table of the `METRIC` values for each seed next to the reference numbers, with any deviation of more than about 30% called out.
4. What you changed or propose to change, and what you did not run.

Do not claim more than was run: say which seeds and sizes were used, and say plainly if a run was shortened.

## Extending the test

When a new defect is found, add the input that triggered it to `BODIES` (or a new phase) in `skills/stenographer/tests/load_test.py`, add the matching check to the `check_*` methods, and add a unit test next to the other tests. Keep the run under about three minutes.
