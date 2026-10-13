"""Portable entry point. All paths are computed from this file, not the cwd."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / 'vendor' / 'agnes-harness'
RUNTIME = HARNESS / 'packages' / 'cli' / 'dist' / 'local'
AGH_HOME = ROOT / '.runtime' / 'agh'
PYTHON = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def run(args, cwd=ROOT, env=None):
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)


def environment():
    env = os.environ.copy()
    env['AGH_HOME'] = str(AGH_HOME)
    env['AGNES_PROFILE'] = 'local-dev'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PATH'] = str(PYTHON.parent) + os.pathsep + env.get('PATH', '')
    return env


def require_python():
    if not PYTHON.is_file():
        raise SystemExit('Run python run.py setup --python-only first.')


def require_runtime():
    require_python()
    if not (RUNTIME / 'agnes.mjs').is_file():
        raise SystemExit('Run python run.py setup to build Agnes Harness first.')


def prepare_home():
    """Create Windows state through the upstream private-DACL primitive."""
    require_runtime()
    AGH_HOME.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        native = RUNTIME / 'node_modules/@agnes/system-node/dist/native/agnes-system.node'
        code = "const fs=require('fs'); const n=require(process.argv[1]); for(const p of process.argv.slice(2)){ if(fs.existsSync(p)) n.protectPrivateDirectory(p); else n.createPrivateDirectory(p); }"
        run(['node', '-e', code, native, AGH_HOME, AGH_HOME / 'profiles', AGH_HOME / 'profiles/local-dev'])
    else:
        for path in (AGH_HOME, AGH_HOME / 'profiles', AGH_HOME / 'profiles/local-dev'):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)


def configure():
    require_python()
    skills = ROOT / '.agh' / 'skills'
    skills.mkdir(parents=True, exist_ok=True)
    for name in ('fundflow-investigation', 'fundflow-verify', 'fundflow-range'):
        shutil.copytree(ROOT / 'agent/skills' / name, skills / name, dirs_exist_ok=True)
    def command(script):
        values = [str(PYTHON), '-X', 'utf8', str(ROOT / 'agent/hooks' / script)]
        if os.name == 'nt':
            return '& ' + ' '.join("'" + value.replace("'", "''") + "'" for value in values)
        return shlex.join(values)
    hooks = {'hooks': {
        'PostToolUse': [{'matcher': 'mcp_fundflow.*', 'hooks': [{'type': 'command', 'command': command('hook_tool_result_check.py'), 'timeout': 30}]}],
        'Stop': [{'hooks': [{'type': 'command', 'command': command('hook_turn_stopping_check.py'), 'timeout': 30}]}],
    }}
    (ROOT / '.agh/hooks.json').write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    params = {'mcpServers': {'fundflow': {'command': str(PYTHON), 'args': ['-X', 'utf8', str(ROOT / 'agent/mcp_server/server.py')]}}}
    (ROOT / '.agh/mcp.generated.json').write_text(json.dumps(params, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (ROOT / 'agent/deliverables').mkdir(parents=True, exist_ok=True)
    print('Generated local skills, hooks and MCP client parameters in .agh/.')


def setup(python_only, gui):
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('Use Python 3.12 for the tested installation (py -3.12 on Windows).')
    if not PYTHON.exists():
        run([sys.executable, '-m', 'venv', ROOT / '.venv'])
    run([PYTHON, '-m', 'pip', 'install', '-r', ROOT / ('requirements-gui.txt' if gui else 'requirements.txt')])
    configure()
    if python_only:
        return
    node = shutil.which('node')
    pnpm = shutil.which('pnpm.cmd' if os.name == 'nt' else 'pnpm')
    if not node or not pnpm:
        raise SystemExit('Install Node >=24.10 and pnpm 10.34.5, then rerun setup.')
    version = subprocess.check_output([node, '--version'], text=True).strip().lstrip('v').split('.')
    if tuple(map(int, version[:2])) < (24, 10):
        raise SystemExit('Node >=24.10 is required.')
    if subprocess.check_output([pnpm, '--version'], text=True).strip() != '10.34.5':
        raise SystemExit('Use pnpm 10.34.5 (npm install -g pnpm@10.34.5).')
    run([pnpm, 'install', '--frozen-lockfile'], cwd=HARNESS)
    if os.name == 'nt':
        header_cache = Path(os.environ['LOCALAPPDATA']) / 'node-gyp/Cache'
        run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
             HARNESS / '.github/scripts/prepare-windows-native.ps1',
             '-CacheRoot', header_cache], cwd=HARNESS)
        # A child PowerShell cannot export its environment back to Python.
        # Pass the prepared header location explicitly to the build process.
        build_env = os.environ.copy()
        build_env['AGNES_NODE_HEADERS'] = str(header_cache / '.'.join(version))
    else:
        build_env = None
    run([pnpm, '--filter', '@agnes/cli', 'build:local'], cwd=HARNESS, env=build_env)
    run([node, RUNTIME / 'agnes.mjs', '--help'], cwd=RUNTIME, env=environment())
    print('Ready: python run.py start')


def trust_hooks():
    # Explicit opt-in; only write this project's isolated profile, never ~/.agh.
    require_runtime()
    prepare_home()
    configure()
    hooks = ROOT / '.agh/hooks.json'
    digest = 'sha256-' + hashlib.sha256(hooks.read_bytes()).hexdigest()
    target = AGH_HOME / 'profiles/local-dev/profile.yaml'
    target.parent.mkdir(parents=True, exist_ok=True)
    boot = str(RUNTIME).replace("'", "''")
    content = (f'name: local-dev\ncommandHooks:\n  trustedUnconfined:\n'
               f'    - source: workspace\n      configDigest: {digest}\n      workspaceRoot: \'{boot}\'\n')
    if target.exists() and target.read_text(encoding='utf-8') != content:
        raise SystemExit(f'Existing profile differs; preserve it and merge commandHooks manually: {target}')
    target.write_text(content, encoding='utf-8')
    print('Trusted digest-bound hooks in isolated project profile. Restart the workbench.')


def verify():
    require_python()
    checks = ['agent/mcp_server/smoke.py', 'agent/mcp_server/p1_smoke.py',
              'agent/range/check_generator.py', 'agent/range/check_evaluate.py',
              'agent/mcp_server/p3_smoke.py', 'agent/mcp_server/p4_smoke.py',
              'agent/verify/report_check_selftest.py', 'agent/hooks/p4_hooks_smoke.py',
              'agent/mcp_server/protocol_smoke.py']
    for check in checks:
        print('CHECK ' + check, flush=True)
        run([PYTHON, '-X', 'utf8', ROOT / check], env=environment())
    print('All synthetic Python/MCP/hook checks passed. Live AGH/model approval is a separate check.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    install = sub.add_parser('setup')
    install.add_argument('--python-only', action='store_true')
    install.add_argument('--gui', action='store_true')
    sub.add_parser('configure')
    sub.add_parser('verify')
    sub.add_parser('trust-hooks')
    start = sub.add_parser('start')
    start.add_argument('--port', type=int, default=4177)
    cli = sub.add_parser('agh')
    cli.add_argument('arguments', nargs=argparse.REMAINDER)
    sub.add_parser('register-mcp')
    sub.add_parser('gui')
    args = parser.parse_args()
    if args.action == 'setup':
        setup(args.python_only, args.gui)
    elif args.action == 'configure':
        configure()
    elif args.action == 'verify':
        verify()
    elif args.action == 'trust-hooks':
        trust_hooks()
    elif args.action == 'gui':
        require_python()
        run([PYTHON, ROOT / 'mmfrisk_gui.py'])
    else:
        require_runtime()
        prepare_home()
        if args.action == 'start':
            cli_args = ['serve', '--profile', 'local-dev', '--port', str(args.port), '--cwd', str(RUNTIME)]
            print('In Web, select this project workspace: ' + str(ROOT), flush=True)
        elif args.action == 'register-mcp':
            cli_args = ['mcp', 'add', 'fundflow', '--name', 'fundflow', '--stdio', str(PYTHON),
                        '--arg', '-X', '--arg', 'utf8', '--arg', str(ROOT / 'agent/mcp_server/server.py')]
        else:
            cli_args = args.arguments
        run(['node', RUNTIME / 'agnes.mjs', *cli_args], cwd=RUNTIME, env=environment())


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
