"""子进程测试全部门，仅使用隔离临时工作区。"""
import json,subprocess,sys,tempfile,shutil,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
TIMINGS=[]
def call(path,payload):
    started=time.perf_counter()
    p=subprocess.run([sys.executable,'-X','utf8',str(path)],input=json.dumps(payload,ensure_ascii=False),text=True,encoding='utf-8',capture_output=True,timeout=.8)
    TIMINGS.append(time.perf_counter()-started)
    assert p.returncode==0 and isinstance(json.loads(p.stdout),dict),(p.returncode,p.stdout,p.stderr)
    return json.loads(p.stdout)
def event(name,value):
    return {'name':'mcp_fundflow_test_'+name,'result':{'content':[{'type':'text','text':value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)}]}}
def main():
    tool=ROOT/'agent/hooks/hook_tool_result_check.py'
    good={'结论':{},'证据':{},'异常':[]}
    assert call(tool,event('classify_tiers',good))=={}
    assert call(tool,event('classify_tiers','bad'))['decision']=='block'
    assert call(tool,event('classify_tiers',{}))['decision']=='block'
    assert call(tool,event('classify_tiers',{'结论':{'判定':[{'依据':{}}]},'证据':{},'异常':[]}))['additionalContext']
    assert call(tool,event('detect_laundering',{'结论':{'判定':[{'五维':[],'命中说明':None}]},'证据':{},'异常':[]}))['additionalContext']
    assert call(tool,event('scan_structure',{'结论':{'完整':False},'证据':{},'异常':[]}))['additionalContext']
    assert call(tool,{'name':'other_tool'})=={}
    assert call(tool,event('scan_dataset','bad'))=={}
    with tempfile.TemporaryDirectory(prefix='hooks_test_') as temp:
        root=Path(temp);scripts=root/'agent/hooks';scripts.mkdir(parents=True)
        stop=scripts/'hook_turn_stopping_check.py';shutil.copyfile(ROOT/'agent/hooks/hook_turn_stopping_check.py',stop)
        deliver=root/'agent/deliverables';deliver.mkdir()
        payload={'session_id':'synthetic-session','cwd':str(root)}
        assert call(stop,payload)=={}
        dataset=deliver/('a'*32);dataset.mkdir()
        assert call(stop,payload)=={}
        (dataset/'05_tiers.csv').write_text('synthetic',encoding='utf-8')
        assert call(stop,payload)['decision']=='block'
        assert call(stop,payload)=={}
        (dataset/'06_report.html').write_text('synthetic',encoding='utf-8')
        assert call(stop,payload)['decision']=='block'
        assert call(stop,payload)=={}
        state=json.loads(next((deliver/'.hooks_state').glob('*.json')).read_text(encoding='utf-8'))
        assert state['gates']==['G1','G2']
        (dataset/'07_verify.md').write_text('synthetic',encoding='utf-8')
        assert call(stop,{'session_id':'another'})=={}
    print(json.dumps({'status':'P4 HOOKS SELFTEST PASS','cases':len(TIMINGS),'max_seconds':max(TIMINGS),'G1_G2_once':True,'isolated':True},ensure_ascii=False))
if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
