"""直接五工具冒烟，并核验默认算法等价、异常与缓存失效。"""
import contextlib
import copy
import io
import json
import sys
from pathlib import Path
from common import CACHE, DELIVERABLES, ga
from fixtures import generate
from tools import scan_dataset, build_fund_graph, classify_tiers, rank_suspects, export_evidence
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def check(result):
    assert set(result) == {'结论', '证据', '异常', 'artifacts'}, result
    assert isinstance(result['结论'], dict) and isinstance(result['证据'], dict)
    assert isinstance(result['异常'], list)
    assert result['结论'].get('成功') is not False, result
    return result

def main():
    output = []
    def passed(msg):
        output.append('PASS ' + msg)
    data = generate()
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        scan = check(scan_dataset(str(data)))
        did = scan['结论']['dataset_id']
        assert len(scan['证据']['失败清单']) == 1
        assert {x['类型'] for x in scan['证据']['文件清单'] if x['状态'] == '成功'} == {'wechat', 'alipay', 'bank'}
        passed('scan_dataset：三种xlsx格式成功，损坏文件显式报告')
        graph = check(build_fund_graph(did))
        counts = graph['证据']['四层去重']
        assert all(counts[k] == 1 for k in ('平台内转账', '自转账', '全局重复', '双向重复')), counts
        passed('build_fund_graph：四层实际计数，平台/自转账/全局重复/双向重复各1条')
        tiers = check(classify_tiers(did))
        for row in tiers['结论']['判定']:
            assert set(row['依据']) == {'指标值', '百分位', '适用规则'}
            assert row['依据']['适用规则']
        ctx = CACHE[did]
        with contextlib.redirect_stdout(io.StringIO()):
            old = ga.GamblingAnalyzer(ctx['tx'], ctx['identity'])
            old._clean(); old._build_graph(); old._merge_person_nodes(); old._classify_levels(); old._score_suspects()
        assert {n: v['level'] for n, v in old.nodes.items()} == {n: v['level'] for n, v in ctx['analyzer'].nodes.items()}
        passed('classify_tiers：所有节点具备依据三元组，默认层级与旧算法完全一致')
        ranking = check(rank_suspects(did))
        assert {n: v['risk_score'] for n, v in old.nodes.items()} == {n: v['risk_score'] for n, v in ctx['analyzer'].nodes.items()}
        for row in ranking['结论']['排名']:
            assert min(round(sum(row['评分构成'].values()), 1), 100) == row['风险评分']
        passed('rank_suspects：各维贡献回算一致，默认评分与旧算法完全一致')
        export = check(export_evidence(did))
        for path in export['artifacts'].values():
            assert Path(path).is_file()
            assert Path(path).is_relative_to(DELIVERABLES)
        text = Path(export['artifacts']['研判报告']).read_text(encoding='utf-8')
        assert '认定' not in text.replace('层级认定及依据', '') and '定罪' not in text
        passed('export_evidence：报告/排名/证据索引存在，输出限定deliverables，缺Trace显式标注')
        override = {k: 0 for k in ('p50_in', 'p70_in', 'p80_in', 'p90_in', 'p95_in', 'p99_in', 'p70_out_d', 'p80_out_d', 'p90_out_d', 'p80_in_d')}
        changed = check(classify_tiers(did, override))
        assert changed['证据']['参数']['p95_in'] == 0
        assert 'rank_result' not in ctx
        assert tiers['结论']['层级分布'] != changed['结论']['层级分布']
        restored = check(classify_tiers(did))
        assert restored['结论'] == tiers['结论']
        passed('阈值覆盖实际改变研判，排名缓存失效，恢复默认结果一致')
        invalid = classify_tiers(did, {'p95_in': -1})
        assert invalid['结论']['成功'] is False and invalid['异常']
        invalid_id = rank_suspects('invalid-id')
        assert invalid_id['结论']['成功'] is False
        passed('非法阈值与无效dataset_id返回显式异常')
        a = ctx['analyzer']
        edge = next(iter(a.G.edges()))
        a.G.add_edge(edge[1], edge[0], weight=1, count=1)
        blocked = classify_tiers(did, {'p50_in': 0})
        assert blocked['结论']['成功'] is False and '有向环' in str(blocked['异常'])
        passed('有向资金环显式阻断，避免旧拓扑深度算法不终止')
    assert not capture.getvalue(), capture.getvalue()
    passed('stdout纪律：五工具直接调用无stdout杂质')
    output.append('SMOKE ALL PASS')
    output.append('合成数据目录：' + str(data))
    output.append('样例交付物目录：' + str(DELIVERABLES / did))
    text = '\n'.join(output) + '\n'
    (DELIVERABLES / 'smoke_output.txt').write_text(text, encoding='utf-8')
    print(text, end='')

if __name__ == '__main__':
    main()
