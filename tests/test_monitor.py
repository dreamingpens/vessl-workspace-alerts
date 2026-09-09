import copy
import json
import unittest
from unittest.mock import Mock, patch

from monitor import main, post_slack, process, send_slack, transition


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

    def test_removed_workspace_is_not_armed_when_readded(self):
        state = transition({}, rows("running"))
        state = transition(state, {})
        pending = transition(state, rows("stopped"))["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reason"], "initial_stopped")


if __name__ == "__main__":
    unittest.main()
