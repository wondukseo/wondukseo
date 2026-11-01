#!/usr/bin/env python3
"""Review or install a private, local LaunchAgent for public activity colors.

The default is a read-only plan. Installation copies a fixed source allowlist;
it never copies account configuration, credentials, sessions, or usage JSON.
Existing installations are deliberately preserved rather than overwritten.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
from typing import Callable, Optional


LABEL = "local.wonduk.chatgpt-token-activity"
INTERVAL_SECONDS = 4 * 60 * 60
RUNNER_FILES = (
    "update_token_activity.py",
    "token_activity/__init__.py",
    "token_activity/collect.py",
    "token_activity/render.py",
)
STANDARD_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MISSING_SERVICE = 113


class InstallError(RuntimeError):
    """An actionable diagnostic containing no account data or child output."""


@dataclass(frozen=True)
class InstallPlan:
    home: Path
    root: Path
    runner: Path
    plist: Path
    files: dict[str, bytes]
    configuration: dict


def check_path(path: Path) -> None:
    """Reject redirected destinations, including dangling links and ancestors."""
    if not path.is_absolute():
        raise InstallError("Installation paths must be absolute.")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InstallError("A source or installation path is a symbolic link; nothing was replaced.")
        if part != path and part.exists() and not part.is_dir():
            raise InstallError("An installation parent is not a directory.")


def executable(value: Optional[str], default: str) -> Path:
    selected = value or default
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        found = shutil.which(selected)
        if found is None:
            raise InstallError("A required executable is unavailable; select its absolute path.")
        candidate = Path(found)
    candidate = Path(os.path.abspath(candidate))
    resolved = candidate.resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise InstallError("A required executable is unavailable or not executable.")
    # Validate the target, but retain a stable launcher such as Homebrew's bin
    # symlink. Pinning its current Cellar version would break routine upgrades.
    return candidate


def check_python(python: Path) -> None:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c",
             "import sys; print(int(sys.version_info >= (3, 9) and sys.prefix == sys.base_prefix))"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("The selected Python could not be checked.") from None
    if result.returncode != 0 or result.stdout.strip() != "1":
        raise InstallError("Select a stable, non-virtualenv Python 3.9 or newer.")


def make_plan(source: Path, home: Path, python: Path, gh: Path,
              codex: Optional[Path] = None, discovered_codex: Optional[Path] = None) -> InstallPlan:
    source, home = Path(os.path.abspath(source)), Path(os.path.abspath(home))
    check_path(source)
    check_path(home)
    if not source.is_dir() or not home.is_dir():
        raise InstallError("The source directory and current user's home must exist.")
    files = {}
    for relative in RUNNER_FILES:
        path = source / relative
        check_path(path)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_SOURCE_BYTES:
            raise InstallError("A required reviewed runner source is missing or invalid.")
        data = path.read_bytes()
        if len(data) > MAX_SOURCE_BYTES:
            raise InstallError("A runner source exceeds the size limit.")
        try:
            compile(data.decode("utf-8"), relative, "exec")
        except (SyntaxError, UnicodeError):
            raise InstallError("A runner source is not valid Python.") from None
        files[relative] = data

    root = home / "Library/Application Support/ChatGPT Token Activity"
    runner = root / "runner"
    plist = home / "Library/LaunchAgents" / (LABEL + ".plist")
    arguments = [str(python), "-B", "-E", "-s", "-u", str(runner / RUNNER_FILES[0]),
                 "--publish", "--gh", str(gh), "--timeout", "25"]
    if codex is not None:
        arguments.extend(("--codex", str(codex)))
    search_dirs = [str(python.parent), str(gh.parent)]
    if codex is not None or discovered_codex is not None:
        search_dirs.append(str((codex or discovered_codex).parent))
    search_dirs.extend(STANDARD_PATH)
    configuration = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(search_dirs))},
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(root / "logs/status.log"),
        "StandardErrorPath": str(root / "logs/error.log"),
    }
    plan = InstallPlan(home, root, runner, plist, files, configuration)
    check_destination(plan)
    return plan


def check_destination(plan: InstallPlan) -> None:
    for path in (plan.root, plan.plist):
        check_path(path)
        if path.exists():
            raise InstallError("An installation or LaunchAgent already exists; it was preserved. Review it before reinstalling.")


def launchctl(arguments: list[str]) -> int:
    try:
        return subprocess.run(["/bin/launchctl", *arguments], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20).returncode
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("launchctl could not complete; its output was not logged.") from None


def check_agent_absent(control: Callable[[list[str]], int], uid: int) -> None:
    domain = "gui/" + str(uid)
    if control(["print", domain]) != 0:
        raise InstallError("A logged-in macOS GUI session is required to install this LaunchAgent.")
    status = control(["print", domain + "/" + LABEL])
    if status == 0:
        raise InstallError("A LaunchAgent with this label is already loaded; it was preserved.")
    if status != MISSING_SERVICE:
        raise InstallError("The existing LaunchAgent state could not be confirmed; installation stopped.")


def install(plan: InstallPlan, control: Callable[[list[str]], int] = launchctl,
            uid: Optional[int] = None) -> None:
    """Install new files only; undo our files if loading fails and the job is absent."""
    uid = os.getuid() if uid is None else uid
    check_destination(plan)
    check_agent_absent(control, uid)
    directories: list[Path] = []
    written: list[tuple[Path, bytes, int, int]] = []
    bootstrapping = False

    def directory(path: Path) -> None:
        check_path(path)
        if path.exists():
            if not path.is_dir():
                raise InstallError("A destination parent is not a directory.")
            return
        directory(path.parent)
        path.mkdir(mode=0o700)
        directories.append(path)

    def write(path: Path, data: bytes) -> None:
        check_path(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        info = path.stat()
        written.append((path, data, info.st_dev, info.st_ino))

    def clean_new_files() -> None:
        # Do not remove a path another process replaced, or new diagnostics.
        for path, data, device, inode in reversed(written):
            try:
                info = path.lstat()
                if (stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode)
                        and path.read_bytes() == data):
                    path.unlink()
            except OSError:
                pass
        for path in reversed(directories):
            try:
                path.rmdir()
            except OSError:
                pass

    try:
        directory(plan.root.parent)
        # Exclusive creation refuses a concurrent installer or unrelated folder.
        plan.root.mkdir(mode=0o700)
        directories.append(plan.root)
        directory(plan.runner / "token_activity")
        directory(plan.root / "logs")
        directory(plan.plist.parent)
        for relative, data in plan.files.items():
            write(plan.runner / relative, data)
        write(plan.root / "logs/status.log", b"")
        write(plan.root / "logs/error.log", b"")
        write(plan.plist, plistlib.dumps(plan.configuration, sort_keys=True))
        # Recheck before the one action that starts the scheduled publisher.
        check_agent_absent(control, uid)
        bootstrapping = True
        domain = "gui/" + str(uid)
        if control(["bootstrap", domain, str(plan.plist)]) != 0:
            raise InstallError("LaunchAgent loading failed.")
        if control(["print", domain + "/" + LABEL]) != 0:
            raise InstallError("The newly loaded LaunchAgent could not be verified.")
    except (Exception, KeyboardInterrupt) as error:
        if bootstrapping:
            target = "gui/" + str(uid) + "/" + LABEL
            try:
                absent = control(["print", target]) == MISSING_SERVICE
            except InstallError:
                absent = False
            if not absent:
                # A failed bootstrap may mean another process loaded this label
                # after our last check. Without proven ownership, never bootout
                # that job or remove files a successfully loaded job may need.
                raise InstallError("LaunchAgent state is uncertain. Installation files were retained for safe recovery.") from None
        clean_new_files()
        if isinstance(error, InstallError):
            raise error
        raise InstallError("Installation failed. Existing files were preserved; any new diagnostics were retained.") from None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Install and immediately start the four-hour publisher")
    parser.add_argument("--python", help="Stable Python 3.9+ executable")
    parser.add_argument("--gh", help="GitHub CLI executable")
    parser.add_argument("--codex", help="Optional explicit Codex CLI executable")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin" or os.geteuid() == 0:
            raise InstallError("Run this installer as your normal logged-in macOS user, without sudo.")
        python = executable(args.python, "/usr/bin/python3")
        check_python(python)
        gh = executable(args.gh, "gh")
        codex = executable(args.codex, "codex") if args.codex else None
        discovered = shutil.which("codex") if codex is None else None
        discovered_codex = Path(os.path.abspath(discovered)) if discovered else None
        plan = make_plan(Path(__file__).resolve().parent, Path.home(), python, gh, codex, discovered_codex)
        check_agent_absent(launchctl, os.getuid())
        print("Local runner: " + str(plan.runner))
        print("LaunchAgent: " + str(plan.plist))
        print("Schedule: every 4 hours, and immediately when loaded or after login.")
        print("Publishes activity colors only; logs contain status messages only.")
        for relative, data in plan.files.items():
            print("Reviewed source SHA256 " + hashlib.sha256(data).hexdigest() + "  " + relative)
        if not args.apply:
            print("Dry run: no files changed and no automation started. Add --apply after reviewing this plan.")
            return 0
        install(plan)
        print("Installed and loaded. The next automatic run is scheduled within four hours.")
        return 0
    except (InstallError, OSError) as error:
        print(str(error) if isinstance(error, InstallError) else "Installation filesystem check failed; no existing files were replaced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
