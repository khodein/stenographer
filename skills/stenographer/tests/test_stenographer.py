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
        self.assertIn('already exists', self.run_cli('init', '--task', 'Another task'))
        self.assertEqual(self.log.read_bytes(), before)

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
