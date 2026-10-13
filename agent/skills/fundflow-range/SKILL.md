---
name: fundflow-range
description: 生成带独立标准答案的合成资金流xlsx靶场，按冻结的dev/holdout场景运行工具评测，解读层级准确率、洗钱召回与误报；用于合成实验和演示。
---

# 合成靶场

显式激活：“按 fundflow-range 技能生成并评测合成场景”。在项目根执行以下 PowerShell 命令；只用合成数据，不读取真实流水。

## 生成

```powershell
.venv/Scripts/python.exe agent/range/generator.py --scenario s30_noise_full_dev
.venv/Scripts/python.exe agent/range/generator.py --name inline_demo --size 30 --noise --quality full --seed 1
```

也可调用 `simulate_case(scenario_name="s30_noise_full_dev")`，或内联 `name/size/noise/quality/seed`，两种方式互斥。该工具只生成，不评测；写文件由AGH默认destructive审批覆盖，遵循审批交互与等待纪律。同名重跑替换本生成器管理的文件；存在未管理文件时显式拒绝。

size只支持30/100/500，quality为full/missing；seed必须显式记录。产物在 `agent/deliverables/range/<name>/`：`dataset/*.xlsx`、`truth.json`、`summary.json`、`manifest.json`。角色和洗钱真值由生成器直接预设，不从工具预测反推。xlsx ZIP元数据固定，manifest提供逐文件SHA256；相同种子和参数可复现字节。

## 冻结与评测

场景在 `agent/range/scenarios/*.yaml`（JSON子集的合法YAML）；至少6个dev、2个holdout。调参只看dev；冻结 `agent/range/parameters.json` 的参数版本后跑holdout，禁止根据holdout结果回改参数。当前版本 `default-v1-frozen-20261011` 无阈值覆盖。真实阈值覆盖仍须提议、说明理由并等待明确确认。

```powershell
.venv/Scripts/python.exe agent/range/evaluate.py --split dev --cache-check
.venv/Scripts/python.exe agent/range/evaluate.py --split holdout
```

单场景使用 `--scenario s30_noise_full_dev`；`--split all`依次跑dev与holdout，仍须先冻结参数。评测进程内按scan_dataset→build_fund_graph→classify_tiers→detect_laundering→scan_structure→rank_suspects调用工具，不经AGH。大结果从工具完整JSON读取，不能只用返回前50行。

结果在 `agent/range/reports/<dev或holdout>_<YYYY-MM-DD>.md/.csv/.json` 与同名 `_roles.csv`。MD含场景和汇总，CSV含样本数/时长/参数版本/失败项；JSON含逐账号预测、分角色指标、混淆矩阵及完整失败原因。命令重跑覆盖当天同split报告，需留存历史时先保存报告。

## 指标解读

层级准确率=正确层级/可评层级数；功能角色未预设层级时不纳入层级分母。洗钱召回=TP/(TP+FN)，误报率=FP/(FP+TN)，按生成时独立洗钱真值评测。分母0记“不适用”；缺失预测计入失败样本，禁止作为阴性或静默排除。

坏数据场景保留损坏文件和缺金额列文件，scan_dataset的失败文件+原因必须报告，状态为“部分”，指标仅覆盖成功解析账单。结构检测器失败单列，不当0命中；旧时间检测器np缺陷仍保留。高总体准确率不证明少数代理层级可靠，应同时查分角色和混淆矩阵。合成指标不外推真实案件或司法认定。

复验命令：`agent/range/check_generator.py`验证三规模、完整解析、坏文件和字节复现；`agent/range/check_evaluate.py`验证零分母及失败隔离；均用项目 `.venv/Scripts/python.exe`执行。`--cache-check`验证A→B→A，不允许不同dataset串扰。
