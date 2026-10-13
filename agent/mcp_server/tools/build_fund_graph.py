"""建图复用旧算法，并直接观测四层去重计数。"""
from common import context, folder, ok, tool, finish, observe
from adapter import EvidenceAnalyzer
import pandas as pd
import json

@tool
def build_fund_graph(dataset_id: str) -> dict:
    """清洗、四层去重并合并同人账号，缓存资金图，返回各层实际排除计数。"""
    ctx = context(dataset_id)
    if 'graph_result' in ctx:
        return ctx['graph_result']
    if ctx['tx'].empty:
        raise ValueError('没有有效交易，请先修正体检异常')
    analyzer = EvidenceAnalyzer(ctx['tx'], ctx['identity'])
    analyzer._clean()
    counts = {}
    def capture(event, values):
        if event == 'return':
            for title, key in [('平台内转账', 'n_internal'), ('自转账', 'n_self'), ('全局重复', 'n_global_deduped'), ('双向重复', 'n_bilateral_deduped')]:
                counts[title] = int(values.get(key, 0))
            counts['有效记录'] = len(values['all_flows'])
            counts['端点过滤'] = len(analyzer.tx) - counts['平台内转账'] - counts['自转账'] - int(values['n_before_global_dedup'])
    observe(analyzer._build_graph, capture)
    merged = analyzer._merge_person_nodes()
    if not analyzer.nodes:
        raise ValueError('过滤后资金图为空，不能研判')
    ctx['analyzer'] = analyzer
    path = folder(dataset_id) / '02_cleaned.csv'
    analyzer.tx.to_csv(path, index=False, encoding='utf-8')
    issues = list(ctx['issues']) + ['双向去重沿用旧算法：相反方向、同额、同一5分钟桶；可能合并真实往返交易，需核对。']
    result = finish(ctx, '04_graph_stats', ok({'dataset_id': dataset_id, '节点数': len(analyzer.nodes), '边数': analyzer.G.number_of_edges()},
        {'四层去重': counts, '清洗剔除非正金额': len(ctx['tx']) - len(analyzer.tx), '同人合并组数': merged,
         '计数口径': "四层按旧引擎实际执行观测；自转账计数可能与平台标记重叠；端点过滤为顺序余额。旧引擎打印行将'全局重复+双向重复'合计标注为'双向流水重复'（历史措辞，旧码不改）；本工具按分项如实计数。"},
        issues, {'标准交易表': str(path)}))
    ctx['base_nodes'] = __import__('copy').deepcopy(analyzer.nodes)
    entities = folder(dataset_id) / '03_entities.csv'
    pd.DataFrame([{'规范账号': str(n), '显示名称': analyzer.identity.resolve(n),
                   '身份状态': '待人工核实'} for n in analyzer.nodes]).to_csv(entities, index=False, encoding='utf-8')
    stats = folder(dataset_id) / '04_graph_stats.md'
    stats.write_text('# 资金图统计\n\n02_cleaned.csv是清洗/同人归一后的旧tx，仍可能含未进入图的记录；图有效记录以四层去重为准。\n\n```json\n'
        + json.dumps(ctx['results']['04_graph_stats'], ensure_ascii=False, indent=2, default=str) + '\n```\n', encoding='utf-8')
    result['artifacts'].update({'实体表': str(entities), '图统计记录': str(stats)})
    ctx['graph_result'] = result
    return result
