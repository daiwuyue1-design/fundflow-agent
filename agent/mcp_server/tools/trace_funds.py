"""调用旧Visualizer追线，直接观测其BFS链路。"""
import pandas as pd
from common import context, folder, ga, ok, tool, finish, observe

@tool
def trace_funds(dataset_id: str, target_id: str | None = None, max_depth: int = 5,
                top_n: int = 5, min_common: int = 2) -> dict:
    """资金穿透与完全相同上游集合分组。链路按旧BFS展开，可能重复，不可加总为赌资；超过50行落文件。自动种子需先rank，明确目标只需build。"""
    ctx = context(dataset_id)
    if 'analyzer' not in ctx:
        raise ValueError('请先 build_fund_graph')
    a = ctx['analyzer']
    if target_id is not None and target_id not in a.nodes:
        raise ValueError('目标规范账号不存在，请从层级表选择精确账号')
    if target_id is None and 'rank_result' not in ctx:
        raise ValueError('自动Top-N种子请先 rank_suspects；或指定 target_id')
    if not 0 <= max_depth <= 50 or not 1 <= top_n <= 50 or min_common < 2:
        raise ValueError('max_depth须0–50、top_n须1–50、min_common须≥2')
    visual = ga.Visualizer(a)
    links = []
    seeds = []
    def capture(event, local):
        links.extend(local.get('node_links', []))
        seeds.extend(local.get('seeds', []))
    fig = observe(visual.fund_penetration_trace, capture,
                  kwargs={'target_id': target_id, 'max_depth': max_depth, 'top_n': top_n})
    _, groups = visual.common_upstream_analysis(min_common=min_common)
    rows = [{'来源': str(src), '去向': str(dst), '金额元': float(amt), '展开深度': depth}
            for src, dst, amt, depth in links]
    # 分组表摊平，成员超过50也由finish统一截断，完整分组保留文件。
    members = [{'组号': i, '成员': str(m), '共同上游': g['upstreams']}
               for i, g in enumerate(groups, 1) for m in g['members']]
    out = folder(dataset_id)
    path = out / '05_trace.csv'
    pd.DataFrame(rows, columns=['来源', '去向', '金额元', '展开深度']).to_csv(path, index=False, encoding='utf-8')
    fig.write_html(str(out / '05_trace.html'), include_plotlyjs='cdn')
    return finish(ctx, '05_trace', ok({'dataset_id': dataset_id, '种子': seeds,
        '穿透链路': rows, '共同上游成员': members, '分组数': len(groups)},
        {'参数': {'max_depth': max_depth, 'top_n': top_n, 'min_common': min_common},
         '分组口径': '上游集合完全相同；旧方法最多返回Top20组。成员数为账号数，未经实际人数核实。',
         '共同上游组详情': [{k: v for k, v in g.items() if k not in ('members', 'member_names')} for g in groups],
         '追线口径': '双向BFS、全局visited；同边可能重复，边界节点仍记录相邻边；不是独立交易路径清单。'},
        list(ctx['issues']) + ['链路行金额不可重复加总；图金额沿用旧去重后权重，非赌资。'],
        {'穿透链路表': str(path), '穿透图': str(out / '05_trace.html')}))
