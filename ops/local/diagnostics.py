"""Mac diagnostics client. A dedicated restricted route is mandatory."""
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from ops_policy import diagnostic_request
from ssh_route import load_route, ssh_command, Refused


def main(argv=None, run=subprocess.run):
    args = sys.argv[1:] if argv is None else argv
    try:
        diagnostic_request(args)
        route = load_route('diagnostics-gateway.json')
        return run(ssh_command(route, shlex.join(['wo-diagnostics', *args])),
                   stdin=subprocess.DEVNULL, timeout=45).returncode
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print('wo-diagnostics: unavailable: ' + str(exc), file=sys.stderr)
        return 64


if __name__ == '__main__':
    sys.exit(main())
