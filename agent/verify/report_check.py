# coding: utf-8
"""Phase 4 完整版验收检查。"""
import argparse,csv,json,math,re,sys
from html.parser import HTMLParser
from pathlib import Path
REQUIRED=("01_intake.md","02_cleaned.csv","03_entities.csv","04_graph_stats.md","05_tiers.csv","05_laundering.csv","06_report.html","06_evidence_index.json")
HEADINGS=("数据来源与完整性","组织结构","层级认定及依据","资金穿透","洗钱线索","重点人员","待核实事项","侦查建议")
FORBIDDEN=("认定","定罪","犯罪事实")
DISCLAIMER=("供研判方向对照，最终认定以司法机关为准","最终认定以司法机关为准","最终司法结论由司法机关作出","仅供侦查核实，嫌疑≠司法结论")
class P(HTMLParser):
 def __init__(self):
  super().__init__();self.s=[];self.visible=[];self.headings=[];self.claims=[];self.scope="";self.sections={};self.current=None;self.all_visible=[]
 def handle_starttag(self,t,a):
  a=dict(a);r={"tag":t,"attrs":a,"text":""}
  if t=='section':self.current=None
  if t not in ("meta","link","br","hr","img","input"): self.s.append(r)
 def handle_data(self,d):
  for r in self.s:r["text"]+=d
  if not any(r['tag'] in ('script','style') for r in self.s):self.all_visible.append(d)
  if self.current is not None and not any(r['tag'] in ('script','style') for r in self.s):self.sections[self.current]+=d+' '
  if not any(r["tag"] in ("script","style","blockquote") for r in self.s):self.visible.append(d)
 def handle_endtag(self,t):
  for i in range(len(self.s)-1,-1,-1):
   if self.s[i]["tag"]==t:
    rs=self.s[i:];self.s=self.s[:i]
    for r in rs:
     if r["tag"]=="h2":
      title=r['text'].strip();self.headings.append(title);self.current=title;self.sections.setdefault(title,'')
     if "data-tool" in r["attrs"] or "data-pointer" in r["attrs"]:self.claims.append((r["attrs"],r["text"].strip()))
     if r["attrs"].get("id")=="report-scope":self.scope=r["text"]
    break
def ptr(x,p):
 if not isinstance(p,str) or not p.startswith("/"):raise ValueError("证据pointer须为JSON Pointer")
 for k in p[1:].split("/"):
  k=k.replace("~1","/").replace("~0","~");x=x[int(k)] if isinstance(x,list) else x[k]
 return x

def number(text):
 match=re.fullmatch(r'\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*(万元|万|元)?\s*',text)
 if not match:raise ValueError('无法解析数值或金额单位')
 return float(match[1].replace(',','')),match[2]

def chapter_checks(q,rs,issues,warnings,checks):
 before=len(issues)
 def count(label,patterns,expected,text):
  values=[]
  for pattern in patterns:values.extend(re.findall(pattern,text))
  if not values:warnings.append('章级数字无法唯一匹配：'+label);return
  if len(set(values))>1:warnings.append('章级数字有多个表述：'+label)
  if any(int(value)!=expected for value in values):issues.append(f'章级数字不一致：{label}，证据={expected}，报告={values}')
 dist=rs['05_tiers']['结论']['层级分布']
 text=q.sections.get('层级认定及依据','')
 for label,value in dist.items():
  count(label,[re.escape(label)+r'["\s:：]*([0-9]+)'],value,text)
 graph=rs['04_graph_stats']['结论'];text=q.sections.get('组织结构','')
 for label in ('节点数','边数'):count(label,[label+r'\s*[:：]?\s*(\d+)'],graph[label],text)
 count('洗钱命中数',[r'(?:洗钱特征嫌疑节点数|洗钱命中数|嫌疑节点数)\s*[:：]?\s*(\d+)'],rs['05_laundering']['结论']['嫌疑节点数'],q.sections.get('洗钱线索',''))
 failures=rs['01_intake']['证据']['失败清单'];text=q.sections.get('数据来源与完整性','')+' '+q.sections.get('待核实事项','')
 values=re.findall(r'(?:坏文件数|失败文件数|解析失败数)\s*[:：]?\s*(\d+)',text)
 if values:
  if any(int(v)!=len(failures) for v in values):issues.append('失败文件数量不一致')
 else:warnings.append('未单独呈现失败文件总数；核对逐文件清单')
 for item in failures:
  filename=Path(item['文件']).name;reason=str(item['原因'])
  if filename not in text or reason.replace('\\','') not in text.replace('\\',''):issues.append('失败文件/原因未完整呈现：'+filename)
 checks['章级关键数字']=len(issues)==before
