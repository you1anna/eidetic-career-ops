"""Real loopback SSH characterization: a restricted key must not reuse an admin master."""
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from ssh_route import ssh_command


class SSHIsolationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/sbin/sshd').exists() and shutil.which('ssh-keygen'),
                         'Mac SSH integration harness; VPS verification uses a non-login service account')
    def test_restricted_identity_with_existing_administrator_master(self):
        with tempfile.TemporaryDirectory(prefix='gateway-ssh-') as folder:
            root = Path(folder).resolve()
            for name in ('host', 'admin', 'restricted'):
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / name)], check=True)
            keys = root / 'authorized_keys'
            keys.write_text('command="printf administrator" ' + (root / 'admin.pub').read_text() +
                            'restrict,command="printf restricted" ' + (root / 'restricted.pub').read_text())
            known = root / 'known_hosts'
            known.write_text('synthetic-gateway ' + (root / 'host.pub').read_text())
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            config = root / 'sshd.conf'
            config.write_text(f'Port {port}\nListenAddress 127.0.0.1\nHostKey {root / "host"}\n'
                f'PidFile {root / "pid"}\nAuthorizedKeysFile {keys}\nStrictModes no\n'
                'PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n'
                'PermitRootLogin no\nAllowAgentForwarding no\nLogLevel ERROR\n')
            log = (root / 'sshd.log').open('w+')
            daemon = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)], stdout=log, stderr=log)
            master = None
            try:
                for _ in range(50):
                    if daemon.poll() is not None:
                        log.seek(0)
                        self.fail('synthetic sshd failed: ' + log.read())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.05)
                user = pwd.getpwuid(os.getuid()).pw_name
                host = user + '@127.0.0.1'
                control = root / 'master'
                base = ['ssh', '-F', '/dev/null', '-p', str(port), '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known),
                        '-o', 'HostKeyAlias=synthetic-gateway', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none']
                master = subprocess.Popen([*base, '-i', str(root / 'admin'), '-M', '-S', str(control), '-N', host],
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                for _ in range(100):
                    if control.exists():
                        break
                    if master.poll() is not None:
                        log.seek(0)
                        self.fail('synthetic admin master failed: ' + log.read())
                    time.sleep(.05)
                unsafe = subprocess.run([*base, '-i', str(root / 'restricted'), '-S', str(control), host, 'probe'],
                                        capture_output=True, timeout=10)
                self.assertEqual(unsafe.stdout, b'administrator', 'fixture did not reproduce connection reuse')
                route = dict(host=host, key=str(root / 'restricted'), host_key_alias='synthetic-gateway', restricted=True)
                safe = ssh_command(route, 'probe')
                safe[1:1] = ['-p', str(port), '-o', 'UserKnownHostsFile=' + str(known)]
                # An inherited later setting cannot override the capability's explicit refusal.
                safe[-2:-2] = ['-o', 'ControlPath=' + str(control)]
                result = subprocess.run(safe, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(result.stdout, b'restricted')
                forward = ssh_command(route, '')[:-1]
                forward[1:1] = ['-p', str(port), '-o', 'UserKnownHostsFile=' + str(known), '-W', '127.0.0.1:' + str(port)]
                denied = subprocess.run(forward, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
                self.assertEqual(denied.returncode, 255)
                self.assertIn(b'administratively prohibited', denied.stderr)
            finally:
                for process in (master, daemon):
                    if process is not None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait()
                log.close()


if __name__ == '__main__':
    unittest.main()
