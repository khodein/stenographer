#!/usr/bin/env python3

import argparse
import fcntl
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


def safe_name(value: str) -> str:
    return value.replace("/", "__")


def transcript_path() -> tuple[Path, Path, str, str]:
    repository_root, repository, branch = git_context()
    transcript_root = Path(
        os.environ.get(
            "STENOGRAPHER_ROOT",
            Path.home() / ".config" / "ai-rules" / "stenographer",
        )
    ).expanduser()
    path = transcript_root / safe_name(repository) / f"{safe_name(branch)}.md"
    return path, repository_root, repository, branch


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


def header(repository_root: Path, repository: str, branch: str, task: str) -> str:
    now = timestamp()
    return (
        f"# {branch}\n\n"
        f"- Repository: `{repository}`\n"
        f"- Path: `{repository_root}`\n"
        f"- Branch: `{branch}`\n"
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


def digest_path_for(path: Path) -> Path:
    return path.with_name(f"{path.stem}.digest.md")


def build_digest(content: str, repository: str, branch: str) -> str:
    status = read_field(content, "Status")
    created = read_field(content, "Created")
    updated = read_field(content, "Updated")
    task = read_task(content)
    events = parse_events(content)

    replaced = superseded_events(events)
    active = [event for event in events if event["id"] not in replaced]
    decisions = [event for event in active if event["type"] in {"decision", "user-decision"}]
    blockers = [event for event in active if event["type"] == "blocker"]
    plans = [event for event in active if event["type"] == "plan"]

    files: dict[str, None] = {}
    symbols: dict[str, None] = {}
    for event in events:
        if event["type"] != "code-change":
            continue
        for item in extract_list_section(event["block"], "Files"):
            files[item] = None
        for item in extract_list_section(event["block"], "Symbols"):
            symbols[item] = None

    counts: dict[str, int] = {}
    for event in events:
        counts[event["type"]] = counts.get(event["type"], 0) + 1

    lines = [
        f"# Digest: {branch}",
        "",
        f"- Repository: `{repository}`",
        f"- Branch: `{branch}`",
        f"- Status: `{status}`",
        f"- Created: `{created}`",
        f"- Updated: `{updated}`",
        f"- Total events: `{len(events)}`",
        "",
        "> Derived file, not the source of truth. If it disagrees with or lacks enough "
        "detail for a decision, check the full transcript `"
        f"{branch}.md`. Regenerate with the `digest` command.",
        "",
        "## Task",
        "",
        task,
        "",
        "## Event counts by type",
        "",
    ]
    lines.extend(f"- `{event_type}`: {counts[event_type]}" for event_type in EVENT_TYPES if event_type in counts)
    lines.append("")

    lines.append("## Decisions")
    lines.append("")
    if decisions:
        lines.extend(f"- {event['timestamp']} — {event['title']}" for event in decisions)
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## Blockers")
    lines.append("")
    if blockers:
        lines.extend(f"- {event['timestamp']} — {event['title']}" for event in blockers)
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## Affected files")
    lines.append("")
    if files:
        lines.extend(f"- `{item}`" for item in files)
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## Affected symbols")
    lines.append("")
    if symbols:
        lines.extend(f"- `{item}`" for item in symbols)
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## Plan and its deltas (in recorded order)")
    lines.append("")
    if plans:
        for plan in plans:
            lines.extend((f"_{plan['id']} · {plan['timestamp']}_", "", extract_body(plan["block"]), ""))
    else:
        lines.append("_none_")

    return "\n".join(lines)


def event_entry(args: argparse.Namespace) -> str:
    parts = [
        f"### {timestamp()} — {args.title.strip()}",
        f"- Type: `{args.type}`",
        f"- Author: `{args.actor}`",
        f"- ID: `{args.event_id}`",
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


def build_context(content: str, path: Path, repository: str, branch: str) -> str:
    events = parse_events(content)
    replaced = superseded_events(events)
    active = [event for event in events if event["id"] not in replaced]
    lines = [
        f"# Context: {repository} / {branch}", "",
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
    path, _, repository, branch = transcript_path()
    if not path.exists():
        raise RuntimeError("Transcript does not exist. Run init first")
    print(build_context(path.read_text(encoding="utf-8"), path, repository, branch))
    return 0


def command_digest() -> int:
    path, _, repository, branch = transcript_path()
    if not path.exists():
        raise RuntimeError("Transcript does not exist. Run init first")
    content = path.read_text(encoding="utf-8")
    digest = build_digest(content, repository, branch)
    digest_file = digest_path_for(path)
    lock_file = with_lock(digest_file)
    try:
        atomic_write(digest_file, digest)
    finally:
        lock_file.close()
    print(f"Digest updated: {digest_file}")
    return 0


def command_index() -> int:
    _, repository, _branch = git_context()
    transcript_root = Path(
        os.environ.get(
            "STENOGRAPHER_ROOT",
            Path.home() / ".config" / "ai-rules" / "stenographer",
        )
    ).expanduser()
    repo_dir = transcript_root / safe_name(repository)
    if not repo_dir.exists():
        raise RuntimeError(f"Repository transcript directory not found: {repo_dir}")

    entries = []
    for branch_file in sorted(repo_dir.glob("*.md")):
        if branch_file.name == "INDEX.md" or branch_file.name.endswith(".digest.md"):
            continue
        branch_content = branch_file.read_text(encoding="utf-8")
        status = read_field(branch_content, "Status")
        updated = read_field(branch_content, "Updated")
        task = read_task(branch_content).splitlines()[0] if read_task(branch_content) else ""
        entries.append((branch_file.stem, status, updated, task.replace("|", "\\|")))

    lines = [
        f"# Repository transcripts: {repository}",
        "",
        "| Branch | Status | Updated | Task |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| `{branch}` | {status} | {updated} | {task} |" for branch, status, updated, task in entries)

    index_file = repo_dir / "INDEX.md"
    lock_file = with_lock(index_file)
    try:
        atomic_write(index_file, "\n".join(lines))
    finally:
        lock_file.close()
    print(f"Index updated: {index_file}")
    return 0


def command_init(task: str) -> int:
    path, repository_root, repository, branch = transcript_path()
    validate_text([task])
    lock_file = with_lock(path)
    try:
        if path.exists():
            print(f"Transcript already exists: {path}")
            return 0
        atomic_write(path, header(repository_root, repository, branch, task))
    finally:
        lock_file.close()
    print(f"Transcript created: {path}")
    return 0


def command_append(args: argparse.Namespace) -> int:
    path, _, _, _ = transcript_path()
    if args.body_file:
        if args.body is not None:
            raise RuntimeError(
                "Both --body and --body-file were given — choose one way to pass the text"
            )
        body_file = Path(args.body_file).expanduser()
        if not body_file.exists():
            raise RuntimeError(f"--body-file path not found: {body_file}")
        args.body = body_file.read_text(encoding="utf-8").rstrip("\n")
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
        raise RuntimeError("Transcript does not exist. Run init first")
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
        content = update_timestamp(content)
        atomic_write(path, f"{content}\n\n{event_entry(args)}")
    finally:
        lock_file.close()
    print(f"Added event {args.event_id}: {path}")
    return 0


def command_resume() -> int:
    path, _, _, _ = transcript_path()
    if not path.exists():
        raise RuntimeError("Transcript does not exist. Run init first")
    lock_file = with_lock(path)
    try:
        content = path.read_text(encoding="utf-8").rstrip("\n")
        content = update_status(content, "in progress")
        content = update_timestamp(content)
        atomic_write(path, content)
    finally:
        lock_file.close()
    print("Resumed work on the transcript: in progress")
    return 0


def command_finish(status: str) -> int:
    path, _, _, _ = transcript_path()
    status = status.strip()
    if not status:
        raise RuntimeError("Finish status must not be empty")
    if not path.exists():
        raise RuntimeError("Transcript does not exist. Run init first")
    lock_file = with_lock(path)
    try:
        content = path.read_text(encoding="utf-8").rstrip("\n")
        content = update_status(content, status)
        content = update_timestamp(content)
        atomic_write(path, content)
    finally:
        lock_file.close()
    print(f"Status updated: {status}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AI agent work stenographer for a Git branch")
    commands = root.add_subparsers(dest="action", required=True)

    show = commands.add_parser("show", help="Show the current branch transcript")
    show.add_argument(
        "--type",
        dest="types",
        help="Comma-separated list of event types to filter output, e.g. decision,blocker,plan",
    )
    show.add_argument("--event", help="Show the full event by ID, e.g. E001")

    init = commands.add_parser("init", help="Create the current branch's transcript")
    init.add_argument("--task", required=True, help="Original task description")

    append = commands.add_parser("append", help="Add an event")
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
    append.add_argument("--file", action="append")
    append.add_argument("--symbol", action="append")
    append.add_argument("--command")
    append.add_argument("--result")
    append.add_argument("--supersedes", action="append", help="ID of the event being superseded; repeatable")
    append.add_argument("--context-section", dest="context_section", choices=CONTEXT_SECTIONS)
    append.add_argument("--evidence-status", dest="evidence_status", choices=("confirmed", "hypothesis", "proposed"))

    commands.add_parser("context", help="Extract context without modifying the transcript")

    commands.add_parser("digest", help="Regenerate the current branch's derived digest")

    commands.add_parser("index", help="Regenerate the current repository's branch index")

    commands.add_parser("resume", help="Resume work on an existing transcript")

    finish = commands.add_parser("finish", help="Mark work on the branch as finished")
    finish.add_argument("--status", default="done", help="Final transcript status")
    return root


def main() -> int:
    args = parser().parse_args()
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
