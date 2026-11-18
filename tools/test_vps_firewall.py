"""The loopback guard must add its own nftables table without disturbing UFW's firewall."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
VPS = ROOT / "ops/vps"


def loopback_step(text: str) -> str:
    match = re.search(r"^  workspace-loopback\)\n(.*?)^    ;;", text, re.M | re.S)
    if not match:
        raise AssertionError("workspace-loopback step not found in harden.sh")
    return match.group(1)


class LoopbackGuardFirewallTests(unittest.TestCase):
    def test_guard_unit_does_not_start_the_distribution_nftables_service(self):
        # On 23 October `Wants=nftables.service` started it for the first time; its
        # /etc/nftables.conf begins with `flush ruleset`, which removed every UFW rule.
        unit = (VPS / "work-ops-loopback-guard.service").read_text()
        for line in unit.splitlines():
            key, _, value = line.partition("=")
            if key.strip() in {"Wants", "Requires", "Requisite", "BindsTo", "PartOf", "Upholds", "After", "Before"}:
                self.assertNotIn("nftables.service", value, line)

    def test_guard_ruleset_touches_only_its_own_table(self):
        rules = [l.split("#", 1)[0].strip() for l in (VPS / "work-ops-loopback.nft").read_text().splitlines()]
        rules = [l for l in rules if l]
        self.assertFalse([l for l in rules if l.startswith("flush")], "the guard must not flush other rules")
        tables = [l for l in rules if re.match(r"^(add |delete )?table\b", l)]
        self.assertTrue(tables)
        for line in tables:
            self.assertRegex(line, r"\btable inet work-ops-loopback\b")

    def test_guard_step_reverts_unless_ufw_is_still_active(self):
        step = loopback_step((VPS / "harden.sh").read_text())
        self.assertIn("ufw status", step)
        verify = step[step.index("ufw status"):]
        self.assertIn("revert_loopback", verify)


if __name__ == "__main__":
    unittest.main()
