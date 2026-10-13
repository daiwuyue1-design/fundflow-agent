"""直接调用工具箱、读取完整产物、按独立答案评测；失败另列。"""
import argparse
from collections import Counter
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from generator import generate, load_scenario, SCENARIOS, write_json
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp_server'))
from common import CACHE, DELIVERABLES
from tools import scan_dataset, build_fund_graph, classify_tiers, detect_laundering, scan_structure, rank_suspects

REPORTS = Path(__file__).with_name('reports')
PARAMETERS = Path(__file__).with_name('parameters.json')


def ratio(n, d):
    return n / d if d else '不适用'


def metrics(rows):
    result = Counter()
    confusion = Counter()
    for row in rows:
        if row['true_tier'] is not None:
            if row['pred_tier'] is None:
                result['tier_failed'] += 1
            else:
                result['tier_valid'] += 1
                result['tier_correct'] += row['true_tier'] == row['pred_tier']
                confusion[(row['true_tier'], row['pred_tier'])] += 1
        if row['pred_laundering'] is None:
            result['laundering_failed'] += 1
        else:
            result['laundering_valid'] += 1
            label = ('TP' if row['true_laundering'] else 'FP') if row['pred_laundering'] else ('FN' if row['true_laundering'] else 'TN')
            result[label] += 1
    for key in ('tier_valid', 'tier_correct', 'tier_failed', 'laundering_valid', 'laundering_failed', 'TP', 'FN', 'FP', 'TN'):
        result.setdefault(key, 0)
    return {**result, 'tier_accuracy': ratio(result['tier_correct'], result['tier_valid']),
            'laundering_recall': ratio(result['TP'], result['TP'] + result['FN']),
            'false_positive_rate': ratio(result['FP'], result['FP'] + result['TN']),
            'tier_confusion': [{'true': a, 'predicted': b, 'count': c} for (a, b), c in sorted(confusion.items())]}


def full(result):
    path = result['artifacts'].get('完整结果')
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else result


def evaluate(params, config):
    start = time.perf_counter()
    failures, results, accounts, did = [], {}, [], None
    try:
        artifacts = generate(**params)
        truth = json.loads(Path(artifacts['标准答案路径']).read_text(encoding='utf-8'))
        accounts = truth['accounts']
        scan = full(scan_dataset(artifacts['数据集目录']))
        results['scan_dataset'] = scan
        if scan['结论'].get('成功') is False:
            raise RuntimeError(json.dumps(scan['异常'], ensure_ascii=False))
        did = scan['结论']['dataset_id']
        for failure in scan['证据']['失败清单']:
            failures.append({'stage': 'scan_dataset', **failure})
        for fn in (build_fund_graph, classify_tiers, detect_laundering, scan_structure, rank_suspects):
            result = full(fn(did, thresholds=config['thresholds']) if fn is classify_tiers else fn(did))
            results[fn.__name__] = result
            if result['结论'].get('成功') is False:
                failures.append({'stage': fn.__name__, '原因': result['异常']})
            if fn is scan_structure:
                failures.extend({'stage': 'scan_structure', '检测器': name, **state}
                                for name, state in result['结论'].get('检测器状态', {}).items() if state['状态'] == '失败')
    except Exception as exc:
        failures.append({'stage': 'scenario', '原因': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc()})
    tiers = {r['账号']: r['层级'] for r in results.get('classify_tiers', {}).get('结论', {}).get('判定', [])}
    laundering = {r['账号']: r['洗钱嫌疑'] for r in results.get('detect_laundering', {}).get('结论', {}).get('判定', [])}
    rows = [{'scenario': params['name'], 'account': a['合成ID'], 'role': a['角色标签'],
             'true_tier': a['层级标签'], 'pred_tier': tiers.get(a['合成ID']),
             'true_laundering': a['洗钱相关'], 'pred_laundering': laundering.get(a['合成ID'])} for a in accounts]
    status = '失败' if not accounts or any(r['pred_tier'] is None and r['true_tier'] is not None for r in rows) or any(r['pred_laundering'] is None for r in rows) else ('部分' if failures else '完整')
    if not accounts:
        status = '失败'
    result = {'scenario': params['name'], 'split': params['split'], 'status': status,
              'seconds': round(time.perf_counter() - start, 4), 'parameter_version': config['version'],
              'parameter_sha256': hashlib.sha256(PARAMETERS.read_bytes()).hexdigest(),
              'accounts': len(accounts), 'failure_count': len(failures), **metrics(rows),
              'failures': failures, 'by_role': {role: metrics([r for r in rows if r['role'] == role]) for role in sorted({r['role'] for r in rows})},
              'tool_artifacts': {name: r['artifacts'] for name, r in results.items()}}
    if did:
        CACHE.pop(did, None)
    return result, rows


