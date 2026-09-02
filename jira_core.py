import base64
import configparser
import json
import os
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_CONFIG = "config.ini"
DEFAULT_JQL_FILE = "query.jql"


def load_config(path=None):
    config_path = path or os.getenv("JIRA_SYNC_CONFIG", DEFAULT_CONFIG)
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(config_path):
        raise FileNotFoundError(f"Could not read configuration file: {config_path}")

    token = parser["jira"].get("api_token", "").strip()
    if not token:
        raise RuntimeError(
            "Jira API token is missing from config.ini. "
            "Add 'api_token = YOUR_TOKEN' under the [jira] section."
        )

    return {
        "config_path": config_path,
        "email": parser["jira"]["email"].strip(),
        "token": token,
        "cloud_id": parser["jira"]["cloud_id"].strip(),
        "site_url": parser["jira"]["site_url"].rstrip("/"),
        "postgres": {
            "host": parser["postgres"]["host"].strip(),
            "port": parser["postgres"].getint("port", 5432),
            "database": parser["postgres"]["database"].strip(),
            "user": parser["postgres"]["user"].strip(),
            "password": parser["postgres"]["password"],
        },
        "sync": {
            "sync_name": parser["sync"].get("sync_name", "jira_security_issues"),
            "overlap_minutes": parser["sync"].getint("overlap_minutes", 5),
        },
    }


def load_jql(path=None):
    jql_path = Path(path or os.getenv("JIRA_JQL_FILE", DEFAULT_JQL_FILE))
    if not jql_path.exists():
        raise FileNotFoundError(f"Could not read JQL file: {jql_path}")
    jql = jql_path.read_text(encoding="utf-8").strip()
    if not jql:
        raise RuntimeError(f"JQL file is empty: {jql_path}")
    return jql


def add_incremental_clause(base_jql, since):
    if not since:
        return base_jql.strip()

    jira_time = since.strftime("%Y-%m-%d %H:%M")
    text = base_jql.strip()
    lower = text.lower()
    pos = lower.rfind("order by ")
    if pos >= 0:
        body = text[:pos].rstrip()
        order = text[pos:].strip()
        return f'{body}\nAND updated >= "{jira_time}"\n{order}'
    return f'{text}\nAND updated >= "{jira_time}"\nORDER BY updated ASC'


