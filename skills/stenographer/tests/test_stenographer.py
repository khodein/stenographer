import argparse
import importlib.util
import json
import os
import re
import shutil
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


class BaseCase(unittest.TestCase):
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


class StenographerTest(BaseCase):
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
        self.assertIn('E001 · ', digest)
        self.assertIn('· plan · Plan', digest)
        self.assertIn('· plan · Delta', digest)
        self.assertIn("· user-decision · User's choice", digest)
        self.assertNotIn('Full plan', digest)

    def test_digest_lists_active_events_with_ids(self):
        self.append('First decision')
        self.append('Second decision', '--supersedes', 'E001')
        self.run_cli('append', '--type', 'tool-call', '--title', 'Noisy call', '--body', 'x')
        self.run_cli('append', '--type', 'decision', '--title', 'L' * 200, '--body', 'x')
        self.run_cli('digest')
        digest = self.log.with_name('test.digest.md').read_text()
        self.assertNotIn('First decision', digest)
        self.assertIn('E002 · ', digest)
        self.assertRegex(digest, r'- E002 · \d{2}-\d{2} \d{2}:\d{2} · decision · ')
        self.assertIn('Second decision (supersedes E001)', digest)
        self.assertNotIn('Noisy call', digest)
        self.assertIn('L' * 119 + '…', digest)
        self.assertNotIn('L' * 120, digest)

    def build_digest(self, specs):
        """specs: (type, branch, supersedes) per event; returns the digest of an in-memory transcript."""
        content = m.header(self.repo, 'repo', 'big', 'Big task')
        for number, (event_type, branch, supersedes) in enumerate(specs, 1):
            args = argparse.Namespace(
                title=f'{event_type} {number}', type=event_type, actor='a', event_id=f'E{number:03d}',
                branch=branch, supersedes=supersedes, context_section=None, evidence_status=None,
                body='x', file=['f.kt'] if event_type == 'code-change' else None,
                symbol=['f.F'] if event_type == 'code-change' else None, command=None, result=None)
            content += '\n\n' + m.event_entry(args)
        return m.build_digest(content, 'repo', 'big')

    def test_digest_folds_older_events_but_keeps_always_types(self):
        specs = [('decision', 'main', [])] * 120
        specs[0] = ('user-request', 'main', [])
        specs[9] = ('plan', 'main', [])
        specs[19] = ('blocker', 'main', [])
        digest = self.build_digest(specs)
        for line in ('- E001 · ', '- E010 · ', '- E020 · '):
            self.assertIn(line, digest)
        self.assertIn('- … E002–E009 (earlier, 8 events): 8 decision', digest)
        self.assertIn('- … E011–E019 (earlier, 9 events): 9 decision', digest)
        self.assertIn('- … E021–E040 (earlier, 20 events): 20 decision', digest)
        self.assertNotIn('- E040 · ', digest)
        self.assertIn('- E041 · ', digest)
        self.assertIn('- E120 · ', digest)
        lines = [line for line in digest.splitlines() if line.startswith('- E')]
        self.assertEqual(len(lines), 83)
        self.assertEqual(digest.count('- … '), 3)

    def test_digest_has_no_summary_up_to_the_limit_and_singular_after_it(self):
        digest = self.build_digest([('decision', 'main', [])] * 80)
        self.assertNotIn('- … ', digest)
        digest = self.build_digest([('decision', 'main', [])] * 81)
        self.assertIn('- … E001–E001 (earlier, 1 event): 1 decision', digest)

    def test_digest_folds_per_branch_segment(self):
        specs = [('decision', 'a', [])] * 30 + [('code-change', 'b', [])] * 30 + [('decision', 'a', [])] * 60
        digest = self.build_digest(specs)
        self.assertIn('- … E001–E030 (earlier, 30 events): 30 decision', digest)
        self.assertIn('- … E031–E040 (earlier, 10 events): 10 code-change', digest)
        self.assertLess(digest.index('_branch: a_'), digest.index('E001–E030'))
        self.assertLess(digest.index('E001–E030'), digest.index('_branch: b_'))
        self.assertLess(digest.index('_branch: b_'), digest.index('E031–E040'))
        self.assertLess(digest.index('E031–E040'), digest.index('_branch: a_', digest.index('E031–E040')))

    def test_folded_summary_skips_superseded_events(self):
        specs = [('decision', 'main', [])] * 100
        specs[4] = ('decision', 'main', ['E003'])
        digest = self.build_digest(specs)
        self.assertIn('- … E001–E020 (earlier, 19 events): 19 decision', digest)

    def test_digest_size_is_bounded_for_long_transcripts(self):
        types = ['decision', 'code-change', 'verification', 'user-addition', 'external-action']
        specs = [(types[i % 5], f'branch-{i // 100}', []) for i in range(1500)]
        specs[0] = ('user-request', 'branch-0', [])
        digest = self.build_digest(specs)
        self.assertLess(len(digest), 16000)
        self.assertIn('- E1500 · ', digest)

    def test_digest_limits_recent_checks(self):
        for n in range(7):
            self.run_cli('append', '--type', 'verification', '--title', f'Check {n}',
                         '--command', 'c', '--result', 'ok')
        self.run_cli('digest')
        digest = self.log.with_name('test.digest.md').read_text()
        self.assertNotIn('Check 1\n', digest)
        self.assertIn('Check 2', digest)
        self.assertIn('Check 6', digest)

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
        # The header-like line inside the fence is stored escaped, everything else verbatim.
        self.assertIn(body.replace('### 2026', '\\### 2026'), self.run_cli('show', '--event', 'E001'))

    def test_unbalanced_fence_in_body_does_not_hide_later_events(self):
        # A diff hunk can show an opening fence without its closing line.
        body = 'Change:\n\n@@ -1,3 +1,3 @@\n \n-Old\n+New\n \n ```text\n <path>'
        self.run_cli('append', '--type', 'research', '--title', 'Unbalanced fence', '--body', body)
        self.append('After the fence')
        self.append('And another one')
        events = m.parse_events(self.log.read_text())
        self.assertEqual([e['id'] for e in events], ['E001', 'E002', 'E003'])
        self.assertIn('After the fence', self.run_cli('context'))
        self.assertIn('E003', self.log.with_name('test.digest.md').read_text())

    def test_fenced_copy_of_an_old_event_is_not_an_event(self):
        self.append('First decision')
        copy = 'Pasted:\n```markdown\n### 2026-09-01T12:00:00+04:00 — First decision\n- Type: `decision`\n- Author: `agent`\n- ID: `E001`\n\nText\n```'
        self.run_cli('append', '--type', 'research', '--title', 'Pasted event', '--body', copy)
        self.append('Next decision')
        self.assertEqual([e['id'] for e in m.parse_events(self.log.read_text())], ['E001', 'E002', 'E003'])

    def test_pasted_event_block_does_not_break_the_transcript(self):
        self.append('First decision')
        pasted = ('Pasted from another transcript:\n\n### 2026-01-01T00:00:00+04:00 — Pasted\n'
                  '- Type: `decision`\n- Author: `x`\n- ID: `E001`\n\nbody')
        self.run_cli('append', '--type', 'user-addition', '--actor', 'user', '--title', 'Pasted event', '--body', pasted)
        self.append('After the paste')
        events = m.parse_events(self.log.read_text())
        self.assertEqual([e['id'] for e in events], ['E001', 'E002', 'E003'])
        self.assertEqual([e['title'] for e in events], ['First decision', 'Pasted event', 'After the paste'])
        shown = self.run_cli('show', '--event', 'E002')
        self.assertIn('\\### 2026-01-01T00:00:00+04:00 — Pasted', shown)
        self.assertIn('- Type: `decision`', shown)
        digest = self.log.with_name('test.digest.md').read_text()
        self.assertEqual(digest.count('· Pasted'), 1)
        self.assertNotIn('2026-01-01', digest)

    def test_header_like_lines_are_escaped_in_every_text_field(self):
        header = '### 2026-01-01T00:00:00+04:00 — Fake\n- Type: `decision`\n- Author: `x`'
        self.run_cli('append', '--type', 'code-change', '--title', 'Fields', '--body', header,
                     '--file', 'a.kt\n' + header, '--symbol', 'a.A\n' + header,
                     '--command', 'echo\n' + header, '--result', 'ok\n' + header)
        self.append('Next')
        events = m.parse_events(self.log.read_text())
        self.assertEqual([e['id'] for e in events], ['E001', 'E002'])
        self.assertEqual(self.log.read_text().count('\\### 2026-01-01'), 5)

    def test_escaping_leaves_normal_text_and_the_real_header_alone(self):
        text = 'Heading:\n### Not an event\n#### 2026-01-01T00:00:00 — Deeper\n\\### 2026-01-01T00:00:00+04:00 — Already escaped'
        self.run_cli('append', '--type', 'research', '--title', 'Plain', '--body', text)
        self.assertIn(text, self.run_cli('show', '--event', 'E001'))
        self.assertEqual(len(m.parse_events(self.log.read_text())), 1)

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



