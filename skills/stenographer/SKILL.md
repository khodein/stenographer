---
name: stenographer
description: "Maintains a complete real-time transcript for the current task (stored under an explicit name, by default the current Git branch), recording verbatim user messages and visible AI responses, decisions, changes, checks, and external operations, without process noise or duplicates. Off by default: run only on the user's explicit request, and don't ask about it in advance."
---

# Task Stenographer

## Purpose

Maintain a chronological transcript of significant events for the current task. Completeness means capturing requirements, decisions, changes, and evidence of results — not copying the raw tool-call stream. The transcript is a real-time event log, not an end-of-task summary.

Use `~/.local/share/skills/stenographer/scripts/stenographer.py`. Run it **from the working repository**, not from the skill directory: the script resolves the repository (and, by default, the current branch) via `git` relative to the current directory, so running it elsewhere produces the wrong Git context. Always use the full invocation:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py <command>
```

Do not rely on a shell alias: individual tool calls may run in isolated or non-interactive shells that don't read your shell profile.

Store transcripts outside the working repository, in the stenographer repository the skill is installed from:

```text
<skill repository>/transcripts/<repository>/<key>.md
```

The key is the transcript's name. By default it is the current branch name; `--name <key>` chooses another one (see "Choosing the transcript"). `/` in a key is replaced with `__` so a key never creates a nested directory. The `transcripts/` folder lives in the stenographer repository the skill is installed from (git-ignored; if the skill was copied rather than linked, it sits inside the skill directory), so the stored path is whatever the script prints. Override the storage root with the `STENOGRAPHER_ROOT` environment variable only when actually needed.

Details of `--supersedes`/ID semantics for existing records, the exact `--symbol` naming rules and diff format for `code-change`, and fully worked `append` examples live in `~/.local/share/skills/stenographer/reference.md` next to this file. Read it when the matching situation comes up (superseding an older decision, the first `code-change`, unclear example syntax) — not necessarily at the start of every task.

## Choosing whether to record

The stenographer is off by default. Don't ask about it before a task and don't run any stenographer command until the user explicitly enables it (by invoking the skill, "keep a transcript", etc.). No instruction means off.

- The choice holds for the whole current task: follow-ups, new stages, resuming after a pause, or restoring context don't require enabling again. Carry the mode through task handoffs. For a genuinely new, independent task the mode is off again, unless the user set the mode for the whole session or some other explicit scope.
- While off, don't read or modify the transcript, and don't run `show`, `context`, `list`, `init`, `resume`, `append`, `digest`, `index`, or `finish`. A direct request to work with an old transcript authorizes only that specific work, not recording the current task.
- If the user changes their choice mid-task, apply it immediately. After disabling, don't run even the closing commands. After enabling, start recording from that point — don't reconstruct missed events from memory; note the user's request and mark where recording begins.

The rest of this skill applies only while the stenographer is enabled. An existing transcript or an "in progress" status do not substitute for the user's choice.

## Choosing the transcript

A transcript belongs to a task, not to a branch: one task can span several branches, and every event records the branch it happened on (`Branch`, filled in automatically; `detached` outside a named branch). The transcript is identified by its key.

- Default key: the current branch name. It is used only when no `--name` is given.
- `list` prints the repository's transcripts (key, status, update date, branches, task). It is read-only and works in detached HEAD.
- Once the key is chosen, pass `--name <key>` on **every** command (`init`, `append`, `show`, `context`, `digest`, `resume`, `finish`), even while you stay on the branch the key was taken from. Without `--name` the script falls back to the current branch and may write to the wrong transcript, or fail after the branch changes. All command examples in this skill omit `--name` for brevity: add it to each of them.
- After a context reset, restore the key with `list` (match by task and branches; if unsure, ask the user) before running anything else.
- A key must be a single line, must not start with `.`, contain `..` or a backslash, end with `.digest`, or be `INDEX`. Names that differ only by case are treated as the same name, so are `a/b` and `a__b`.
- `init --name X` on an existing `X` succeeds only with the same task (idempotent); a different task is rejected so a new task never ends up in someone else's transcript. Don't work around the error: continue the existing transcript or pick another name.

## Startup after the user agrees

Do this after the mode is chosen, before further research, planning, code changes, or external actions:

1. Read this file completely.
2. Run `python3 ~/.local/share/skills/stenographer/scripts/stenographer.py list` from the working repository to see the existing transcripts, without printing any transcript. Then pick the key:
   a. the user named a transcript or the task clearly continues one from the list → use that key;
   b. otherwise, if a transcript whose key equals the current branch name exists → continue it by default;
   c. otherwise, including detached HEAD, don't guess: tell the user that no transcript was found for the current branch, mention that the list can be used to continue an existing one, and ask whether to create a new one and under which name (suggest the branch name, or a short task name when the branch doesn't describe the task). Wait for the answer.
3. If the chosen transcript exists:
   a. read the digest `<key>.digest.md` first (it lies next to the transcript, in the directory printed by `list`): a one-line-per-event map of the active events with their IDs. If it is missing, run `python3 ~/.local/share/skills/stenographer/scripts/stenographer.py digest`;
   b. run `python3 ~/.local/share/skills/stenographer/scripts/stenographer.py context` and read the output as your primary context;
   c. use the IDs from the digest and the context to read the underlying source events via `show --event E001`; for a plan with deltas, read the base plan and every delta that applies to it. The digest line and the context output are a pointer and a trimmed excerpt, not a substitute for the full text of requirements;
   d. for older records with no supersede links, don't assume the decision still holds automatically: cross-check the user's latest input and the current code, reading `show --type user-request,user-addition,user-decision,decision,plan` or the whole transcript if needed;
   e. run `python3 ~/.local/share/skills/stenographer/scripts/stenographer.py resume` to set the status back to "in progress".
4. If it doesn't exist, run `python3 ~/.local/share/skills/stenographer/scripts/stenographer.py init --task "<short task title>" --name <key>`.
5. Record the current user message as a `user-request` event for a new transcript, or `user-addition` with `--actor user` when continuing an existing one.
6. Only then move on to the rest of the work.

Reading this `SKILL.md`, `list`, the first `show`/`context` call, and `init` when needed are bootstrap actions; the results are not recorded after the fact, because reliable recording isn't possible before the transcript is found or created. Everything after that is recorded in real time, in order.

The digest is a derived artifact, not the source of truth. It's built by deterministic parsing of already-recorded events (no LLM summarization) and is regenerated automatically after every `init`, `append`, `resume`, and `finish`; `digest` rebuilds it manually without any risk of losing data — the full transcript is never changed by it. When in doubt or in case of a mismatch, trust the full transcript over the digest. Never run `digest` as a routine step.

When creating a transcript, pass a short task title to `init --task`; save the full original request once, in a `user-request` event.

If the repository can't be resolved, or there is neither a named branch nor an agreed `--name`, stop transcription, tell the user, and don't create a file under a made-up name.

## Selecting events: meaning first, then type

Record significant events right after they happen, before the next action on the task. Don't defer recording to the end, and don't reconstruct history from memory. The exclusions below apply to every event type: renaming routine activity to `research` or `action` doesn't make it worth recording.

Record:
- every task-related user message verbatim, including short confirmations, refusals, and corrections;
- substantive AI questions and answers, new findings, decisions, assumptions, and changes of direction;
- the final plan and every later plan change;
- every file change, with paths, symbols, and diff;
- checks actually performed and their outcome, significant errors and blockers;
- branch creation, commits, pushes, PRs, and external changes, with an identifier and a confirmed outcome.

Record each fact once, choosing the most precise type:
- an ordinary search/read → `research` with the finding and its source (path/symbol/line or URL), only when it's material to the task;
- a local edit → `code-change`, without extra `action`, `tool-call`, or `tool-result` entries duplicating the same edit;
- a check → `verification` with the exact command, its scope, and the result;
- a failed check → `error` with diagnostics instead of a duplicate `verification`; record the later successful check separately;
- a substantive AI comment → `agent-comment`; don't follow it with a `research`/`decision` repeating the same finding.

If the final substantive response repeats already-recorded facts, still save it verbatim as `agent-response` — it marks a useful conversation boundary. Don't also add a paraphrase of the final response. Skip pure acknowledgements ("got it", "moving on") that add no new condition or result, in both intermediate and final AI messages. Don't shorten user messages under this rule.

Use `tool-call`/`tool-result` when the operation itself matters for reconstructing what happened: an external mutation, a long-running operation with its own state, or diagnostics that depend on the exact parameters used. Record the call before running it and the result after; link the result to the call by time/title instead of repeating the command. If the result is already captured as a `commit`, `push`, `external-action`, or `error` event, a separate `tool-result` isn't needed. Running something is not proof that it succeeded.

## What not to record

- Bootstrap actions: reading the transcript, rules, conventions, or skill files; calls to the stenographer itself, `digest`, `index`, and their technical confirmations. If a rule changed a decision, capture that inside the substantive event instead.
- Routine code reading, search commands, and match lists with no new finding; a repeated `status` or `diff` when nothing changed.
- Announcements ("starting", "reading", "checking", "using the skill"), stage transitions, and repeated acknowledgements of the user's own edits. A new constraint or decision inside a comment is still recorded verbatim with that comment.
- Polling with no new result, session/cell/chunk IDs, tool-call duration, JSON envelopes, and other transport metadata. Record meaningful performance measurements. Record turn tokens only in `agent-response` via `--with-usage` (see "Completing a response"); never type the numbers by hand.
- Full successful logs, an entire file just read, repeated listings, and technical `Done`/`Success` messages — a verifiable result is enough instead.
- Separate entries for every todo item or step-status change. Record plan content changes under the next section instead.
- Empty events and placeholder titles. Prepare the body first, then run `append`. If a call fails, fix the cause and retry — don't create an empty event instead.

Don't treat errors, blockers, and disproved hypotheses as noise if they change a decision or help avoid repeating a failure. Briefly record the cause, the material diagnostics, and the next step; record a repeat of the same error as a reference to the first event with an updated status, without copying the stack trace again.

## Recording the full plan

- Don't record draft todo reshuffling; record an agreed change of scope, order, or conditions right away as a `plan` delta.
- Once the final plan is fully formed and ready to publish to the user, immediately record a separate `plan` event with the complete verbatim plan content.
- A reference to a plan file, its path, a short summary, or a chunk list does not substitute for publishing the full plan in the transcript.
- If the full plan lives in an external or temporary file, read that file completely before the final response and put its entire content in the `plan` event's `--body`.
- Record the first publication of the final plan in full. On a later change, do **not** duplicate the whole plan: record a separate `plan` event with only the delta — which items were added (`+`), removed (`-`), or reworded (`~`), with a brief reason. Don't edit the existing record. Re-publish the full plan only if it was restructured enough that a delta would be unreadable.

## Content rules

- Record every user message, response, and comment verbatim and in full, regardless of length.
- Record substantive AI comments and responses that pass the selection rules above verbatim and in full. Skip process announcements and empty acknowledgements.
- Put the verbatim text of a message, response, or comment in `--body`; use `--title` only as a short event label, never as a substitute for the text. The title is also the event's line in the digest, which a later reader scans to decide which events to open, so make it self-contained: one line, the specific decision, change, or finding and its key reason ("Cache in Room, migrate from DataStore"), not a generic label ("Decision", "Update"). Titles longer than 120 characters are cut in the digest.
- If the body contains Markdown backticks (inline code, a fenced diff block), `$`, or other shell-active characters, don't pass that text inline via `--body` on the command line. Write it to a file (with your editing tool, not through the shell, with no escaping) and pass the path via `--body-file`. This avoids the shell reinterpreting the content as a command — a repeatedly observed failure mode with inline `--body` containing backticks. `--body` and `--body-file` are mutually exclusive; passing both is rejected.
- Set `--actor`: `user` for user events (`user-request`, `user-addition`, `user-decision`), `system` for system events, and for AI actions, comments, and responses, the actual model name (e.g. `Claude Opus 4.8` or `Codex GPT-5`) — never leave the generic `agent`. Each agent fills in its own name. You can set a default once via the `STENOGRAPHER_ACTOR` environment variable, but when multiple agents work the same transcript, still set the name explicitly on every event.
- For a tool call, record the tool name, the full operation or command, parameters with secrets removed, and the purpose of the call.
- For a tool result, keep the status and the minimal excerpt that supports the outcome; for a successful check, the command and result; for an error, the material diagnostics. Don't call an excerpt the full output. Mask secrets and confidential data with `[REDACTED]` **before** calling `append`, while still recording the event — the script fully rejects a record if it detects a secret rather than editing it, so masking is the agent's responsibility; the built-in check only catches a limited set of patterns and doesn't replace manual redaction.
- Label excerpts from long output as a "material excerpt"; state the number of omitted lines only if actually counted. Keep everything needed to understand an error or a result, without an arbitrary limit that cuts off evidence.
- Don't record hidden model reasoning. Record only conclusions, decisions, and observable actions.
- Don't record secrets, tokens, passwords, cookies, personal data, or raw confidential tool output.
- For every change, use the full file path from the repository root, not just the file name. Any event with `--file` must also have `--symbol`; the script rejects the record without it. Exact `--symbol` naming rules and the `code-change` diff format are in `reference.md`.
- For commands, record the command and the outcome: succeeded, failed, or blocked.
- For errors, record the cause and the next safe step.
- Never rewrite existing history or delete events.
- Don't leave a trailing blank line at the end of the file.

### Event metadata

Add these to a significant event's own record when useful, instead of creating a duplicate just to surface it:

| Parameter | Values and meaning |
| --- | --- |
| `--context-section` | `goal` — a goal; `requirement` — a requirement/constraint; `result` — an achieved result; `question` — an open question; `next-step` — an explicit next step |
| `--evidence-status` | `confirmed` — confirmed, state the basis in the body; `hypothesis` — a hypothesis; `proposed` — a proposal not yet accepted |

The branch is not a parameter: every event gets `Branch` automatically from the current Git state.

Don't pass off the user's agreement as a technical check: state exactly what was confirmed and how. Without this metadata the tool shows "not set"/"unknown" rather than inferring a status from words like "done" or from the event type.

## Event types

Use one of:

- `user-request`
- `user-addition`
- `user-decision`
- `agent-comment`
- `agent-response`
- `plan`
- `research`
- `decision`
- `action`
- `tool-call`
- `tool-result`
- `code-change`
- `verification`
- `error`
- `blocker`
- `commit`
- `push`
- `external-action`

## Commands

Extract context and open a specific event (only while enabled):

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py context
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py show --event E042
```

