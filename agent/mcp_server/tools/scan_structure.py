"""覆盖旧引擎全部七个结构检测器，保留原始命中信息。"""
import pandas as pd
import copy
from common import context, folder, ok, tool, finish, ga

DETECTORS = ('_detect_account_sharing', '_detect_accountants', '_detect_credit_settlement',
             '_detect_credit_allocation', '_detect_large_settlement', '_detect_time_patterns',
             '_detect_one_way_accounts')

@tool
def scan_structure(dataset_id: str) -> dict:
    """扫描共用账号/做账人/信用结算/信用分配/大额结算/时间模式/单向账户。仅沿用原置信或命中字段。classify重算重置节点，须重扫；随后重跑rank。"""
    ctx = context(dataset_id)
    if 'analyzer' not in ctx:
        raise ValueError('请先 build_fund_graph')
    if 'structure_result' in ctx:
        return ctx['structure_result']
    actual = {n for n in dir(ga.GamblingAnalyzer) if n.startswith('_detect_')}
    if actual != set(DETECTORS) | {'_detect_money_laundering'}:
        raise ValueError(f'旧检测器清单变化，需人工核对：{sorted(actual)}')
    ctx.pop('rank_result', None)
    for key in ('05_ranking', '06_export'):
        ctx.get('results', {}).pop(key, None)
    rows, counts, states, issues = [], {}, {}, list(ctx['issues'])
    for name in DETECTORS:
        before = copy.deepcopy(ctx['analyzer'].nodes)
        try:
            hits = getattr(ctx['analyzer'], name)()
        except Exception as exc:
            ctx['analyzer'].nodes = before
            states[name] = {'状态': '失败', '原因': f'{type(exc).__name__}: {exc}'}
            counts[name] = None
            issues.append({'检测器': name, '状态': '失败', '原因': states[name]['原因']})
            continue
        states[name] = {'状态': '完成'}
        counts[name] = len(hits)
        items = hits.items() if isinstance(hits, dict) else ((r['account'], r) for r in hits)
        for account, evidence in items:
            confidence = evidence.get('confidence') if isinstance(evidence, dict) else None
            rows.append({'账号': str(account), '检测器': name, '命中': True,
                         '置信标注': confidence if confidence is not None else '未提供数值置信度；保留旧命中字段',
                         '原始命中字段': evidence})
    path = folder(dataset_id) / '05_structure.csv'
    pd.DataFrame(rows, columns=['账号', '检测器', '命中', '置信标注', '原始命中字段']).to_csv(path, index=False, encoding='utf-8')
    result = finish(ctx, '05_structure', ok({'dataset_id': dataset_id, '命中': rows, '各检测器命中数': counts,
        '检测器状态': states, '完整': all(s['状态'] == '完成' for s in states.values())},
        {'检测器清单': list(DETECTORS), '置信口径': '命中不是概率或司法结论；旧方法未提供数值置信度时不生成概率。'},
        issues + ['共用账号旧检测把2–4字中文词当潜在人名，命中需人工核对；不能据此换算实际人数。',
            '结构检测部分时间解析失败在旧方法内部被跳过，结果未命中不代表不存在该结构；核对体检时间质量。'],
        {'结构表': str(path)}))
    ctx['structure_result'] = result
    return result
