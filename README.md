# Jira sprint completion wizard

An interactive Python 3.10+ script using the **community [jira-cli](https://github.com/ankitpokhrel/jira-cli)** for every Jira request. No Python packages are required.

Configure your Jira site, board, saved filter and custom fields in `settings.json`. Runtime access uses the CLI alone.

## Install

Requires Linux/macOS, Python 3.10+ and [community jira-cli](https://github.com/ankitpokhrel/jira-cli#installation) (tested with v1.7.0). Official Atlassian `acli` is a different executable and cannot be substituted.

On a new machine, after installing those dependencies:

```bash
mkdir -p ~/tools ~/.local/bin
git clone https://github.com/grobgl/jira-sprint-complete.git ~/tools/jira-sprint-complete
cd ~/tools/jira-sprint-complete
ln -s "$PWD/sprint_complete.py" ~/.local/bin/jira-sprint-complete
cp settings.example.json settings.json
```

Ensure `~/.local/bin` is on your `PATH`. Edit `settings.json` for your Jira site, project, board, saved filter, custom field IDs and statuses. The checked-in example shows the settings structure; replace its values with your own. Local settings, credentials and session journals are ignored by Git.

## Run

```bash
jira-sprint-complete --dry-run
jira-sprint-complete
```

The command is installed in `~/.local/bin`. Alternatively:

```bash
python3 ~/tools/jira-sprint-complete/sprint_complete.py --dry-run
```

On first run, enter your Atlassian email address and API token. Your email/settings are saved in `settings.json` for real runs. The token is used only for the process and its CLI children; it is never written to the settings or journal. You can supply `JIRA_API_TOKEN` through your existing secret manager instead. The CLI uses basic authentication with an Atlassian API token; select **Create API token** (without scopes) at https://id.atlassian.com/manage-profile/security/api-tokens.

To set the environment variable without typing the token into shell history:

```bash
read -rsp 'Jira API token: ' JIRA_API_TOKEN
echo
export JIRA_API_TOKEN
jira-sprint-complete --dry-run
```

An existing non-empty `JIRA_API_TOKEN` skips the token prompt. Set `JIRA_LOGIN` to your Atlassian email to skip the email prompt, including during dry runs:

```bash
export JIRA_LOGIN='your.name@example.com'
```

`JIRA_LOGIN` overrides the saved email for that run. Jira authenticates with the email/token pair; ticket ownership uses the authenticated account via `currentUser()`. Dry runs do not save your email.

A preliminary issue-search request checks live Jira access before selecting sprints. On a 401 response the wizard explains that credentials were rejected and the token may be expired, revoked or invalid (or the email may not match); Jira does not reliably identify which. A 403 response is reported separately as an access/permission problem. The wizard cannot show a token's expiry date or warn ahead of expiry.

The wizard creates its own temporary CLI configuration. It does not require `jira init` or alter an existing Jira CLI configuration. The temporary configuration includes the numeric Story Points schema, so decimal estimates are sent as JSON numbers.

The wizard is tested with community `jira` CLI **v1.7.0**.

## Interaction

1. Select the closing → next sprint pair in a **single step**. For one active and one future sprint, press Enter once to accept the displayed pair. When there are multiple options, select a pair from the combined list. The first pair uses the earliest-created future sprint (lowest ID). Explicit `--sprint` / `--next-sprint` arguments narrow the list. The CLI exposes at most the latest 50 sprints per state.
2. Confirm the displayed scope. **Only tickets assigned to your authenticated Jira user** are included, using the board's saved filter and the **In Progress status category**, including review/testing statuses. Ownership is rechecked before mutations; reassigned originals and prepared carryovers stop the wizard. There is no all-assignees mode.
3. Review each ticket: **Done / Carry / Skip / View / Quit**. Enter chooses Done. No decision is applied until its points and final confirmation have been answered.
4. Done defaults spent points to the current estimate. Carry requires an explicit spent estimate, then suggests `max(initial − spent, 0)` for remaining work. You can independently re-estimate the remainder. Missing estimates always require input. Zero and decimals (up to three decimal places) are accepted.
5. Inspect the final applied totals and any remaining in-progress tickets. The script **leaves the sprint open**.

For carryovers, the wizard creates a clone with a recovery label, restores/verifies its assignee, parent/epic, points, destination sprint, In Progress status and clone link, then records the split on both tickets. It records the original estimate in a comment **before** replacing the original's Story Points with confirmed spent points and marking it Done. The original retains its closing-sprint membership.

Summary, description, issue type, priority, components and labels are checked against the source. The CLI does not copy attachment contents, comments, history, subtasks, arbitrary custom fields, release versions or all existing links. Carryover subtasks whose parent/sprint rules prevent the move will stop for manual handling; the wizard does not clone an entire hierarchy. Jira workflow validators and permissions can also require manual action.

## Useful options

```bash
# Explicit sprint selection; IDs are still validated against board/state.
jira-sprint-complete --sprint 12345 --next-sprint 12346

# Alternate wizard settings (JSON; see settings.example.json).
jira-sprint-complete --settings /path/to/settings.json

# Some workflows name transitions differently from their destination status.
jira-sprint-complete --done-transition "Complete work" \
  --progress-transition "Start work"

jira-sprint-complete --help
```

`--done-transition` and `--progress-transition` select the CLI's **transition names**. The destination statuses still default to `Done` and `In Progress` and are verified after the transition. These options can be corrected when resuming a partially applied ticket.

## Interruption and recovery

Run the same command, select the same sprint pair and accept **Resume**. The wizard saves each confirmed decision before mutating Jira, persists intent before each request, and checkpoints verified results. Pending tickets are retained even when their originals have already reached Done. Newly eligible tickets are appended on resume; skipped tickets are offered again.

Journal files live under `state/`, one per board/closing sprint, with mode `0600`. They contain issue snapshots and decisions, but no token. Keep them until the sprint is settled. An advisory lock prevents two processes using the same journal concurrently. Use the same state directory for a sprint across runs.

Older journals with all-board scope are rejected and preserved for manual review; they cannot silently resume work on other people's tickets.

If a request fails after Jira might have committed it, resume reconciles observable state first. Recovery labels find existing clones; unique comment markers identify audit comments. If no matching result is visible, the wizard requires an explicit decision before retrying. Jira search can lag, so wait and inspect Jira before retrying an uncertain clone. It never automatically retries a mutation with an uncertain result.

If a tracked ticket changes between checkpoints, the wizard displays its current contents and saved decision, then asks whether to continue; the default is to stop. Rejecting the prompt leaves the journal intact. For an unexpected metadata mismatch, correct the clone in Jira, rerun and explicitly review/accept the changed ticket. To change a decision after some operations have been applied, settle that ticket manually before continuing; do not delete its journal to force a new clone.

This is a sequence of verified CLI operations, not an atomic transaction. Requests already applied remain applied after a later failure. Jira changes occurring during a CLI request cannot be atomically excluded. The journal and verification make these cases reviewable; no rollback deletes a carryover or reopens a ticket automatically.

Dry runs execute the discovery and decision prompts but send no mutations and do not update settings or persistent session state. They do not resume existing partially applied sessions.

## Validation

```bash
cd ~/tools/jira-sprint-complete
python3 -m unittest -v
```

Tests use the actual installed `jira` binary against a local HTTP Jira fixture. They exercise cloning, point splitting, metadata restoration, decimal points, search pagination, no-write dry runs, concurrent edits, and recovery from responses lost after mutations. They never connect to live Jira. Live authentication and your Jira site's workflow permissions need the first authenticated dry run and first confirmed ticket to verify.
