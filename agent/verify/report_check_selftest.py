"""逐项负例均由独立临时样例产生，不改原证据。"""
import json,shutil,tempfile,sys
from pathlib import Path
from report_check import check
def synthetic_sample():
    """Build independent synthetic evidence; never depend on prior case output."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp_server'))
    from fixtures import generate
    from common import DELIVERABLES
    from tools import (scan_dataset, build_fund_graph, classify_tiers,
                       detect_laundering, scan_structure, rank_suspects,
                       profile_account, export_evidence)
    result = scan_dataset(str(generate()))
    did = result['结论']['dataset_id']
    for tool in (build_fund_graph, classify_tiers, detect_laundering, scan_structure, rank_suspects):
        result = tool(did)
        assert result['结论'].get('成功') is not False, result
    profile_account(did, 'synthetic_receiver')
    result = export_evidence(did)
    assert result['结论'].get('成功') is not False, result
    return DELIVERABLES / did
def main():
    sample = synthetic_sample()
    cases={}
    with tempfile.TemporaryDirectory(prefix='report_check_') as temp:
        root=Path(temp)/'dataset'
        def fresh():
            shutil.copytree(sample,root,dirs_exist_ok=True)
            return root/'06_report.html',root/'06_evidence_index.json'
        report,evidence=fresh();assert check(root)['状态']=='PASS'
        def htmlcase(name,transform,expected='FAIL',warning=None):
            report,_=fresh();report.write_text(transform(report.read_text(encoding='utf-8')),encoding='utf-8');r=check(root)
            assert r['状态']==expected,(name,r)
            if warning:assert any(warning in w for w in r['警告']),(name,r)
            cases[name]=r['状态']
        data = json.loads(evidence.read_text(encoding='utf-8'))
        gambler_count = data['工具结果']['05_tiers']['结论']['层级分布']['赌客线索']
        htmlcase('层级分布',lambda s:s.replace(f'赌客线索&quot;: {gambler_count}', '赌客线索&quot;: 999'))
        htmlcase('节点数自由文本',lambda s:s.replace('<h2>组织结构</h2>','<h2>组织结构</h2><p>节点数：999</p>'))
        htmlcase('边数自由文本',lambda s:s.replace('<h2>组织结构</h2>','<h2>组织结构</h2><p>边数：999</p>'))
        htmlcase('洗钱命中数',lambda s:s.replace('<h2>洗钱线索</h2>','<h2>洗钱线索</h2><p>洗钱命中数：999</p>'))
        htmlcase('坏文件总数',lambda s:s.replace('<h2>数据来源与完整性</h2>','<h2>数据来源与完整性</h2><p>坏文件数：999</p>'))
        htmlcase('失败原因',lambda s:s.replace('无法解析文件格式','故意篡改原因'))
        htmlcase('结构失败',lambda s:s.replace('_detect_time_patterns','hidden_detector'))
        htmlcase('正文金额单位',lambda s:s.replace('</body>','<p>金额单位：万元</p></body>'))
        htmlcase('免责句缺失',lambda s:s.replace('仅供侦查核实，嫌疑≠司法结论。','').replace('最终司法结论由司法机关作出',''))
        htmlcase('禁用词',lambda s:s.replace('</body>','<p>已认定犯罪事实并定罪</p></body>'))
        htmlcase('未匹配金额警告',lambda s:s.replace('</body>','<p>不明金额：1万元</p></body>'),'PASS','万元数值')
        for name,key in (('洗钱缺五维','五维'),('洗钱缺说明','命中说明')):
            report,evidence=fresh();data=json.loads(evidence.read_text(encoding='utf-8'));data['工具结果']['05_laundering']['结论']['判定'][0].pop(key)
            evidence.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');assert check(root)['状态']=='FAIL';cases[name]='FAIL'
        report,evidence=fresh();data=json.loads(evidence.read_text(encoding='utf-8'));data['工具结果']['05_laundering']['结论']['判定'][0]['五维'][0].pop('阈值')
        evidence.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');assert check(root)['状态']=='FAIL';cases['洗钱缺阈值']='FAIL'
        report,evidence=fresh();data=json.loads(evidence.read_text(encoding='utf-8'));data['工具结果']['05_profile']['结论']['指标']['in_flow']=8000
        evidence.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        text=report.read_text(encoding='utf-8').replace('</body>','<p>金额 <span data-tool="05_profile" data-pointer="/结论/指标/in_flow">0.8万元</span></p></body>')
        report.write_text(text,encoding='utf-8');assert check(root)['状态']=='PASS'
        report.write_text(text.replace('0.8万元','8万元'),encoding='utf-8');assert check(root)['状态']=='FAIL';cases['元万元换算']='PASS/FAIL'
        report,evidence=fresh();(root/'02_cleaned.csv').write_bytes(b'');assert check(root)['状态']=='FAIL';cases['空交付物']='FAIL'
        fresh();(root/'05_laundering.csv').write_text('账号,总分\nsynthetic,1\n',encoding='utf-8');r=check(root);assert r['状态']=='PASS' and '05_laundering.csv列不完整' in r['警告'];cases['列级警告']='PASS+warning'
    print(json.dumps({'status':'REPORT_CHECK SELFTEST PASS','cases':cases},ensure_ascii=False,indent=2))
if __name__=='__main__':sys.stdout.reconfigure(encoding='utf-8');main()
