#!/usr/bin/env bash
# One-time, version-pinned preparation for approved decision 57.
# No launcher activation; keep Chromium 1234 for existing 1.62 consumers.
# See plans/2026-10-30-dependency-maintenance.md for evidence and rollback.
set -euo pipefail
old=/opt/codex-browsers/chromium-1234
new=/opt/codex-browsers/chromium-1248
profile=/etc/apparmor.d/codex-chromium-1248
url=https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux64/chrome-headless-shell-linux64.zip
sha=c3e50596aef52c58f8fda3c6b3411c33d34c88b177b3ed1c2243359fd3b8ba20
if [[ $(id -u) != 0 || -e "$new" || -e "$profile" ]]; then
  echo "Refusing: run as root only for an absent candidate runtime and profile." >&2
  exit 2
fi
[[ $(stat -c %U /opt/codex-browsers) == root ]]
stage=$(mktemp -d /opt/codex-browsers/.prepare-1248.XXXXXX)
trap 'rm -rf "$stage"' EXIT
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$url" -o "$stage/browser.zip"
printf '%s  %s\n' "$sha" "$stage/browser.zip" | sha256sum --check
python3 - "$stage" <<'PY'
from pathlib import Path
import hashlib,json,os,shutil,sys,zipfile
old=Path('/opt/codex-browsers/chromium-1234')
manifest=json.loads((old/'installation-manifest.json').read_text())
for name,item in manifest['files'].items():
 p=old/name
 if p.is_symlink(): assert os.readlink(p)==item.get('symlink',item.get('target')),name
 else: assert hashlib.sha256(p.read_bytes()).hexdigest()==item['sha256'],name
for p in [old,*old.rglob('*')]:
 s=p.lstat(); assert s.st_uid==0 and (p.is_symlink() or not s.st_mode & 0o022),str(p)
stage=Path(sys.argv[1]); target=stage/'runtime'; target.mkdir(mode=0o755)
with zipfile.ZipFile(stage/'browser.zip') as z:
 for item in z.infolist():
  p=Path(item.filename)
  assert not p.is_absolute() and '..' not in p.parts and p.parts[0]=='chrome-headless-shell-linux64'
  assert (item.external_attr>>16)&0o170000 != 0o120000,'Unexpected archive symlink'
  out=Path(z.extract(item,target))
  out.chmod(0o755 if out.is_dir() or (item.external_attr>>16)&0o111 else 0o644)
shutil.copytree(old/'runtime',target/'runtime',symlinks=True)
(target/'fonts.conf').write_text((old/'fonts.conf').read_text().replace('chromium-1234','chromium-1248'))
files={}
for p in target.rglob('*'):
 if p.is_symlink(): files[str(p.relative_to(target))]={'symlink':os.readlink(p)}
 elif p.is_file(): files[str(p.relative_to(target))]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
manifest.update(source_url='https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux64/chrome-headless-shell-linux64.zip',archive_sha256='c3e50596aef52c58f8fda3c6b3411c33d34c88b177b3ed1c2243359fd3b8ba20',playwright='1.64.1',retained_libraries_from=str(old),files=files)
manifest.pop('resolved_url',None)
(target/'installation-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(f'Prepared {len(files)} verified file/symlink entries; existing library provenance retained')
PY
mv "$stage/runtime" "$new"
cat > "$profile" <<'PROFILE'
# Approved decision 57: exact root-owned Playwright Chromium 1248 only.
abi <abi/5.0>,
include <tunables/global>
profile codex_chromium_1248 /opt/codex-browsers/chromium-1248/chrome-headless-shell-linux64/chrome-headless-shell flags=(unconfined) {
  userns,
  @{exec_path} mr,
}
PROFILE
chmod 644 "$profile"
apparmor_parser -r "$profile"
LD_LIBRARY_PATH="$new/runtime/usr/lib/x86_64-linux-gnu" "$new/chrome-headless-shell-linux64/chrome-headless-shell" --version
printf 'Prepared new protected runtime/profile; existing runtime and launch helper unchanged.\n'
