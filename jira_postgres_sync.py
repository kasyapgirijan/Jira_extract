import os
import json
import base64
import argparse
import configparser
from datetime import datetime, timezone, timedelta

import requests
import psycopg


# ============================================================
# CONFIGURATION
# ============================================================

CONFIG_FILE = os.getenv("JIRA_SYNC_CONFIG", "config.ini")

# Disable interpolation so PostgreSQL passwords containing '%' work normally.
config = configparser.ConfigParser(interpolation=None)
if not config.read(CONFIG_FILE):
    raise FileNotFoundError(f"Could not read configuration file: {CONFIG_FILE}")

EMAIL = config["jira"]["email"]
TOKEN_ENV = config["jira"].get("token_env", "jira_api_token")
TOKEN = os.environ.get(TOKEN_ENV)
if not TOKEN:
    raise RuntimeError(f"Environment variable '{TOKEN_ENV}' is not set")

CLOUD_ID = config["jira"]["cloud_id"]
JIRA_SITE = config["jira"]["site_url"].rstrip("/")
BASE_URL = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"

PG_HOST = config["postgres"]["host"]
PG_PORT = config["postgres"].getint("port", 5432)
PG_DATABASE = config["postgres"]["database"]
PG_USER = config["postgres"]["user"]
PG_PASSWORD = config["postgres"]["password"]

SYNC_NAME = config["sync"].get("sync_name", "jira_security_issues")
SYNC_OVERLAP_MINUTES = config["sync"].getint("overlap_minutes", 5)


# ============================================================
# BASELINE JQL
#
# This is intentionally kept as a literal JQL block, matching the working
# Jira_extract.py approach. The only intentional change is that there is no
# status filter, so Closed/Done/Resolved issues are included in the offline DB.
# ============================================================

BASE_JQL = r'''
issuetype = Bug
AND origin = "Security Testing"
AND "Cross Functional Team" = "EPO/Product Intervention"
AND project IN (
    "aRcore Platform Development",
    "AI ML Development",
    "Data Bridge",
    "LifeSphere Advanced Compliance Docs",
    "LifeSphere Advanced Signals",
    "LifeSphere Business Intelligence",
    "LifeSphere Connect",
    "LifeSphere CTMS",
    "LifeSphere EasyDocs",
    "LifeSphere EDC",
    "LifeSphere Electronic Submission (Legacy)",
    "LifeSphere EV Triage",
    "LifeSphere Literature Intelligence",
    "LifeSphere Medical Information",
    "LifeSphere MultiVigilance",
    "LifeSphere Product Complaints",
    "LifeSphere Regulatory",
    "LifeSphere Reporter MT",
    "LifeSphere Reporter PV",
    "LifeSphere RIDS",
    "LifeSphere Safety Document Distribution",
    "LifeSphere Safety MultiVigilance (Legacy)",
    "LifeSphere Signals Intelligence",
    "LifeSphere SUSAR Reporting",
    "LifeSphere Trial Disclosure",
    "LifeSphere Vet Safety",
    "LSMW Admin App",
    "Navix Agents",
    "Platform",
    "SPORIFY v2"
)
'''


# ============================================================
# AUTHENTICATION - SAME PATTERN AS WORKING Jira_extract.py
# ============================================================

def build_headers():
    raw = f"{EMAIL}:{TOKEN}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("utf-8")
    return {
        "Authorization": f"Basic {encoded}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


HEADERS = build_headers()


def test_auth():
    url = f"{BASE_URL}/rest/api/3/myself"
    r = requests.get(url, headers=HEADERS, timeout=30)

    if not r.ok:
        print("Authentication failed")
        print("Status:", r.status_code)
        print(r.text)
        raise SystemExit(1)

    data = r.json()
    print("Authentication successful")
    print("Logged in as:", data.get("displayName"))


# ============================================================
# JIRA FIELD DISCOVERY - SAME PATTERN AS WORKING EXTRACTOR
# ============================================================

def get_all_fields():
    url = f"{BASE_URL}/rest/api/3/field"
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def create_field_map(fields):
    result = {}
    for field in fields:
        name = field.get("name")
        if name:
            result[name.lower()] = field.get("id")
    return result


def get_field_id(field_map, name):
    field_id = field_map.get(name.lower())
    if field_id:
        print(f"{name}: {field_id}")
    else:
        print(f"WARNING: Could not find field: {name}")
    return field_id


# ============================================================
# JIRA VALUE HELPERS
# ============================================================

def jira_value(value):
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, list):
        values = [jira_value(item) for item in value]
        return ", ".join(str(v) for v in values if v is not None)

    if isinstance(value, dict):
        for key in ("displayName", "value", "name", "key", "label", "id"):
            if value.get(key) is not None:
                return jira_value(value[key])
        return json.dumps(value, ensure_ascii=False)

    return str(value)


def version_names(values):
    if not values:
        return None
    return ", ".join(
        item.get("name")
        for item in values
        if isinstance(item, dict) and item.get("name")
    )


# ============================================================
# JQL
# ============================================================

