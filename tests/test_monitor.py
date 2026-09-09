import copy
import unittest
from unittest.mock import Mock

from monitor import process, transition


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
    def test_initial_stopped_is_baseline(self):
        self.assertEqual(transition({}, rows("stopped"))["pending"], [])

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
        self.assertEqual(transition(state, rows("stopped"))["pending"], [])


if __name__ == "__main__":
    unittest.main()
