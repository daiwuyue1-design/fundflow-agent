# 新环境发布验证（2026-10-13）

在独立发布副本中新建 Python 虚拟环境，从网络安装依赖；Node 也独立按锁文件安装，未复用原项目环境。检查仅使用脚本生成的虚构账单，没有打开、扫描或复制真实流水目录。

环境：Windows x64，Python 3.12.10，Node 24.14.0，pnpm 10.34.5。

| 检查 | 结果 |
| --- | --- |
| `run.py setup --python-only` | PASS，新建 venv 并安装全部声明依赖 |
| `pip check` | PASS |
| `pnpm install --frozen-lockfile` | PASS，47 workspace projects / 468 packages |
| Windows Node headers + `@agnes/cli build:local` | PASS，原生模块及完整 Web/CLI distribution 构建成功 |
| CLI `--help` | PASS |
| AGH MCP 注册、信任、启用及工具目录 | PASS，actual=ready，列出 11 个工具 |
| `run.py start --port 4197` | PASS，隔离目录启动；HTTP GET / 返回 200 |
| `run.py verify` | PASS，9 个合成检查脚本全部通过 |
| stdio MCP initialize/list_tools/11 工具实际调用 | PASS，日志未污染协议、dataset_id 跨调用可用 |
| hooks 子进程 15 用例 | PASS，最大耗时约 0.24 秒；G1/G2 同门仅提醒一次 |
| 报告验收负例自检 | PASS，运行时生成合成报告后检查数值、证据、表述及空文件 |
| 单人查询 `--help` | PASS，支持显式 `--data-dir` |

已修复的移植问题：本机 hooks 命令绝对路径、固定 Host boot 根、Python 依赖不全、单人查询默认真实输入路径、报告自检依赖旧 dataset ID、协议测试限定项目虚拟环境路径、Windows 子 PowerShell 设置的 headers 环境无法传回父进程、AGH 状态目录继承宽权限导致后台拒绝启动。

原生构建输出上游第三方包缺 LICENSE 文件提示及 SQLite experimental 提示；构建退出码为 0，原有上游 LICENSE/NOTICE 保留。没有修改上游源码或关闭其目录权限验证。

没有使用模型账号发起真实推理；Provider 配置、Skills 审阅启用和 hooks 在模型会话中的完整装配须在使用者本机完成。GUI 未做交互验收。macOS/Linux 工作台未实测；CI 已配置 Windows/Linux 的合成 Python 检查，尚未在 GitHub 运行。

运行日志留在本地（忽略，不纳入源码包），不将绝对路径、会话凭据或运行生成账单发布。`requirements-lock-windows.txt` 是此 Windows 环境的全量 Python 版本快照；跨平台安装使用 `requirements.txt`（MCP 的 pywin32 依赖由平台自动选择）。
