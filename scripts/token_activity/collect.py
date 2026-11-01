"""Read account-wide token activity through the installed Codex CLI.

Only aggregate daily counts leave this module. Callers must keep the returned
counts in memory or private storage; public artifacts should contain colors only.
This module never reads credentials, configuration files, or conversation logs.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time
from typing import Any


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DAILY_BUCKETS = 20_000
MAX_TOKEN_COUNT = (1 << 63) - 1
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class CollectionError(RuntimeError):
    """A safe, fixed diagnostic that never contains account data or child output."""


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_binary(binary: str | None = None) -> str:
    """Find the CLI without inspecting any account or configuration files."""
    if binary is not None:
        if not isinstance(binary, str) or not binary.strip():
            raise CollectionError("The Codex executable path is invalid.")
        candidate = Path(binary).expanduser()
        if _executable(candidate):
            return str(candidate.resolve())
        raise CollectionError("The selected Codex executable is unavailable.")

    on_path = shutil.which("codex")
    if on_path and _executable(Path(on_path)):
        return str(Path(on_path).resolve())

    home = Path.home()
    for applications in (Path("/Applications"), home / "Applications"):
        for app in ("Codex.app", "ChatGPT.app"):
            candidate = applications / app / "Contents/Resources/codex"
            if _executable(candidate):
                return str(candidate.resolve())

    # Only inspect extension executable paths, never their settings or storage.
    machine = os.uname().machine
    architecture = {"arm64": "macos-aarch64", "x86_64": "macos-x86_64"}.get(machine)
    if architecture:
        extensions = home / ".vscode/extensions"
        try:
            versions = sorted(
                (entry for entry in extensions.iterdir() if entry.name.startswith("openai.chatgpt-")),
                key=lambda entry: tuple(int(part) for part in re.findall(r"\d+", entry.name)),
                reverse=True,
            )
            for extension in versions:
                candidate = extension / "bin" / architecture / "codex"
                if _executable(candidate):
                    return str(candidate.resolve())
        except OSError:
            pass
    raise CollectionError("Install or select the official Codex CLI before collecting activity.")


def normalize_usage(result: Any) -> dict[str, Any]:
    """Validate aggregate buckets and discard all other response fields."""
    if not isinstance(result, dict) or not isinstance(result.get("summary"), dict):
        raise CollectionError("Codex returned an unsupported usage response.")
    buckets = result.get("dailyUsageBuckets")
    if buckets is None:
        raise CollectionError("Daily token activity is unavailable; existing public activity was not replaced.")
    if not isinstance(buckets, list) or len(buckets) > MAX_DAILY_BUCKETS:
        raise CollectionError("Codex returned invalid daily token activity.")
    daily: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise CollectionError("Codex returned invalid daily token activity.")
        date = bucket.get("startDate")
        tokens = bucket.get("tokens")
        if not isinstance(date, str) or not _DATE.fullmatch(date):
            raise CollectionError("Codex returned an invalid activity date.")
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            raise CollectionError("Codex returned an invalid activity date.") from None
        if date in seen:
            raise CollectionError("Codex returned duplicate activity dates.")
        if type(tokens) is not int or not 0 <= tokens <= MAX_TOKEN_COUNT:
            raise CollectionError("Codex returned an invalid token count.")
        seen.add(date)
        daily.append({"date": date, "tokens": tokens})
    daily.sort(key=lambda bucket: bucket["date"])
    return {
        "source": "codex-account-usage",
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "daily": daily,
    }


def _send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    if message.get("method") not in {"initialize", "initialized", "account/usage/read"}:
        raise CollectionError("The activity collector refused an unsupported operation.")
    assert process.stdin is not None
    try:
        process.stdin.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
        process.stdin.flush()
    except (OSError, ValueError):
        raise CollectionError("The Codex connection closed before collection finished.") from None


def _close(process: subprocess.Popen[bytes]) -> None:
    if process.stdin:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    if process.stdout:
        process.stdout.close()


def collect_usage(binary: str | None = None, timeout: float = 25) -> dict[str, Any]:
    """Fetch aggregates once, without login, account mutation, or thread calls.

    The CLI manages its existing authentication. This client never asks it to
    log in, refresh authentication, or consume credits. No child output is logged.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise CollectionError("The collection timeout must be between 0 and 300 seconds.")
    executable = resolve_binary(binary)
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        raise CollectionError("The Codex activity connection could not be started.") from None

    buffer = bytearray()
    total_bytes = 0
    expected_id = 1
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            _send(process, {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "token_activity", "version": "1.0.0"},
                "capabilities": {"experimentalApi": False},
            }})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectionError("Codex activity collection timed out.")
                if not selector.select(remaining):
                    raise CollectionError("Codex activity collection timed out.")
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    raise CollectionError("The Codex connection closed before collection finished.")
                total_bytes += len(chunk)
                if total_bytes > MAX_RESPONSE_BYTES:
                    raise CollectionError("The Codex response exceeded the collection size limit.")
                buffer.extend(chunk)
                lines = buffer.split(b"\n")
                buffer = bytearray(lines.pop())
                for line in lines:
                    if time.monotonic() >= deadline:
                        raise CollectionError("Codex activity collection timed out.")
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        raise CollectionError("Codex returned an invalid protocol message.") from None
                    if not isinstance(message, dict):
                        raise CollectionError("Codex returned an invalid protocol message.")
                    if "method" in message:
                        if "id" in message:
                            raise CollectionError("Codex requested an unsupported interactive operation; collection stopped.")
                        # Notifications may contain private details. Ignore them.
                        continue
                    if type(message.get("id")) is not int or message["id"] != expected_id:
                        raise CollectionError("Codex returned an unexpected protocol response.")
                    if "error" in message:
                        raise CollectionError("Codex could not read token activity. Check the account in the Codex app.")
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise CollectionError("Codex returned an unsupported protocol response.")
                    if expected_id == 1:
                        _send(process, {"method": "initialized"})
                        _send(process, {"id": 2, "method": "account/usage/read"})
                        expected_id = 2
                    else:
                        return normalize_usage(result)
    except OSError:
        raise CollectionError("The Codex activity connection failed.") from None
    finally:
        _close(process)
