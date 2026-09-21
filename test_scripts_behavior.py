"""シェルスクリプト・systemdユニットの挙動/構成の回帰テスト(実機・root不要)．"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SYSTEMD = ROOT / "systemd"


def text(name):
    return (ROOT / name).read_text(encoding="utf-8")


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kwargs)


class UnitHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.unit_dir = self.tmp.name
        self.env = {**os.environ, "QZSS_UNIT_DIR": self.unit_dir, "QZSS_UNIT_USER": "pi",
                    "QZSS_SYSTEMCTL": "true"}

    def tearDown(self):
        self.tmp.cleanup()

    def helper(self, *args):
        return run(["bash", str(ROOT / "qzss_unit_helper.sh"), *args], env=self.env)

    def test_install_renders_templates_and_users_and_is_idempotent(self):
        first = self.helper("install", str(SYSTEMD))
        self.assertEqual(first.returncode, 0, first.stderr)
        names = {line.split()[1] for line in first.stdout.splitlines() if line.startswith("changed")}
        self.assertIn("qzss-decoder@.service", names)
        self.assertIn("qzss-reception-watch.timer", names)
        self.assertIn("qzss-reception-watch.service", names)
        self.assertIn("qzss-cpu-governor.service", names)
        decoder = Path(self.unit_dir, "qzss-decoder@.service").read_text(encoding="utf-8")
        self.assertIn("qzss-map@%i.service", decoder)  # テンプレートは%iのまま
        watch = Path(self.unit_dir, "qzss-reception-watch.service").read_text(encoding="utf-8")
        self.assertIn("qzss-decoder@pi.service", watch)  # 通常ユニットは%iをユーザー名へ
        self.assertNotIn("%i", watch)
        second = self.helper("install", str(SYSTEMD))
        self.assertEqual(second.stdout, "")  # 変更なしなら何も出さない=daemon-reload不要
        self.assertEqual([p for p in os.listdir(self.unit_dir) if p.startswith(".")], [])

    def test_changed_unit_is_replaced_and_reported(self):
        self.helper("install", str(SYSTEMD))
        Path(self.unit_dir, "qzss-map@.service").write_text("[Unit]\nDescription=old\n")
        out = self.helper("install", str(SYSTEMD))
        self.assertEqual(out.stdout.strip(), "changed qzss-map@.service")

    def test_obsolete_performance_governor_unit_is_removed(self):
        Path(self.unit_dir, "qzss-cpu-performance.service").write_text("[Unit]\n")
        out = self.helper("install", str(SYSTEMD))
        self.assertIn("removed qzss-cpu-performance.service", out.stdout)
        self.assertFalse(Path(self.unit_dir, "qzss-cpu-performance.service").exists())

    def test_invalid_unit_aborts_before_anything_is_installed(self):
        src = Path(self.tmp.name, "src")
        src.mkdir()
        (src / "qzss-a.service").write_text("[Unit]\nDescription=a\n")
        (src / "qzss-b.service").write_text("no unit section\n")
        out = self.helper("install", str(src))
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(Path(self.unit_dir, "qzss-a.service").exists())  # 全か無か

    def test_foreign_names_and_symlinks_are_not_installed(self):
        src = Path(self.tmp.name, "src")
        src.mkdir()
        (src / "evil.service").write_text("[Unit]\n")
        (src / "qzss-ok.timer").write_text("[Unit]\n[Timer]\n")
        self.helper("install", str(src))
        self.assertFalse(Path(self.unit_dir, "evil.service").exists())
        self.assertTrue(Path(self.unit_dir, "qzss-ok.timer").exists())
        (src / "qzss-link.service").symlink_to("/etc/passwd")
        self.assertNotEqual(self.helper("install", str(src)).returncode, 0)

    def test_enable_rejects_bad_names(self):
        self.assertNotEqual(self.helper("enable", "../../etc/passwd").returncode, 0)
        self.assertNotEqual(self.helper("enable", "ssh.service").returncode, 0)

    def test_enable_accepts_changed_names_reported_by_install(self):
        out = self.helper("install", str(SYSTEMD))
        names = [line.split()[1] for line in out.stdout.splitlines() if line.startswith("changed")]
        result = self.helper("enable", *names)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repo_installs_only_in_root_mode_paths(self):
        script = text("qzss_unit_helper.sh")
        self.assertIn("/etc/systemd/system", script)
        self.assertIn('realpath "$home/qzss/qzss-pi-package/systemd"', script)


class CheckUrgentTests(unittest.TestCase):
    """check_urgent.sh を実際に動かし，updateの結果に応じたurgent_seenの更新を確認する．"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin = base / "origin.git"
        self.work = base / "pi"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.env = env
        run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], env=env)
        run(["git", "clone", "-q", str(self.origin), str(self.work)], env=env)
        (self.work / "URGENT_UPDATE").write_text("urgent 1\n")
        for name in ("check_urgent.sh", "lib_log.sh"):
            shutil.copy(ROOT / name, self.work / name)
        stub = self.work / "update_check.sh"
        stub.write_text('#!/bin/bash\necho ran >> "$(dirname "$0")/update_state/stub_calls"\nexit "${STUB_STATUS:-0}"\n')
        stub.chmod(0o755)
        run(["git", "checkout", "-q", "-b", "main"], cwd=self.work, env=env)
        run(["git", "add", "."], cwd=self.work, env=env)
        run(["git", "commit", "-q", "-m", "init"], cwd=self.work, env=env)
        run(["git", "push", "-q", "origin", "main"], cwd=self.work, env=env)
        self.state = self.work / "update_state"

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, status):
        return run(["bash", str(self.work / "check_urgent.sh")], cwd=self.work,
                   env={**self.env, "STUB_STATUS": str(status)})

    def calls(self):
        f = self.state / "stub_calls"
        return len(f.read_text().splitlines()) if f.exists() else 0

    def test_marker_is_recorded_only_after_successful_update(self):
        self.assertEqual(self.check(0).returncode, 0)
        self.assertEqual((self.state / "urgent_seen").read_text().strip(), "urgent 1")
        self.check(0)
        self.assertEqual(self.calls(), 1)  # 確認済みなら再実行しない

    def test_failed_update_does_not_mark_seen_and_backs_off(self):
        self.assertEqual(self.check(1).returncode, 1)
        self.assertFalse((self.state / "urgent_seen").exists())
        self.check(1)
        self.assertEqual(self.calls(), 1)  # バックオフ中は再試行しない
        # バックオフ時間を過ぎたら再試行され，成功すれば確認済みになる
        retry = (self.state / "urgent_retry").read_text().split()
        (self.state / "urgent_retry").write_text("{} {} 0\n".format(retry[0], retry[1]))
        self.assertEqual(self.check(0).returncode, 0)
        self.assertEqual(self.calls(), 2)
        self.assertTrue((self.state / "urgent_seen").exists())

    def test_busy_ota_is_not_a_failure_and_is_retried_next_time(self):
        self.assertEqual(self.check(75).returncode, 75)
        self.assertFalse((self.state / "urgent_seen").exists())
        self.assertFalse((self.state / "urgent_retry").exists())
        self.check(0)
        self.assertEqual(self.calls(), 2)


