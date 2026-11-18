from pathlib import Path
import plistlib
import unittest

import service


class OffsiteAgentTests(unittest.TestCase):
    def test_offsite_agent_runs_daily_after_the_vps_backup_and_scan(self):
        home = Path('/Users/Test Person')
        root = home / 'Projects/Work Ops'
        plist = plistlib.loads(plistlib.dumps(service.build_offsite_plist(root, home)))
        self.assertEqual(plist['Label'], 'com.macmini.workops-offsite')
        self.assertEqual(plist['ProgramArguments'], ['/bin/bash', str(root / 'ops/offsite-weekly.sh')])
        self.assertEqual(plist['StartCalendarInterval'], {'Hour': 7, 'Minute': 30})  # daily (decision #23)
        self.assertFalse(plist['RunAtLoad'])
        self.assertIn('/opt/homebrew/bin', plist['EnvironmentVariables']['PATH'])  # age and ssh
        self.assertTrue(plist['StandardOutPath'].endswith('offsite.log'))


if __name__ == '__main__':
    unittest.main()
