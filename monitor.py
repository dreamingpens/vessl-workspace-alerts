"""Hourly stop notifications, with encrypted durable state and no paid services."""
import argparse
import base64
import copy
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

from cryptography.fernet import Fernet
from snapshot import SelectionError, parse_targets, validate_rows


STATE_PATH = ".monitor/state.enc"


def transition(state, rows):
    """Keep the running latch through intermediate states; queue each stop once."""
    result = copy.deepcopy(state)
    watches = result.setdefault("workspaces", {})
    pending = result.setdefault("pending", [])
    for wid, row in rows.items():
        watch = watches.setdefault(wid, {"armed": False})
        if row["status"] == "running":
            watch["armed"] = True
        elif row["status"] == "stopped" and watch["armed"]:
            pending.append({
                "event_id": str(uuid.uuid4()), "id": wid, "name": row["name"],
                "owner": row.get("owner", ""),
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })
            watch["armed"] = False
    # Removed targets cannot inherit an old running latch if re-added later.
    result["workspaces"] = {wid: watches[wid] for wid in rows}
    result["version"] = 1
    return result


class GitHubState:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.token = os.environ["GH_TOKEN"]
        self.cipher = Fernet(os.environ["STATE_ENCRYPTION_KEY"].encode())
        self.sha = None

    def request(self, method, path, payload=None):
        req = Request(
            "https://api.github.com" + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urlopen(req, timeout=25) as response:
            return json.load(response)

    def load(self):
        repo = self.request("GET", f"/repos/{self.repo}")
        if repo["private"]:
            raise RuntimeError("This monitor only runs in a public repository to avoid runner charges")
        self.branch = repo["default_branch"]
        try:
            item = self.request("GET", f"/repos/{self.repo}/contents/{STATE_PATH}?ref={self.branch}")
        except HTTPError as error:
            if error.code != 404:
                raise
            return {"version": 1, "workspaces": {}, "pending": []}
        self.sha = item["sha"]
        state = json.loads(self.cipher.decrypt(base64.b64decode(item["content"])))
        if state.get("version") != 1 or not isinstance(state.get("pending"), list):
            raise ValueError("Invalid encrypted state")
        return state

    def save(self, state):
        encrypted = self.cipher.encrypt(json.dumps(state, ensure_ascii=False).encode())
        payload = {"message": "Update encrypted monitor state", "branch": self.branch,
                   "content": base64.b64encode(encrypted).decode()}
        if self.sha:
            payload["sha"] = self.sha
        item = self.request("PUT", f"/repos/{self.repo}/contents/{STATE_PATH}", payload)
        self.sha = item["content"]["sha"]


def snapshot():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("snapshot.py"))],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        if result.returncode == 2 and result.stderr.startswith("Selection error: "):
            raise SelectionError(result.stderr.strip())
        raise RuntimeError("VESSL lookup failed")
    rows = json.loads(result.stdout)
    validate_rows(parse_targets(os.environ["VESSL_WORKSPACE_IDS"]), rows)
    return rows


def send_slack(events):
    webhook = os.environ["SLACK_WEBHOOK_URL"]
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.hostname != "hooks.slack.com" or not parsed.path.startswith("/services/"):
        raise ValueError("Invalid Slack webhook")
    lines = ["🔴 VESSL 워크스페이스 중지 감지"]
    for event in events:
        lines.append(f"• {html.escape(event['name'])} (ID: {event['id']}) — stopped\n"
                     f"  소유자: {html.escape(event.get('owner', ''))}\n"
                     f"  감지 시각(UTC): {event['detected_at']}")
    lines.append("1시간 간격으로 확인합니다. 표시 시각은 실제 중지 시각이 아닌 감지 시각입니다.")
    req = Request(webhook, data=json.dumps({"text": "\n".join(lines)}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=25) as response:
        if response.status != 200 or response.read().strip() != b"ok":
            raise RuntimeError("Slack did not acknowledge the message")


def process(store, fetch, send, dry_run=False):
    state = store.load()
    rows = fetch()  # Lookup failures never modify the saved state.
    state = transition(state, rows)
    if dry_run:
        print("Dry run successful: authentication, state decryption and workspace reads verified. No writes or messages.")
        return
    # Save the outbox before delivery; a failed send is retried on the next run.
    store.save(state)
    if state["pending"]:
        send(state["pending"])
        state["pending"] = []
        store.save(state)
    print("Monitor check completed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    required = ["GH_TOKEN", "GITHUB_REPOSITORY", "STATE_ENCRYPTION_KEY",
                "VESSL_ACCESS_TOKEN", "VESSL_DEFAULT_ORGANIZATION", "VESSL_WORKSPACE_IDS"]
    if not args.dry_run:
        required.append("SLACK_WEBHOOK_URL")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        sys.exit("Setup incomplete: configure repository settings (VESSL_WORKSPACE_IDS as a Variable; credentials as Secrets): " + ", ".join(missing))
    process(GitHubState(), snapshot, send_slack, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except SelectionError as error:
        sys.exit(str(error))
    except Exception:
        # HTTP exceptions can contain the secret webhook URL or API response.
        sys.exit("Monitor failed. Check VESSL/Slack credentials and repository write access. Saved alerts retry next run.")
