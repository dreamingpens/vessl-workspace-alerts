import copy
from datetime import datetime, timedelta, timezone
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from monitor import main, post_slack, process, send_slack, start_workspace, transition


def rows(status):
    return {"123": {"name": "sample-workspace", "status": status}}


class Store:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.saves = 0

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.saves += 1


class MonitorTests(unittest.TestCase):
    def test_low_time_boundary_deduplication_extension_and_restart(self):
        now = datetime(2026, 9, 13, tzinfo=timezone.utc)
        current = rows("running")
        def check(state, seconds):
            current["123"]["scheduled_termination_dt"] = (now + timedelta(seconds=seconds)).isoformat()
            return transition(state, current, now=now)
        state = check({}, 18000)
        self.assertEqual(state["pending"], [])
        state = check(state, 17999)
        self.assertEqual(state["pending"][0]["reason"], "low_remaining_time")
        state["pending"].clear()
        self.assertEqual(check(state, 100)["pending"], [])
        state = check(state, 20000)
        state = check(state, 12000)
        self.assertEqual(len(state["pending"]), 1)
        state = transition(state, rows("stopped"), now=now)
        state["pending"].clear()
        self.assertEqual(len(check(state, 10000)["pending"]), 1)

    def test_missing_invalid_or_nonrunning_deadline_does_not_warn(self):
        for status, deadline in (("running", None), ("running", "bad"),
                                 ("running", "2026-09-13T00:00:00"),
                                 ("pending", "2026-09-13T00:00:00Z")):
            current = rows(status)
            current["123"]["scheduled_termination_dt"] = deadline
            self.assertEqual(transition({}, current)["pending"], [])

    def test_low_time_delivery_retry_and_message_without_start(self):
        current = rows("running")
        current["123"]["scheduled_termination_dt"] = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        store = Store({})
        with self.assertRaises(TimeoutError):
            process(store, lambda: current, Mock(side_effect=TimeoutError))
        pending = copy.deepcopy(store.state["pending"])
        with patch("monitor.post_slack") as post:
            process(store, lambda: current, send_slack)
        self.assertIn("남은 시간 5시간 미만", post.call_args.args[0])
        self.assertIn("종료 예정 시각", post.call_args.args[0])
        self.assertNotIn("자동 시작", post.call_args.args[0])
        self.assertEqual(len(pending), 1)
        self.assertEqual(store.state["pending"], [])
        self.start.assert_not_called()

    def setUp(self):
        # Existing notification tests must never invoke a real workspace start.
        patcher = patch("monitor.start_workspace")
        self.start = patcher.start()
        self.addCleanup(patcher.stop)

    def test_stopped_targets_start_on_every_check_even_without_new_alert(self):
        store = Store(transition({}, rows("stopped")))
        store.state["pending"].clear()
        send = Mock()
        process(store, lambda: rows("stopped"), send)
        process(store, lambda: rows("stopped"), send)
        self.assertEqual(self.start.call_count, 2)
        self.start.assert_called_with("123")
        send.assert_not_called()

    def test_only_currently_stopped_targets_start(self):
        for status in ("running", "stopping", "pending", "queued", "initializing", "error", "unknown"):
            with self.subTest(status=status):
                store = Store(transition({}, rows("stopped")))
                process(store, lambda: rows(status), Mock())
        process(Store({}), lambda: {}, Mock())
        self.start.assert_not_called()

    def test_start_failure_does_not_block_other_targets_or_alert_delivery(self):
        current = {**rows("stopped"), "456": {"name": "second", "status": "stopped"}}
        self.start.side_effect = [RuntimeError("private API response"), None]
        store, send = Store({}), Mock()
        with self.assertRaisesRegex(RuntimeError, "stopped targets retry"):
            process(store, lambda: current, send)
        self.assertEqual([call.args[0] for call in self.start.call_args_list], ["123", "456"])
        events = send.call_args.args[0]
        self.assertEqual([event["start_result"] for event in events], ["failed", "requested"])
        self.assertEqual(store.state["pending"], [])
        self.start.side_effect = None
        process(store, lambda: current, send)
        self.assertEqual(self.start.call_count, 4)

    def test_start_precedes_slack_and_state_write_failures(self):
        for failure in ("save", "send"):
            with self.subTest(failure=failure):
                store, send = Store({}), Mock()
                if failure == "save":
                    store.save = Mock(side_effect=TimeoutError)
                else:
                    send.side_effect = TimeoutError
                self.start.reset_mock()
                with self.assertRaises(TimeoutError):
                    process(store, lambda: rows("stopped"), send)
                self.start.assert_called_once_with("123")

    def test_old_pending_alert_does_not_inherit_new_start_result(self):
        store = Store(transition({}, rows("stopped")))
        pending = copy.deepcopy(store.state["pending"])
        send = Mock()
        process(store, lambda: rows("stopped"), send)
        send.assert_called_once_with(pending)

    def test_slack_reports_request_outcome_without_claiming_running(self):
        event = transition({}, rows("stopped"))["pending"][0]
        with patch("monitor.post_slack") as post:
            event["start_result"] = "requested"
            send_slack([event])
            self.assertIn("CLI 시작 요청 성공", post.call_args.args[0])
            self.assertIn("실행 완료 여부는 다음", post.call_args.args[0])
            event["start_result"] = "failed"
            send_slack([event])
            self.assertIn("CLI 시작 요청 실패", post.call_args.args[0])

    def test_slack_test_only_sends_one_message_without_state_access(self):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "configured"}, clear=True), \
             patch("sys.argv", ["monitor.py", "--test-slack"]), \
             patch("monitor.post_slack") as post, patch("monitor.GitHubState") as store, \
             patch("monitor.snapshot") as fetch:
            main()
        post.assert_called_once()
        self.assertIn("테스트", post.call_args.args[0])
        store.assert_not_called()
        fetch.assert_not_called()
        self.start.assert_not_called()

    def test_slack_http_payload_and_acknowledgment(self):
        response = Mock(status=200)
        response.read.return_value = b"ok"
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/example"}), \
             patch("monitor.urlopen") as request:
            request.return_value.__enter__.return_value = response
            post_slack("test message")
            self.assertEqual(json.loads(request.call_args.args[0].data), {"text": "test message"})
            response.read.return_value = b"invalid_payload"
            with self.assertRaises(RuntimeError):
                post_slack("test message")

    def test_slack_test_requires_webhook(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch("sys.argv", ["monitor.py", "--test-slack"]), \
             patch("monitor.post_slack") as post:
            with self.assertRaisesRegex(SystemExit, "SLACK_WEBHOOK_URL"):
                main()
        post.assert_not_called()

    def test_initial_stopped_alerts_once_and_can_alert_after_restart(self):
        store = Store({})
        send = Mock()
        process(store, lambda: rows("stopped"), send)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0][0]["reason"], "initial_stopped")
        process(store, lambda: rows("stopped"), send)
        send.assert_called_once()
        process(store, lambda: rows("running"), send)
        process(store, lambda: rows("stopped"), send)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[0][0]["reason"], "stopped_transition")

    def test_initial_stopped_delivery_failure_preserves_same_event_for_retry(self):
        store = Store({})
        with self.assertRaises(TimeoutError):
            process(store, lambda: rows("stopped"), Mock(side_effect=TimeoutError))
        pending = copy.deepcopy(store.state["pending"])
        self.assertEqual(len(pending), 1)
        send = Mock()
        process(store, lambda: rows("stopped"), send)
        send.assert_called_once_with(pending)
        self.assertEqual(store.state["pending"], [])

    def test_existing_stopped_baseline_is_not_retroactively_alerted(self):
        state = {"version": 1, "workspaces": {"123": {"armed": False}}, "pending": []}
        self.assertEqual(transition(state, rows("stopped")), state)

    def test_first_observation_message_is_distinct_from_transition(self):
        event = transition({}, rows("stopped"))["pending"][0]
        with patch("monitor.post_slack") as post:
            send_slack([event])
            self.assertIn("등록 후 첫 확인에서 이미 중지", post.call_args.args[0])
            self.assertNotIn("실행 중이었던", post.call_args.args[0])
            # Persisted events from the older version have no reason field.
            del event["reason"]
            send_slack([event])
            self.assertIn("실행 중이었던", post.call_args.args[0])

    def test_initial_stopped_dry_run_does_not_consume_first_alert(self):
        store = Store({})
        send = Mock()
        process(store, lambda: rows("stopped"), send, dry_run=True)
        self.assertEqual(store.state, {})
        self.assertEqual(store.saves, 0)
        send.assert_not_called()
        process(store, lambda: rows("stopped"), send)
        send.assert_called_once()

    def test_intermediate_states_preserve_running_latch(self):
        state = transition({}, rows("running"))
        state = transition(state, rows("stopping"))
        state = transition(state, rows("stopped"))
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual(len(transition(state, rows("stopped"))["pending"]), 1)

    def test_restarted_workspace_can_alert_again(self):
        state = transition({}, rows("running"))
        state = transition(state, rows("stopped"))
        state["pending"].clear()
        state = transition(state, rows("running"))
        self.assertEqual(len(transition(state, rows("stopped"))["pending"]), 1)

    def test_lookup_error_does_not_modify_state_or_send(self):
        store = Store(transition({}, rows("running")))
        send = Mock()
        with self.assertRaises(TimeoutError):
            process(store, Mock(side_effect=TimeoutError), send)
        self.assertEqual(store.saves, 0)
        send.assert_not_called()
        self.start.assert_not_called()

    def test_failed_slack_delivery_retries_even_after_restart(self):
        store = Store(transition({}, rows("running")))
        with self.assertRaises(TimeoutError):
            process(store, lambda: rows("stopped"), Mock(side_effect=TimeoutError))
        self.assertEqual(len(store.state["pending"]), 1)
        send = Mock()
        process(store, lambda: rows("running"), send)
        send.assert_called_once()
        self.assertEqual(store.state["pending"], [])
        self.assertTrue(store.state["workspaces"]["123"]["armed"])

    def test_dry_run_has_no_side_effects(self):
        store = Store(transition({}, rows("running")))
        send = Mock()
        process(store, lambda: rows("stopped"), send, dry_run=True)
        self.assertEqual(store.saves, 0)
        send.assert_not_called()
        self.start.assert_not_called()

    def test_removed_workspace_is_not_armed_when_readded(self):
        state = transition({}, rows("running"))
        state = transition(state, {})
        pending = transition(state, rows("stopped"))["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reason"], "initial_stopped")


class StartWorkspaceTests(unittest.TestCase):
    def test_cli_uses_explicit_id_and_noninteractive_private_output(self):
        with patch("monitor.subprocess.run", return_value=Mock(returncode=0)) as run:
            start_workspace("123")
        self.assertEqual(run.call_args.args[0], ["vessl", "workspace", "start", "123"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertEqual(run.call_args.kwargs["env"]["VESSL_SAVE_CONFIG"], "false")

    def test_cli_failures_are_sanitized(self):
        for error in (None, FileNotFoundError("private"),
                      subprocess.TimeoutExpired("private", 60, output="private")):
            with self.subTest(error=error), patch("monitor.subprocess.run", side_effect=error,
                                                return_value=Mock(returncode=1, stderr="private")):
                with self.assertRaises(RuntimeError) as raised:
                    start_workspace("123")
                self.assertNotIn("private", str(raised.exception))

    def test_invalid_id_never_invokes_cli(self):
        with patch("monitor.subprocess.run") as run:
            for wid in ("", "--help", "123; echo secret", "alice/gpu", "0"):
                with self.assertRaises(ValueError):
                    start_workspace(wid)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
