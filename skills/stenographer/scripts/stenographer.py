#!/usr/bin/env python3

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional


EVENT_TYPES = (
    "user-request",
    "user-addition",
    "user-decision",
    "agent-comment",
    "agent-response",
    "plan",
    "research",
    "decision",
    "action",
    "tool-call",
    "tool-result",
    "code-change",
    "verification",
    "error",
    "blocker",
    "commit",
    "push",
    "external-action",
)

# Event types listed in the digest; verification/error are limited to the most recent ones.
DIGEST_TYPES = (
    "user-request",
    "user-addition",
    "user-decision",
    "plan",
    "decision",
    "code-change",
    "blocker",
    "commit",
    "push",
    "external-action",
)
DIGEST_SECTIONS = ("question", "next-step")
DIGEST_RECENT_TYPES = ("verification", "error")
DIGEST_RECENT_LIMIT = 5
DIGEST_TITLE_LIMIT = 120

# Transcript key chosen with --name; None means "use the current branch".
TRANSCRIPT_KEY: Optional[str] = None

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def run_git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not resolve Git context")
    return result.stdout.strip()


def git_context() -> tuple[Path, str, str]:
    repository_root = Path(run_git("rev-parse", "--show-toplevel")).resolve()
    branch = run_git("branch", "--show-current")
    if not branch:
        raise RuntimeError("Current HEAD is not on a named Git branch")
    return repository_root, repository_root.name, branch


def current_branch() -> str:
    """The branch an event was recorded on; "detached" when HEAD isn't on a named branch."""
    return run_git("branch", "--show-current").replace("`", "'") or "detached"


def transcripts_root(script: Path = Path(__file__)) -> Path:
    """STENOGRAPHER_ROOT, else `transcripts/` in the repository this skill is checked out from.

    When the skill was copied instead of linked, there is no repository above it:
    fall back to `transcripts/` inside the skill directory.
    """
    override = os.environ.get("STENOGRAPHER_ROOT")
    if override:
        return Path(override).expanduser()
    skill_dir = script.resolve().parents[1]
    checkout = skill_dir.parents[1]
    if skill_dir.parent.name == "skills" and (checkout / ".git").exists():
        return checkout / "transcripts"
    return skill_dir / "transcripts"


def safe_name(value: str) -> str:
    return value.replace("/", "__")


def validate_key(key: str) -> str:
    key = key.strip()
    if not key or "\n" in key or "\r" in key or "\\" in key:
        raise RuntimeError("--name must be a non-empty single-line name without backslashes")
    if key.startswith(".") or ".." in key or key.endswith(".digest") or key.lower() == "index":
        raise RuntimeError(f"--name {key!r} is reserved or unsafe: don't start it with '.', include '..', end with '.digest', or use 'INDEX'")
    return key


def missing_transcript_error(key: str) -> RuntimeError:
    return RuntimeError(
        f"Transcript '{key}' does not exist. Create it with `init --task \"...\"` "
        "(add --name <key> to choose a name other than the current branch), "
        "or pass --name of an existing one; `list` shows the existing transcripts"
    )


def transcript_path() -> tuple[Path, Path, str, str]:
    """Returns the transcript path, repository root, repository name, and the transcript key (--name or the branch)."""
    if TRANSCRIPT_KEY is None:
        repository_root, repository, key = git_context()
    else:
        repository_root = Path(run_git("rev-parse", "--show-toplevel")).resolve()
        repository, key = repository_root.name, validate_key(TRANSCRIPT_KEY)
    path = transcripts_root() / safe_name(repository) / f"{safe_name(key)}.md"
    return path, repository_root, repository, key


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_text(values: list[str]) -> None:
    joined = "\n".join(values)
    for pattern in SECRET_PATTERNS:
        if pattern.search(joined):
            raise RuntimeError("Record rejected: content resembling a secret was detected")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as temporary:
        temporary.write(content.rstrip("\n"))
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def with_lock(path: Path):
    lock_path = path.parent / f".{path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def header(repository_root: Path, repository: str, key: str, task: str) -> str:
    now = timestamp()
    return (
        f"# {key}\n\n"
        f"- Repository: `{repository}`\n"
        f"- Path: `{repository_root}`\n"
        f"- Key: `{key}`\n"
        f"- Created: `{now}`\n"
        f"- Updated: `{now}`\n"
        f"- Status: `in progress`\n\n"
        "## Context\n\n"
        f"{task.strip()}\n\n"
        "## Events"
    )


