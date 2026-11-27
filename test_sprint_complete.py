"""Exercise the real jira binary against a local Jira HTTP fixture, never live Jira."""
import contextlib
import copy
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import sprint_complete as wizard


def issue(key='PROJ-1', number=1):
    return dict(id=str(number), key=key, fields={
        'summary': 'Improve retry handling',
        'description': {'type': 'doc', 'version': 1, 'content': [
            {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Keep this description.'}]}]},
        'status': {'id': '1', 'name': 'In Progress', 'statusCategory': {'key': 'indeterminate'}},
        'issuetype': {'id': '100', 'name': 'Story'},
        'priority': {'id': '3', 'name': 'Medium'},
        'components': [{'id': '7', 'name': 'Ingestion'}],
        'parent': {'id': '99', 'key': 'PROJ-99'},
        'assignee': {'accountId': 'alice', 'displayName': 'Alice Example'},
        'reporter': {'accountId': 'alice', 'displayName': 'Alice Example'},
        'labels': ['ingestion'], 'comment': {'comments': [], 'total': 0}, 'issuelinks': [],
        'updated': '2026-11-27T00:00:00.000+0000',
        'created': '2026-11-01T00:00:00.000+0000',
        'customfield_10001': 5, 'customfield_10003': 'PROJ-99',
        'customfield_10002': [{'id': 10, 'name': 'Sprint 10', 'state': 'active'}],
    })


class Store:
    def __init__(self):
        self.issues = {'PROJ-1': issue()}
        self.events = []
        self.fault = None
        self.no_points = False
        self.auth_status = None
        self.clock = 0

    def touch(self, key):
        self.clock += 1
        self.issues[key]['fields']['updated'] = f'2026-11-27T00:01:{self.clock:02}.000+0000'


def handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data=None, status=200):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if data is not None:
                self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            if store.auth_status:
                return self.respond({'errorMessages': ['Authentication failed']}, store.auth_status)
            if path.endswith('/myself'):
                return self.respond({'accountId': 'alice', 'displayName': 'Alice Example',
                                     'emailAddress': 'alice@example.test'})
            if path.endswith('/sprint'):
                state = query.get('state', ['active'])[0]
                sprint = {'id': 10 if state == 'active' else 11, 'name': f'Sprint {10 if state == "active" else 11}',
                          'state': state, 'originBoardId': 1}
                return self.respond({'values': [sprint], 'isLast': True, 'startAt': 0, 'maxResults': 50})
            if path.endswith('/search/jql'):
                jql = query['jql'][0]
                # Real Jira rejects a nonexistent issue as a numeric boundary.
                if re.search(r'\bid\s*>\s*0\b', jql):
                    return self.respond({'errorMessages': ['Issue does not exist or you do not have permission to see it.']}, 400)
                exclusions = re.search(r'key NOT IN \(([^)]+)\)', jql)
                excluded = set(json.loads('[' + exclusions[1] + ']')) if exclusions else set()
                labels = re.search(r'labels = "([^"]+)"', jql)
                candidates = [i for i in store.issues.values() if i['key'] not in excluded]
                key_match = re.search(r'\bkey = "([^"]+)"', jql)
                if key_match:
                    candidates = [i for i in candidates if i['key'] == key_match[1]]
                if 'assignee = currentUser()' in jql:
                    candidates = [i for i in candidates if wizard.assignee_id(i) == 'alice']
                if labels:
                    candidates = [i for i in candidates if labels[1] in i['fields']['labels']]
                elif 'statusCategory' in jql:
                    candidates = [i for i in candidates if wizard.category(i) == 'indeterminate'
                                  and 10 in wizard.sprint_ids(i, 'customfield_10002')]
                # Jira sorts id by project/key, not the internal numeric issue ID.
                candidates.sort(key=lambda i: (i['key'].split('-')[0], int(i['key'].split('-')[1])))
                # Deliberately cap below the requested size to exercise paging.
                return self.respond({'issues': candidates[:2], 'isLast': len(candidates) <= 2})
            if path.endswith('/user/assignable/search'):
                return self.respond([{'accountId': 'alice', 'displayName': 'Alice Example',
                                      'emailAddress': 'alice@example.test', 'active': True}])
            if path.endswith('/issueLinkType'):
                return self.respond({'issueLinkTypes': [{'id': '1', 'name': 'Cloners',
                                                         'inward': 'is cloned by', 'outward': 'clones'}]})
            match = re.search(r'/issue/([A-Z]+-\d+)(/transitions)?$', path)
            if match:
                if match[2]:
                    return self.respond({'transitions': [
                        {'id': '1', 'name': 'Done', 'isAvailable': True,
                         'to': {'id': '2', 'name': 'Done'}},
                        {'id': '2', 'name': 'In Progress', 'isAvailable': True,
                         'to': {'id': '1', 'name': 'In Progress'}}]})
                return self.respond(store.issues[match[1]])
            self.respond({'errorMessages': ['Unknown fixture GET ' + self.path]}, 404)

        def mutate(self):
            path = urlsplit(self.path).path
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            store.events.append((self.command, path, body))
            response, status, operation = None, 204, None
            if path == '/rest/api/3/issue' and self.command == 'POST':
                operation = 'clone'
                key = f'PROJ-{len(store.issues) + 100}'
                clone = issue(key, len(store.issues) + 100)
                clone['fields'].update(copy.deepcopy(body['fields']))
                clone['fields']['priority'] = {'id': '3', 'name': 'Medium'}
                clone['fields']['issuetype'] = {'id': '100', 'name': 'Story'}
                clone['fields']['components'] = [{'id': '7', 'name': 'Ingestion'}]
                clone['fields']['parent'] = None  # CLI clone needs explicit restoration.
                clone['fields']['assignee'] = None
                clone['fields']['customfield_10003'] = None
                clone['fields']['customfield_10001'] = None
                clone['fields']['customfield_10002'] = []
                clone['fields']['status'] = {'name': 'Backlog', 'statusCategory': {'key': 'new'}}
                store.issues[key] = clone
                response, status = {'id': clone['id'], 'key': key}, 201
            elif path.endswith('/issueLink'):
                source, target = body['inwardIssue']['key'], body['outwardIssue']['key']
                store.issues[source]['fields']['issuelinks'].append(
                    {'type': {'name': 'Cloners'}, 'outwardIssue': {'key': target}})
                store.touch(source)
                status = 201
            elif '/sprint/11/issue' in path:
                for key in body['issues']:
                    store.issues[key]['fields']['customfield_10002'] = [{'id': 11, 'state': 'future'}]
                    store.touch(key)
            elif '/epic/PROJ-99/issue' in path:
                for key in body['issues']:
                    store.issues[key]['fields']['customfield_10003'] = 'PROJ-99'
                    store.touch(key)
            else:
                match = re.search(r'/issue/(PROJ-\d+)(/(assignee|transitions|comment))?$', path)
                if not match:
                    return self.respond({'errorMessages': ['Unknown fixture write ' + path]}, 404)
                key, operation = match[1], match[3] or 'edit'
                fields = store.issues[key]['fields']
                if operation == 'assignee':
                    fields['assignee'] = {'accountId': 'alice', 'displayName': 'Alice Example'} if body.get('accountId') else None
                elif operation == 'transitions':
                    done = body['transition']['id'] == '1'
                    fields['status'] = {'name': 'Done' if done else 'In Progress',
                                        'statusCategory': {'key': 'done' if done else 'indeterminate'}}
                elif operation == 'comment':
                    fields['comment']['comments'].append({'body': body['body']})
                    fields['comment']['total'] += 1
                    response, status = {'id': '1'}, 201
                else:
                    for name, value in body.get('fields', {}).items():
                        if name == 'parent':
                            value = {'id': '99', 'key': value['key']}
                        fields[name] = value
                    for name, changes in body.get('update', {}).items():
                        if name == 'customfield_10001' and not store.no_points:
                            fields[name] = changes[0]['set']
                store.touch(key)
            if store.fault is not None and store.fault == operation:
                store.fault = None
                return self.respond({'errorMessages': ['Simulated lost response after commit']}, 500)
            self.respond(response, status)

        do_POST = mutate
        do_PUT = mutate
    return Handler


@unittest.skipUnless(shutil.which('jira'), 'Requires community jira-cli on PATH')
class RealCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.settings = json.loads((wizard.ROOT / 'settings.example.json').read_text())
        self.settings.update(site=f'http://127.0.0.1:{self.server.server_port}', login='alice@example.test')
        self.jira = wizard.Jira(self.settings, shutil.which('jira'), self.tmp.name)
        self.jira.env['JIRA_API_TOKEN'] = 'fixture-token'
        self.state = dict(closing={'id': 10, 'name': 'Sprint 10'}, next={'id': 11, 'name': 'Sprint 11'},
                          keys=['PROJ-1'], tickets={}, skipped=[])
        self.path = Path(self.tmp.name) / 'state.json'
        self.session = wizard.Session(self.jira, self.settings, self.path, self.state)
        self.row = dict(key='PROJ-1', original=self.jira.issue('PROJ-1'), estimate='5',
                        action='c', spent='2', remaining='3', steps=[], pending=None, complete=False,
                        marker='sprint-carry-10-proj-1', expected={'PROJ-1': issue()['fields']['updated']})
        self.state['tickets']['PROJ-1'] = self.row
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_carryover_preserves_work_and_splits_points_before_closing(self):
        self.session.apply(self.row)
        source = self.store.issues['PROJ-1']
        clone = self.store.issues[self.row['clone']]
        self.assertEqual(wizard.category(source), 'done')
        self.assertEqual(source['fields']['customfield_10001'], 2)
        self.assertEqual(wizard.sprint_ids(source, 'customfield_10002'), {10})
        self.assertEqual(wizard.category(clone), 'indeterminate')
        self.assertEqual(clone['fields']['customfield_10001'], 3)
        self.assertEqual(wizard.sprint_ids(clone, 'customfield_10002'), {11})
        self.assertEqual(wizard.assignee_id(clone), 'alice')
        self.assertEqual(clone['fields']['parent']['key'], 'PROJ-99')
        self.assertEqual(clone['fields']['description'], source['fields']['description'])
        writes = len(self.store.events)
        self.session.apply(self.row)
        self.assertEqual(len(self.store.events), writes, 'Completed steps must not repeat mutations')
        self.assertTrue(json.loads(self.path.read_text())['tickets']['PROJ-1']['complete'])

    def test_done_does_not_clone_and_accepts_decimal_points(self):
        self.row.update(action='d', spent='0.5', remaining=None)
        self.session.apply(self.row)
        self.assertEqual(len(self.store.issues), 1)
        self.assertEqual(self.store.issues['PROJ-1']['fields']['customfield_10001'], 0.5)
        self.assertEqual(wizard.category(self.store.issues['PROJ-1']), 'done')

    def test_lost_clone_response_resumes_without_duplicate(self):
        self.store.fault = 'clone'
        with self.assertRaises(wizard.WizardError):
            self.session.apply(self.row)
        self.assertEqual(self.row['pending'], 'clone')
        self.assertEqual(len(self.store.issues), 2)
        self.assertEqual(wizard.category(self.store.issues['PROJ-1']), 'indeterminate')
        self.session.apply(self.row)
        self.assertEqual(len(self.store.issues), 2)
        self.assertTrue(self.row['complete'])

    def test_lost_comment_response_does_not_duplicate_comment(self):
        self.row.update(action='d', remaining=None)
        self.store.fault = 'comment'
        with self.assertRaises(wizard.WizardError):
            self.session.apply(self.row)
        self.assertEqual(len(self.store.issues['PROJ-1']['fields']['comment']['comments']), 1)
        self.session.apply(self.row)
        self.assertEqual(len(self.store.issues['PROJ-1']['fields']['comment']['comments']), 1)
        self.assertTrue(self.row['complete'])

    def test_ignored_custom_field_update_does_not_close_original(self):
        self.store.no_points = True
        with self.assertRaises(wizard.WizardError):
            self.session.apply(self.row)
        self.assertEqual(wizard.category(self.store.issues['PROJ-1']), 'indeterminate')
        self.assertEqual(self.store.issues['PROJ-1']['fields']['customfield_10001'], 5)

    def test_concurrent_edit_requires_explicit_acceptance(self):
        self.row.update(action='d', remaining=None)
        self.store.touch('PROJ-1')
        with patch('builtins.input', return_value='n'), self.assertRaises(wizard.WizardError):
            self.session.apply(self.row)
        self.assertFalse(self.store.events)

    def test_pagination_fetches_all_issues_despite_short_server_pages(self):
        for number in range(2, 6):
            self.store.issues[f'PROJ-{number}'] = issue(f'PROJ-{number}', number)
        self.assertEqual(self.jira.search('statusCategory = "In Progress"'),
                         [f'PROJ-{n}' for n in range(1, 6)])

    def test_pagination_shared_board_has_no_numeric_sentinel_or_project_sort_gaps(self):
        self.store.issues = {
            'PROJ-1': issue('PROJ-1', 100),
            'PROJ-2': issue('PROJ-2', 300),
            'AUX-1': issue('AUX-1', 200),
            'AUX-2': issue('AUX-2', 50),
            'OTHER-1': issue('OTHER-1', 400),
        }
        keys = self.jira.search('statusCategory = "In Progress"')
        self.assertEqual(len(keys), 5)
        self.assertEqual(set(keys), set(self.store.issues))

    def test_cli_sprint_output_and_config_are_compatible(self):
        self.assertEqual(self.jira.sprints('active'), [{'id': 10, 'name': 'Sprint 10', 'state': 'active'}])
        self.assertEqual(self.jira.sprints('future')[0]['id'], 11)

    def test_authentication_rejection_explains_expiry_is_uncertain(self):
        self.store.auth_status = 401
        with self.assertRaisesRegex(wizard.WizardError, 'expired, revoked or invalid'):
            self.jira.check_access()
        self.assertFalse(self.store.events)

    def test_permission_rejection_is_not_reported_as_expired(self):
        self.store.auth_status = 403
        with self.assertRaisesRegex(wizard.WizardError, 'denied access \\(403\\)'):
            self.jira.check_access()

    def test_reassigned_original_cannot_be_modified(self):
        self.store.issues['PROJ-1']['fields']['assignee'] = {'accountId': 'bob', 'displayName': 'Bob'}
        self.store.touch('PROJ-1')
        with self.assertRaisesRegex(wizard.WizardError, 'no longer assigned'):
            self.session.apply(self.row)
        self.assertFalse(self.store.events)

    def test_default_scope_is_only_mine_and_environment_token_skips_prompt(self):
        other = issue('PROJ-2', 2)
        other['fields']['assignee'] = {'accountId': 'bob', 'displayName': 'Bob'}
        unassigned = issue('PROJ-3', 3)
        unassigned['fields']['assignee'] = None
        self.store.issues.update({'PROJ-2': other, 'PROJ-3': unassigned})
        settings = Path(self.tmp.name) / 'settings.json'
        configured = dict(self.settings, login='')
        wizard.save_json(settings, configured)
        args = ['sprint_complete.py', '--settings', str(settings), '--dry-run']
        answers = iter(['', '', 's'])
        output = io.StringIO()
        with patch.object(wizard.sys, 'argv', args), patch.object(wizard.sys.stdin, 'isatty', return_value=True), \
                patch.dict(wizard.os.environ, {'JIRA_API_TOKEN': 'fixture-token', 'JIRA_LOGIN': 'alice@example.test'}), \
                patch('builtins.input', side_effect=lambda *a: next(answers)), \
                patch.object(wizard.getpass, 'getpass', side_effect=AssertionError('Token prompt must be skipped')), \
                contextlib.redirect_stdout(output):
            wizard.main()
        self.assertIn('1 tickets;', output.getvalue())
        self.assertIn('Scope: only tickets assigned to you', output.getvalue())
        self.assertNotIn('PROJ-2', output.getvalue())
        self.assertNotIn('PROJ-3', output.getvalue())
        self.assertFalse(self.store.events)

    def test_dry_run_never_writes_jira_or_journal(self):
        settings = Path(self.tmp.name) / 'settings.json'
        wizard.save_json(settings, self.settings)
        args = ['sprint_complete.py', '--settings', str(settings), '--dry-run',
                '--state-dir', str(Path(self.tmp.name) / 'dry-state')]
        answers = iter(['', '', 'c', '2', '', ''])
        with patch.object(wizard.sys, 'argv', args), patch.object(wizard.sys.stdin, 'isatty', return_value=True), \
                patch.dict(wizard.os.environ, {'JIRA_API_TOKEN': 'fixture-token'}), \
                patch('builtins.input', side_effect=lambda *a: next(answers)):
            wizard.main()
        self.assertFalse(self.store.events)
        self.assertFalse(list((Path(self.tmp.name) / 'dry-state').glob('*.json')))

    def test_wizard_applies_then_resumes_without_repeating_writes(self):
        settings = Path(self.tmp.name) / 'settings.json'
        wizard.save_json(settings, self.settings)
        args = ['sprint_complete.py', '--settings', str(settings),
                '--state-dir', str(Path(self.tmp.name) / 'live-state')]
        answers = iter(['', '', 'c', '2', '', ''])
        with patch.object(wizard.sys, 'argv', args), patch.object(wizard.sys.stdin, 'isatty', return_value=True), \
                patch.dict(wizard.os.environ, {'JIRA_API_TOKEN': 'fixture-token'}), \
                patch('builtins.input', side_effect=lambda *a: next(answers)):
            wizard.main()
        self.assertEqual(wizard.category(self.store.issues['PROJ-1']), 'done')
        writes = len(self.store.events)
        answers = iter(['', '', ''])
        with patch.object(wizard.sys, 'argv', args), patch.object(wizard.sys.stdin, 'isatty', return_value=True), \
                patch.dict(wizard.os.environ, {'JIRA_API_TOKEN': 'fixture-token'}), \
                patch('builtins.input', side_effect=lambda *a: next(answers)):
            wizard.main()
        self.assertEqual(len(self.store.events), writes)

    def test_lost_done_transition_response_resumes_without_reclosing(self):
        self.row.update(action='d', remaining=None)
        self.store.fault = 'transitions'
        with self.assertRaises(wizard.WizardError):
            self.session.apply(self.row)
        self.assertEqual(wizard.category(self.store.issues['PROJ-1']), 'done')
        writes = len(self.store.events)
        self.session.apply(self.row)
        self.assertEqual(len(self.store.events), writes)
        self.assertTrue(self.row['complete'])


class InputTests(unittest.TestCase):
    def test_invalid_points(self):
        for invalid in ('NaN', 'Infinity', '-1', '1.1234', '', 'abc'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                wizard.points(invalid)
        self.assertEqual(wizard.points('0'), '0')
        self.assertEqual(wizard.points('0.125'), '0.125')


if __name__ == '__main__':
    unittest.main()
