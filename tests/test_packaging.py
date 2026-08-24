import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.package_common import resolve_account_key


ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_release_builder_accepts_an_explicit_account_key(self):
        with TemporaryDirectory() as directory:
            missing_token = Path(directory) / "missing-token"
            self.assertEqual(resolve_account_key(ROOT, missing_token, "ABCDEF012345"), "abcdef012345")

    def test_release_builder_rejects_an_invalid_account_key(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                resolve_account_key(ROOT, Path(directory) / "missing-token", "not-a-key")

    def test_linux_installer_is_current_user_only(self):
        script = (ROOT / "packaging" / "install.sh").read_text()
        self.assertNotIn("sudo ", script)
        self.assertIn("systemctl --user", script)
        self.assertIn("install -m 600", script)
        self.assertIn("systemctl --user restart", script)

    def test_windows_installer_is_non_admin_and_persistent(self):
        script = (ROOT / "packaging" / "windows" / "install.ps1").read_text()
        self.assertIn("-RunLevel Limited", script)
        self.assertNotIn("-RunLevel Highest", script)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", script)
        self.assertIn("icacls.exe", script)
        self.assertIn("pythonw.exe", script)
        self.assertIn("winget.exe", script)
        self.assertIn("--scope user", script)
        self.assertLess(script.index("Stop-ScheduledTask"), script.index("Copy-Item"))

    def test_windows_machine_installer_requests_admin_and_runs_as_system(self):
        script = (ROOT / "packaging" / "windows" / "installer.nsi").read_text()
        task_script = (ROOT / "packaging" / "windows" / "register_machine_task.ps1").read_text()
        self.assertIn("RequestExecutionLevel admin", script)
        self.assertIn('New-ScheduledTaskPrincipal -UserId "SYSTEM"', task_script)
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", task_script)
        self.assertIn("Register-ScheduledTask", task_script)
        self.assertIn("--account-key", task_script)
        self.assertIn("--profiles-root", task_script)
        self.assertIn("ProfilesDirectory", task_script)
        self.assertIn("--state-file", task_script)
        self.assertIn("--log-max-bytes", task_script)
        self.assertIn("--log-backups", task_script)
        self.assertNotIn("schtasks.exe /Create", script)
        self.assertIn("collector_windows_machine.py", script)
        self.assertIn("register_machine_task.ps1", script)
        self.assertIn("Sysnative\\WindowsPowerShell", script)
        self.assertIn("advfirewall firewall add rule", script)
        self.assertIn("SetRegView 64", script)
        self.assertLess(script.index('schtasks.exe /End /TN "CodexUsageCollectorMachine"'), script.index('File /r "${RUNTIME_DIR}\\*.*"'))

    def test_release_checks_live_repository_visibility(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn('gh api "repos/$GITHUB_REPOSITORY" --jq .visibility', workflow)
        self.assertNotIn("github.event.repository.private", workflow)


if __name__ == "__main__":
    unittest.main()
