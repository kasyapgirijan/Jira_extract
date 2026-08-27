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
- keeps all matching Jira statuses, including closed/resolved/done issues, as an offline reporting copy

## Jira filter

The sync uses this business filter and deliberately does **not** filter on status:

```jql
issuetype = Bug
AND origin = "Security Testing"
AND "Cross Functional Team" = "EPO/Product Intervention"
AND project IN (...configured project list...)
```

This means closed issues are retained in PostgreSQL together with open issues. `status` and `status_category` remain available as columns so Power BI can apply status filters without losing the underlying historical/offline dataset.

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

- `jira_issues` — current Jira issue state, including all matching statuses
- `jira_sync_state` — checkpoint and sync status
- `vw_security_jira_issues` — matching security issues intended for reporting/Power BI

The PostgreSQL database and database user themselves must already exist.

## Incremental behavior

On the first run there is no checkpoint, so all Jira issues matching the security filter are queried regardless of status. After a successful run, `jira_sync_state.last_sync_at` is recorded.

Later runs query Jira using approximately:

```jql
issuetype = Bug
AND origin = "Security Testing"
AND "Cross Functional Team" = "EPO/Product Intervention"
AND project IN (...)
AND updated >= "<last successful sync minus overlap>"
ORDER BY updated ASC
```

The status condition is intentionally omitted so changes to closed, resolved, or done issues are still synchronized.

## Configuration note

Python `ConfigParser` interpolation is disabled in the sync script, so PostgreSQL passwords containing `%` are accepted without escaping the character.

## Security

Do not commit API tokens or production database passwords. `config.ini` is ignored by Git and `config.example.ini` contains placeholders only.
