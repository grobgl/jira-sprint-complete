#!/usr/bin/env python3
"""Interactive sprint accounting; all Jira traffic goes through jira-cli."""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation

ROOT = Path(__file__).resolve().parent


class WizardError(Exception):
    pass


def ask(message, default=None):
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{message}{suffix}: ").strip()
    return value or (str(default) if default is not None else "")


def yes(message, default=False):
    while True:
        answer = ask(message + (" [Y/n]" if default else " [y/N]")).lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if not answer:
            return default


def points(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Enter a non-negative number (up to three decimal places).") from None
    if not number.is_finite() or number < 0 or number.as_tuple().exponent < -3:
        raise ValueError("Enter a non-negative number (up to three decimal places).")
    return format(number.normalize(), "f")


def ask_points(message, default=None):
    while True:
        try:
            return points(ask(message, default))
        except ValueError as error:
            print(error)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def choose_sprints(active, future, closing_id=None, next_id=None):
    groups = []
    for items, title, selected in ((active, 'active closing', closing_id),
                                   (future, 'future destination', next_id)):
        if not items:
            raise WizardError(f'No {title} sprint available. Create/select one in Jira first.')
        matches = items if selected is None else [item for item in items if item['id'] == selected]
        if not matches:
            raise WizardError(f'Sprint {selected} is not an available {title} sprint.')
        groups.append(matches)
    pairs = [(closing, upcoming) for closing in groups[0] for upcoming in groups[1]]
    print('\nSprint completion (closing → next)')
    for index, (closing, upcoming) in enumerate(pairs, 1):
        print(f"  {index}. {closing['name']} (#{closing['id']}) → "
              f"{upcoming['name']} (#{upcoming['id']})")
    while True:
        answer = ask("Select", 1)
        if answer.isdigit() and 1 <= int(answer) <= len(pairs):
            return pairs[int(answer) - 1]
        print("Enter a number from the list.")


def sprint_ids(issue, field, live_only=False):
    values = issue['fields'].get(field) or []
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise WizardError("Expected Jira Cloud sprint objects; this wizard supports Jira Cloud.")
    return {int(v['id']) for v in values
            if not live_only or v.get('state', '').lower() != 'closed'}


def assignee_id(issue):
    return (issue['fields'].get('assignee') or {}).get('accountId')


def category(issue):
    return issue['fields']['status'].get('statusCategory', {}).get('key')


def linked(issue, other):
    return any((link.get('inwardIssue') or link.get('outwardIssue') or {}).get('key') == other
               and link.get('type', {}).get('name', '').lower() == 'cloners'
               for link in issue['fields'].get('issuelinks', []))


class Jira:
    def __init__(self, settings, executable, directory):
        self.settings = settings
        self.executable = executable
        self.config = Path(directory) / 'jira.json'
        custom = [{"key": settings['points_field'], "name": settings['points_name'],
                   # Viper decodes Go struct names, rather than the API JSON tags.
                   "schema": {"datatype": "number"}}]
        config = {"installation": "Cloud", "server": settings['site'],
                  "login": settings['login'], "auth_type": "basic",
                  "project": {"key": settings['project'], "type": "classic"},
                  "board": {"id": settings['board']['id'], "name": settings['board']['name']},
                  "issue": {"fields": {"custom": custom}}}
        save_json(self.config, config)
        # Prevent unrelated Jira environment/config defaults from redirecting this run.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('JIRA_')}
        if os.environ.get('JIRA_API_TOKEN'):
            self.env['JIRA_API_TOKEN'] = os.environ['JIRA_API_TOKEN']
        self.env.update(NO_COLOR='1', TERM='dumb', CLICOLOR='0')

    def run(self, *args):
        command = [self.executable, '--config', str(self.config), *map(str, args)]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, env=self.env, timeout=120)
        except subprocess.TimeoutExpired:
            raise WizardError("Jira command timed out. Its result may be uncertain; resume to reconcile.") from None
        if (result.returncode == 1 and args[:2] == ('issue', 'list')
                and 'No result found for given query' in result.stdout + result.stderr):
            return '[]'
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip()
            if re.search(r'\b401\b|\bunauthorized\b|basic authentication.*failed', message, re.IGNORECASE):
                raise WizardError(
                    'Jira rejected your credentials (401). The API token may be expired, revoked or invalid, '
                    'or the email may not match. Jira does not identify which cause applies. '
                    'Create an API token without scopes at '
                    'https://id.atlassian.com/manage-profile/security/api-tokens '
                    'and update JIRA_API_TOKEN (or enter the replacement at the prompt).')
            if re.search(r'\b403\b|\bforbidden\b', message, re.IGNORECASE):
                raise WizardError('Jira denied access (403). Check your account permissions and organisation '
                                  'token policies. This response does not establish that the token is expired.')
            raise WizardError(f"jira {shlex.join(list(map(str, args[:3])))} failed:\n{message}")
        return result.stdout

    def issue(self, key):
        return json.loads(self.run('issue', 'view', key, '--raw'))

    def check_access(self):
        # The CLI's `me` is local-only and sprint listing hides HTTP errors.
        # Issue search preserves HTTP failures and currentUser() requires login.
        self.run('issue', 'list', '--raw', '--jql',
                 'project IS NOT EMPTY AND assignee = currentUser()', '--paginate', '1')

    def require_mine(self, key):
        query = f'project IS NOT EMPTY AND key = {json.dumps(key)} AND assignee = currentUser()'
        matches = json.loads(self.run('issue', 'list', '--raw', '--jql', query, '--paginate', '1'))
        if not isinstance(matches, list) or not any(item.get('key') == key for item in matches):
            raise WizardError(f'{key} is no longer assigned to your authenticated Jira account. '
                              'Stopped before further changes to this ticket.')

    def search(self, query):
        """Page by excluding returned keys; Cloud CLI offsets are ignored.

        Numeric ID sentinels are invalid issue references, and Jira's id sort
        groups keys by project rather than ordering internal numeric IDs.
        Exclusions avoid relying on either behaviour on a shared board.
        """
        result, seen = [], set()
        while True:
            jql = f'project IS NOT EMPTY AND ({query})'
            if result:
                jql += ' AND key NOT IN (' + ','.join(json.dumps(key) for key in result) + ')'
            output = self.run('issue', 'list', '--raw', '--jql', jql,
                              '--order-by', 'id', '--reverse', '--paginate', '100')
            if 'No result found for given query' in output:
                return result
            page = json.loads(output)
            if not isinstance(page, list):
                raise WizardError('Unexpected jira issue list JSON; expected an array.')
            if not page:
                return result
            keys = [item['key'] for item in page]
            if seen.intersection(keys):
                raise WizardError('Jira pagination repeated issues; stopping before any writes.')
            seen.update(keys)
            result.extend(keys)
            # A short page is not necessarily the last page: server limits can differ.

    def sprints(self, state):
        output = self.run('sprint', 'list', '--state', state, '--plain', '--no-headers',
                          '--columns', 'id,state,name')
        if 'No result found for given query' in output:
            return []
        items = []
        for line in output.splitlines():
            match = re.fullmatch(r'\s*(\d+)\s+(active|future|closed)\s+(.+?)\s*', line)
            if match:
                items.append(dict(id=int(match[1]), state=match[2], name=match[3]))
            elif line.strip():
                raise WizardError(f'Unexpected sprint-list output: {line!r}')
        return sorted(items, key=lambda item: item['id'])

    def set_points(self, key, value):
        handle = self.settings['points_name'].strip().lower().replace(' ', '-')
        self.run('issue', 'edit', key, '--custom', f'{handle}={value}', '--no-input')