def update_timestamp(content: str) -> str:
    return re.sub(
        r"(?m)^- Updated: `[^`]+`$",
        f"- Updated: `{timestamp()}`",
        content,
        count=1,
    )


def update_status(content: str, status: str) -> str:
    return re.sub(
        r"(?m)^- Status: `[^`]+`$",
        f"- Status: `{status}`",
        content,
        count=1,
    )


EVENT_HEADER_RE = re.compile(
    r"(?m)^### (?P<timestamp>\d{4}-\d{2}-\d{2}T\S+) — (?P<title>.+)\n"
    r"- Type: `[^`]+`\n- Author: `[^`]+`"
)
CONTEXT_SECTIONS = ("goal", "requirement", "result", "question", "next-step")


def metadata(block: str, label: str) -> str:
    return read_field(block.split("\n\n", 1)[0], label)


def parse_events(content: str) -> list[dict]:
    # Event-like examples inside a Markdown code fence are not transcript events.
    fenced_offsets = set()
    fence = None
    offset = 0
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\n"))
        if fence is not None:
            fenced_offsets.add(offset)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
        offset += len(line)
    matches = [match for match in EVENT_HEADER_RE.finditer(content) if match.start() not in fenced_offsets]
    events = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        block = content[match.start():end].rstrip("\n")
        type_match = re.search(r"(?m)^- Type: `([^`]+)`$", block)
        events.append(
            {
                "timestamp": match.group("timestamp"),
                "title": match.group("title").strip(),
                "type": type_match.group(1) if type_match else "",
                "block": block,
                # Older events get a stable positional number without migrating the file.
                "id": metadata(block, "ID") or f"E{index + 1:03d}",
                "branch": metadata(block, "Branch"),
                "supersedes": metadata(block, "Supersedes").split(),
                "section": metadata(block, "Section"),
                "status": metadata(block, "Evidence") or "not set",
            }
        )
    ids = [event["id"] for event in events]
    if any(not re.fullmatch(r"E\d{3,}", event_id) for event_id in ids):
        raise RuntimeError("Invalid event ID: expected E001 or higher")
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate event IDs in the transcript")
    return events


def superseded_events(events: list[dict]) -> dict[str, str]:
    seen = set()
    replaced = {}
    for event in events:
        for target in event["supersedes"]:
            if target not in seen:
                raise RuntimeError(f"Invalid supersede reference: {event['id']} → {target}")
            replaced[target] = event["id"]
        seen.add(event["id"])
    return replaced


def extract_list_section(block: str, label: str) -> list[str]:
    match = re.search(rf"(?m)^{label}:\n((?:- `[^`]+`\n?)+)", block)
    if not match:
        return []
    return re.findall(r"`([^`]+)`", match.group(1))


def extract_body(block: str) -> str:
    without_meta = re.sub(
        r"\A### [^\n]+\n(?:- [^\n]+\n?)+", "", block, count=1
    )
    cut_match = re.search(r"\n(Files:|Symbols:|Command: `|Result:)", without_meta)
    if cut_match:
        without_meta = without_meta[: cut_match.start()]
    return without_meta.strip()


def read_field(content: str, label: str) -> str:
    match = re.search(rf"(?m)^- {label}: `([^`]+)`$", content)
    return match.group(1) if match else ""


def read_task(content: str) -> str:
    match = re.search(r"## Context\n\n(.*?)\n\n## Events", content, re.DOTALL)
    return match.group(1).strip() if match else ""


def shorten_title(title: str) -> str:
    return title if len(title) <= DIGEST_TITLE_LIMIT else title[: DIGEST_TITLE_LIMIT - 1].rstrip() + "…"


def short_time(value: str) -> str:
    match = re.match(r"\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})", value)
    return f"{match[1]} {match[2]}" if match else value


def digest_path_for(path: Path) -> Path:
    return path.with_name(f"{path.stem}.digest.md")


