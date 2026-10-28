#!/usr/bin/env python3
"""Container command dispatcher; exec keeps signals and exit codes intact."""
import os
from pathlib import Path
import sys
from runtime_paths import data_dir

SCRIPTS = {
    'web': 'webui.py', 'daemon': 'contest_daemon.py', 'solve': 'oj_solver.py',
    'contest': 'contest_solver.py', 'benchmark': 'benchmark_solver.py',
    'testdata': 'testdata_supplement.py', 'onebot': 'onebot_server.py',
    'config': 'config_cli.py', 'init': 'config_cli.py', 'doctor': 'config_cli.py',
}


def command_args(args):
    args = list(args or ['daemon', '--no-interactive'])
    name, rest = args[0], args[1:]
    if name in ('python', 'python3'):
        return [sys.executable, *rest]
    script = SCRIPTS.get(name)
    if script is None:
        raise ValueError('未知命令，可用: ' + ', '.join(SCRIPTS))
    if name in ('init', 'doctor'):
        rest.insert(0, name)
    if name == 'daemon' and '--no-interactive' not in rest:
        rest.append('--no-interactive')
    return [sys.executable, '-u', str(Path(__file__).with_name(script)), *rest]


def main():
    try:
        command = command_args(sys.argv[1:])
        directory = data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.chdir(directory)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    os.execv(command[0], command)


if __name__ == '__main__':
    raise SystemExit(main())
