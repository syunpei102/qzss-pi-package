"""スクリプトが実行する `sudo ...` が，install_services.sh のsudoers定義と完全一致するかのテスト(#17)．

sudoersは引数を文字列として完全一致で照合する(qzss-map@pi と qzss-map@pi.service は別物)．
一致しないとNOPASSWDが効かず，OTA・自動復旧・キオスク再起動が実機で静かに失敗する．
"""
import re
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USER = "pi"
HELPER = "/usr/local/sbin/qzss-unit-helper"
# sudoersの管理対象外(installerの対話実行など，パスワードありで動くもの)
SKIP_SCRIPTS = {"install_services.sh"}
NON_SYSTEMCTL_TOOLS = {"install", "chmod", "visudo", "sed", "tee"}


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def sudoers_entries():
    line = next(l for l in read("install_services.sh").splitlines() if l.startswith("SUDOERS_LINE="))
    body = line.split("NOPASSWD:", 1)[1].rstrip('"')
    entries = []
    for cmd in body.split(", "):
        cmd = cmd.replace("$USER_NAME", USER).strip()
        entries.append(shlex.split(cmd))
    return entries


def normalise(text):
    for var in ("$(whoami)", "${SERVICE_USER}", "$USER_NAME", "${USER_NAME}"):
        text = text.replace(var, USER)
    return text.replace("$UNIT_HELPER", HELPER).replace("${UNIT_HELPER}", HELPER)


def heal_units():
    """report_status.sh の check_and_heal_service に渡すユニット名(.service付き)"""
    return [normalise(m) for m in re.findall(r'check_and_heal_service "([^"]+)"', read("report_status.sh"))
            if not m.startswith("$")]


def sudo_invocations(script):
    calls = []
    for raw in read(script).splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or stripped.startswith("echo") or "sudo" not in stripped:
            continue
        m = re.search(r'\bsudo\s+(?:-n\s+)?(.*)$', stripped)
        if not m:
            continue
        rest = re.split(r'\s*(?:;|\|\||&&|\||2>|>)', m.group(1))[0].strip()
        rest = normalise(rest)
        variants = [rest]
        if "${unit%.service}" in rest:
            variants = [rest.replace("${unit%.service}", u[:-len(".service")]) for u in heal_units()]
        for v in variants:
            calls.append((script, raw.strip(), shlex.split(v)))
    return calls


def is_allowed(tokens, entries):
    cmd = list(tokens)
    if cmd[0] in ("systemctl", "modprobe"):
        cmd[0] = {"systemctl": "/usr/bin/systemctl", "modprobe": "/sbin/modprobe"}[cmd[0]]
    for entry in entries:
        if len(entry) == 1 and cmd[0] == entry[0]:
            return True  # 引数指定なしのエントリは任意の引数を許可(qzss-unit-helper)
        if cmd == entry:
            return True
    return False


class SudoersAlignmentTests(unittest.TestCase):
    def test_every_sudo_call_in_runtime_scripts_matches_a_sudoers_entry_exactly(self):
        entries = sudoers_entries()
        checked = 0
        for script in sorted(p.name for p in ROOT.glob("*.sh")):
            if script in SKIP_SCRIPTS:
                continue
            for name, line, tokens in sudo_invocations(script):
                if tokens[0] in NON_SYSTEMCTL_TOOLS:
                    continue
                checked += 1
                self.assertTrue(is_allowed(tokens, entries),
                                "{}: sudoersに完全一致する定義がありません: {}\n  {}".format(name, tokens, line))
        self.assertGreater(checked, 8)  # 抽出が空振りして通ってしまわないこと

    def test_report_status_uses_no_dot_service_suffix_in_sudo_restarts(self):
        for name, line, tokens in sudo_invocations("report_status.sh"):
            if "restart" in tokens:
                self.assertFalse(any(t.endswith(".service") for t in tokens), line)

    def test_kiosk_and_ota_restart_paths_are_covered(self):
        entries = sudoers_entries()
        for script in ("kiosk_watchdog.sh", "kiosk_daily_reload.sh", "update_check.sh"):
            restarts = [t for _, _, t in sudo_invocations(script) if "restart" in t]
            self.assertTrue(restarts, script)
            for tokens in restarts:
                self.assertTrue(is_allowed(tokens, entries), (script, tokens))

    def test_ota_unit_sync_commands_are_allowed(self):
        entries = sudoers_entries()
        self.assertIn(["/usr/bin/systemctl", "daemon-reload"], entries)
        self.assertTrue(is_allowed([HELPER, "install", "/x"], entries))
        self.assertTrue(is_allowed([HELPER, "enable", "qzss-a.timer"], entries))
        self.assertTrue(is_allowed(["/usr/bin/systemctl", "reboot"], entries))

    def test_checker_detects_a_mismatch(self):
        entries = sudoers_entries()
        self.assertFalse(is_allowed(["systemctl", "restart", "qzss-kiosk@pi.service"], entries))
        self.assertFalse(is_allowed(["systemctl", "restart", "qzss-map@pi.service", "qzss-decoder@pi.service"], entries))
        self.assertTrue(is_allowed(["systemctl", "restart", "qzss-map@pi", "qzss-decoder@pi"], entries))


if __name__ == "__main__":
    unittest.main()