def cache_check():
    """A→B→A验证数据集上下文和同参数结果未被另一场景覆盖。"""
    ids = []
    try:
        snapshots = []
        for name, size in (('cache_a', 30), ('cache_b', 100)):
            artifact = generate(name, size=size, seed=701, noise=True)
            scan = scan_dataset(artifact['数据集目录'])
            did = scan['结论']['dataset_id']
            ids.append(did)
            graph = build_fund_graph(did)
            tier = classify_tiers(did)
            assert graph['结论']['节点数'] == size
            snapshots.append(json.dumps(full(tier), sort_keys=True, ensure_ascii=False))
        assert ids[0] != ids[1] and CACHE[ids[0]]['analyzer'] is not CACHE[ids[1]]['analyzer']
        assert snapshots[0] == json.dumps(full(classify_tiers(ids[0])), sort_keys=True, ensure_ascii=False)
        assert snapshots[0] != snapshots[1]
        return {'status': 'PASS', 'datasets': ids, 'sizes': [30, 100], 'sequence': 'A→B→A', 'key': 'CACHE[dataset_id]'}
    finally:
        for did in ids:
            CACHE.pop(did, None)


def write_reports(split, results, samples, config):
    REPORTS.mkdir(exist_ok=True)
    stem = f'{split}_{date.today().isoformat()}'
    total = {'scenario': '汇总', 'split': split, 'status': '部分' if any(r['status'] != '完整' for r in results) else '完整',
             'seconds': round(sum(r['seconds'] for r in results), 4), 'parameter_version': config['version'],
             'accounts': sum(r['accounts'] for r in results), 'failure_count': sum(r['failure_count'] for r in results),
             **metrics(samples)}
    fields = ['scenario', 'split', 'status', 'seconds', 'parameter_version', 'accounts', 'failure_count',
              'tier_valid', 'tier_correct', 'tier_failed', 'tier_accuracy', 'laundering_valid', 'laundering_failed',
              'TP', 'FN', 'FP', 'TN', 'laundering_recall', 'false_positive_rate']
    with (REPORTS / (stem + '.csv')).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows([*results, total])
    with (REPORTS / (stem + '_roles.csv')).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, ['scenario', 'role', *fields[7:]], extrasaction='ignore')
        writer.writeheader()
        for result in results:
            writer.writerows({'scenario': result['scenario'], 'role': role, **m} for role, m in result['by_role'].items())
        writer.writerows({'scenario': '汇总', 'role': role, **metrics([s for s in samples if s['role'] == role])}
                        for role in sorted({s['role'] for s in samples}))
    write_json(REPORTS / (stem + '.json'), {'parameters': config, 'scenarios': results, 'total': total,
                                          'by_role': {role: metrics([s for s in samples if s['role'] == role]) for role in sorted({s['role'] for s in samples})},
                                          'samples': samples})
    lines = [f'# {split} 合成靶场评测 {date.today().isoformat()}', '',
             f'参数版本：{config["version"]}；默认自适应百分位，无阈值覆盖。调参仅dev；冻结后holdout仅评测，禁止回改。', '',
             '层级准确率=正确层级/可评层级；召回=TP/(TP+FN)；误报=FP/(FP+TN)。分母0为“不适用”。功能角色的层级真值为空，不强行指定层级；洗钱真值均独立预设。', '',
             '结构检测失败单列，未影响成功的层级/洗钱指标；缺失预测从对应分母排除并计失败样本。坏文件场景为“部分”，指标仅覆盖成功解析的账单，不能代表损坏数据全量性能。', '',
             '|场景|状态|层级正确/有效|准确率|TP/FN/FP/TN|召回|误报|失败项|秒|', '|---|---|---|---|---|---|---|---|---|']
    for r in [*results, total]:
        lines.append(f'|{r["scenario"]}|{r["status"]}|{r["tier_correct"]}/{r["tier_valid"]}|{r["tier_accuracy"]}|{r["TP"]}/{r["FN"]}/{r["FP"]}/{r["TN"]}|{r["laundering_recall"]}|{r["false_positive_rate"]}|{r["failure_count"]}|{r["seconds"]}|')
    lines += ['', '分角色指标见同名_roles.csv；层级混淆矩阵、洗钱混淆矩阵、逐账号预测、完整失败清单及工具产物索引见同名.json。', '', '## 失败原文', '']
    for r in results:
        lines += [r['scenario'], '', '```json', json.dumps(r['failures'], ensure_ascii=False, indent=2), '```', '']
    (REPORTS / (stem + '.md')).write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'report': str(REPORTS / (stem + '.md')), 'summary': total}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description='合成靶场评测（进程内直接调用）')
    parser.add_argument('--split', choices=['dev', 'holdout', 'all'], default='dev')
    parser.add_argument('--scenario')
    parser.add_argument('--cache-check', action='store_true')
    args = parser.parse_args()
    config = json.loads(PARAMETERS.read_text(encoding='utf-8'))
    if not config['frozen']:
        raise ValueError('参数未冻结，禁止启动正式评测')
    if args.cache_check:
        result = cache_check()
        write_json(DELIVERABLES / 'phase3_cache_check.json', result)
        print(json.dumps(result, ensure_ascii=False))
    scenarios = [load_scenario(args.scenario)] if args.scenario else [load_scenario(p.stem) for p in sorted(SCENARIOS.glob('*.yaml'))]
    for split in ('dev', 'holdout'):
        if not args.scenario and args.split not in (split, 'all'):
            continue
        selected = [s for s in scenarios if s['split'] == split]
        if not selected:
            continue
        results, samples = [], []
        for params in selected:
            r, rows = evaluate(params, config)
            results.append(r)
            samples.extend(rows)
            print(json.dumps({'scenario': params['name'], 'status': r['status'], 'failures': r['failure_count']}, ensure_ascii=False), flush=True)
        write_reports(split, results, samples, config)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
