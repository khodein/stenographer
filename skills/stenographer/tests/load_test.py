#!/usr/bin/env python3
"""Load test for the stenographer: a long task across several branches, driven through the real CLI.

Not part of `unittest discover` (the name doesn't start with `test`). See docs/load-testing.md.

    python3 skills/stenographer/tests/load_test.py [--events 1200] [--seed 7]

Everything happens in a temporary directory (a throwaway git repository and its own
STENOGRAPHER_ROOT); real transcripts are never touched. Exit code 0 means no problems.
"""

import argparse
import importlib.util
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

DEFAULT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stenographer.py"
MILESTONES = (50, 150, 300, 600, 1200)
KEY = "load-task"
BRANCHES = ("feature/login", "feature/login-fix", "hotfix/otp", "feature/login-ui")
EVENTS_PER_BRANCH_STINT = 40

BODIES = (
    "Plain Russian text: добавить экран входа с биометрией, проверить fallback на PIN.",
    "Code: `val x = foo()` and $HOME and \"quotes\" and 'single' and \\ backslash",
    "Balanced fence:\n```kotlin\nfun a() = 1\n```\nafter fence",
    "Unbalanced fence (diff hunk):\n\n@@ -1,3 +1,3 @@\n \n-old\n+new\n \n ```text\n <path>",
    "Tilde fence unbalanced:\n~~~\nstill open",
    "Fake header, fenced, stale ID:\n```markdown\n### 2026-01-01T00:00:00+04:00 — Fake\n- Type: `decision`\n- Author: `x`\n- ID: `E001`\n\nx\n```",
    "Line that looks like metadata:\n- ID: `E999`\n- Branch: `evil`\n- Supersedes: `E001`",
    "Long line " + "x" * 5000,
    "Multi\nline\n\n\nwith blank lines\n\n",
    "Emoji and CJK: 🚀 日本語 тест",
    "Pasted event, unfenced, reused ID:\n\n### 2026-01-01T00:00:00+04:00 — Pasted\n- Type: `decision`\n- Author: `x`\n- ID: `E001`\n\nbody",
    "Pasted event, unfenced, far-ahead ID:\n\n### 2026-01-01T00:00:00+04:00 — Pasted2\n- Type: `plan`\n- Author: `x`\n- ID: `E9999`\n\nbody",
)
# (type, weight): roughly the mix of a real task; the last five are not listed in the digest.
TYPE_WEIGHTS = (
    ("decision", 18), ("code-change", 25), ("verification", 14), ("user-addition", 6),
    ("external-action", 4), ("error", 3), ("plan", 2), ("blocker", 1), ("user-decision", 2),
    ("user-request", 1), ("research", 6), ("action", 6), ("agent-comment", 5),
    ("tool-call", 4), ("tool-result", 3),
)


