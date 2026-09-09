"""Read selected workspaces through the SDK used by the VESSL CLI.

The parent captures all output; workspace details must never reach Actions logs.
"""
import contextlib
import io
import json
import os
import sys


def main():
    ids = [x.strip() for x in os.environ["VESSL_WORKSPACE_IDS"].split(",")]
    if not ids or any(not x.isdigit() for x in ids):
        raise ValueError("Invalid workspace IDs")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import vessl
        from vessl.workspace import read_workspace

        vessl.vessl_api.api_client.configuration.verify_ssl = True
        rows = {}
        for wid in ids:
            item = read_workspace(
                int(wid), organization_name=os.environ["VESSL_DEFAULT_ORGANIZATION"]
            )
            if str(item.id) != wid or not item.name or not item.status:
                raise ValueError("Invalid workspace response")
            rows[wid] = {"name": item.name, "status": item.status.lower(),
                         "owner": item.created_by.username}
    print(json.dumps(rows))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Do not expose API response bodies, credentials, names or IDs.
        sys.exit("VESSL snapshot failed; check credentials and selected workspace IDs.")