def build_digest(content: str, repository: str, key: str) -> str:
    status = read_field(content, "Status")
    created = read_field(content, "Created")
    updated = read_field(content, "Updated")
    task = read_task(content)
    events = parse_events(content)

    replaced = superseded_events(events)
    replacements: dict[str, list[str]] = {}
    for event in events:
        for target in event["supersedes"]:
            replacements.setdefault(event["id"], []).append(target)

    active = [event for event in events if event["id"] not in replaced]
    recent_checks = {
        event["id"] for event in [e for e in active if e["type"] in DIGEST_RECENT_TYPES][-DIGEST_RECENT_LIMIT:]
    }
    listed = [
        event
        for event in active
        if event["type"] in DIGEST_TYPES
        or event["section"] in DIGEST_SECTIONS
        or event["id"] in recent_checks
    ]

    files: dict[str, None] = {}
    symbols: dict[str, None] = {}
    for event in events:
        if event["type"] != "code-change":
            continue
        for item in extract_list_section(event["block"], "Files"):
            files[item] = None
        for item in extract_list_section(event["block"], "Symbols"):
            symbols[item] = None

    lines = [
        f"# Digest: {key}",
        "",
        f"- Repository: `{repository}`",
        f"- Key: `{key}`",
        f"- Status: `{status}`",
        f"- Created: `{created}`",
        f"- Updated: `{updated}`",
        f"- Total events: `{len(events)}`",
        "",
        "> Derived file, not the source of truth. One line per active event: "
        "`<ID> · <time> · <type> · <title>`. For details run `show --event <ID>`; "
        "if it disagrees with or lacks enough detail, check the full transcript `"
        f"{safe_name(key)}.md`. Regenerate with the `digest` command.",
        "",
        "## Task",
        "",
        task,
        "",
        "## Affected files",
        "",
    ]
    if files:
        lines.extend(f"- `{item}`" for item in files)
    else:
        lines.append("_none_")
    lines.extend(["", "## Affected symbols", ""])
    if symbols:
        lines.extend(f"- `{item}`" for item in symbols)
    else:
        lines.append("_none_")
    lines.extend(["", "## Events", ""])
    if listed:
        shown_branch = ""
        for event in listed:
            if event["branch"] and event["branch"] != shown_branch:
                shown_branch = event["branch"]
                lines.append(f"_branch: {shown_branch}_")
            note = f" (supersedes {', '.join(replacements[event['id']])})" if event["id"] in replacements else ""
            lines.append(
                f"- {event['id']} · {short_time(event['timestamp'])} · {event['type']} · "
                f"{shorten_title(event['title'])}{note}"
            )
    else:
        lines.append("_none_")

    return "\n".join(lines)


def event_entry(args: argparse.Namespace) -> str:
    parts = [
        f"### {timestamp()} — {args.title.strip()}",
        f"- Type: `{args.type}`",
        f"- Author: `{args.actor}`",
        f"- ID: `{args.event_id}`",
        f"- Branch: `{args.branch}`",
    ]
    if args.supersedes:
        parts.append(f"- Supersedes: `{' '.join(args.supersedes)}`")
    if args.context_section:
        parts.append(f"- Section: `{args.context_section}`")
    if args.evidence_status:
        parts.append(f"- Evidence: `{args.evidence_status}`")
    if args.body:
        parts.extend(("", args.body))
    if args.file:
        parts.extend(("", "Files:"))
        parts.extend(f"- `{item}`" for item in args.file)
    if args.symbol:
        parts.extend(("", "Symbols:"))
        parts.extend(f"- `{item}`" for item in args.symbol)
    if args.command:
        parts.extend(("", f"Command: `{args.command}`"))
    if args.result:
        parts.extend(("", f"Result: {args.result.strip()}"))
    return "\n".join(parts)


def command_show(types: Optional[str], event_id: Optional[str] = None) -> int:
    path, _, _, _ = transcript_path()
    print(f"TRANSCRIPT_PATH={path}")
    if not path.exists():
        print("TRANSCRIPT_STATUS=missing")
        return 0
    print("TRANSCRIPT_STATUS=existing")
    content = path.read_text(encoding="utf-8")
    if event_id:
        events = parse_events(content)
        replaced = superseded_events(events)
        event = next((event for event in events if event["id"] == event_id), None)
        if event is None:
            raise RuntimeError(f"Event {event_id} not found")
        print(f"EVENT_ID={event_id}")
        print(f"SUPERSEDED_BY={replaced.get(event_id, 'no explicit supersede')}")
        print(event["block"])
        return 0
    if not types:
        print(content)
        return 0
    wanted = {item.strip() for item in types.split(",") if item.strip()}
    unknown = wanted - set(EVENT_TYPES)
    if unknown:
        raise RuntimeError(f"Unknown event types: {', '.join(sorted(unknown))}")
    filtered = [event["block"] for event in parse_events(content) if event["type"] in wanted]
    print("\n\n".join(filtered) if filtered else "(no events of the selected types)")
    return 0


