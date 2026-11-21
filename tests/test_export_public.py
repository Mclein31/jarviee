"""Tests for scripts/export_public.py using small temporary fake git repositories."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "export_public.py"


def _git(cwd, *args):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fake Author",
        GIT_AUTHOR_EMAIL="fake@example.com",
        GIT_COMMITTER_NAME="Fake Author",
        GIT_COMMITTER_EMAIL="fake@example.com",
    )
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_fake_repo(repo_dir, files, denylist=None):
    _git(repo_dir, "init", "-b", "main")
    for rel, content in files.items():
        path = repo_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        _git(repo_dir, "add", rel)
    _git(repo_dir, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    if denylist is not None:
        dl = repo_dir / "scripts" / "private_denylist.txt"
        dl.parent.mkdir(parents=True, exist_ok=True)
        dl.write_text(denylist, encoding="utf-8")


def _run_export(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + list(args),
        capture_output=True,
        text=True,
    )


class ExportPublicTests(unittest.TestCase):
    def test_help_exits_zero(self):
        res = _run_export("--help")
        self.assertEqual(res.returncode, 0, res.stderr)
        for flag in ("--no-git", "--skip-tests", "--allow", "--author-name", "--author-email"):
            self.assertIn(flag, res.stdout)

    def test_exclude_list_respected_and_no_git_skip_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            target = tmp_path / "out"

            excluded_files = {
                ".brain/NOTES.md": "private notes\n",
                ".design-review/snap.png": b"\x89PNG\r\n\x1a\n",
                ".impeccable/critique.md": "critique\n",
                ".taste-skill/report.json": "{}\n",
                ".omniroute-work/tmp.txt": "tmp\n",
                ".playwright-mcp/page.md": "snap\n",
                ".claude/settings.json": "{}\n",
                "HANDOFF.md": "handoff\n",
                "showcase/demo.png": b"\x89PNG\r\n\x1a\n",
                "scripts/private_denylist.txt": "SecretTerm\n",
                "local.db": b"SQLite format 3\x00",
                "sub/app.log": "log line\n",
                "pkg/__pycache__/a.cpython-39.pyc": b"\x00\x01\x02",
            }
            included_files = {
                "app.py": "print('hello public')\n",
                "tests/test_failing.py": (
                    "import unittest\n"
                    "class Failing(unittest.TestCase):\n"
                    "    def test_fail(self):\n"
                    "        self.fail('should not run with --skip-tests')\n"
                ),
            }
            all_files = dict(excluded_files)
            all_files.update(included_files)
            _init_fake_repo(repo, all_files)

            res = _run_export(str(target), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertEqual(res.returncode, 0, res.stdout + "\n" + res.stderr)
            for rel in excluded_files:
                self.assertFalse((target / rel).exists(), f"{rel} should have been excluded")
                self.assertIn(rel, res.stdout)
            for rel in included_files:
                self.assertTrue((target / rel).is_file(), f"{rel} should have been exported")
            self.assertFalse((target / ".git").exists())

    def test_denylist_hit_fails_and_leaves_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            target = tmp_path / "out"

            _init_fake_repo(
                repo,
                {"app.py": "x = 1\nname = 'secretprojectx'\n"},
                denylist="# comment line\n\nSecretProjectX\n/Users/privateuser\n",
            )
            res = _run_export(str(target), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("app.py:2:SecretProjectX", res.stdout)
            self.assertTrue((target / "app.py").is_file(), "export must be left for inspection")

    def test_missing_denylist_warns_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            target = tmp_path / "out"

            _init_fake_repo(repo, {"app.py": "print('clean')\n"}, denylist=None)
            res = _run_export(str(target), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("WARNING", res.stderr)
            self.assertIn("private_denylist.txt", res.stderr)

    def test_builtin_secret_patterns_detected(self):
        secret_lines = [
            ("sk-ant-key", "A = 'sk" + "-ant-9x8y7z6w5v4u3t2s1r0q'\n"),
            ("sk-api-key", "B = 'sk" + "-9x8y7z6w5v4u3t2s1r0q1234'\n"),
            ("github-token", "C = 'ghp" + "_9x8y7z6w5v4u3t2s1r0q1234567890abcd'\n"),
            ("google-api-key", "D = 'AIza" + "SyDummy9x8y7z6w5v4u3t2s1r0q123456'\n"),
            ("slack-token", "E = 'xoxb" + "-1234567890-1234567890-abcdef123456'\n"),
            ("aws-access-key", "F = 'AKIA" + "9876543210ABCDEF'\n"),
            ("private-key", "G = '-----BEGIN " + "RSA PRIVATE KEY-----'\n"),
            ("secret-assignment", "api_token = 'a1b2c3d4e5f6" + "0718293a4b5c6d7e8f90'\n"),
            ("secret-assignment", "db_password = 'Xy9" + "Kp2Qm8Rt4Vw6Za1Bc3De+FgH=='\n"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            target = tmp_path / "out"

            content = "".join(line for _, line in secret_lines)
            content += "config_key = 'some_long_benign_identifier_value'\n"
            _init_fake_repo(repo, {"secrets.py": content}, denylist="")

            res = _run_export(str(target), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertNotEqual(res.returncode, 0)
            for idx, (label, _) in enumerate(secret_lines, 1):
                self.assertIn(f"secrets.py:{idx}:{label}", res.stdout)
            benign_lineno = len(secret_lines) + 1
            self.assertNotIn(f"secrets.py:{benign_lineno}:", res.stdout)

    def test_fake_fixture_allowlist_in_tests_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()

            fake_tests = (
                "S1 = 'sk" + "-ABCDEF1234567890ABCDEF1234567890'\n"
                "S2 = 'sk" + "-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789'\n"
                "S3 = 'sk" + "-1234567890abcdef'\n"
                "S4 = 'sk" + "-test-987654321'\n"
                "S5 = 'AKIA" + "IOSFODNN7EXAMPLE'\n"
            )
            _init_fake_repo(repo, {"tests/test_fixtures.py": fake_tests}, denylist="SecretProjectX\n")

            target_ok = tmp_path / "out_ok"
            res_ok = _run_export(str(target_ok), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertEqual(res_ok.returncode, 0, res_ok.stdout + "\n" + res_ok.stderr)

            # Non-allowlisted secret in tests/ must fail.
            repo2 = tmp_path / "repo2"
            repo2.mkdir()
            _init_fake_repo(
                repo2,
                {"tests/test_real.py": "REAL = 'sk" + "-ant-real998877665544332211'\n"},
                denylist="SecretProjectX\n",
            )
            res_bad_secret = _run_export(str(tmp_path / "out_bad_secret"), "--source", str(repo2), "--no-git", "--skip-tests")
            self.assertNotEqual(res_bad_secret.returncode, 0)
            self.assertIn("tests/test_real.py:1:sk-ant-key", res_bad_secret.stdout)

            # Denylist hit inside tests/ is never bypassed by BUILTIN_TEST_ALLOWLIST.
            repo3 = tmp_path / "repo3"
            repo3.mkdir()
            _init_fake_repo(
                repo3,
                {"tests/test_private.py": "P = 'SecretProjectX'\n"},
                denylist="SecretProjectX\n",
            )
            res_bad_deny = _run_export(str(tmp_path / "out_bad_deny"), "--source", str(repo3), "--no-git", "--skip-tests")
            self.assertNotEqual(res_bad_deny.returncode, 0)
            self.assertIn("tests/test_private.py:1:SecretProjectX", res_bad_deny.stdout)

            # Fake fixture outside tests/ fails unless --allow is given.
            repo4 = tmp_path / "repo4"
            repo4.mkdir()
            _init_fake_repo(
                repo4,
                {"app.py": "EXAMPLE = 'sk" + "-test-987654321'\n"},
                denylist="",
            )
            res_outside = _run_export(str(tmp_path / "out_outside"), "--source", str(repo4), "--no-git", "--skip-tests")
            self.assertNotEqual(res_outside.returncode, 0)

    def test_allow_regex_suppresses_hit_and_invalid_regex_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            _init_fake_repo(
                repo,
                {"app.py": "EXAMPLE = 'sk" + "-test-987654321'\n"},
                denylist="",
            )
            res_ok = _run_export(
                str(tmp_path / "out_allowed"),
                "--source", str(repo),
                "--no-git", "--skip-tests",
                "--allow", "sk" + r"-test-\d+",
            )
            self.assertEqual(res_ok.returncode, 0, res_ok.stdout + "\n" + res_ok.stderr)

            res_bad_rx = _run_export(
                str(tmp_path / "out_bad_rx"),
                "--source", str(repo),
                "--no-git", "--skip-tests",
                "--allow", "[unclosed",
            )
            self.assertNotEqual(res_bad_rx.returncode, 0)

    def test_refuses_non_empty_target_and_target_inside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            _init_fake_repo(repo, {"app.py": "print('ok')\n"}, denylist="")

            non_empty = tmp_path / "non_empty"
            non_empty.mkdir()
            marker = non_empty / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            res1 = _run_export(str(non_empty), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertNotEqual(res1.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

            inside = repo / "sub_export"
            res2 = _run_export(str(inside), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertNotEqual(res2.returncode, 0)
            self.assertFalse(inside.exists())

            empty_target = tmp_path / "empty_ok"
            empty_target.mkdir()
            res3 = _run_export(str(empty_target), "--source", str(repo), "--no-git", "--skip-tests")
            self.assertEqual(res3.returncode, 0, res3.stderr)
            self.assertTrue((empty_target / "app.py").is_file())

    def test_git_init_and_tests_run_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            passing_test = (
                "import unittest\n"
                "class Ok(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertEqual(1 + 1, 2)\n"
            )
            _init_fake_repo(repo, {"app.py": "x = 1\n", "tests/test_ok.py": passing_test}, denylist="")

            # Missing --author-name / --author-email without --no-git must fail and suggest noreply.
            res_no_author = _run_export(str(tmp_path / "out_no_author"), "--source", str(repo))
            self.assertNotEqual(res_no_author.returncode, 0)
            self.assertIn("noreply.github.com", res_no_author.stderr)

            # Full export with passing tests and author flags creates single commit on main, no remotes.
            target_ok = tmp_path / "out_git"
            res_ok = _run_export(
                str(target_ok),
                "--source", str(repo),
                "--author-name", "Public User",
                "--author-email", "12345+pub@users.noreply.github.com",
            )
            self.assertEqual(res_ok.returncode, 0, res_ok.stdout + "\n" + res_ok.stderr)
            self.assertTrue((target_ok / ".git").is_dir())
            branch = _git(target_ok, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            self.assertEqual(branch, "main")
            count = _git(target_ok, "rev-list", "--count", "HEAD").stdout.strip()
            self.assertEqual(count, "1")
            log_line = _git(target_ok, "log", "-1", "--format=%s|%an|%ae|%cn|%ce").stdout.strip()
            self.assertEqual(
                log_line,
                "Initial public release|Public User|12345+pub@users.noreply.github.com|Public User|12345+pub@users.noreply.github.com",
            )
            remotes = _git(target_ok, "remote").stdout.strip()
            self.assertEqual(remotes, "")

            # Failing test aborts before git init.
            repo_fail = tmp_path / "repo_fail"
            repo_fail.mkdir()
            failing_test = (
                "import unittest\n"
                "class Bad(unittest.TestCase):\n"
                "    def test_bad(self):\n"
                "        self.fail('boom')\n"
            )
            _init_fake_repo(repo_fail, {"tests/test_bad.py": failing_test}, denylist="")
            target_fail = tmp_path / "out_fail_test"
            res_fail = _run_export(
                str(target_fail),
                "--source", str(repo_fail),
                "--author-name", "Public User",
                "--author-email", "12345+pub@users.noreply.github.com",
            )
            self.assertNotEqual(res_fail.returncode, 0)
            self.assertFalse((target_fail / ".git").exists())


if __name__ == "__main__":
    unittest.main()
