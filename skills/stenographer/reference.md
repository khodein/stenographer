# Stenographer — detailed reference

Don't read this in full at the start of every task — only the relevant section, when the matching situation comes up: an existing transcript with superseded decisions, the first `code-change` event, or unclear `append` syntax. Core rules and the startup sequence are in `SKILL.md`.

## Identifiers, supersedes, and context extraction

`append` assigns an ID (`E001`, `E002`, …) under a file lock and returns it in the result. IDs are unique within a transcript. Older events with no ID get a stable positional number when read, without rewriting history — so events can never be reordered or deleted. Use the ID you were given or that you read; never guess one.

When a new decision fully replaces an older one, add `--supersedes E042` to the regular event. Repeat the flag for multiple supersedes. You can only reference events that already exist in this transcript; an invalid reference is rejected before the write. The new record hides the superseded one from `context`/`digest`, but the original event is preserved and still available via `show --event E042`.

- A supersede means the record is fully displaced from the active context. For a partial correction of a compound decision, first restate the conditions that still hold alongside the new ones. Don't mark a user's whole original request as superseded just because one point was clarified.
- A plan delta doesn't supersede the base plan: record it without `--supersedes`. When fully replacing a plan, list the base plan and the deltas that no longer apply.
- For a resolved open question or blocker, record the outcome with `--supersedes` pointing at the question/blocker. Record a rejected decision as a new event with the reason and a reference, not by deleting history.
- If a supersede is later reverted, the old decision doesn't automatically come back: explicitly record the restored decision as a new event and supersede the one that reverted it.
- Use `--supersedes` only for an established replacement, not because two records sound similar. Don't supersede an agreed decision with an unconfirmed proposal.

`context` is read-only: it builds its output from the current file without an LLM, without touching the digest or the transcript. It surfaces goals, decisions, plans and deltas, results, questions, hypotheses, next steps, the last five checks/errors and user messages, files/symbols, and supersede links. Bodies longer than 800 characters are truncated with an explicit marker — this is navigation to the source, not a replacement for reading it. Full event text: `show --event <ID>`.

The digest (`<key>.digest.md`) is the cheapest entry point: it is regenerated automatically after `init`, `append`, `resume`, and `finish`, and a failure to write it never fails the command (a warning goes to stderr; `resume` or `digest` repairs it). After the header, task, and affected files/symbols it lists one line per active event, in recorded order: `E012 · 10-16 10:58 · decision · <title> (supersedes E007)`. Only these are listed: `user-request`, `user-addition`, `user-decision`, `plan`, `decision`, `code-change`, `blocker`, `commit`, `push`, `external-action`, any event with `--context-section question` or `next-step`, and the five most recent `verification`/`error` events. Superseded events are omitted; titles are cut at 120 characters. With more than 80 listed events, only the latest 80 keep their own line. Older ones are folded into one line per branch segment, `- … E002–E140 (earlier, 97 events): 31 decision, 42 code-change — `show --type decision,code-change` or `show --event <ID>``, except `user-request`, `user-decision`, `plan`, and `blocker` events, which always keep their own line. Superseded events are not counted. This keeps the digest at roughly 2,000 tokens however long the task runs. Whenever the branch of the listed events changes, a `_branch: <name>_` line precedes the first event on it (the first listed event always gets one). Bodies are never included — open an event with `show --event <ID>`.

Any line of `--body`, `--command`, `--result`, `--file`, or `--symbol` that looks like an event header (`### <ISO time> — …`) is stored with a leading backslash (`\### 2026-…`), so a pasted transcript fragment never becomes a phantom event or a duplicate ID. `show --event` prints the stored form. Lines that merely start with `###`, or already start with a backslash, are left as they are.

Don't auto-tag or guess supersede links for older records. A record with no explicit supersede is not proof that it's still current. Before making changes, check the current files — the user may have changed them since the last recorded event. If you establish that a record is stale, record a new observation and its basis.

