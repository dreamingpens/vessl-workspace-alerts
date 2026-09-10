"""Hourly stop notifications and CLI restart requests, with encrypted state."""
import argparse
import base64
import copy
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

from cryptography.fernet import Fernet
from snapshot import SelectionError, parse_targets, validate_rows
from registrations import load_targets


STATE_PATH = ".monitor/state.enc"


def transition(state, rows):
    """Queue first-observed stops and later running-to-stopped transitions once."""
    result = copy.deepcopy(state)
    watches = result.setdefault("workspaces", {})
    pending = result.setdefault("pending", [])
    for wid, row in rows.items():
        first_observation = wid not in watches
        watch = watches.setdefault(wid, {"armed": False})
        if row["status"] == "running":
            watch["armed"] = True
        elif row["status"] == "stopped" and (first_observation or watch["armed"]):
            pending.append({
                "event_id": str(uuid.uuid4()), "id": wid, "name": row["name"],
                "owner": row.get("owner", ""),
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "reason": "initial_stopped" if first_observation else "stopped_transition",
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


def snapshot(targets=None):
    if targets is None:
        targets = parse_targets(os.environ["VESSL_WORKSPACE_IDS"])
    if not targets:
        return {}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("snapshot.py"))],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        env={**os.environ, "VESSL_WORKSPACE_IDS": ",".join(targets)},
    )
    if result.returncode:
        if result.returncode == 2 and result.stderr.startswith("Selection error: "):
            raise SelectionError(result.stderr.strip())
        raise RuntimeError("VESSL lookup failed")
    rows = json.loads(result.stdout)
    validate_rows(targets, rows)
    return rows


def post_slack(text):
    webhook = os.environ["SLACK_WEBHOOK_URL"]
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.hostname != "hooks.slack.com" or not parsed.path.startswith("/services/"):
        raise ValueError("Invalid Slack webhook")
    req = Request(webhook, data=json.dumps({"text": text}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=25) as response:
        if response.status != 200 or response.read().strip() != b"ok":
            raise RuntimeError("Slack did not acknowledge the message")


def start_workspace(wid):
    """Request a start through the CLI without exposing its output in public logs."""
    if not re.fullmatch(r"[1-9][0-9]*", wid):
        raise ValueError("Invalid workspace ID")
    try:
        result = subprocess.run(
            ["vessl", "workspace", "start", wid],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            env={**os.environ, "VESSL_SAVE_CONFIG": "false"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("VESSL start request failed or timed out") from None
    if result.returncode:
        raise RuntimeError("VESSL start request failed")


def send_slack(events):
    lines = ["🔴 VESSL 워크스페이스 중지 상태 알림"]
    for event in events:
        reason = ("등록 후 첫 확인에서 이미 중지된 상태입니다."
                  if event.get("reason") == "initial_stopped"
                  else "실행 중이었던 워크스페이스의 중지를 확인했습니다.")
        lines.append(f"• {html.escape(event['name'])} (ID: {event['id']}) — stopped\n"
                     f"  {reason}\n"
                     f"  소유자: {html.escape(event.get('owner', ''))}\n"
                     f"  감지 시각(UTC): {event['detected_at']}")
        if event.get("start_result") == "requested":
            lines.append("  자동 시작: CLI 시작 요청 성공 (실행 완료 여부는 다음 상태 확인에서 확인합니다).")
        elif event.get("start_result") == "failed":
            lines.append("  자동 시작: CLI 시작 요청 실패 또는 시간 초과. 다음 확인에서도 stopped이면 재시도합니다.")
    lines.append("1시간 간격으로 확인합니다. 표시 시각은 실제 중지 시각이 아닌 감지 시각입니다.")
    post_slack("\n".join(lines))
    print(f"Slack acknowledged {len(events)} workspace alert(s) (HTTP 200, ok).")


def process(store, fetch, send, dry_run=False, start=None):
    state = store.load()
    rows = fetch()  # Lookup failures never modify the saved state.
    previous_events = {event["event_id"] for event in state.get("pending", [])}
    state = transition(state, rows)
    if dry_run:
        print("Dry run successful: authentication, state decryption and workspace reads verified. No starts, writes or messages.")
        return
    start = start if start is not None else start_workspace
    outcomes = {}
    # Restart from current observations, never from the historical alert outbox.
    # Try every stopped target before Slack delivery or state writes can fail.
    for wid, row in rows.items():
        if row["status"] != "stopped":
            continue
        try:
            start(wid)
            outcomes[wid] = "requested"
        except Exception:
            outcomes[wid] = "failed"
    for event in state["pending"]:
        if event["event_id"] not in previous_events and event["id"] in outcomes:
            event["start_result"] = outcomes[event["id"]]
    failed = sum(outcome == "failed" for outcome in outcomes.values())
    print(f"VESSL start requests: {len(outcomes) - failed} succeeded, {failed} failed.")
    # Save the outbox before delivery; a failed send is retried on the next run.
    store.save(state)
    if state["pending"]:
        send(state["pending"])
        state["pending"] = []
        store.save(state)
    if failed:
        raise RuntimeError("VESSL start request(s) failed; stopped targets retry next check")
    print("Monitor check completed.")


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--test-slack", action="store_true")
    args = parser.parse_args()
    if args.test_slack:
        if not os.environ.get("SLACK_WEBHOOK_URL"):
            sys.exit("Setup incomplete: add the SLACK_WEBHOOK_URL repository Secret.")
        post_slack("🧪 VESSL 알림 연결 테스트\n"
                   "이 메시지는 Slack 연결 확인용 테스트 알림입니다.\n"
                   "워크스페이스 중지 알림도 이 채널로 전달됩니다.")
        print("Slack acknowledged the test message (HTTP 200, ok).")
        return
    required = ["GH_TOKEN", "GITHUB_REPOSITORY", "STATE_ENCRYPTION_KEY",
                "VESSL_ACCESS_TOKEN", "VESSL_DEFAULT_ORGANIZATION"]
    if not args.dry_run:
        required.append("SLACK_WEBHOOK_URL")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        sys.exit("Setup incomplete: configure repository settings (VESSL_WORKSPACE_IDS as a Variable; credentials as Secrets): " + ", ".join(missing))
    store = GitHubState()

    def fetch_registered():
        targets = load_targets(store.request, store.repo, os.environ.get("VESSL_WORKSPACE_IDS", ""))
        return snapshot(targets)

    process(store, fetch_registered, send_slack, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except SelectionError as error:
        sys.exit(str(error))
    except Exception:
        # HTTP exceptions can contain the secret webhook URL or API response.
        sys.exit("Monitor failed. Check VESSL read/start permissions, Slack credentials and repository write access. Saved alerts and currently stopped targets retry next run.")
