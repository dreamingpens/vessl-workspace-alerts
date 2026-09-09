"""Read owner-approved issue registrations; never execute issue contents."""
import re
from urllib.parse import urlencode

from snapshot import SelectionError, parse_targets


LABEL = "workspace-registration"


def pages(request, path, **query):
    for page in range(1, 101):
        items = request("GET", path + "?" + urlencode({**query, "per_page": 100, "page": page}))
        if not isinstance(items, list):
            raise ValueError("Invalid GitHub issue response")
        yield from items
        if len(items) < 100:
            return
    raise RuntimeError("Registration pagination exceeded limit")


def approved_target(comments, owner):
    """Only the owner's latest command counts, independently of the issue body.

    Commands must be separate, unedited comments. This also prevents someone
    with issue moderation rights from editing an old owner comment into a grant.
    """
    target = None
    for comment in sorted(comments, key=lambda item: item["id"]):
        if comment.get("user", {}).get("login", "").casefold() != owner.casefold():
            continue
        body = (comment.get("body") or "").strip()
        if not re.match(r"^/(?:watch|unwatch)(?:\s|$)", body):
            continue
        target = None  # A malformed/edited latest command fails closed.
        if not comment.get("created_at") or comment["created_at"] != comment.get("updated_at"):
            continue
        if body == "/unwatch":
            continue
        match = re.fullmatch(r"/watch ([^\r\n]+)", body)
        if not match:
            continue
        value = match[1].strip()
        if len(value) > 256 or any(ord(c) < 32 for c in value):
            continue
        try:
            targets = parse_targets(value)
        except SelectionError:
            continue
        if "," not in value and len(targets) == 1:
            target = targets[0]
    return target


def load_targets(request, repo, base_value):
    targets = parse_targets(base_value) if base_value.strip() else []
    owner = repo.split("/", 1)[0]
    issues = pages(request, f"/repos/{repo}/issues", state="open", labels=LABEL,
                   sort="created", direction="asc")
    approved = 0
    for issue in issues:
        if "pull_request" in issue or issue.get("state") != "open":
            continue
        if LABEL not in {label["name"] for label in issue.get("labels", [])}:
            continue
        number = int(issue["number"])
        comments = pages(request, f"/repos/{repo}/issues/{number}/comments")
        target = approved_target(comments, owner)
        if target:
            approved += 1
            if target not in targets:
                targets.append(target)
        else:
            print(f"Registration issue #{number}: awaiting a valid owner /watch command.")
    print(f"Registration check completed: {approved} approved open issue(s).")
    return targets