def build_context(content: str, path: Path, repository: str, key: str) -> str:
    events = parse_events(content)
    replaced = superseded_events(events)
    active = [event for event in events if event["id"] not in replaced]
    lines = [
        f"# Context: {repository} / {key}", "",
        f"Source: {path}",
        f"Updated in transcript: {read_field(content, 'Updated') or 'unknown'}",
        f"Branch status per record: {read_field(content, 'Status') or 'unknown'}", "",
        "This is an extract of records, not a check of the current code. The absence of an explicit supersede does not prove it's still current.",
        "Full text: show --event E001 (substitute the ID). Evidence status is set by the author, not computed by the tool.",
        "For older records the ID is computed positionally; never reorder or delete events.", "",
        "## Original task", "", read_task(content) or "Not set.",
    ]

    def section(title: str, selected: list[dict]) -> None:
        lines.extend(("", f"## {title}", ""))
        if not selected:
            lines.append("No explicit records; state unknown.")
        for event in selected:
            line_number = content[:content.index(event["block"])].count("\n") + 1
            lines.append(
                f"- [{event['id']}]({path}#L{line_number}) · {event['title']} "
                f"· {event['type']} · evidence: {event['status']}"
            )
            body = extract_body(event["block"])
            if body:
                excerpt = body if len(body) <= 800 else body[:800] + "… [excerpt; full text via show --event]"
                lines.extend("  " + line for line in excerpt.splitlines())
            result = re.search(r"(?m)^Result: (.*)", event["block"])
            command = re.search(r"(?m)^Command: `(.*)`$", event["block"])
            if command:
                lines.append("  Command: " + command.group(1))
            if result:
                lines.append("  Result: " + result.group(1))

    section("Goals and requirements with no explicit supersede", [e for e in active if e["section"] in {"goal", "requirement"}])
    section("Decisions with no explicit supersede", [e for e in active if e["type"] in {"decision", "user-decision"}])
    section("Plans and deltas in recorded order", [e for e in active if e["type"] == "plan"])
    section("Results marked by the author", [e for e in active if e["section"] == "result"])
    section("Questions and blockers with no explicit supersede", [e for e in active if e["section"] == "question" or e["type"] == "blocker"])
    section("Hypotheses and proposals", [e for e in active if e["status"] in {"hypothesis", "proposed"}])
    section("Explicitly stated next steps", [e for e in active if e["section"] == "next-step"])
    section("Latest checks and errors (up to 5; not a guarantee of current state)",
            [e for e in active if e["type"] in {"verification", "error"}][-5:])
    section("Latest user messages (up to 5; the rest via show)",
            [e for e in events if e["type"].startswith("user-")][-5:])
    lines.extend(("", "## Files and symbols from recorded changes", ""))
    changes = [e for e in events if e["type"] == "code-change"]
    files = {}
    for event in changes:
        for file in extract_list_section(event["block"], "Files"):
            files[file] = event
    for file, event in files.items():
        symbols = ", ".join(extract_list_section(event["block"], "Symbols")) or "not set"
        lines.append(f"- `{file}` · {event['id']} · {symbols}")
    if not files:
        lines.append("No recorded changes.")
    lines.extend(("", "## Explicit supersedes", ""))
    lines.extend(f"- {old} → {new}" for old, new in replaced.items())
    if not replaced:
        lines.append("None recorded. Supersede links are not auto-restored for older records.")
    return "\n".join(lines)


def command_context() -> int:
    path, _, repository, key = transcript_path()
    if not path.exists():
        raise missing_transcript_error(key)
    print(build_context(path.read_text(encoding="utf-8"), path, repository, key))
    return 0


def write_digest(path: Path, content: str, repository: str, key: str) -> Path:
    digest_file = digest_path_for(path)
    digest = build_digest(content, repository, key)
    lock_file = with_lock(digest_file)
    try:
        atomic_write(digest_file, digest)
    finally:
        lock_file.close()
    return digest_file