def check(directory):
 root=Path(directory).resolve();issues=[];warnings=[];checks={}
 missing=[n for n in REQUIRED if not (root/n).is_file() or (root/n).stat().st_size==0];checks["交付物01–06"]=not missing;issues += ["缺少或为空："+n for n in missing]
 q=P();report=root/"06_report.html"
 if report.is_file():
  try:q.feed(report.read_text(encoding="utf-8"))
  except Exception as e:issues.append("报告读取失败："+str(e))
 visible='\n'.join(q.visible)
 conclusion=visible
 for allowed in (*HEADINGS,*DISCLAIMER):conclusion=conclusion.replace(allowed,'')
 bad=[x for x in FORBIDDEN if x in conclusion];checks["表述红线"]=not bad;issues += ["结论表述禁用词："+x for x in bad]
 checks["免责句"]=any(x in visible for x in DISCLAIMER)
 if not checks["免责句"]:issues.append("缺少供研判方向对照及最终认定以司法机关为准的免责句")
 checks["八章结构"]=tuple(q.headings[:8])==HEADINGS
 if not checks["八章结构"]:issues.append("报告八章缺失或顺序不一致")
 try:
  ev=json.loads((root/"06_evidence_index.json").read_text(encoding="utf-8"));rs=ev["工具结果"];tiers=rs["05_tiers"]["结论"].get("判定",[])
  checks["依据三元组"]=bool(tiers) and all(all(r.get("依据",{}).get(k) for k in ("指标值","百分位","适用规则")) for r in tiers)
  if not checks["依据三元组"]:issues.append("层级判定缺少依据三元组")
  lr=rs["05_laundering"]["结论"].get("判定",[])
  dimensions={'境外关键词','高过路率','快速中转','整数金额','单线中转'}
  checks["洗钱证据"]=bool(lr) and all(isinstance(r.get('五维'),list) and len(r['五维'])==5 and {x.get('维度') for x in r['五维']}==dimensions and r.get('命中说明') is not None and all(x.get('阈值') is not None and x.get('说明') and x.get('得分') is not None for x in r['五维']) for r in lr)
  if not checks["洗钱证据"]:issues.append("洗钱判定缺少五维、阈值或命中说明")
  st=rs.get("05_structure",{}).get("结论",{})
  if st.get("完整") is False:
   failed=[k for k,v in st.get("检测器状态",{}).items() if v.get("状态")=="失败"];warnings.append("结构检测不完整；失败检测器："+",".join(failed or ["未提供"]))
   for name in failed:
    if name not in visible or str(st['检测器状态'][name].get('原因','')) not in visible:issues.append('结构检测失败未在报告正文显式列出：'+name)
  valid=0
  for a,t in q.claims:
   try:
    e=ptr(rs[a["data-tool"]],a["data-pointer"]);v,unit=number(t)
    amount=any(k in a['data-pointer'] for k in ('金额','流入','流出','in_flow','out_flow','total_flow'))
    if unit and not amount:raise ValueError('非金额数值附带金额单位')
    if unit in ('万','万元'):v*=10000
    if isinstance(e,bool) or not isinstance(e,(int,float)) or not math.isfinite(v) or not math.isfinite(e) or not math.isclose(v,e,rel_tol=1e-9,abs_tol=1e-6):raise ValueError("报告与证据不一致")
    valid+=1
   except (KeyError,ValueError,TypeError,IndexError) as e:issues.append("数值回溯失败 "+str(a)+"："+str(e))
  if not q.claims:issues.append("缺少可回溯数值标注data-tool/data-pointer")
  checks["标注数值回溯"]=bool(q.claims) and valid==len(q.claims)
  scope=ev.get("口径",{});rscope=json.loads(q.scope) if q.scope else {}
  for k in ("金额单位","时间范围","账号范围","金额类型","平台内转账"):
   if not scope.get(k) or rscope.get(k)!=scope.get(k):issues.append("口径缺失或不一致："+k)
  ie=rs["01_intake"]["证据"]
  if scope.get("金额单位")!=ie.get("金额单位") or scope.get("时间范围")!=ie.get("时间跨度"):issues.append("口径与受理证据不一致")
  checks["口径一致性"]=not any("口径" in x for x in issues)
  chapter_checks(q,rs,issues,warnings,checks)
  lf=root/"05_laundering.csv"
  if lf.is_file():
   with lf.open(encoding="utf-8-sig",newline="") as f:cols=next(csv.reader(f),[])
   required=("账号","总分","封顶前得分","洗钱嫌疑","命中说明","五维","纳入分析");checks["洗钱表列"]=all(x in cols for x in required)
   if not checks["洗钱表列"]:warnings.append("05_laundering.csv列不完整")
  if not ev.get("Trace引用"):warnings.append("未提供AGH Trace引用")
  checks["金额单位标注"]=bool(re.search(r"元|万元|人民币",visible))
  if not checks["金额单位标注"]:issues.append("报告正文未标注金额单位")
  for unit in re.findall(r'金额单位["\s:：]*(万元|元)',visible):
   if unit!=scope.get('金额单位'):issues.append('正文金额单位声明与scope不一致：'+unit)
  for value,unit in re.findall(r'(\d+(?:\.\d+)?)\s*(万元|元)',visible):
   if unit=='万元' and scope.get('金额单位')=='元':
    if not any('万元' in t for _,t in q.claims):warnings.append('正文万元数值未绑定唯一证据，需人工核对换算：'+value)
 except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as e:issues.append("证据索引或口径读取失败："+type(e).__name__+"："+str(e))
 warnings.append("自动检查不能证明自由文本全部数字、法源逐字真实性、实际人数或司法事实；需人工复核")
 return {"状态":"FAIL" if issues else "PASS","检查项":checks,"问题":issues,"警告":warnings,"目录":str(root),"标注数值数":len(q.claims),"验收范围":"Phase 4五项完整版；警告不自动判FAIL"}
def main():
 p=argparse.ArgumentParser();p.add_argument("directory");a=p.parse_args();r=check(a.directory);root=Path(a.directory).resolve()
 if not root.is_dir():print(json.dumps(r,ensure_ascii=False,indent=2));return 1
 destination=root/'07_verify.md'
 r['检查项']['交付物07验收记录']=True
 destination.write_text('# 初版验收检查\n\n'+r['状态']+'\n\n```json\n'+json.dumps(r,ensure_ascii=False,indent=2)+'\n```\n',encoding='utf-8')
 r['检查项']['交付物07验收记录']=destination.is_file() and destination.stat().st_size>0
 print(json.dumps(r,ensure_ascii=False,indent=2));return 0 if r['状态']=='PASS' else 1
if __name__=="__main__":
 if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
 raise SystemExit(main())
