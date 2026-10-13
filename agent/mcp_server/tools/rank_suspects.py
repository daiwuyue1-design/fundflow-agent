"""风险排序及可回算的各维得分。"""
from common import context, folder, ok, tool, finish
from adapter import LEVELS

@tool
def rank_suspects(dataset_id: str) -> dict:
    """对已研判节点执行旧风险评分。推荐build→classify→detect_laundering→rank；判层重算后须重跑检测。未检测洗钱时附加分0，不能表述为无洗钱。结构扫描后排名失效需重跑。"""
    ctx = context(dataset_id)
    if 'tier_result' not in ctx:
        raise ValueError('请先调用 classify_tiers')
    if 'rank_result' in ctx:
        return ctx['rank_result']
    a = ctx['analyzer']
    a.score_with_evidence()
    rows = [{'账号': str(n), '层级': LEVELS[v['level']], '风险评分': v['risk_score'],
             '评分构成': a.score_parts[n], '封顶前合计': sum(a.score_parts[n].values())} for n, v in a.nodes.items()]
    rows.sort(key=lambda r: (-r['风险评分'], r['账号']))
    for i, row in enumerate(rows, 1):
        row['排名'] = i
        if min(round(row['封顶前合计'], 1), 100) != row['风险评分']:
            raise ValueError('评分构成回算与旧算法不一致')
    path = folder(dataset_id) / '05_ranking.csv'
    a.get_suspect_df().to_csv(path, index=False, encoding='utf-8')
    result = finish(ctx, '05_ranking', ok({'dataset_id': dataset_id, '排名': rows},
        {'评分规则': '各维贡献求和，保留一位小数，100分封顶', '范围': '洗钱附加分取决于 detect_laundering 是否已执行（未执行时按0，未检测不代表无洗钱）；结构附加分取决于 scan_structure 是否已执行；单向资金附加分沿用旧算法。',
         '洗钱检测状态': '已检测' if 'laundering_result' in ctx else '未检测'},
        ctx['issues'], {'排名表': str(path)}))
    ctx['rank_result'] = result
    return result
