import base64
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_token_activity as updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {"full_name": updater.REPOSITORY, "private": False,
                         "default_branch": "main", "archived": False}
        self.asset = {"type": "file", "path": updater.ASSET_PATH, "sha": "a" * 40,
                      "encoding": "base64", "content": base64.b64encode(b"old").decode()}

    def test_publish_only_one_asset_with_expected_sha_and_noreply_identity(self):
        with patch.object(updater, "resolve_gh", return_value="/bin/gh"), patch.object(
                updater, "_github", side_effect=[self.metadata, self.asset,
                {"content": {"path": updater.ASSET_PATH}}]) as request:
            self.assertTrue(updater.publish_svg("new"))
        self.assertEqual(len(request.call_args_list), 3)
        command = request.call_args_list[-1].args
        self.assertEqual(command[1], "repos/wondukseo/wondukseo/contents/assets/chatgpt-token-activity.svg")
        self.assertEqual(command[2]["sha"], "a" * 40)
        self.assertEqual(command[2]["branch"], "main")
        self.assertEqual(base64.b64decode(command[2]["content"]), b"new")
        self.assertTrue(command[2]["author"]["email"].endswith("@users.noreply.github.com"))

    def test_unchanged_asset_creates_no_commit(self):
        with patch.object(updater, "resolve_gh", return_value="/bin/gh"), patch.object(
                updater, "_github", side_effect=[self.metadata, self.asset]) as request:
            self.assertFalse(updater.publish_svg("old"))
            self.assertEqual(request.call_count, 2)

    def test_repository_must_match_approved_public_target(self):
        for field, value in (("private", True), ("private", None), ("archived", True),
                             ("full_name", "wondukseo/quokka-mac-suite"), ("default_branch", "other")):
            with self.subTest(field=field, value=value):
                metadata = dict(self.metadata, **{field: value})
                with patch.object(updater, "resolve_gh", return_value="/bin/gh"), patch.object(
                        updater, "_github", return_value=metadata) as request:
                    with self.assertRaises(updater.UpdateError):
                        updater.publish_svg("new")
                    self.assertEqual(request.call_count, 1)

    def test_missing_or_unexpected_asset_never_creates_a_file(self):
        for field, value in (("path", "README.md"), ("type", "symlink"), ("sha", None),
                             ("encoding", "none"), ("content", "bad base64!")):
            with self.subTest(field=field):
                with patch.object(updater, "resolve_gh", return_value="/bin/gh"), patch.object(
                        updater, "_github", side_effect=[self.metadata, dict(self.asset, **{field: value})]) as request:
                    with self.assertRaises(updater.UpdateError):
                        updater.publish_svg("new")
                    self.assertEqual(request.call_count, 2)

    def test_concurrent_update_failure_does_not_retry_or_force(self):
        with patch.object(updater, "resolve_gh", return_value="/bin/gh"), patch.object(
                updater, "_github", side_effect=[self.metadata, self.asset, updater.UpdateError("Conflict")]) as request:
            with self.assertRaises(updater.UpdateError):
                updater.publish_svg("new")
            self.assertEqual(request.call_count, 3)

    def test_github_error_output_is_not_exposed(self):
        result = subprocess.CompletedProcess([], 1, b'{"secret":"PRIVATE_VALUE"}', b"PRIVATE_VALUE")
        with patch.object(updater.subprocess, "run", return_value=result) as run:
            with self.assertRaises(updater.UpdateError) as error:
                updater._github("/bin/gh", "repos/wondukseo/wondukseo")
            self.assertNotIn("PRIVATE_VALUE", str(error.exception))
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.args[0][2:4], ["--hostname", "github.com"])

    def test_failed_collection_keeps_existing_file_and_logs_no_exception_data(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "activity.svg"
            path.write_text("last good")
            output = io.StringIO()
            with patch.object(updater, "collect_usage", side_effect=RuntimeError("PRIVATE_VALUE")), redirect_stderr(output):
                self.assertEqual(updater.main(["--output", str(path)]), 1)
            self.assertEqual(path.read_text(), "last good")
            self.assertNotIn("PRIVATE_VALUE", output.getvalue())

    def test_unconfirmed_put_does_not_claim_no_publication(self):
        cases = [subprocess.TimeoutExpired(["gh"], 30),
                 subprocess.CompletedProcess([], 0, b"PRIVATE_VALUE"),
                 subprocess.CompletedProcess([], 0, b"[]")]
        for case in cases:
            with self.subTest(case=type(case).__name__):
                kwargs = {"side_effect": case} if isinstance(case, Exception) else {"return_value": case}
                with patch.object(updater.subprocess, "run", **kwargs):
                    with self.assertRaises(updater.UpdateError) as error:
                        updater._github("/bin/gh", "contents/asset", payload={"content": "colors"})
                    self.assertIn("could not be confirmed", str(error.exception))
                    self.assertNotIn("PRIVATE_VALUE", str(error.exception))
                    self.assertNotIn("not published", str(error.exception))

    def test_publish_writes_no_local_files_and_has_no_raw_count_log(self):
        snapshot = {"source": "codex-account-usage", "fetched_at": "2026-11-01T12:00:00Z",
                    "daily": [{"date": "2026-11-01", "tokens": 12345678912345}]}
        output = io.StringIO()
        with patch.object(updater, "collect_usage", return_value=snapshot), patch.object(
                updater, "render_svg", return_value="<svg>colors</svg>") as render, patch.object(
                updater, "write_svg") as write, patch.object(updater, "publish_svg", return_value=True) as publish, redirect_stdout(output):
            self.assertEqual(updater.main(["--publish", "--gh", "/bin/gh"]), 0)
        write.assert_not_called()
        render.assert_called_once_with(snapshot)
        publish.assert_called_once_with("<svg>colors</svg>", gh="/bin/gh")
        self.assertNotIn("12345678912345", output.getvalue())

    def test_atomic_local_export(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "assets" / "activity.svg"
            updater.write_svg(path, "new colors")
            self.assertEqual(path.read_text(), "new colors")
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
