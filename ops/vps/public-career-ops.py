#!/usr/bin/env python3
"""Install the authorised exact-path public pitch ingress; keep the private rule intact.
Run on the VPS with sudo python3 after deploying the static files.
"""
from pathlib import Path
import subprocess

CONFIG = Path('/etc/cloudflared/config.yml')
PUBLIC_RULE = '''  - hostname: work.example.com
    path: ^/career-ops(/|/(style\\.css|journey\\.js|icon\\.svg|career-ops-discussion-brief\\.txt))?$
    service: http://127.0.0.1:8769
'''
PRIVATE_RULE = '''  - hostname: work.example.com
    service: http://127.0.0.1:8769
    originRequest:
      access:
        required: true
'''

def install():
    before = CONFIG.read_text()
    if PUBLIC_RULE in before:
        print('Exact pitch ingress already installed')
        return
    if before.count(PRIVATE_RULE) != 1 or before.count('ingress:\n') != 1:
        raise SystemExit('Unexpected tunnel configuration; inspect before changing')
    after = before.replace(PRIVATE_RULE, PUBLIC_RULE + PRIVATE_RULE)
    backup = CONFIG.with_name('config.yml.before-career-ops')
    if backup.exists():
        raise SystemExit('Backup already exists; inspect before changing')
    backup.write_text(before)
    backup.chmod(0o600)
    try:
        CONFIG.write_text(after)
        subprocess.run(['cloudflared', 'tunnel', 'ingress', 'validate'], check=True)
        subprocess.run(['systemctl', 'restart', 'cloudflared'], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'cloudflared'], check=True)
    except BaseException:
        CONFIG.write_text(before)
        subprocess.run(['systemctl', 'restart', 'cloudflared'], check=False)
        raise
    print('Exact public pitch routes installed; private JWT rule preserved byte-for-byte')

if __name__ == '__main__':
    install()
