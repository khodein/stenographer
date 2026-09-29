# Stenographer — detailed reference

Don't read this in full at the start of every task — only the relevant section, when the matching situation comes up: an existing transcript with superseded decisions, the first `code-change` event, or unclear `append` syntax. Core rules and the startup sequence are in `SKILL.md`.

## Identifiers, supersedes, and context extraction

`append` assigns an ID (`E001`, `E002`, …) under a file lock and returns it in the result. IDs are unique within a branch's transcript. Older events with no ID get a stable positional number when read, without rewriting history — so events can never be reordered or deleted. Use the ID you were given or that you read; never guess one.

When a new decision fully replaces an older one, add `--supersedes E042` to the regular event. Repeat the flag for multiple supersedes. You can only reference events that already exist in this transcript; an invalid reference is rejected before the write. The new record hides the superseded one from `context`/`digest`, but the original event is preserved and still available via `show --event E042`.

- A supersede means the record is fully displaced from the active context. For a partial correction of a compound decision, first restate the conditions that still hold alongside the new ones. Don't mark a user's whole original request as superseded just because one point was clarified.
- A plan delta doesn't supersede the base plan: record it without `--supersedes`. When fully replacing a plan, list the base plan and the deltas that no longer apply.
- For a resolved open question or blocker, record the outcome with `--supersedes` pointing at the question/blocker. Record a rejected decision as a new event with the reason and a reference, not by deleting history.
- If a supersede is later reverted, the old decision doesn't automatically come back: explicitly record the restored decision as a new event and supersede the one that reverted it.
- Use `--supersedes` only for an established replacement, not because two records sound similar. Don't supersede an agreed decision with an unconfirmed proposal.

`context` is read-only: it builds its output from the current file without an LLM, without touching the digest or the transcript. It surfaces goals, decisions, plans and deltas, results, questions, hypotheses, next steps, the last five checks/errors and user messages, files/symbols, and supersede links. Bodies longer than 800 characters are truncated with an explicit marker — this is navigation to the source, not a replacement for reading it. Full event text: `show --event <ID>`.

Don't auto-tag or guess supersede links for older records. A record with no explicit supersede is not proof that it's still current. Before making changes, check the current files — the user may have changed them since the last recorded event. If you establish that a record is stale, record a new observation and its basis.

Example of a new decision fully replacing an older one:

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append \
  --type decision --title "Example: revised retry policy" \
  --body-file /path/to/decision.md --supersedes E042 \
  --context-section requirement --evidence-status confirmed
```

The body should contain the decision itself and the basis for confirming it (e.g. a choice the user agreed to). The flag itself is not an automatic check.

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