Example of a new decision fully replacing an older one:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append \
  --type decision --title "Example: revised retry policy" \
  --body-file /path/to/decision.md --supersedes E042 \
  --context-section requirement --evidence-status confirmed
```

The body should contain the decision itself and the basis for confirming it (e.g. a choice the user agreed to). The flag itself is not an automatic check.

## Transcript keys and branches

- The key is the transcript's name and its file name (`<key>.md`, `<key>.digest.md`). Without `--name` it is the current branch; `--name` is accepted by `init`, `append`, `show`, `context`, `digest`, `resume`, and `finish`, and must be repeated on each call. `list` and `index` need no key.
- Allowed: any single-line name. Rejected: empty, starting with `.`, containing `..` or `\`, ending with `.digest`, equal to `INDEX` (any case). `/` is stored as `__` in the file name; the header keeps the original key.
- Uniqueness is checked by `init`: `init --name X --task T` on an existing `X` is a no-op only if `T` equals the stored task (surrounding whitespace ignored); otherwise it fails and the file stays untouched. It also fails when another transcript maps to the same file (`a/b` vs `a__b`) or differs only by case (`Shared` vs `shared`), on every filesystem.
- Without `--name`, a missing transcript is an error that points to `list` and `init`; in detached HEAD, a key must be given explicitly.
- Every new event stores `- Branch: \`<name>\``: the branch at the time of `append`, or `detached`. `list` shows the branches seen in a transcript, in order of first appearance; the digest marks branch changes.

Example: continuing one task on a second branch:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py list
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py resume --name checkout-redesign
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append --name checkout-redesign \
  --type user-addition --actor user --title "Follow-up on the new branch" --body "<verbatim user message>"
```

## --symbol naming and code-change diff format

- For a class, object, or interface, use its fully qualified name, e.g. `com.example.payments.InvoiceRenderer`.
- For a method, use the fully qualified owner and method name, e.g. `com.example.payments.InvoiceService#calculateTotal`.
- For a top-level function, use the full package and function name.
- For XML, build files, resources, and other files without a code symbol, use the resource's full name in `--symbol`, or `not applicable: <file type>`.
- Treat a `code-change` event without `--file` as incomplete and don't record it.
- In a `code-change` event's `--body`, include a short description together with a unified diff of the change (before/after state with `-U3` context), not the whole file. The diff must have hunk headers like `@@ -a,b +c,d @@` with line numbers and ±3 lines of context around the change. For a new file, its full content is the diff. Don't reuse a cumulative `git diff` against HEAD — it may include already-recorded or the user's own changes. Capture the state right before your edit and record only your own delta; re-reading the same diff is not a new event.

## Append examples

Record a user message (verbatim text in `--body`):

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append \
  --type user-request \
  --actor user \
  --title "Request: retry policy for failed payments" \
  --body "<verbatim user message, in full>"
```

Add a code-change event (body — a short description plus a unified diff with line numbers — written to a file with your editing tool, e.g. a scratch file, and passed via `--body-file`; a diff may contain backticks or `$`, so don't use inline `--body` for it):

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append \
  --type code-change \
  --actor "Example Agent" \
  --title "Fixed retry backoff calculation" \
  --body-file /path/to/scratch/code-change-body.md \
  --file "src/payments/InvoiceService.kt" \
  --symbol "com.example.payments.InvoiceService"
```

`code-change-body.md` content in this example:

```
Fixed exponential backoff to cap at the configured maximum delay.

@@ -12,7 +12,7 @@ class InvoiceService(...) {
     private fun nextDelay(attempt: Int): Duration {
         val raw = baseDelay * 2.0.pow(attempt)
-        return raw.seconds
+        return raw.seconds.coerceAtMost(maxDelay)
     }
```

For a command or a check:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append \
  --type verification \
  --actor "Example Agent" \
  --title "Ran the payments test suite" \
  --command "./gradlew :payments:test" \
  --result "Passed"
```
