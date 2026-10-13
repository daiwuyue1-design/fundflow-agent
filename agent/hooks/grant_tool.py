"""Preview a digest-bound hook grant; --boot-root must be explicitly provided."""
import argparse
import hashlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
HOOKS = PROJECT / '.agh' / 'hooks.json'
DEFAULT_PROFILE_FILE = Path.home() / '.agh' / 'profiles' / 'local-dev' / 'profile.yaml'
DEFAULT_BOOT_ROOT = None

TEMPLATE = """# AGH user 层配置（local-dev profile）— hooks 受信授权（由 grant_tool.py 生成/更新）
# grant.workspaceRoot = Host 装配 boot 根；configDigest = <项目>/.agh/hooks.json 字节 sha256。
# 维护：hooks.json 变更后重新运行 grant_tool.py --write 并重启工作台。回滚：删除本文件+重启。
name: local-dev
commandHooks:
  trustedUnconfined:
    - source: workspace
      configDigest: {digest}
      workspaceRoot: '{boot_root}'
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='写入部署文件（默认仅预览）')
    parser.add_argument('--boot-root', default=None, help='Host 装配 boot 根（运行时 dist/local 的绝对路径；必填）')
    parser.add_argument('--profile-file', default=str(DEFAULT_PROFILE_FILE), help='授权部署文件路径')
    args = parser.parse_args()

    if not HOOKS.is_file():
        print(f'[ERR] 未找到 {HOOKS}（先部署 hooks.json）')
        return 1
    if not args.boot_root or not Path(args.boot_root).is_absolute():
        parser.error('--boot-root 必须是 Agnes Harness dist/local 的绝对路径')
    digest = 'sha256-' + hashlib.sha256(HOOKS.read_bytes()).hexdigest()
    content = TEMPLATE.format(digest=digest, boot_root=args.boot_root)

    print(f'hooks.json : {HOOKS}')
    print(f'digest     : {digest}')
    print(f'boot root  : {args.boot_root}')
    if args.write:
        target = Path(args.profile_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        print(f'[OK] 已写入 {target}')
        print('NEXT: restart the workbench and verify hook execution.')
    else:
        print('--- 预览（未落盘；加 --write 应用） ---')
        print(content)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