def refresh_digest(path: Path, content: str, repository: str, key: str) -> None:
    # The transcript is already saved; a failed derived digest must not fail the command.
    try:
        write_digest(path, content, repository, key)
    except Exception as error:
        print(f"Warning: digest not updated: {error}", file=sys.stderr)


def command_digest() -> int:
    path, _, repository, key = transcript_path()
    if not path.exists():
        raise missing_transcript_error(key)
    digest_file = write_digest(path, path.read_text(encoding="utf-8"), repository, key)
    print(f"Digest updated: {digest_file}")
    return 0


def repository_transcripts_dir() -> tuple[str, Path]:
    repository = Path(run_git("rev-parse", "--show-toplevel")).resolve().name
    return repository, transcripts_root() / safe_name(repository)


def command_list() -> int:
    repository, repo_dir = repository_transcripts_dir()
    entries = []
    for transcript_file in repo_dir.glob("*.md") if repo_dir.exists() else ():
        if transcript_file.name == "INDEX.md" or transcript_file.name.endswith(".digest.md"):
            continue
        content = transcript_file.read_text(encoding="utf-8")
        task = read_task(content).splitlines()[0] if read_task(content) else ""
        try:
            branches = list(dict.fromkeys(event["branch"] for event in parse_events(content) if event["branch"]))
        except RuntimeError:
            branches = ["?"]
        entries.append(
            (
                read_field(content, "Key") or transcript_file.stem,
                read_field(content, "Status"),
                read_field(content, "Updated"),
                ", ".join(branches).replace("|", "\\|"),
                task.replace("|", "\\|"),
            )
        )
    if not entries:
        print(f"No transcripts for repository {repository}. Create one with `init --task \"...\" [--name <key>]`")
        return 0
    print(f"Transcripts of repository {repository} in {repo_dir} (newest first). Continue one by passing --name <key> to every command:")
    print()
    print("| Key | Status | Updated | Branches | Task |")
    print("| --- | --- | --- | --- | --- |")
    for key, status, updated, branches, task in sorted(entries, key=lambda entry: entry[2], reverse=True):
        print(f"| `{key}` | {status} | {updated} | {branches} | {task} |")
    return 0


def command_index() -> int:
    repository, repo_dir = repository_transcripts_dir()
    if not repo_dir.exists():
        raise RuntimeError(f"Repository transcript directory not found: {repo_dir}")

    entries = []
    for transcript_file in sorted(repo_dir.glob("*.md")):
        if transcript_file.name == "INDEX.md" or transcript_file.name.endswith(".digest.md"):
            continue
        transcript_content = transcript_file.read_text(encoding="utf-8")
        status = read_field(transcript_content, "Status")
        updated = read_field(transcript_content, "Updated")
        task = read_task(transcript_content).splitlines()[0] if read_task(transcript_content) else ""
        entries.append((transcript_file.stem, status, updated, task.replace("|", "\\|")))

    lines = [
        f"# Repository transcripts: {repository}",
        "",
        "| Key | Status | Updated | Task |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| `{key}` | {status} | {updated} | {task} |" for key, status, updated, task in entries)

    index_file = repo_dir / "INDEX.md"
    lock_file = with_lock(index_file)
    try:
        atomic_write(index_file, "\n".join(lines))
    finally:
        lock_file.close()
    print(f"Index updated: {index_file}")
    return 0


def ensure_key_is_free(path: Path, key: str, task: str) -> None:
    """Rejects a key that would land in someone else's transcript; the same key with the same task is fine."""
    for other in path.parent.glob("*.md"):
        if other.name == "INDEX.md" or other.name.endswith(".digest.md"):
            continue
        if other.stem.lower() == path.stem.lower() and other.stem != path.stem:
            raise RuntimeError(
                f"Name '{key}' conflicts with the existing transcript '{other.stem}' "
                "(names that differ only by case are treated as the same); choose another --name"
            )
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    stored_key = read_field(content, "Key")
    if stored_key != key:
        raise RuntimeError(
            f"Name '{key}' maps to the same file as the existing transcript '{stored_key}'; choose another --name"
        )
    stored_task = read_task(content).strip()
    if stored_task != task.strip():
        first_line = stored_task.splitlines()[0] if stored_task else ""
        raise RuntimeError(
            f"Name '{key}' is taken by a transcript with another task: \"{first_line}\". "
            "Continue it without init, or choose another --name for a new task (`list` shows the existing ones)"
        )


