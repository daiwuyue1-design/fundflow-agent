"""tool_result硬/软档证据检查；stdlib-only且恒输出合法JSON对象。"""
import json,sys

def emit(value):
    sys.stdout.write(json.dumps(value,ensure_ascii=False,separators=(',',':')))

def main():
    try: event=json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception: emit({'decision':'block','reason':'hook载荷不是合法JSON对象'}); return 0
    if not isinstance(event,dict): emit({'decision':'block','reason':'hook载荷必须为JSON对象'}); return 0
    name=str(event.get('name',''))
    if not name.startswith('mcp_fundflow'): emit({}); return 0
    if not any(name.endswith('_'+tool) for tool in ('classify_tiers','detect_laundering','scan_structure')):emit({});return 0
    result=event.get('result'); text=None
    if isinstance(result,dict):
        for item in result.get('content',[]):
            if isinstance(item,dict) and item.get('type')=='text': text=item.get('text'); break
    if not isinstance(text,str): emit({'decision':'block','reason':'工具结果缺少content.text'}); return 0
    try: value=json.loads(text)
    except Exception: emit({'decision':'block','reason':'工具结果文本不是合法JSON'}); return 0
    if not isinstance(value,dict) or not all(k in value for k in ('结论','证据','异常')):
        emit({'decision':'block','reason':'工具结果缺少三段式契约：结论/证据/异常'}); return 0
    notes=[]
    if name.endswith('classify_tiers'):
        rows=value.get('结论',{}).get('判定',[])
        if any(not isinstance(r,dict) or not isinstance(r.get('依据'),dict) or not all(r.get('依据',{}).get(k) for k in ('指标值','百分位','适用规则')) for r in rows):
            notes.append('classify_tiers存在层级判定缺少指标值/百分位/适用规则')
    elif name.endswith('detect_laundering'):
        rows=value.get('结论',{}).get('判定',[])
        if any(not isinstance(r,dict) or not isinstance(r.get('五维'),list) or len(r['五维'])!=5 or r.get('命中说明') is None or any(not isinstance(x,dict) or x.get('阈值') is None or not x.get('说明') for x in r.get('五维',[])) for r in rows):
            notes.append('detect_laundering存在判定缺少五维/阈值/说明')
    elif name.endswith('scan_structure'):
        state=value.get('结论',{}).get('检测器状态',{})
        failures=[n for n,s in state.items() if isinstance(s,dict) and s.get('状态')=='失败']
        if (value.get('结论',{}).get('完整') is False and not failures) or any(not state[n].get('原因') or not any(isinstance(a,dict) and a.get('检测器')==n and a.get('状态')=='失败' and a.get('原因') for a in value.get('异常',[])) for n in failures):
            notes.append('scan_structure检测失败未显式列出')
    emit({'additionalContext':'；'.join(notes)} if notes else {})
    return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        emit({'additionalContext':'证据字段类型异常，自动证据检查未完成；请读取完整工具结果人工核对'})
