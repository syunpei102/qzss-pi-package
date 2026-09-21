"""DEVICE_VERIFICATION.md が実際のユニット名と非破壊な手順になっているかのテスト(#19)．"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOC = (ROOT / "DEVICE_VERIFICATION.md").read_text(encoding="utf-8")
ONBOARDING = (ROOT / "NEW_DEVICE_ONBOARDING.md").read_text(encoding="utf-8")
SETUP = (ROOT / "SETUP.md").read_text(encoding="utf-8")
TEMPLATES = ("qzss-map", "qzss-decoder", "qzss-kiosk")


class VerificationDocTests(unittest.TestCase):
    def test_onboarding_uses_current_template_names_and_report_interval(self):
        self.assertIn("qzss-map@<ユーザー名>.service", ONBOARDING)
        self.assertIn("qzss-decoder@<ユーザー名>.service", ONBOARDING)
        self.assertNotIn("1時間おきの状態報告", ONBOARDING)
        self.assertIn("5分おきの状態報告", ONBOARDING)
        self.assertNotIn("これは1時間おきに", SETUP)
        self.assertIn("これは5分おきに", SETUP)

    def test_template_units_are_never_named_without_the_at_sign(self):
        for line in DOC.splitlines():
            if "存在しない" in line:  # 「@なしの名前は存在しない」という注意書き自体は許可
                continue
            for name in TEMPLATES:
                self.assertIsNone(re.search(r"{}\.service".format(name), line), line)

    def test_every_unit_mentioned_exists_in_the_repository(self):
        existing = {p.name for p in (ROOT / "systemd").iterdir()}
        for unit in set(re.findall(r"\b(qzss-[a-z-]+(?:@[^\s.`\"]*)?\.(?:service|timer))", DOC)):
            base = unit.split("@")[0]
            candidate = unit if "@" not in unit else base + ".service"
            if unit == "qzss-cpu-performance.service":  # 削除確認のための言及
                continue
            self.assertIn(candidate, existing, unit)

    def test_rollback_procedure_uses_a_throwaway_clone_not_the_live_checkout(self):
        section = DOC[DOC.index("## 6.5"):DOC.index("## 6.6")]
        self.assertIn("mktemp -d", section)
        self.assertIn("git clone", section)
        self.assertIn("stub", section.lower())
        # 稼働中のcheckoutに対する破壊的操作の手順を含まない
        self.assertNotIn("~/qzss/qzss-map`で", section)
        for line in section.splitlines():
            if "--allow-empty" in line or "reset --hard" in line:
                if line.strip().startswith("**"):  # 「reset --hardもしない」という注意書き
                    continue
                self.assertIn('"$T/', line, line)  # 必ず一時cloneに対してのみ

    def test_new_operational_checks_are_documented(self):
        for keyword in ("オフライン", "15分", "qzss-reception-watch", "qzss-cpu-governor",
                        "ondemand", "127.0.0.1", "OnUnitActiveSec=5min"):
            self.assertIn(keyword, DOC, keyword)


if __name__ == "__main__":
    unittest.main()
