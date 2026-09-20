"""systemdへ配置すべき運用監視がinstallerから脱落しないための回帰テスト．"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class SystemdInstallationTests(unittest.TestCase):
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
