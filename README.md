# Stenographer

`stenographer` is a skill for AI coding agents (Claude Code, Codex, and any other agent that can read a Markdown skill file) that maintains a chronological task transcript for each Git branch. It records verbatim user messages, visible AI responses, actions, tool calls, results, decisions, code changes, and verification events while excluding secrets and hidden model reasoning. The agent asks whether to enable it for each new task and only records while you've said yes.

## Why use it?

AI-assisted development can span long sessions, multiple agents, and repeated context resets. Important details often end up scattered across chat history, terminal output, and uncommitted changes. Stenographer turns that activity into a durable, branch-specific record.

This helps you:

- preserve task context between sessions and agents;
- understand why a technical decision was made;
- review which tools, commands, and checks were performed;
- trace code changes back to the request that caused them;
- hand work over without reconstructing the entire conversation;
- keep a useful engineering journal outside the project repository.

Unlike a summary written at the end, the transcript is updated throughout the task. Events stay chronological and separate, making the record useful for investigation, review, and resuming unfinished work.

## What it records

- user requests, clarifications, and decisions, verbatim;
- visible AI comments and final responses;
- actions, commands, tool calls, and their results;
- plans and their deltas, implementation decisions, and discovered constraints;
- changed files with their fully qualified symbols and a unified diff;
- verification results, errors, commits, pushes, and external actions.

The skill explicitly instructs the agent not to record secrets, raw confidential data, or hidden reasoning; the CLI itself also rejects a write if the text matches common secret patterns (private keys, bearer tokens, common API-key formats).

Every event gets a stable ID (`E001`, `E002`, …). A new decision can mark an older one as superseded with `--supersedes`, which hides the old record from derived views while keeping full history — nothing is ever rewritten or deleted.

The skill stores transcripts outside the working repository at:

```text
~/.config/ai-rules/stenographer/<repository>/<branch>.md
```

## Installation

Clone the repository, then install the skill once to a shared location and link it into whichever agents you use — this keeps a single copy in sync everywhere, and keeps the `stenographer.py` path in `SKILL.md`/`reference.md` valid regardless of which agent reads them:

```bash
git clone https://github.com/khodein/stenographer.git
mkdir -p ~/.local/share/skills
cp -R stenographer/skills/stenographer ~/.local/share/skills/

# Claude Code
mkdir -p ~/.claude/skills
ln -s ~/.local/share/skills/stenographer ~/.claude/skills/stenographer

# Codex
mkdir -p ~/.codex/skills
ln -s ~/.local/share/skills/stenographer ~/.codex/skills/stenographer
```

Restart your agent, or start a new task, after installation. If you only use one of these agents, skip the other symlink.

## Usage

Invoke the skill explicitly with `$stenographer` (Codex) or by name (Claude Code), or add a project instruction requiring it before work in Git repositories. By default the agent asks once per task whether to enable it and only records after you agree. Run every command below from the Git repository whose branch should be transcribed.

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py show
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py show --event E001
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py context
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py init --task "Task description"
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append --type action --title "Action" --body "Details"
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py digest
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py index
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py resume
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py finish
```

- `context` builds a read-only, deterministic extract of the transcript (goals, decisions, plans, open questions, recent checks) without touching the file — useful for resuming a task without re-reading the whole log.
- `digest` regenerates a derived `<branch>.digest.md` summary; `index` regenerates a repository-wide `INDEX.md` listing every stored branch transcript with its status.

See `skills/stenographer/SKILL.md` for the full agent-facing rules (what to record, what to skip, content conventions) and `skills/stenographer/reference.md` for the `--supersedes`/ID semantics, `--symbol`/diff conventions for `code-change`, and fully worked `append` examples.

## Requirements

- Python 3.9 or newer
- Git
- macOS or another Unix-like system with `fcntl`

## Development

```bash
python3 -m unittest skills/stenographer/tests/test_stenographer.py
```

## License

MIT
