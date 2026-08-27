# Jira Extract

Utilities for extracting Jira Cloud issue data and loading it into PostgreSQL for reporting/Power BI.

## PostgreSQL sync

`jira_postgres_sync.py` now deliberately uses the working `Jira_extract.py` implementation as its Jira retrieval baseline:

- same scoped-token Basic authentication pattern
- same `/rest/api/3/field` custom-field discovery pattern
- same `/rest/api/3/search/jql` POST request format
- same `nextPageToken` pagination approach
- literal baseline JQL rather than reconstructing the business filter dynamically
- no status filter, so Open/Closed/Resolved/Done issues are kept in the offline database copy

The database layer is added after retrieval and uses Jira `issue_id` as the PostgreSQL primary key with `ON CONFLICT` UPSERT.

## Jira filter

```jql
issuetype = Bug
AND origin = "Security Testing"
AND "Cross Functional Team" = "EPO/Product Intervention"
AND project IN (...configured project list...)
```

There is intentionally no `status` condition.

## Full vs incremental behavior

The script automatically performs a full baseline pull whenever:

- `jira_issues` contains zero rows, or
- no successful checkpoint exists, or
- `--full` is supplied.

Force a full pull with:

```powershell
python .\jira_postgres_sync.py --full
```

A full pull uses the literal baseline JQL exactly, without an `updated` clause or `ORDER BY`, so its Jira retrieval behavior can be compared directly with the known-good `Jira_extract.py` script.

Once the database contains data and a successful checkpoint exists, later runs append an incremental condition similar to:

```jql
AND updated >= "<last successful sync minus overlap>"
ORDER BY updated ASC
```

If an empty database receives zero Jira issues during a full baseline pull, the script raises an error and does **not** save a success checkpoint. This prevents a zero-result first run from incorrectly switching future runs into incremental mode.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Copy the example configuration:

```powershell
Copy-Item config.example.ini config.ini
```

Edit `config.ini` with Jira Cloud ID/site details and PostgreSQL connection details.

The real `config.ini` is excluded by `.gitignore` because it can contain database credentials.

Set the Jira token as an environment variable:

```powershell
$env:jira_api_token="YOUR_TOKEN"
python .\jira_postgres_sync.py --full
```

Linux/macOS:

```bash
export jira_api_token='YOUR_TOKEN'
python3 jira_postgres_sync.py --full
```

You can optionally use another INI path:

```powershell
$env:JIRA_SYNC_CONFIG="C:\path\to\config.ini"
python .\jira_postgres_sync.py --full
```

## Database objects

The script creates these objects inside the configured PostgreSQL database if they do not already exist:

- `jira_issues` — offline/current copy of matching Jira issues across all statuses
- `jira_sync_state` — incremental checkpoint and sync status
- `vw_security_jira_issues` — Power BI/reporting view over the stored Jira issues

The PostgreSQL database and database user themselves must already exist.

## Configuration note

Python `ConfigParser` interpolation is disabled, so PostgreSQL passwords containing `%` work without escaping the character.

## Security

Do not commit API tokens or production database passwords. `config.ini` is ignored by Git and `config.example.ini` contains placeholders only.
