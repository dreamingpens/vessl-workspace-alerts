import unittest
from unittest.mock import Mock, patch

from monitor import main, snapshot
from registrations import LABEL, approved_target, load_targets, pages


def comment(body, login="maintainer", number=1, edited=False):
    return {"id": number, "user": {"login": login}, "body": body,
            "created_at": "2026-09-09T00:00:00Z",
            "updated_at": "2026-09-09T01:00:00Z" if edited else "2026-09-09T00:00:00Z"}


def issue(number=1, **kwargs):
    return {"number": number, "state": "open", "labels": [{"name": LABEL}], **kwargs}


class RegistrationTests(unittest.TestCase):
    def test_only_owner_can_approve_even_if_comment_claims_owner(self):
        forged = comment("/watch attacker/pod", login="outsider")
        forged["author_association"] = "OWNER"
        self.assertIsNone(approved_target([forged], "maintainer"))
        self.assertEqual(approved_target([comment("/watch alice/pod", login="Maintainer")],
                                         "maintainer"), "alice/pod")

    def test_latest_owner_command_replaces_or_revokes(self):
        first = comment("/watch 123")
        second = comment("/watch bob/pod", number=2)
        self.assertEqual(approved_target([second, first], "maintainer"), "bob/pod")
        self.assertIsNone(approved_target([first, second, comment("/unwatch", number=3)],
                                         "maintainer"))

    def test_non_owner_revocation_and_normal_comments_are_ignored(self):
        comments = [comment("/watch 123"), comment("Thanks!", number=2),
                    comment("/unwatch", login="outsider", number=3)]
        self.assertEqual(approved_target(comments, "maintainer"), "123")

    def test_edited_approval_cannot_grant_and_requires_new_command(self):
        comments = [comment("/watch 123"), comment("/watch 456", number=2, edited=True)]
        self.assertIsNone(approved_target(comments, "maintainer"))
        comments.append(comment("/watch 789", number=3))
        self.assertEqual(approved_target(comments, "maintainer"), "789")

    def test_malformed_or_multiline_latest_command_fails_closed(self):
        for body in ["/watch", "/watch 0", "/watch bare-name", "/watch 1,2",
                     "/watch a/pod\n/watch b/pod", "/watch a/" + "x" * 257,
                     "/watch a/pod\x00", "/unwatch extra"]:
            with self.subTest(body=body):
                self.assertIsNone(approved_target([comment("/watch 123"),
                                                  comment(body, number=2)], "maintainer"))

    def test_quoted_command_is_not_approval(self):
        for body in ["> /watch 123", "```\n/watch 123\n```", "Please /watch 123"]:
            self.assertIsNone(approved_target([comment(body)], "maintainer"))

    def test_merge_manual_and_approved_targets_ignores_edited_issue_body(self):
        request = Mock(side_effect=[
            [issue(body="/watch attacker/pod"), issue(2), issue(3)],
            [comment("/watch alice/pod")], [comment("/watch 123")],
            [comment("/watch intruder/pod", login="outsider")],
        ])
        self.assertEqual(load_targets(request, "maintainer/repo", "123,alice/pod,456"),
                         ["123", "alice/pod", "456"])

    def test_closed_unlabeled_and_pull_requests_do_not_register(self):
        request = Mock(return_value=[issue(state="closed"), issue(2, labels=[]),
                                     issue(3, pull_request={})])
        self.assertEqual(load_targets(request, "maintainer/repo", ""), [])
        request.assert_called_once()

    def test_forms_can_be_only_source(self):
        request = Mock(side_effect=[[issue()], [comment("/watch alice/pod")]])
        self.assertEqual(load_targets(request, "maintainer/repo", ""), ["alice/pod"])

    def test_pagination_does_not_drop_late_approval(self):
        request = Mock(side_effect=[[comment("hello", number=i) for i in range(100)],
                                    [comment("/watch 123", number=101)]])
        self.assertEqual(approved_target(pages(request, "/comments"), "maintainer"), "123")
        self.assertIn("page=2", request.call_args.args[1])

    def test_github_failure_is_not_interpreted_as_empty_registrations(self):
        with self.assertRaises(TimeoutError):
            load_targets(Mock(side_effect=TimeoutError), "maintainer/repo", "123")

    def test_empty_selection_skips_vessl_lookup(self):
        with patch("monitor.subprocess.run") as run:
            self.assertEqual(snapshot([]), {})
        run.assert_not_called()

    def test_dry_run_uses_merged_targets_without_writes_or_slack(self):
        env = {key: "configured" for key in ["GH_TOKEN", "GITHUB_REPOSITORY",
               "STATE_ENCRYPTION_KEY", "VESSL_ACCESS_TOKEN", "VESSL_DEFAULT_ORGANIZATION"]}
        env["VESSL_WORKSPACE_IDS"] = "123"
        with patch.dict("os.environ", env, clear=True), \
             patch("sys.argv", ["monitor.py", "--dry-run"]), \
             patch("monitor.GitHubState") as constructor, \
             patch("monitor.snapshot", return_value={}) as fetch, \
             patch("monitor.send_slack") as slack:
            store = constructor.return_value
            store.repo = "maintainer/repo"
            store.load.return_value = {"version": 1, "workspaces": {}, "pending": []}
            store.request.side_effect = [[issue()], [comment("/watch alice/pod")]]
            main()
            fetch.assert_called_once_with(["123", "alice/pod"])
            store.save.assert_not_called()
            slack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
