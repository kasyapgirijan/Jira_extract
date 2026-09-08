import argparse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import psycopg

from jira_core import (
    JiraClient,
    add_incremental_clause,
    discover_fields,
    issue_to_record,
    load_config,
    load_jql,
    requested_fields,
)


UPSERT_SQL = """
INSERT INTO jira_issues (
    issue_id, issue_key, summary, issue_type, status, status_category,
    project_key, project_name, project_type, priority, resolution,
    assignee, reporter, creator, jira_created_at, jira_updated_at,
    jira_resolution_at, affects_versions, fix_versions, watchers,
    security_level, origin, cross_functional_team, seccon, severity,
    security_scan_type, security_cvss, issue_url, raw_json,
    db_created_at, db_updated_at
)
VALUES (
    %(issue_id)s, %(issue_key)s, %(summary)s, %(issue_type)s, %(status)s,
    %(status_category)s, %(project_key)s, %(project_name)s, %(project_type)s,
    %(priority)s, %(resolution)s, %(assignee)s, %(reporter)s, %(creator)s,
    %(jira_created_at)s, %(jira_updated_at)s, %(jira_resolution_at)s,
    %(affects_versions)s, %(fix_versions)s, %(watchers)s,
    %(security_level)s, %(origin)s, %(cross_functional_team)s, %(seccon)s,
    %(severity)s, %(security_scan_type)s, %(security_cvss)s, %(issue_url)s,
    %(raw_json)s::jsonb, NOW(), NOW()
)
ON CONFLICT (issue_id)
DO UPDATE SET
    issue_key = EXCLUDED.issue_key,
    summary = EXCLUDED.summary,
    issue_type = EXCLUDED.issue_type,
    status = EXCLUDED.status,
    status_category = EXCLUDED.status_category,
    project_key = EXCLUDED.project_key,
    project_name = EXCLUDED.project_name,
    project_type = EXCLUDED.project_type,
    priority = EXCLUDED.priority,
    resolution = EXCLUDED.resolution,
    assignee = EXCLUDED.assignee,
    reporter = EXCLUDED.reporter,
    creator = EXCLUDED.creator,
    jira_created_at = EXCLUDED.jira_created_at,
    jira_updated_at = EXCLUDED.jira_updated_at,
    jira_resolution_at = EXCLUDED.jira_resolution_at,
    affects_versions = EXCLUDED.affects_versions,
    fix_versions = EXCLUDED.fix_versions,
    watchers = EXCLUDED.watchers,
    security_level = EXCLUDED.security_level,
    origin = EXCLUDED.origin,
    cross_functional_team = EXCLUDED.cross_functional_team,
    seccon = EXCLUDED.seccon,
    severity = EXCLUDED.severity,
    security_scan_type = EXCLUDED.security_scan_type,
    security_cvss = EXCLUDED.security_cvss,
    issue_url = EXCLUDED.issue_url,
    raw_json = EXCLUDED.raw_json,
    db_updated_at = NOW()
WHERE jira_issues.jira_updated_at IS DISTINCT FROM EXCLUDED.jira_updated_at
   OR jira_issues.raw_json IS DISTINCT FROM EXCLUDED.raw_json
   OR jira_issues.jira_resolution_at IS DISTINCT FROM EXCLUDED.jira_resolution_at
   OR jira_issues.severity IS DISTINCT FROM EXCLUDED.severity
   OR jira_issues.security_scan_type IS DISTINCT FROM EXCLUDED.security_scan_type
   OR jira_issues.security_cvss IS DISTINCT FROM EXCLUDED.security_cvss;
"""


def db_connect(cfg):
    pg = cfg["postgres"]
    return psycopg.connect(
        host=pg["host"], port=pg["port"], dbname=pg["database"],
        user=pg["user"], password=pg["password"],
    )