class Session:
    def __init__(self, jira, settings, path, state):
        self.jira, self.s, self.path, self.state = jira, settings, path, state

    def save(self):
        save_json(self.path, self.state)

    def capture(self, row):
        row['expected'] = {key: self.jira.issue(key)['fields']['updated']
                           for key in [row['key'], row.get('clone')] if key}

    def unchanged(self, row):
        self.jira.require_mine(row['key'])
        if row.get('clone') and 'assignee' in row['steps']:
            self.jira.require_mine(row['clone'])
        for key, updated in row['expected'].items():
            if self.jira.issue(key)['fields']['updated'] != updated:
                print(f'\n{key} changed since the last checkpoint.')
                print(self.jira.run('issue', 'view', key, '--plain'))
                print(f"Saved decision: {row['spent']} spent points; "
                      f"{row['remaining']} remaining points; action {row['action']}.")
                if not yes('After reviewing these changes, continue with this saved decision?'):
                    raise WizardError('Concurrent changes were not accepted; stopped before further writes.')
                row['expected'][key] = self.jira.issue(key)['fields']['updated']
                self.save()

    def step(self, row, name, action, verified):
        if name in row['steps']:
            return
        if row.get('pending'):
            if row['pending'] != name:
                raise WizardError(f"Unresolved step: {row['pending']}")
            if verified():
                row['steps'].append(name)
                row['pending'] = None
                self.capture(row)
                self.save()
                return
            print(f'\nPrevious attempt at {name!r} has an uncertain result.')
            if not yes('After inspecting Jira, retry this operation?'):
                raise WizardError('Stopped with the pending operation preserved.')
            # Explicitly inspected uncertain result; checkpoint current versions.
            self.capture(row)
        self.unchanged(row)
        if not verified():
            row['pending'] = name
            self.save()  # Persist intent before sending a mutation.
            action()
            if not verified():
                raise WizardError(f'{name}: Jira did not show the expected result. '
                                  'The original will remain open unless its close step already ran.')
        row['steps'].append(name)
        row['pending'] = None
        self.capture(row)
        self.save()

    def clone(self, row):
        if row.get('clone'):
            return
        marker = row['marker']
        matches = self.jira.search(f'labels = "{marker}"')
        if len(matches) > 1:
            raise WizardError(f'Multiple carryovers found for {row["key"]}: {matches}')
        if matches and row.get('pending') != 'clone':
            raise WizardError('A carryover already exists outside this journal. Review it before proceeding: '
                              + ', '.join(matches))
        if not matches:
            if row.get('pending') == 'clone':
                print('The previous clone request may have succeeded but is not searchable yet.')
                if not yes('After checking Jira for a carryover, retry cloning?'):
                    raise WizardError('Resume later to search again; no duplicate was created.')
                self.capture(row)
            self.unchanged(row)
            row['pending'] = 'clone'
            self.save()
            labels = list(dict.fromkeys(row['original']['fields'].get('labels', []) + [marker]))
            arguments = ['issue', 'clone', row['key'], '--project', row['key'].split('-')[0]]
            for label in labels:
                arguments += ['--label', label]
            output = self.jira.run(*arguments)
            # Clone has no JSON output. Its success line contains the new browse URL.
            keys = re.findall(r'/browse/([A-Z][A-Z0-9_]*-\d+)', output)
            if len(set(keys)) != 1 or keys[0] == row['key']:
                raise WizardError('Could not identify the new clone; resume to find its marker label.')
            matches = [keys[0]]
        row['clone'] = matches[0]
        candidate = self.jira.issue(row['clone'])
        if marker not in candidate['fields'].get('labels', []):
            raise WizardError('Clone is missing its recovery marker.')
        # Save the identity before any subsequent request can fail.
        row['pending'] = None
        row['steps'].append('clone')
        self.capture(row)
        self.save()

    def verify_metadata(self, row):
        original, clone = row['original']['fields'], self.jira.issue(row['clone'])['fields']
        for field in ('summary', 'description'):
            if clone.get(field) != original.get(field):
                raise WizardError(f"Clone {row['clone']} has different {field}. "
                                  'Correct it in Jira before proceeding; original remains open.')
        for field in ('priority', 'parent'):
            identifier = 'key' if field == 'parent' else 'id'
            if (clone.get(field) or {}).get(identifier) != (original.get(field) or {}).get(identifier):
                raise WizardError(f'Clone has a different {field}; original remains open.')
        if {c['id'] for c in clone.get('components', [])} != {c['id'] for c in original.get('components', [])}:
            raise WizardError('Clone has different components; original remains open.')
        if not set(original.get('labels', [])).issubset(clone.get('labels', [])):
            raise WizardError('Clone did not retain the original labels.')
        if (clone.get('issuetype') or {}).get('id') != (original.get('issuetype') or {}).get('id'):
            raise WizardError('Clone has a different issue type.')

    def comment(self, row, key, body):
        # Alphanumeric text survives jira-cli's Markdown-to-wiki escaping.
        marker = (f"SPRINTCOMPLETIONRECORD{self.state['closing']['id']}X"
                  f"{row['key'].replace('-', '')}X{key.replace('-', '')}")
        text = body + '\n' + marker
        def found():
            # Raw issue embeds only a page of comments; never automatically repeat
            # an uncertain write when the marker is absent from that page.
            comments = self.jira.issue(key)['fields'].get('comment', {}).get('comments', [])
            return any(marker in json.dumps(c.get('body'), ensure_ascii=False) for c in comments)
        self.step(row, f'comment {key}',
                  lambda: self.jira.run('issue', 'comment', 'add', key, text, '--no-input'), found)

    def apply(self, row):
        key, pf, sf = row['key'], self.s['points_field'], self.s['sprint_field']
        if row['action'] == 'c':
            self.clone(row)
            clone = row['clone']
            original = row['original']
            parent = (original['fields'].get('parent') or {}).get('key')
            if parent:
                self.step(row, 'parent',
                          lambda: self.jira.run('issue', 'edit', clone, '--parent', parent, '--no-input'),
                          lambda: (self.jira.issue(clone)['fields'].get('parent') or {}).get('key') == parent)
            self.verify_metadata(row)
            assignee = original['fields'].get('assignee') or {}
            name = assignee.get('emailAddress') or assignee.get('displayName') or 'x'
            self.step(row, 'assignee',
                      lambda: self.jira.run('issue', 'assign', clone, name, '--project', key.split('-')[0]),
                      lambda: assignee_id(self.jira.issue(clone)) == assignee_id(original))
            epic = original['fields'].get(self.s['epic_field'])
            if epic:
                self.step(row, 'epic', lambda: self.jira.run('epic', 'add', epic, clone),
                          lambda: self.jira.issue(clone)['fields'].get(self.s['epic_field']) == epic)
            self.step(row, 'remaining points', lambda: self.jira.set_points(clone, row['remaining']),
                      lambda: equal_points(self.jira.issue(clone), pf, row['remaining']))
            target = int(self.state['next']['id'])
            self.step(row, 'next sprint', lambda: self.jira.run('sprint', 'add', target, clone),
                      lambda: sprint_ids(self.jira.issue(clone), sf) == {target})
            self.step(row, 'clone in progress',
                      lambda: self.jira.run('issue', 'move', clone, self.s.get('progress_transition', self.s['progress_status'])),
                      lambda: self.jira.issue(clone)['fields']['status']['name'] == self.s['progress_status']
                      and category(self.jira.issue(clone)) == 'indeterminate')
            self.step(row, 'clone link', lambda: self.jira.run('issue', 'link', key, clone, 'Cloners'),
                      lambda: linked(self.jira.issue(key), clone))
            self.verify_metadata(row)
            # Revalidate all critical carryover state before touching the original.
            current = self.jira.issue(clone)
            if (not equal_points(current, pf, row['remaining'])
                    or sprint_ids(current, sf) != {target}
                    or category(current) != 'indeterminate'
                    or assignee_id(current) != assignee_id(original)):
                raise WizardError('Carryover verification failed; original remains open.')
        closing = self.state['closing']
        body = (f"Sprint completion: {closing['name']} (#{closing['id']}).\n"
                f"Initial estimate: {row['estimate'] if row['estimate'] is not None else 'unestimated'}; "
                f"confirmed points spent: {row['spent']}.")
        if row['action'] == 'c':
            body += (f"\nRemaining work: {row['clone']}, {row['remaining']} points, "
                     f"{self.state['next']['name']} (#{self.state['next']['id']}).")
            self.comment(row, row['clone'], f"Carried over from {key}.\n" + body)
        # Record the initial estimate before overwriting it.
        self.comment(row, key, body)
        self.step(row, 'spent points', lambda: self.jira.set_points(key, row['spent']),
                  lambda: equal_points(self.jira.issue(key), pf, row['spent']))
        self.step(row, 'close original',
                  lambda: self.jira.run('issue', 'move', key, self.s.get('done_transition', self.s['done_status'])),
                  lambda: self.jira.issue(key)['fields']['status']['name'] == self.s['done_status']
                  and category(self.jira.issue(key)) == 'done')
        final = self.jira.issue(key)
        if (not equal_points(final, pf, row['spent'])
                or int(closing['id']) not in sprint_ids(final, sf)
                or category(final) != 'done'):
            raise WizardError('Final verification failed. Review the journal and ticket.')
        row['complete'] = True
        self.save()
        print(f"  ✓ {key} closed" + (f" → {row['clone']} carried over" if row.get('clone') else ''))


