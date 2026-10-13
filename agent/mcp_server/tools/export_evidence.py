"""本机证据材料导出，所有产物留在指定交付目录。"""
import html
import json
from pathlib import Path
from common import context, folder, ga, ok, tool, finish, write_json

@tool
def export_evidence(dataset_id: str, trace_refs: list[str] | None = None) -> dict:
    """导出本机八章研判摘要、旧可视化和证据索引。对外导出须提议→等待用户确认→执行；trace_refs由调用方提供，工具不伪造或自动核验Trace。"""
    ctx = context(dataset_id)
    if 'rank_result' not in ctx:
        raise ValueError('请先调用 rank_suspects')
    a = ctx['analyzer']
    out = folder(dataset_id)
    previous = ga.OUTPUT_DIR
    try:
        ga.OUTPUT_DIR = out
        report = Path(ga.generate_html_report(a, ga.Visualizer(a), a.get_suspect_df()))
    finally:
        ga.OUTPUT_DIR = previous
    # 旧模板含“认定/定罪”用语，仅适配导出文本，不改旧模板与引擎行为。
    text = report.read_text(encoding='utf-8').replace('认定', '研判').replace('定罪', '司法结论').replace('犯罪事实', '待核实线索')
    refs = trace_refs or []
    evidence = out / '06_evidence_index.json'
    scope = {'金额单位': '元', '时间范围': ctx['results']['01_intake']['证据']['时间跨度'],
             '账号范围': '当前dataset全部同人合并后的规范账号；显示姓名待核实',
             '金额类型': '图流水，非赌资；洗钱/结构检测使用旧合并tx而非图有效去重清单',
             '平台内转账': '图统计排除；旧检测仍可能使用含平台交易的tx'}
    # 重复导出不把上次06_export嵌套进新索引。
    results = {k: v for k, v in ctx['results'].items() if k != '06_export'}
    write_json(evidence, {'dataset_id': dataset_id, 'Trace引用': refs, 'Trace状态': '调用方提供，未核验' if refs else '尚未提供',
                          '口径': scope, '工具结果': results})
    detected = '已检测' if 'laundering_result' in ctx else '未检测（不能解释为无洗钱）'
    structured = ('已扫描，完整=' + str(ctx['results']['05_structure']['结论']['完整'])) if 'structure_result' in ctx else '未检测'
    warning = f'<section><h2>证据口径与待核实事项</h2><p>嫌疑≠司法结论；金额单位：元。洗钱：{detected}；结构：{structured}。未检测或检测失败的旧报告栏目不代表已核验结果。</p>'
    warning += '<p>完整依据三元组、评分构成、体检异常与Trace引用见 <a href="06_evidence_index.json">证据索引</a>。</p>'
    warning += '<pre>' + html.escape(str(ctx['issues'])) + '</pre></section>'
    text = text.replace('</body>', warning + '</body>')
    report.write_text(text, encoding='utf-8')
    issues = list(ctx['issues'])
    if '05_structure' in results:
        issues.extend(results['05_structure']['异常'])
    if not refs:
        issues.append('未提供 AGH Trace 引用；引用回填由调用方/编排方完成')
    def claim(tool_name, pointer, label):
        value = results[tool_name]
        for key in pointer.strip('/').split('/'):
            value = value[int(key)] if isinstance(value, list) else value[key]
        return (f'<p>{html.escape(label)}：<span data-tool="{tool_name}" '
                f'data-pointer="{html.escape(pointer, quote=True)}">{value}</span></p>')
    sections = [
        ('数据来源与完整性', claim('01_intake', '/结论/文件数', '文件数') + claim('01_intake', '/结论/交易数', '解析交易数')
         + '<pre>' + html.escape(json.dumps(scope, ensure_ascii=False)) + '</pre>'),
        ('组织结构', claim('04_graph_stats', '/结论/节点数', '规范节点数') + claim('04_graph_stats', '/结论/边数', '资金边数')
         + f'<p>结构检测：{structured}；资金关联不自动等于组织或代理关系。</p>'),
        ('层级认定及依据', '<p>本章仅列层级嫌疑与依据，最终司法结论由司法机关作出。</p><pre>'
         + html.escape(json.dumps(results['05_tiers']['结论']['层级分布'], ensure_ascii=False))
         + '</pre><p>逐账号三元组见05_tiers.csv及06_evidence_index.json。</p>'),
        ('资金穿透', '<p>' + ('已追线，详见05_trace.csv；重复边不可加总。' if '05_trace' in results else '未追线，待核实。') + '</p>'),
        ('洗钱线索', f'<p>{detected}；命中仅为特征线索，不作罪名推定。</p>'
         + (claim('05_laundering', '/结论/嫌疑节点数', '洗钱特征嫌疑节点数') if '05_laundering' in results else '')),
        ('重点人员', '<p>排序为核实优先级，评分构成见05_ranking.csv与证据索引。</p>'
         + ''.join(claim('05_ranking', f'/结论/排名/{i}/风险评分', str(r['账号']) + '风险评分')
                   for i, r in enumerate(results['05_ranking']['结论']['排名'][:3]))),
        ('待核实事项', '<pre>' + html.escape(json.dumps(issues, ensure_ascii=False, default=str)) + '</pre>'),
        ('侦查建议', '<p>核实账号实际使用人、投注记录、合法来源、上下级及利润分配证据；缺失项不以推测补齐。</p>')]
    canonical = out / '06_report.html'
    canonical.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>资金流研判摘要</title><body>'
        + '<h1>资金流研判摘要</h1><p>仅供侦查核实，嫌疑≠司法结论。</p>'
        + '<script id="report-scope" type="application/json">' + json.dumps(scope, ensure_ascii=False).replace('<', '\\u003c') + '</script>'
        + ''.join(f'<section><h2>{title}</h2>{body}</section>' for title, body in sections)
        + '<p><a href="analysis_report.html">旧可视化明细</a>（仅作辅助；栏目检测状态以上述说明为准）</p></body></html>', encoding='utf-8')
    return finish(ctx, '06_export', ok({'dataset_id': dataset_id, '状态': '已导出本机材料', '表述': '仅供侦查核实的线索材料'},
        {'Trace引用': refs, 'Trace核验': '需编排方回填', '上游结果': list(ctx['results'])}, issues,
        {'研判报告': str(canonical), '可视化明细': str(report), '排名表': str(out / '05_ranking.csv'), '证据索引': str(evidence)}))
