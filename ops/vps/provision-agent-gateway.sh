#!/usr/bin/env bash
# Explicitly authorized access provisioning only. No deployment, migrations or service restart.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
SSH=(ssh -i "${SSH_KEY:-$HOME/.ssh/work_ops_vps}" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=yes "${SSH_USER:-ops}@${SERVER_HOST:-100.64.0.10}")
python3 - <<'LOCAL' | "${SSH[@]}" sudo -n /usr/bin/python3 -
from pathlib import Path
print('SOURCE = ' + repr(Path('ops/vps/wo_agent_gateway.py').read_bytes()))
print(r'''
import json, os, pathlib, pwd, re, subprocess
os.umask(0o077)
codex = pwd.getpwnam('codex')
ops = pwd.getpwnam('ops')
if codex.pw_dir != '/home/codex' or ops.pw_dir != '/home/ops':
    raise SystemExit('Unexpected account homes; inspect before provisioning')
host_key = subprocess.check_output(['ssh-keygen', '-lf', '/etc/ssh/ssh_host_ed25519_key.pub'], text=True)
if 'SHA256:examplefingerprintreplacewithyourownkeyAAAAA' not in host_key:
    raise SystemExit('Host fingerprint changed; stop and verify independently')

# Key generation and route writes run as codex, never root in a user-writable tree.
account_setup = r"""
import json, os, pathlib, subprocess
os.umask(0o077)
home = pathlib.Path.home()
ssh = home / '.ssh'
ssh.mkdir(mode=0o700, exist_ok=True)
ssh.chmod(0o700)
key = ssh / 'workops_live'
if not key.exists():
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                    'work-ops VPS codex live gateway', '-f', str(key)], check=True)
key.chmod(0o600)
config = home / '.config/work-ops/live-gateway.json'
config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
route = dict(host='ops@127.0.0.1', key=str(key), host_key_alias='203.0.113.10', restricted=True)
if config.exists() and json.loads(config.read_text()) != route:
    raise SystemExit('Existing gateway configuration differs; inspect before replacing')
config.write_text(json.dumps(route, indent=2) + '\n')
config.chmod(0o600)
"""
subprocess.run(['runuser', '-u', 'codex', '--', '/usr/bin/python3', '-c', account_setup], check=True)
pub = subprocess.check_output(['runuser', '-u', 'codex', '--', 'cat',
                               '/home/codex/.ssh/workops_live.pub'], text=True).strip()
if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+ work-ops VPS codex live gateway', pub):
    raise SystemExit('Unexpected public key format')

directory = pathlib.Path('/usr/local/lib/work-ops')
if directory.is_symlink(): raise SystemExit('Trusted directory is a symlink')
directory.mkdir(mode=0o755, exist_ok=True)
os.chown(directory, 0, 0)
directory.chmod(0o755)
target = directory / 'wo_agent_gateway.py'
if target.is_symlink(): raise SystemExit('Trusted helper is a symlink')
temporary = directory / '.wo_agent_gateway.new'
with temporary.open('xb') as stream: stream.write(SOURCE)
os.chown(temporary, 0, 0)
temporary.chmod(0o644)
temporary.replace(target)

forced = 'sudo -n -u workops --preserve-env=SSH_ORIGINAL_COMMAND /usr/bin/python3 -I ' + str(target)
entry = 'from="127.0.0.1",restrict,command="' + forced + '" ' + pub
authorized = pathlib.Path('/home/ops/.ssh/authorized_keys')
if authorized.is_symlink(): raise SystemExit('Authorized keys is a symlink')
lines = authorized.read_text().splitlines()
keydata = pub.split()[1]
existing = [line for line in lines if keydata in line.split()]
if existing and existing != [entry]:
    raise SystemExit('Key already has different authorization; inspect before replacing')
if not existing:
    with authorized.open('a') as stream:
        if lines: stream.write('\n')
        stream.write(entry + '\n')
os.chown(authorized, ops.pw_uid, ops.pw_gid)
authorized.chmod(0o600)
print('Restricted gateway installed; other keys and sshd settings preserved.')
subprocess.run(['ssh-keygen', '-lf', '/home/codex/.ssh/workops_live.pub'], check=True)
''')
LOCAL
