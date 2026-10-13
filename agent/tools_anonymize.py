"""合成/授权分享前的账单脱敏：密钥只从文件读取，绝不进入结果。"""
import argparse
import hashlib
import hmac
import json
import re
import math
from datetime import datetime
from pathlib import Path
import pandas as pd

HASH_PATTERNS = ('账号', '帐号', '账户', '卡号', '姓名', '户名', '交易对方', '流水号', '单号', '订单号', '交易号', '凭证号', '终端号', 'IP', 'MAC', '设备', '商户', '商家', '手机', '身份证', '邮箱')
AMOUNT_COLUMNS = ('金额', '余额', '借方', '贷方', '发生额', '交易金额', '实付金额')
TIME_MARKERS = ('时间', '日期', '创建时间', '交易日期', '记账日期')
SUMMARY_MARKERS = ('备注', '摘要', '描述', '附言', '商品', '交易说明')

def _key(path):
    path = Path(path)
    if not path.is_file(): raise FileNotFoundError('密钥文件不存在')
    raw = path.read_bytes().splitlines()
    if not raw or not raw[0].strip(): raise ValueError('密钥文件首行为空')
    return raw[0].strip()

def _hash(value, key):
    if pd.isna(value) or str(value).strip() == '': return ''
    return hmac.new(key, str(value).strip().encode('utf-8'), hashlib.sha256).hexdigest()

def _bucket(value):
    try: amount = abs(float(str(value).replace(',', '').replace('¥', '').replace('￥', '').strip()))
    except (ValueError, TypeError): return '未知'
    if not math.isfinite(amount):return '未知'
    for limit, label in ((100,'0-100'),(1000,'100-1000'),(5000,'1000-5000'),(10000,'5000-10000'),(50000,'1万-5万'),(100000,'5万-10万'),(500000,'10万-50万'),(1000000,'50万-100万')):
        if amount < limit: return label
    return '100万以上'

def _date(value):
    parsed = pd.to_datetime(value, errors='coerce')
    return '' if pd.isna(parsed) else parsed.strftime('%Y-%m-%d')

def _mask(value, sensitive=()):
    if pd.isna(value): return ''
    text = str(value)
    for token in sensitive:
        if token:text=text.replace(token,'[敏感字段]')
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号]', text)
    text = re.sub(r'(?<!\d)\d{17}[\dXx](?!\d)', '[身份证号]', text)
    text = re.sub(r'(?<!\d)\d{12,30}(?!\d)', '[长数字账号]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP地址]', text)
    text = re.sub(r'\b[0-9A-Fa-f]{2}[:-](?:[0-9A-Fa-f]{2}[:-]){4}[0-9A-Fa-f]{2}\b','[MAC地址]',text)
    return text

def _safe_output(input_dir, output_dir):
    src = Path(input_dir).resolve(); dst = Path(output_dir).resolve()
    if not src.is_dir(): raise NotADirectoryError('输入目录不存在或不是目录')
    if dst == src or src in dst.parents or dst in src.parents: raise ValueError('输入输出目录不能相等或互相包含')
    return src, dst

def anonymize_directory(input_dir, output_dir, key_file):
    src, dst = _safe_output(input_dir, output_dir); key = _key(key_file)
    if Path(key_file).resolve().is_relative_to(src) or Path(key_file).resolve().is_relative_to(dst):raise ValueError('密钥文件必须位于输入输出目录之外')
    dst.mkdir(parents=True,exist_ok=True)
    files = sorted(p for p in src.iterdir() if p.is_file())
    results=[]; used_columns=set()
    for index, path in enumerate(files, 1):
        item={'输入文件': f'文件{index:04d}', '状态':'失败'}
        if path.suffix.lower() not in ('.xlsx','.xls','.csv'):
            item.update({'状态':'跳过','原因':'不支持的文件扩展名'});results.append(item);continue
        try:
            if path.suffix.lower()=='.csv': df=pd.read_csv(path,dtype=str)
            else: df=pd.read_excel(path,dtype=str)
            df.columns=[str(c).strip() for c in df.columns]; out=pd.DataFrame(index=df.index); handled=set()
            sensitive=sorted({str(v).strip() for c in df.columns if any(m in c for m in HASH_PATTERNS) for v in df[c].dropna() if str(v).strip()},key=len,reverse=True)
            for col in df.columns:
                if any(mark in col for mark in HASH_PATTERNS): out[col+'_hash']=df[col].map(lambda x:_hash(x,key));handled.add(col);used_columns.add(col)
                elif any(mark in col for mark in AMOUNT_COLUMNS): out[col+'区间']=df[col].map(_bucket);handled.add(col);used_columns.add(col)
                elif any(mark in col for mark in TIME_MARKERS): out[col+'日期']=df[col].map(_date);handled.add(col);used_columns.add(col)
                elif any(mark in col for mark in SUMMARY_MARKERS): out[col+'脱敏']=df[col].map(lambda v:_mask(v,sensitive));handled.add(col);used_columns.add(col)
                else: out[col]=df[col]
            filename=f'anonymized_{index:04d}.xlsx'; target=dst/filename;out.to_excel(target,index=False)
            item.update({'状态':'成功','输出文件':filename,'行数':len(out),'识别列':sorted(handled),'未识别列':sorted(set(df.columns)-handled)})
        except Exception as exc: item['原因']=f'{type(exc).__name__}: 解析或脱敏写出失败（不回显原值）'
        results.append(item)
    if not files: results.append({'输入文件':'','状态':'跳过','原因':'输入目录没有xlsx/xls/csv文件'})
    result={'文件清单':results,'识别列':sorted(used_columns),'策略':'HMAC-SHA256/金额分箱/时间按日/摘要规则掩码；敏感原列不写出','输出文件数':sum(x['状态']=='成功' for x in results)}
    manifest=dst/'manifest.json';manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');result['清单路径']=str(manifest)
    return result

def main():
    p=argparse.ArgumentParser(description='账单脱敏CLI；密钥只读文件且不打印');p.add_argument('input_dir');p.add_argument('output_dir');p.add_argument('key_file');a=p.parse_args()
    result=anonymize_directory(a.input_dir,a.output_dir,a.key_file);print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if all(x['状态'] in ('成功','跳过') for x in result['文件清单']) else 1
if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    try:raise SystemExit(main())
    except (ValueError,OSError) as exc:
        print(json.dumps({'成功':False,'原因':str(exc)},ensure_ascii=False));raise SystemExit(1)
