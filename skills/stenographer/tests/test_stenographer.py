import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'stenographer.py'
spec = importlib.util.spec_from_file_location('stenographer', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StenographerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-b', 'test', str(self.repo)], check=True, capture_output=True)
        self.env = dict(os.environ, STENOGRAPHER_ROOT=str(self.root / 'logs'))
        self.run_cli('init', '--task', 'Context check')
        self.log = self.root / 'logs' / 'repo' / 'test.md'

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
        return result.stdout

    def append(self, title, *args):
        return self.run_cli('append', '--type', 'decision', '--title', title,
                            '--body', title, *args)

    def test_replacement_chain_and_read_only_context(self):
        self.append('First decision')
        self.append('Second decision', '--supersedes', 'E001')
        self.append('Third decision', '--supersedes', 'E002', '--evidence-status', 'confirmed')
        before = self.log.read_bytes()
        context = self.run_cli('context')
        self.assertIn('Third decision', context)
        self.assertNotIn('First decision', context)
        self.assertNotIn('Second decision', context)
        self.assertIn('E001 → E002', context)
        self.assertIn('E002 → E003', context)
        self.assertIn('confirmed', context)
        self.assertEqual(self.log.read_bytes(), before)
        shown = self.run_cli('show', '--event', 'E001')
        self.assertIn('First decision', shown)
        self.assertIn('SUPERSEDED_BY=E002', shown)

    def test_invalid_reference_and_empty_message_do_not_write(self):
        before = self.log.read_bytes()
        self.run_cli('append', '--type', 'decision', '--title', 'x', '--body', 'x',
                     '--supersedes', 'E099', ok=False)
        self.run_cli('append', '--type', 'user-request', '--title', 'x', '--body', '  ', ok=False)
        self.assertEqual(self.log.read_bytes(), before)

    def test_legacy_ids_are_stable_after_append(self):
        legacy = '\n\n### 2026-09-01T12:00:00+04:00 — Legacy decision\n- Type: `decision`\n- Author: `agent`\n\nLegacy body'
        self.log.write_text(self.log.read_text() + legacy)
        self.append('New decision', '--supersedes', 'E001')
        self.assertIn(legacy, self.log.read_text())
        self.assertIn('E002', self.run_cli('context'))
        self.assertIn('Legacy body', self.run_cli('show', '--event', 'E001'))

    def test_parallel_append_assigns_unique_ids(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda n: self.append(f'Decision {n}'), range(12)))
        events = m.parse_events(self.log.read_text())
        self.assertEqual([e['id'] for e in events], [f'E{n:03d}' for n in range(1, 13)])

    def test_plan_delta_and_user_decision_in_digest(self):
        self.run_cli('append', '--type', 'plan', '--title', 'Plan', '--body', 'Full plan')
        self.run_cli('append', '--type', 'plan', '--title', 'Delta', '--body', '+ Addition')
        self.run_cli('append', '--type', 'user-decision', '--actor', 'user', '--title', "User's choice", '--body', 'yes')
        self.run_cli('digest')
        digest = self.log.with_name('test.digest.md').read_text()
        self.assertIn('Full plan', digest)
        self.assertIn('+ Addition', digest)
        self.assertIn("User's choice", digest)

    def test_context_unknown_state_and_verification(self):
        context = self.run_cli('context')
        self.assertIn('state unknown', context)
        self.run_cli('append', '--type', 'verification', '--title', 'Checked XML',
                     '--command', 'xmllint --noout a.xml', '--result', 'Passed')
        context = self.run_cli('context')
        self.assertIn('xmllint --noout a.xml', context)
        self.assertIn('Result: Passed', context)
        self.assertIn('evidence: not set', context)

    def test_fenced_event_example_is_not_an_event(self):
        body = 'Example:\n```markdown\n### 2026-09-01T12:00:00+04:00 — Example\n- Type: `decision`\n- Author: `agent`\n\nText\n```'
        self.run_cli('append', '--type', 'research', '--title', 'Format example', '--body', body)
        self.append('Real decision')
        self.assertEqual(len(m.parse_events(self.log.read_text())), 2)
        self.assertIn(body, self.run_cli('show', '--event', 'E001'))

    def test_context_sections_and_multiple_replacements(self):
        self.append('Old question', '--context-section', 'question')
        self.append('Old next step', '--context-section', 'next-step')
        self.append('Answer received', '--context-section', 'result', '--evidence-status', 'confirmed',
                    '--supersedes', 'E001', '--supersedes', 'E002')
        self.append('Unverified hypothesis', '--evidence-status', 'hypothesis')
        context = self.run_cli('context')
        self.assertNotIn('Old question', context)
        self.assertNotIn('Old next step', context)
        self.assertIn('Answer received', context)
        self.assertIn('Unverified hypothesis', context)
        self.assertIn('E001 → E003', context)
        self.assertIn('E002 → E003', context)

    def test_body_file_and_file_symbols_survive(self):
        body = self.root / 'body.md'
        body.write_text('Description\n\n@@ -1 +1 @@\n-old\n+new')
        self.run_cli('append', '--type', 'code-change', '--title', 'Changed a method',
                     '--body-file', str(body), '--file', 'src/example.kt', '--symbol', 'example.run')
        self.assertIn(body.read_text(), self.run_cli('show', '--event', 'E001'))
        context = self.run_cli('context')
        self.assertIn('src/example.kt', context)
        self.assertIn('example.run', context)

    def test_excerpt_is_explicit_and_full_event_is_available(self):
        text = 'Detailed decision. ' * 100
        self.run_cli('append', '--type', 'decision', '--title', 'Long entry', '--body', text)
        self.assertIn('[excerpt; full text via show --event]', self.run_cli('context'))
        self.assertIn(text.rstrip(), self.run_cli('show', '--event', 'E001'))

    def test_secret_rejected(self):
        before = self.log.read_bytes()
        self.run_cli('append', '--type', 'research', '--title', 'Secret',
                     '--body', 'Authorization: Bearer example-test-value', ok=False)
        self.assertEqual(self.log.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