def command_init(task: str) -> int:
    path, repository_root, repository, key = transcript_path()
    validate_text([task])
    lock_file = with_lock(path)
    try:
        ensure_key_is_free(path, key, task)
        if path.exists():
            if not digest_path_for(path).exists():
                refresh_digest(path, path.read_text(encoding="utf-8"), repository, key)
            print(f"Transcript already exists: {path}")
            return 0
        content = header(repository_root, repository, key, task)
        atomic_write(path, content)
        refresh_digest(path, content, repository, key)
    finally:
        lock_file.close()
    print(f"Transcript created: {path}")
    return 0


def last_turn_usage() -> Optional[str]:
    """Sums the tokens of AI responses after the last user message from the Claude Code session transcript."""
    project_dir = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    sessions = sorted(project_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not sessions:
        return None
    usage_by_message: dict = {}
    for line in sessions[0].read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("isSidechain"):
            continue
        message = record.get("message") or {}
        if record.get("type") == "user" and not record.get("isMeta"):
            content = message.get("content")
            if isinstance(content, str) or any(
                isinstance(block, dict) and block.get("type") == "text" for block in content or []
            ):
                usage_by_message = {}
        elif record.get("type") == "assistant" and message.get("usage"):
            usage_by_message[message.get("id") or record.get("uuid")] = message["usage"]
    if not usage_by_message:
        return None
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
    for usage in usage_by_message.values():
        for key in totals:
            totals[key] += usage.get(key) or 0
    return (
        f"Turn tokens (from the session transcript, {len(usage_by_message)} API calls): "
        f"input {totals['input_tokens']}, cache read {totals['cache_read_input_tokens']}, "
        f"cache write {totals['cache_creation_input_tokens']}, output {totals['output_tokens']}"
    )


def command_append(args: argparse.Namespace) -> int:
    path, _, repository, key = transcript_path()
    if args.body_file:
        if args.body is not None:
            raise RuntimeError(
                "Both --body and --body-file were given — choose one way to pass the text"
            )
        body_file = Path(args.body_file).expanduser()
        if not body_file.exists():
            raise RuntimeError(f"--body-file path not found: {body_file}")
        args.body = body_file.read_text(encoding="utf-8").rstrip("\n")
    if args.with_usage:
        usage_line = last_turn_usage()
        if usage_line is None:
            raise RuntimeError("Claude Code session transcript with token usage not found")
        args.body = f"{args.body or ''}\n\n{usage_line}".strip()
    values = [
        args.title,
        args.body or "",
        args.command or "",
        args.result or "",
        *(args.file or []),
        *(args.symbol or []),
    ]
    validate_text(values)
    if not args.title.strip() or not any((value or "").strip() for value in (args.body, args.result, args.command)):
        raise RuntimeError("An event must have a non-empty title and a body, command, or result")
    if any("\n" in value or "\r" in value for value in (args.title, args.actor)):
        raise RuntimeError("Title and actor must each be a single line")
    if args.type in {
        "user-request",
        "user-addition",
        "user-decision",
        "agent-comment",
        "agent-response",
    } and not (args.body or "").strip():
        raise RuntimeError(f"Event type {args.type} requires verbatim --body text")
    if args.type == "code-change" and not args.file:
        raise RuntimeError(
            "Event type code-change requires a full --file path"
        )
    if args.file and not args.symbol:
        raise RuntimeError(
            "When --file is given, --symbol is required: the fully qualified class/"
            "function name, or 'not applicable: <file type>' for files without a code symbol"
        )
    if not path.exists():
        raise missing_transcript_error(key)
    lock_file = with_lock(path)
    try:
        content = path.read_text(encoding="utf-8").rstrip("\n")
        events = parse_events(content)
        superseded_events(events)
        ids = {event["id"] for event in events}
        unknown = set(args.supersedes or []) - ids
        if unknown:
            raise RuntimeError(f"Supersede targets not found: {', '.join(sorted(unknown))}")
        number = max((int(event_id[1:]) for event_id in ids), default=0) + 1
        args.event_id = f"E{number:03d}"
        args.branch = current_branch()
        content = update_timestamp(content)
        content = f"{content}\n\n{event_entry(args)}"
        atomic_write(path, content)
        refresh_digest(path, content, repository, key)
    finally:
        lock_file.close()
    print(f"Added event {args.event_id}: {path}")
    return 0


def command_resume() -> int:
    path, _, repository, key = transcript_path()
    if not path.exists():
        raise missing_transcript_error(key)
    lock_file = with_lock(path)
    try:
        content = path.read_text(encoding="utf-8").rstrip("\n")
        content = update_status(content, "in progress")
        content = update_timestamp(content)
        atomic_write(path, content)
        refresh_digest(path, content, repository, key)
    finally:
        lock_file.close()
    print("Resumed work on the transcript: in progress")
    return 0


def command_finish(status: str) -> int:
    path, _, repository, key = transcript_path()
    status = status.strip()
    if not status:
        raise RuntimeError("Finish status must not be empty")
    if not path.exists():
        raise missing_transcript_error(key)
    lock_file = with_lock(path)
    try:
        content = path.read_text(encoding="utf-8").rstrip("\n")
        content = update_status(content, status)
        content = update_timestamp(content)
        atomic_write(path, content)
        refresh_digest(path, content, repository, key)
    finally:
        lock_file.close()
    print(f"Status updated: {status}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AI agent work stenographer for a task transcript")
    commands = root.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--name",
        help="Transcript key (file name) instead of the current Git branch; pass it on every command for that transcript",
    )

    show = commands.add_parser("show", parents=[common], help="Show the transcript")
    show.add_argument(
        "--type",
        dest="types",
        help="Comma-separated list of event types to filter output, e.g. decision,blocker,plan",
    )
    show.add_argument("--event", help="Show the full event by ID, e.g. E001")

    init = commands.add_parser("init", parents=[common], help="Create the transcript")
    init.add_argument("--task", required=True, help="Original task description")

    append = commands.add_parser("append", parents=[common], help="Add an event")
    append.add_argument("--type", required=True, choices=EVENT_TYPES)
    append.add_argument("--title", required=True)
    append.add_argument(
        "--actor",
        default=os.environ.get("STENOGRAPHER_ACTOR", "agent"),
        help=(
            "user for user events, the agent's model name (e.g. "
            "'Claude Opus 4.8') for AI actions, system for system events"
        ),
    )
    append.add_argument("--body")
    append.add_argument(
        "--body-file",
        help=(
            "Path to a file with the event body instead of --body. Avoids shell-"
            "escaping issues (backticks, $, quotes) in long or Markdown text: "
            "write the text to a file with your editing tool and pass its path here"
        ),
    )
    append.add_argument(
        "--with-usage",
        action="store_true",
        help="Append the sum of AI response tokens for the current turn to the body (from the Claude Code session transcript)",
    )
    append.add_argument("--file", action="append")
    append.add_argument("--symbol", action="append")
    append.add_argument("--command")
    append.add_argument("--result")
    append.add_argument("--supersedes", action="append", help="ID of the event being superseded; repeatable")
    append.add_argument("--context-section", dest="context_section", choices=CONTEXT_SECTIONS)
    append.add_argument("--evidence-status", dest="evidence_status", choices=("confirmed", "hypothesis", "proposed"))

    commands.add_parser("context", parents=[common], help="Extract context without modifying the transcript")

    commands.add_parser("digest", parents=[common], help="Regenerate the transcript's derived digest")

    commands.add_parser("list", help="List the transcripts of the current repository (read-only)")

    commands.add_parser("index", help="Regenerate the current repository's transcript index")

    commands.add_parser("resume", parents=[common], help="Resume work on an existing transcript")

    finish = commands.add_parser("finish", parents=[common], help="Mark work on the transcript as finished")
    finish.add_argument("--status", default="done", help="Final transcript status")
    return root


def main() -> int:
    args = parser().parse_args()
    global TRANSCRIPT_KEY
    TRANSCRIPT_KEY = getattr(args, "name", None)
    try:
        if args.action == "show":
            if args.types and args.event:
                raise RuntimeError("Choose --type or --event")
            return command_show(args.types, args.event)
        if args.action == "context":
            return command_context()
        if args.action == "init":
            return command_init(args.task)
        if args.action == "append":
            return command_append(args)
        if args.action == "digest":
            return command_digest()
        if args.action == "list":
            return command_list()
        if args.action == "index":
            return command_index()
        if args.action == "resume":
            return command_resume()
        if args.action == "finish":
            return command_finish(args.status)
        raise RuntimeError(f"Unknown command: {args.action}")
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
