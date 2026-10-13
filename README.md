# 涉赌资金流分析智能体（Fundflow Agent）

基于 Agnes Harness 的本地工作台，增加 11 个 Python MCP 工具、3 个业务 skills、工具结果检查及收工阶段 hooks、合成账单靶场和报告验收。`agent/` 是业务主目录，根目录三个 Python 文件是既有分析引擎、GUI 与单人查询；`vendor/agnes-harness/` 是未经修改的固定版本上游源码。

仓库仅包含程序和仿真场景，不包含真实流水、身份表、密钥、会话或历史分析产物。普通测试不需要模型账号。模型会话需由使用者在本机配置自己的 Provider。

## 环境

推荐 Windows 10/11 + PowerShell + Python **3.12**（含 venv/pip）+ Node.js **>=24.10** + pnpm **10.34.5** + Git。完整工作台构建在 Windows 上需要 Visual Studio C++ Build Tools（Desktop development with C++）及 Windows SDK；安装依赖需要网络。安装 pnpm：`npm install -g pnpm@10.34.5`。

macOS 需要 Xcode Command Line Tools；Linux 需要 C/C++ 构建环境、bubblewrap 与可用 user namespaces。Python 命令将下面的 `py -3.12` 替换为 `python3.12`。GUI 额外需要 Tk（Linux 通常为 python3-tk）。本仓库验证结果见 [VALIDATION.md](VALIDATION.md)，不要把 Windows 检查结果视为所有平台已验证。

## 从 GitHub 启动

```powershell
git clone <本仓库的 GitHub URL> fundflow-agent
cd fundflow-agent
py -3.12 run.py setup
py -3.12 run.py verify
py -3.12 run.py start
```

setup 创建本项目 `.venv`、安装 Python 依赖、生成本机 MCP/hooks/skills 配置、用上游 pnpm 锁文件安装 Node 依赖并构建完整工作台。打开 start 打印的完整 URL（可能带访问令牌），保持终端运行。默认端口 4177，冲突时用 `run.py start --port 4181`。配置、Provider 账号和会话存储隔离在 `.runtime/agh/`，不使用原机器 `~/.agh`；启动器不停止其他人的工作台。

仓库换位置后重跑 `setup`（虚拟环境不可直接跨机器搬运）；仅重新生成路径可用 `configure`。`run.py` 路径均由脚本所在位置确定，可从其他当前目录调用。不要提交 `.venv`、`.agh` 或 `.runtime`。

只使用 MCP/靶场时可跳过 Node 和工作台：

```powershell
py -3.12 run.py setup --python-only
py -3.12 run.py verify
.venv\Scripts\python.exe -X utf8 agent\mcp_server\server.py
```

server.py 是 **stdio MCP 服务**，最后一条命令等待客户端 JSON-RPC 输入，不是网页服务。其他 MCP 客户端可读取 `.agh/mcp.generated.json` 的 command/args；日志只写 stderr。

## 工作台首次装配

另开一个本仓库终端，运行：

```powershell
py -3.12 run.py register-mcp
py -3.12 run.py agh mcp get fundflow
```

新服务默认禁用。在 Web 设置 → MCP 审阅 Python 路径及工具范围后启用 fundflow。或使用 `run.py agh mcp trust fundflow --expected-revision <当前revision>`，重新 get 后再 `mcp enable ...`。若 fundflow 已存在，不重复 add；按上游 [MCP 指南](vendor/agnes-harness/docs/guide/mcp.zh-CN.md) 用当前 revision 更新 command/args。

Web 中新增/选择 **本仓库根目录** 工作区，不能选择 vendor 目录。设置 → Skills，刷新该工作区，审阅并启用 fundflow-investigation、fundflow-range、fundflow-verify。新机器会生成新 workspace/resource ID，不复用旧机器 ID。查看上游 [Skills 指南](vendor/agnes-harness/docs/guide/skills.zh-CN.md)。

hooks 文件由 setup 生成，但 Windows 没有进程隔离时需显式同意本地命令 hooks 才会运行。若决定启用此功能，在启动工作台之前运行：

```powershell
py -3.12 run.py trust-hooks
py -3.12 run.py start
```

trust-hooks 将脚本字节摘要和本机 Host boot 路径绑定，写入本项目隔离 profile；不会写用户全局授权。已有其他 profile 内容时拒绝覆盖，需人工合并 `commandHooks`。hooks 命令或仓库路径改变后需重新生成、审阅并更新摘要、重启工作台。默认审批策略保留。hook 脚本测试通过不等于真实工作台事件已执行；需在合成会话检查事件记录、软提示及 G1/G2 阶段门。撤销时移除 profile 中对应 commandHooks 授权，再重启。

最后在 Web 设置 → Provider 配置自己的模型账号，选择当前会话模型。密钥不填进源码、聊天或 Git。模型 API 会产生费用；本地解析工具本身不需模型密钥。

## 合成演示

```powershell
.venv\Scripts\python.exe -X utf8 agent\range\generator.py --scenario s30_noise_full_dev
.venv\Scripts\python.exe -X utf8 agent\range\evaluate.py --scenario s30_noise_full_dev
py -3.12 run.py agh mcp tools fundflow
```

在本仓库工作区的新会话显式说：“按 fundflow-range 技能生成合成场景；仅使用合成数据。”检查实际调用 `simulate_case` 成功及文件输出。随后可说：“按 fundflow-investigation 技能分析刚生成的合成账单，保留异常，暂不导出。”导出需用户确认，导出后显式调用 fundflow-verify。

11 工具：scan_dataset、build_fund_graph、classify_tiers、detect_laundering、scan_structure、rank_suspects、trace_funds、profile_account、export_evidence、simulate_case、anonymize_dataset。dataset_id 缓存属于 MCP 进程，重启后需重新 scan。运行产物在 `agent/deliverables/`；靶场评测在 `agent/range/reports/`。

可选 GUI：`py -3.12 run.py setup --python-only --gui`，再 `py -3.12 run.py gui`。单人查询需显式提供目录，如 `.venv\Scripts\python.exe query_person.py syn_demo --data-dir <合成账单绝对路径>`；不会猜测真实流水目录。

## 发布和限制

此发布目录没有原项目 Git 历史。上传前在此目录新建仓库：

```powershell
git init
git add .
git status --short
git commit -m "Initial portable fundflow agent"
git branch -M main
git remote add origin <你创建的 GitHub 仓库 URL>
git push -u origin main
```

`SOURCE_MANIFEST.json` 记录交付源文件 SHA256（不含自身及运行时产物），`UPSTREAM.json` 固定上游版本。不要上传原项目目录或其 `.git`。`.gitignore` 只防止后续误加入，不会清除已经提交的文件。

上游代码的 Apache-2.0 LICENSE、NOTICE 和第三方声明原样保留。本仓库应用代码**保留所有权利**（source-available，公开可见不构成授权，见 [LICENSE](LICENSE)）。

算法沿用既有引擎，部署修复不代表研判结论准确性验证。旧结构/时间检测失败等会作为异常保留；合成评测不证明真实案件效果。报告用于线索研判，最终认定以司法机关为准。
