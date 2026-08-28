import argparse
from datetime import datetime, timezone, timedelta

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
    issue_id,
    issue_key,
    summary,
    issue_type,
    status,
    status_category,
    project_key,
    project_name,
    project_type,
    priority,
    resolution,
    assignee,
    reporter,
    creator,
    jira_created_at,
    jira_updated_at,
    affects_versions,
    fix_versions,
    watchers,
    security_level,
    origin,
    cross_functional_team,
    seccon,
    severity,
    issue_url,
    raw_json,
    db_created_at,
    db_updated_at
)
VALUES (
    %(issue_id)s,
    %(issue_key)s,
    %(summary)s,
    %(issue_type)s,
    %(status)s,
    %(status_category)s,
    %(project_key)s,
    %(project_name)s,
    %(project_type)s,
    %(priority)s,
    %(resolution)s,
    %(assignee)s,
    %(reporter)s,
    %(creator)s,
    %(jira_created_at)s,
    %(jira_updated_at)s,
    %(affects_versions)s,
    %(fix_versions)s,
    %(watchers)s,
    %(security_level)s,
    %(origin)s,
    %(cross_functional_team)s,
    %(seccon)s,
    %(severity)s,
    %(issue_url)s,
    %(raw_json)s::jsonb,
    NOW(),
    NOW()
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
    affects_versions = EXCLUDED.affects_versions,
    fix_versions = EXCLUDED.fix_versions,
    watchers = EXCLUDED.watchers,
    security_level = EXCLUDED.security_level,
    origin = EXCLUDED.origin,
    cross_functional_team = EXCLUDED.cross_functional_team,
    seccon = EXCLUDED.seccon,
    severity = EXCLUDED.severity,
    issue_url = EXCLUDED.issue_url,
    raw_json = EXCLUDED.raw_json,
    db_updated_at = NOW()
WHERE jira_issues.jira_updated_at IS DISTINCT FROM EXCLUDED.jira_updated_at
   OR jira_issues.raw_json IS DISTINCT FROM EXCLUDED.raw_json
   OR jira_issues.severity IS DISTINCT FROM EXCLUDED.severity;
"""


def db_connect(cfg):
    pg = cfg["postgres"]
    return psycopg.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["database"],
        user=pg["user"],
        password=pg["password"],
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
            affects_versions TEXT,
            fix_versions TEXT,
            watchers INTEGER,
            security_level TEXT,
            origin TEXT,
            cross_functional_team TEXT,
            seccon TEXT,
            severity TEXT,
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

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jira_issues_updated "
            "ON jira_issues(jira_updated_at);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jira_issues_project "
            "ON jira_issues(project_key);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jira_issues_status "
            "ON jira_issues(status);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jira_issues_severity "
            "ON jira_issues(severity);"
        )

        cur.execute("""
        CREATE OR REPLACE VIEW vw_security_jira_issues AS
        SELECT
            issue_id,
            issue_key,
            summary,
            issue_type,
            status,
            status_category,
            project_key,
            project_name,
            project_type,
            priority,
            resolution,
            assignee,
            reporter,
            creator,
            origin,
            cross_functional_team,
            seccon,
            severity,
            security_level,
            jira_created_at,
            jira_updated_at,
            affects_versions,
            fix_versions,
            watchers,
            issue_url,
            db_created_at,
            db_updated_at
        FROM jira_issues;
        """)

    conn.commit()


def table_row_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM jira_issues")
        return cur.fetchone()[0]


def get_last_sync(conn, sync_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_sync_at FROM jira_sync_state WHERE sync_name = %s",
            (sync_name,),
        )
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
        INSERT INTO jira_sync_state (
            sync_name, last_status, last_error, updated_at
        )
        VALUES (%s, 'FAILED', %s, NOW())
        ON CONFLICT (sync_name)
        DO UPDATE SET
            last_status = 'FAILED',
            last_error = EXCLUDED.last_error,
            updated_at = NOW();
        """, (sync_name, str(error)[:5000]))
    conn.commit()


def upsert_page(conn, issues, cfg, custom_fields):
    records = [
        issue_to_record(issue, cfg["site_url"], custom_fields)
        for issue in issues
    ]
    if not records:
        return 0

    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, records)

    # Commit each successful Jira page. If a later network call fails, the
    # checkpoint is not advanced, so the next run safely replays the overlap.
    conn.commit()
    return len(records)


def main():
    parser = argparse.ArgumentParser(
        description="Sync Jira issues into PostgreSQL."
    )
    parser.add_argument("--config", help="Path to config.ini")
    parser.add_argument("--jql", help="Path to query.jql")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore the saved checkpoint and perform a full Jira pull.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    sync_name = cfg["sync"]["sync_name"]
    overlap = cfg["sync"]["overlap_minutes"]
    started_at = datetime.now(timezone.utc)

    client = JiraClient(
        cfg["email"],
        cfg["token"],
        cfg["cloud_id"],
        cfg["site_url"],
    )

    print("JIRA -> POSTGRES SYNC")
    user = client.test_auth()
    print("Authenticated as:", user.get("displayName"))

    custom_fields = discover_fields(client)
    fields = requested_fields(custom_fields)
    base_jql = load_jql(args.jql)

    with db_connect(cfg) as conn:
        initialize_database(conn)

        rows_before = table_row_count(conn)
        last_sync = get_last_sync(conn, sync_name)

        full_load = args.full or rows_before == 0 or last_sync is None

        if full_load:
            jql = base_jql
            print("Mode: FULL baseline load")
        else:
            since = last_sync - timedelta(minutes=overlap)
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
            for _, issues in client.search_pages(jql, fields):
                processed += upsert_page(conn, issues, cfg, custom_fields)

            if full_load and processed == 0:
                raise RuntimeError(
                    "Full baseline Jira query returned 0 issues. "
                    "No checkpoint was advanced."
                )

            save_success(conn, sync_name, started_at, processed)

            print("\nSync successful")
            print("Processed this run:", processed)
            print("Database rows after sync:", table_row_count(conn))

        except Exception as error:
            # Page-level inserts already committed remain safe. Do not advance
            # last_sync_at; the next run will replay from the previous checkpoint.
            try:
                save_failure(conn, sync_name, error)
            except Exception:
                pass
            raise


if __name__ == "__main__":
    main()