def build_jql(last_sync=None, force_full=False):
    # Full mode deliberately returns the exact baseline JQL without ORDER BY
    # or updated filters. This lets us compare behaviour directly with the
    # known-good Jira_extract.py script.
    if force_full or last_sync is None:
        return BASE_JQL.strip()

    incremental_from = last_sync - timedelta(minutes=SYNC_OVERLAP_MINUTES)
    jira_time = incremental_from.strftime("%Y-%m-%d %H:%M")

    return (
        BASE_JQL.strip()
        + f'\nAND updated >= "{jira_time}"'
        + "\nORDER BY updated ASC"
    )


# ============================================================
# JIRA SEARCH - SAME PAGINATION/PAYLOAD AS WORKING EXTRACTOR
# ============================================================

def search_issues(jql, requested_fields):
    url = f"{BASE_URL}/rest/api/3/search/jql"

    issues = []
    next_page_token = None
    page = 1

    while True:
        payload = {
            "jql": jql.strip(),
            "fields": requested_fields,
            "maxResults": 100,
        }

        if next_page_token:
            payload["nextPageToken"] = next_page_token

        print(f"Downloading page {page}...")

        r = requests.post(
            url,
            headers=HEADERS,
            json=payload,
            timeout=120,
        )

        if not r.ok:
            print("JQL request failed")
            print("Status:", r.status_code)
            print(r.text)
            raise RuntimeError(f"Jira search failed with HTTP {r.status_code}")

        data = r.json()
        current = data.get("issues", [])
        issues.extend(current)

        print(f"Received {len(current)} issues | Total: {len(issues)}")

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

        page += 1

    return issues


# ============================================================
# POSTGRESQL
# ============================================================

def get_db_connection():
    return psycopg.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DATABASE,
        user=PG_USER,
        password=PG_PASSWORD,
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
            issue_url TEXT,
            raw_json JSONB,
            db_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            db_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

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

        # Migration compatibility with earlier versions of this repository.
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS raw_json JSONB;")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS db_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
        cur.execute("ALTER TABLE jira_issues ADD COLUMN IF NOT EXISTS db_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_updated ON jira_issues(jira_updated_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_project ON jira_issues(project_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jira_issues_status ON jira_issues(status);")

        # No status filter here: Power BI gets the complete offline copy.
        cur.execute("""
        CREATE OR REPLACE VIEW vw_security_jira_issues AS
        SELECT
            issue_id, issue_key, summary, issue_type, status, status_category,
            project_key, project_name, project_type, priority, resolution,
            assignee, reporter, creator, origin, cross_functional_team, seccon,
            jira_created_at, jira_updated_at, affects_versions, fix_versions,
            watchers, security_level, issue_url, db_created_at, db_updated_at
        FROM jira_issues;
        """)

    conn.commit()


def get_db_issue_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM jira_issues")
        return cur.fetchone()[0]


