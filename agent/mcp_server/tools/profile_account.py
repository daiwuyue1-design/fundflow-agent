"""仅使用当前图与节点查询；不导入会自动探查真实目录的query_person。"""
from common import context, ok, tool, finish
from adapter import LEVELS

@tool
def profile_account(dataset_id: str, account_id: str) -> dict:
    """按精确规范账号生成可引用画像：流量/度/净额比/标签/上下游；build后可用，层级、洗钱与结构明确标注是否检测。"""
    ctx = context(dataset_id)
    if 'analyzer' not in ctx:
        raise ValueError('请先 build_fund_graph')
    a = ctx['analyzer']
    if account_id not in a.nodes:
        raise ValueError('精确规范账号不存在；请从层级表选择，不根据模糊姓名猜测')
    v = a.nodes[account_id]
    def neighbors(edges, upstream):
        rows = []
        for src, dst, data in edges:
            node = src if upstream else dst
            rows.append({'账号': str(node), '显示名称': a.identity.resolve(node),
                         '金额元': data.get('weight', 0), '笔数': data.get('count', 0),
                         '层级': LEVELS.get(a.nodes[node].get('level'), '未研判') if 'tier_result' in ctx else '未研判'})
        return sorted(rows, key=lambda r: (-r['金额元'], r['账号']))
    fields = {k: v.get(k) for k in ('in_flow', 'out_flow', 'total_flow', 'in_degree', 'out_degree', 'net_out_ratio')}
    tags = {k: value for k, value in v.items() if k.startswith(('is_', 'has_'))}
    return finish(ctx, '05_profile', ok({'dataset_id': dataset_id, '账号': account_id,
        '显示名称': a.identity.resolve(account_id), '指标': fields, '标签': tags,
        '层级': LEVELS.get(v.get('level'), '未研判') if 'tier_result' in ctx else '未研判',
        '上游': neighbors(a.G.in_edges(account_id, data=True), True),
        '下游': neighbors(a.G.out_edges(account_id, data=True), False)},
        {'金额单位': '元', '时间范围': ctx['results']['01_intake']['证据']['时间跨度'],
         '账号口径': '同人合并后规范节点；姓名仍需身份核实',
         '净额比口径': '沿用节点net_out_ratio，分母与方向以旧节点计算为准，未另算赌资',
         '检测状态': {name: '已检测' if key in ctx else '未检测' for name, key in
                      [('层级', 'tier_result'), ('洗钱', 'laundering_result'), ('结构', 'structure_result')]},
         '依据来源': '当前节点指标与图边；复用query_person的图边查询逻辑，不读取其默认数据目录'}, ctx['issues']))