class CommandsTest(BaseCase):
    """Every CLI command."""

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                       check=True, capture_output=True)

    def test_init_is_idempotent_and_creates_header(self):
        content = self.log.read_text()
        self.assertIn('Context check', content)
        self.assertIn('in progress', content)
        before = self.log.read_bytes()
        self.assertIn('already exists', self.run_cli('init', '--task', ' Context check '))
        self.assertEqual(self.log.read_bytes(), before)

    def test_init_rejects_taken_name_with_another_task(self):
        before = self.log.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), 'init', '--task', 'Another task'], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('taken by a transcript with another task', result.stderr)
        self.assertIn('Context check', result.stderr)
        self.assertEqual(self.log.read_bytes(), before)

    def test_init_rejects_names_mapping_to_the_same_file(self):
        self.run_cli('init', '--task', 'x', '--name', 'feature/cache')
        before = (self.root / 'logs' / 'repo' / 'feature__cache.md').read_bytes()
        self.run_cli('init', '--task', 'x', '--name', 'feature__cache', ok=False)
        self.assertEqual((self.root / 'logs' / 'repo' / 'feature__cache.md').read_bytes(), before)
        self.run_cli('init', '--task', 'x', '--name', 'feature/cache')

    def test_init_rejects_names_differing_only_by_case(self):
        self.run_cli('init', '--task', 'x', '--name', 'Shared')
        self.run_cli('init', '--task', 'x', '--name', 'shared', ok=False)
        self.run_cli('init', '--task', 'x', '--name', 'SHARED', ok=False)
        names = sorted(p.name for p in (self.root / 'logs' / 'repo').glob('*.md') if 'digest' not in p.name)
        self.assertEqual(names, ['Shared.md', 'test.md'])

    def test_commands_fail_without_transcript(self):
        self.log.unlink()
        self.run_cli('resume', ok=False)
        self.run_cli('digest', ok=False)
        self.run_cli('finish', ok=False)
        self.run_cli('append', '--type', 'decision', '--title', 'x', '--body', 'x', ok=False)
        self.assertIn('TRANSCRIPT_STATUS=missing', self.run_cli('show'))

    def test_show_all_and_filtered_by_type(self):
        self.append('A decision')
        self.run_cli('append', '--type', 'research', '--title', 'A finding', '--body', 'found it')
        everything = self.run_cli('show')
        self.assertIn('TRANSCRIPT_STATUS=existing', everything)
        self.assertIn('A decision', everything)
        self.assertIn('A finding', everything)
        only_research = self.run_cli('show', '--type', 'research')
        self.assertIn('A finding', only_research)
        self.assertNotIn('A decision', only_research)
        self.assertIn('no events of the selected types', self.run_cli('show', '--type', 'blocker'))

    def test_show_rejects_unknown_type_unknown_event_and_both_selectors(self):
        self.run_cli('show', '--type', 'nonsense', ok=False)
        self.run_cli('show', '--event', 'E099', ok=False)
        self.run_cli('show', '--type', 'decision', '--event', 'E001', ok=False)

    def test_every_event_type_can_be_appended(self):
        for event_type in m.EVENT_TYPES:
            extra = ('--file', 'a/B.kt', '--symbol', 'B.run') if event_type == 'code-change' else ()
            self.run_cli('append', '--type', event_type, '--actor', 'user', '--title', f'Event {event_type}',
                         '--body', f'Body {event_type}', *extra)
        events = m.parse_events(self.log.read_text())
        self.assertEqual([e['type'] for e in events], list(m.EVENT_TYPES))

    def test_append_validation_rules(self):
        before = self.log.read_bytes()
        body = self.root / 'b.md'
        body.write_text('x')
        for args in (
            ('--type', 'code-change', '--title', 'x', '--body', 'x'),
            ('--type', 'research', '--title', 'x', '--body', 'x', '--file', 'a.kt'),
            ('--type', 'research', '--title', 'x'),
            ('--type', 'research', '--title', 'two\nlines', '--body', 'x'),
            ('--type', 'research', '--title', 'x', '--body', 'x', '--body-file', str(body)),
            ('--type', 'research', '--title', 'x', '--body-file', str(self.root / 'none')),
        ):
            self.run_cli('append', *args, ok=False)
        self.assertEqual(self.log.read_bytes(), before)

    def test_unknown_event_type_is_rejected_by_argparse(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'append', '--type', 'nonsense', '--title', 'x',
                                 '--body', 'x'], cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(m.parse_events(self.log.read_text())), 0)

    def test_resume_and_finish_update_status(self):
        self.run_cli('finish')
        self.assertIn('Status: `done`', self.log.read_text())
        self.run_cli('finish', '--status', 'merged')
        self.assertIn('Status: `merged`', self.log.read_text())
        self.run_cli('resume')
        self.assertIn('Status: `in progress`', self.log.read_text())
        self.run_cli('finish', '--status', '  ', ok=False)

    def test_digest_is_created_and_updated_automatically(self):
        digest = self.log.with_name('test.digest.md')
        self.assertIn('Context check', digest.read_text())
        self.append('Auto decision')
        self.assertIn('Auto decision', digest.read_text())
        self.run_cli('finish', '--status', 'done')
        self.assertIn('Status: `done`', digest.read_text())
        self.run_cli('resume')
        self.assertIn('Status: `in progress`', digest.read_text())

    def test_resume_and_init_restore_missing_digest(self):
        digest = self.log.with_name('test.digest.md')
        digest.unlink()
        self.run_cli('init', '--task', 'Context check')
        self.assertTrue(digest.exists())
        digest.write_text('stale')
        self.run_cli('resume')
        self.assertIn('Context check', digest.read_text())

    def test_digest_failure_does_not_fail_append(self):
        digest = self.log.with_name('test.digest.md')
        digest.unlink()
        digest.mkdir()
        result = subprocess.run([sys.executable, str(SCRIPT), 'append', '--type', 'decision',
                                 '--title', 'Kept', '--body', 'x'], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('digest not updated', result.stderr)
        self.assertIn('Kept', self.log.read_text())

    def test_digest_is_regenerated(self):
        self.append('Digest decision')
        self.run_cli('digest')
        digest = self.log.with_name('test.digest.md')
        self.assertIn('Digest decision', digest.read_text())
        self.append('Second digest decision')
        self.run_cli('digest')
        self.assertIn('Second digest decision', digest.read_text())

    def test_index_lists_branches_and_skips_digests(self):
        self.run_cli('digest')
        self.run_cli('finish')
        self.run_cli('index')
        index = (self.root / 'logs' / 'repo' / 'INDEX.md').read_text()
        self.assertIn('| `test` | done |', index)
        self.assertIn('Context check', index)
        self.assertNotIn('digest', index)

    def test_index_fails_without_repository_directory(self):
        shutil.rmtree(self.log.parent)
        self.run_cli('index', ok=False)

    def test_detached_head_is_rejected(self):
        self.git('commit', '--allow-empty', '-m', 'c', '-q')
        self.git('checkout', '--detach', '-q')
        self.run_cli('context', ok=False)

    def test_header_uses_key_instead_of_branch(self):
        content = self.log.read_text()
        self.assertIn('- Key: `test`', content)
        self.assertNotIn('- Branch:', content)
        self.assertIn('- Key: `test`', self.log.with_name('test.digest.md').read_text())

    def test_explicit_name_creates_separate_transcript(self):
        self.run_cli('init', '--task', 'Shared task', '--name', 'davai-davai')
        named = self.root / 'logs' / 'repo' / 'davai-davai.md'
        self.assertIn('- Key: `davai-davai`', named.read_text())
        self.run_cli('append', '--type', 'decision', '--title', 'Named decision', '--body', 'x', '--name', 'davai-davai')
        self.assertIn('Named decision', named.read_text())
        self.assertNotIn('Named decision', self.log.read_text())
        self.assertIn('Named decision', named.with_name('davai-davai.digest.md').read_text())
        self.assertIn('Named decision', self.run_cli('context', '--name', 'davai-davai'))
        self.assertIn('Named decision', self.run_cli('show', '--name', 'davai-davai'))
        self.run_cli('finish', '--name', 'davai-davai')
        self.assertIn('Status: `done`', named.read_text())
        self.assertIn('Status: `in progress`', self.log.read_text())

    def test_named_transcript_works_across_branches_and_detached_head(self):
        self.run_cli('init', '--task', 'Shared task', '--name', 'shared')
        self.git('commit', '--allow-empty', '-m', 'c', '-q')
        self.git('checkout', '--detach', '-q')
        self.run_cli('append', '--type', 'decision', '--title', 'Detached ok', '--body', 'x', '--name', 'shared')
        self.assertIn('Detached ok', (self.root / 'logs' / 'repo' / 'shared.md').read_text())

    def test_missing_named_transcript_explains_how_to_create_it(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'resume', '--name', 'nope'], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Transcript 'nope' does not exist", result.stderr)
        self.assertIn('init', result.stderr)
        self.assertIn('--name', result.stderr)

    def test_unsafe_names_are_rejected(self):
        for name in ('', '  ', '.hidden', 'a..b', '..', 'x.digest', 'INDEX', 'index', 'a\\b'):
            self.run_cli('init', '--task', 'x', '--name', name, ok=False)
        self.assertEqual(sorted(p.name for p in (self.root / 'logs' / 'repo').glob('*.md')), ['test.digest.md', 'test.md'])

    def test_name_with_slash_becomes_double_underscore(self):
        self.run_cli('init', '--task', 'x', '--name', 'feature/cache')
        self.assertTrue((self.root / 'logs' / 'repo' / 'feature__cache.md').exists())

    def test_list_shows_transcripts_newest_first_without_writing(self):
        self.run_cli('init', '--task', 'Shared | task', '--name', 'feature/shared')
        self.run_cli('finish', '--name', 'feature/shared')
        files = sorted(p.name for p in (self.root / 'logs' / 'repo').iterdir())
        output = self.run_cli('list')
        self.assertEqual(sorted(p.name for p in (self.root / 'logs' / 'repo').iterdir()), files)
        self.assertIn('| Key | Status | Updated | Branches | Task |', output)
        self.assertIn('`feature/shared` | done', output)
        self.assertIn('Shared \\| task', output)
        self.assertIn('`test` | in progress', output)
        self.assertLess(output.index('feature/shared'), output.index('`test`'))
        self.assertNotIn('digest', output.split('Task |', 1)[1])

    def test_event_records_branch_and_detached(self):
        self.append('On test')
        self.assertIn('- Branch: `test`', self.log.read_text())
        self.git('commit', '--allow-empty', '-m', 'c', '-q')
        self.git('checkout', '--detach', '-q')
        self.append('Detached', '--name', 'test')
        events = m.parse_events(self.log.read_text())
        self.assertEqual([e['branch'] for e in events], ['test', 'detached'])
        self.assertIn('Branch: `detached`', self.run_cli('show', '--event', 'E002', '--name', 'test'))

    def test_branch_with_backtick_does_not_break_parsing(self):
        self.git('commit', '--allow-empty', '-m', 'c', '-q')
        self.git('checkout', '-b', 'feat`x', '-q')
        self.append('Odd branch', '--name', 'test')
        self.assertEqual(m.parse_events(self.log.read_text())[0]['branch'], "feat'x")

    def test_digest_marks_branch_changes_and_list_shows_branches(self):
        self.append('First')
        self.append('Second')
        self.git('commit', '--allow-empty', '-m', 'c', '-q')
        self.git('checkout', '-b', 'feature/next', '-q')
        self.append('Third', '--name', 'test')
        digest = self.log.with_name('test.digest.md').read_text()
        self.assertEqual(digest.count('_branch: test_'), 1)
        self.assertEqual(digest.count('_branch: feature/next_'), 1)
        self.assertLess(digest.index('_branch: test_'), digest.index('First'))
        self.assertLess(digest.index('Second'), digest.index('_branch: feature/next_'))
        self.assertLess(digest.index('_branch: feature/next_'), digest.index('Third'))
        self.assertIn('| test, feature/next |', self.run_cli('list'))

    def test_list_without_transcripts_and_in_detached_head(self):
        self.log.unlink()
        self.log.with_name('test.digest.md').unlink()
        self.assertIn('No transcripts', self.run_cli('list'))
        self.run_cli('init', '--task', 'x', '--name', 'shared')
        self.git('commit', '--allow-empty', '-m', 'c', '-q')
        self.git('checkout', '--detach', '-q')
        self.assertIn('`shared`', self.run_cli('list'))

    def test_missing_transcript_hint_mentions_list(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'resume', '--name', 'nope'], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertIn('`list`', result.stderr)

    def test_default_root_is_transcripts_in_the_checkout(self):
        os.environ.pop('STENOGRAPHER_ROOT', None)
        self.assertEqual(m.transcripts_root(), SCRIPT.resolve().parents[3] / 'transcripts')
        self.assertTrue((SCRIPT.resolve().parents[3] / '.git').exists())

    def test_env_overrides_root(self):
        os.environ['STENOGRAPHER_ROOT'] = '~/somewhere'
        self.addCleanup(os.environ.pop, 'STENOGRAPHER_ROOT')
        self.assertEqual(m.transcripts_root(), Path('~/somewhere').expanduser())

    def test_copied_skill_stores_transcripts_inside_the_skill_directory(self):
        os.environ.pop('STENOGRAPHER_ROOT', None)
        copy = self.root / 'share' / 'skills' / 'stenographer' / 'scripts' / 'stenographer.py'
        copy.parent.mkdir(parents=True)
        shutil.copy(SCRIPT, copy)
        self.assertEqual(m.transcripts_root(copy), copy.resolve().parents[1] / 'transcripts')

    def test_symlinked_skill_resolves_to_the_checkout(self):
        os.environ.pop('STENOGRAPHER_ROOT', None)
        link = self.root / 'linked'
        link.symlink_to(SCRIPT.resolve().parents[1])
        self.assertEqual(m.transcripts_root(link / 'scripts' / 'stenographer.py'),
                         SCRIPT.resolve().parents[3] / 'transcripts')

    def test_list_prints_the_transcript_directory(self):
        self.assertIn(str(self.root / 'logs' / 'repo'), self.run_cli('list'))

    def test_index_lists_keys(self):
        self.run_cli('init', '--task', 'x', '--name', 'shared')
        self.run_cli('index')
        index = (self.root / 'logs' / 'repo' / 'INDEX.md').read_text()
        self.assertIn('| Key |', index)
        self.assertIn('`shared`', index)
        self.assertIn('`test`', index)

    def test_detached_head_error_points_to_name_and_list(self):
        self.git('commit', '--allow-empty', '-m', 'c', '-q')
        self.git('checkout', '--detach', '-q')
        result = subprocess.run([sys.executable, str(SCRIPT), 'context'], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('--name <key>', result.stderr)
        self.assertIn('`list`', result.stderr)

    def test_branch_slash_becomes_double_underscore(self):
        self.git('checkout', '-b', 'feature/x', '-q')
        self.run_cli('init', '--task', 'Slash branch')
        self.assertTrue((self.root / 'logs' / 'repo' / 'feature__x.md').exists())

    def test_unknown_command_is_rejected(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'bogus'], cwd=self.repo, env=self.env,
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)


class WithUsageTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.env['HOME'] = str(self.root / 'home')
        encoded = re.sub(r'[^A-Za-z0-9]', '-', os.path.realpath(self.repo))
        self.sessions = self.root / 'home' / '.claude' / 'projects' / encoded
        self.sessions.mkdir(parents=True)

    def write_session(self, records, name='s.jsonl'):
        path = self.sessions / name
        path.write_text('\n'.join(json.dumps(r) for r in records))
        return path

    @staticmethod
    def user(text='hi', **extra):
        return {'type': 'user', 'message': {'content': text}, **extra}

    @staticmethod
    def assistant(msg_id, inp, out, cache_read=0, cache_write=0, block='text', **extra):
        usage = {'input_tokens': inp, 'output_tokens': out,
                 'cache_read_input_tokens': cache_read, 'cache_creation_input_tokens': cache_write}
        return {'type': 'assistant', 'uuid': f'u-{msg_id}-{block}',
                'message': {'id': msg_id, 'usage': usage, 'content': [{'type': block}]}, **extra}

    def append_response(self, ok=True):
        return self.run_cli('append', '--type', 'agent-response', '--actor', 'Claude', '--title', 'Answer',
                            '--body', 'Final answer', '--with-usage', ok=ok)

    def test_sums_only_current_turn_and_deduplicates_content_blocks(self):
        self.write_session([
            self.user('old question'),
            self.assistant('m0', 1000, 1000, 1000, 1000),
            self.user('current question'),
            self.assistant('m1', 2, 10, 100, 20, block='thinking'),
            self.assistant('m1', 2, 10, 100, 20, block='tool_use'),
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}},
            self.assistant('m2', 3, 30, 200, 0),
        ])
        self.append_response()
        shown = self.run_cli('show', '--event', 'E001')
        self.assertIn('Final answer', shown)
        self.assertIn('2 API calls', shown)
        self.assertIn('input 5, cache read 300, cache write 20, output 40', shown)

    def test_ignores_sidechain_and_meta_user_records(self):
        self.write_session([
            self.user('question'),
            self.assistant('m1', 1, 5),
            self.user('<system-reminder>', isMeta=True),
            self.assistant('side', 900, 900, isSidechain=True),
            self.assistant('m2', 2, 7),
        ])
        self.append_response()
        self.assertIn('input 3, cache read 0, cache write 0, output 12', self.run_cli('show', '--event', 'E001'))

    def test_uses_newest_session_file(self):
        old = self.write_session([self.user(), self.assistant('a', 111, 111)], 'old.jsonl')
        os.utime(old, (1, 1))
        self.write_session([self.user(), self.assistant('b', 7, 9)], 'new.jsonl')
        self.append_response()
        self.assertIn('input 7, cache read 0, cache write 0, output 9', self.run_cli('show', '--event', 'E001'))

    def test_missing_usage_data_is_an_error_and_does_not_write(self):
        before = self.log.read_bytes()
        self.append_response(ok=False)
        self.write_session([self.user('only a question')])
        self.append_response(ok=False)
        self.assertEqual(self.log.read_bytes(), before)

    def test_tolerates_corrupt_lines_and_missing_usage_fields(self):
        (self.sessions / 's.jsonl').write_text('not json\n' + json.dumps(self.user()) + '\n' + json.dumps(
            {'type': 'assistant', 'uuid': 'x', 'message': {'id': 'm', 'usage': {'output_tokens': 4}}}))
        self.append_response()
        self.assertIn('input 0, cache read 0, cache write 0, output 4', self.run_cli('show', '--event', 'E001'))

    def test_without_flag_body_has_no_token_line(self):
        self.write_session([self.user(), self.assistant('m', 1, 1)])
        self.run_cli('append', '--type', 'agent-response', '--title', 'Answer', '--body', 'Final answer')
        self.assertNotIn('Turn tokens', self.run_cli('show', '--event', 'E001'))

    def test_usage_works_with_body_file_and_without_body(self):
        self.write_session([self.user(), self.assistant('m', 1, 2)])
        body = self.root / 'b.md'
        body.write_text('From file `code` $x')
        self.run_cli('append', '--type', 'agent-comment', '--title', 'Comment',
                     '--body-file', str(body), '--with-usage')
        shown = self.run_cli('show', '--event', 'E001')
        self.assertIn('From file `code` $x', shown)
        self.assertIn('Turn tokens', shown)
        self.run_cli('append', '--type', 'verification', '--title', 'Check', '--command', 'true', '--with-usage')
        self.assertIn('Turn tokens', self.run_cli('show', '--event', 'E002'))


if __name__ == '__main__':
    unittest.main()
