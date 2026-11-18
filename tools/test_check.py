"""Exercise the check runner's exit status without launching browsers or using the network."""
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class CheckRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wo checks ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "commands"
        self.script = self.root / "ops/check.sh"
        self.script.parent.mkdir()
        shutil.copyfile(ROOT / "ops/check.sh", self.script)
        (self.script.parent / 'local').mkdir()
        shutil.copyfile(ROOT / 'ops/local/check_runner.py', self.script.parent / 'local/check_runner.py')
        for name in ("bash", "dirname", "mkdir", "tail", "mktemp", "rm", "find", "env", "grep"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.command(self.root / ".venv/bin/python",
                     'if [ "$1" = ops/local/check_runner.py ]; then\n'
                     '  if [ "$2" = preflight ]; then exit 0; fi\n'
                     '  if [ "$2" = node-deps ]; then exit "${WO_TEST_DEPS_EXIT:-0}"; fi\n'
                     f'  exec {shlex.quote(sys.executable)} "$@"\n'
                     'fi\n'
                     'printf "python %s contract=%s\\n" "$*" "$WO_CONTRACT_DB" >> "$WO_TEST_TRACE"\n'
                     'exit "${WO_TEST_PY_EXIT:-0}"\n')
        self.command(self.bin / "node",
                     'if [ "$1" = -e ]; then echo "node -e (browser probe)" >> "$WO_TEST_TRACE"; printf "%s\\n" "${WO_TEST_BROWSERS-chromium webkit}"; exit 0; fi\n'
                     'printf "node %s browsers=%s chromium=%s\\n" "$*" "$WO_UI_BROWSERS" "$WO_UI_CHROMIUM" >> "$WO_TEST_TRACE"\n'
                     'exit "${WO_TEST_UI_EXIT:-0}"\n')
        self.command(self.bin / "npm",
                     'printf "npm %s\\n" "$*" >> "$WO_TEST_TRACE"\n'
                     'if [ "${WO_TEST_NPM_EXIT:-0}" != 0 ]; then exit "$WO_TEST_NPM_EXIT"; fi\n'
                     'mkdir -p tests/ui/node_modules/playwright-core\n')
        self.package = self.root / "tests/ui/node_modules/playwright-core"
        self.package.mkdir(parents=True)
        data = self.root / "data/work-ops.db"
        data.parent.mkdir()
        data.write_bytes(b"test runner placeholder; fake runtimes do not open this file")

    def command(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + source)
        path.chmod(0o755)

    def run_checks(self, *args, **env):
        # The agent running these tests may itself be sandboxed. The script must see a clean
        # environment unless a test sets the seatbelt variable on purpose.
        base = {k: v for k, v in os.environ.items() if k not in {"CURSOR_SANDBOX", "CODEX_SANDBOX"}}
        return subprocess.run(
            ["/bin/bash", str(self.script), *args], capture_output=True, text=True, timeout=10,
            env={**base, "PATH": str(self.bin), "TMPDIR": str(self.root), "WO_TEST_TRACE": str(self.trace),
                 "PLAYWRIGHT_BROWSERS_PATH": "", "WO_UI_CHROMIUM": "", **env},
        )

    def preinstalled_chromium(self):
        browsers = self.root / "pw-browsers"
        self.command(browsers / "chromium", "exit 0\n")
        return browsers

    def test_default_mode_reports_missing_browsers_as_a_skip(self):
        result = self.run_checks("ui", WO_TEST_BROWSERS="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIP", result.stdout)
        self.assertNotIn("ok    browser", result.stdout)
        self.assertNotIn("smoke.mjs", self.trace.read_text())

    def test_strict_mode_rejects_missing_browsers(self):
        result = self.run_checks("--strict", "ui", WO_TEST_BROWSERS="")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SKIP", result.stdout)
        self.assertIn("strict", result.stdout.lower())

    def test_strict_mode_accepts_successful_required_checks(self):
        result = self.run_checks("--strict", "contract", "ui")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok    contract", result.stdout)
        self.assertIn("ok    browser", result.stdout)
        self.assertIn("smoke.mjs", self.trace.read_text())

    def test_visual_suite_is_explicit_and_uses_existing_browser_selection(self):
        result = self.run_checks("--strict", "visual")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text()
        self.assertIn("visual-regression.mjs browsers=chromium,webkit", trace)
        self.assertIn("visual-cleanup-check.mjs browsers=chromium,webkit", trace)
        self.assertIn("Browser suites: 1 at a time", result.stdout)
        self.assertNotIn("smoke.mjs", trace)
        self.assertNotIn("--update", trace)

    def test_visual_failures_remain_failures(self):
        result = self.run_checks("--strict", "visual", WO_TEST_UI_EXIT="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)

    def test_browser_artifacts_default_to_this_runs_private_log_directory(self):
        destination = self.root / 'artifact-destination'
        self.command(self.bin / "node",
                     'if [ "$1" = -e ]; then printf "chromium webkit\\n"; exit 0; fi\n'
                     'case "$1" in tests/ui/*.mjs) printf "%s" "$WO_UI_ARTIFACT_DIR" > "$WO_TEST_ARTIFACT_RECORD";; esac\n')
        result = self.run_checks("--strict", "ui", WO_UI_ARTIFACT_DIR="",
                                 WO_TEST_ARTIFACT_RECORD=str(destination))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        logs = list(self.root.glob('work-ops-check.*'))
        self.assertEqual(len(logs), 1)
        self.assertEqual(destination.read_text(), str(logs[0] / 'browser-artifacts'))

    def test_success_removes_routine_captures_but_failure_keeps_evidence(self):
        self.command(self.bin / "node",
                     'if [ "$1" = -e ]; then printf "chromium webkit\\n"; exit 0; fi\n'
                     'if [ -n "$SCREENSHOT_DIR" ]; then\n'
                     '  mkdir -p "$SCREENSHOT_DIR"\n'
                     '  printf "synthetic capture" > "$SCREENSHOT_DIR/desktop.png"\n'
                     'fi\n'
                     'exit "${WO_TEST_UI_EXIT:-0}"\n')
        for code in ("0", "1"):
            with self.subTest(exit_code=code):
                before = set(self.root.glob('work-ops-check.*'))
                result = self.run_checks("--strict", "ui", WO_TEST_UI_EXIT=code)
                self.assertEqual(result.returncode, int(code), result.stdout + result.stderr)
                created = set(self.root.glob('work-ops-check.*')) - before
                self.assertEqual(len(created), 1)
                run = created.pop()
                self.assertEqual((run / 'career-captures/desktop.png').exists(), code == "1")
                self.assertTrue(list(run.glob('*.log')), 'text receipts remain in both cases')

    @unittest.skipUnless(sys.platform == "darwin", "the seatbelt abort is a macOS application registration failure")
    def test_seatbelt_sandbox_does_not_launch_a_browser(self):
        for var in ("CURSOR_SANDBOX", "CODEX_SANDBOX"):
            with self.subTest(var=var):
                self.trace.unlink(missing_ok=True)
                result = self.run_checks("ui", **{var: "seatbelt"})
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("were not started", result.stdout)
                self.assertIn("crash report", result.stdout)
                self.assertIn("No browser was launched", result.stdout)
                trace = self.trace.read_text() if self.trace.exists() else ""
                self.assertNotIn("smoke.mjs", trace)
                self.assertNotIn("webkit.launch", trace)
                self.assertIn("node --test", trace)

    def test_both_browsers_run_with_their_own_builds(self):
        result = self.run_checks("--strict", "ui")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok    browser smoke (desktop Chromium, iPhone WebKit)", result.stdout)
        self.assertIn("smoke.mjs browsers=chromium,webkit chromium=\n", self.trace.read_text())

    def test_a_preinstalled_chromium_runs_the_desktop_half_and_webkit_is_a_skip(self):
        browsers = self.preinstalled_chromium()
        result = self.run_checks("ui", WO_TEST_BROWSERS="", PLAYWRIGHT_BROWSERS_PATH=str(browsers))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ok    browser smoke (desktop Chromium at {browsers}/chromium)", result.stdout)
        self.assertIn("SKIP  browser smoke: iPhone WebKit", result.stdout)
        self.assertIn(f"smoke.mjs browsers=chromium chromium={browsers}/chromium", self.trace.read_text())

    def test_strict_mode_rejects_a_run_without_webkit(self):
        browsers = self.preinstalled_chromium()
        result = self.run_checks("--strict", "ui", WO_TEST_BROWSERS="chromium",
                                 PLAYWRIGHT_BROWSERS_PATH=str(browsers))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ok    browser smoke (desktop Chromium)", result.stdout)
        self.assertIn("SKIP  browser smoke: iPhone WebKit", result.stdout)
        self.assertIn("strict", result.stdout.lower())

    def test_empty_sandbox_playwright_path_falls_back_to_default_cache(self):
        empty_sandbox = self.root / "empty-sandbox-cache"
        empty_sandbox.mkdir()
        self.command(self.bin / "node",
                     'if [ "$1" = -e ]; then\n'
                     '  echo "node -e path=${PLAYWRIGHT_BROWSERS_PATH-unset}" >> "$WO_TEST_TRACE"\n'
                     '  if [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then exit 0; fi\n'
                     '  printf "chromium webkit\\n"\n'
                     '  exit 0\n'
                     'fi\n'
                     'printf "node %s path=${PLAYWRIGHT_BROWSERS_PATH-unset} browsers=%s\\n" "$*" "$WO_UI_BROWSERS" >> "$WO_TEST_TRACE"\n'
                     'exit 0\n')
        result = self.run_checks("--strict", "ui", PLAYWRIGHT_BROWSERS_PATH=str(empty_sandbox))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok    browser smoke (desktop Chromium, iPhone WebKit)", result.stdout)
        self.assertIn("smoke.mjs path=unset browsers=chromium,webkit", self.trace.read_text())

    def test_pytest_uses_the_worker_override(self):
        result = self.run_checks("unit", WO_PYTEST_WORKERS="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-m pytest -n 1", self.trace.read_text())

    def test_contract_passes_the_selected_database_to_its_distinct_checks(self):
        result = self.run_checks("--strict", "contract", WO_PYTEST_WORKERS="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text()
        self.assertIn("workspace/check_contract.py", trace)
        self.assertIn("contract=data/work-ops.db", trace)
        self.assertNotIn("-k ContractTests", trace)

    def test_stale_javascript_dependencies_fail_before_browser_execution(self):
        result = self.run_checks("--strict", "ui", WO_TEST_DEPS_EXIT="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # Dependency-backed comparator tests must not run against stale packages either.
        self.assertFalse(self.trace.exists())

    def test_browser_concurrency_is_bounded_and_vps_is_serial(self):
        for profile, expected in (("mac", "2"), ("vps", "1")):
            with self.subTest(profile=profile):
                result = self.run_checks("ui", WO_CHECK_PROFILE=profile, WO_UI_JOBS=expected if profile == 'mac' else '')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"Browser suites: {expected} at a time", result.stdout)
        result = self.run_checks("ui", WO_UI_JOBS="3")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        result = self.run_checks("ui", WO_CHECK_PROFILE="vps", WO_UI_JOBS="2")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_the_vps_uses_one_worker_and_installs_nothing(self):
        result = self.run_checks("unit", WO_CHECK_PROFILE="vps")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text()
        self.assertIn("-m pytest -n 1", trace)
        self.assertNotIn("pip", trace)

    def test_the_vps_runs_unittest_without_installing_when_pytest_is_absent(self):
        self.command(self.root / ".venv/bin/python",
                     'if [ "$1" = ops/local/check_runner.py ]; then\n'
                     '  if [ "$2" = preflight ]; then exit 0; fi\n'
                     f'  exec {shlex.quote(sys.executable)} "$@"\n'
                     'fi\n'
                     'printf "python %s\\n" "$*" >> "$WO_TEST_TRACE"\n'
                     'case "$*" in\n'
                     '  *pip*) exit 1;;\n'
                     '  -c*) exit 1;;\n'
                     '  *unittest*) exit 0;;\n'
                     'esac\n'
                     'exit 0\n')
        result = self.run_checks("unit", "contract", WO_CHECK_PROFILE="vps")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text()
        self.assertNotIn("pip", trace)
        self.assertIn("unittest discover -s tools", trace)
        self.assertIn("unittest discover -s workspace -p test_*.py", trace)
        self.assertIn("unittest discover -s ops/local", trace)
        self.assertIn("-m unittest discover -s workspace -p check_contract.py", trace)
        self.assertIn("pytest is not installed on this host", result.stdout)

    def test_an_edit_to_the_script_during_a_run_does_not_change_the_run(self):
        (self.bin / "cat").symlink_to(shutil.which("cat"))
        self.command(self.bin / "node",
                     'if [ "$1" = -e ]; then printf "chromium webkit\\n"; exit 0; fi\n'
                     'printf "node %s\\n" "$*" >> "$WO_TEST_TRACE"\n'
                     'body=$(cat "$WO_TEST_SCRIPT")\n'
                     'printf "# a line another session added while the checks ran\\n%s\\n" "$body" > "$WO_TEST_SCRIPT"\n')
        result = self.run_checks("--strict", "ui", WO_TEST_SCRIPT=str(self.script))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("syntax error", result.stderr)
        self.assertIn("career-ops.mjs", self.trace.read_text())

    def test_a_missing_venv_is_made_before_the_checks(self):
        (self.root / ".venv/bin/python").rename(self.root / "python.fake")
        self.command(self.root / "ops/make-venv.sh",
                     'mkdir -p .venv/bin && cp python.fake .venv/bin/python && echo "made .venv"\n')
        (self.bin / "cp").symlink_to(shutil.which("cp"))
        result = self.run_checks("contract")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("made .venv", result.stdout)
        self.assertIn("ok    contract", result.stdout)

    def test_a_venv_that_cannot_be_made_fails_before_any_check(self):
        (self.root / ".venv/bin/python").unlink()
        self.command(self.root / "ops/make-venv.sh", 'echo "no python here" >&2; exit 1\n')
        result = self.run_checks("contract")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL  no .venv", result.stdout)
        self.assertFalse(self.trace.exists())

    def test_test_failures_remain_failures_in_both_modes(self):
        for flags in ((), ("--strict",)):
            with self.subTest(flags=flags):
                result = self.run_checks(*flags, "ui", WO_TEST_UI_EXIT="1")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL  browser", result.stdout)

    def test_dependency_setup_uses_the_lockfile(self):
        self.package.rmdir()
        result = self.run_checks("ui")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("npm ci --prefix tests/ui", self.trace.read_text())
        self.assertNotIn("npm install", self.trace.read_text())

    def test_node_test_dependencies_are_ready_before_unit_checks(self):
        self.package.rmdir()
        result = self.run_checks("ui")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text()
        self.assertLess(trace.index("npm ci --prefix tests/ui"), trace.index("node --test"))

    def test_missing_node_does_not_attempt_dependency_installation(self):
        (self.bin / "node").unlink()
        self.package.rmdir()
        result = self.run_checks("ui")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIP", result.stdout)
        self.assertFalse(self.trace.exists())

    def test_strict_mode_rejects_dependency_install_failure(self):
        self.package.rmdir()
        result = self.run_checks("--strict", "ui", WO_TEST_NPM_EXIT="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SKIP", result.stdout)
        self.assertNotIn("smoke.mjs", self.trace.read_text())

    def test_unknown_suite_is_rejected_before_running_any_checks(self):
        result = self.run_checks("contract", "typo")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.trace.exists())

    def test_help_does_not_require_a_virtual_environment(self):
        (self.root / ".venv/bin/python").unlink()
        result = self.run_checks("--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--strict", result.stdout)

    def test_deployment_stops_before_ssh_when_browser_checks_are_skipped(self):
        deploy = self.root / "ops/vps/deploy.sh"
        deploy.parent.mkdir()
        shutil.copyfile(ROOT / "ops/vps/deploy.sh", deploy)
        self.script.chmod(0o755)
        # Simulate the revision snapshot boundary; its Git/archive behaviour has separate real-Git
        # tests. Here the strict check runner and the deploy's failure propagation are real.
        self.command(self.bin / "python3", 'exec /bin/bash "$2/ops/check.sh" --strict\n')
        self.command(self.bin / "git",
                     'case "$*" in\n'
                     '*" show "*) printf "decision #28\\n";;\n'
                     '*" rev-parse "*) printf "1234567890abcdef\\n";;\n'
                     '*" log -1 "*) printf "1234567 fixture commit\\n";;\n'
                     'esac\n')
        self.command(self.bin / "ssh",
                     'printf "UNEXPECTED SSH\\n" >> "$WO_TEST_TRACE"\nexit 99\n')
        result = subprocess.run(
            ["/bin/bash", str(deploy), "--local-approved"],
            capture_output=True, text=True, timeout=10,
            env={k: v for k, v in os.environ.items() if k not in {"CURSOR_SANDBOX", "CODEX_SANDBOX"}} | {
                "PATH": str(self.bin), "TMPDIR": str(self.root), "WO_TEST_TRACE": str(self.trace),
                "WO_TEST_BROWSERS": "", "PLAYWRIGHT_BROWSERS_PATH": "", "WO_UI_CHROMIUM": ""},
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SKIP", result.stdout)
        self.assertIn("nothing was deployed", result.stderr)
        self.assertNotIn("UNEXPECTED SSH", self.trace.read_text())


class MakeVenvTests(unittest.TestCase):
    """ops/make-venv.sh with fake interpreters: no real environment or network is used."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wo venv ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "commands"
        (self.root / "ops").mkdir()
        shutil.copyfile(ROOT / "ops/make-venv.sh", self.root / "ops/make-venv.sh")
        (self.root / "requirements.txt").write_text("PyYAML==6.0.3\n")
        (self.root / "requirements-dev.txt").write_text("-r requirements.txt\npytest==9.1.1\n")
        for name in ("bash", "dirname", "mkdir", "chmod", "cat"):
            (self.bin / name).symlink_to(shutil.which(name))
        maker = ('printf "%s %s\\n" "$(basename "$0")" "$*" >> "$WO_TEST_TRACE"\n'
                 'mkdir -p .venv/bin\n'
                 "cat > .venv/bin/python <<'EOF'\n"
                 '#!/bin/sh\n'
                 'case "$*" in -V) echo "Python 3.12.9";; *pip*) exit "${WO_TEST_PIP_EXIT:-0}";; esac\n'
                 'EOF\n'
                 'chmod +x .venv/bin/python\n')
        self.maker = maker
        (self.bin / "basename").symlink_to(shutil.which("basename"))

    def command(self, name, source):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + source)
        path.chmod(0o755)

    def make(self, **env):
        return subprocess.run(
            ["/bin/bash", str(self.root / "ops/make-venv.sh")], capture_output=True, text=True,
            timeout=10, cwd=self.bin,
            env={**os.environ, "PATH": str(self.bin), "WO_TEST_TRACE": str(self.trace), **env},
        )

    def test_makes_the_environment_with_python_3_12(self):
        self.command("python3.12", self.maker)
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("created .venv (Python 3.12.9) with requirements-dev.txt", result.stdout)
        self.assertIn("python3.12 -m venv .venv", self.trace.read_text())

    def test_an_existing_environment_is_left_alone_silently(self):
        self.command("python3.12", self.maker)
        self.make()
        self.trace.unlink()
        result = self.make()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(self.trace.exists())

    def test_uv_environments_include_pip(self):
        self.command("uv", self.maker)
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--seed", self.trace.read_text())

    def test_runtime_requirements_install_when_the_test_runner_cannot(self):
        self.command("python3.12", self.maker.replace(
            '*pip*) exit "${WO_TEST_PIP_EXIT:-0}";;',
            '*pip*) printf "%s\\n" "$*" >> "$WO_TEST_TRACE"; case "$*" in *requirements-dev.txt*) exit 1;; *) exit 0;; esac;;'))
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("with requirements.txt", result.stderr)
        self.assertIn("-r requirements-dev.txt", self.trace.read_text())
        self.assertIn("-r requirements.txt", self.trace.read_text())

    def test_a_failed_install_is_reported_and_fails(self):
        self.command("python3.12", self.maker)
        result = self.make(WO_TEST_PIP_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not install", result.stderr)


if __name__ == "__main__":
    unittest.main()