def equal_points(issue, field, expected):
    value = issue['fields'].get(field)
    return value is not None and Decimal(str(value)) == Decimal(expected)


def review(jira, settings, key, index, count):
    issue = jira.issue(key)
    fields = issue['fields']
    if category(issue) != 'indeterminate':
        print(f"{key}: no longer in progress; skipped.")
        return None
    estimate = fields.get(settings['points_field'])
    if estimate is not None:
        estimate = points(estimate)
    while True:
        print(f"\n[{index}/{count}] {key}  {fields['summary']}")
        print(f"{(fields.get('assignee') or {}).get('displayName', 'Unassigned')} · "
              f"{fields['status']['name']} · {estimate if estimate is not None else 'unestimated'} points")
        print(f"{settings['site']}/browse/{key}")
        action = ask('[d] Done  [c] Carry remaining work  [s] Skip  [v] View  [q] Quit', 'd').lower()
        if action == 'q':
            raise KeyboardInterrupt
        if action == 'v':
            print(jira.run('issue', 'view', key, '--plain'))
            continue
        if action == 's':
            return None
        if action not in ('d', 'c'):
            continue
        spent = ask_points('Points spent this sprint', estimate if action == 'd' else None)
        remaining = None
        if action == 'c':
            default = str(max(Decimal(estimate) - Decimal(spent), Decimal(0))) if estimate is not None else None
            remaining = ask_points('Remaining estimate (independent re-estimate allowed)', default)
        print(f"\n{key} → {settings['done_status']}, {spent} spent points")
        if action == 'c':
            print(f"New clone → {settings['progress_status']}, {remaining} remaining points, next sprint")
        if yes('Apply this decision?', default=True):
            return dict(key=key, original=issue, estimate=estimate, action=action, spent=spent,
                        remaining=remaining, steps=[], pending=None, complete=False,
                        expected={key: fields['updated']})