def initialize_database(conn):
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS jira_issues (
            issue_id BIGINT PRIMARY KEY,
            issue_key VARCHAR(100) UNIQUE NOT NULL,
            summary TEXT,
            issue_type VARCHAR(100),
            status VARCHAR(100),
            status_category VARCHAR(100),
            project_key VARCHAR(100),
            project_name TEXT,
            project_type VARCHAR(100),
            priority VARCHAR(100),
            resolution VARCHAR(100),
            assignee TEXT,
            reporter TEXT,
            creator TEXT,
            jira_created_at TIMESTAMPTZ,
            jira_updated_at TIMESTAMPTZ,
            jira_resolution_at TIMESTAMPTZ,
            affects_versions TEXT,
            fix_versions TEXT,
            watchers INTEGER,
            security_level TEXT,
            origin TEXT,
            cross_functional_team TEXT,
            seccon TEXT,
            severity TEXT,
            security_scan_type TEXT,
            security_cvss NUMERIC(4,1),
            issue_url TEXT,
            raw_json JSONB,
            db_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            db_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        # Backward-compatible migrations for databases created by older versions.
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS raw_json JSONB;")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS cross_functional_team TEXT;")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS seccon TEXT;")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS severity TEXT;")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS security_scan_type TEXT;")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS security_cvss NUMERIC(4,1);")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS jira_resolution_at TIMESTAMPTZ;")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS jira_sync_state (
            sync_name VARCHAR(100) PRIMARY KEY,
            last_sync_at TIMESTAMPTZ,
            last_issue_count INTEGER,
            last_status VARCHAR(50),
            last_error TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_updated ON jira_issues(jira_updated_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_resolution_at ON jira_issues(jira_resolution_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_project ON jira_issues(project_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_status ON jira_issues(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_severity ON jira_issues(severity);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_scan_type ON jira_issues(security_scan_type);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_cvss ON jira_issues(security_cvss);")

        # Recreate the view so newly added columns can be inserted safely.
        cur.execute("DROP VIEW IF EXISTS vw_security_jira_issues;")
        cur.execute("""
        CREATE VIEW vw_security_jira_issues AS
        SELECT
            issue_id, issue_key, summary, issue_type, status, status_category,
            project_key, project_name, project_type, priority, resolution,
            assignee, reporter, creator, origin, cross_functional_team,
            seccon, severity, security_scan_type, security_cvss, security_level,
            jira_created_at, jira_updated_at, jira_resolution_at,
            affects_versions, fix_versions, watchers, issue_url,
            db_created_at, db_updated_at
        FROM jira_issues;
        """)
    conn.commit()


def table_row_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM jira_issues")
        return cur.fetchone()[0]


def get_last_sync(conn, sync_name):
    with conn.cursor() as cur:
        cur.execute("SELECT last_sync_at FROM jira_sync_state WHERE sync_name = %s", (sync_name,))
        row = cur.fetchone()
        return row[0] if row else None


def save_success(conn, sync_name, started_at, count):
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO jira_sync_state (
            sync_name, last_sync_at, last_issue_count,
            last_status, last_error, updated_at
        )
        VALUES (%s, %s, %s, 'SUCCESS', NULL, NOW())
        ON CONFLICT (sync_name)
        DO UPDATE SET
            last_sync_at = EXCLUDED.last_sync_at,
            last_issue_count = EXCLUDED.last_issue_count,
            last_status = 'SUCCESS',
            last_error = NULL,
            updated_at = NOW();
        """, (sync_name, started_at, count))
    conn.commit()


def save_failure(conn, sync_name, error):
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO jira_sync_state (sync_name, last_status, last_error, updated_at)
        VALUES (%s, 'FAILED', %s, NOW())
        ON CONFLICT (sync_name)
        DO UPDATE SET
            last_status = 'FAILED',
            last_error = EXCLUDED.last_error,
            updated_at = NOW();
        """, (sync_name, str(error)[:5000]))
    conn.commit()


def upsert_page(conn, issues, cfg, custom_fields):
    records = [issue_to_record(issue, cfg["site_url"], custom_fields) for issue in issues]
    if not records:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, records)
    conn.commit()
    return len(records)


def reconcile_origins(conn, client, custom_fields, since=None):
    """Remove confirmed Origin corrections, including old missed corrections.

    Incremental runs search stored IDs updated since the checkpoint, without
    the business filters that would hide corrected tickets. Full runs check all.
    Stage deletions until every lookup succeeds; the caller commits them with
    the successful checkpoint. A 403/404 or missing field aborts cleanup.
    """
    origin_field = custom_fields["origin"]
    print("Origin cleanup: reading stored issue IDs...", flush=True)
    with conn.cursor() as cur:
        cur.execute("SELECT issue_id FROM jira_issues ORDER BY issue_id")
        issue_ids = [row[0] for row in cur.fetchall()]
    print(f"Origin cleanup: {len(issue_ids)} stored tickets", flush=True)

    candidates = issue_ids
    if since is not None:
        candidates = set()
        for offset in range(0, len(issue_ids), 100):
            batch = issue_ids[offset:offset + 100]
            print(
                f"Origin cleanup: searching updated tickets in batch "
                f"{offset // 100 + 1}/{(len(issue_ids) + 99) // 100}...",
                flush=True,
            )
            id_query = "id IN (" + ",".join(str(i) for i in batch) + ")"
            jql = add_incremental_clause(id_query, since)
            for _, issues in client.search_pages(jql, [origin_field]):
                for issue in issues:
                    issue_id = int(issue["id"])
                    if issue_id not in batch:
                        raise RuntimeError("Unexpected issue in reconciliation search")
                    candidates.add(issue_id)

    removed = []
    print(f"Origin cleanup: verifying {len(candidates)} tickets with Jira", flush=True)
    for index, issue_id in enumerate(sorted(candidates), 1):
        print(f"Origin cleanup: checking {index}/{len(candidates)} (ID {issue_id})...", flush=True)
        issue = client.get_issue(issue_id, [origin_field])
        fields = issue.get("fields") or {}
        if str(issue.get("id")) != str(issue_id) or origin_field not in fields:
            raise RuntimeError(f"Cannot verify Origin for issue {issue_id}; cleanup aborted")
        origin = fields[origin_field]
        if isinstance(origin, dict) and isinstance(origin.get("value"), str):
            origin = origin["value"]
        if origin is None or (isinstance(origin, str) and not origin.strip()):
            removed.append(issue_id)
        elif isinstance(origin, str):
            if origin.strip().casefold() != "security testing":
                removed.append(issue_id)
        else:
            raise RuntimeError(f"Unexpected Origin format for issue {issue_id}; cleanup aborted")

    if removed:
        print(f"Origin cleanup: staging removal of {len(removed)} rows...", flush=True)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jira_issues WHERE issue_id = ANY(%s)", (removed,))
    return len(removed)


def main():
    parser = argparse.ArgumentParser(description="Sync Jira issues into PostgreSQL.")
    parser.add_argument("--config", help="Path to config.ini")
    parser.add_argument("--jql", help="Path to query.jql")
    parser.add_argument(
        "--full", action="store_true",
        help="Ignore the saved checkpoint and perform a full Jira pull.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    sync_name = cfg["sync"]["sync_name"]
    overlap = cfg["sync"]["overlap_minutes"]
    started_at = datetime.now(timezone.utc)

    client = JiraClient(cfg["email"], cfg["token"], cfg["cloud_id"], cfg["site_url"])

    print("JIRA -> POSTGRES SYNC")
    user = client.test_auth()
    print("Authenticated as:", user.get("displayName"))
    # JQL date literals use the authenticated Jira user's timezone.
    jira_timezone = ZoneInfo(user["timeZone"])

    custom_fields = discover_fields(client)
    fields = requested_fields(custom_fields)
    base_jql = load_jql(args.jql)

    print("Connecting to PostgreSQL...", flush=True)
    with db_connect(cfg) as conn:
        # All configurations share jira_issues, so serialize runs across names.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(741920381)")
            if not cur.fetchone()[0]:
                raise RuntimeError("Another Jira sync is already running")
        print("Initializing database schema and reporting view...", flush=True)
        initialize_database(conn)
        rows_before = table_row_count(conn)
        last_sync = get_last_sync(conn, sync_name)
        full_load = args.full or rows_before == 0 or last_sync is None
        since = None

        if full_load:
            jql = base_jql
            print("Mode: FULL baseline load")
        else:
            since = (last_sync - timedelta(minutes=overlap)).astimezone(jira_timezone)
            jql = add_incremental_clause(base_jql, since)
            print("Mode: incremental")
            print("Last successful sync:", last_sync)
            print("Incremental start:", since)

        print("Database rows before sync:", rows_before)
        print("\nJQL:")
        print("-" * 72)
        print(jql)
        print("-" * 72)

        processed = 0
        try:
            for page, issues in client.search_pages(jql, fields):
                print(f"Database: writing Jira page {page} ({len(issues)} tickets)...", flush=True)
                processed += upsert_page(conn, issues, cfg, custom_fields)
                print(f"Database: page {page} committed", flush=True)

            removed = reconcile_origins(conn, client, custom_fields, since)
            print("Committing cleanup and successful-sync checkpoint...", flush=True)
            save_success(conn, sync_name, started_at, processed)
            print("\nSync successful")
            print("Processed this run:", processed)
            print("Removed after Origin correction:", removed)
            print("Database rows after sync:", table_row_count(conn))

        except Exception as error:
            conn.rollback()
            try:
                save_failure(conn, sync_name, error)
            except Exception:
                pass
            raise


if __name__ == "__main__":
    main()
