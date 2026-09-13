"""Read selected workspaces through the SDK used by the VESSL CLI.

The parent captures all output; workspace details must never reach Actions logs.
"""
import contextlib
from datetime import datetime
import io
import json
import os
import re
import sys


class SelectionError(ValueError):
    """Actionable errors containing positions, never API bodies or credentials."""


def parse_targets(value):
    targets = []
    for index, part in enumerate(value.split(","), 1):
        target = part.strip()
        if re.fullmatch(r"[1-9][0-9]*", target):
            pass
        elif target.count("/") == 1:
            owner, name = (p.strip() for p in target.split("/"))
            if not owner or not name or any(c.isspace() for c in owner) or "\n" in name:
                raise SelectionError(f"Target {index}: expected an ID or owner/name.")
            target = f"{owner}/{name}"
        else:
            raise SelectionError(f"Target {index}: expected an ID or owner/name.")
        if target not in targets:
            targets.append(target)
    return targets


def list_candidates(api, organization):
    """VESSL's mine=False excludes own workspaces; combine both lists."""
    candidates = {}
    for mine in (True, False):
        offset, seen = 0, set()
        for _ in range(100):
            page = api.workspace_list_api(
                organization_name=organization, mine=mine, limit=100,
                offset=offset, _request_timeout=20,
            )
            items = page.results
            if items is None:
                raise ValueError("Incomplete workspace list")
            total = getattr(page.page_info, "total_count", None)
            for item in items:
                wid = str(item.id)
                if wid in seen:
                    raise ValueError("Workspace pagination changed; retry next run")
                seen.add(wid)
                candidates[wid] = item
            offset += len(items)
            if total is not None:
                if offset >= total:
                    break
                if not items:
                    raise ValueError("Incomplete workspace list")
            elif not items:
                break
        else:
            raise ValueError("Workspace pagination exceeded limit")
    return list(candidates.values())


def validate_rows(targets, rows):
    if not isinstance(rows, dict) or not rows:
        raise ValueError("Empty workspace response")
    expected = set()
    for index, target in enumerate(targets, 1):
        if "/" in target:
            owner, name = target.split("/", 1)
            matches = [wid for wid, row in rows.items()
                       if row["owner"] == owner and row["name"] == name]
        else:
            matches = [target] if target in rows else []
        if len(matches) != 1:
            raise SelectionError(f"Target {index}: workspace resolution failed; check the selector or use its numeric ID.")
        expected.update(matches)
    if expected != set(rows):
        raise ValueError("Unexpected workspace response")
    for wid, row in rows.items():
        if not re.fullmatch(r"[1-9][0-9]*", wid) or not row.get("name") or not row.get("owner") or not row.get("status"):
            raise ValueError("Invalid workspace response")


def collect_rows(targets, read, list_all):
    candidates = list_all() if any("/" in target for target in targets) else []
    ids = set()
    for index, target in enumerate(targets, 1):
        if "/" not in target:
            ids.add(target)
            continue
        owner, name = target.split("/", 1)
        matches = {str(item.id) for item in candidates
                   if item.name == name and item.created_by.username == owner}
        if not matches:
            raise SelectionError(f"Target {index}: no workspace matches owner/name.")
        if len(matches) > 1:
            raise SelectionError(f"Target {index}: multiple workspaces match owner/name; use a numeric ID.")
        ids.update(matches)
    rows = {}
    for wid in sorted(ids):
        try:
            item = read(int(wid))
        except Exception as error:
            status = getattr(error, "status", None)
            if status in (401, 403, 404):
                # Use selector positions, never SDK messages or response bodies.
                positions = [str(index) for index, target in enumerate(targets, 1)
                             if target == wid or ("/" in target and any(
                                 str(candidate.id) == wid
                                 and f"{candidate.created_by.username}/{candidate.name}" == target
                                 for candidate in candidates))]
                raise SelectionError(
                    f"Target {', '.join(positions)}: workspace read returned HTTP {status}; "
                    "check access permissions and remove deleted workspaces from the selectors."
                ) from None
            raise
        if str(item.id) != wid:
            raise ValueError("Workspace ID mismatch")
        rows[wid] = {"name": item.name, "status": item.status.lower(),
                     "owner": item.created_by.username}
        deadline = getattr(item, "scheduled_termination_dt", None)
        rows[wid]["scheduled_termination_dt"] = (
            deadline.isoformat() if isinstance(deadline, datetime) else deadline
        )
    # Re-check owner/name after detail reads in case a workspace was renamed.
    validate_rows(targets, rows)
    return rows


def main():
    targets = parse_targets(os.environ["VESSL_WORKSPACE_IDS"])
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import vessl
        from vessl.workspace import read_workspace

        vessl.vessl_api.api_client.configuration.verify_ssl = True
        organization = os.environ["VESSL_DEFAULT_ORGANIZATION"]
        rows = collect_rows(
            targets,
            lambda wid: read_workspace(wid, organization_name=organization),
            lambda: list_candidates(vessl.vessl_api, organization),
        )
    print(json.dumps(rows))


if __name__ == "__main__":
    try:
        main()
    except SelectionError as error:
        print(f"Selection error: {error}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        # Do not expose API response bodies, credentials, names or IDs.
        sys.exit("VESSL snapshot failed; check credentials and workspace selectors.")
