from types import SimpleNamespace as NS
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock

from monitor import transition
from snapshot import SelectionError, collect_rows, list_candidates, parse_targets, validate_rows


def workspace(wid, owner="alice", name="gpu-pod", status="running"):
    return NS(id=wid, name=name, status=status, created_by=NS(username=owner))


class SelectionTests(unittest.TestCase):
    def test_404_skips_both_aliases_and_reads_remaining_target(self):
        error = RuntimeError("secret API response")
        error.status = 404
        read = Mock(side_effect=[error, workspace(456, name="other")])
        skipped = []
        result = collect_rows(["123", "alice/gpu-pod", "456"], read,
                              lambda: [workspace(123)], skipped)
        self.assertEqual(skipped, [1, 2])
        self.assertEqual(set(result), {"456"})
        self.assertEqual(read.call_count, 2)

    def test_all_missing_targets_are_valid(self):
        error = RuntimeError("private")
        error.status = 404
        skipped = []
        result = collect_rows(["123", "alice/missing"], Mock(side_effect=error),
                              lambda: [], skipped)
        self.assertEqual(result, {})
        self.assertEqual(set(skipped), {1, 2})
        validate_rows(["123", "alice/missing"], result, skipped)

    def test_invalid_skip_metadata_does_not_hide_missing_results(self):
        for skipped in ([], [0], [2], [1, 1], [True], "1"):
            with self.subTest(skipped=skipped), self.assertRaises(ValueError):
                validate_rows(["123"], {}, skipped)

    def test_unavailable_workspace_reports_positions_without_api_details(self):
        for status in (401, 403):
            error = RuntimeError("secret API response")
            error.status = status
            with self.subTest(status=status), self.assertRaises(SelectionError) as caught:
                collect_rows(["123", "alice/gpu-pod"], Mock(side_effect=error),
                             lambda: [workspace(123)])
            self.assertIn(f"Target 1, 2: workspace read returned HTTP {status}", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("alice", str(caught.exception))

    def test_unexpected_read_error_is_not_misclassified(self):
        error = RuntimeError("unexpected failure")
        with self.assertRaises(RuntimeError) as caught:
            collect_rows(["123"], Mock(side_effect=error), lambda: [])
        self.assertIs(caught.exception, error)

    def test_scheduled_termination_datetime_is_json_serializable(self):
        item = workspace(123)
        item.scheduled_termination_dt = datetime(2026, 9, 13, tzinfo=timezone.utc)
        result = collect_rows(["123"], lambda wid: item, lambda: [])
        self.assertEqual(result["123"]["scheduled_termination_dt"], "2026-09-13T00:00:00+00:00")

    def test_mixed_selectors_and_whitespace(self):
        self.assertEqual(parse_targets("123, alice/gpu-pod,123, bob / cpu-pod "),
                         ["123", "alice/gpu-pod", "bob/cpu-pod"])

    def test_invalid_selectors(self):
        for value in ("", "gpu-pod", "123,", "/pod", "alice/", "a/b/c", "0", "１２３"):
            with self.subTest(value=value), self.assertRaises(SelectionError):
                parse_targets(value)

    def test_numeric_only_does_not_list_other_workspaces(self):
        listing = Mock(side_effect=AssertionError("Unexpected list request"))
        rows = collect_rows(["123"], lambda wid: workspace(wid), listing)
        self.assertEqual(set(rows), {"123"})
        listing.assert_not_called()

    def test_same_workspace_by_id_and_name_is_read_once(self):
        read = Mock(return_value=workspace(123))
        rows = collect_rows(["123", "alice/gpu-pod"], read, lambda: [workspace(123)])
        self.assertEqual(set(rows), {"123"})
        read.assert_called_once_with(123)

    def test_owner_disambiguates_duplicate_names(self):
        items = [workspace(123), workspace(456, owner="bob")]
        rows = collect_rows(["bob/gpu-pod"], lambda wid: items[1], lambda: items)
        self.assertEqual(set(rows), {"456"})

    def test_ambiguous_names_fail_before_detail_reads(self):
        for items in ([workspace(123), workspace(456)],):
            with self.subTest(items=items):
                read = Mock()
                with self.assertRaises(SelectionError):
                    collect_rows(["alice/gpu-pod"], read, lambda: items)
                read.assert_not_called()

    def test_rename_during_lookup_is_rejected(self):
        with self.assertRaises(SelectionError):
            collect_rows(["alice/gpu-pod"], lambda wid: workspace(wid, name="renamed"),
                         lambda: [workspace(123)])

    def test_selector_change_retains_state_by_id(self):
        initial = collect_rows(["123"], lambda wid: workspace(wid), lambda: [])
        state = transition({}, initial)
        stopped = workspace(123, status="stopped")
        final = collect_rows(["alice/gpu-pod", "123"], lambda wid: stopped, lambda: [stopped])
        self.assertEqual(len(transition(state, final)["pending"]), 1)

    def test_incomplete_or_extra_snapshot_is_rejected(self):
        rows = {"123": {"owner": "alice", "name": "gpu-pod", "status": "running"}}
        with self.assertRaises(SelectionError):
            validate_rows(["123", "bob/gpu-pod"], rows)
        with self.assertRaises(ValueError):
            validate_rows(["123"], dict(rows, **{"456": rows["123"]}))

    def test_pagination_combines_own_and_others_without_duplicates(self):
        def page(**kwargs):
            mine, offset = kwargs["mine"], kwargs["offset"]
            items = [workspace(123), workspace(456)] if mine else [workspace(456), workspace(789)]
            return NS(results=items[offset:offset+1], page_info=NS(total_count=2))
        api = NS(workspace_list_api=Mock(side_effect=page))
        self.assertEqual({w.id for w in list_candidates(api, "example")}, {123, 456, 789})
        self.assertEqual(api.workspace_list_api.call_count, 4)

    def test_truncated_pagination_is_rejected(self):
        api = NS(workspace_list_api=Mock(return_value=NS(results=[], page_info=NS(total_count=2))))
        with self.assertRaises(ValueError):
            list_candidates(api, "example")


if __name__ == "__main__":
    unittest.main()