class LoadTest:
    def __init__(self, script: Path, events: int, seed: int, digest_max_chars: int, keep: bool):
        spec = importlib.util.spec_from_file_location("stenographer", script)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.script, self.events, self.digest_max_chars, self.keep = script, events, digest_max_chars, keep
        self.root = Path(tempfile.mkdtemp(prefix="stenographer-load-"))
        self.repo = self.root / "demo-load"
        self.repo.mkdir()
        self.env = dict(os.environ, STENOGRAPHER_ROOT=str(self.root / "logs"))
        self.random = random.Random(seed)
        self.problems: list[str] = []
        self.bodies: dict[str, str] = {}
        self.appended = 0
        self.digest_size = self.transcript_size = 0
        self.parallel_ids: list[str] = []

    # --- helpers -----------------------------------------------------------------------------

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def cli(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(self.script), *args], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        if (result.returncode == 0) != ok:
            self.problems.append(f"unexpected exit {result.returncode} for `{' '.join(args[:3])}`: {result.stderr.strip()[:200]}")
        return result

    def append(self, event_type: str, title: str, body: str, *extra: str, actor: str = "Claude Sonnet 5.5") -> Optional[str]:
        # One file per call: parallel appends must not share a body file.
        descriptor, name = tempfile.mkstemp(suffix=".md", dir=self.root)
        with os.fdopen(descriptor, "w", encoding="utf-8") as body_file:
            body_file.write(body)
        result = self.cli("append", "--name", KEY, "--type", event_type, "--actor", actor,
                          "--title", title, "--body-file", name, *extra)
        os.unlink(name)
        match = re.search(r"E\d+", result.stdout)
        event_id = match.group(0) if result.returncode == 0 and match else None
        if event_id:
            self.bodies[event_id] = body
            self.appended += 1
        return event_id

    def random_event(self, number: int) -> tuple[str, str, str, list[str], str]:
        types, weights = zip(*TYPE_WEIGHTS)
        event_type = self.random.choices(types, weights=weights)[0]
        title = f"{event_type} #{number}: " + ("long " * self.random.choice([1, 1, 1, 30]))[:200]
        extra: list[str] = []
        if event_type == "code-change":
            extra = ["--file", f"app/File{number % 17}.kt", "--symbol", f"com.shop.File{number % 17}"]
        if event_type == "verification":
            extra = ["--command", "./gradlew test", "--result", "Passed"]
        actor = "user" if event_type.startswith("user-") else "Claude Sonnet 5.5"
        return event_type, title, self.random.choice(BODIES), extra, actor

    # --- phases ------------------------------------------------------------------------------

    def grow(self) -> None:
        self.git("init", "-q", "-b", BRANCHES[0])
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        self.cli("init", "--name", KEY, "--task", "Load test: login rework across several branches")
        number = 0
        for target in [size for size in MILESTONES if size < self.events] + [self.events]:
            started, before = time.time(), number
            while number < target:
                if number % EVENTS_PER_BRANCH_STINT == 0:
                    self.git("checkout", "-q", "-B", BRANCHES[(number // EVENTS_PER_BRANCH_STINT) % len(BRANCHES)])
                event_type, title, body, extra, actor = self.random_event(number)
                self.append(event_type, title, body, *extra, actor=actor)
                number += 1
            print(f"METRIC append latency while growing to {target}: {(time.time() - started) / (number - before) * 1000:.0f} ms")

    def supersede_chain(self) -> None:
        previous = None
        for step in range(6):
            extra = ["--supersedes", previous] if previous else []
            previous = self.append("decision", f"Chain step {step}: use approach {step}", f"approach {step}", *extra)

    def parallel_appends(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda n: self.append("decision", f"Parallel decision {n}", f"parallel body {n}"), range(48)))
        if None in ids or len(set(ids)) != 48:
            self.problems.append(f"parallel appends: {len(set(ids))} unique IDs of 48 (None means a failed append)")
        self.parallel_ids = [event_id for event_id in ids if event_id]

    def detached_head(self) -> None:
        self.git("checkout", "-q", "--detach")
        if not self.append("verification", "Detached head check", "x", "--command", "c", "--result", "ok"):
            self.problems.append("append with --name failed in detached HEAD")
        self.cli("context", ok=False)  # without --name there is no key to fall back to

    # --- checks ------------------------------------------------------------------------------

    def check_integrity(self) -> tuple[list[dict], str]:
        path = self.root / "logs" / "demo-load" / f"{KEY}.md"
        content = path.read_text(encoding="utf-8")
        events = self.m.parse_events(content)
        if [e["id"] for e in events] != [f"E{n:03d}" for n in range(1, len(events) + 1)]:
            self.problems.append("IDs are not a contiguous sequence")
        if len(events) != self.appended:
            self.problems.append(f"{len(events)} events parsed, {self.appended} appended")
        if content.endswith("\n"):
            self.problems.append("transcript ends with a newline")
        print(f"METRIC events: {len(events)}, transcript: {len(content) / 1024:.0f} KB")

        # Every parallel append is verified, plus a random sample of the rest.
        rest = sorted(set(self.bodies) - set(self.parallel_ids))
        sample = self.parallel_ids + self.random.sample(rest, min(40, len(rest)))
        escaped_only = broken = 0
        for event_id in sample:
            shown = self.cli("show", "--name", KEY, "--event", event_id).stdout
            body = self.bodies[event_id].strip()
            if self.m.escape_event_headers(self.bodies[event_id]).strip() not in shown:
                broken += 1
            elif body not in shown:
                escaped_only += 1
        if broken:
            self.problems.append(f"{broken} of {len(sample)} sampled events did not round-trip through show --event")
        print(f"METRIC sampled {len(sample)} events: {escaped_only} differ only by the header escape")
        return events, content

    def check_digest(self, events: list[dict], content: str) -> None:
        digest = (self.root / "logs" / "demo-load" / f"{KEY}.digest.md").read_text(encoding="utf-8")
        replaced = self.m.superseded_events(events)
        event_lines = [line for line in digest.splitlines() if re.match(r"- E\d+ · ", line)]
        folded = [line for line in digest.splitlines() if line.startswith("- … ")]
        print(f"METRIC digest: {len(digest)} chars (~{len(digest) // 4} tokens), {len(event_lines)} event lines, "
              f"{len(folded)} folded lines, {len(re.findall(r'^_branch: ', digest, re.M))} branch markers")
        if any(re.match(r"- (E\d+)", line).group(1) in replaced for line in event_lines):
            self.problems.append("digest lists superseded events")
        if len(digest) > self.digest_max_chars:
            self.problems.append(f"digest is {len(digest)} chars, over the {self.digest_max_chars} regression alarm")
        if self.events > 200 and not folded:
            self.problems.append("no folded lines in the digest of a long transcript")
        always = [e["id"] for e in events if e["type"] in self.m.DIGEST_ALWAYS_TYPES and e["id"] not in replaced]
        missing = [event_id for event_id in always if f"- {event_id} · " not in digest]
        if missing:
            self.problems.append(f"always-kept events missing from the digest: {missing[:5]}")
        if any(len(line) > 220 for line in event_lines):
            self.problems.append("digest event line longer than 220 chars")
        if "Chain step 5" not in digest or "Chain step 0" in digest:
            self.problems.append("supersede chain not collapsed to its last step in the digest")
        self.digest_size, self.transcript_size = len(digest), len(content)

    def check_commands(self) -> None:
        started = time.time()
        context = self.cli("context", "--name", KEY).stdout
        print(f"METRIC context: {len(context)} chars (~{len(context) // 4} tokens), {(time.time() - started) * 1000:.0f} ms, "
              f"{100 * len(context) / self.transcript_size:.1f}% of the transcript")
        sizes = {}
        for block in re.split(r"\n(?=## )", context):
            sizes[block.splitlines()[0][3:]] = len(block)
        for title, size in sorted(sizes.items(), key=lambda item: -item[1])[:5]:
            print(f"METRIC   context section {size:>7} chars: {title}")
        started = time.time()
        listing = self.cli("list").stdout
        print(f"METRIC list: {(time.time() - started) * 1000:.0f} ms")
        used = BRANCHES[:min(len(BRANCHES), (self.events - 1) // EVENTS_PER_BRANCH_STINT + 1)]
        if any(branch not in listing for branch in used):
            self.problems.append("list does not show all branches")
        started = time.time()
        self.cli("digest", "--name", KEY)
        print(f"METRIC digest rebuild: {(time.time() - started) * 1000:.0f} ms")
        print(f"METRIC digest is {100 * self.digest_size / self.transcript_size:.1f}% of the transcript")

    def check_state_transitions(self) -> None:
        digest_path = self.root / "logs" / "demo-load" / f"{KEY}.digest.md"
        self.cli("finish", "--name", KEY, "--status", "done")
        if "Status: `done`" not in digest_path.read_text(encoding="utf-8"):
            self.problems.append("digest status not refreshed after finish")
        self.cli("resume", "--name", KEY)
        if "Status: `in progress`" not in digest_path.read_text(encoding="utf-8"):
            self.problems.append("digest status not refreshed after resume")
        self.cli("init", "--name", KEY, "--task", "A different task", ok=False)

    def run(self) -> int:
        started = time.time()
        try:
            self.grow()
            self.supersede_chain()
            self.parallel_appends()
            self.detached_head()
            events, content = self.check_integrity()
            self.check_digest(events, content)
            self.check_commands()
            self.check_state_transitions()
        finally:
            print(f"METRIC total time: {time.time() - started:.1f} s")
            if self.keep:
                print(f"kept: {self.root}")
            else:
                shutil.rmtree(self.root, ignore_errors=True)
        print("RESULT: FAIL" if self.problems else "RESULT: PASS")
        for problem in self.problems:
            print(f" - {problem}")
        return 1 if self.problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", type=int, default=1200, help="events in the growth phase (default 1200)")
    parser.add_argument("--seed", type=int, default=7, help="random seed; the same seed gives the same run")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT, help="stenographer.py under test")
    parser.add_argument("--digest-max-chars", type=int, default=20000, help="digest size regression alarm")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory for inspection")
    args = parser.parse_args()
    return LoadTest(args.script.resolve(), args.events, args.seed, args.digest_max_chars, args.keep).run()


if __name__ == "__main__":
    raise SystemExit(main())