def get_last_sync(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_sync_at FROM jira_sync_state WHERE sync_name = %s",
            (SYNC_NAME,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def save_sync_success(conn, sync_started_at, issue_count):
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO jira_sync_state
            (sync_name, last_sync_at, last_issue_count, last_status, last_error, updated_at)
        VALUES (%s, %s, %s, 'SUCCESS', NULL, NOW())
        ON CONFLICT (sync_name) DO UPDATE SET
            last_sync_at = EXCLUDED.last_sync_at,
            last_issue_count = EXCLUDED.last_issue_count,
            last_status = 'SUCCESS',
            last_error = NULL,
            updated_at = NOW();
        """, (SYNC_NAME, sync_started_at, issue_count))


def save_sync_failure(conn, error):
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO jira_sync_state
            (sync_name, last_status, last_error, updated_at)
        VALUES (%s, 'FAILED', %s, NOW())
        ON CONFLICT (sync_name) DO UPDATE SET
            last_status = 'FAILED',
            last_error = EXCLUDED.last_error,
            updated_at = NOW();
        """, (SYNC_NAME, str(error)[:5000]))


UPSERT_SQL = """
INSERT INTO jira_issues (
    issue_id, issue_key, summary, issue_type, status, status_category,
    project_key, project_name, project_type, priority, resolution,
    assignee, reporter, creator, jira_created_at, jira_updated_at,
    affects_versions, fix_versions, watchers, security_level,
    origin, cross_functional_team, seccon, issue_url, raw_json,
    db_created_at, db_updated_at
) VALUES (
    %(issue_id)s, %(issue_key)s, %(summary)s, %(issue_type)s, %(status)s, %(status_category)s,
    %(project_key)s, %(project_name)s, %(project_type)s, %(priority)s, %(resolution)s,
    %(assignee)s, %(reporter)s, %(creator)s, %(jira_created_at)s, %(jira_updated_at)s,
    %(affects_versions)s, %(fix_versions)s, %(watchers)s, %(security_level)s,
    %(origin)s, %(cross_functional_team)s, %(seccon)s, %(issue_url)s,
    %(raw_json)s::jsonb, NOW(), NOW()
)
ON CONFLICT (issue_id) DO UPDATE SET
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
    issue_url = EXCLUDED.issue_url,
    raw_json = EXCLUDED.raw_json,
    db_updated_at = NOW()
WHERE jira_issues.jira_updated_at IS DISTINCT FROM EXCLUDED.jira_updated_at
   OR jira_issues.issue_key IS DISTINCT FROM EXCLUDED.issue_key;
"""


def issue_to_record(issue, origin_field, cross_team_field, seccon_field):
    fields = issue.get("fields", {})
    project = fields.get("project") or {}
    status = fields.get("status") or {}

    return {
        "issue_id": int(issue["id"]),
        "issue_key": issue["key"],
        "summary": fields.get("summary"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("name"),
        "project_key": project.get("key"),
        "project_name": project.get("name"),
        "project_type": project.get("projectTypeKey"),
        "priority": (fields.get("priority") or {}).get("name"),
        "resolution": (fields.get("resolution") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "creator": (fields.get("creator") or {}).get("displayName"),
        "jira_created_at": fields.get("created"),
        "jira_updated_at": fields.get("updated"),
        "affects_versions": version_names(fields.get("versions")),
        "fix_versions": version_names(fields.get("fixVersions")),
        "watchers": (fields.get("watches") or {}).get("watchCount"),
        "security_level": (fields.get("security") or {}).get("name"),
        "origin": jira_value(fields.get(origin_field)) if origin_field else None,
        "cross_functional_team": jira_value(fields.get(cross_team_field)) if cross_team_field else None,
        "seccon": jira_value(fields.get(seccon_field)) if seccon_field else None,
        "issue_url": f"{JIRA_SITE}/browse/{issue['key']}",
        "raw_json": json.dumps(issue, ensure_ascii=False),
    }


def upsert_issues(conn, issues, origin_field, cross_team_field, seccon_field):
    with conn.cursor() as cur:
        for issue in issues:
            cur.execute(
                UPSERT_SQL,
                issue_to_record(issue, origin_field, cross_team_field, seccon_field),
            )
    return len(issues)


# ============================================================
# MAIN
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Sync Jira security issues to PostgreSQL")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force the exact baseline Jira JQL and ignore the incremental checkpoint",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("JIRA -> POSTGRES SYNC")
    test_auth()

    print("Discovering Jira fields...")
    fields = get_all_fields()
    field_map = create_field_map(fields)

    origin_field = get_field_id(field_map, "Origin")
    cross_team_field = get_field_id(field_map, "Cross Functional Team")
    seccon_field = get_field_id(field_map, "SecCon")

    if not origin_field or not cross_team_field:
        raise RuntimeError("Required Jira custom fields were not found")

    requested_fields = [
        "summary",
        "issuetype",
        "status",
        "project",
        "priority",
        "resolution",
        "assignee",
        "reporter",
        "creator",
        "created",
        "updated",
        "versions",
        "fixVersions",
        "watches",
        "security",
        origin_field,
        cross_team_field,
    ]

    if seccon_field:
        requested_fields.append(seccon_field)

    sync_started_at = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        initialize_database(conn)

        try:
            existing_rows = get_db_issue_count(conn)
            last_sync = get_last_sync(conn)

            # Critical safeguard: an empty issue table always means FULL LOAD,
            # even if a stale sync_state row exists from an earlier zero-result run.
            full_load = args.full or existing_rows == 0 or last_sync is None

            print("Database issue rows:", existing_rows)
            print("Mode:", "FULL baseline load" if full_load else "incremental")
            if last_sync:
                print("Last successful sync:", last_sync)

            jql = build_jql(last_sync=last_sync, force_full=full_load)

            print("JQL:")
            print("--------------------------------------------------")
            print(jql)
            print("--------------------------------------------------")

            issues = search_issues(jql, requested_fields)

            # Do not poison the checkpoint when the first historical pull
            # unexpectedly returns zero records.
            if full_load and existing_rows == 0 and len(issues) == 0:
                raise RuntimeError(
                    "Baseline Jira query returned 0 issues. "
                    "No sync checkpoint was saved. Run Jira_extract.py with the same "
                    "credentials/JQL and compare the result before retrying."
                )

            processed = upsert_issues(
                conn,
                issues,
                origin_field,
                cross_team_field,
                seccon_field,
            )

            save_sync_success(conn, sync_started_at, processed)
            conn.commit()

            total_rows = get_db_issue_count(conn)
            print("Sync successful")
            print("Jira issues received:", len(issues))
            print("Records processed:", processed)
            print("Total rows now in jira_issues:", total_rows)

        except Exception as error:
            conn.rollback()

            # Do not create a FAILED sync_state row when an empty first load is
            # the problem; keeping the table empty ensures the next run remains full.
            try:
                if get_db_issue_count(conn) > 0:
                    save_sync_failure(conn, error)
                    conn.commit()
            except Exception:
                pass

            print("SYNC FAILED:", error)
            raise


if __name__ == "__main__":
    main()
