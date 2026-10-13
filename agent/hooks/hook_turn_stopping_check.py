"""收工阶段门G1/G2；每个session+dataset+门只提醒一次。"""
import json,os,re,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; DELIVER=ROOT/'agent'/'deliverables'; STATE=DELIVER/'.hooks_state'
def emit(v):sys.stdout.write(json.dumps(v,ensure_ascii=False,separators=(',',':')))
def main():
    event={}
    try:event=json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception: emit({});return 0
    if not isinstance(event,dict):emit({});return 0
    session=re.sub(r'[^A-Za-z0-9_.-]','_',str(event.get('session_id') or os.environ.get('AGNES_SESSION_ID','unknown')))
    dirs=sorted((p for p in DELIVER.iterdir() if p.is_dir() and re.fullmatch(r'[0-9a-f]{32}',p.name)),key=lambda p:p.stat().st_mtime,reverse=True)
    if not dirs:emit({});return 0
    dataset=dirs[0]; gates=[]
    if any(dataset.glob('05_tiers.*')) and not (dataset/'06_report.html').is_file():gates.append(('G1','研判材料已产出但未见导出报告；若属等待确认/分阶段推进，请说明当前阶段与后续计划'))
    if (dataset/'06_report.html').is_file() and not (dataset/'07_verify.md').is_file():gates.append(('G2','已导出未见验收记录（07_verify.md）；若验收由编排方另行执行，请注明'))
    if not gates:emit({});return 0
    STATE.mkdir(parents=True,exist_ok=True); notes=[]
    marker=STATE/f'stop_{session}_{dataset.name}.json'
    saved=json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {'gates':[]}
    for gate,note in gates:
        if gate in saved['gates']:continue
        saved['gates'].append(gate);notes.append(note)
    marker.write_text(json.dumps(saved,ensure_ascii=False),encoding='utf-8')
    emit({'decision':'block','reason':'；'.join(notes)[:2048]} if notes else {});return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:emit({'reason':'收工阶段门读取失败，未完成交付检查；请人工核对'})
