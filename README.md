# Stenographer

`stenographer` is a skill for AI coding agents (Claude Code, Codex, and any other agent that can read a Markdown skill file) that maintains a chronological transcript for each task. By default a task is identified by its Git branch; you can also name it explicitly and keep one transcript across several branches. It records verbatim user messages, visible AI responses, actions, tool calls, results, decisions, code changes, and verification events while excluding secrets and hidden model reasoning. It is off by default and records only while you've explicitly enabled it for the task.

## Why use it?

AI-assisted development can span long sessions, multiple agents, and repeated context resets. Important details often end up scattered across chat history, terminal output, and uncommitted changes. Stenographer turns that activity into a durable, task-specific record.

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
<stenographer repository>/transcripts/<repository>/<key>.md
```

The `transcripts/` folder is created inside the repository the skill is installed from and is listed in `.gitignore`: transcripts hold verbatim messages and diffs, so they stay local. Set `STENOGRAPHER_ROOT` to store them elsewhere. This needs the skill to be *linked* to the checkout (see Installation); if you copy it instead, transcripts are stored in a `transcripts/` folder next to `scripts/` inside the copy.

The `<key>` is the current branch name unless you choose another name with `--name`. Every event records the branch it was written on, so a transcript can follow one task through several branches.

## Installation

Clone the repository, then link the skill once into a shared location and from there into whichever agents you use — this keeps a single copy in sync everywhere, keeps the `stenographer.py` path in `SKILL.md`/`reference.md` valid regardless of which agent reads them, and lets the script find the repository where transcripts are stored:

```bash
git clone https://github.com/khodein/stenographer.git
mkdir -p ~/.local/share/skills
ln -s "$PWD/stenographer/skills/stenographer" ~/.local/share/skills/stenographer

# Claude Code
mkdir -p ~/.claude/skills
ln -s ~/.local/share/skills/stenographer ~/.claude/skills/stenographer

# Codex
mkdir -p ~/.codex/skills
ln -s ~/.local/share/skills/stenographer ~/.codex/skills/stenographer
```

Restart your agent, or start a new task, after installation. If you only use one of these agents, skip the other symlink.

## Usage

Invoke the skill explicitly with `$stenographer` (Codex) or by name (Claude Code), or add a project instruction requiring it before work in Git repositories. It is off by default: the agent never asks in advance and records only after you enable it for the task. Run every command below from the Git repository whose transcripts you want to use.

```bash
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py show
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py show --event E001
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py context
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py list
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py init --task "Task description" --name my-task
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py append --type action --title "Action" --body "Details"
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py digest
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py index
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py resume
python3 ~/.local/share/skills/stenographer/scripts/stenographer.py finish
```

- `context` builds a read-only, deterministic extract of the transcript (goals, decisions, plans, open questions, recent checks) without touching the file — useful for resuming a task without re-reading the whole log.
- `list` prints the repository's transcripts (key, status, last update, branches, task) so you can pick one to continue. Pass `--name <key>` to every command to work with a transcript other than the one named after the current branch; `init` refuses a name already taken by a different task.
- `<key>.digest.md` is a derived, token-cheap map of the transcript: one line per active event (`E012 · 10-16 10:58 · decision · title`), so an agent can scan it and open only the events it needs with `show --event`. It is updated automatically after every `init`, `append`, `resume`, and `finish`; `digest` rebuilds it manually.
- `index` regenerates a repository-wide `INDEX.md` listing every stored transcript with its status.

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
