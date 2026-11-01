#!/usr/bin/env python3
"""Read account aggregates in memory and export only a color activity card."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from token_activity.collect import CollectionError, collect_usage
from token_activity.render import render_svg


REPOSITORY = "wondukseo/wondukseo"
BRANCH = "main"
ASSET_PATH = "assets/chatgpt-token-activity.svg"
COMMIT_IDENTITY = {
    "name": "wondukseo",
    "email": "103374757+wondukseo@users.noreply.github.com",
}
MAX_GITHUB_RESPONSE = 2 * 1024 * 1024


class UpdateError(RuntimeError):
    """Fixed status text safe for an unattended local log."""


def resolve_gh(binary: str | None = None) -> str:
    candidate = str(Path(binary).expanduser()) if binary else shutil.which("gh")
    if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise UpdateError("GitHub CLI is unavailable; activity was not published.")
    return str(Path(candidate).resolve())


def _github(gh: str, endpoint: str, payload: dict | None = None) -> dict:
    command = [gh, "api", "--hostname", "github.com", endpoint]
    body = None
    if payload is not None:
        command.extend(["--method", "PUT", "--input", "-"])
        body = json.dumps(payload).encode("utf-8")
    try:
        result = subprocess.run(
            command, input=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        message = ("GitHub update could not be confirmed; check the profile before retrying."
                   if payload is not None else "GitHub did not respond; activity was not published.")
        raise UpdateError(message) from None
    if result.returncode or len(result.stdout) > MAX_GITHUB_RESPONSE:
        raise UpdateError("GitHub operation failed; check CLI access, branch policy, and the profile before retrying.")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError, RecursionError):
        message = ("GitHub update could not be confirmed; check the profile before retrying."
                   if payload is not None else "GitHub returned an invalid response; activity was not published.")
        raise UpdateError(message) from None
    if not isinstance(value, dict):
        message = ("GitHub update could not be confirmed; check the profile before retrying."
                   if payload is not None else "GitHub returned an invalid response; activity was not published.")
        raise UpdateError(message)
    return value


def publish_svg(svg: str, gh: str | None = None) -> bool:
    """Update one existing public asset, using its SHA to reject concurrent edits.

    The initial reviewed PR must already be merged. This cannot create repos,
    branches, missing files, or change visibility, and never stages local files.
    """
    executable = resolve_gh(gh)
    repository = _github(executable, f"repos/{REPOSITORY}")
    if (repository.get("full_name") != REPOSITORY
            or repository.get("private") is not False
            or repository.get("default_branch") != BRANCH
            or repository.get("archived") is not False):
        raise UpdateError("The public profile repository does not match the approved target; publication stopped.")
    endpoint = f"repos/{REPOSITORY}/contents/{ASSET_PATH}"
    current = _github(executable, endpoint + f"?ref={BRANCH}")
    sha = current.get("sha")
    if (current.get("type") != "file" or current.get("path") != ASSET_PATH
            or current.get("encoding") != "base64"
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not isinstance(current.get("content"), str)):
        raise UpdateError("The reviewed activity asset is unavailable; publication stopped.")
    try:
        previous = base64.b64decode(current["content"].replace("\n", ""), validate=True)
    except (ValueError, binascii.Error):
        raise UpdateError("The existing activity asset could not be verified; publication stopped.") from None
    content = svg.encode("utf-8")
    if previous == content:
        return False
    result = _github(executable, endpoint, {
        "message": "Update token activity colors",
        "content": base64.b64encode(content).decode("ascii"),
        "sha": sha,
        "branch": BRANCH,
        "author": COMMIT_IDENTITY,
        "committer": COMMIT_IDENTITY,
    })
    updated = result.get("content")
    if not isinstance(updated, dict) or updated.get("path") != ASSET_PATH:
        raise UpdateError("GitHub update confirmation was unavailable; check the profile before retrying.")
    return True


def write_svg(path: Path, svg: str) -> None:
    """Replace an output atomically after a complete successful collection."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".activity-", suffix=".svg", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(svg)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Official Codex executable (otherwise discovered locally)")
    parser.add_argument("--gh", help="GitHub CLI executable for publication")
    parser.add_argument("--timeout", type=float, default=25, help="Account read deadline in seconds (default: 25)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--output", type=Path, help="Local SVG output path")
    target.add_argument("--publish", action="store_true", help="Update only the approved public profile SVG")
    args = parser.parse_args(argv)
    try:
        # Raw counts live only inside this process. Nothing writes JSON or logs them.
        snapshot = collect_usage(binary=args.codex, timeout=args.timeout)
        svg = render_svg(snapshot)
        del snapshot
        if args.publish:
            changed = publish_svg(svg, gh=args.gh)
            print("Activity colors published." if changed else "Activity colors unchanged; no commit created.")
        else:
            output = args.output or Path(__file__).resolve().parents[1] / ASSET_PATH
            write_svg(output, svg)
            print("Local activity SVG updated. Exact token counts were not saved or published.")
    except (CollectionError, UpdateError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except ValueError:
        print("Daily activity is unavailable or invalid; existing activity was kept.", file=sys.stderr)
        return 1
    except Exception:
        # Never echo arbitrary exception text: it could contain a subprocess reply.
        print("Activity update failed; inspect the local setup and retry.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
