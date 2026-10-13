"""CLI/MCP脱敏实跑，扫描全部ZIP成员、单元格和清单。"""
import contextlib,io,json,subprocess,tempfile,zipfile,html,sys
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools_anonymize import anonymize_directory,HASH_PATTERNS
from tools.anonymize_dataset import anonymize_dataset

def main():
    original=[]
    with tempfile.TemporaryDirectory(prefix='p4_anon_') as temp:
        work=Path(temp);source=work/'input';source.mkdir();key=work/'synthetic.key'
        fake_key='synthetic-test-key-only';key.write_text(fake_key+'\nignored-second-line',encoding='utf-8')
        for index,columns in enumerate((
            ['交易对方账号','交易对方','商户单号','微信单号','金额','交易时间','备注','支付方式'],
            ['对方账号','交易对方','支付宝交易号','商家订单号','金额','创建时间','备注','收/支'],
            ['对方账号','对方户名','流水号','借方','贷方','余额','交易日期','摘要'],
            ['付款支付帐号','收款支付帐号','付款银行卡号','收款银行卡号','收款方的商户名称','交易支付设备IP','金额','交易时间','交易摘要'],
        )):
            row={};tokens=[]
            for col in columns:
                if any(mark in col for mark in HASH_PATTERNS):
                    token=f'synthetic-secret-{index}-{len(tokens)}';row[col]=token;tokens.append(token);original.append(token)
                elif col in ('金额','借方','贷方','余额'):row[col]='12345.67'
                elif '时间' in col or '日期' in col:row[col]='2026-01-01 12:34:56'
                elif col in ('备注','摘要','交易摘要'):row[col]='合成备注 '+ ' '.join(tokens)+' 13812345678 AA:BB:CC:DD:EE:FF'
                else:row[col]='业务保留'
            pd.DataFrame([row]).to_excel(source/f'fixture_{index}.xlsx',index=False)
        (source/'broken.xlsx').write_bytes(b'synthetic invalid xlsx')
        (source/'unsupported.txt').write_text('synthetic',encoding='utf-8')
        out=work/'direct';capture=io.StringIO()
        with contextlib.redirect_stdout(capture):r=anonymize_directory(source,out,key)
        assert capture.getvalue()=='' and len(r['文件清单'])==6 and r['输出文件数']==4
        assert sum(x['状态']=='失败' for x in r['文件清单'])==1 and sum(x['状态']=='跳过' for x in r['文件清单'])==1
        for channel in ('mcp','cli'):
            target=work/channel
            if channel=='mcp':
                with contextlib.redirect_stdout(capture):v=anonymize_dataset(str(source),str(target),str(key))
                assert v['结论']['成功'] and Path(v['artifacts']['清单路径']).exists()
            else:
                proc=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve().parents[1]/'tools_anonymize.py'),str(source),str(target),str(key)],capture_output=True,text=True,encoding='utf-8')
                assert proc.returncode==1  # 有预设损坏文件，CLI明确失败
                assert json.loads(proc.stdout)['输出文件数']==4
            originals=original+[fake_key,'13812345678','AA:BB:CC:DD:EE:FF']
            for path in target.iterdir():
                contents=[path.read_bytes()]
                if path.suffix=='.xlsx':
                    with zipfile.ZipFile(path) as archive:contents.extend(archive.read(n) for n in archive.namelist())
                    df=pd.read_excel(path,dtype=str)
                    contents.append(df.to_csv(index=False).encode('utf-8'))
                decoded='\n'.join(html.unescape(b.decode('utf-8',errors='ignore')) for b in contents)
                assert not any(token in decoded for token in originals),path.name
        # 无坏文件的CLI成功出口。
        clean=work/'clean';clean.mkdir();pd.DataFrame([{'对方账号':'synthetic-clean-id','金额':1234,'交易时间':'2026-01-01'}]).to_excel(clean/'clean.xlsx',index=False)
        proc=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve().parents[1]/'tools_anonymize.py'),str(clean),str(work/'clean_out'),str(key)],capture_output=True,text=True,encoding='utf-8')
        assert proc.returncode==0 and json.loads(proc.stdout)['输出文件数']==1
        for bad in ((work/'missing',work/'out',key),(source,source/'nested',key),(source,work,key),(source,work/'out',work/'missing.key')):
            with contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(capture):error=anonymize_dataset(*(str(p) for p in bad))
            assert error['结论']['成功'] is False and fake_key not in json.dumps(error)
    print('P4 ALL PASS: CLI/第11工具；四类账单敏感列；ZIP全成员+单元格+清单泄漏0命中；失败/跳过清单；坏参数；stdout与密钥纪律')
if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
