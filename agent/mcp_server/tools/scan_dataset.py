"""数据受理与完整性体检。"""
import uuid
from pathlib import Path
import pandas as pd
from common import CACHE, ga, ok, tool, finish, folder

@tool
def scan_dataset(folder_path: str, identity_path: str | None = None) -> dict:
    """扫描本机账单目录，返回文件解析状态、失败清单、身份未核实比例及 dataset_id。"""
    path = Path(folder_path).resolve()
    identity = ga.IdentityMapper()
    if identity_path:
        # 旧身份读取器内部有异常吞咽，先用严格读取核验文件结构。
        ip = Path(identity_path).resolve()
        raw = pd.read_excel(ip, dtype=str) if ip.suffix.lower() in ('.xlsx', '.xls') else pd.read_csv(ip, dtype=str, encoding='utf-8-sig', on_bad_lines='error')
        raw.columns = raw.columns.str.strip()
        if not identity._col(list(raw.columns), identity._COL_ACCOUNT) or not identity._col(list(raw.columns), identity._COL_NAME):
            raise ValueError('身份表必须包含账号与姓名列')
        identity.load(str(ip))
        if not identity.mapping:
            raise ValueError('身份表未成功注册任何映射')
    files = ga.scan_folder(str(path))
    parsed, status, issues = [], [], []
    for file in files:
        if identity_path and file.resolve() == Path(identity_path).resolve():
            continue
        item = {'文件': str(file)}
        try:
            # CSV 严格读取，避免旧 auto_read(on_bad_lines=skip) 静默丢行。
            if file.suffix.lower() == '.csv':
                attempts = []
                for enc in ('utf-8-sig', 'gb18030'):
                    try:
                        raw = pd.read_csv(file, dtype=str, encoding=enc, on_bad_lines='error')
                        break
                    except UnicodeDecodeError as exc:
                        attempts.append(str(exc))
                else:
                    raise ValueError('CSV 编码解析失败：' + '; '.join(attempts))
                raw.columns = raw.columns.str.strip()
            else:
                raw = ga.DataLoader.auto_read(str(file))
            kind = ga.DataLoader.detect_type(raw)
            if kind == 'unknown':
                raise ValueError('未知账单类型，禁止默认猜测为银行流水')
            cols = ' '.join(raw.columns)
            if not any(k in cols for k in ('时间', '日期')) or not any(k in cols for k in ('金额', '借方', '贷方', '发生额')):
                raise ValueError('缺少时间或金额关键列')
            if kind in ('wechat', 'alipay') and not any(k in cols for k in ('收/支', '收支', '交易类型', '资金状态', '借贷标志')):
                raise ValueError('缺少收支方向列，禁止将全部记录猜测为支出')
            owner = ga.extract_owner_from_filename(file, identity)
            norm = getattr(ga.DataLoader, 'normalize_' + kind)(raw, owner)
            norm['source_file'] = str(file)
            invalid = {'无效时间': int(norm.datetime_parsed.isna().sum()), '非正金额': int((norm.amount_clean <= 0).sum()),
                       '空对手': int(norm.counterpart_id.fillna('').str.strip().eq('').sum())}
            if any(invalid.values()):
                issues.append({'文件': str(file), '数据质量': invalid})
            if not any(k in cols for k in ('对方账号', '对方账户', '付款支付帐号', '收款支付帐号', '收/付款方账号', '交易对方账号')):
                issues.append({'文件': str(file), '警告': '缺少显式对手账号，旧归一器可能使用账单自报姓名；身份未经核实。'})
            item.update({'状态': '成功', '类型': kind, '原始行数': len(raw), '标准行数': len(norm), '质量': invalid})
            parsed.append(norm)
        except Exception as exc:
            item.update({'状态': '失败', '原因': str(exc)})
            issues.append(item.copy())
        status.append(item)
    tx = pd.concat(parsed, ignore_index=True) if parsed else pd.DataFrame()
    dataset_id = uuid.uuid4().hex
    ctx = {'id': dataset_id, 'tx': tx, 'identity': identity, 'issues': issues, 'path': str(path)}
    CACHE[dataset_id] = ctx
    accounts = set()
    if not tx.empty:
        accounts = set(tx.owner_id) | set(tx.counterpart_id.fillna(''))
    accounts = {identity._normalize_key(str(a)) for a in accounts if str(a).strip()}
    unverified = sorted(a for a in accounts if a not in identity.mapping)
    times = tx.datetime_parsed.dropna() if not tx.empty else pd.Series(dtype='datetime64[ns]')
    result = finish(ctx, '01_intake', ok({'dataset_id': dataset_id, '文件数': len(status), '交易数': len(tx), '可建图': not tx.empty},
        {'文件清单': status, '失败清单': [s for s in status if s['状态'] == '失败'],
         '未核实比例': len(unverified) / max(len(accounts), 1), '未核实账号': unverified,
         '未核实口径': '未命中身份表的账号数/非空唯一账号数；姓名与账号均未经人工真实性核验',
         '时间跨度': [str(times.min()), str(times.max())] if len(times) else [], '金额单位': '元'},
        issues + ([] if len(tx) else ['没有有效记录，不能建图'])))
    intake = folder(dataset_id) / '01_intake.md'
    import json
    intake.write_text('# 数据受理与完整性\n\n金额单位：元；仅报告流水线索，不换算赌资。\n\n```json\n'
        + json.dumps(ctx['results']['01_intake'], ensure_ascii=False, indent=2, default=str) + '\n```\n', encoding='utf-8')
    result['artifacts']['受理记录'] = str(intake)
    return result
