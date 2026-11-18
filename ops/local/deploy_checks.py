"""Check one committed revision in private local scratch; never copy local data or deploy.

The archive owns all source/test inputs. Only installed dependency directories are shared, and
their declared inputs must match the selected revision before and after the run. A rollback with
different pins needs a checkout with those dependencies prepared first, not a misleading pass.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


DEPENDENCY_INPUTS = ('.python-version', 'requirements.txt', 'requirements-dev.txt',
                     'tests/ui/package.json', 'tests/ui/package-lock.json',
                     'ops/email-intake/package.json', 'ops/email-intake/package-lock.json')
DEPENDENCIES = ('.venv', 'tests/ui/node_modules', 'ops/email-intake/node_modules')


def dependency_inputs_match(repo: Path, snapshot: Path) -> None:
    for name in DEPENDENCY_INPUTS:
        original, candidate = repo / name, snapshot / name
        have = original.read_bytes() if original.is_file() else None
        want = candidate.read_bytes() if candidate.is_file() else None
        if have != want:
            raise ValueError(f'dependency inputs differ for {name}; prepare the selected revision and its '
                             'dependencies in a separate checkout before deploying it')


def run(repo: Path, revision: str) -> int:
    repo = repo.resolve()
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--verify',
                                        f'{revision}^{{commit}}'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='work-ops-revision.') as temporary:
        root = Path(temporary)
        snapshot = root / 'source'
        snapshot.mkdir(mode=0o700)
        archive = root / 'source.tar'
        # Full committed tree, locally only: includes test config, network guards and served assets.
        subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar',
                        f'--output={archive}', revision], check=True)
        subprocess.run(['tar', '-xf', str(archive), '-C', str(snapshot)], check=True)
        archive.unlink()
        dependency_inputs_match(repo, snapshot)
        for name in DEPENDENCIES:
            source, target = repo / name, snapshot / name
            if not source.is_dir():
                raise ValueError(f'missing installed dependencies: {name}; follow ops/LOCAL.md before deploying')
            if target.exists() or target.is_symlink():
                raise ValueError(f'dependency directory must not be committed: {name}')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source, target_is_directory=True)
        # An empty repository lets the existing git-ignore assertion run, without Git history.
        subprocess.run(['git', '-C', str(snapshot), 'init', '-q', '--initial-branch=main'], check=True)
        env = dict(os.environ)
        for name in ('WO_DB', 'WO_CONTRACT_DB', 'WO_UI_DB', 'PYTHONPATH', 'PYTHONHOME'):
            env.pop(name, None)
        print(f'Checking committed revision {revision} (private local snapshot; synthetic data)', flush=True)
        code = subprocess.run([str(snapshot / 'ops/check.sh'), '--strict'], cwd=snapshot, env=env).returncode
        if code == 0:
            dependency_inputs_match(repo, snapshot)
        return code


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: deploy_checks.py REPOSITORY REVISION')
    try:
        sys.exit(run(Path(sys.argv[1]), sys.argv[2]))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'deploy checks refused: {error}')
