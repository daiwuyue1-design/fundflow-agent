"""只生成虚构账单，不读取真实流水。"""
from pathlib import Path
import pandas as pd
from common import DELIVERABLES

def generate(root: Path | None = None) -> Path:
    root = root or DELIVERABLES / 'smoke_fixtures'
    root.mkdir(parents=True, exist_ok=True)
    wx = []
    for i in range(12):
        wx.append({'交易时间': f'2026-01-01 01:{i:02d}:00', '交易对方账号': f'synthetic_member_{i:02d}',
                   '交易对方': f'虚构样例{i}', '收/支': '收入', '金额': str(1000 + i * 200),
                   '商户单号': f'SYN-WX-{i}', '支付方式': '零钱', '商品': '合成充值', '备注': '虚构测试'})
    wx += [dict(wx[0]), {'交易时间': '2026-01-02 12:00:00', '交易对方账号': '零钱通',
                        '交易对方': '零钱通', '收/支': '支出', '金额': '50', '商户单号': 'SYN-INTERNAL', '支付方式': '零钱'}]
    pd.DataFrame(wx).to_excel(root / 'synthetic_hub_微信.xlsx', index=False)
    al = [{'创建时间': '2026-01-01 10:00:00', '对方账号': 'synthetic_receiver', '交易对方': '虚构收款账户',
           '收/支': '支出', '金额': '6000', '支付宝交易号': 'SYN-ALI', '交易状态': '成功', '备注': '虚构测试'}]
    al.append(dict(al[0], 创建时间='2026-01-01 10:01:00', **{'收/支': '收入', '支付宝交易号': 'SYN-ALI-MIRROR'}))
    pd.DataFrame(al).to_excel(root / 'synthetic_alipay_支付宝.xlsx', index=False)
    bank = [{'交易日期': '2026-01-01 11:00:00', '对方账号': 'synthetic_receiver', '对方户名': '虚构收款账户',
             '借方': '2000', '贷方': '0', '余额': '5000', '流水号': 'SYN-BANK', '摘要': '虚构测试'},
            {'交易日期': '2026-01-01 12:00:00', '对方账号': '银行卡', '对方户名': '虚构银行账户',
             '借方': '20', '贷方': '0', '余额': '4980', '流水号': 'SYN-SELF', '摘要': '虚构自转账'}]
    pd.DataFrame(bank).to_excel(root / 'synthetic_bank_银行卡.xlsx', index=False)
    (root / 'synthetic_broken.xlsx').write_bytes(b'not an Excel document')
    return root

if __name__ == '__main__':
    print(generate())
