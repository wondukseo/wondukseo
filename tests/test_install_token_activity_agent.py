"""Offline LaunchAgent checks: temporary homes and a simulated launchctl only."""

import importlib.util
import io
from pathlib import Path
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_token_activity_agent.py"
SPEC = importlib.util.spec_from_file_location("install_token_activity_agent", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class LaunchctlFixture:
    def __init__(self, loaded=False, fail_bootstrap=False, bootstrap_loads=True, unknown=False):
        self.loaded = loaded
        self.fail_bootstrap = fail_bootstrap
        self.bootstrap_loads = bootstrap_loads
        self.unknown = unknown
        self.calls = []

    def __call__(self, arguments):
        self.calls.append(arguments)
        if arguments[0] == "print":
            if arguments[1] == "gui/501":
                return 0
            if self.unknown:
                return 1
            return 0 if self.loaded else installer.MISSING_SERVICE
        if arguments[0] == "bootstrap":
            # A failed response can still leave a job whose owner is unknown.
            self.loaded = self.bootstrap_loads
            return 1 if self.fail_bootstrap else 0
        raise AssertionError("Unexpected launchctl command")


class InstallerChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.source = self.root / "source"
        self.home.mkdir()
        self.source.mkdir()
        for relative in installer.RUNNER_FILES:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# reviewed synthetic runner\n", encoding="utf-8")
        # These must never enter the installed copy, even beside reviewed code.
        (self.source / "auth.json").write_text('"DO_NOT_COPY_AUTH"')
        (self.source / ".env").write_text("DO_NOT_COPY_ENV=fixture")
        (self.source / "private-usage.json").write_text('"DO_NOT_COPY_COUNTS"')

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self, explicit=False):
        return installer.make_plan(
            self.source, self.home, Path("/usr/bin/python3"), Path("/opt/homebrew/bin/gh"),
            codex=Path("/opt/tools/codex") if explicit else None,
            discovered_codex=None if explicit else Path("/opt/codex-extension/bin/codex"),
        )

    def test_read_only_plan_and_explicit_allowlist(self):
        before = set(self.home.rglob("*"))
        plan = self.plan()
        self.assertEqual(set(self.home.rglob("*")), before)
        self.assertEqual(set(plan.files), set(installer.RUNNER_FILES))
        self.assertNotIn(b"DO_NOT_COPY", b"".join(plan.files.values()))
        config = plan.configuration
        self.assertEqual(config["StartInterval"], 14400)
        self.assertIs(config["RunAtLoad"], True)
        self.assertEqual(config["ProcessType"], "Background")
        self.assertEqual(config["Umask"], 0o077)
        self.assertNotIn("KeepAlive", config)
        self.assertEqual(set(config["EnvironmentVariables"]), {"PATH"})
        self.assertIn("/opt/codex-extension/bin", config["EnvironmentVariables"]["PATH"].split(":"))
        self.assertNotIn("--codex", config["ProgramArguments"])
        self.assertIn("-B", config["ProgramArguments"])
        self.assertIn("-E", config["ProgramArguments"])
        self.assertIn("-s", config["ProgramArguments"])
        self.assertNotIn("-I", config["ProgramArguments"])

    def test_explicit_codex_is_pinned_and_publication_arguments_are_fixed(self):
        plan = self.plan(explicit=True)
        arguments = plan.configuration["ProgramArguments"]
        self.assertEqual(arguments[0], "/usr/bin/python3")
        self.assertEqual(arguments[5], str(plan.runner / "update_token_activity.py"))
        self.assertEqual(arguments[6:], ["--publish", "--gh", "/opt/homebrew/bin/gh",
                                        "--timeout", "25", "--codex", "/opt/tools/codex"])

    def test_install_copies_only_reviewed_snapshot_with_private_permissions(self):
        plan = self.plan()
        (self.source / "update_token_activity.py").write_text("# later edit\n")
        control = LaunchctlFixture()
        installer.install(plan, control, uid=501)
        self.assertTrue(control.loaded)
        self.assertEqual({p.relative_to(plan.runner).as_posix() for p in plan.runner.rglob("*") if p.is_file()},
                         set(installer.RUNNER_FILES))
        for relative in installer.RUNNER_FILES:
            self.assertEqual((plan.runner / relative).read_bytes(), plan.files[relative])
        config = plistlib.loads(plan.plist.read_bytes())
        self.assertEqual(config, plan.configuration)
        for directory in (plan.root, plan.runner, plan.runner / "token_activity", plan.root / "logs"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for file in (*plan.runner.rglob("*.py"), plan.plist, plan.root / "logs/status.log", plan.root / "logs/error.log"):
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual([call[0] for call in control.calls].count("bootstrap"), 1)

    def test_existing_directory_and_unrelated_files_are_preserved(self):
        plan = self.plan()
        plan.root.mkdir(parents=True)
        marker = plan.root / "unrelated.txt"
        marker.write_text("preserve")
        control = LaunchctlFixture()
        with self.assertRaises(installer.InstallError):
            installer.install(plan, control, uid=501)
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual(control.calls, [])

    def test_existing_plist_is_preserved(self):
        plan = self.plan()
        plan.plist.parent.mkdir(parents=True)
        plan.plist.write_bytes(b"unrelated existing plist")
        with self.assertRaises(installer.InstallError):
            installer.install(plan, LaunchctlFixture(), uid=501)
        self.assertEqual(plan.plist.read_bytes(), b"unrelated existing plist")
        self.assertFalse(plan.root.exists())

    def test_loaded_or_unknown_agent_prevents_filesystem_changes(self):
        for control in (LaunchctlFixture(loaded=True), LaunchctlFixture(unknown=True)):
            plan = self.plan()
            with self.assertRaises(installer.InstallError):
                installer.install(plan, control, uid=501)
            self.assertFalse(plan.root.exists())
            self.assertFalse(plan.plist.exists())
            self.assertNotIn("bootstrap", [call[0] for call in control.calls])

    def test_bootstrap_failure_rolls_back_only_after_job_is_confirmed_absent(self):
        plan = self.plan()
        control = LaunchctlFixture(fail_bootstrap=True, bootstrap_loads=False)
        with self.assertRaisesRegex(installer.InstallError, "loading failed"):
            installer.install(plan, control, uid=501)
        self.assertFalse(control.loaded)
        self.assertFalse(plan.plist.exists())
        self.assertFalse(plan.root.exists())
        self.assertNotIn("bootout", [call[0] for call in control.calls])
        self.assertTrue((self.source / "auth.json").exists())

    def test_bootstrap_collision_preserves_unproven_same_label_job_and_files(self):
        plan = self.plan()
        # A competing installer may load the label after the second absence
        # check, causing our bootstrap to fail. We cannot claim ownership.
        control = LaunchctlFixture(fail_bootstrap=True)
        with self.assertRaisesRegex(installer.InstallError, "state is uncertain"):
            installer.install(plan, control, uid=501)
        self.assertTrue(control.loaded)
        self.assertTrue(plan.plist.is_file())
        self.assertTrue((plan.runner / "update_token_activity.py").is_file())
        self.assertNotIn("bootout", [call[0] for call in control.calls])

    def test_executable_preserves_stable_symlink_across_package_upgrades(self):
        versioned = self.root / "Cellar/gh/1/bin/gh"
        versioned.parent.mkdir(parents=True)
        versioned.write_text("#!/bin/sh\nexit 0\n")
        versioned.chmod(0o755)
        stable = self.root / "bin/gh"
        stable.parent.mkdir()
        stable.symlink_to(versioned)
        selected = installer.executable(str(stable), "gh")
        self.assertEqual(selected, stable)
        self.assertNotEqual(selected, versioned)
        plan = installer.make_plan(self.source, self.home, stable, selected)
        self.assertEqual(plan.configuration["ProgramArguments"][0], str(stable))
        arguments = plan.configuration["ProgramArguments"]
        self.assertEqual(arguments[arguments.index("--gh") + 1], str(stable))
        self.assertIn(str(stable.parent), plan.configuration["EnvironmentVariables"]["PATH"].split(":"))

    def test_source_and_destination_symlinks_are_rejected(self):
        source = self.source / "token_activity/render.py"
        source.unlink()
        source.symlink_to(self.source / "auth.json")
        with self.assertRaises(installer.InstallError):
            self.plan()
        source.unlink()
        source.write_text("# reviewed\n")
        (self.home / "Library").symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            self.plan()

    def test_invalid_source_is_rejected_before_installation(self):
        (self.source / "token_activity/render.py").write_text("not valid python !")
        with self.assertRaisesRegex(installer.InstallError, "not valid Python"):
            self.plan()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_default_cli_is_dry_run_without_install_or_bootstrap(self):
        plan = self.plan()
        control = LaunchctlFixture()
        with mock.patch.object(installer.sys, "platform", "darwin"), \
             mock.patch.object(installer.os, "geteuid", return_value=501), \
             mock.patch.object(installer.os, "getuid", return_value=501), \
             mock.patch.object(installer, "executable", return_value=Path("/usr/bin/python3")), \
             mock.patch.object(installer, "check_python"), \
             mock.patch.object(installer, "make_plan", return_value=plan), \
             mock.patch.object(installer, "launchctl", control), \
             mock.patch.object(installer, "install") as apply, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(installer.main([]), 0)
            apply.assert_not_called()
            self.assertIn("Dry run", output.getvalue())
        self.assertFalse(plan.root.exists())
        self.assertFalse(plan.plist.exists())
        self.assertNotIn("bootstrap", [call[0] for call in control.calls])

    def test_python_check_rejects_older_or_virtualenv_runtime_without_child_output(self):
        result = mock.Mock(returncode=0, stdout="0\n")
        with mock.patch.object(installer.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(installer.InstallError, "Python 3.9"):
                installer.check_python(Path("/some/python"))


if __name__ == "__main__":
    unittest.main()
