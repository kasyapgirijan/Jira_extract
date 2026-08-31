import argparse

import pandas as pd

from jira_core import (
    JiraClient,
    discover_fields,
    issue_to_record,
    load_config,
    load_jql,
    requested_fields,
)


def main():
    parser = argparse.ArgumentParser(
        description="Export Jira issues to Excel/CSV using the shared Jira query."
    )
    parser.add_argument("--config", help="Path to config.ini")
    parser.add_argument("--jql", help="Path to query.jql")
    parser.add_argument("--xlsx", default="jira_security_bugs.xlsx")
    parser.add_argument("--csv", default="jira_security_bugs.csv")
    args = parser.parse_args()

    cfg = load_config(args.config)
    client = JiraClient(cfg["email"], cfg["token"], cfg["cloud_id"], cfg["site_url"])

    user = client.test_auth()
    print("Authentication successful")
    print("Logged in as:", user.get("displayName"))

    custom_fields = discover_fields(client)
    fields = requested_fields(custom_fields)
    jql = load_jql(args.jql)

    print("\nRunning JQL:")
    print("-" * 72)
    print(jql)
    print("-" * 72)

    rows = []
    for _, issues in client.search_pages(jql, fields):
        for issue in issues:
            rows.append(issue_to_record(issue, cfg["site_url"], custom_fields))

    print(f"\nTotal Jira issues: {len(rows)}")
    if not rows:
        print("No issues found.")
        return

    df = pd.DataFrame(rows)
    df = df.drop(columns=["raw_json"], errors="ignore")

    df = df.rename(columns={
        "issue_id": "Issue id",
        "issue_key": "Issue key",
        "summary": "Summary",
        "issue_type": "Issue Type",
        "status": "Status",
        "status_category": "Status Category",
        "project_key": "Project key",
        "project_name": "Project name",
        "project_type": "Project type",
        "priority": "Priority",
        "resolution": "Resolution",
        "assignee": "Assignee",
        "reporter": "Reporter",
        "creator": "Creator",
        "jira_created_at": "Created",
        "jira_updated_at": "Updated",
        "affects_versions": "Affects versions",
        "fix_versions": "Fix versions",
        "watchers": "Watchers",
        "security_level": "Security",
        "origin": "Origin",
        "cross_functional_team": "Cross Functional Team",
        "seccon": "SecCon",
        "severity": "Severity",
        "security_scan_type": "Security Scan Type",
        "issue_url": "Issue URL",
    })

    for column in ("Created", "Updated"):
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], errors="coerce", utc=True).dt.tz_localize(None)

    df.to_excel(args.xlsx, index=False, engine="openpyxl")
    df.to_csv(args.csv, index=False, encoding="utf-8-sig")

    print("Excel:", args.xlsx)
    print("CSV:", args.csv)


if __name__ == "__main__":
    main()
