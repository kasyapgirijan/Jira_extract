import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from jira_core import JiraClient
import jira_postgres_sync as sync


class OriginSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value.__enter__.return_value
        self.cur.fetchall.return_value = [(1,), (2,)]
        self.client = MagicMock()
        self.fields = {"origin": "customfield_1"}
        self.client.search_pages.return_value = [(1, [self.issue(1, None), self.issue(2, None)])]

    def issue(self, ident, origin):
        return {"id": str(ident), "fields": {"customfield_1": origin}}

    def deletes(self):
        return [c for c in self.cur.execute.call_args_list if c.args[0].startswith("DELETE")]

    def test_corrected_origin_removed_but_security_ticket_kept(self):
        self.client.get_issue.side_effect = [
            self.issue(1, {"value": "Functional Testing"}),
            self.issue(2, {"value": "Security Testing"}),
        ]
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields), 1)
        self.assertEqual(self.deletes()[0].args[1], ([1],))
        self.conn.commit.assert_not_called()

    def test_cleared_origin_removed(self):
        self.client.get_issue.side_effect = [self.issue(1, None), self.issue(2, "")]
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields), 2)

    def test_security_origin_preserved(self):
        self.client.get_issue.side_effect = [
            self.issue(1, "Security Testing"), self.issue(2, "security testing"),
        ]
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields), 0)
        self.assertFalse(self.deletes())

    def test_failure_after_candidate_does_not_delete_anything(self):
        self.client.get_issue.side_effect = [self.issue(1, None), RuntimeError("HTTP 404")]
        with self.assertRaises(RuntimeError):
            sync.reconcile_origins(self.conn, self.client, self.fields)
        self.assertFalse(self.deletes())

    def test_missing_field_or_wrong_id_aborts_cleanup(self):
        for invalid in ({"id": "2", "fields": {}}, self.issue(99, None)):
            with self.subTest(invalid=invalid):
                self.client.get_issue.side_effect = [self.issue(1, None), invalid]
                with self.assertRaises(RuntimeError):
                    sync.reconcile_origins(self.conn, self.client, self.fields)
                self.assertFalse(self.deletes())

    def test_unknown_origin_shapes_abort_cleanup(self):
        for origin in ({"id": "123"}, [], 42, {"value": None}):
            with self.subTest(origin=origin):
                self.client.get_issue.side_effect = [self.issue(1, None), self.issue(2, origin)]
                with self.assertRaises(RuntimeError):
                    sync.reconcile_origins(self.conn, self.client, self.fields)
                self.assertFalse(self.deletes())

    def test_empty_database_needs_no_jira_lookup(self):
        self.cur.fetchall.return_value = []
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields), 0)
        self.client.get_issue.assert_not_called()

    def test_http_errors_are_not_origin_corrections(self):
        client = JiraClient("email", "token", "cloud", "https://example.atlassian.net")
        client.session = MagicMock()
        for status in (403, 404, 429, 500):
            response = client.session.get.return_value
            response.ok = False
            response.status_code = status
            response.text = "Unavailable"
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                client.get_issue(1, ["customfield_1"])

    def run_main(self, full=False, cleanup_error=None):
        cfg = {"email": "e", "token": "t", "cloud_id": "c", "site_url": "https://example",
               "sync": {"sync_name": "test", "overlap_minutes": 5}}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        client = MagicMock()
        client.test_auth.return_value = {"timeZone": "Asia/Kolkata"}
        client.search_pages.return_value = [(1, [])]
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["sync"] + (["--full"] if full else [])))
            mocks = {}
            for name, value in {
                "load_config": cfg, "JiraClient": client, "discover_fields": self.fields,
                "requested_fields": [], "load_jql": 'origin = "Security Testing"',
                "db_connect": conn, "initialize_database": None, "table_row_count": 2,
                "get_last_sync": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "reconcile_origins": 2, "save_success": None, "save_failure": None,
            }.items():
                mocks[name] = stack.enter_context(patch.object(sync, name, return_value=value))
            if cleanup_error:
                mocks["reconcile_origins"].side_effect = cleanup_error
                with self.assertRaises(RuntimeError):
                    sync.main()
                conn.rollback.assert_called_once()
                mocks["save_failure"].assert_called_once()
                mocks["save_success"].assert_not_called()
            else:
                sync.main()
                mocks["reconcile_origins"].assert_called_once()
                since = mocks["reconcile_origins"].call_args.args[3]
                if full:
                    self.assertIsNone(since)
                else:
                    self.assertEqual(since.strftime("%Y-%m-%d %H:%M %z"),
                                     "2026-01-01 05:25 +0530")
                mocks["save_success"].assert_called_once()

    def test_incremental_runs_cleanup_even_with_no_business_search_results(self):
        self.run_main()

    def test_incremental_only_checks_updated_ids_without_origin_filter(self):
        self.client.search_pages.return_value = [(1, [self.issue(2, "Functional Testing")])]
        self.client.get_issue.return_value = self.issue(2, "Functional Testing")
        since = datetime(2026, 1, 1, 10, 55, tzinfo=timezone.utc)
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields, since), 1)
        self.client.get_issue.assert_called_once_with(2, ["customfield_1"])
        jql = self.client.search_pages.call_args.args[0]
        self.assertIn('updated >= "2026-01-01 10:55"', jql)
        self.assertIn('id IN (1,2)', jql)
        self.assertNotIn('origin', jql.lower())
        self.assertEqual(self.deletes()[0].args[1], ([2],))

    def test_no_updates_means_no_issue_lookups_or_deletes(self):
        self.client.search_pages.return_value = [(1, [])]
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields, since), 0)
        self.client.get_issue.assert_not_called()
        self.assertFalse(self.deletes())

    def test_incremental_batches_ids_and_reads_all_pages(self):
        self.cur.fetchall.return_value = [(i,) for i in range(1, 102)]
        self.client.search_pages.side_effect = [
            [(1, [self.issue(1, None)]), (2, [self.issue(100, None)])],
            [(1, [self.issue(101, None)])],
        ]
        self.client.get_issue.side_effect = lambda i, fields: self.issue(i, None)
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields, since), 3)
        self.assertEqual(self.client.search_pages.call_count, 2)
        self.assertEqual(self.deletes()[0].args[1], ([1, 100, 101],))

    def test_failed_later_search_page_does_not_delete(self):
        def pages(*args):
            yield 1, [self.issue(1, None)]
            raise RuntimeError("HTTP 500")
        self.client.search_pages.side_effect = pages
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(RuntimeError):
            sync.reconcile_origins(self.conn, self.client, self.fields, since)
        self.assertFalse(self.deletes())
        self.client.get_issue.assert_not_called()

    def test_empty_full_search_still_reconciles(self):
        self.run_main(full=True)

    def test_failed_cleanup_does_not_advance_checkpoint(self):
        self.run_main(cleanup_error=RuntimeError("Unavailable"))

    def test_full_12548_security_tickets_need_no_individual_requests(self):
        self.cur.fetchall.return_value = [(i,) for i in range(1, 12549)]
        self.client.search_pages.side_effect = [
            [(1, [self.issue(i, {"value": "Security Testing"})
                  for i in range(start, min(start + 100, 12549))])]
            for start in range(1, 12549, 100)
        ]
        with patch("builtins.print"):
            self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields), 0)
        self.assertEqual(self.client.search_pages.call_count, 126)
        self.client.get_issue.assert_not_called()
        self.assertFalse(self.deletes())

    def test_search_correction_reversed_before_confirmation_is_kept(self):
        self.client.get_issue.side_effect = lambda i, fields: self.issue(i, "Security Testing")
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields), 0)
        self.assertFalse(self.deletes())

    def test_missing_full_search_ticket_is_confirmed_before_cleanup(self):
        self.client.search_pages.return_value = [(1, [self.issue(1, "Security Testing")])]
        self.client.get_issue.return_value = self.issue(2, "Security Testing")
        self.assertEqual(sync.reconcile_origins(self.conn, self.client, self.fields), 0)
        self.client.get_issue.assert_called_once_with(2, ["customfield_1"])
        self.assertFalse(self.deletes())

    def test_unknown_search_origin_aborts_without_deletion(self):
        self.client.search_pages.return_value = [(1, [{"id": "1", "fields": {}}])]
        with self.assertRaises(RuntimeError):
            sync.reconcile_origins(self.conn, self.client, self.fields)
        self.assertFalse(self.deletes())


if __name__ == "__main__":
    unittest.main()
