# Jira Extract

Jira Cloud extraction and PostgreSQL synchronization for reporting/Power BI.

## Why this refactor exists

The working local exporter and the PostgreSQL sync had drifted apart. In particular, the working exporter was using:

```jql
AND "Cross Functional Team" IS NOT EMPTY
```

while the PostgreSQL version had been using an exact value filter. That made the two scripts return different datasets. The project lists had also diverged.

The refactored code now has **one Jira client and one JQL file shared by both outputs**, so the Excel/CSV exporter and PostgreSQL loader cannot silently use different Jira logic.

## Structure

- `jira_core.py` — authentication, retries, Jira field discovery, pagination and record normalization
- `query.jql` — local, Git-ignored business query on the sync host (not shipped in the repository)
- `Jira_extract.py` — Excel/CSV exporter built on `jira_core.py`
- `jira_postgres_sync.py` — PostgreSQL full/incremental sync built on the same core
- `config.example.ini` — configuration template
- `requirements.txt` — Python dependencies

## Current Jira baseline

`query.jql` follows the working extractor baseline and deliberately has **no status filter**, so the offline database can retain open, closed, resolved and done issues.

The important working condition is:

```jql
issuetype = Bug
AND origin = "Security Testing"
AND "Cross Functional Team" IS NOT EMPTY
AND project IN (...)
```

If the project set changes, edit only `query.jql`. Both scripts will immediately use the same query.

The shared Jira layer also discovers and extracts the custom fields `Origin`, `Cross Functional Team`, `Security SecCon`/`SecCon`, and `Severity`. The Severity value is stored as text (for example `Severity-1`, `Severity-2`, `Severity-3`) in PostgreSQL and is also included in Excel/CSV exports.

## Setup

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create the local configuration:

```powershell
Copy-Item config.example.ini config.ini
```

Place your server's business query in `query.jql` alongside the configuration,
or use `--jql` / `JIRA_JQL_FILE` to point to its local path. Both scripts read
that file; no query is bundled with new checkouts. Keep the Origin condition
`origin = "Security Testing"`: PostgreSQL cleanup uses that scope.

**Existing installations:** back up your local `query.jql` outside the checkout
before pulling the commit that untracks it, then restore it or point
`JIRA_JQL_FILE` to the backup. Git may remove the previously tracked file during
that update. Subsequent edits to the restored file remain local.

Edit `config.ini` with your Jira and PostgreSQL settings, including the scoped Jira API token:

```ini
[jira]
email = your.email@company.com
api_token = YOUR_JIRA_API_TOKEN
cloud_id = YOUR-CLOUD-ID
site_url = https://YOUR-SITE.atlassian.net

[postgres]
host = localhost
port = 5432
database = jira_reporting
user = jira_sync
password = YOUR_POSTGRES_PASSWORD

[sync]
sync_name = jira_security_issues
overlap_minutes = 5
```

No Jira token environment variable is required. `config.ini` is ignored by Git and must remain local/private. `ConfigParser` interpolation is disabled, so PostgreSQL passwords containing `%` work without escaping.

## Validate Jira first

Both scripts use the same Jira code and `query.jql`. You can validate the Jira side with:

```powershell
python .\Jira_extract.py
```

It will generate:

- `jira_security_bugs.xlsx`
- `jira_security_bugs.csv`

The Jira HTTP session retries temporary connection failures, HTTP 429 rate limits and common 5xx responses. This is useful for large exports that require many Jira pages.

## PostgreSQL full baseline

Force a complete load with:

```powershell
python .\jira_postgres_sync.py --full
```

The script creates these objects automatically in the configured database:

- `jira_issues`
- `jira_sync_state`
- `vw_security_jira_issues`

The PostgreSQL database and user must already exist.

Each Jira page is UPSERTed and committed using Jira `issue_id` as the primary key. If a later Jira page fails because of a transient network problem, pages already written remain in PostgreSQL, but the successful-sync checkpoint is **not** advanced. A retry safely replays data using `ON CONFLICT`.

When a newly-added field such as `Severity` needs to be backfilled for existing historical rows, run one `--full` sync. The schema migration itself is automatic (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).

## Incremental sync

After a successful baseline, run:

```powershell
python .\jira_postgres_sync.py
```

The sync reads `jira_sync_state.last_sync_at` and adds approximately:

```jql
AND updated >= "<last successful sync minus overlap>"
ORDER BY updated ASC
```

The default overlap is five minutes and can be changed in `config.ini`.

Each hourly run also searches stored Jira IDs using the same `updated` window,
without the Origin, project, team or issue-type filters. It then reads the
Origin of those updated tickets in batches. A ticket whose Origin was corrected away from `Security Testing`
(including a cleared Origin) is removed from `jira_issues` and therefore from
`vw_security_jira_issues`. Assignee changes alone do not remove a ticket.
Run `--full` once after upgrading to repair corrections older than the saved
checkpoint; full runs recheck every stored ID.

Both modes search stored IDs in batches of 100; incremental mode includes the
updated window, while full mode checks all stored IDs. Individual issue reads
are only needed to confirm potential removals, or to verify IDs missing from a
full search. For 12,548 unchanged Security Testing tickets, full cleanup uses
126 search requests (assuming 100 results per page), instead of 12,548 individual
reads. Progress messages show batches, confirmations, database writes and commits.
JQL timestamps use the authenticated Jira user's
timezone (install the updated requirements, including `tzdata`, on Windows).
All checks must succeed before any cleanup is applied. Missing Origin fields,
permission errors, missing issues and API failures abort cleanup and leave the
checkpoint unchanged. Deletions and the success checkpoint commit together;
page upserts already committed remain safe to replay. Concurrent sync runs
against this database are rejected using a PostgreSQL advisory lock.

An empty full search is allowed: stored issues are still checked individually.
Cleanup currently covers Origin corrections only, not changes to project,
issue type or Cross Functional Team, deleted tickets, or lost Jira visibility.

A full load is automatically selected if `jira_issues` is empty or there is no successful checkpoint. You can always override with `--full`.

## Alternate config or JQL files

```powershell
python .\Jira_extract.py --config C:\path\config.ini --jql C:\path\query.jql
python .\jira_postgres_sync.py --full --config C:\path\config.ini --jql C:\path\query.jql
```

You can also set only the file-location overrides if needed:

```powershell
$env:JIRA_SYNC_CONFIG="C:\path\config.ini"
$env:JIRA_JQL_FILE="C:\path\query.jql"
```

These environment variables point to files only; the Jira API token itself is read from `config.ini`.

## Power BI

Point Power BI/PostgreSQL Gateway at:

```text
vw_security_jira_issues
```

The view includes all synchronized statuses and the Severity field. Apply status, Severity, or Cross Functional Team filtering in Power BI without deleting the underlying offline Jira copy.

## Security

Do not commit `config.ini`. It contains the Jira API token and may contain the production PostgreSQL password. Keep the file local to the sync host and restrict NTFS permissions to the Windows account or service account that runs the scheduled task.
