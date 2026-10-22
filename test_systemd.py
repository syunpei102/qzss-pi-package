"""systemdへ配置すべき運用監視がinstallerから脱落しないための回帰テスト．"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


class SystemdInstallationTests(unittest.TestCase):
    def test_wifi_recovery_runs_at_boot_and_periodically(self):
        service = (ROOT / 'systemd' / 'qzss-wifi-recovery.service').read_text(encoding='utf-8')
        timer = (ROOT / 'systemd' / 'qzss-wifi-recovery.timer').read_text(encoding='utf-8')
        installer = (ROOT / 'install_services.sh').read_text(encoding='utf-8')
        script = (ROOT / 'wifi_recovery_check.sh').read_text(encoding='utf-8')
        self.assertIn('wifi_recovery_check.sh', service)
        self.assertIn('OnUnitActiveSec=5min', timer)
        self.assertIn('enable --now "qzss-wifi-recovery.timer"', installer)
        self.assertIn('exit 1', script)

    def test_wifi_recovery_reports_failure_and_success(self):
        source = (ROOT / 'wifi_recovery_check.sh').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            (temp / 'wifi_recovery_check.sh').write_text(source, encoding='utf-8')
            mock_bin = temp / 'bin'
            mock_bin.mkdir()
            for name, body in {
                'ip': '#!/bin/sh\n[ "${WLAN_PRESENT:-0}" = 1 ]\n',
                'sudo': '#!/bin/sh\nexit 0\n',
                'sleep': '#!/bin/sh\nexit 0\n',
            }.items():
                path = mock_bin / name
                path.write_text(body, encoding='utf-8')
                path.chmod(0o755)
            env = {**os.environ, 'PATH': f'{mock_bin}:{os.environ["PATH"]}'}
            failed = subprocess.run(['bash', str(temp / 'wifi_recovery_check.sh')], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertIn('復旧しませんでした', failed.stdout)
            healthy = subprocess.run(['bash', str(temp / 'wifi_recovery_check.sh')],
                                     env={**env, 'WLAN_PRESENT': '1'}, capture_output=True,
                                     text=True, timeout=10)
            self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)

    def test_decoder_depends_on_the_matching_map_instance(self):
        decoder = (ROOT / 'systemd' / 'qzss-decoder.service').read_text(encoding='utf-8')
        self.assertIn('After=network.target qzss-map@%i.service', decoder)
        self.assertIn('Wants=qzss-map@%i.service', decoder)
        self.assertNotIn('qzss-map.service', decoder)

    def test_reception_watch_units_exist_and_are_enabled_by_installer(self):
        service = ROOT / 'systemd' / 'qzss-reception-watch.service'
        timer = ROOT / 'systemd' / 'qzss-reception-watch.timer'
        self.assertTrue(service.is_file(), 'reception watch service unit is missing')
        self.assertTrue(timer.is_file(), 'reception watch timer unit is missing')

        service_text = service.read_text(encoding='utf-8')
        timer_text = timer.read_text(encoding='utf-8')
        installer = (ROOT / 'install_services.sh').read_text(encoding='utf-8')
        self.assertIn('reception_watch.py', service_text)
        self.assertIn('OnUnitActiveSec=30s', timer_text)
        self.assertIn('qzss-reception-watch.service', installer)
        self.assertIn('enable --now "qzss-reception-watch.timer"', installer)


if __name__ == '__main__':
    unittest.main()
