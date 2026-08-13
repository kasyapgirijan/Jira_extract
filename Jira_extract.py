import os
import base64
import json
import requests
import pandas as pd

# ============================================================
# CONFIG
# ============================================================

EMAIL = "your.email@company.com"
TOKEN = os.environ["jira_api_token"]

CLOUD_ID = "YOUR-CLOUD-ID"

BASE_URL = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"

OUTPUT_XLSX = "jira_security_bugs.xlsx"
OUTPUT_CSV = "jira_security_bugs.csv"


# ============================================================
# JQL
# ============================================================

JQL = r'''
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
AND status != Closed
'''


# ============================================================
# AUTHENTICATION
# ============================================================

def build_headers():
    raw = f"{EMAIL}:{TOKEN}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("utf-8")

    return {
        "Authorization": f"Basic {encoded}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


HEADERS = build_headers()


# ============================================================
# TEST AUTH
# ============================================================

def test_auth():

    url = f"{BASE_URL}/rest/api/3/myself"

    r = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )

    if not r.ok:
        print("Authentication failed")
        print("Status:", r.status_code)
        print(r.text)
        raise SystemExit(1)

    data = r.json()

    print("Authentication successful")
    print("Logged in as:", data.get("displayName"))
    print()


# ============================================================
# FIELD DISCOVERY
# ============================================================

def get_all_fields():

    url = f"{BASE_URL}/rest/api/3/field"

    r = requests.get(
        url,
        headers=HEADERS,
        timeout=60
    )

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
# VALUE NORMALIZATION
# ============================================================

def jira_value(value):
    """
    Convert Jira objects/custom-field values into readable strings.
    """

    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, bool):
        return str(value)

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, list):

        values = []

        for item in value:
            normalized = jira_value(item)

            if normalized != "":
                values.append(str(normalized))

        return ", ".join(values)

    if isinstance(value, dict):

        for key in [
            "displayName",
            "value",
            "name",
            "key",
            "label",
            "id"
        ]:
            if value.get(key) is not None:
                return jira_value(value[key])

        # Fallback for unexpected custom-field structures
        return json.dumps(
            value,
            ensure_ascii=False
        )

    return str(value)


def version_names(value):

    if not value:
        return ""

    return ", ".join(
        v.get("name", "")
        for v in value
        if isinstance(v, dict)
    )


# ============================================================
# JQL SEARCH
# ============================================================

def search_issues(requested_fields):

    url = f"{BASE_URL}/rest/api/3/search/jql"

    issues = []
    next_page_token = None
    page = 1

    while True:

        payload = {
            "jql": JQL.strip(),
            "fields": requested_fields,
            "maxResults": 100
        }

        if next_page_token:
            payload["nextPageToken"] = next_page_token

        print(f"Downloading page {page}...")

        r = requests.post(
            url,
            headers=HEADERS,
            json=payload,
            timeout=120
        )

        if not r.ok:
            print()
            print("JQL request failed")
            print("Status:", r.status_code)
            print(r.text)
            raise SystemExit(1)

        data = r.json()

        current = data.get("issues", [])

        issues.extend(current)

        print(
            f"Received {len(current)} issues "
            f"| Total: {len(issues)}"
        )

        next_page_token = data.get("nextPageToken")

        if not next_page_token:
            break

        page += 1

    return issues


# ============================================================
# FLATTEN ISSUE
# ============================================================

def flatten_issue(
    issue,
    origin_field,
    cross_team_field,
    seccon_field
):

    f = issue.get("fields", {})

    project = f.get("project") or {}
    issue_type = f.get("issuetype") or {}
    status = f.get("status") or {}
    status_category = status.get("statusCategory") or {}
    priority = f.get("priority") or {}
    resolution = f.get("resolution") or {}
    assignee = f.get("assignee") or {}
    reporter = f.get("reporter") or {}
    creator = f.get("creator") or {}
    security = f.get("security") or {}
    watches = f.get("watches") or {}

    return {

        "Summary":
            f.get("summary", ""),

        "Issue key":
            issue.get("key", ""),

        "Issue id":
            issue.get("id", ""),

        "Issue Type":
            issue_type.get("name", ""),

        "Status":
            status.get("name", ""),

        "Project key":
            project.get("key", ""),

        "Project name":
            project.get("name", ""),

        "Project type":
            project.get("projectTypeKey", ""),

        "Priority":
            priority.get("name", ""),

        "Resolution":
            resolution.get("name", ""),

        "Assignee":
            assignee.get("displayName", ""),

        "Reporter":
            reporter.get("displayName", ""),

        "Creator":
            creator.get("displayName", ""),

        "Created":
            f.get("created", ""),

        "Updated":
            f.get("updated", ""),

        "Affects versions":
            version_names(f.get("versions")),

        "Fix versions":
            version_names(f.get("fixVersions")),

        "Watchers":
            watches.get("watchCount", ""),

        "Status Category":
            status_category.get("name", ""),

        "Security":
            security.get("name", ""),

        "Origin":
            jira_value(
                f.get(origin_field)
            ) if origin_field else "",

        "Cross Functional Team":
            jira_value(
                f.get(cross_team_field)
            ) if cross_team_field else "",

        "SecCon":
            jira_value(
                f.get(seccon_field)
            ) if seccon_field else "",

        "Issue URL":
            (
                f"https://YOUR-SITE.atlassian.net/"
                f"browse/{issue.get('key', '')}"
            )
    }


# ============================================================
# MAIN
# ============================================================

def main():

    test_auth()

    print("Discovering Jira fields...")

    fields = get_all_fields()

    field_map = create_field_map(fields)

    print()
    print("Custom field IDs")
    print("----------------")

    origin_field = get_field_id(
        field_map,
        "Origin"
    )

    cross_team_field = get_field_id(
        field_map,
        "Cross Functional Team"
    )

    seccon_field = get_field_id(
        field_map,
        "SecCon"
    )

    print()

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
        "security"
    ]

    for field in [
        origin_field,
        cross_team_field,
        seccon_field
    ]:
        if field:
            requested_fields.append(field)

    print("Running JQL:")
    print("----------------")
    print(JQL)
    print("----------------")
    print()

    issues = search_issues(
        requested_fields
    )

    print()
    print("Total Jira issues:", len(issues))

    rows = []

    for issue in issues:

        rows.append(
            flatten_issue(
                issue,
                origin_field,
                cross_team_field,
                seccon_field
            )
        )

    df = pd.DataFrame(rows)

    if df.empty:
        print("No issues found.")
        return

    # Convert Jira timestamps cleanly for Excel
    for column in [
        "Created",
        "Updated"
    ]:

        if column in df.columns:

            df[column] = pd.to_datetime(
                df[column],
                errors="coerce",
                utc=True
            ).dt.tz_localize(None)

    # Excel
    df.to_excel(
        OUTPUT_XLSX,
        index=False,
        engine="openpyxl"
    )

    # CSV
    df.to_csv(
        OUTPUT_CSV,
        index=False,
        encoding="utf-8-sig"
    )

    print()
    print("Export complete")
    print("----------------")
    print("Excel:", OUTPUT_XLSX)
    print("CSV  :", OUTPUT_CSV)


if __name__ == "__main__":
    main()
