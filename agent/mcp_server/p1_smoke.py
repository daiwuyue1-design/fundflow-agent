"""P1行为验收：旧算法等价、正向五维、重算失效、大表与报告门禁。"""
import contextlib
import copy
import io
import json
import re
import sys
from pathlib import Path
import networkx as nx
import pandas as pd
from common import CACHE, DELIVERABLES, ga, folder
from fixtures import generate
from tools import (scan_dataset, build_fund_graph, classify_tiers, rank_suspects, export_evidence,
                   detect_laundering, trace_funds, profile_account, scan_structure)
from smoke import check
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'verify'))
from report_check import check as report_check

def main():
    output = []
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        did = check(scan_dataset(str(generate())))['结论']['dataset_id']
        check(build_fund_graph(did)); check(classify_tiers(did))
        ctx = CACHE[did]
        check(rank_suspects(did))
        old = copy.deepcopy(ctx['analyzer'])
        with contextlib.redirect_stdout(sys.stderr):
            old._detect_money_laundering()
        detect = check(detect_laundering(did))
        assert 'rank_result' not in ctx
        assert [(v['launder_score'], v['launder_flags'], v['is_launder_suspect']) for v in old.nodes.values()] == [
            (v['launder_score'], v['launder_flags'], v['is_launder_suspect']) for v in ctx['analyzer'].nodes.values()]
        output.append('PASS 洗钱检测写回与旧算法等价，检测后排名失效')
        structure = check(scan_structure(did))
        assert len(structure['结论']['检测器状态']) == 7
        failed = structure['结论']['检测器状态']['_detect_time_patterns']
        assert failed['状态'] == '失败' and 'UnboundLocalError' in failed['原因']
        assert structure['结论']['各检测器命中数']['_detect_time_patterns'] is None
        assert any('未提供数值置信度' in str(row['置信标注']) for row in structure['结论']['命中'])
        output.append('PASS 七结构检测器全部调用，旧np错误显式失败而非0命中，未自造置信度')
        rank = check(rank_suspects(did))
        assert rank['证据']['洗钱检测状态'] == '已检测'
        profile = check(profile_account(did, 'synthetic_receiver'))
        assert profile['结论']['指标']['in_flow'] == ctx['analyzer'].nodes['synthetic_receiver']['in_flow']
        assert profile['结论']['上游']
        traced = check(trace_funds(did))
        expected = []
        from common import observe
        visual = ga.Visualizer(ctx['analyzer'])
        observe(visual.fund_penetration_trace, lambda event, values: expected.extend(values.get('node_links', [])))
        assert [(r['来源'], r['去向'], r['金额元'], r['展开深度']) for r in traced['结论']['穿透链路']] == expected
        output.append('PASS 画像指标/上下游与图一致，穿透链路与旧BFS逐行一致')
        exported = check(export_evidence(did))
        out = folder(did)
        assert report_check(out)['状态'] == 'PASS'
        # 单次局部篡改后恢复，逐个验证门禁拒绝，不改上游算法。
        report = out / '06_report.html'
        original = report.read_text(encoding='utf-8')
        report.write_text(re.sub(r'(data-pointer="/结论/节点数">)\d+', r'\g<1>9999', original), encoding='utf-8')
        assert report_check(out)['状态'] == 'FAIL'
        report.write_text(original.replace('</body>', '<p>已认定犯罪事实并定罪</p></body>'), encoding='utf-8')
        assert report_check(out)['状态'] == 'FAIL'
        report.write_text(original, encoding='utf-8')
        evidence = out / '06_evidence_index.json'
        raw = evidence.read_text(encoding='utf-8')
        data = json.loads(raw)
        data['工具结果']['05_tiers']['结论']['判定'][0]['依据'].pop('适用规则')
        evidence.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        assert report_check(out)['状态'] == 'FAIL'
        evidence.write_text(raw, encoding='utf-8')
        clean = out / '02_cleaned.csv'
        clean.rename(out / '02_cleaned.backup')
        try:
            assert report_check(out)['状态'] == 'FAIL'
        finally:
            (out / '02_cleaned.backup').rename(clean)
        report.write_text(original.replace('"金额单位": "元"', '"金额单位": "万元"'), encoding='utf-8')
        assert report_check(out)['状态'] == 'FAIL'
        report.write_text(original, encoding='utf-8')
        output.append('PASS 报告正常PASS；篡改数值/禁用结论/缺三元组/缺交付物/单位冲突均FAIL')
        override = {key: 0 for key in ctx['analyzer'].parameters}
        check(classify_tiers(did, override))
        assert all(key not in ctx for key in ('laundering_result', 'structure_result', 'rank_result'))
        assert all(key not in ctx['results'] for key in ('05_laundering', '05_structure', '05_ranking', '06_export'))
        undetected = check(rank_suspects(did))
        assert undetected['证据']['洗钱检测状态'] == '未检测'
        assert all(r['评分构成']['洗钱附加'] == 0 for r in undetected['结论']['排名'])
        output.append('PASS 判层重算失效洗钱/结构/排名/导出；未检测洗钱附加为0并明确状态')
        # 构造明确正向的五维测试：所有记录都是虚构，比较旧方法结果。
        positive = copy.deepcopy(ctx['analyzer'])
        template = copy.deepcopy(next(iter(positive.nodes.values())))
        positive.G = nx.DiGraph()
        positive.G.add_edge('synthetic_up', 'synthetic_mid', weight=120000, count=3)
        positive.G.add_edge('synthetic_mid', 'synthetic_end', weight=120000, count=1)
        positive.nodes = {}
        for n, inf, outf, level in [('synthetic_up', 0, 120000, 1), ('synthetic_mid', 120000, 120000, 3),
                                  ('synthetic_end', 120000, 0, 4)]:
            v = copy.deepcopy(template)
            v.update(name=n, in_flow=inf, out_flow=outf, total_flow=inf+outf,
                     in_degree=positive.G.in_degree(n), out_degree=positive.G.out_degree(n), level=level,
                     net_out_ratio=(outf-inf)/max(inf+outf, 1))
            positive.nodes[n] = v
        keyword = ga.OVERSEAS_KEYWORDS[0]
        positive.tx = pd.DataFrame([{'owner_id':'synthetic_mid','cp_key':'synthetic_up' if i<3 else 'synthetic_end',
            'amount_clean':40000 if i<3 else 120000, 'is_income':i<3, 'remark':keyword, 'desc':'虚构正向样例',
            'datetime_parsed':pd.Timestamp('2026-01-01')+pd.Timedelta(hours=i)} for i in range(4)])
        old = copy.deepcopy(positive)
        with contextlib.redirect_stdout(sys.stderr):
            old._detect_money_laundering()
        CACHE['p1_positive'] = {'id':'p1_positive','analyzer':positive,'tier_result':{},'issues':[],'results':{}}
        detect = check(detect_laundering('p1_positive'))
        mid = next(r for r in detect['结论']['判定'] if r['账号']=='synthetic_mid')
        assert [r['得分'] for r in mid['五维']] == [40.,25.,20.,15.,15.], mid
        assert mid['总分'] == old.nodes['synthetic_mid']['launder_score'] == 100
        assert positive.nodes['synthetic_mid']['is_launder_suspect'] is True
        positive_rank = check(rank_suspects('p1_positive'))
        assert next(r for r in positive_rank['结论']['排名'] if r['账号'] == 'synthetic_mid')['评分构成']['洗钱附加'] == 15
        output.append('PASS 正向五维40/25/20/15/15，原始115封顶100，与旧算法一致')
        # 大图追线返回前50；文件保存全部，顺序与旧图一致。
        large = copy.deepcopy(ctx['analyzer'])
        large.G = nx.DiGraph()
        large.nodes = {}
        for i in range(61):
            n = 'synthetic_large_' + str(i)
            large.nodes[n] = dict(template, name=n)
            large.G.add_node(n)
            if i:
                large.G.add_edge('synthetic_large_0', n, weight=i, count=1)
        CACHE['p1_large'] = {'id':'p1_large','analyzer':large,'issues':[],'results':{}}
        traced = check(trace_funds('p1_large', target_id='synthetic_large_0'))
        assert len(traced['结论']['穿透链路']) == 50
        assert traced['结论']['穿透链路总数'] > 50
        assert len(pd.read_csv(traced['artifacts']['穿透链路表'])) == traced['结论']['穿透链路总数']
        output.append('PASS >50行追线主动摘要，完整CSV保留全部行')
        # 恢复默认链，留下可验收样例，检查器将生成07。
        check(classify_tiers(did)); check(detect_laundering(did)); check(scan_structure(did))
        check(rank_suspects(did)); check(trace_funds(did)); check(export_evidence(did))
        assert report_check(out)['状态'] == 'PASS'
    assert not capture.getvalue(), capture.getvalue()
    output.extend(['PASS P1直接调用stdout无杂质', 'P1 ALL PASS', '样例交付物目录：' + str(out)])
    text = '\n'.join(output) + '\n'
    (DELIVERABLES / 'p1_output.txt').write_text(text, encoding='utf-8')
    print(text, end='')

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
