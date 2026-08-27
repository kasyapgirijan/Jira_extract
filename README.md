# Jira Extract

Utilities for extracting Jira Cloud issue data and loading it into PostgreSQL for reporting/Power BI.

## Incremental PostgreSQL sync

`jira_postgres_sync.py`:

- authenticates to Jira Cloud using a scoped Atlassian API token
- discovers custom Jira field IDs such as `Origin`, `Cross Functional Team`, and `SecCon`
- creates the PostgreSQL tables, indexes, sync-state table, and reporting view automatically
- performs a full load on the first run
- performs incremental loads afterwards using Jira's `updated` timestamp
- overlaps incremental windows by a configurable number of minutes to avoid boundary misses
- uses Jira `issue_id` as the PostgreSQL primary key
- uses PostgreSQL `ON CONFLICT` to update existing records
- keeps issues that leave the active filter and marks them with `in_scope = false`, so closed issues do not remain stale in reporting

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Copy the example configuration:

```bash
cp config.example.ini config.ini
```

On Windows PowerShell:

```powershell
Copy-Item config.example.ini config.ini
```

Edit `config.ini` with your Jira Cloud ID/site details and PostgreSQL connection details.

The real `config.ini` is excluded by `.gitignore` because it can contain database credentials.

Set the Jira API token as an environment variable rather than putting it in source control:

```powershell
$env:jira_api_token="YOUR_TOKEN"
python .\jira_postgres_sync.py
```

Linux/macOS:

```bash
export jira_api_token='YOUR_TOKEN'
python3 jira_postgres_sync.py
```

You can optionally point the script at another INI file:

```powershell
$env:JIRA_SYNC_CONFIG="C:\path\to\config.ini"
python .\jira_postgres_sync.py
```

## Database objects

The script creates these objects inside the configured PostgreSQL database if they do not already exist:

- `jira_issues` — current Jira issue state
- `jira_sync_state` — checkpoint and sync status
- `vw_security_jira_issues` — active security issues intended for reporting/Power BI

The PostgreSQL database and database user themselves must already exist.

## Incremental behavior

On the first run there is no checkpoint, so all bugs from the configured project set are queried. After a successful run, `jira_sync_state.last_sync_at` is recorded.

Later runs query Jira using approximately:

```jql
issuetype = Bug
AND project IN (...)
AND updated >= "<last successful sync minus overlap>"
```

The incremental JQL intentionally does **not** include `status != Closed`. This allows a previously open issue that becomes closed to be returned on the next sync and marked `in_scope = false` in PostgreSQL.

The reporting view exposes only rows where `in_scope = true`.

## Security

Do not commit API tokens or production database passwords. `config.ini` is ignored by Git and `config.example.ini` contains placeholders only.