def jira_value(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        items = [jira_value(item) for item in value]
        return ", ".join(str(item) for item in items if item not in (None, ""))
    if isinstance(value, dict):
        for key in ("displayName", "value", "name", "key", "label", "id"):
            if value.get(key) is not None:
                return jira_value(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def jira_number(value):
    """Normalize a Jira numeric custom field to float, returning None when blank/non-numeric."""
    normalized = jira_value(value)
    if normalized in (None, ""):
        return None
    try:
        return float(normalized)
    except (TypeError, ValueError):
        return None


def version_names(values):
    if not values:
        return None
    return ", ".join(
        item.get("name")
        for item in values
        if isinstance(item, dict) and item.get("name")
    )


class JiraClient:
    def __init__(self, email, token, cloud_id, site_url):
        self.email = email
        self.token = token
        self.cloud_id = cloud_id
        self.site_url = site_url.rstrip("/")
        self.base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"

        raw = f"{email}:{token}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("utf-8")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        retry = Retry(
            total=6, connect=6, read=6, status=6, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)

    def test_auth(self):
        response = self.session.get(f"{self.base_url}/rest/api/3/myself", timeout=(15, 60))
        self._raise_for_jira(response, "Authentication test")
        return response.json()

    def get_fields(self):
        response = self.session.get(f"{self.base_url}/rest/api/3/field", timeout=(15, 60))
        self._raise_for_jira(response, "Field discovery")
        return response.json()

    @staticmethod
    def field_map(fields):
        result = {}
        for field in fields:
            name = field.get("name")
            if name:
                result[name.casefold()] = field.get("id")
        return result

    @staticmethod
    def resolve_field(field_map, *names):
        for name in names:
            field_id = field_map.get(name.casefold())
            if field_id:
                return field_id
        return None

    def search_pages(self, jql, requested_fields, page_size=100):
        url = f"{self.base_url}/rest/api/3/search/jql"
        next_page_token = None
        page_number = 1
        total = 0
        while True:
            payload = {"jql": jql.strip(), "fields": requested_fields, "maxResults": page_size}
            if next_page_token:
                payload["nextPageToken"] = next_page_token
            response = self.session.post(url, json=payload, timeout=(15, 180))
            self._raise_for_jira(response, f"JQL search page {page_number}")
            data = response.json()
            issues = data.get("issues", [])
            total += len(issues)
            print(f"Page {page_number}: received {len(issues)} | total {total}")
            yield page_number, issues
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
            page_number += 1

    @staticmethod
    def _raise_for_jira(response, operation):
        if response.ok:
            return
        detail = response.text[:4000]
        raise RuntimeError(f"{operation} failed: HTTP {response.status_code}\n{detail}")


def discover_fields(client):
    fields = client.get_fields()
    fmap = client.field_map(fields)

    origin = client.resolve_field(fmap, "Origin")
    cross_team = client.resolve_field(fmap, "Cross Functional Team")
    seccon = client.resolve_field(fmap, "Security SecCon", "SecCon", "Security Seccon")
    severity = client.resolve_field(fmap, "Severity", "Security Severity", "Custom field (Severity)")
    security_scan_type = client.resolve_field(
        fmap,
        "Security Scan Type",
        "Custom field (Security Scan Type)",
    )
    security_cvss = client.resolve_field(
        fmap,
        "Security CVSS",
        "CVSS",
        "Custom field (Security CVSS)",
    )

    print("Discovered Jira field IDs:")
    print("  Origin:", origin)
    print("  Cross Functional Team:", cross_team)
    print("  Security SecCon:", seccon)
    print("  Severity:", severity)
    print("  Security Scan Type:", security_scan_type)
    print("  Security CVSS:", security_cvss)

    if not origin:
        raise RuntimeError("Required Jira field 'Origin' was not found")
    if not cross_team:
        raise RuntimeError("Required Jira field 'Cross Functional Team' was not found")
    if not severity:
        print("WARNING: Jira field 'Severity' was not found; severity will be blank.")
    if not security_scan_type:
        print("WARNING: Jira field 'Security Scan Type' was not found; security_scan_type will be blank.")
    if not security_cvss:
        print("WARNING: Jira field 'Security CVSS' was not found; security_cvss will be blank.")

    return {
        "origin": origin,
        "cross_team": cross_team,
        "seccon": seccon,
        "severity": severity,
        "security_scan_type": security_scan_type,
        "security_cvss": security_cvss,
    }


def requested_fields(custom_fields):
    fields = [
        "summary", "issuetype", "status", "project", "priority", "resolution",
        "resolutiondate", "assignee", "reporter", "creator", "created", "updated",
        "versions", "fixVersions", "watches", "security",
    ]
    for field_id in (
        custom_fields.get("origin"), custom_fields.get("cross_team"),
        custom_fields.get("seccon"), custom_fields.get("severity"),
        custom_fields.get("security_scan_type"), custom_fields.get("security_cvss"),
    ):
        if field_id and field_id not in fields:
            fields.append(field_id)
    return fields


def issue_to_record(issue, site_url, custom_fields):
    fields = issue.get("fields", {})
    project = fields.get("project") or {}
    issue_type = fields.get("issuetype") or {}
    status = fields.get("status") or {}
    priority = fields.get("priority") or {}
    resolution = fields.get("resolution") or {}
    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    creator = fields.get("creator") or {}
    watches = fields.get("watches") or {}
    security = fields.get("security") or {}

    return {
        "issue_id": int(issue["id"]),
        "issue_key": issue.get("key"),
        "summary": fields.get("summary"),
        "issue_type": issue_type.get("name"),
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("name"),
        "project_key": project.get("key"),
        "project_name": project.get("name"),
        "project_type": project.get("projectTypeKey"),
        "priority": priority.get("name"),
        "resolution": resolution.get("name"),
        "assignee": assignee.get("displayName"),
        "reporter": reporter.get("displayName"),
        "creator": creator.get("displayName"),
        "jira_created_at": fields.get("created"),
        "jira_updated_at": fields.get("updated"),
        "jira_resolution_at": fields.get("resolutiondate"),
        "affects_versions": version_names(fields.get("versions")),
        "fix_versions": version_names(fields.get("fixVersions")),
        "watchers": watches.get("watchCount"),
        "security_level": security.get("name"),
        "origin": jira_value(fields.get(custom_fields.get("origin"))),
        "cross_functional_team": jira_value(fields.get(custom_fields.get("cross_team"))),
        "seccon": jira_value(fields.get(custom_fields.get("seccon"))) if custom_fields.get("seccon") else None,
        "severity": jira_value(fields.get(custom_fields.get("severity"))) if custom_fields.get("severity") else None,
        "security_scan_type": (
            jira_value(fields.get(custom_fields.get("security_scan_type")))
            if custom_fields.get("security_scan_type") else None
        ),
        "security_cvss": (
            jira_number(fields.get(custom_fields.get("security_cvss")))
            if custom_fields.get("security_cvss") else None
        ),
        "issue_url": f"{site_url.rstrip('/')}/browse/{issue.get('key')}",
        "raw_json": json.dumps(issue, ensure_ascii=False),
    }