Show the transcript, in full or filtered by type:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py show
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py show --type decision,blocker,plan
```

Create a transcript:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py init --task "Short task description"
```

Add an event (minimal template; `--type` is one of the list above):

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append \
  --type <type> --actor <user|system|model name> --title "<short title>" \
  --body "<verbatim text>"
```

Use `--body-file <path>` instead of `--body` for text with backticks/`$`/Markdown; `code-change` additionally requires `--file` and `--symbol`. Fully worked examples (`user-request`, `code-change` with a diff via `--body-file`, `verification`) are in `reference.md`.

The digest (`<key>.digest.md`) is updated automatically by `init`, `append`, `resume`, and `finish`: status, task, affected files/symbols, and one line `<ID> · <MM-DD HH:MM> · <type> · <title>` per active event, with a `_branch: <name>_` line whenever the branch changes. To rebuild it manually, for example if it was deleted or looks stale:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py digest
```

List the repository's transcripts (read-only; key, status, update date, branches, task, newest first):

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py list
```

Regenerate the repository's index (`INDEX.md` in the repository's transcript directory) — a key/status/date/task table across every stored transcript:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py index
```

Resume work on an existing transcript after reading it fully:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py resume
```

Mark a transcript as finished (default status `done`):

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py finish --status "done"
```

## Completing a response

Before the final response, while the stenographer is enabled:

1. Make sure every significant event from this turn is recorded per the selection rules, with no empty or duplicate entries.
2. If a final plan was produced, make sure its full content was published in the transcript as a separate `plan` event.
3. Record the substantive final response verbatim as an `agent-response` event; skip a pure acknowledgement with no new information. Don't create an additional summary. Add the `--with-usage` flag: the script appends the sum of AI response tokens for the turn (input, cache read/write, output) from the Claude Code session transcript. Use it only for `agent-response` and `agent-comment`; if the transcript isn't found, record the event without the flag.
4. Don't treat the transcript as a substitute for the response to the user: the final response must stand on its own.
5. When the task is done (merged/closed), run `python3 ~/.local/share/skills/stenographer/scripts/stenographer.py finish` to move the status from "in progress" to "done"; the digest is updated by the command itself.