class ConfigurationTests(unittest.TestCase):
    def test_no_unit_forces_performance_governor(self):
        self.assertFalse((SYSTEMD / "qzss-cpu-performance.service").exists())
        for unit in SYSTEMD.iterdir():
            self.assertNotIn("echo performance", unit.read_text(encoding="utf-8"), unit.name)
        installer = text("install_services.sh")
        self.assertNotIn("qzss-cpu-performance", installer)
        self.assertIn("qzss-cpu-governor", installer)
        self.assertNotIn("performance", text("cpu_governor.sh").split("GOVERNOR=")[1].splitlines()[0])

    def test_map_service_listens_on_loopback_only(self):
        self.assertIn("Environment=HOST=127.0.0.1", (SYSTEMD / "qzss-map.service").read_text(encoding="utf-8"))
        self.assertIn("HOST=127.0.0.1", text("start_pi_local.sh"))

    def test_polling_intervals_are_not_wasteful(self):
        health = (SYSTEMD / "qzss-cloud-health-check.timer").read_text(encoding="utf-8")
        self.assertNotIn("OnUnitActiveSec=30s", health)
        self.assertRegex(health, r"OnUnitActiveSec=(\d+)min")
        # リモートコマンドの最大遅延は1時間ではなく数分
        status = (SYSTEMD / "qzss-report-status.timer").read_text(encoding="utf-8")
        minutes = int(re.search(r"OnUnitActiveSec=(\d+)min", status).group(1))
        self.assertLessEqual(minutes, 5)

    def test_cloud_health_check_does_not_log_every_run(self):
        script = text("cloud_health_check.sh")
        self.assertNotIn("チェック実行", script)
        self.assertIn("rotate_log", script)

    def test_growing_logs_are_rotated(self):
        for name in ("update_check.sh", "report_status.sh", "cloud_health_check.sh", "check_urgent.sh"):
            self.assertIn("rotate_log", text(name), name)

    def test_report_status_does_not_roll_back_while_ota_runs(self):
        script = text("report_status.sh")
        self.assertIn('flock -n 8', script)
        self.assertIn("update.lock", script)

    def test_update_check_exits_with_busy_code_when_locked(self):
        script = text("update_check.sh")
        self.assertIn("flock -n 9", script)
        self.assertIn("exit 75", script)

    def test_ota_syncs_units_through_the_helper_with_daemon_reload_and_enable(self):
        script = text("update_check.sh")
        self.assertIn("sync_systemd_units", script)
        self.assertIn('"$UNIT_HELPER" install', script)
        self.assertIn('"$UNIT_HELPER" enable', script)
        # ロールバック時もユニットを元へ戻す
        rollback = script[script.index('rollback_repo "$PI_DIR"'):]
        self.assertIn("sync_systemd_units", rollback)

    def test_sudoers_allows_only_the_helper_and_daemon_reload_for_units(self):
        installer = text("install_services.sh")
        self.assertIn("/usr/local/sbin/qzss-unit-helper", installer)
        self.assertIn("/usr/bin/systemctl daemon-reload", installer)
        self.assertNotIn("/bin/cp", installer.split("SUDOERS_LINE=")[1].splitlines()[0])

    def test_all_shell_scripts_have_valid_syntax(self):
        for script in ROOT.glob("*.sh"):
            result = run(["bash", "-n", str(script)])
            self.assertEqual(result.returncode, 0, "{}: {}".format(script.name, result.stderr))

    def test_every_installed_unit_is_covered_by_the_installer_or_helper(self):
        # systemd/ の全ユニットは helper が配置する。timerはinstallerでenableされていること
        installer = text("install_services.sh")
        for timer in SYSTEMD.glob("*.timer"):
            self.assertIn('enable --now "{}"'.format(timer.name), installer, timer.name)


if __name__ == "__main__":
    unittest.main()