def summary(state, dry_run=False):
    rows = list(state['tickets'].values())
    completed = [r for r in rows if r.get('complete')]
    carried = sum(r['action'] == 'c' for r in completed)
    spent = sum((Decimal(r['spent']) for r in completed), Decimal(0))
    remaining = sum((Decimal(r['remaining']) for r in completed if r['action'] == 'c'), Decimal(0))
    skipped = len(state.get('skipped', []))
    pending = sum(not r.get('complete') for r in rows)
    unreviewed = len(set(state['keys']) - set(state['tickets']) - set(state.get('skipped', [])))
    print(f"\n{'DRY RUN — proposed' if dry_run else 'Applied'}: {len(completed)} closed, "
          f"{carried} carried, {skipped} skipped, {pending} unfinished, {unreviewed} unreviewed.")
    print(f"Points spent: {spent}; remaining: {remaining}. Sprint left open.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=ROOT / 'settings.json')
    parser.add_argument('--jira', default='jira', help='Community jira-cli executable')
    parser.add_argument('--sprint', type=int, help='Closing active sprint ID')
    parser.add_argument('--next-sprint', type=int, help='Destination future sprint ID')
    parser.add_argument('--done-transition', help='Workflow transition name if different from Done')
    parser.add_argument('--progress-transition', help='Workflow transition name if different from In Progress')
    parser.add_argument('--dry-run', action='store_true', help='Read Jira and prompt; never mutate Jira or session state')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'state')
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise WizardError('Run this wizard in an interactive terminal.')
    executable = shutil.which(args.jira)
    if not executable:
        raise WizardError('Community jira-cli is required: https://github.com/ankitpokhrel/jira-cli#installation')
    settings = json.loads((args.settings if args.settings.exists() else ROOT / 'settings.example.json').read_text())
    if os.environ.get('JIRA_LOGIN'):
        settings['login'] = os.environ['JIRA_LOGIN'].strip()
    if args.done_transition:
        settings['done_transition'] = args.done_transition
    if args.progress_transition:
        settings['progress_transition'] = args.progress_transition
    if not settings.get('login'):
        settings['login'] = ask('Atlassian email address')
        if not settings['login']:
            raise WizardError('An Atlassian email address is required.')
        if not args.dry_run:
            save_json(args.settings, settings)
    if not os.environ.get('JIRA_API_TOKEN'):
        print('Jira API token (used only for this process; never saved).')
        print('Create/manage tokens: https://id.atlassian.com/manage-profile/security/api-tokens')
        print('Select "Create API token" (without scopes).')
        token = getpass.getpass('Token: ')
        if not token:
            raise WizardError('Set JIRA_API_TOKEN or enter a token.')
        os.environ['JIRA_API_TOKEN'] = token
    with tempfile.TemporaryDirectory(prefix='jira-sprint-') as directory:
        jira = Jira(settings, executable, directory)
        # Use an API command that preserves authentication failures from Jira.
        print('Checking Jira access…')
        jira.check_access()
        active_sprints = jira.sprints('active')
        board = settings['board']
        print(f"\n{settings['site']}\nBoard: {board['name']} (#{board['id']}); filter #{board['filter']}")
        closing, upcoming = choose_sprints(active_sprints, jira.sprints('future'),
                                           args.sprint, args.next_sprint)
        scope = 'mine'
        identity_settings = {k: v for k, v in settings.items()
                             if k not in ('done_transition', 'progress_transition')}
        identity = dict(settings=identity_settings, closing=closing, next=upcoming, scope=scope)
        # Stable filename; all identity-changing options are checked on resume.
        state_dir = (Path(directory) / 'dry-state' if args.dry_run
                     else args.state_dir.expanduser().resolve())
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"board-{board['id']}-sprint-{closing['id']}.json"
        lock_path = path.with_suffix('.lock')
        with open(lock_path, 'a') as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WizardError('Another wizard is working on this sprint.') from None
            query = f'filter = {int(board["filter"])} AND sprint = {closing["id"]} AND statusCategory = "In Progress"'
            query += ' AND assignee = currentUser()'
            if path.exists() and not args.dry_run:
                state = json.loads(path.read_text())
                if state['identity'].get('scope') != 'mine':
                    raise WizardError('This older journal includes other assignees and cannot be resumed '
                                      'by the personal-ticket wizard. The journal has been preserved for review.')
                if state['identity'] != identity:
                    raise WizardError(f'Saved session settings/sprints/scope differ. Resume using the original settings: {path}')
                if not yes(f'Resume saved session {path.name}?', True):
                    return
                # Keep pending originals even if already closed; append newly eligible work.
                added = [key for key in jira.search(query) if key not in state['keys']]
                state['keys'].extend(added)
                if added:
                    print(f'Added {len(added)} newly eligible tickets to this session.')
            else:
                print('Fetching all matching tickets…')
                keys = jira.search(query)
                state = dict(version=1, identity=identity, closing=closing, next=upcoming,
                             keys=keys, tickets={}, skipped=[])
            print(f"\n{closing['name']} → {upcoming['name']}\nScope: only tickets assigned to you ({settings['login']})")
            print(f"{len(state['keys'])} tickets; matched category: In Progress (including review/testing).")
            print(f'JQL: {query}')
            print('Original story points become confirmed spent points. The sprint stays open.')
            if not yes('Start dry run?' if args.dry_run else 'Start reviewing?', True):
                return
            session = Session(jira, settings, path, state)
            if not args.dry_run:
                session.save()
            try:
                for index, key in enumerate(state['keys'], 1):
                    row = state['tickets'].get(key)
                    if row and row.get('complete'):
                        continue
                    if key in state['skipped']:
                        if not yes(f'Revisit skipped ticket {key}?', True):
                            continue
                        state['skipped'].remove(key)
                    if not row:
                        jira.require_mine(key)
                        current = jira.issue(key)
                        if int(closing['id']) not in sprint_ids(current, settings['sprint_field']):
                            raise WizardError(f'{key} left the closing sprint; rerun after reviewing its membership.')
                        row = review(jira, settings, key, index, len(state['keys']))
                        if row is None:
                            state['skipped'].append(key)
                            if not args.dry_run:
                                session.save()
                            continue
                        row['marker'] = f'sprint-carry-{closing["id"]}-{key.lower()}'
                        state['tickets'][key] = row
                        if not args.dry_run:
                            session.save()
                    elif not yes(f'Resume confirmed decision for {key} ({row["action"]})?', True):
                        raise KeyboardInterrupt
                    if args.dry_run:
                        row['complete'] = True
                        print('  DRY RUN — no Jira changes')
                    else:
                        # Revalidate sprint lifecycle immediately before mutations.
                        if (closing['id'] not in {s['id'] for s in jira.sprints('active')}
                                or upcoming['id'] not in {s['id'] for s in jira.sprints('future')}):
                            raise WizardError('Sprint lifecycle changed; stopping before further writes.')
                        jira.require_mine(key)
                        session.apply(row)
                if not args.dry_run:
                    # Include issues started/added during the wizard in the final report.
                    outstanding = jira.search(query)
                    if outstanding:
                        print('\nStill in progress: ' + ', '.join(outstanding))
                        print('Run again to revisit skipped and newly eligible tickets.')
            finally:
                summary(state, args.dry_run)
                if not args.dry_run:
                    print(f'Journal: {path}')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nStopped. Run the same command to resume confirmed decisions.')
        sys.exit(130)
    except (WizardError, OSError, ValueError, KeyError) as error:
        print(f'\nStopped: {error}', file=sys.stderr)
        sys.exit(1)
