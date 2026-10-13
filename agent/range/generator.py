"""只按显式种子生成合成账单；答案来自角色设计而非工具预测。"""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import random
import re
import zipfile

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'agent/deliverables/range'
SCENARIOS = Path(__file__).with_name('scenarios')
VERSION = 'synthetic-v1'


def load_scenario(name):
    """场景文件使用 JSON 子集的合法 YAML，无额外解析依赖。"""
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        raise ValueError('场景名仅允许字母、数字、下划线和连字符')
    return json.loads('\n'.join(line for line in (SCENARIOS / (name + '.yaml')).read_text(encoding='utf-8').splitlines()
                                if not line.lstrip().startswith('#')))


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_xlsx(path, rows):
    """固定 workbook 属性和 ZIP 时间戳，保证文件字节可复现。"""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        pd.DataFrame(rows).to_excel(writer, index=False)
        writer.book.properties.created = datetime(2026, 1, 1)
        writer.book.properties.modified = datetime(2026, 1, 1)
    with zipfile.ZipFile(buffer) as source, zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as target:
        for name in sorted(source.namelist()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            payload = source.read(name)
            if name == 'docProps/core.xml':
                payload = re.sub(rb'(<dcterms:(?:created|modified)[^>]*>)[^<]+',
                                 rb'\g<1>2026-01-01T00:00:00Z', payload)
            target.writestr(info, payload)


def generate(name, size=30, noise=False, quality='full', seed=1, split='inline'):
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        raise ValueError('场景名仅允许字母、数字、下划线和连字符')
    if isinstance(size, bool) or size not in (30, 100, 500):
        raise ValueError('size 必须为30/100/500')
    if not isinstance(noise, bool) or quality not in ('full', 'missing'):
        raise ValueError('noise须为布尔值；quality须为full/missing')
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError('seed须为整数')
    if split not in ('dev', 'holdout', 'inline'):
        raise ValueError('split须为dev/holdout/inline')
    rng = random.Random(seed)
    root = OUTPUT / name
    dataset = root / 'dataset'
    dataset.mkdir(parents=True, exist_ok=True)
    # 同名重跑只替换本生成器自己的文件，禁止混入未管理文件。
    manifest = root / 'manifest.json'
    if manifest.exists():
        for filename in json.loads(manifest.read_text(encoding='utf-8'))['files']:
            p = dataset / filename
            if p.parent == dataset and p.name.startswith('syn_'):
                p.unlink(missing_ok=True)
    if any(dataset.iterdir()):
        raise ValueError('场景目录存在未管理文件，请换场景名')
    accounts, groups = [], {}
    def allocate(role, count, tier=None, laundering=False):
        group = []
        for _ in range(count):
            aid = f'syn{len(accounts):04d}'
            accounts.append({'合成ID': aid, '合成名': f'合成账户{len(accounts):04d}', '角色标签': role,
                             '层级标签': tier, '洗钱相关': laundering,
                             '备注': '生成时预设角色；功能角色不强行指定结构层级'})
            group.append(aid)
        groups[role] = group
        return group
    first = allocate('一级代理嫌疑', 1, '一级代理嫌疑')[0]
    second = allocate('二级代理嫌疑', max(2, size // 50), '二级代理嫌疑')
    third = allocate('三级代理嫌疑', max(3, size // 20), '三级代理嫌疑')
    collector = allocate('归集转出线索', 1)[0]
    accountant = allocate('做账/代收付线索', 1)[0]
    water = allocate('过路/水房线索', 1, laundering=True)[0]
    runner = allocate('跑分线索', 1, laundering=True)[0]
    platform = allocate('平台内转账噪音', 1)[0]
    normal = allocate('待定外围', size // 5 if noise else 0, '待定外围')
    gamblers = allocate('赌客线索', size - len(accounts), '赌客线索')
    rebates, depositors = gamblers[:5], gamblers[5:]
    ledger = {a['合成ID']: [] for a in accounts}
    edges, serial = set(), 0
    base = datetime(2026, 1, 1)
    def transfer(src, dst, amount, hour, remark='合成充值', internal=False):
        nonlocal serial
        serial += 1
        stamp = (base + timedelta(hours=hour, minutes=serial % 50)).strftime('%Y-%m-%d %H:%M:%S')
        trade = f'SYN-{seed}-{serial:06d}'
        for owner, cp, direction in ((src, dst, '支出'), (dst, src, '收入')):
            if owner in ledger:
                ledger[owner].append({'交易时间': stamp, '交易对方账号': cp, '交易对方': cp,
                    '收/支': direction, '金额': amount, '商户单号': trade, '支付方式': '零钱',
                    '商品': '合成交易', '备注': remark})
        if not internal:
            edges.add((src, dst))
    totals = Counter()
    for i, gambler in enumerate(depositors):
        agent = third[i % len(third)]
        amount = rng.randint(5, 15) * 1000
        for day in range(3):
            transfer(gambler, agent, amount, day * 24 + 1)
            totals[agent] += amount
    upstream = Counter()
    for i, agent in enumerate(third):
        parent = second[i % len(second)]
        transfer(agent, parent, round(totals[agent] * .9, 2), 80, '合成代理结算，留存抽水10%')
        upstream[parent] += round(totals[agent] * .9, 2)
    for agent in second:
        transfer(agent, first, round(upstream[agent] * .95, 2), 110, '合成代理结算，留存抽水5%')
    transfer(first, collector, max(1000, round(sum(upstream.values()) * .85, 2)), 140, '合成归集转出')
    transfer(first, accountant, 30000, 150, '合成返利预算')
    for i, gambler in enumerate(rebates):
        transfer(accountant, gambler, 1000, 160 + i, '合成返利佣金')
    # 专用通道给水房/跑分同时提供owner收入和支出视角，低留存、快转、整千拆分。
    for i in range(3):
        transfer(collector, water, 60000, 200 + i * 48, '合成中转')
        transfer(water, runner, 59000, 201 + i * 48, '合成中转')
        transfer(runner, rebates[i], 58000, 202 + i * 48, '合成拆分')
    transfer(platform, collector, 12.37, 350, '合成账户连接')
    transfer(platform, '零钱通', 100, 351, '合成平台内部划转', internal=True)
    for i, aid in enumerate(normal):
        transfer(aid, rebates[i % 5], rng.randint(10, 200) + .37, 400 + i,
                 '合成日常消费' if i % 2 else '合成借款往来')
    files = []
    for aid, rows in ledger.items():
        filename = 'syn_bill_' + aid + '_微信.xlsx'
        write_xlsx(dataset / filename, rows)
        files.append(filename)
    failures = []
    if quality == 'missing':
        filename = 'syn_missing_微信.xlsx'
        write_xlsx(dataset / filename, [{k: v for k, v in ledger[first][0].items() if k != '金额'}])
        files.append(filename)
        failures.append({'文件': filename, '原因类型': '缺少时间或金额关键列'})
        filename = 'syn_broken.xlsx'
        (dataset / filename).write_bytes(b'SYNTHETIC INVALID XLSX')
        files.append(filename)
        failures.append({'文件': filename, '原因类型': '无法解析文件格式'})
    metadata = dict(name=name, size=size, noise=noise, quality=quality, seed=seed, split=split, generator_version=VERSION)
    summary = {'节点数': len(accounts), '边数': len(edges), '交易事件数': serial,
               '角色分布': dict(Counter(a['角色标签'] for a in accounts)), '预期失败': failures,
               '平台口径': '零钱通为平台标识，不计合成账户规模；内部划转不计资金图边'}
    write_json(root / 'truth.json', {'metadata': metadata, 'accounts': accounts, 'summary': summary})
    write_json(root / 'summary.json', summary)
    write_json(manifest, {'files': files, 'sha256': {f: hashlib.sha256((dataset / f).read_bytes()).hexdigest() for f in files}})
    return {'数据集目录': str(dataset), '标准答案路径': str(root / 'truth.json'), '统计摘要': summary,
            '清单': str(manifest)}


def main():
    parser = argparse.ArgumentParser(description='合成靶场生成器')
    parser.add_argument('--scenario')
    parser.add_argument('--name', default='inline_demo')
    parser.add_argument('--size', type=int, default=30)
    parser.add_argument('--noise', action='store_true')
    parser.add_argument('--quality', choices=['full', 'missing'], default='full')
    parser.add_argument('--seed', type=int, default=1)
    args = parser.parse_args()
    params = load_scenario(args.scenario) if args.scenario else dict(name=args.name, size=args.size, noise=args.noise, quality=args.quality, seed=args.seed)
    print(json.dumps(generate(**params), ensure_ascii=False))


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
