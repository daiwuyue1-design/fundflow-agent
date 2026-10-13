#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网赌团伙资金链分析系统
============================================================
功能：自动解析微信/支付宝/银行账单，识别团伙层级，生成可视化报告
作者：侦查辅助系统
版本：2.0
"""

import os
import time
import sys
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import warnings
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np
import networkx as nx
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

warnings.filterwarnings('ignore')

# ═══════════════════════════════════════════════════════════
# 配置区
# ═══════════════════════════════════════════════════════════
# 优先使用现有“结果”目录，兼容当前项目结构；否则回退到 output
_BASE_DIR = Path(__file__).parent
OUTPUT_DIR = _BASE_DIR / ("结果" if (_BASE_DIR / "结果").exists() else "output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 网赌相关关键词（可自行扩充）
GAMBLING_KEYWORDS = [
    '博彩', '彩票', '竞猜', '足彩', '欧赔', '亚盘', '走地', '让球',
    '棋牌', '德州', '百家乐', '轮盘', '骰子', '老虎机', '捕鱼',
    '充值', '提现', '返点', '洗码', '代理', '招募', '下注', '赔率',
    'bet', 'sport', 'game', '球队', '比分', '赛事',
]

# 时间聚集阈值（分钟）- 短时间内大量交易视为批量操作
BURST_TIME_WINDOW = 5
BURST_MIN_COUNT = 5

# ── 人员层级体系（赌球团伙金字塔结构）──────────────────────────────────────
# 层级编号（数字越小层级越高）
LEVEL_AGENT1  = 1   # 一级代理（总代/庄家）：汇聚全局资金，向下分配，高流量、高出度
LEVEL_AGENT2  = 2   # 二级代理（中级代理）：承上启下，有明显的上家和多个下家
LEVEL_AGENT3  = 3   # 三级代理（基层代理）：直接管理赌客，资金规模相对小
LEVEL_GAMBLER = 4   # 赌客（会员）：只有小额充值/投注，极少向上转账，主要是单向流出
LEVEL_UNKNOWN = 5   # 账务/洗钱/待定外围人员（无法归类到上述四类）

# 层级阈值（依据典型网赌案例经验值，可根据实际数据调整）
LEVEL1_MIN_INFLOW  = 500_000   # 一级代理：累计流入 >= 50万
LEVEL2_MIN_INFLOW  = 50_000    # 二级代理：累计流入 >= 5万
LEVEL3_MIN_INFLOW  = 5_000     # 三级代理：累计流入 >= 5千
# 赌客：流入 < 5千，或几乎只有支出，下游出度极少

# ── 洗钱识别参数 ────────────────────────────────────────────────────────
# 境外账户关键词（转账对方账号/备注含这些词，视为境外资金流动）
OVERSEAS_KEYWORDS = [
    'overseas', 'international', '境外', '海外', '汇款', '外汇',
    'usdt', 'btc', 'bitcoin', 'eth', 'crypto', 'tether',
    'western union', 'moneygram', 'paypal', 'wise', 'payoneer',
    '澳门', '香港', 'hk', 'macau', 'philippines', '菲律宾',
    '柬埔寨', 'cambodia', '缅甸', 'myanmar', '越南', 'vietnam',
    'overseas_transfer', 'remittance',
]

# 洗钱特征阈值
MONEY_LAUNDER_TRANSIT_RATIO = 0.8   # 流入后 >=80% 转出：典型中转账户
MONEY_LAUNDER_MIN_FLOW      = 30_000  # 至少 3万流水才纳入洗钱分析
MONEY_LAUNDER_MAX_BALANCE_RATIO = 0.15  # 净余额 < 流入 15%：钱没有留存，只是过路
MONEY_LAUNDER_BURST_INTERVAL = 24   # 小时：收到钱后 24h 内转出视为高风险中转


# ═══════════════════════════════════════════════════════════
# 模块一：数据读取与统一格式化
# ═══════════════════════════════════════════════════════════
class DataLoader:
    """统一读取微信/支付宝/银行账单"""

    # 大文件阈值：超过此大小使用优化读取策略
    LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50MB

    @staticmethod
    def auto_read(filepath: str) -> pd.DataFrame:
        """自动检测文件格式并读取（优化大文件性能）"""
        p = Path(filepath)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {filepath}")

        file_size = p.stat().st_size
        is_large_file = file_size > DataLoader.LARGE_FILE_THRESHOLD

        suffix = p.suffix.lower()
        try:
            if suffix in ['.xlsx', '.xls']:
                # 对于大Excel文件，先尝试用引擎参数优化
                if is_large_file:
                    print(f"  [PKG] 检测到较大Excel文件 ({file_size/1024/1024:.1f}MB)，使用优化读取...")

                # 尝试跳过前几行（微信/支付宝账单通常有说明头）
                # 大文件时优先尝试常见的skip值，减少尝试次数
                skip_candidates = [0, 1, 16, 17] if is_large_file else [0, 1, 2, 3, 4, 16, 17]

                for skip in skip_candidates:
                    try:
                        # 大文件时使用openpyxl引擎（通常更快）
                        engine = 'openpyxl' if is_large_file else None
                        df = pd.read_excel(filepath, skiprows=skip, dtype=str, engine=engine)
                        if len(df.columns) >= 5 and len(df) > 0:
                            df.columns = df.columns.str.strip()
                            return df
                    except Exception:
                        continue

            elif suffix == '.csv':
                # 对于大CSV文件，优先尝试最可能的编码，减少尝试次数
                if is_large_file:
                    print(f"  [PKG] 检测到较大CSV文件 ({file_size/1024/1024:.1f}MB)，使用优化读取...")
                    # 大文件时只尝试最可能的两种编码
                    enc_candidates = ['utf-8-sig', 'gb18030']
                    skip_candidates = [0, 1, 16]
                else:
                    enc_candidates = ['utf-8-sig', 'gbk', 'gb18030', 'utf-8']
                    skip_candidates = [0, 1, 2, 3, 4, 16, 17]

                for enc in enc_candidates:
                    for skip in skip_candidates:
                        try:
                            df = pd.read_csv(filepath, encoding=enc,
                                             skiprows=skip, dtype=str,
                                             on_bad_lines='skip',
                                             low_memory=False)  # 避免混合类型推断警告
                            if len(df.columns) >= 5 and len(df) > 0:
                                df.columns = df.columns.str.strip()
                                return df
                        except Exception:
                            continue
        except Exception as e:
            raise ValueError(f"读取文件失败: {e}")

        raise ValueError(f"无法解析文件格式: {filepath}")

    @staticmethod
    def detect_type(df: pd.DataFrame) -> str:
        """自动判断账单类型（微信/支付宝/银行/双向流水）"""
        cols_str = ' '.join(str(c) for c in df.columns).lower()

        # ── 优先检测双向流水（第三方支付监控导出：付款账号+收款账号同时存在）──
        bilateral_signals = ['付款支付帐号', '付款银行卡号', '收款支付帐号', '收款银行卡号',
                             '付款银行卡银行名称', '收款银行卡银行名称', '交易主体的出入账标识']
        bi_score = sum(1 for s in bilateral_signals if s.lower() in cols_str)
        if bi_score >= 3:
            return 'bilateral'

        # 微信：特征列"商户单号"/"支付方式"/"收/支"+"商品"
        wechat_signals = ['商户单号', '支付方式', '当前状态', '微信单号']
        alipay_signals = ['商家订单号', '支付宝交易号', '对方账号', '交易状态', '实际金额']
        bank_signals   = ['借方', '贷方', '余额', '发生额', '账号', '卡号']

        wx_score = sum(1 for s in wechat_signals if s.lower() in cols_str)
        al_score = sum(1 for s in alipay_signals if s.lower() in cols_str)
        bk_score = sum(1 for s in bank_signals  if s.lower() in cols_str)

        # 收/支 是微信/支付宝共有特征
        if '收/支' in cols_str or '收支' in cols_str:
            wx_score += 1
            al_score += 1

        best = max(wx_score, al_score, bk_score)
        if best == 0:
            return 'unknown'
        if wx_score == best:
            return 'wechat'
        if al_score == best:
            return 'alipay'
        return 'bank'

    @staticmethod
    def normalize_wechat(df: pd.DataFrame, owner_id: str) -> pd.DataFrame:
        """微信账单 → 标准格式"""
        col_map = {}
        for c in df.columns:
            cl = c.strip()
            # 时间
            if any(k in cl for k in ['交易时间', '时间', '日期']):
                col_map.setdefault(c, 'datetime')
            # 对方账号（微信号/手机号）
            if any(k in cl for k in ['对方账号', '收/付款方账号', '交易对方账号']):
                col_map[c] = 'counterpart_id'
            # 对方姓名
            elif any(k in cl for k in ['交易对方', '对方', '收/付款方', '姓名', '昵称']):
                if c not in col_map:
                    col_map[c] = 'counterpart_name'
            # 收支方向
            if any(k in cl for k in ['收/支', '收支', '交易类型', '类型', '借贷标志']):
                col_map.setdefault(c, 'direction')
            # 金额
            if any(k in cl for k in ['金额', '交易金额', '实际金额']):
                col_map.setdefault(c, 'amount')
            # 备注
            if any(k in cl for k in ['备注', '说明']):
                col_map.setdefault(c, 'remark')
            # 商品/描述
            if any(k in cl for k in ['商品', '描述', '交易说明']):
                col_map.setdefault(c, 'desc')
            # 单号
            if any(k in cl for k in ['单号', '流水号', '订单号', '交易号', '凭证']):
                col_map.setdefault(c, 'trade_no')

        df = df.rename(columns=col_map).copy()
        df = df.loc[:, ~df.columns.duplicated(keep='first')]
        for r in ['datetime', 'direction', 'amount']:
            if r not in df.columns:
                df[r] = ''
        # counterpart_id 优先取显式账号列，否则用姓名列
        if 'counterpart_id' not in df.columns:
            df['counterpart_id'] = df['counterpart_name'] if 'counterpart_name' in df.columns else ''
        if 'counterpart_name' not in df.columns:
            df['counterpart_name'] = df['counterpart_id']

        df['owner_id'] = owner_id
        df['channel'] = 'wechat'
        df['amount_clean'] = df['amount'].apply(DataLoader._parse_amount)
        df['is_income'] = df['direction'].astype(str).str.contains('收入|入账|转入|收款', na=False)
        df['datetime_parsed'] = pd.to_datetime(df['datetime'], errors='coerce')
        for col in ['remark', 'desc', 'trade_no']:
            if col not in df.columns:
                df[col] = ''
            else:
                df[col] = df[col].fillna('').astype(str)
        df['keyword_hit'] = (df['desc'].astype(str) + df['remark'].astype(str)).apply(
            DataLoader._keyword_match)
        return df[['owner_id', 'channel', 'datetime_parsed', 'counterpart_id',
                   'counterpart_name', 'amount_clean', 'is_income',
                   'trade_no', 'desc', 'remark', 'keyword_hit']].rename(
            columns={'counterpart_name': 'counterpart_name_raw'})

    @staticmethod
    def normalize_alipay(df: pd.DataFrame, owner_id: str) -> pd.DataFrame:
        """支付宝账单 → 标准格式"""
        col_map = {}
        for c in df.columns:
            cl = c.strip()
            if any(k in cl for k in ['创建时间', '交易创建时间', '时间', '日期']):
                col_map.setdefault(c, 'datetime')
            if any(k in cl for k in ['对方账号', '收/付款账号', '收款账号', '付款账号']):
                col_map[c] = 'counterpart_id'
            elif any(k in cl for k in ['交易对方', '对方', '收/付款方', '姓名', '对方姓名']):
                if c not in col_map:
                    col_map[c] = 'counterpart_name'
            if any(k in cl for k in ['收/支', '收支', '交易类型', '类型', '资金状态']):
                col_map.setdefault(c, 'direction')
            if any(k in cl for k in ['金额', '交易金额', '实付金额']):
                col_map.setdefault(c, 'amount')
            if any(k in cl for k in ['备注', '描述', '附言']):
                col_map.setdefault(c, 'remark')
            if any(k in cl for k in ['商品', '说明', '交易说明']):
                col_map.setdefault(c, 'desc')
            if any(k in cl for k in ['单号', '流水号', '订单号', '交易号', '凭证']):
                col_map.setdefault(c, 'trade_no')

        df = df.rename(columns=col_map).copy()
        df = df.loc[:, ~df.columns.duplicated(keep='first')]
        for r in ['datetime', 'direction', 'amount']:
            if r not in df.columns:
                df[r] = ''
        if 'counterpart_id' not in df.columns:
            df['counterpart_id'] = df['counterpart_name'] if 'counterpart_name' in df.columns else ''
        if 'counterpart_name' not in df.columns:
            df['counterpart_name'] = df['counterpart_id']

        df['owner_id'] = owner_id
        df['channel'] = 'alipay'
        df['amount_clean'] = df['amount'].apply(DataLoader._parse_amount)
        df['is_income'] = df['direction'].astype(str).str.contains('收入|入账|收款|转入', na=False)
        df['datetime_parsed'] = pd.to_datetime(df['datetime'], errors='coerce')
        for col in ['remark', 'desc', 'trade_no']:
            if col not in df.columns:
                df[col] = ''
            else:
                df[col] = df[col].fillna('').astype(str)
        df['keyword_hit'] = (df['desc'].astype(str) + df['remark'].astype(str)).apply(
            DataLoader._keyword_match)
        return df[['owner_id', 'channel', 'datetime_parsed', 'counterpart_id',
                   'counterpart_name', 'amount_clean', 'is_income',
                   'trade_no', 'desc', 'remark', 'keyword_hit']].rename(
            columns={'counterpart_name': 'counterpart_name_raw'})

    @staticmethod
    def normalize_bank(df: pd.DataFrame, owner_id: str) -> pd.DataFrame:
        """银行流水 → 标准格式"""
        col_map = {}
        for c in df.columns:
            cl = c.strip()
            if any(k in cl for k in ['交易日期', '记账日期', '日期', '时间', '交易时间']):
                col_map.setdefault(c, 'datetime')
            if any(k in cl for k in ['借方', '支出', '转出金额', '付款金额']):
                col_map.setdefault(c, 'debit')
            if any(k in cl for k in ['贷方', '收入', '转入金额', '收款金额']):
                col_map.setdefault(c, 'credit')
            if any(k in cl for k in ['余额', '账户余额', '可用余额']):
                col_map.setdefault(c, 'balance')
            if any(k in cl for k in ['对方户名', '对方姓名', '收款人', '付款人', '交易对方']):
                col_map.setdefault(c, 'counterpart_name')
            if any(k in cl for k in ['对方账号', '对方账户', '收款账号', '付款账号']):
                col_map[c] = 'counterpart_id'
            if any(k in cl for k in ['摘要', '用途', '交易摘要', '备注', '附言']):
                col_map.setdefault(c, 'desc')
            if any(k in cl for k in ['流水号', '凭证号', '交易流水', '参考号']):
                col_map.setdefault(c, 'trade_no')
            # 借贷合并列（部分银行只有一列金额+正负区分）
            if any(k in cl for k in ['交易金额', '发生额', '金额']):
                col_map.setdefault(c, 'amount_combined')

        df = df.rename(columns=col_map).copy()
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

        # 处理金额：优先用借贷分列，次选合并金额列
        if 'debit' not in df.columns and 'credit' not in df.columns:
            if 'amount_combined' in df.columns:
                amt = df['amount_combined'].apply(DataLoader._parse_amount)
                # 正数=收入(贷方), 负数=支出(借方)
                df['credit'] = amt.clip(lower=0)
                df['debit'] = (-df['amount_combined'].apply(
                    lambda x: float(str(x).replace(',', '').replace('¥', '').strip() or 0)
                    if not pd.isna(x) else 0)).clip(lower=0)
            else:
                df['debit'] = '0'
                df['credit'] = '0'
        for r in ['debit', 'credit']:
            if r not in df.columns:
                df[r] = '0'

        df['debit_amt'] = df['debit'].apply(DataLoader._parse_amount)
        df['credit_amt'] = df['credit'].apply(DataLoader._parse_amount)
        df['amount_clean'] = df.apply(
            lambda r: r['credit_amt'] if r['credit_amt'] > 0 else r['debit_amt'], axis=1)
        df['is_income'] = df['credit_amt'] > 0
        df['owner_id'] = owner_id
        df['channel'] = 'bank'
        df['counterpart_id'] = df['counterpart_id'].fillna('').astype(str) \
            if 'counterpart_id' in df.columns else ''
        df['counterpart_name_raw'] = df['counterpart_name'].fillna('').astype(str) \
            if 'counterpart_name' in df.columns else ''
        # counterpart_id 为空时用 counterpart_name_raw 补充
        mask = df['counterpart_id'].str.strip() == ''
        df.loc[mask, 'counterpart_id'] = df.loc[mask, 'counterpart_name_raw']
        df['datetime_parsed'] = pd.to_datetime(
            df['datetime'].fillna('') if 'datetime' in df.columns else '', errors='coerce')
        df['remark'] = df['desc'].fillna('').astype(str) if 'desc' in df.columns else ''
        df['trade_no'] = df['trade_no'].fillna('').astype(str) \
            if 'trade_no' in df.columns else ''
        df['keyword_hit'] = df['remark'].apply(DataLoader._keyword_match)
        out = df[['owner_id', 'channel', 'datetime_parsed', 'counterpart_id',
                  'counterpart_name_raw', 'amount_clean', 'is_income',
                  'trade_no', 'remark', 'keyword_hit']].copy()
        out['desc'] = df['desc'].fillna('').astype(str) if 'desc' in df.columns else ''
        return out

    @staticmethod
    def normalize_bilateral(df: pd.DataFrame, owner_id: str) -> pd.DataFrame:
        """
        双向流水（第三方支付/银行监控导出）→ 标准格式。
        特征：每行同时含付款账号和收款账号，例如：
          付款支付帐号 | 付款银行卡号 | 收款支付帐号 | 收款银行卡号 | 交易主体的出入账标识 | 交易金额 | ...
        处理策略：
          - 若 owner_id 与付款账号匹配 → is_income=False，counterpart = 收款方
          - 若 owner_id 与收款账号匹配 → is_income=True，counterpart = 付款方
          - 若无法匹配（全局流水，如公安调取的第三方数据）→ 用「出入账标识」列判断，
            否则同时生成两条记录（付款视角 + 收款视角），用于建图
        """
        col_map = {}
        for c in df.columns:
            cl = c.strip()
            # 时间
            if any(k in cl for k in ['交易时间', '时间', '日期', '记账时间']):
                col_map.setdefault(c, 'datetime')
            # 付款方账号（优先支付账号，次选卡号）
            if any(k in cl for k in ['付款支付帐号', '付款支付账号']):
                col_map[c] = 'payer_account'
            elif '付款银行卡号' == cl:
                col_map.setdefault(c, 'payer_account')
            # 付款方名称
            if any(k in cl for k in ['付款方', '付款人', '付款银行卡银行名称']):
                if c not in col_map:
                    col_map.setdefault(c, 'payer_name')
            # 收款方账号
            if any(k in cl for k in ['收款支付帐号', '收款支付账号']):
                col_map[c] = 'payee_account'
            elif '收款银行卡号' == cl:
                col_map.setdefault(c, 'payee_account')
            # 收款方名称/商户
            if any(k in cl for k in ['收款方的商户名称', '收款人', '收款方']):
                if c not in col_map:
                    col_map.setdefault(c, 'payee_name')
            # 金额
            if any(k in cl for k in ['交易金额', '发生额', '金额']):
                col_map.setdefault(c, 'amount')
            # 余额
            if any(k in cl for k in ['交易余额', '余额', '账户余额']):
                col_map.setdefault(c, 'balance')
            # 出入账标识
            if any(k in cl for k in ['出入账标识', '借贷标志', '收支标志', '交易类型']):
                col_map.setdefault(c, 'direction_flag')
            # 备注/描述
            if any(k in cl for k in ['备注', '摘要', '用途', '附言', '说明']):
                col_map.setdefault(c, 'desc')
            # 流水号
            if any(k in cl for k in ['交易流水号', '流水号', '凭证号', '银行外部渠道交易流水号', '参考号']):
                col_map.setdefault(c, 'trade_no')

        df = df.rename(columns=col_map).copy()
        # 去除重复列名（多个源列映射到同一目标名时，保留第一列）
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

        # ── 确保必需列存在 ──────────────────────────────────────────
        for col in ['payer_account', 'payee_account', 'payer_name', 'payee_name',
                    'amount', 'datetime', 'direction_flag', 'desc', 'trade_no']:
            if col not in df.columns:
                df[col] = ''

        df['amount_clean'] = df['amount'].apply(DataLoader._parse_amount)
        df['datetime_parsed'] = pd.to_datetime(df['datetime'].fillna(''), errors='coerce')
        df['remark'] = df['desc'].fillna('').astype(str)
        df['trade_no'] = df['trade_no'].fillna('').astype(str)
        df['keyword_hit'] = df['remark'].apply(DataLoader._keyword_match)

        payer = df['payer_account'].fillna('').astype(str).str.strip()
        payee = df['payee_account'].fillna('').astype(str).str.strip()
        payer_nm = df['payer_name'].fillna('').astype(str).str.strip()
        payee_nm = df['payee_name'].fillna('').astype(str).str.strip()

        # ── 判断方向 ────────────────────────────────────────────────
        # 策略1：用「出入账标识」列
        direction_raw = df['direction_flag'].fillna('').astype(str).str.strip()
        is_income_flag = direction_raw.str.contains('入|收|贷|credit|in', case=False, na=False)

        # 策略2：看 owner_id 在哪一侧
        owner_is_payer = payer.str.contains(re.escape(owner_id), na=False) | \
                         (owner_id in payer.values)
        owner_is_payee = payee.str.contains(re.escape(owner_id), na=False) | \
                         (owner_id in payee.values)

        # 最终 is_income / counterpart_id / counterpart_name_raw
        # 若 owner 能匹配到某侧，优先按实际方向；否则用 direction_flag
        if owner_is_payer.any() or owner_is_payee.any():
            # owner 明确是付款方 → 支出
            cond_out = owner_is_payer & ~owner_is_payee
            # owner 明确是收款方 → 收入
            cond_in  = owner_is_payee & ~owner_is_payer
            # 双侧都匹配（同账号转自己）→ 按 direction_flag 兜底
            cond_both = owner_is_payer & owner_is_payee
            cond_neither = ~owner_is_payer & ~owner_is_payee

            is_income = pd.Series(False, index=df.index)
            is_income[cond_in]      = True
            is_income[cond_both]    = is_income_flag[cond_both]
            is_income[cond_neither] = is_income_flag[cond_neither]

            cp_id = pd.Series('', index=df.index)
            cp_id[cond_out]      = payee[cond_out]
            cp_id[cond_in]       = payer[cond_in]
            cp_id[cond_both]     = payee[cond_both]
            cp_id[cond_neither]  = payee[cond_neither]  # 全局流水默认对方为收款方

            cp_nm = pd.Series('', index=df.index)
            cp_nm[cond_out]      = payee_nm[cond_out]
            cp_nm[cond_in]       = payer_nm[cond_in]
            cp_nm[cond_both]     = payee_nm[cond_both]
            cp_nm[cond_neither]  = payee_nm[cond_neither]
        else:
            # 全局流水，owner 在两侧均无法匹配
            # 用 direction_flag 判断方向，对方 = 另一侧账号
            is_income = is_income_flag
            cp_id = payee.where(~is_income, payer)
            cp_nm = payee_nm.where(~is_income, payer_nm)

        df['is_income'] = is_income
        df['counterpart_id'] = cp_id.fillna('').astype(str)
        df['counterpart_name_raw'] = cp_nm.fillna('').astype(str)
        # counterpart_id 为空时用名称补充
        mask_empty = df['counterpart_id'].str.strip() == ''
        df.loc[mask_empty, 'counterpart_id'] = df.loc[mask_empty, 'counterpart_name_raw']

        df['owner_id'] = owner_id
        df['channel'] = 'bank'

        out = df[['owner_id', 'channel', 'datetime_parsed', 'counterpart_id',
                  'counterpart_name_raw', 'amount_clean', 'is_income',
                  'trade_no', 'remark', 'keyword_hit']].copy()
        out['desc'] = df['desc'].fillna('').astype(str) if 'desc' in df.columns else ''
        return out

    @staticmethod
    def _parse_amount(val) -> float:
        """解析金额字符串"""
        if pd.isna(val):
            return 0.0
        s = str(val).replace(',', '').replace('¥', '').replace('￥', '').strip()
        s = re.sub(r'[^\d.\-]', '', s)
        try:
            return abs(float(s))
        except Exception:
            return 0.0

    @staticmethod
    def _keyword_match(text: str) -> bool:
        text_lower = text.lower()
        return any(kw in text_lower for kw in GAMBLING_KEYWORDS)


# ═══════════════════════════════════════════════════════════
# 模块二：身份对照表
# ═══════════════════════════════════════════════════════════
class IdentityMapper:
    """
    加载身份对照表，统一账号→真实姓名映射。

    支持两种固定表头格式：
      · 微信/支付宝表：账号  姓名  身份证号码  手机号  微信号
      · 银行卡表    ：账号  姓名  身份证号码  所属银行

    每行的「账号」「手机号」「微信号」均会注册为映射键，
    流水中任何一个字段命中都能查到对应姓名和身份信息。
    """

    # ── 固定列名规范（strip后匹配）──────────────────────────────────
    # 账号列（主键，所有表通用）
    _COL_ACCOUNT  = {'账号', '卡号', '银行卡号', '支付账号'}
    # 姓名列
    _COL_NAME     = {'姓名', '真实姓名', '户名'}
    # 身份证列
    _COL_IDCARD   = {'身份证号码', '身份证号', '证件号码', '证件号'}
    # 手机号列（微信/支付宝表专用）
    _COL_PHONE    = {'手机号', '手机', '联系电话', '电话'}
    # 微信号列（微信表专用）
    _COL_WECHAT   = {'微信号', 'wechat', 'wx'}
    # 银行列（银行卡表专用，信息存储但不作为映射键）
    _COL_BANK     = {'所属银行', '开户银行', '银行名称', '银行'}

    def __init__(self, filepath: Optional[str] = None):
        self.mapping         = {}   # account_key → real_name
        self.id_card         = {}   # account_key → id_card_no
        self.phone           = {}   # account_key → phone_no
        self.wechat          = {}   # account_key → wechat_id
        self.bank            = {}   # account_key → bank_name
        self.detail          = {}   # account_key → {name, id_card, phone, wechat, bank, account}
        self.name_to_accounts = {}  # real_name  → set(账号/手机/微信/身份证 等所有键)
        self._loaded         = 0    # 成功注册的条数

        if filepath:
            self.load(filepath)

    # ── 内部工具 ──────────────────────────────────────────────────
    @staticmethod
    def _col(df_cols: list, candidates: set) -> Optional[str]:
        """在 df 的列名中找第一个匹配候选集的列，返回列名或 None"""
        for c in df_cols:
            if c.strip() in candidates:
                return c
        return None

    @staticmethod
    def _normalize_key(key: str) -> str:
        """
        规范化身份对照表中的账号键，使其与流水侧 _normalize_account_id 保持一致：
          · 去除首尾空格和引号
          · 统一小写
          · 去除空格和连字符（银行卡格式：6222-1234 → 62221234）
          · 去除尾部小数点
          · 提取括号前的账号本体（如 6222xxxx(1) → 6222xxxx），括号内数字是时间顺序编号
        """
        k = key.strip().strip('"\'').lower()
        # 提取括号前的账号本体（括号内数字为时间顺序编号，忽略）
        k = re.sub(r'[\\(（）].*$', '', k).strip()
        # 去空格和连字符
        k = re.sub(r'[\s\-]', '', k)
        # 去尾部点号
        k = k.rstrip('.')
        return k

    # 视为空值的占位符（Excel 常见"无效填写"惯例）
    _EMPTY_PLACEHOLDERS = {
        'nan', 'none', 'null', 'na', 'n/a', 'n.a.', 'n/a.',
        '-', '--', '---', '/', '//', '\\', '无', '空', '待填', '待填写',
        '?', '??', '未知', '不详', '暂无', '暂无数据', '',
    }

    @staticmethod
    def _val(row, col: Optional[str]) -> str:
        if col is None:
            return ''
        v = str(row.get(col, '')).strip()
        if v.lower() in IdentityMapper._EMPTY_PLACEHOLDERS:
            return ''
        return v

    def _register(self, key: str, name: str, id_no: str,
                   phone: str, wechat: str, bank: str, account: str,
                   is_primary: bool = True):
        """
        将一个键注册到映射，同时写入 detail 和 name_to_accounts。

        is_primary=True  → 主账号（对照表「账号/卡号」列），参与图节点合并
        is_primary=False → 辅助查询键（手机号/微信号/身份证），仅供 resolve() 查询，
                           不加入 name_to_accounts，不会成为图合并的别名
        """
        # 规范化 key（去序号、去格式差异），与流水侧 _normalize_account_id 保持一致
        key = self._normalize_key(key)
        if not key:
            return
        # 若 name 是占位符（如 '-'、'无'、'未知' 等），视为无名，回退到 key
        clean_name = name if (name and name.lower() not in self._EMPTY_PLACEHOLDERS) else ''
        self.mapping[key] = clean_name or key
        if id_no:
            self.id_card[key] = id_no
        if phone:
            self.phone[key]   = phone
        if wechat:
            self.wechat[key]  = wechat
        if bank:
            self.bank[key]    = bank
        self.detail[key] = {
            'name':    name,
            'id_card': id_no,
            'phone':   phone,
            'wechat':  wechat,
            'bank':    bank,
            'account': account,
        }
        # 只有主账号才建立反向索引（参与图节点合并）
        # 辅助键（手机/微信/身份证）只用于查询，不作为图中的别名节点
        if clean_name and is_primary:
            self.name_to_accounts.setdefault(clean_name, set()).add(key)

    # ── 主加载函数 ────────────────────────────────────────────────
    def load(self, filepath: str):
        """
        解析身份对照表文件（支持 xlsx / xls / csv）。

        xlsx/xls 文件会**遍历所有 sheet**，每张子表独立解析后合并，
        以兼容「微信/支付宝」与「银行卡」分开存放在同一 Excel 的场景。
        """
        p = Path(filepath)
        suffix = p.suffix.lower()

        if suffix in ('.xlsx', '.xls'):
            try:
                xl = pd.ExcelFile(filepath)
                sheet_names = xl.sheet_names
            except Exception:
                sheet_names = [0]   # 兜底：只读第一张

            for sname in sheet_names:
                try:
                    df = pd.read_excel(filepath, sheet_name=sname, dtype=str)
                    # 防御：列名可能是 RangeIndex（空 sheet 时），先转 str 再 strip
                    df.columns = df.columns.astype(str).str.strip()
                    # 若列数太少或行数为0，跳过（可能是空 sheet 或说明页）
                    if len(df.columns) < 2 or len(df) == 0:
                        continue
                    print(f"  [--] 解析身份对照表 sheet: {sname}（{len(df)} 行）")
                    self._parse_sheet(df)
                except Exception as e:
                    print(f"  ⚠️  读取 sheet '{sname}' 失败: {e}")
        else:
            # CSV：仍用原有逻辑，只有单一 sheet
            df = DataLoader.auto_read(filepath)
            df.columns = df.columns.str.strip()
            self._parse_sheet(df)

    def _parse_sheet(self, df: 'pd.DataFrame'):
        """解析单张身份对照表 DataFrame，将账号/姓名注册到映射中。"""
        # ── 防御：空 sheet 或列名不是字符串（Sheet3 全空时 columns 可能是 RangeIndex）
        if df is None or len(df.columns) == 0:
            return
        try:
            df = df.copy()
            df.columns = df.columns.astype(str).str.strip()
        except Exception:
            return
        # 若所有列名都是纯数字（RangeIndex 转 str 后如 '0','1','2'），视为空 sheet 跳过
        non_trivial = [c for c in df.columns if c.strip() and not re.match(r'^\d+$', c.strip())]
        if len(non_trivial) == 0:
            return
        cols = list(df.columns)

        col_account = self._col(cols, self._COL_ACCOUNT)
        col_name    = self._col(cols, self._COL_NAME)
        col_idcard  = self._col(cols, self._COL_IDCARD)
        col_phone   = self._col(cols, self._COL_PHONE)
        col_wechat  = self._col(cols, self._COL_WECHAT)
        col_bank    = self._col(cols, self._COL_BANK)

        # 识别成功判断：至少有账号列或姓名列
        structured = (col_account is not None) or (col_name is not None)

        if structured:
            # ── 结构化解析（按列名）────────────────────────────────
            for _, row in df.iterrows():
                account = self._val(row, col_account)
                name    = self._val(row, col_name)
                id_no   = self._val(row, col_idcard)
                phone   = self._val(row, col_phone)
                wechat  = self._val(row, col_wechat)
                bank    = self._val(row, col_bank)

                if not name and not account:
                    continue   # 空行跳过

                # 主账号 → 姓名（参与图节点合并）
                if account:
                    self._register(account, name, id_no, phone, wechat, bank, account,
                                   is_primary=True)
                # 手机号/微信号/身份证：仅作辅助查询键，不参与图节点合并
                if phone and phone != account:
                    self._register(phone, name, id_no, phone, wechat, bank, account,
                                   is_primary=False)
                # 微信号也作为辅助键
                if wechat and wechat != account and wechat != phone:
                    self._register(wechat, name, id_no, phone, wechat, bank, account,
                                   is_primary=False)
                # 身份证也作为辅助键
                if id_no:
                    self._register(id_no, name, id_no, phone, wechat, bank, account,
                                   is_primary=False)
                # 姓名本身也映射（直接用姓名出现时不丢失）——占位符不映射
                real_name = name if (name and name.lower() not in self._EMPTY_PLACEHOLDERS) else ''
                if real_name:
                    self.mapping[real_name] = real_name
                    if real_name not in self.detail:
                        self.detail[real_name] = self.detail.get(account or phone or wechat or id_no, {})

                self._loaded += 1

        else:
            # ── 兜底：旧版自动推断逻辑（列名不规范时使用）──────────
            print("  ⚠️  身份对照表列名不符合规范，降级为自动推断模式")
            for _, row in df.iterrows():
                vals = row.tolist()
                name, id_no, phone_no = '', '', ''
                for v in vals:
                    v = str(v).strip()
                    if re.match(r'^\d{17}[\dXx]$', v):
                        id_no = v
                    elif re.match(r'^1[3-9]\d{9}$', v):
                        phone_no = v
                    elif len(v) >= 2 and not re.match(r'^\d+$', v) and v not in ('nan', ''):
                        if not name:
                            name = v
                key_candidates = [str(v).strip() for v in vals
                                  if str(v).strip() not in ('nan', '', name)]
                for key in key_candidates:
                    if key:
                        self._register(key, name, id_no, phone_no, '', '', key)
                if name:
                    self.mapping[name] = name
                self._loaded += 1

    # ── 查询接口 ──────────────────────────────────────────────────
    def resolve(self, account: str) -> str:
        """查找真实姓名，找不到返回原值；若 mapping 里存的是占位符，也回退到原值"""
        orig = str(account).strip()   # 保留原始值用于回退显示
        
        # 1. 直接精确匹配
        if orig in self.mapping:
            mapped = self.mapping[orig]
        else:
            # 2. 规范化后匹配
            key = self._normalize_key(orig)
            if key in self.mapping:
                mapped = self.mapping[key]
            else:
                # 3. 处理银行卡号匹配
                mapped = None
                if orig.isdigit():
                    for mapped_key, mapped_name in self.mapping.items():
                        if mapped_key.isdigit():
                            if mapped_key == orig:
                                mapped = mapped_name
                                break
                            elif mapped_key.endswith(orig) and len(mapped_key) > len(orig):
                                mapped = mapped_name
                                break
                            elif orig.endswith(mapped_key) and len(orig) > len(mapped_key):
                                mapped = mapped_name
                                break
                # 4. 匹配失败，回退到原始值
                if mapped is None:
                    mapped = orig
        
        # 如果 mapping 里的值是占位符（如 '-'），回退到原始账号本身
        if mapped.lower() in self._EMPTY_PLACEHOLDERS:
            return orig
        return mapped



    def get_canonical_id(self, account: str) -> str:
        """
        返回该账号所属真实人物的「规范ID」。
        规范ID = 真实姓名（若能识别），否则返回原账号。
        用于图合并：同一人的多个账号都映射到同一规范ID。
        """
        orig = str(account).strip()
        
        # 1. 直接精确匹配
        if orig in self.mapping:
            return self.mapping[orig]
        
        # 2. 规范化后匹配
        key = self._normalize_key(orig)
        if key in self.mapping:
            return self.mapping[key]
        
        # 3. 处理银行卡号匹配
        # 检查是否是数字账号
        if orig.isdigit():
            # 尝试匹配完整银行卡号
            for mapped_key, mapped_name in self.mapping.items():
                # 只对数字账号进行匹配（银行卡号）
                if mapped_key.isdigit():
                    # 情况1：当前账号是完整银行卡号，映射中有完整或部分匹配
                    if mapped_key == orig:
                        return mapped_name
                    # 情况2：当前账号是银行卡号的后几位，映射中有完整银行卡号
                    elif mapped_key.endswith(orig) and len(mapped_key) > len(orig):
                        return mapped_name
                    # 情况3：当前账号是完整银行卡号，映射中有后几位匹配
                    elif orig.endswith(mapped_key) and len(orig) > len(mapped_key):
                        return mapped_name
        
        # 4. 匹配失败，返回原账号
        return orig



    def get_all_accounts(self, name: str) -> set:
        """返回某真实姓名名下的所有账号键集合（用于批量合并）"""
        return self.name_to_accounts.get(str(name).strip(), set())

    def build_merge_map(self) -> dict:
        """
        构建「账号 → 规范ID」全局映射表。
        规范ID = 真实姓名（只有在身份对照表中出现的账号才有合并）。
        未知账号不在映射里（保持原样）。
        
        修复：为每个姓名添加自映射（name → name），确保姓名节点也能被正确识别为规范ID。
        """
        merge_map = {}
        for name, keys in self.name_to_accounts.items():
            # 姓名自映射（name → name），确保姓名节点被识别为规范ID
            merge_map[name] = name
            # 所有别名账号映射到姓名
            for k in keys:
                merge_map[k] = name
        return merge_map

    def get_detail(self, account: str) -> dict:
        """返回该账号的完整身份信息字典（name/id_card/phone/wechat/bank/account）"""
        return self.detail.get(str(account).strip(), {})

    def get_id_card(self, account: str) -> str:
        return self.id_card.get(str(account).strip(), '')

    def get_phone(self, account: str) -> str:
        return self.phone.get(str(account).strip(), '')

    def get_wechat(self, account: str) -> str:
        return self.wechat.get(str(account).strip(), '')

    def get_bank(self, account: str) -> str:
        return self.bank.get(str(account).strip(), '')

    # ── 自定义标签管理 ──────────────────────────────────────────────
    # custom_tags: account_key → set of tag strings
    # 调用者可通过 add_tag() / remove_tag() / get_tags() 维护
    def add_tag(self, account: str, tag: str):
        """为某账号/姓名添加自定义标签（如 '重点追查'、'已抓获' 等）"""
        if not hasattr(self, 'custom_tags'):
            self.custom_tags: dict = {}
        key = self._normalize_key(account) or account.strip()
        if not key or not tag.strip():
            return
        self.custom_tags.setdefault(key, set()).add(tag.strip())
        # 如果传入的是姓名，同时对该姓名下所有账号打标
        for acc in self.get_all_accounts(account):
            self.custom_tags.setdefault(acc, set()).add(tag.strip())

    def remove_tag(self, account: str, tag: str):
        """移除某账号/姓名的自定义标签"""
        if not hasattr(self, 'custom_tags'):
            return
        key = self._normalize_key(account) or account.strip()
        if key in self.custom_tags:
            self.custom_tags[key].discard(tag.strip())

    def get_tags(self, account: str) -> list:
        """获取某账号/节点的所有自定义标签列表"""
        if not hasattr(self, 'custom_tags'):
            return []
        key = self._normalize_key(account) or account.strip()
        tags = set(self.custom_tags.get(key, set()))
        # 也查姓名维度
        name = self.resolve(account)
        if name != account:
            name_key = self._normalize_key(name) or name.strip()
            tags |= self.custom_tags.get(name_key, set())
        return sorted(tags)

    def get_all_tagged(self) -> dict:
        """返回所有已打标签的账号字典 {account_key: [tag, ...]}"""
        if not hasattr(self, 'custom_tags'):
            return {}
        return {k: sorted(v) for k, v in self.custom_tags.items() if v}

    def batch_add_tags(self, tag_dict: dict):
        """
        批量添加标签。
        tag_dict: {账号或姓名: '标签1,标签2,...'} 或 {账号: ['标签1', '标签2']}
        """
        for account, tags in tag_dict.items():
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]
            for t in tags:
                self.add_tag(account, t)


# ─────────────────────────────────────────────────────────
# 公共工具函数
# ─────────────────────────────────────────────────────────
def _smart_label(name: str, node: str, max_len: int = 8) -> str:
    """
    图节点的智能显示标签：
      · 有真实姓名（短于 max_len）→ 直接显示真名
      · 名称太长（通常是原始账号）→ 显示「前6位…后4位」保留头尾，有辨识度
    """
    if len(name) <= max_len:
        return name
    return name[:6] + '…' + name[-4:]


# ═══════════════════════════════════════════════════════════
# 模块三：核心分析引擎
# ═══════════════════════════════════════════════════════════
class GamblingAnalyzer:
    """主分析引擎"""

    def __init__(self, transactions: pd.DataFrame, identity: IdentityMapper):
        self.tx = transactions.copy()
        self.identity = identity
        self.nodes = {}   # node_id → {name, level, metrics}
        self.edges = []   # (from, to, amount, count)
        self.G = nx.DiGraph()

    def run(self):
        print("\n[1/5] 数据清洗与标准化...")
        self._clean()
        print("[2/5] 构建资金流向图...")
        self._build_graph()
        print("[2.5/5] 合并同一人的多账号节点...")
        merged_count = self._merge_person_nodes()
        if merged_count:
            print(f"     已合并 {merged_count} 组同一人的多账号节点")
        print("[2.7/5] 检测账号多人使用情况...")
        shared_accounts = self._detect_account_sharing()
        if shared_accounts:
            print(f"     检测到 {len(shared_accounts)} 个账号可能被多人使用")
        print("[2.8/5] 识别做账人员...")
        accountants = self._detect_accountants()
        if accountants:
            print(f"     检测到 {len(accountants)} 个可能的做账人员账号")
        print("[2.9/5] 检测信用额度结算周期...")
        credit_settlement = self._detect_credit_settlement()
        if credit_settlement:
            print(f"     检测到 {len(credit_settlement)} 个信用额度结算账号")
        print("[2.95/5] 检测信用额度分配模式...")
        credit_allocation = self._detect_credit_allocation()
        if credit_allocation:
            print(f"     检测到 {len(credit_allocation)} 个信用额度分配账号")
        print("[2.97/5] 检测大额一次性结算交易...")
        large_settlement = self._detect_large_settlement()
        if large_settlement:
            print(f"     检测到 {len(large_settlement)} 个大额结算账号")
        print("[2.99/5] 分析时间模式，识别每周信用额度注入...")
        time_patterns = self._detect_time_patterns()
        if time_patterns:
            print(f"     检测到 {len(time_patterns)} 个每周信用额度注入账号")
        print("[2.995/5] 检测单向资金流动账号...")
        one_way_accounts = self._detect_one_way_accounts()
        if one_way_accounts:
            only_inflow = [acc for acc in one_way_accounts if acc['type'] == 'only_inflow']
            only_outflow = [acc for acc in one_way_accounts if acc['type'] == 'only_outflow']
            print(f"     检测到 {len(only_inflow)} 个仅流入账号，{len(only_outflow)} 个仅流出账号")
        print("[3/5] 识别团伙层级（代理/赌客/外围）...")
        self._classify_levels()
        # 层级分布统计
        from collections import Counter
        lc = Counter(v['level'] for v in self.nodes.values())
        level_name = {
            LEVEL_AGENT1: '一级代理',
            LEVEL_AGENT2: '二级代理',
            LEVEL_AGENT3: '三级代理',
            LEVEL_GAMBLER: '赌客',
            LEVEL_UNKNOWN: '待定外围',
        }
        dist = '  '.join(f"{level_name[k]}:{lc.get(k,0)}"
                         for k in [LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3,
                                   LEVEL_GAMBLER, LEVEL_UNKNOWN] if lc.get(k, 0) > 0)
        print(f"     层级分布: {dist}  (共 {len(self.nodes)} 个节点)")
        print("[3.5/5] 识别洗钱嫌疑账号...")
        self._detect_money_laundering()
        print("[4/5] 计算风险评分...")
        self._score_suspects()
        print("[5/5] 完成分析 ✅")

    @staticmethod
    def _normalize_account_id(s: pd.Series) -> pd.Series:
        """
        规范化账号/节点 ID，消除因格式差异导致同一账户被识别为多个节点：
          · 去除首尾空格和引号
          · 统一转为小写（微信 openid / 邮箱等）
          · 去除账号中的空格、连字符（银行卡格式差异：6222-1234-5678 vs 6222123456789）
          · 去除尾部小数点（粘贴时常见）
          · 提取括号前的账号本体（如 6222xxxx(1) → 6222xxxx），括号内数字为时间顺序编号
        """
        return (
            s.fillna('')
             .astype(str)
             .str.strip()
             .str.strip('"\'')
             .str.lower()
             .str.replace(r'[\s\-]', '', regex=True)   # 去空格和连字符
             .str.replace(r'[\\(（）].*$', '', regex=True)  # 提取括号前的账号本体
             .str.rstrip('.')                           # 去尾部点号
        )

    def _clean(self):
        # ── 先去除重复列名（多文件合并后可能出现） ──────────────────────
        self.tx = self.tx.loc[:, ~self.tx.columns.duplicated()]

        self.tx = self.tx.dropna(subset=['amount_clean'])
        self.tx = self.tx[self.tx['amount_clean'] > 0].copy()

        # 确保关键列为一维 Series（防止 apply 收到 DataFrame）
        def _to_str_series(col: str) -> None:
            s = self.tx[col]
            # 如果意外是 DataFrame（重复列），取第一列
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            self.tx[col] = s.fillna('').astype(str)

        _to_str_series('counterpart_id')
        _to_str_series('counterpart_name_raw')
        _to_str_series('owner_id')

        # ── 账号规范化：消除格式差异导致的重复节点 ───────────────────────
        self.tx['owner_id']        = self._normalize_account_id(self.tx['owner_id'])
        self.tx['counterpart_id']  = self._normalize_account_id(self.tx['counterpart_id'])
        # counterpart_name_raw 只做 strip，不做 lower（中文姓名保持原样）
        self.tx['counterpart_name_raw'] = (
            self.tx['counterpart_name_raw'].str.strip().str.strip('"\'')
        )

        # 统一对方标识：优先用 counterpart_id，次选 counterpart_name_raw
        cid = self.tx['counterpart_id'].str.strip()
        cname = self.tx['counterpart_name_raw'].str.strip()
        self.tx['cp_key'] = cid.where(cid != '', cname)

        # ── 规范化 cp_key（去后缀、去格式差异，与对照表侧 _normalize_key 保持一致）───
        self.tx['cp_key'] = self._normalize_account_id(self.tx['cp_key'])

        # ── 处理「账单自报姓名」节点 ──────────────────────────────────────
        # 当 counterpart_id 为空、cp_key 来自账单里的 counterpart_name_raw 时，
        # 检查该姓名是否已在身份对照表中登记。
        # 若未登记：加上「[待核实]」前缀，使其在图和报告中可区分于已认证节点。
        # 若已登记（resolve 返回了不同的值，或姓名本身就在 mapping 里）：保持原值。
        def _mark_unverified_name(row) -> str:
            cid_val   = str(row.get('counterpart_id', '') or '').strip()
            cname_val = str(row.get('counterpart_name_raw', '') or '').strip()
            cp        = str(row.get('cp_key', '') or '').strip()
            # 只处理「来自 name_raw」的情况（counterpart_id 为空）
            if cid_val or not cname_val or not cp:
                return cp
            # 若该姓名已在身份对照表中 → 不加标记
            resolved = self.identity.resolve(cp)
            if resolved != cp:
                # resolve 返回了不同值（如真实姓名映射到了账号）：已认证
                return cp
            if cp in self.identity.mapping or cp in self.identity.detail:
                return cp
            # 该名字未登记：标记为待核实
            return f'[待核实]{cp}'

        # 只在有 counterpart_name_raw 列时执行（防御性检查）
        if 'counterpart_name_raw' in self.tx.columns:
            self.tx['cp_key'] = self.tx.apply(_mark_unverified_name, axis=1)

        # 注意：所有账号都保留原样，不进行过滤
        # 特殊账号（如微信支付商户号）会通过 identity.resolve 方法在身份对照表中查找对应关系


        self.tx['cp_name'] = self.tx['cp_key'].apply(self.identity.resolve)
        self.tx['owner_name'] = self.tx['owner_id'].apply(self.identity.resolve)

        # ── 标记平台内转账（零钱通/余额宝/零钱提现等，不代表真实资金流动）─────
        # 这类交易的对方账号或备注含有特定标志，属于账户内部划转，不应计入净流量
        _internal_cp_patterns = [
            '零钱通', '零钱', '余额宝', '花呗', '借呗', '微粒贷',
            '理财通', '微信理财', '零钱转出', '零钱存入',
            '收益发放', '自动转入', '自动转出', '定期转入', '定期转出',
        ]
        _internal_remark_patterns = [
            '零钱通', '余额宝转出', '余额宝转入', '自动还款',
            '零钱提现', '零钱充值', '提现到银行卡',
        ]

        def _is_internal_transfer(row) -> bool:
            cp = str(row.get('cp_key', '') or '').strip()
            remark = str(row.get('remark', '') or '') + str(row.get('desc', '') or '')
            for pat in _internal_cp_patterns:
                if pat in cp:
                    return True
            for pat in _internal_remark_patterns:
                if pat in remark:
                    return True
            return False

        self.tx['is_internal_transfer'] = self.tx.apply(_is_internal_transfer, axis=1)

        # ── 标记自转账（同一人名下不同账号之间互转）────────────────────────
        # 依赖身份对照表：若 owner 和 counterpart 解析后是同一个真实姓名，视为自转账
        def _is_self_transfer(row) -> bool:
            owner_name  = str(row.get('owner_name', '') or '').strip()
            cp_name     = str(row.get('cp_name', '') or '').strip()
            # 若两侧真实姓名相同（且非空/待核查），判定为自转账
            if owner_name and cp_name and owner_name == cp_name and owner_name != '待核查':
                return True
            # 若 owner_id == cp_key（账号完全相同），也是自转账
            if str(row.get('owner_id', '')).strip() == str(row.get('cp_key', '')).strip():
                return True
            return False

        self.tx['is_self_transfer'] = self.tx.apply(_is_self_transfer, axis=1)

        # ── 清洗后诊断 ─────────────────────────────────────────────────
        total = len(self.tx)
        cp_filled = (self.tx['cp_key'].str.strip() != '').sum()
        unique_owners = self.tx['owner_id'].nunique()
        unique_cps    = self.tx['cp_key'].nunique()
        n_internal = self.tx['is_internal_transfer'].sum()
        n_self     = self.tx['is_self_transfer'].sum()
        print(f"\n  📊 数据清洗完成: {total} 条记录")
        print(f"  📊 账号规范化后：主账户 {unique_owners} 个，对方账户 {unique_cps} 个")
        print(f"  📊 对方标识(cp_key)有值: {cp_filled} 条 ({cp_filled/max(total,1):.1%})")
        print(f"  📊 平台内转账（零钱通/余额宝等）: {n_internal} 条（将排除出资金图）")
        print(f"  📊 自转账（同人不同账号）: {n_self} 条（将排除出资金图）")
        if cp_filled == 0:
            # 帮助用户快速定位原始列名
            print("  ⚠️  对方标识全为空！请检查账单列名是否被正确识别。")
            print(f"  [--] counterpart_id 样本: {self.tx['counterpart_id'].head(3).tolist()}")
            print(f"  [--] counterpart_name_raw 样本: {self.tx['counterpart_name_raw'].head(3).tolist()}")
            print("  [TIP] 建议：检查原始账单表头，确认包含对方账号/对方名称等关键列")
        else:
            print(f"  📊 cp_key 样本: {self.tx['cp_key'].dropna().head(3).tolist()}")

    def _build_graph(self):
        """从流水记录构建有向资金图（含去重逻辑，防止资金重复计算）

        去重策略（按优先级依次执行）：
          ① 排除平台内转账（零钱通提现/余额宝赎回等），这类不代表真实资金流动
          ② 排除自转账（同一人名下不同账号互转，_merge_person_nodes 已处理自环，
             但此处提前剔除可避免节点流量虚增）
          ③ 全局交易去重：同一笔交易可能在多个账单文件中重复出现
             （如微信提现同时出现在微信账单和银行账单中），基于时间+金额+双方账号去重
          ④ 双向流水去重：A→B 和 B→A 在同一时间窗口（±5分钟）内金额相近时，
             视为同一笔转账被双方账单同时记录，只保留一侧记录
        """
        # ── ① 排除平台内转账和自转账 ──────────────────────────────────
        tx_valid = self.tx.copy()
        if 'is_internal_transfer' in tx_valid.columns:
            n_internal = tx_valid['is_internal_transfer'].sum()
            tx_valid = tx_valid[~tx_valid['is_internal_transfer']]
        else:
            n_internal = 0
        if 'is_self_transfer' in tx_valid.columns:
            n_self = tx_valid['is_self_transfer'].sum()
            tx_valid = tx_valid[~tx_valid['is_self_transfer']]
        else:
            n_self = 0

        # 收款：资金从 counterpart → owner
        income = tx_valid[tx_valid['is_income'] == True].copy()
        # 付款：资金从 owner → counterpart
        outcome = tx_valid[tx_valid['is_income'] == False].copy()

        all_flows = pd.concat([
            income.assign(src=income['cp_key'], dst=income['owner_id']),
            outcome.assign(src=outcome['owner_id'], dst=outcome['cp_key'])
        ])
        all_flows = all_flows[all_flows['src'] != all_flows['dst']]
        all_flows = all_flows[all_flows['src'].str.strip() != '']
        all_flows = all_flows[all_flows['dst'].str.strip() != '']
        # 过滤掉账号本身就是占位符的行（如 '-'、'无'、'/'）
        _ph = IdentityMapper._EMPTY_PLACEHOLDERS
        all_flows = all_flows[~all_flows['src'].str.strip().str.lower().isin(_ph)]
        all_flows = all_flows[~all_flows['dst'].str.strip().str.lower().isin(_ph)]

        n_before_global_dedup = len(all_flows)

        # ── ② 全局交易去重（处理同一笔交易在多个文件中重复）──────────────────
        # 场景：微信提现到银行卡，会同时出现在微信账单和银行账单中
        # 去重键：有向边(src,dst) + 金额（精确到元）+ 时间（1分钟桶）
        if 'datetime_parsed' in all_flows.columns:
            all_flows = all_flows.copy()

            def _time_bucket_minute(dt):
                if pd.isna(dt):
                    return -1
                try:
                    return int(dt.timestamp() // 60)
                except Exception:
                    return -1

            all_flows['_time_bucket_min'] = all_flows['datetime_parsed'].apply(_time_bucket_minute)
            # 金额保留到元（取整）作为去重键的一部分，允许1元误差
            all_flows['_amt_key_round'] = all_flows['amount_clean'].round(0).astype(int)
            # 构建全局去重键：源→目标 + 金额 + 时间（1分钟粒度）
            all_flows['_global_dedup_key'] = list(zip(
                all_flows['src'],
                all_flows['dst'],
                all_flows['_amt_key_round'],
                all_flows['_time_bucket_min']
            ))

            # 标记重复记录（保留第一条，优先保留有交易单号的）
            # 有 trade_no 的记录通常更完整，优先保留
            all_flows['_has_trade_no'] = all_flows['trade_no'].fillna('').str.strip() != ''
            all_flows = all_flows.sort_values(
                ['_global_dedup_key', '_has_trade_no'],
                ascending=[True, False]
            ).drop_duplicates(subset=['_global_dedup_key'], keep='first')

            n_global_deduped = n_before_global_dedup - len(all_flows)

            # 清理全局去重辅助列
            all_flows = all_flows.drop(
                columns=['_time_bucket_min', '_amt_key_round', '_global_dedup_key', '_has_trade_no'],
                errors='ignore'
            )
        else:
            n_global_deduped = 0

        n_before_bilateral_dedup = len(all_flows)

        # ── ③ 双向流水去重（防止 A→B 和 B→A 双侧账单重复计算）──────────────
        # 去重键：无向边对(min,max) + 金额（精确到分）+ 时间（5分钟桶）
        # 重要：只有在同一去重键组内，同时存在 "A→B" 和 "B→A" 两种方向时，
        #        才认定为双侧账单重复，只保留一条（支出方优先）。
        # 如果组内所有记录方向相同（例如多人同时向同一账号转相同金额），则全部保留。
        if 'datetime_parsed' in all_flows.columns:
            all_flows = all_flows.copy()
            # 构建去重键
            all_flows['_edge_pair'] = all_flows.apply(
                lambda r: tuple(sorted([str(r['src']), str(r['dst'])])), axis=1
            )
            # 时间桶：5分钟粒度（整除300秒）
            def _time_bucket(dt):
                if pd.isna(dt):
                    return -1
                try:
                    return int(dt.timestamp() // 300)
                except Exception:
                    return -1

            all_flows['_time_bucket'] = all_flows['datetime_parsed'].apply(_time_bucket)
            # 金额保留到分（×100取整）作为去重键的一部分
            all_flows['_amt_key'] = (all_flows['amount_clean'] * 100).round(0).astype(int)
            all_flows['_dedup_key'] = list(zip(
                all_flows['_edge_pair'],
                all_flows['_amt_key'],
                all_flows['_time_bucket']
            ))
            # 记录每条记录的有向边方向（src<dst 为正向，src>dst 为反向）
            all_flows['_edge_dir'] = all_flows.apply(
                lambda r: 'fwd' if str(r['src']) <= str(r['dst']) else 'rev', axis=1
            )

            # 找出真正的双向重复组：组内同时存在正向和反向记录
            grp_dirs = all_flows.groupby('_dedup_key')['_edge_dir'].nunique()
            bilateral_keys = set(grp_dirs[grp_dirs > 1].index)

            if bilateral_keys:
                # 对双向重复组：按 is_income 升序排（支出方优先），只保留第一条
                mask_bilateral = all_flows['_dedup_key'].isin(bilateral_keys)
                df_bilateral = all_flows[mask_bilateral].sort_values(
                    ['_dedup_key', 'is_income'],
                    key=lambda col: col if col.name != 'is_income' else col.astype(int)
                ).drop_duplicates(subset=['_dedup_key'], keep='first')
                df_unilateral = all_flows[~mask_bilateral]
                all_flows_deduped = pd.concat([df_unilateral, df_bilateral], ignore_index=True)
            else:
                all_flows_deduped = all_flows

            n_bilateral_deduped = n_before_bilateral_dedup - len(all_flows_deduped)
            # 清理辅助列
            all_flows = all_flows_deduped.drop(
                columns=['_edge_pair', '_time_bucket', '_amt_key', '_dedup_key', '_edge_dir'],
                errors='ignore'
            )
        else:
            n_bilateral_deduped = 0

        n_deduped = n_global_deduped + n_bilateral_deduped

        print(f"  📊 去重统计: 排除平台内转账 {n_internal} 条，自转账 {n_self} 条，"
              f"双向流水重复 {n_deduped} 条")
        print(f"  📊 进入资金图的有效记录: {len(all_flows)} 条")

        # ── 汇总边权重 ────────────────────────────────────────────────
        edge_agg = all_flows.groupby(['src', 'dst']).agg(
            total_amount=('amount_clean', 'sum'),
            count=('amount_clean', 'count')
        ).reset_index()

        for _, row in edge_agg.iterrows():
            self.G.add_edge(row['src'], row['dst'],
                            weight=row['total_amount'],
                            count=row['count'])

        # 为每个节点计算统计量
        for node in self.G.nodes():
            in_flow = sum(d['weight'] for _, _, d in self.G.in_edges(node, data=True))
            out_flow = sum(d['weight'] for _, _, d in self.G.out_edges(node, data=True))
            in_count = int(self.G.in_degree(node))   # 确保是 int，防止存入 InDegreeView
            out_count = int(self.G.out_degree(node))  # 确保是 int，防止存入 OutDegreeView
            
            # 检查账号是否在身份对照表中登记
            node_str = str(node).strip()
            resolved_name = self.identity.resolve(node_str)
            is_registered = False
            
            # 检查是否在映射中
            key = self.identity._normalize_key(node_str)
            if key in self.identity.mapping:
                is_registered = True
            elif node_str in self.identity.mapping:
                is_registered = True
            
            # 检查是否是姓名自映射
            if node_str in self.identity.name_to_accounts:
                is_registered = True
            
            self.nodes[node] = {
                'name': resolved_name,
                'in_flow': in_flow,
                'out_flow': out_flow,
                'net_flow': in_flow - out_flow,
                'in_degree': in_count,
                'out_degree': out_count,
                'total_flow': in_flow + out_flow,
                # 净额：去除双向抵消后的单向资金量，更真实反映资金影响力
                # 对于 A→B→A 的循环，净额会接近 0，不会虚增层级
                'net_absolute': abs(in_flow - out_flow),
                'level': 0,
                'risk_score': 0,
                'is_registered': is_registered,
                'original_account': node_str,
            }

    def _detect_one_way_accounts(self):
        """
        检测只有单向资金流动的账号
        
        检测逻辑：
          1. 识别只有流入没有流出的账号
          2. 识别只有流出没有流入的账号
          3. 分析这些账号在赌博网络中的可能角色
        """
        one_way_accounts = []
        for node, info in self.nodes.items():
            if info['in_flow'] > 0 and info['out_flow'] == 0:
                one_way_accounts.append({
                    'account': node,
                    'name': info['name'],
                    'in_flow': info['in_flow'],
                    'type': 'only_inflow'
                })
            elif info['out_flow'] > 0 and info['in_flow'] == 0:
                one_way_accounts.append({
                    'account': node,
                    'name': info['name'],
                    'out_flow': info['out_flow'],
                    'type': 'only_outflow'
                })
        return one_way_accounts

    def _detect_account_sharing(self):
        """
        检测账号多人使用的情况（一个账号被多个真实人物使用）

        检测逻辑：
          1. 分析流水记录中的备注、描述等字段
          2. 检查同一账号是否与多个不同的真实姓名相关联
          3. 识别异常的交易模式（如频繁的资金进出、不同时间段的交易习惯）
        """
        if not hasattr(self, '_tx_original'):
            self._tx_original = self.tx.copy()

        tx = self._tx_original.copy()
        
        # 确保必要列存在
        for col in ['remark', 'desc', 'counterpart_name_raw']:
            if col not in tx.columns:
                tx[col] = ''
            else:
                tx[col] = tx[col].fillna('').astype(str)

        # 按账号分组
        account_owners = {}
        for _, row in tx.iterrows():
            account = str(row.get('owner_id', '') or '').strip()
            if not account:
                continue
            
            # 从备注和描述中提取可能的用户信息
            context = (str(row.get('remark', '') or '') + ' ' + 
                      str(row.get('desc', '') or '') + ' ' +
                      str(row.get('counterpart_name_raw', '') or '')).lower()
            
            # 提取可能的姓名信息
            # 这里可以根据实际情况添加更复杂的姓名提取逻辑
            potential_names = []
            
            # 简单的姓名模式匹配（示例）
            import re
            # 匹配中文姓名（2-4个汉字）
            chinese_names = re.findall(r'[\u4e00-\u9fa5]{2,4}', context)
            potential_names.extend(chinese_names)
            
            if potential_names:
                if account not in account_owners:
                    account_owners[account] = set()
                account_owners[account].update(potential_names)

        # 识别多人使用的账号
        shared_accounts = {}
        for account, names in account_owners.items():
            if len(names) > 1:
                shared_accounts[account] = list(names)

        # 记录到节点信息中
        for account, names in shared_accounts.items():
            if account in self.nodes:
                self.nodes[account]['is_shared_account'] = True
                self.nodes[account]['potential_users'] = names
                print(f"  ⚠️ 检测到账号多人使用: {account} → 可能用户: {', '.join(names)}")

        return shared_accounts

    def _detect_accountants(self):
        """
        识别专门"做账"的人员（代理之间负责抽成分发的账号）

        识别特征：
          1. 资金分发模式：向多个不同账号进行相似金额的转账
          2. 交易频率：定期进行资金分发（如每周、每月固定时间）
          3. 备注特征：备注中包含"抽成"、"分红"、"佣金"等关键词
          4. 层级关系：与多个代理账号有资金往来
        """
        if not hasattr(self, '_tx_original'):
            self._tx_original = self.tx.copy()

        tx = self._tx_original.copy()
        
        # 确保必要列存在
        for col in ['remark', 'desc']:
            if col not in tx.columns:
                tx[col] = ''
            else:
                tx[col] = tx[col].fillna('').astype(str)

        # 筛选支出交易
        outgoing_tx = tx[tx['is_income'] == False].copy()
        
        # 按账号分组
        account_payments = {}
        for _, row in outgoing_tx.iterrows():
            account = str(row.get('owner_id', '') or '').strip()
            if not account:
                continue
            
            cp = str(row.get('cp_key', '') or '').strip()
            amt = float(row.get('amount_clean', 0) or 0)
            time = row.get('datetime_parsed')
            remark = (str(row.get('remark', '') or '') + ' ' + 
                     str(row.get('desc', '') or '')).lower()
            
            if account not in account_payments:
                account_payments[account] = []
            account_payments[account].append({
                'cp': cp,
                'amount': amt,
                'time': time,
                'remark': remark
            })

        # 识别做账人员
        accountants = {}
        for account, payments in account_payments.items():
            # 至少向5个不同账号转账
            unique_recipients = len(set(p['cp'] for p in payments))
            if unique_recipients < 5:
                continue
            
            # 检查是否有做账相关关键词
            has_accountant_keywords = any(
                kw in p['remark'] for p in payments for kw in 
                ['抽成', '分红', '佣金', '返利', '分成', '工资', '奖金']
            )
            
            # 检查转账金额是否相似（可能是固定比例的抽成）
            amounts = [p['amount'] for p in payments if p['amount'] > 0]
            if len(amounts) > 5:
                # 计算金额的标准差
                import numpy as np
                mean_amt = np.mean(amounts)
                std_amt = np.std(amounts)
                # 标准差与均值的比例小于0.3，说明金额较为相似
                if std_amt / mean_amt < 0.3:
                    accountants[account] = {
                        'recipient_count': unique_recipients,
                        'payment_count': len(payments),
                        'avg_amount': mean_amt,
                        'has_keywords': has_accountant_keywords
                    }
            
            # 如果有明确的关键词，也视为做账人员
            elif has_accountant_keywords:
                accountants[account] = {
                    'recipient_count': unique_recipients,
                    'payment_count': len(payments),
                    'has_keywords': has_accountant_keywords
                }

        # 记录到节点信息中
        for account, info in accountants.items():
            if account in self.nodes:
                self.nodes[account]['is_accountant'] = True
                self.nodes[account]['accountant_info'] = info
                print(f"  📊 检测到做账人员: {account} → 向 {info['recipient_count']} 个账号分发资金")

        return accountants

    def _detect_time_patterns(self):
        """
        分析时间模式，识别每周注入信用额度的规律

        基于网赌平台信息：
        1. 皇冠网站会向各个一级代理商提供一个账号
        2. 根据各个城市赌博"事业"的发展情况确定赌资信用额度
        3. 每个星期向账号注入一定量的信用额度
        4. 然后由三级代理商层层分包

        识别特征：
        1. 每周固定时间的资金注入
        2. 金额相对稳定
        3. 从境外或上级代理账号转入
        4. 备注关键词：包含"额度"、"注入"、"信用"等
        """
        if not hasattr(self, '_tx_original'):
            self._tx_original = self.tx.copy()

        tx = self._tx_original.copy()
        
        # 确保必要列存在
        for col in ['remark', 'desc', 'datetime_parsed']:
            if col not in tx.columns:
                if col == 'datetime_parsed':
                    return {}  # 没有时间信息，无法进行时间模式分析
                tx[col] = ''
            else:
                tx[col] = tx[col].fillna('').astype(str)

        # 筛选有效交易
        valid_tx = tx.dropna(subset=['datetime_parsed']).copy()
        if valid_tx.empty:
            return {}

        # 按账号分组
        account_transactions = {}
        for _, row in valid_tx.iterrows():
            account = str(row.get('owner_id', '') or '').strip()
            if not account:
                continue
            
            cp = str(row.get('cp_key', '') or '').strip()
            amt = float(row.get('amount_clean', 0) or 0)
            time = row.get('datetime_parsed')
            is_income = bool(row.get('is_income', False))
            remark = (str(row.get('remark', '') or '') + ' ' + 
                     str(row.get('desc', '') or '')).lower()
            
            if account not in account_transactions:
                account_transactions[account] = []
            account_transactions[account].append({
                'cp': cp,
                'amount': amt,
                'time': time,
                'is_income': is_income,
                'remark': remark
            })

        # 分析时间模式
        time_pattern_info = {}
        
        for account, transactions in account_transactions.items():
            if len(transactions) < 3:
                continue
            
            # 筛选收入交易（信用额度注入）
            income_tx = [t for t in transactions if t['is_income']]
            if len(income_tx) < 2:
                continue
            
            # 按时间排序
            sorted_tx = sorted(income_tx, key=lambda x: x['time'])
            
            # 计算时间间隔（天数）
            time_intervals = []
            for i in range(1, len(sorted_tx)):
                try:
                    # 确保时间是datetime对象
                    time1 = sorted_tx[i]['time']
                    time2 = sorted_tx[i-1]['time']
                    if isinstance(time1, str) and isinstance(time2, str):
                        # 尝试解析字符串时间
                        from datetime import datetime
                        # 尝试多种时间格式
                        formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d']
                        dt1 = None
                        dt2 = None
                        for fmt in formats:
                            try:
                                dt1 = datetime.strptime(time1, fmt)
                                dt2 = datetime.strptime(time2, fmt)
                                break
                            except:
                                continue
                        if dt1 and dt2:
                            delta = (dt1 - dt2).days
                        else:
                            continue
                    elif hasattr(time1, '__sub__') and hasattr(time2, '__sub__'):
                        delta = (time1 - time2).days
                    else:
                        continue
                    if delta > 0:
                        time_intervals.append(delta)
                except Exception:
                    continue
            
            # 检查是否有7天左右的周期性
            has_weekly_pattern = False
            if time_intervals:
                import numpy as np
                mean_interval = np.mean(time_intervals)
                # 时间间隔在6-8天之间，视为周周期
                if 6 <= mean_interval <= 8:
                    has_weekly_pattern = True
            
            # 检查金额是否稳定
            has_stable_amount = False
            amounts = [t['amount'] for t in income_tx if t['amount'] > 0]
            if len(amounts) >= 3:
                mean_amt = np.mean(amounts)
                std_amt = np.std(amounts)
                # 标准差与均值的比例小于0.2，说明金额较为稳定
                if std_amt / mean_amt < 0.2:
                    has_stable_amount = True
            
            # 检查是否有信用额度相关关键词
            has_credit_keywords = any(
                kw in t['remark'] for t in income_tx for kw in 
                ['额度', '注入', '信用', '皇冠', '平台', '周额']
            )
            
            # 综合判断
            if (has_weekly_pattern and has_stable_amount) or \
               (has_credit_keywords and has_weekly_pattern) or \
               (has_stable_amount and has_credit_keywords):
                time_pattern_info[account] = {
                    'has_weekly_pattern': has_weekly_pattern,
                    'has_stable_amount': has_stable_amount,
                    'has_credit_keywords': has_credit_keywords,
                    'transaction_count': len(income_tx),
                    'avg_amount': np.mean(amounts) if amounts else 0
                }

        # 记录到节点信息中
        for account, info in time_pattern_info.items():
            if account in self.nodes:
                self.nodes[account]['has_weekly_credit_injection'] = True
                self.nodes[account]['time_pattern_info'] = info
                print(f"  ⏰ 检测到每周信用额度注入账号: {account} → 平均金额: {info['avg_amount']:.2f}元")

        return time_pattern_info

    def _detect_large_settlement(self):
        """
        检测大额一次性结算交易

        基于网赌平台信息：
        1. 参赌人员每周结算时一次性支付大额人民币
        2. 一个星期的时间，有的参赌人员可能输几百万甚至更多
        3. 因为赌博的时候都不是现金，而是虚拟的信用额度
        4. 但是，在每周结算时，就要一次性支付人民币了

        识别特征：
        1. 大额交易：单笔交易金额较大（如50万以上）
        2. 周期性：每周固定时间的交易
        3. 备注关键词：包含"结算"、"还款"、"赌债"、"周结"等
        4. 交易模式：参赌人员向代理支付的单向大额转账
        """
        if not hasattr(self, '_tx_original'):
            self._tx_original = self.tx.copy()

        tx = self._tx_original.copy()
        
        # 确保必要列存在
        for col in ['remark', 'desc', 'datetime_parsed']:
            if col not in tx.columns:
                if col == 'datetime_parsed':
                    return {}  # 没有时间信息，无法进行时间模式分析
                tx[col] = ''
            else:
                tx[col] = tx[col].fillna('').astype(str)

        # 筛选支出交易（参赌人员向代理支付）
        outgoing_tx = tx[tx['is_income'] == False].copy()
        
        # 筛选大额交易
        large_tx = outgoing_tx[outgoing_tx['amount_clean'] >= 500000].copy()  # 50万以上视为大额
        if large_tx.empty:
            return {}

        # 按账号分组
        account_settlements = {}
        for _, row in large_tx.iterrows():
            account = str(row.get('owner_id', '') or '').strip()
            if not account:
                continue
            
            cp = str(row.get('cp_key', '') or '').strip()
            amt = float(row.get('amount_clean', 0) or 0)
            time = row.get('datetime_parsed')
            remark = (str(row.get('remark', '') or '') + ' ' + 
                     str(row.get('desc', '') or '')).lower()
            
            if account not in account_settlements:
                account_settlements[account] = []
            account_settlements[account].append({
                'cp': cp,
                'amount': amt,
                'time': time,
                'remark': remark
            })

        # 识别大额结算账号
        large_settlement_info = {}
        for account, settlements in account_settlements.items():
            # 至少有1笔大额交易
            if len(settlements) < 1:
                continue
            
            # 检查是否有结算相关关键词
            has_settlement_keywords = any(
                kw in s['remark'] for s in settlements for kw in 
                ['结算', '还款', '赌债', '周结', '皇冠', '平台', '信用', '额度']
            )
            
            # 检查是否有周期性
            has_periodic_pattern = False
            if len(settlements) >= 2:
                # 按时间排序
                sorted_sett = sorted(settlements, key=lambda x: x['time'])
                # 计算时间间隔
                intervals = []
                for i in range(1, len(sorted_sett)):
                    try:
                        time1 = sorted_sett[i]['time']
                        time2 = sorted_sett[i-1]['time']
                        if time1 and time2:
                            if isinstance(time1, str) and isinstance(time2, str):
                                # 尝试解析字符串时间
                                from datetime import datetime
                                # 尝试多种时间格式
                                formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d']
                                dt1 = None
                                dt2 = None
                                for fmt in formats:
                                    try:
                                        dt1 = datetime.strptime(time1, fmt)
                                        dt2 = datetime.strptime(time2, fmt)
                                        break
                                    except:
                                        continue
                                if dt1 and dt2:
                                    delta = (dt1 - dt2).days
                                else:
                                    continue
                            elif hasattr(time1, '__sub__') and hasattr(time2, '__sub__'):
                                delta = (time1 - time2).days
                            else:
                                continue
                            if delta > 0:
                                intervals.append(delta)
                    except Exception:
                        continue
                if intervals:
                    import numpy as np
                    mean_interval = np.mean(intervals)
                    # 时间间隔在6-8天之间，视为周周期
                    if 6 <= mean_interval <= 8:
                        has_periodic_pattern = True
            
            # 检查交易金额是否特别大
            total_amount = sum(s['amount'] for s in settlements)
            avg_amount = total_amount / len(settlements)
            is_large_amount = avg_amount >= 500000  # 平均50万以上
            
            # 综合判断
            if (has_settlement_keywords and is_large_amount) or \
               (has_periodic_pattern and is_large_amount) or \
               (len(settlements) >= 2 and is_large_amount):
                large_settlement_info[account] = {
                    'settlement_count': len(settlements),
                    'total_amount': total_amount,
                    'avg_amount': avg_amount,
                    'has_settlement_keywords': has_settlement_keywords,
                    'has_periodic_pattern': has_periodic_pattern
                }

        # 记录到节点信息中
        for account, info in large_settlement_info.items():
            if account in self.nodes:
                self.nodes[account]['is_large_settlement'] = True
                self.nodes[account]['large_settlement_info'] = info
                print(f"  [$$$] 检测到大额结算账号: {account} → 总金额: {info['total_amount']:.2f}元")

        return large_settlement_info

    def _detect_credit_allocation(self):
        """
        检测代理层级间的信用额度分配模式

        基于网赌平台信息：
        1. 皇冠网站向一级代理商提供信用额度账号
        2. 一级代理向下级代理分配信用额度
        3. 三级代理商层层分包信用额度

        识别特征：
        1. 层级间资金流动：从上级代理到下级代理的资金分配
        2. 批量分配：向多个不同账号进行相似金额的转账
        3. 定期注入：每周固定时间的信用额度注入
        4. 备注关键词：包含"额度"、"分配"、"分包"、"代理"等
        """
        if not hasattr(self, '_tx_original'):
            self._tx_original = self.tx.copy()

        tx = self._tx_original.copy()
        
        # 确保必要列存在
        for col in ['remark', 'desc']:
            if col not in tx.columns:
                tx[col] = ''
            else:
                tx[col] = tx[col].fillna('').astype(str)

        # 筛选支出交易（从上级向下级分配）
        outgoing_tx = tx[tx['is_income'] == False].copy()
        
        # 按账号分组
        account_allocations = {}
        for _, row in outgoing_tx.iterrows():
            account = str(row.get('owner_id', '') or '').strip()
            if not account:
                continue
            
            cp = str(row.get('cp_key', '') or '').strip()
            amt = float(row.get('amount_clean', 0) or 0)
            time = row.get('datetime_parsed')
            remark = (str(row.get('remark', '') or '') + ' ' + 
                     str(row.get('desc', '') or '')).lower()
            
            if account not in account_allocations:
                account_allocations[account] = []
            account_allocations[account].append({
                'cp': cp,
                'amount': amt,
                'time': time,
                'remark': remark
            })

        # 识别信用额度分配账号
        credit_allocation_info = {}
        for account, allocations in account_allocations.items():
            # 至少向3个不同账号分配
            unique_recipients = len(set(a['cp'] for a in allocations))
            if unique_recipients < 3:
                continue
            
            # 检查是否有信用额度相关关键词
            has_credit_keywords = any(
                kw in a['remark'] for a in allocations for kw in 
                ['额度', '分配', '分包', '代理', '皇冠', '平台', '信用']
            )
            
            # 检查分配金额是否相似（可能是固定比例的信用额度）
            amounts = [a['amount'] for a in allocations if a['amount'] > 0]
            has_similar_amounts = False
            if len(amounts) > 3:
                import numpy as np
                mean_amt = np.mean(amounts)
                std_amt = np.std(amounts)
                # 标准差与均值的比例小于0.4，说明金额较为相似
                if std_amt / mean_amt < 0.4:
                    has_similar_amounts = True
            
            # 检查是否有周期性
            has_periodic_pattern = False
            if len(allocations) >= 3:
                # 按时间排序
                sorted_alloc = sorted(allocations, key=lambda x: x['time'])
                # 计算时间间隔
                intervals = []
                for i in range(1, len(sorted_alloc)):
                    try:
                        time1 = sorted_alloc[i]['time']
                        time2 = sorted_alloc[i-1]['time']
                        if time1 and time2:
                            if isinstance(time1, str) and isinstance(time2, str):
                                # 尝试解析字符串时间
                                from datetime import datetime
                                # 尝试多种时间格式
                                formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d']
                                dt1 = None
                                dt2 = None
                                for fmt in formats:
                                    try:
                                        dt1 = datetime.strptime(time1, fmt)
                                        dt2 = datetime.strptime(time2, fmt)
                                        break
                                    except:
                                        continue
                                if dt1 and dt2:
                                    delta = (dt1 - dt2).days
                                else:
                                    continue
                            elif hasattr(time1, '__sub__') and hasattr(time2, '__sub__'):
                                delta = (time1 - time2).days
                            else:
                                continue
                            if delta > 0:
                                intervals.append(delta)
                    except Exception:
                        continue
                if intervals:
                    mean_interval = np.mean(intervals)
                    # 时间间隔在6-8天之间，视为周周期
                    if 6 <= mean_interval <= 8:
                        has_periodic_pattern = True
            
            # 综合判断
            if (has_credit_keywords and unique_recipients >= 3) or \
               (has_similar_amounts and unique_recipients >= 3) or \
               (has_periodic_pattern and unique_recipients >= 3):
                credit_allocation_info[account] = {
                    'recipient_count': unique_recipients,
                    'allocation_count': len(allocations),
                    'has_credit_keywords': has_credit_keywords,
                    'has_similar_amounts': has_similar_amounts,
                    'has_periodic_pattern': has_periodic_pattern
                }

        # 记录到节点信息中
        for account, info in credit_allocation_info.items():
            if account in self.nodes:
                self.nodes[account]['is_credit_allocator'] = True
                self.nodes[account]['credit_allocation_info'] = info
                print(f"  [OUT] 检测到信用额度分配账号: {account} → 向 {info['recipient_count']} 个下级分配")

        return credit_allocation_info

    def _detect_credit_settlement(self):
        """
        检测信用额度结算周期和模式

        基于网赌平台信息：
        1. 每周结算一次，一比一兑换成人民币
        2. 皇冠网站向一级代理商提供信用额度账号
        3. 三级代理商层层分包信用额度
        4. 参赌人员每周结算时一次性支付大额人民币

        识别特征：
        1. 周期性交易：每周固定时间的大额交易
        2. 信用额度注入：从上级代理向下级代理的定期资金流动
        3. 大额一次性结算：参赌人员向代理支付的大额交易
        4. 备注关键词：包含"结算"、"信用"、"额度"、"充值"等
        """
        if not hasattr(self, '_tx_original'):
            self._tx_original = self.tx.copy()

        tx = self._tx_original.copy()
        
        # 确保必要列存在
        for col in ['remark', 'desc', 'datetime_parsed']:
            if col not in tx.columns:
                if col == 'datetime_parsed':
                    return {}  # 没有时间信息，无法进行时间模式分析
                tx[col] = ''
            else:
                tx[col] = tx[col].fillna('').astype(str)

        # 筛选有效交易
        valid_tx = tx.dropna(subset=['datetime_parsed']).copy()
        if valid_tx.empty:
            return {}

        # 按账号分组
        account_transactions = {}
        for _, row in valid_tx.iterrows():
            account = str(row.get('owner_id', '') or '').strip()
            if not account:
                continue
            
            cp = str(row.get('cp_key', '') or '').strip()
            amt = float(row.get('amount_clean', 0) or 0)
            time = row.get('datetime_parsed')
            is_income = bool(row.get('is_income', False))
            remark = (str(row.get('remark', '') or '') + ' ' + 
                     str(row.get('desc', '') or '')).lower()
            
            if account not in account_transactions:
                account_transactions[account] = []
            account_transactions[account].append({
                'cp': cp,
                'amount': amt,
                'time': time,
                'is_income': is_income,
                'remark': remark
            })

        # 分析周期性模式
        credit_settlement_info = {}
        
        for account, transactions in account_transactions.items():
            if len(transactions) < 3:
                continue
            
            # 按时间排序
            sorted_tx = sorted(transactions, key=lambda x: x['time'])
            
            # 计算时间间隔（天数）
            time_intervals = []
            for i in range(1, len(sorted_tx)):
                try:
                    # 确保时间是datetime对象
                    time1 = sorted_tx[i]['time']
                    time2 = sorted_tx[i-1]['time']
                    if isinstance(time1, str) and isinstance(time2, str):
                        # 尝试解析字符串时间
                        from datetime import datetime
                        # 尝试多种时间格式
                        formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d']
                        dt1 = None
                        dt2 = None
                        for fmt in formats:
                            try:
                                dt1 = datetime.strptime(time1, fmt)
                                dt2 = datetime.strptime(time2, fmt)
                                break
                            except:
                                continue
                        if dt1 and dt2:
                            delta = (dt1 - dt2).days
                        else:
                            continue
                    elif hasattr(time1, '__sub__') and hasattr(time2, '__sub__'):
                        delta = (time1 - time2).days
                    else:
                        continue
                    if delta > 0:
                        time_intervals.append(delta)
                except Exception:
                    continue
            
            # 检查是否有7天左右的周期性
            has_weekly_pattern = False
            if time_intervals:
                import numpy as np
                mean_interval = np.mean(time_intervals)
                # 时间间隔在6-8天之间，视为周结算周期
                if 6 <= mean_interval <= 8:
                    has_weekly_pattern = True
            
            # 检查是否有大额交易
            large_transactions = [t for t in transactions if t['amount'] >= 50000]  # 5万以上视为大额
            has_large_transactions = len(large_transactions) >= 1
            
            # 检查备注关键词
            has_credit_keywords = any(
                kw in t['remark'] for t in transactions for kw in 
                ['结算', '信用', '额度', '充值', '周结', '皇冠', '平台']
            )
            
            # 检查是否有从上级代理接收资金（信用额度注入）
            incoming_from_agents = []
            outgoing_to_agents = []
            for t in transactions:
                if t['is_income']:
                    incoming_from_agents.append(t)
                else:
                    outgoing_to_agents.append(t)
            
            # 综合判断
            if (has_weekly_pattern and has_large_transactions) or \
               (has_credit_keywords and has_weekly_pattern) or \
               (len(large_transactions) >= 2 and has_weekly_pattern):
                credit_settlement_info[account] = {
                    'has_weekly_pattern': has_weekly_pattern,
                    'large_transaction_count': len(large_transactions),
                    'has_credit_keywords': has_credit_keywords,
                    'incoming_transactions': len(incoming_from_agents),
                    'outgoing_transactions': len(outgoing_to_agents)
                }

        # 记录到节点信息中
        for account, info in credit_settlement_info.items():
            if account in self.nodes:
                self.nodes[account]['is_credit_settlement'] = True
                self.nodes[account]['credit_settlement_info'] = info
                print(f"  💰 检测到信用额度结算账号: {account} → 周结算模式")

        return credit_settlement_info

    def _merge_person_nodes(self) -> int:
        """
        将身份对照表中同一真实人物名下的多个账号节点合并为单一规范节点。

        设计原则（彻底重写，避免之前的节点生命周期问题）：
          1. 先收集所有需要合并的组（规范ID → 别名列表），不修改图
          2. 对每组：
             a. 把所有别名的边先收集到临时字典，再统一更新到规范ID节点
             b. 确认规范ID节点确实在图中后，再删除别名节点
             c. 最后统一重算规范ID节点的 in_degree/out_degree（直接查图，不缓存）
          3. 同步更新 self.tx 中的 owner_id / cp_key
        """
        merge_map = self.identity.build_merge_map()
        if not merge_map:
            return 0

        # ── 保存原始流水（供洗钱识别使用）────────────────────────────────
        if not hasattr(self, '_tx_original'):
            self._tx_original = self.tx.copy()

        # ── Step 1：按规范ID分组，只收集图中真实存在的别名 ──────────────
        from collections import defaultdict
        groups: dict = defaultdict(set)   # canonical → set(alias节点)
        
        for node in list(self.G.nodes()):
            node_str = str(node).strip()
            
            # 直接使用 get_canonical_id 获取规范ID，支持精确匹配和部分匹配
            canonical = self.identity.get_canonical_id(node_str)
            
            if not canonical or canonical == node_str:
                # 如果规范ID就是节点本身，或者获取失败，跳过
                # 但如果是姓名节点且在 name_to_accounts 中，仍然纳入合并组
                if canonical in self.identity.name_to_accounts:
                    groups[canonical].add(node)
                continue
            # 别名节点（账号 → 真实姓名）
            groups[canonical].add(node)

        if not groups:
            return 0

        merged_count = 0

        for canonical, alias_set in groups.items():
            # 过滤：只处理图中真实存在的别名
            real_aliases = [a for a in alias_set if self.G.has_node(a) and a != canonical]
            if not real_aliases:
                continue

            # ── Step 2a：确保规范ID节点存在 ────────────────────────────
            if not self.G.has_node(canonical):
                self.G.add_node(canonical)

            # ── Step 2b：收集所有别名的边，合并到规范ID节点 ─────────────
            # 用临时字典先累积，避免边修改过程中迭代异常
            # in_edges_to_add: {pred → weight, count}
            # out_edges_to_add: {succ → weight, count}
            in_edges: dict  = {}   # pred → (weight, count)
            out_edges: dict = {}   # succ → (weight, count)

            for alias in real_aliases:
                for pred, _, data in list(self.G.in_edges(alias, data=True)):
                    # 跳过别名组内部互转（会变成自环）
                    src = str(pred).strip()
                    if src == canonical or src in {str(a).strip() for a in alias_set}:
                        continue
                    w = float(data.get('weight', 0) or 0)
                    c = int(data.get('count', 0) or 0)
                    if pred in in_edges:
                        in_edges[pred] = (in_edges[pred][0] + w, in_edges[pred][1] + c)
                    else:
                        in_edges[pred] = (w, c)

                for _, succ, data in list(self.G.out_edges(alias, data=True)):
                    # 跳过别名组内部互转（会变成自环）
                    dst = str(succ).strip()
                    if dst == canonical or dst in {str(a).strip() for a in alias_set}:
                        continue
                    w = float(data.get('weight', 0) or 0)
                    c = int(data.get('count', 0) or 0)
                    if succ in out_edges:
                        out_edges[succ] = (out_edges[succ][0] + w, out_edges[succ][1] + c)
                    else:
                        out_edges[succ] = (w, c)

            # ── Step 2c：将收集到的边写入规范ID节点（合并已有边权重）──────
            for pred, (w, c) in in_edges.items():
                if self.G.has_edge(pred, canonical):
                    self.G[pred][canonical]['weight'] = \
                        float(self.G[pred][canonical].get('weight', 0) or 0) + w
                    self.G[pred][canonical]['count'] = \
                        int(self.G[pred][canonical].get('count', 0) or 0) + c
                else:
                    self.G.add_edge(pred, canonical, weight=w, count=c)

            for succ, (w, c) in out_edges.items():
                if self.G.has_edge(canonical, succ):
                    self.G[canonical][succ]['weight'] = \
                        float(self.G[canonical][succ].get('weight', 0) or 0) + w
                    self.G[canonical][succ]['count'] = \
                        int(self.G[canonical][succ].get('count', 0) or 0) + c
                else:
                    self.G.add_edge(canonical, succ, weight=w, count=c)

            # ── Step 2d：删除别名节点 ────────────────────────────────────
            # 收集注册状态信息
            is_canonical_registered = False
            for alias in real_aliases + [canonical]:
                if alias in self.nodes and self.nodes[alias].get('is_registered', False):
                    is_canonical_registered = True
                    break
            
            for alias in real_aliases:
                if self.G.has_node(alias):
                    self.G.remove_node(alias)
                self.nodes.pop(alias, None)
                merged_count += 1

            # ── Step 2e：重算规范ID节点统计量 ────────────────────────────
            # 此时 canonical 一定在图中（刚刚确认过），安全读取度数
            if not self.G.has_node(canonical):
                # 极端情况：canonical 在 in_edges/out_edges 为空且被清理
                self.nodes.pop(canonical, None)
                continue

            in_flow  = sum(float(d.get('weight', 0) or 0)
                           for _, _, d in self.G.in_edges(canonical, data=True))
            out_flow = sum(float(d.get('weight', 0) or 0)
                           for _, _, d in self.G.out_edges(canonical, data=True))

            # 孤立幽灵节点（既无入也无出）：直接清除
            if in_flow == 0 and out_flow == 0 and self.G.degree(canonical) == 0:
                self.G.remove_node(canonical)
                self.nodes.pop(canonical, None)
                continue

            # ✅ 此处 canonical 确认在图中，in_degree/out_degree 返回整数
            self.nodes[canonical] = {
                'name':         self.identity.resolve(canonical),
                'in_flow':      in_flow,
                'out_flow':     out_flow,
                'net_flow':     in_flow - out_flow,
                'in_degree':    self.G.in_degree(canonical),    # int，节点已确认存在
                'out_degree':   self.G.out_degree(canonical),   # int，节点已确认存在
                'total_flow':   in_flow + out_flow,
                'net_absolute': abs(in_flow - out_flow),
                'level':        0,
                'risk_score':   0,
                'is_registered': is_canonical_registered,
                'original_account': canonical,
            }

        # ── Step 3：同步流水表中的账号标识 ──────────────────────────────
        # 使用 get_canonical_id 来获取规范ID，支持部分匹配
        self.tx['owner_id'] = self.tx['owner_id'].apply(
            lambda x: self.identity.get_canonical_id(str(x).strip()))
        self.tx['cp_key']   = self.tx['cp_key'].apply(
            lambda x: self.identity.get_canonical_id(str(x).strip()))

        return merged_count

    # ═══════════════════════════════════════════════════════════
    # 层级分类辅助方法
    # ═══════════════════════════════════════════════════════════

    def _compute_topo_depth(self) -> dict:
        """计算拓扑深度（BFS）"""
        from collections import deque
        G = self.G
        topo_depth: dict = {}
        roots = [n for n in G.nodes() if G.in_degree(n) == 0]
        if not roots:
            roots = sorted(G.nodes(), key=lambda n: self.nodes.get(n, {}).get('in_flow', 0),
                           reverse=True)[:max(1, len(G.nodes()) // 10)]

        queue: deque = deque()
        in_queue: set = set()
        for r in roots:
            topo_depth[r] = 0
            queue.append(r)
            in_queue.add(r)
        while queue:
            cur = queue.popleft()
            for succ in list(G.successors(cur)):
                new_d = topo_depth[cur] + 1
                if succ not in topo_depth or topo_depth[succ] < new_d:
                    topo_depth[succ] = new_d
                    if succ not in in_queue:
                        queue.append(succ)
                        in_queue.add(succ)
        return topo_depth

    def _build_feature_matrix(self, node_list: list, topo_depth: dict) -> 'np.ndarray':
        """构建节点特征向量矩阵"""
        import numpy as np
        max_depth = max(topo_depth.values(), default=0) or 1
        feature_matrix = []

        for node in node_list:
            info = self.nodes[node]
            in_f  = info['in_flow']
            out_f = info['out_flow']
            in_d  = int(info.get('in_degree', 0) or 0)
            out_d = int(info.get('out_degree', 0) or 0)
            total = max(info['total_flow'], 1)
            depth = topo_depth.get(node, 0)

            f0 = np.log1p(in_f)
            f1 = np.log1p(out_f)
            f2 = np.log1p(out_d)
            f3 = np.log1p(in_d)
            f4 = (out_f - in_f) / total
            f5 = min(out_f / max(in_f, 1), 5.0)
            f6 = depth / max_depth
            f7 = 1.0 if in_d > 0 else 0.0
            f8 = 1.0 if out_d > 0 else 0.0
            feature_matrix.append([f0, f1, f2, f3, f4, f5, f6, f7, f8])

        return np.array(feature_matrix, dtype=np.float32)

    def _kmeans_clustering(self, X: 'np.ndarray', node_list: list) -> 'Optional[np.ndarray]':
        """执行K-Means聚类并返回聚类标签"""
        try:
            from sklearn.cluster import KMeans
            from sklearn.preprocessing import StandardScaler
        except ImportError:
            return None

        n_nodes = len(node_list)
        if n_nodes < 10:
            return None

        try:
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            # 自适应k值
            if n_nodes >= 30:
                k = 5
            elif n_nodes >= 15:
                k = 4
            else:
                k = 3

            km = KMeans(n_clusters=k, random_state=42, n_init=10, max_iter=300)
            cluster_labels = km.fit_predict(X_scaled)

            # 将簇映射到层级
            self._assign_levels_from_clusters(X, cluster_labels, node_list, k)
            return cluster_labels

        except Exception as e:
            print(f"  ⚠️  K-Means 聚类失败（{e}），回退到规则判定")
            return None

    def _assign_levels_from_clusters(self, X: 'np.ndarray', cluster_labels: 'np.ndarray',
                                      node_list: list, k: int):
        """根据聚类结果分配层级"""
        import numpy as np
        from collections import defaultdict

        cluster_nodes: dict = defaultdict(list)
        for idx, cl in enumerate(cluster_labels):
            cluster_nodes[cl].append(idx)

        # 计算每个簇的统计特征
        cluster_stats = {}
        for cl, idxes in cluster_nodes.items():
            arr = X[idxes]
            cluster_stats[cl] = {
                'median_in_flow': np.median(arr[:, 0]),
                'median_out_d': np.median(arr[:, 2]),
                'median_net_out': np.median(arr[:, 4]),
                'median_depth': np.median(arr[:, 6]),
                'n': len(idxes),
            }

        # 代理得分 = 流入高 + 出度高 - 深度惩罚
        def _agent_score(st):
            return st['median_in_flow'] * 0.5 + st['median_out_d'] * 0.5 - st['median_depth'] * 0.3

        sorted_clusters = sorted(cluster_stats.keys(),
                                 key=lambda c: _agent_score(cluster_stats[c]),
                                 reverse=True)

        # 层级映射
        if k == 5:
            level_map_order = [LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_GAMBLER, LEVEL_UNKNOWN]
        elif k == 4:
            level_map_order = [LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_GAMBLER, LEVEL_UNKNOWN]
        else:
            level_map_order = [LEVEL_AGENT3, LEVEL_GAMBLER, LEVEL_UNKNOWN]

        level_assignment = {}
        for i, cl in enumerate(sorted_clusters):
            lv = level_map_order[min(i, len(level_map_order) - 1)]
            level_assignment[cl] = lv

        # 赋值
        for idx, cl in enumerate(cluster_labels):
            node = node_list[idx]
            self.nodes[node]['level'] = level_assignment.get(cl, LEVEL_UNKNOWN)

    def _compute_percentiles(self) -> dict:
        """计算全网流量百分位数"""
        import numpy as np
        all_in_flows = np.array([info['in_flow'] for info in self.nodes.values()])
        all_out_degrees = np.array([int(info.get('out_degree', 0) or 0)
                                     for info in self.nodes.values()], dtype=float)
        all_in_degrees = np.array([int(v.get('in_degree', 0) or 0)
                                    for v in self.nodes.values()], dtype=float)

        return {
            'p50_in': np.percentile(all_in_flows, 50),
            'p70_in': np.percentile(all_in_flows, 70),
            'p80_in': np.percentile(all_in_flows, 80),
            'p90_in': np.percentile(all_in_flows, 90),
            'p95_in': np.percentile(all_in_flows, 95),
            'p99_in': np.percentile(all_in_flows, 99),
            'p70_out_d': max(np.percentile(all_out_degrees, 70), 2),
            'p80_out_d': max(np.percentile(all_out_degrees, 80), 2),
            'p90_out_d': max(np.percentile(all_out_degrees, 90), 3),
            'p80_in_d': max(np.percentile(all_in_degrees, 80), 2),
            'all_in_flows': all_in_flows,
            'all_out_degrees': all_out_degrees,
        }

    def _apply_rule_corrections(self, node_list: list, topo_depth: dict,
                                 percentiles: dict, cluster_labels: 'Optional[np.ndarray]'):
        """应用规则修正层级判定"""
        import numpy as np
        max_depth = max(topo_depth.values(), default=0) or 1

        p = percentiles

        for idx, node in enumerate(node_list):
            info = self.nodes[node]
            in_f = info['in_flow']
            out_f = info['out_flow']
            in_d = int(info.get('in_degree', 0) or 0)
            out_d = int(info.get('out_degree', 0) or 0)
            total = info['total_flow']
            depth = topo_depth.get(node, 0)
            depth_ratio = depth / max_depth

            # 若聚类未运行，先给初始层级
            if cluster_labels is None:
                if in_f >= p['p95_in'] and (out_d >= 3 or in_d >= 5):
                    init_lv = LEVEL_AGENT1
                elif in_f >= p['p80_in'] and out_d >= 2:
                    init_lv = LEVEL_AGENT2
                elif in_f >= p['p70_in'] and (out_d >= 1 or in_d >= 2):
                    init_lv = LEVEL_AGENT3
                elif out_d <= 1:
                    init_lv = LEVEL_GAMBLER
                else:
                    init_lv = LEVEL_UNKNOWN
                info['level'] = init_lv

            current_lv = info['level']
            net_out_ratio = (out_f - in_f) / max(total, 1)

            # 赌客判定
            self._classify_gambler(info, current_lv, in_f, out_f, in_d, out_d, total,
                                   net_out_ratio, p)

            # 代理判定
            self._classify_agent(info, in_f, out_d, in_d, p)

            # 一级代理门槛收紧
            self._validate_level1_agent(info, in_f, out_d, in_d, p)

            # 外围判定
            if total < 1000 and in_d <= 1 and out_d <= 1:
                info['level'] = LEVEL_UNKNOWN

            # 记录调试信息
            info['net_out_ratio'] = float(net_out_ratio)
            info['in_flow_pct'] = float(np.mean(p['all_in_flows'] <= in_f))
            info['out_deg_pct'] = float(np.mean(p['all_out_degrees'] <= out_d))
            info['depth_ratio'] = float(depth_ratio)

    def _classify_gambler(self, info: dict, current_lv: int, in_f: float, out_f: float,
                          in_d: int, out_d: int, total: float, net_out_ratio: float, p: dict):
        """赌客特征判定"""
        # 典型赌客：净流出为主 + 出度小 + 入度小
        gambler_flag = (
            out_d <= 2 and
            in_d <= 2 and
            net_out_ratio > 0.2 and
            in_f < p['p80_in']
        )
        if gambler_flag and current_lv in (LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_UNKNOWN):
            info['level'] = LEVEL_GAMBLER

        # 纯充值型赌客
        if out_d >= 1 and in_d <= 1 and in_f <= p['p50_in'] and net_out_ratio > 0.5:
            if current_lv in (LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_UNKNOWN):
                info['level'] = LEVEL_GAMBLER

        # 有返奖的活跃赌客
        if out_f > in_f and in_f > 0 and out_d <= 3 and in_d <= 2:
            if current_lv in (LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_UNKNOWN):
                info['level'] = LEVEL_GAMBLER

    def _classify_agent(self, info: dict, in_f: float, out_d: int, in_d: int, p: dict):
        """代理特征加强判定"""
        # 三级代理
        if out_d >= p['p80_out_d'] and in_f >= p['p70_in']:
            if info['level'] in (LEVEL_GAMBLER, LEVEL_UNKNOWN):
                info['level'] = LEVEL_AGENT3

        # 二级代理
        if out_d >= p['p90_out_d'] and in_f >= p['p80_in']:
            if info['level'] in (LEVEL_GAMBLER, LEVEL_UNKNOWN, LEVEL_AGENT3):
                info['level'] = LEVEL_AGENT2

        # 汇聚型代理
        if in_d >= p['p80_in_d'] and in_f >= p['p70_in']:
            if info['level'] in (LEVEL_GAMBLER, LEVEL_UNKNOWN):
                info['level'] = LEVEL_AGENT3

    def _validate_level1_agent(self, info: dict, in_f: float, out_d: int, in_d: int, p: dict):
        """一级代理门槛收紧验证"""
        if info['level'] == LEVEL_AGENT1:
            is_top_inflow = in_f >= p['p95_in']
            is_high_out = out_d >= p['p80_out_d']
            is_high_in_d = in_d >= max(p['p80_out_d'], 3)
            if not is_top_inflow:
                info['level'] = LEVEL_AGENT2
            elif not (is_high_out or is_high_in_d):
                info['level'] = LEVEL_AGENT2

    def _fix_topology_consistency(self):
        """拓扑一致性迭代修正"""
        G = self.G
        for _iter in range(3):
            changed = False
            for node, info in self.nodes.items():
                level = info['level']
                if level > LEVEL_AGENT3:
                    continue

                in_neighbors = [n for n in G.predecessors(node) if n in self.nodes]
                if not in_neighbors:
                    continue

                upstream_levels = [self.nodes[n]['level'] for n in in_neighbors]

                # 若所有上游都是赌客/外围，自己不太可能是一级代理
                if all(lv >= LEVEL_GAMBLER for lv in upstream_levels):
                    if level == LEVEL_AGENT1:
                        self.nodes[node]['level'] = LEVEL_AGENT2
                        changed = True
                    elif level == LEVEL_AGENT2:
                        # 二级代理且上游全是赌客，降为三级
                        self.nodes[node]['level'] = LEVEL_AGENT3
                        changed = True

                # 若上游有一级代理，自己最多是二级
                if LEVEL_AGENT1 in upstream_levels:
                    if level == LEVEL_AGENT1:
                        self.nodes[node]['level'] = LEVEL_AGENT2
                        changed = True

            if not changed:
                break

    def _classify_levels(self):
        """
        层级判定 —— 赌球团伙金字塔结构（五级）

        核心方法：K-Means 聚类 + 拓扑/特征修正

        流程：
          1. 构建每个节点的特征向量（流量、出入度、拓扑深度、净流出比等）
          2. 用 K-Means(k=5) 对节点聚类，识别"高流量高出度/中流量/低流量"等簇
          3. 按各簇质心的特征将簇映射到五个层级（一级代理→赌客→外围）
          4. 对少量极端节点做规则修正（如出度=0的节点通常是赌客）
          5. 拓扑一致性修正（迭代3轮，修正逻辑矛盾）

        金字塔比例约束（贴近实际案件）：
          一级代理   ≈ 1-5%    （顶层极少数）
          二级代理   ≈ 5-15%   （中间层）
          三级代理   ≈ 20-40%  （最常见的代理）
          赌客       ≈ 40-70%  （底层最多）
          待定外围   ≈ 剩余
        """
        if not self.nodes:
            return

        # 第一步：计算拓扑深度
        topo_depth = self._compute_topo_depth()

        # 第二步：构建特征向量
        node_list = list(self.nodes.keys())
        X = self._build_feature_matrix(node_list, topo_depth)

        # 第三步：K-Means 聚类
        cluster_labels = self._kmeans_clustering(X, node_list)

        # 第四步：计算百分位数
        percentiles = self._compute_percentiles()

        # 第五步：规则修正
        self._apply_rule_corrections(node_list, topo_depth, percentiles, cluster_labels)

        # 第六步：拓扑一致性修正
        self._fix_topology_consistency()

    def _detect_money_laundering(self):
        """
        洗钱账户识别（境外转账 + 资金中转特征综合打分）

        ── 设计说明（彻底重写，解决 key 不统一问题）──────────────────────────
        之前的问题：
          · _tx_original.owner_id = 原始账号（手机号/邮箱）
          · self.nodes 的 key  = 合并后规范ID（真实姓名）
          · 两侧 key 不匹配 → overseas_hit 永远查不到值

        新设计：
          · 全部使用 self.tx（_merge_person_nodes 之后的合并流水）
          · self.tx.owner_id 已经通过 merge_map 替换为规范ID
          · cp_key 同样已替换为规范ID
          · 与 self.nodes 的 key 完全一致，不需要任何额外映射

        五维度打分：
          A. 境外转账关键词命中（cp_key 或 remark 含境外词）
          B. 高过路率（out/in ≥ 80% 且净余额 < 15%）
          C. 快速中转（收款后 24h 内大额转出）
          D. 整数金额占比高（≥70% 整千，资金拆分特征）
          E. 上游高层→下游末端的单线中转路径
        """
        G = self.G

        # ── 全部使用合并后的 self.tx ──────────────────────────────────────
        # 此时 owner_id 和 cp_key 都已是规范ID，与 self.nodes 的 key 一致
        tx = self.tx.copy()

        # 确保文本列存在
        for col in ['remark', 'desc']:
            if col not in tx.columns:
                tx[col] = ''
            else:
                tx[col] = tx[col].fillna('').astype(str)

        # ── A. 境外关键词命中 ─────────────────────────────────────────────
        # 用 cp_key（已规范化的对方标识）和 remark/desc 检查境外关键词
        overseas_hit:    dict = {}   # owner_id(规范ID) → 命中笔数
        overseas_amount: dict = {}   # owner_id(规范ID) → 命中总金额

        for _, row in tx.iterrows():
            owner = str(row.get('owner_id', '') or '').strip()
            if not owner:
                continue
            cp     = str(row.get('cp_key', '') or '').strip()
            remark = str(row.get('remark', '') or '') + str(row.get('desc', '') or '')
            amt    = float(row.get('amount_clean', 0) or 0)

            text = (cp + ' ' + remark).lower()
            if any(kw.lower() in text for kw in OVERSEAS_KEYWORDS):
                overseas_hit[owner]    = overseas_hit.get(owner, 0) + 1
                overseas_amount[owner] = overseas_amount.get(owner, 0) + amt

        # ── C. 快速中转：收款后 24h 内大额转出 ───────────────────────────
        fast_transit: set = set()
        if 'datetime_parsed' in tx.columns:
            tx_sorted = tx.dropna(subset=['datetime_parsed']).sort_values('datetime_parsed')
            for owner, grp in tx_sorted.groupby('owner_id'):
                incomes  = grp[grp['is_income'] == True]
                outcomes = grp[grp['is_income'] == False]
                if incomes.empty or outcomes.empty:
                    continue
                if incomes['amount_clean'].sum() < MONEY_LAUNDER_MIN_FLOW:
                    continue
                for _, inc_row in incomes.iterrows():
                    t0  = inc_row['datetime_parsed']
                    t1  = t0 + pd.Timedelta(hours=MONEY_LAUNDER_BURST_INTERVAL)
                    out_window = outcomes[
                        (outcomes['datetime_parsed'] >= t0) &
                        (outcomes['datetime_parsed'] <= t1)
                    ]['amount_clean'].sum()
                    if out_window >= inc_row['amount_clean'] * 0.7:
                        fast_transit.add(str(owner))
                        break

        # ── D. 整数金额占比 ───────────────────────────────────────────────
        round_ratio: dict = {}
        for owner, grp in tx.groupby('owner_id'):
            n = len(grp)
            if n == 0:
                continue
            round_ratio[str(owner)] = int((grp['amount_clean'] % 1000 == 0).sum()) / n

        # ── 逐节点综合打分 ────────────────────────────────────────────────
        for node, info in self.nodes.items():
            nid   = str(node)
            in_f  = float(info.get('in_flow', 0) or 0)
            out_f = float(info.get('out_flow', 0) or 0)
            total = float(info.get('total_flow', 0) or 0)

            if total < MONEY_LAUNDER_MIN_FLOW:
                info['launder_score']      = 0
                info['launder_flags']      = []
                info['is_launder_suspect'] = False
                continue

            flags:   list  = []
            l_score: float = 0.0

            # A. 境外关键词
            ov_cnt = int(overseas_hit.get(nid, 0))
            ov_amt = float(overseas_amount.get(nid, 0))
            if ov_cnt >= 3:
                flags.append(f'多次境外转账({ov_cnt}笔，¥{ov_amt/10000:.1f}万)')
                l_score += 40
            elif ov_cnt >= 1:
                flags.append(f'涉境外转账({ov_cnt}笔)')
                l_score += 25

            # B. 高过路率
            if in_f > 0:
                transit_r = out_f / in_f
                balance_r = abs(in_f - out_f) / max(in_f, 1)
                if transit_r >= MONEY_LAUNDER_TRANSIT_RATIO and \
                        balance_r <= MONEY_LAUNDER_MAX_BALANCE_RATIO:
                    flags.append(
                        f'高过路率({transit_r:.0%}流入后转出，净余额仅{balance_r:.0%})')
                    l_score += transit_r * 25

            # C. 快速中转
            if nid in fast_transit:
                flags.append('快速中转（收款后24h内大额转出）')
                l_score += 20

            # D. 整数金额占比
            rr = float(round_ratio.get(nid, 0))
            if rr >= 0.7 and total >= LEVEL2_MIN_INFLOW:
                flags.append(f'整数金额占比高({rr:.0%}，疑似资金拆分)')
                l_score += rr * 15

            # E. 上游高层→下游末端单线中转
            in_d  = int(info.get('in_degree', 0) or 0)
            out_d = int(info.get('out_degree', 0) or 0)
            if in_d <= 2 and out_d <= 2 and in_f >= LEVEL2_MIN_INFLOW:
                in_levels  = [self.nodes[n].get('level', LEVEL_UNKNOWN)
                              for n in G.predecessors(node) if n in self.nodes]
                out_levels = [self.nodes[n].get('level', LEVEL_UNKNOWN)
                              for n in G.successors(node) if n in self.nodes]
                if in_levels and out_levels:
                    avg_in  = sum(in_levels)  / len(in_levels)
                    avg_out = sum(out_levels) / len(out_levels)
                    if avg_in < avg_out and avg_out >= LEVEL_GAMBLER:
                        flags.append('上游高层代理→下游末端账号（典型中转路径）')
                        l_score += 15

            info['launder_score'] = round(min(l_score, 100), 1)
            info['launder_flags'] = flags

            # 触发条件（三选一）：
            #   1. 多次境外转账（≥40分）
            #   2. 单次境外+其他任意特征（≥25分且≥1个flag）
            #   3. 多特征综合（≥30分且≥2个flag）
            info['is_launder_suspect'] = (
                (ov_cnt >= 3 and l_score >= 40) or
                (ov_cnt >= 1 and l_score >= 25 and len(flags) >= 1) or
                (l_score >= 30 and len(flags) >= 2)
            )

        total_launder = sum(1 for v in self.nodes.values() if v.get('is_launder_suspect'))
        print(f"  🚨 洗钱嫌疑账号: {total_launder} 个")

    def _score_suspects(self):
        """风险评分（0-100）
        维度权重：资金量30 + 层级25 + 网络中心度20 + 关键词命中15 + 夜间交易5 + 爆发性交易5
        """
        if not self.nodes:
            return

        # ── 归一化基准 ──────────────────────────────────────────────────
        # 使用 in_flow（单向流入量）作为主要规模基准，比 total_flow 更抗循环干扰
        max_in_flow  = max((v['in_flow'] for v in self.nodes.values()), default=1) or 1
        max_net_abs  = max((v.get('net_absolute', abs(v['in_flow'] - v['out_flow']))
                            for v in self.nodes.values()), default=1) or 1
        # 防御性转换：确保度数是整数值
        max_degree = max((int(v.get('in_degree', 0) or 0) + int(v.get('out_degree', 0) or 0)
                          for v in self.nodes.values()), default=1) or 1

        # ── 关键词命中：以 dict 存储，避免 pandas Series.get 的 index 类型问题 ──
        kw_hits_dict: dict = {}   # { owner_id: hit_count }
        if 'keyword_hit' in self.tx.columns:
            hit_tx = self.tx[self.tx['keyword_hit'].astype(bool)]
            for owner, cnt in hit_tx.groupby('owner_id').size().items():
                kw_hits_dict[str(owner)] = int(cnt)
        max_kw = max(kw_hits_dict.values(), default=1) or 1

        # ── 夜间交易（0-6时）：同样用 dict，key 统一为 str ──────────────
        night_dict: dict = {}
        total_dict: dict = {}
        if 'datetime_parsed' in self.tx.columns:
            valid_dt = self.tx.dropna(subset=['datetime_parsed']).copy()
            valid_dt['_hour'] = valid_dt['datetime_parsed'].dt.hour
            for owner, cnt in valid_dt[valid_dt['_hour'] <= 6].groupby('owner_id').size().items():
                night_dict[str(owner)] = int(cnt)
        for owner, cnt in self.tx.groupby('owner_id').size().items():
            total_dict[str(owner)] = int(cnt)

        # ── 爆发性交易（BURST_TIME_WINDOW 分钟内 ≥ BURST_MIN_COUNT 笔）────
        burst_nodes: set = set()
        if 'datetime_parsed' in self.tx.columns:
            for owner, grp in self.tx.groupby('owner_id'):
                grp_sorted = (grp.dropna(subset=['datetime_parsed'])
                                 .sort_values('datetime_parsed'))
                if len(grp_sorted) < BURST_MIN_COUNT:
                    continue
                times = grp_sorted['datetime_parsed'].tolist()
                for i in range(len(times) - BURST_MIN_COUNT + 1):
                    delta = (times[i + BURST_MIN_COUNT - 1] - times[i]).total_seconds() / 60
                    if delta <= BURST_TIME_WINDOW:
                        burst_nodes.add(str(owner))
                        break

        # ── 逐节点计分 ────────────────────────────────────────────────
        for node, info in self.nodes.items():
            nid = str(node)
            score = 0.0

            # 1) 资金量（30分）：流入量（60%权重）+ 净额绝对值（40%权重）
            #    净额反映实际资金影响力，流入量体现资金规模
            #    两者结合可防止 A→B→A 循环使 total_flow 虚增导致评分虚高
            in_flow_ratio  = info['in_flow'] / max_in_flow
            net_abs_ratio  = info.get('net_absolute', abs(info['in_flow'] - info['out_flow'])) / max_net_abs
            flow_score_raw = 0.6 * (in_flow_ratio ** 0.6) + 0.4 * (net_abs_ratio ** 0.6)
            score += 30 * flow_score_raw

            # 2) 层级（25分）：一级代理最高风险，赌客次之，待定外围最低
            level_score = {
                LEVEL_AGENT1:  25,   # 一级代理
                LEVEL_AGENT2:  18,   # 二级代理
                LEVEL_AGENT3:  12,   # 三级代理
                LEVEL_GAMBLER:  6,   # 赌客（也有法律风险，但比代理低）
                LEVEL_UNKNOWN:  2,   # 待定外围
            }
            score += level_score.get(info.get('level', LEVEL_UNKNOWN), 2)

            # 2b) 洗钱嫌疑加分（最多额外 +15 分）
            launder_sc = info.get('launder_score', 0)
            if launder_sc > 0:
                score += min(launder_sc * 0.15, 15)

            # 3) 网络中心度（20分）：出度+入度越大，在团伙中枢纽作用越强
            # 防御性转换：确保度数是整数值
            degree_ratio = (int(info.get('in_degree', 0) or 0) + int(info.get('out_degree', 0) or 0)) / max_degree
            score += 20 * (degree_ratio ** 0.7)

            # 4) 关键词命中（15分）：至少命中1笔即得基础分8，命中越多越接近15分
            kw = kw_hits_dict.get(nid, 0)
            if kw > 0:
                score += 8 + 7 * min(kw / max_kw, 1.0)

            # 5) 夜间交易比例（5分）
            night = night_dict.get(nid, 0)
            total = max(total_dict.get(nid, 1), 1)
            night_ratio = night / total
            score += 5 * night_ratio

            # 6) 爆发性交易（5分）
            if nid in burst_nodes:
                score += 5

            # 7) 信用额度相关特征（额外加分，最多 +15 分）
            credit_score = 0
            
            # 信用额度结算账号（高风险）
            if info.get('is_credit_settlement', False):
                credit_score += 5
            
            # 信用额度分配账号（高风险）
            if info.get('is_credit_allocator', False):
                credit_score += 4
            
            # 大额结算账号（高风险）
            if info.get('is_large_settlement', False):
                credit_score += 3
            
            # 每周信用额度注入账号（高风险）
            if info.get('has_weekly_credit_injection', False):
                credit_score += 2
            
            # 做账人员账号（中风险）
            if info.get('is_accountant', False):
                credit_score += 1
            
            # 账号多人使用（中风险）
            if info.get('is_shared_account', False):
                credit_score += 1
            
            # 单向资金流动账号（中风险）
            if info['in_flow'] > 0 and info['out_flow'] == 0:
                credit_score += 2  # 仅流入账号，可能是上游资金来源
            elif info['out_flow'] > 0 and info['in_flow'] == 0:
                credit_score += 2  # 仅流出账号，可能是下游资金去向
            
            score += min(credit_score, 15)

            self.nodes[node]['risk_score']  = min(round(score, 1), 100)
            self.nodes[node]['has_keyword'] = kw > 0
            self.nodes[node]['night_ratio'] = float(night_ratio)
            self.nodes[node]['burst']       = nid in burst_nodes

    # ── 缓存支持 ──────────────────────────────────────────────────────────
    def save_cache(self, cache_path: str, identity_file: str = None):
        """
        将分析结果序列化到文件（pickle）。
        首次分析后调用，后续查询可直接 load_cache 秒级恢复。

        identity_file: 身份对照表文件路径（保存路径而非对象本身，
                       避免 pickle 反序列化后数据丢失）
        """
        import pickle
        data = {
            'tx':               self.tx,
            'nodes':            self.nodes,
            'edges':            self.edges,
            'identity_file':    identity_file,
            'version':          3,
            'saved_at':         datetime.now().isoformat(),
        }
        with open(cache_path, 'wb') as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[OK] 分析缓存已保存：{cache_path}  "
              f"({os.path.getsize(cache_path)/1024/1024:.1f} MB)")

    @classmethod
    def load_cache(cls, cache_path: str, identity_file: str = None) -> 'GamblingAnalyzer':
        """
        从缓存文件恢复分析结果（秒级加载，无需重新读取672个Excel）。
        恢复后重新构建 NetworkX 图（极快，<1s）。

        identity_file: 身份对照表文件路径。如果提供，会从文件重新加载
                       identity（保证数据完整且最新）；如果不提供，尝试使用
                       缓存中保存的路径，都没有则 identity 为空。
        """
        import pickle, networkx as nx
        t0 = time.time()
        with open(cache_path, 'rb') as f:
            data = pickle.load(f)

        # 版本校验
        ver = data.get('version', 1)
        if ver < 2:
            raise ValueError("缓存版本过旧，请删除缓存文件重新分析。")

        # 重建分析器实例
        az = cls.__new__(cls)
        az.tx    = data['tx']
        az.nodes = data['nodes']
        az.edges = data['edges']

        # ── 身份对照表恢复 ──
        # v3+：从文件路径重新加载（保证数据完整）
        # v2 兼容：从 pickle 恢复 identity 对象
        id_file = identity_file or data.get('identity_file', '')
        if id_file and os.path.exists(id_file):
            az.identity = IdentityMapper(id_file)
        elif ver == 2 and 'identity' in data:
            az.identity = data['identity']  # v2 兼容
        else:
            az.identity = IdentityMapper()

        # 重建 NetworkX 图
        az.G = nx.DiGraph()
        for n_id, n_info in az.nodes.items():
            az.G.add_node(n_id, **n_info)
        for src, dst, amt, cnt in az.edges:
            if az.G.has_edge(src, dst):
                az.G[src][dst]['weight'] += amt
                az.G[src][dst]['count']  += cnt
            else:
                az.G.add_edge(src, dst, weight=amt, count=cnt)

        elapsed = time.time() - t0
        n_suspects = sum(1 for n in az.nodes.values() if n.get('risk_score', 0) >= 30)
        print(f"[OK] 缓存加载完成：{len(az.nodes)} 个节点，{n_suspects} 名嫌疑人  "
              f"({elapsed:.1f}s)")
        return az

    def get_suspect_df(self) -> pd.DataFrame:
        """输出嫌疑人汇总表（含身份证、手机号、微信号、银行信息、洗钱嫌疑标记）"""
        rows = []
        level_name = {
            LEVEL_AGENT1:  '一级代理（总代/庄家）',
            LEVEL_AGENT2:  '二级代理（中级代理）',
            LEVEL_AGENT3:  '三级代理（基层代理）',
            LEVEL_GAMBLER: '赌客（会员）',
            LEVEL_UNKNOWN: '待定外围人员',
        }
        for node, info in self.nodes.items():
            # ── 从身份对照表查询完整身份信息 ──────────────────────
            detail   = self.identity.get_detail(node)
            id_card  = detail.get('id_card', '') or self.identity.get_id_card(node)
            phone    = detail.get('phone', '')   or self.identity.get_phone(node)
            wechat   = detail.get('wechat', '')  or self.identity.get_wechat(node)
            bank     = detail.get('bank', '')    or self.identity.get_bank(node)

            # ── 姓名显示逻辑 ───────────────────────────────────────
            # 先用 resolve() 查询（可通过账号/手机/微信/身份证等多种 key 命中）
            resolved = self.identity.resolve(str(node).strip())
            # 若 resolve 返回值与原 node 相同（即未命中），说明身份表无记录
            # 若返回值与 node 不同（映射到了真实姓名），则显示真名
            # 特殊情况：节点 ID 本身就是真实姓名（_merge_person_nodes 合并后），
            #   此时 resolve(姓名) == 姓名，需额外判断该姓名是否真的在 identity 中登记过
            node_key = str(node).strip()
            if resolved != node_key:
                # 明确命中到了不同的值（说明有真实映射）
                display_name    = resolved
                is_unregistered = False
            elif node_key in self.identity.name_to_accounts or node_key in self.identity.detail:
                # node 本身就是身份表中的真实姓名（合并后节点 ID）
                display_name    = node_key
                is_unregistered = False
            elif node_key in self.identity.mapping:
                # mapping 里有这个 key，且 resolve 返回了原值（可能是 mapping[k]=k 的姓名自映射）
                mapped_name = self.identity.mapping[node_key]
                if mapped_name and mapped_name.lower() not in self.identity._EMPTY_PLACEHOLDERS:
                    display_name    = mapped_name
                    is_unregistered = False
                else:
                    display_name    = '⚠️ 待核查'
                    is_unregistered = True
            else:
                display_name    = '⚠️ 待核查'
                is_unregistered = True

            # ── 洗钱嫌疑 ──────────────────────────────────────────
            launder_score  = info.get('launder_score', 0)
            launder_flags  = info.get('launder_flags', [])
            is_launder     = info.get('is_launder_suspect', False)
            launder_detail = '；'.join(launder_flags) if launder_flags else '无'

            rows.append({
                '账号/ID':         node,
                '姓名':            display_name,
                '⚠️需核查':        '是' if is_unregistered else '否',
                '身份证号码':      id_card,
                '手机号':          phone,
                '微信号':          wechat,
                '所属银行':        bank,
                '层级':            level_name.get(info.get('level', LEVEL_UNKNOWN), '未知'),
                '流入金额(元)':    round(info.get('in_flow', 0), 2),
                '流出金额(元)':    round(info.get('out_flow', 0), 2),
                '净收入(元)':      round(info.get('net_flow', 0), 2),
                '总流水(元)':      round(info.get('total_flow', 0), 2),
                '上游账户数':      info.get('in_degree', 0),
                '下游账户数':      info.get('out_degree', 0),
                '关键词命中':      '是' if info.get('has_keyword') else '否',
                '夜间交易比例':    f"{info.get('night_ratio', 0):.1%}",
                '爆发性交易':      '是' if info.get('burst') else '否',
                '洗钱嫌疑':        '🚨 是' if is_launder else '否',
                '洗钱评分':        launder_score,
                '洗钱特征':        launder_detail,
                '风险评分':        info.get('risk_score', 0),
            })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df['风险评分'] = pd.to_numeric(df['风险评分'], errors='coerce').fillna(0)
        return df.sort_values('风险评分', ascending=False).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════
# 模块四：可视化引擎
# ═══════════════════════════════════════════════════════════
class Visualizer:
    def __init__(self, analyzer: GamblingAnalyzer):
        self.analyzer = analyzer
        self.G = analyzer.G
        self.nodes = analyzer.nodes

    # ─────────────────────────────────────────
    # 图一：交互式资金流向网络图
    # ─────────────────────────────────────────
    def fund_flow_network(self) -> go.Figure:
        G = self.G
        if len(G.nodes()) == 0:
            return go.Figure()

        # ── 节点过多时只取 Top 150（按总流量排序），防止布局卡死 ──
        MAX_NODES = 150
        if len(G.nodes()) > MAX_NODES:
            top_nodes = sorted(
                self.nodes.keys(),
                key=lambda n: self.nodes[n].get('total_flow', 0),
                reverse=True
            )[:MAX_NODES]
            G = G.subgraph(top_nodes).copy()

        # 布局：统一用 spring_layout（kamada_kawai 对大图 O(n^3) 太慢）
        n_nodes = len(G.nodes())
        try:
            if n_nodes <= 50:
                pos = nx.spring_layout(G, k=2.5, seed=42, iterations=30)
            elif n_nodes <= 150:
                pos = nx.spring_layout(G, k=2.0 / (n_nodes ** 0.3), seed=42, iterations=30)
            else:
                pos = nx.random_layout(G, seed=42)
        except Exception:
            pos = nx.random_layout(G, seed=42)

        level_colors = {
            LEVEL_AGENT1:  '#FF3B30',
            LEVEL_AGENT2:  '#FF9500',
            LEVEL_AGENT3:  '#FFCC00',
            LEVEL_GAMBLER: '#34C759',
            LEVEL_UNKNOWN: '#8E8E93',
        }
        level_labels = {
            LEVEL_AGENT1:  '一级代理',
            LEVEL_AGENT2:  '二级代理',
            LEVEL_AGENT3:  '三级代理',
            LEVEL_GAMBLER: '赌客',
            LEVEL_UNKNOWN: '待定外围',
        }

        # ── 画边：全部合并到 1 个 trace，用 None 分隔（比每条边一个 trace 快 100x）──
        edge_list = list(G.edges(data=True))
        max_weight = max((d.get('weight', 0) for _, _, d in edge_list), default=1) or 1

        # 按权重分成 3 档（轻/中/重），每档合并为一个 trace
        BAND_COLORS = [
            'rgba(180,180,180,0.25)',   # 轻
            'rgba(120,120,180,0.45)',   # 中
            'rgba(80,80,220,0.70)',     # 重
        ]
        BAND_WIDTHS = [0.8, 1.8, 3.0]
        band_xs  = [[], [], []]
        band_ys  = [[], [], []]

        for src, dst, data in edge_list:
            if src not in pos or dst not in pos:
                continue
            x0, y0 = pos[src]
            x1, y1 = pos[dst]
            w = data.get('weight', 0)
            band = 0 if w < max_weight * 0.2 else (1 if w < max_weight * 0.6 else 2)
            band_xs[band] += [x0, x1, None]
            band_ys[band] += [y0, y1, None]

        edge_traces = []
        band_names = ['小额边', '中等边', '大额边']
        for i in range(3):
            if not band_xs[i]:
                continue
            edge_traces.append(go.Scatter(
                x=band_xs[i], y=band_ys[i],
                mode='lines',
                name=band_names[i],
                line=dict(width=BAND_WIDTHS[i], color=BAND_COLORS[i]),
                hoverinfo='none',
                showlegend=False
            ))

        # ── 画节点（按层级分组，每层一个 trace）──
        node_traces = []
        
        for level in [LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_GAMBLER, LEVEL_UNKNOWN]:
            level_nodes = [n for n, d in self.nodes.items()
                           if d['level'] == level and n in pos]
            if not level_nodes:
                continue
            xs, ys, texts, sizes, hover_texts = [], [], [], [], []
            for node in level_nodes:
                x, y = pos[node]
                info = self.nodes[node]
                xs.append(x)
                ys.append(y)
                display_name = _smart_label(info['name'], node, max_len=8)
                # 待核查/洗钱嫌疑追加标记符号
                marker_suffix = ''
                if '待核查' in str(info.get('name', '')):
                    marker_suffix += ' ❓'
                if info.get('is_launder_suspect'):
                    marker_suffix += ' 🚨'
                texts.append(display_name + marker_suffix)
                node_size = max(10, min(50, 10 + info['total_flow'] / 50000))
                sizes.append(node_size)
                hover_texts.append(
                    f"<b>{info['name']}</b><br>"
                    f"账号: {node}<br>"
                    f"层级: {level_labels[level]}<br>"
                    f"总流水: ¥{info['total_flow']:,.0f}<br>"
                    f"流入: ¥{info['in_flow']:,.0f}<br>"
                    f"流出: ¥{info['out_flow']:,.0f}<br>"
                    f"风险评分: {info['risk_score']}<br>"
                    + (f"<b style='color:red'>🚨 洗钱嫌疑（{info.get('launder_score',0)}分）</b><br>"
                       if info.get('is_launder_suspect') else '')
                    + (f"<b style='color:orange'>❓ 身份未登记，建议核查</b>"
                       if '待核查' in str(info.get('name', '')) else '')
                )
            node_traces.append(go.Scatter(
                x=xs, y=ys,
                mode='markers+text',
                name=f"{level_labels[level]}",
                text=texts,
                textposition='top center',
                textfont=dict(size=9),
                hovertext=hover_texts,
                hoverinfo='text',
                marker=dict(
                    size=sizes,
                    color=level_colors[level],
                    line=dict(width=1.5, color='white'),
                    opacity=0.9
                )
            ))

        fig = go.Figure(data=edge_traces + node_traces)
        fig.update_layout(
            title=dict(text='<b>网赌团伙资金流向图谱</b>',
                       font=dict(size=22, color='#1a1a2e'), x=0.5),
            showlegend=True,
            hovermode='closest',
            paper_bgcolor='#f8f9fa',
            plot_bgcolor='#f8f9fa',
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            height=700,
            legend=dict(
                title='账户层级',
                bgcolor='rgba(255,255,255,0.9)',
                bordercolor='#ccc',
                borderwidth=1
            ),
            margin=dict(l=20, r=20, t=60, b=20)
        )
        return fig

    # ─────────────────────────────────────────
    # 图二：层级关系树图
    # ─────────────────────────────────────────
    def hierarchy_chart(self) -> go.Figure:
        """桑基图展示资金层级流向（只展示流量 Top-N，防止节点过多崩溃）"""
        G = self.G
        if not G.nodes():
            return go.Figure()

        # ── 只取流量最高的 Top 30 节点（Top 100 会导致浏览器白屏/卡死）──────
        MAX_SANKEY_NODES = 30
        all_node_list = list(self.nodes.items())
        if len(all_node_list) > MAX_SANKEY_NODES:
            all_node_list = sorted(
                all_node_list,
                key=lambda kv: kv[1].get('total_flow', 0),
                reverse=True
            )[:MAX_SANKEY_NODES]

        node_set  = {n for n, _ in all_node_list}
        nodes_list = [n for n, _ in all_node_list]

        node_idx = {n: i for i, n in enumerate(nodes_list)}
        level_labels = {
            LEVEL_AGENT1:  '一级代理',
            LEVEL_AGENT2:  '二级代理',
            LEVEL_AGENT3:  '三级代理',
            LEVEL_GAMBLER: '赌客',
            LEVEL_UNKNOWN: '待定外围',
        }
        level_colors_rgba = {
            LEVEL_AGENT1:  'rgba(255,59,48,0.8)',
            LEVEL_AGENT2:  'rgba(255,149,0,0.8)',
            LEVEL_AGENT3:  'rgba(255,204,0,0.8)',
            LEVEL_GAMBLER: 'rgba(52,199,89,0.8)',
            LEVEL_UNKNOWN: 'rgba(142,142,147,0.8)',
        }

        node_colors = [level_colors_rgba.get(self.nodes.get(n, {}).get('level', LEVEL_UNKNOWN), 'gray')
                       for n in nodes_list]
        node_labels = [_smart_label(self.nodes.get(n, {}).get('name', n), n, max_len=8) + '<br>'
                       + f"({level_labels.get(self.nodes.get(n, {}).get('level', LEVEL_UNKNOWN), '?')})"
                       for n in nodes_list]

        # ── 只保留两端均在 node_set 内的边，并聚合重复边 ─────────────────
        edge_agg: dict = {}   # (src_idx, dst_idx) → total_weight
        for src, dst, data in G.edges(data=True):
            if src not in node_set or dst not in node_set:
                continue
            key = (node_idx[src], node_idx[dst])
            edge_agg[key] = edge_agg.get(key, 0) + max(data.get('weight', 0), 0.01)

        if not edge_agg:
            return go.Figure()

        sources = [k[0] for k in edge_agg]
        targets = [k[1] for k in edge_agg]
        values  = list(edge_agg.values())

        fig = go.Figure(go.Sankey(
            node=dict(
                pad=15,
                thickness=20,
                line=dict(color='white', width=0.5),
                label=node_labels,
                color=node_colors,
                hovertemplate='<b>%{label}</b><br>'
                              '流入: ¥%{value:,.0f}<extra></extra>'
            ),
            link=dict(
                source=sources,
                target=targets,
                value=values,
                color='rgba(150,150,150,0.3)',
                hovertemplate='资金流向<br>金额: ¥%{value:,.0f}<extra></extra>'
            )
        ))
        fig.update_layout(
            title=dict(
                text=f'<b>团伙资金层级流向（桑基图，Top {len(nodes_list)} 节点）</b>',
                font=dict(size=22), x=0.5
            ),
            paper_bgcolor='#f8f9fa',
            height=600,
            margin=dict(l=20, r=20, t=60, b=20)
        )
        return fig

    # ─────────────────────────────────────────
    # 图三：嫌疑人风险评分排行
    # ─────────────────────────────────────────
    def suspect_ranking_chart(self, df: pd.DataFrame) -> go.Figure:
        # 防御：空 DataFrame 或缺少必要列时返回占位图
        required_cols = {'层级', '风险评分', '姓名'}
        if df.empty or not required_cols.issubset(df.columns):
            fig = go.Figure()
            fig.add_annotation(text="暂无嫌疑人数据", xref="paper", yref="paper",
                               x=0.5, y=0.5, showarrow=False, font=dict(size=16))
            fig.update_layout(height=300, paper_bgcolor='#f8f9fa', plot_bgcolor='white')
            return fig

        top_n = min(30, len(df))
        # ⚡ 改为升序：水平条形图 Plotly 从下到上渲染 y 轴，
        # 升序数据 → 最高分在最顶部（视觉正确），不需要 autorange='reversed'
        df_top = df.head(top_n).copy().sort_values('风险评分', ascending=True).reset_index(drop=True)
        level_color_map = {
            '一级代理（总代/庄家）': '#FF3B30',
            '二级代理（中级代理）': '#FF9500',
            '三级代理（基层代理）': '#FFCC00',
            '赌客（会员）': '#34C759',
            '待定外围人员': '#8E8E93',
        }
        colors = [level_color_map.get(str(l), '#999') for l in df_top['层级']]

        # 截断层级名称，只取括号前部分用于文字标注（更简洁）
        level_short = {
            '一级代理（总代/庄家）': '一级代理',
            '二级代理（中级代理）': '二级代理',
            '三级代理（基层代理）': '三级代理',
            '赌客（会员）': '赌客',
            '待定外围人员': '外围',
        }
        texts = [
            f"{s:.1f}分 | {level_short.get(str(l), str(l))}"
            for s, l in zip(df_top['风险评分'], df_top['层级'])
        ]

        fig = go.Figure(go.Bar(
            x=df_top['风险评分'],
            y=df_top['姓名'],
            orientation='h',
            marker_color=colors,
            text=texts,
            textposition='outside',
            hovertemplate=(
                '<b>%{y}</b><br>'
                '风险评分: %{x}<br>'
                '<extra></extra>'
            )
        ))
        fig.update_layout(
            title=dict(text=f'<b>嫌疑人风险评分排行（Top {top_n}）</b>',
                       font=dict(size=20), x=0.5),
            xaxis=dict(title='风险评分', range=[0, 115]),
            # 不设 autorange='reversed'，升序数据自然让最高分在顶部
            yaxis=dict(automargin=True),
            paper_bgcolor='#f8f9fa',
            plot_bgcolor='white',
            height=max(400, top_n * 36),
            margin=dict(l=150, r=130, t=60, b=40)
        )
        return fig

    # ─────────────────────────────────────────
    # 图四：时间维度分析
    # ─────────────────────────────────────────
    def time_analysis_chart(self) -> go.Figure:
        tx = self.analyzer.tx.dropna(subset=['datetime_parsed'])
        if len(tx) == 0:
            return go.Figure()

        # 大数据集采样（>5万条时随机采样2万条做时间分析，足够体现分布特征）
        MAX_SAMPLES = 20000
        if len(tx) > MAX_SAMPLES:
            tx = tx.sample(n=MAX_SAMPLES, random_state=42)

        tx = tx.copy()
        tx['hour'] = tx['datetime_parsed'].dt.hour
        tx['weekday'] = tx['datetime_parsed'].dt.dayofweek
        tx['date'] = tx['datetime_parsed'].dt.date

        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=['每小时交易频次分布', '星期交易分布',
                            '每日交易金额趋势', '关键词命中交易占比'],
            specs=[[{}, {}], [{}, {'type': 'pie'}]]
        )

        # 子图1：小时分布
        hourly = tx.groupby('hour')['amount_clean'].sum().reindex(range(24), fill_value=0)
        fig.add_trace(go.Bar(
            x=list(range(24)), y=hourly.values,
            name='小时交易量', marker_color='#007AFF',
            showlegend=False
        ), row=1, col=1)

        # 高亮夜间（0-6时）
        for h in range(7):
            fig.add_vrect(x0=h - 0.5, x1=h + 0.5, fillcolor='rgba(255,59,48,0.15)',
                          line_width=0, row=1, col=1)

        # 子图2：星期分布
        weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        weekly = tx.groupby('weekday')['amount_clean'].sum().reindex(range(7), fill_value=0)
        fig.add_trace(go.Bar(
            x=weekday_names, y=weekly.values,
            name='星期交易量', marker_color='#34C759',
            showlegend=False
        ), row=1, col=2)

        # 子图3：每日趋势
        daily = tx.groupby('date')['amount_clean'].sum().reset_index()
        daily.columns = ['date', 'amount']
        fig.add_trace(go.Scatter(
            x=daily['date'], y=daily['amount'],
            mode='lines+markers', name='每日金额',
            line=dict(color='#FF9500', width=2),
            showlegend=False
        ), row=2, col=1)

        # 子图4：关键词命中饼图
        kw_hit = tx['keyword_hit'].value_counts()
        labels = ['命中赌博关键词', '普通交易']
        values = [kw_hit.get(True, 0), kw_hit.get(False, 0)]
        fig.add_trace(go.Pie(
            labels=labels,
            values=values,
            marker_colors=['#FF3B30', '#007AFF'],
            showlegend=True
        ), row=2, col=2)

        fig.update_layout(
            title=dict(text='<b>交易时间维度分析</b>', font=dict(size=20), x=0.5),
            paper_bgcolor='#f8f9fa',
            plot_bgcolor='white',
            height=700,
            margin=dict(l=50, r=50, t=80, b=50)
        )
        return fig

    # ─────────────────────────────────────────
    # 图五：资金流入流出对比
    # ─────────────────────────────────────────
    def inout_comparison(self, df: pd.DataFrame) -> go.Figure:
        required_cols = {'姓名', '流入金额(元)', '流出金额(元)'}
        if df.empty or not required_cols.issubset(df.columns):
            fig = go.Figure()
            fig.add_annotation(text="暂无资金对比数据", xref="paper", yref="paper",
                               x=0.5, y=0.5, showarrow=False, font=dict(size=16))
            fig.update_layout(height=300, paper_bgcolor='#f8f9fa', plot_bgcolor='white')
            return fig

        top_n = min(20, len(df))
        df_top = df.head(top_n).copy()

        fig = go.Figure()
        fig.add_trace(go.Bar(
            name='流入金额',
            x=df_top['姓名'],
            y=df_top['流入金额(元)'],
            marker_color='rgba(52,199,89,0.8)',
        ))
        fig.add_trace(go.Bar(
            name='流出金额',
            x=df_top['姓名'],
            y=df_top['流出金额(元)'],
            marker_color='rgba(255,59,48,0.8)',
        ))
        fig.update_layout(
            title=dict(text='<b>Top 嫌疑人资金流入流出对比</b>', font=dict(size=20), x=0.5),
            barmode='group',
            xaxis_title='嫌疑人',
            yaxis_title='金额（元）',
            paper_bgcolor='#f8f9fa',
            plot_bgcolor='white',
            height=500,
            margin=dict(l=50, r=50, t=60, b=100)
        )
        return fig

    # ─────────────────────────────────────────
    # 图五：人员组织架构图
    # ─────────────────────────────────────────
    def org_structure_chart(self) -> go.Figure:
        """
        分层布局的人员组织架构图。
        Y 轴 = 层级（一级最高），X 轴按节点流量分散排列。
        只展示 Top-120 节点，避免过密。
        """
        G = self.G
        if not G.nodes():
            return go.Figure()

        # ── 取 Top-120 节点（按总流量）──
        MAX_ORG = 120
        top_nodes = sorted(
            self.nodes.keys(),
            key=lambda n: self.nodes[n].get('total_flow', 0),
            reverse=True
        )[:MAX_ORG]
        sub_G = G.subgraph(top_nodes).copy()

        level_y = {
            LEVEL_AGENT1:  4.0,
            LEVEL_AGENT2:  3.0,
            LEVEL_AGENT3:  2.0,
            LEVEL_GAMBLER: 1.0,
            LEVEL_UNKNOWN: 0.0,
        }
        level_colors = {
            LEVEL_AGENT1:  '#FF3B30',
            LEVEL_AGENT2:  '#FF9500',
            LEVEL_AGENT3:  '#FFCC00',
            LEVEL_GAMBLER: '#34C759',
            LEVEL_UNKNOWN: '#8E8E93',
        }
        level_labels = {
            LEVEL_AGENT1:  '一级代理',
            LEVEL_AGENT2:  '二级代理',
            LEVEL_AGENT3:  '三级代理',
            LEVEL_GAMBLER: '赌客',
            LEVEL_UNKNOWN: '待定外围',
        }

        # ── 按层级分组，X 坐标均匀分散 ──
        from collections import defaultdict
        level_groups: dict = defaultdict(list)
        for n in top_nodes:
            lv = self.nodes[n].get('level', LEVEL_UNKNOWN)
            level_groups[lv].append(n)

        pos: dict = {}
        for lv, members in level_groups.items():
            lv_y = level_y.get(lv, 0.0)
            members_sorted = sorted(members,
                                    key=lambda n: self.nodes[n].get('total_flow', 0),
                                    reverse=True)
            n_m = len(members_sorted)
            for i, node in enumerate(members_sorted):
                x = (i + 0.5) / n_m if n_m > 1 else 0.5
                pos[node] = (x, lv_y)

        # ── 画边（合并为单 trace）──
        ex, ey = [], []
        for src, dst in sub_G.edges():
            if src in pos and dst in pos:
                x0, y0 = pos[src]
                x1, y1 = pos[dst]
                ex += [x0, x1, None]
                ey += [y0, y1, None]

        traces = []
        if ex:
            traces.append(go.Scatter(
                x=ex, y=ey, mode='lines',
                line=dict(color='rgba(120,120,180,0.35)', width=1.2),
                hoverinfo='none', showlegend=False
            ))


        # ── 画节点（按层级分组）──
        for lv in [LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_GAMBLER, LEVEL_UNKNOWN]:
            members = level_groups.get(lv, [])
            if not members:
                continue
            xs, ys, texts, sizes, hovers = [], [], [], [], []
            for node in members:
                if node not in pos:
                    continue
                x, y = pos[node]
                info = self.nodes[node]
                xs.append(x)
                ys.append(y)
                name = info.get('name', node)
                short = _smart_label(name, node, max_len=8)
                # 附加标记符号
                suffix = ''
                if '待核查' in str(name):
                    suffix += ' ❓'
                if info.get('is_launder_suspect'):
                    suffix += ' 🚨'
                texts.append(short + suffix)
                sz = max(12, min(45, 12 + info['total_flow'] / 80000))
                sizes.append(sz)
                hovers.append(
                    f"<b>{name}</b><br>"
                    f"账号: {node}<br>"
                    f"层级: {level_labels[lv]}<br>"
                    f"总流水: ¥{info['total_flow']:,.0f}<br>"
                    f"流入: ¥{info['in_flow']:,.0f} / 流出: ¥{info['out_flow']:,.0f}<br>"
                    # 防御性转换：确保度数是整数值
                    f"上游账户: {int(info.get('in_degree', 0) or 0)} 个 / 下游账户: {int(info.get('out_degree', 0) or 0)} 个<br>"
                    f"风险评分: {info['risk_score']}"
                    + (f"<br><b style='color:red'>🚨 洗钱嫌疑（{info.get('launder_score',0)}分）</b>"
                       if info.get('is_launder_suspect') else '')
                    + (f"<br><b style='color:orange'>❓ 身份未登记，建议核查</b>"
                       if '待核查' in str(name) else '')
                )
            traces.append(go.Scatter(
                x=xs, y=ys, mode='markers+text',
                name=f"{level_labels[lv]} ({len(members)}人)",
                text=texts, textposition='top center',
                textfont=dict(size=8),
                hovertext=hovers, hoverinfo='text',
                marker=dict(
                    size=sizes, color=level_colors[lv],
                    line=dict(width=1.5, color='white'), opacity=0.92
                )
            ))

        fig = go.Figure(data=traces)
        fig.update_layout(
            title=dict(text='<b>团伙人员组织架构图（赌球团伙金字塔）</b>',
                       font=dict(size=20, color='#1a1a2e'), x=0.5),
            showlegend=True,
            hovermode='closest',
            paper_bgcolor='#f8f9fa',
            plot_bgcolor='#f8f9fa',
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False,
                       range=[-0.02, 1.02]),
            yaxis=dict(
                showgrid=True, gridcolor='rgba(200,200,200,0.3)',
                zeroline=False, showticklabels=True,
                tickvals=[4.0, 3.0, 2.0, 1.0, 0.0],
                ticktext=['一级代理（总代/庄家）', '二级代理（中级代理）',
                          '三级代理（基层代理）', '赌客（会员）', '待定外围'],
                tickfont=dict(size=11, color='#555')
            ),
            height=720,
            legend=dict(title='层级', bgcolor='rgba(255,255,255,0.9)',
                        bordercolor='#ccc', borderwidth=1),
            margin=dict(l=160, r=20, t=60, b=20)
        )
        return fig

    # ─────────────────────────────────────────
    # 重点人物 & 关键关系分析
    # ─────────────────────────────────────────
    def key_persons_analysis(self) -> dict:
        """
        返回侦查重点关注的人物和关系，包含：
        - top_hubs:       资金枢纽（总流水最高）
        - top_brokers:    中介节点（中介中心度，即连接上下游最多的节点）
        - top_pairs:      高频/大额关系对（两个节点间总转账金额最大）
        - loops:          疑似循环转账（A→B→C→A，洗钱闭环）
        - burst_persons:  爆发性交易人员
        - night_persons:  高夜间交易比例人员
        """
        G = self.G
        result: dict = {}

        # ── 1. 资金枢纽 Top10 ──────────────────────────────────────────
        hubs = sorted(
            self.nodes.items(),
            key=lambda kv: kv[1].get('total_flow', 0),
            reverse=True
        )[:10]
        result['top_hubs'] = [
            {
                'id': nid,
                'name': info.get('name', nid),
                'level': info.get('level', 4),
                'total_flow': info.get('total_flow', 0),
                'in_flow': info.get('in_flow', 0),
                'out_flow': info.get('out_flow', 0),
                'risk_score': info.get('risk_score', 0),
            }
            for nid, info in hubs
        ]

        # ── 2. 中介中心度 Top10（只在 ≤2000 节点图上跑，超大图用度数代替）──
        n_nodes = len(G.nodes())
        if n_nodes <= 2000:
            try:
                bc = nx.betweenness_centrality(G, normalized=True, weight='weight',
                                               k=min(200, n_nodes))
            except Exception:
                # 防御性转换：确保度数是整数值
                bc = {n: (int(self.nodes[n].get('in_degree', 0) or 0) + int(self.nodes[n].get('out_degree', 0) or 0))
                      for n in self.nodes}
        else:
            # 超大图：用加权度数估算
            # 防御性转换：确保度数是整数值
            bc = {n: int(self.nodes[n].get('in_degree', 0) or 0) + int(self.nodes[n].get('out_degree', 0) or 0)
                  for n in self.nodes}

        top_brokers_ids = sorted(bc, key=bc.get, reverse=True)[:10]
        result['top_brokers'] = [
            {
                'id': nid,
                'name': self.nodes.get(nid, {}).get('name', nid),
                'level': self.nodes.get(nid, {}).get('level', 4),
                'betweenness': round(bc.get(nid, 0), 6),
                'total_flow': self.nodes.get(nid, {}).get('total_flow', 0),
                'risk_score': self.nodes.get(nid, {}).get('risk_score', 0),
            }
            for nid in top_brokers_ids
            if nid in self.nodes
        ]

        # ── 3. 高额关系对 Top15（两账户间累计转账金额）──────────────────
        edge_sums: dict = {}
        for src, dst, data in G.edges(data=True):
            key = tuple(sorted([src, dst]))
            edge_sums[key] = edge_sums.get(key, 0) + data.get('weight', 0)

        top_pairs_raw = sorted(edge_sums.items(), key=lambda x: x[1], reverse=True)[:15]
        result['top_pairs'] = [
            {
                'a_id': pair[0], 'a_name': self.nodes.get(pair[0], {}).get('name', pair[0]),
                'a_level': self.nodes.get(pair[0], {}).get('level', 4),
                'b_id': pair[1], 'b_name': self.nodes.get(pair[1], {}).get('name', pair[1]),
                'b_level': self.nodes.get(pair[1], {}).get('level', 4),
                'total_amount': amount,
                # 是否双向转账（互转）
                'is_mutual': G.has_edge(pair[1], pair[0]) and G.has_edge(pair[0], pair[1]),
            }
            for pair, amount in top_pairs_raw
            if pair[0] in self.nodes and pair[1] in self.nodes
        ]

        # ── 4. 闭环转账（疑似洗钱链条，长度 2-4）────────────────────────
        loops = []
        # 只在 Top-50 高流量节点子图上找环（从80降到50，大幅加速）
        top_cycle = sorted(self.nodes.keys(),
                       key=lambda n: self.nodes[n].get('total_flow', 0),
                       reverse=True)[:50]
        sub = G.subgraph(top_cycle).copy()
        try:
            import sys
            import threading
            _old_limit = sys.getrecursionlimit()
            sys.setrecursionlimit(5000)

            seen_loops: set = set()
            cycle_timeout = 15  # 最多等15秒
            cycle_result = {'loops': []}

            def _find_cycles():
                try:
                    _find_cycles.timeout = False
                    try:
                        cycle_iter = nx.simple_cycles(sub, length_bound=4)
                    except TypeError:
                        cycle_iter = nx.simple_cycles(sub)
                    for cycle in cycle_iter:
                        if _find_cycles.timeout:
                            break
                        if 2 <= len(cycle) <= 4:
                            key = tuple(sorted(cycle))
                            if key not in seen_loops:
                                seen_loops.add(key)
                                total = sum(
                                    G[cycle[i]][cycle[(i + 1) % len(cycle)]].get('weight', 0)
                                    for i in range(len(cycle))
                                    if G.has_edge(cycle[i], cycle[(i + 1) % len(cycle)])
                                )
                                cycle_result['loops'].append({
                                    'chain': [self.nodes.get(n, {}).get('name', n) for n in cycle],
                                    'chain_ids': cycle,
                                    'length': len(cycle),
                                    'total_amount': total,
                                })
                                if len(cycle_result['loops']) >= 100:
                                    break
                except Exception:
                    pass

            _find_cycles.timeout = False
            thread = threading.Thread(target=_find_cycles, daemon=True)
            thread.start()
            thread.join(timeout=cycle_timeout)
            if thread.is_alive():
                _find_cycles.timeout = True
                print("     [!] 闭环检测超时，已截断（部分结果可能不完整）")
            sys.setrecursionlimit(_old_limit)

            loops = cycle_result.get('loops', [])
            loops.sort(key=lambda x: x['total_amount'], reverse=True)
            loops = loops[:10]
        except Exception:
            loops = []
        result['loops'] = loops

        # ── 5. 爆发性交易人员 ────────────────────────────────────────────
        result['burst_persons'] = [
            {
                'id': nid,
                'name': info.get('name', nid),
                'level': info.get('level', 4),
                'risk_score': info.get('risk_score', 0),
                'total_flow': info.get('total_flow', 0),
            }
            for nid, info in self.nodes.items()
            if info.get('burst', False)
        ]
        result['burst_persons'].sort(key=lambda x: x['risk_score'], reverse=True)

        # ── 6. 高夜间交易比例（夜间比 >= 40%）─────────────────────────────
        result['night_persons'] = sorted(
            [
                {
                    'id': nid,
                    'name': info.get('name', nid),
                    'level': info.get('level', 4),
                    'night_ratio': info.get('night_ratio', 0),
                    'risk_score': info.get('risk_score', 0),
                }
                for nid, info in self.nodes.items()
                if info.get('night_ratio', 0) >= 0.4
            ],
            key=lambda x: x['night_ratio'],
            reverse=True
        )[:15]

        return result

    # ─────────────────────────────────────────
    # 图七：单人中心关系网络
    # ─────────────────────────────────────────
    def single_person_network(self, person_id: str, depth: int = 1) -> go.Figure:
        """
        以指定账号/姓名为中心，展开其 depth 跳以内的关系圈。
        depth=1：只展示直接上下游
        depth=2：展示到二级关系

        person_id 可以是图中的节点 ID（账号或姓名），也可以是 IdentityMapper 能识别的姓名。
        返回一个 Plotly Figure。
        """
        G = self.G
        # 先尝试精确匹配节点
        if person_id not in G.nodes():
            # 尝试在节点中按 name 字段搜索
            matched = [nid for nid, info in self.nodes.items()
                       if str(info.get('name', '')).strip() == person_id.strip()]
            if matched:
                person_id = matched[0]
            else:
                # 返回空图提示
                fig = go.Figure()
                fig.update_layout(
                    title=f'未找到节点：{person_id}',
                    template='plotly_dark',
                    paper_bgcolor='#0d1117',
                    plot_bgcolor='#0d1117',
                )
                return fig

        # BFS 收集 depth 跳内的节点
        visited = {person_id}
        frontier = {person_id}
        for _ in range(depth):
            next_frontier = set()
            for nid in frontier:
                next_frontier |= set(G.predecessors(nid)) | set(G.successors(nid))
            next_frontier -= visited
            visited |= next_frontier
            frontier = next_frontier

        sub_G = G.subgraph(visited).copy()

        # 布局：统一用 spring_layout
        n_nodes = len(sub_G.nodes())
        if n_nodes == 0:
            return go.Figure()
        try:
            pos = nx.spring_layout(sub_G, k=2.0 / (n_nodes ** 0.3), seed=42, iterations=30)
        except Exception:
            pos = nx.random_layout(sub_G, seed=42)

        # 颜色映射
        level_colors = {
            LEVEL_AGENT1:  '#ff3b30',
            LEVEL_AGENT2:  '#ff9500',
            LEVEL_AGENT3:  '#ffcc00',
            LEVEL_GAMBLER: '#34c759',
            LEVEL_UNKNOWN: '#8e8e93',
        }
        level_names = {
            LEVEL_AGENT1:  '一级代理',
            LEVEL_AGENT2:  '二级代理',
            LEVEL_AGENT3:  '三级代理',
            LEVEL_GAMBLER: '赌客',
            LEVEL_UNKNOWN: '待定外围',
        }

        # 边
        edge_x, edge_y = [], []
        for src, dst in sub_G.edges():
            if src in pos and dst in pos:
                x0, y0 = pos[src]
                x1, y1 = pos[dst]
                edge_x += [x0, x1, None]
                edge_y += [y0, y1, None]

        edge_trace = go.Scatter(
            x=edge_x, y=edge_y,
            line=dict(width=1.2, color='rgba(100,120,150,0.5)'),
            hoverinfo='none', mode='lines',
        )

        # 节点
        node_traces = []
        for lv, color in level_colors.items():
            lv_nodes = [n for n in sub_G.nodes()
                        if self.nodes.get(n, {}).get('level', LEVEL_UNKNOWN) == lv]
            if not lv_nodes:
                continue
            nx_list = [pos[n][0] for n in lv_nodes]
            ny_list = [pos[n][1] for n in lv_nodes]
            sizes = [max(12, min(40, self.nodes.get(n, {}).get('total_flow', 0) / 50000 + 12))
                     for n in lv_nodes]
            # 中心节点特殊高亮
            border_colors = ['#ffffff' if n == person_id else color for n in lv_nodes]
            border_widths = [4 if n == person_id else 1.5 for n in lv_nodes]
            labels = [_smart_label(self.nodes.get(n, {}).get('name', n), n) for n in lv_nodes]
            hover_texts = []
            for n in lv_nodes:
                info = self.nodes.get(n, {})
                center_mark = '⭐ 中心节点  ' if n == person_id else ''
                tags = self.analyzer.identity.get_tags(n) if hasattr(self.analyzer.identity, 'custom_tags') else []
                tag_str = ('  🏷️ ' + ' '.join(f'[{t}]' for t in tags)) if tags else ''
                hover_texts.append(
                    f"{center_mark}<b>{info.get('name', n)}</b><br>"
                    f"层级：{level_names.get(lv, '?')}{tag_str}<br>"
                    f"总流水：¥{info.get('total_flow', 0)/10000:.1f}万<br>"
                    f"流入：¥{info.get('in_flow', 0)/10000:.1f}万  "
                    f"流出：¥{info.get('out_flow', 0)/10000:.1f}万<br>"
                    f"风险评分：{info.get('risk_score', 0)}"
                )
            node_traces.append(go.Scatter(
                x=nx_list, y=ny_list,
                mode='markers+text',
                marker=dict(
                    size=sizes, color=color,
                    line=dict(color=border_colors, width=border_widths),
                    opacity=0.9,
                ),
                text=labels,
                textposition='top center',
                textfont=dict(size=10, color='#e6edf3'),
                hovertext=hover_texts,
                hoverinfo='text',
                name=level_names.get(lv, '?'),
            ))

        center_info = self.nodes.get(person_id, {})
        center_name = center_info.get('name', person_id)
        fig = go.Figure(data=[edge_trace] + node_traces)
        fig.update_layout(
            title=dict(
                text=f'👤 单人关系网络：{center_name}（{depth}跳以内，共 {n_nodes} 人）',
                font=dict(size=16, color='#e6edf3'),
                x=0.5,
            ),
            showlegend=True,
            hovermode='closest',
            template='plotly_dark',
            paper_bgcolor='#0d1117',
            plot_bgcolor='#0d1117',
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            margin=dict(l=20, r=20, t=60, b=20),
            height=600,
            legend=dict(
                bgcolor='rgba(22,27,34,0.9)',
                bordercolor='#30363d',
                borderwidth=1,
                font=dict(color='#c9d1d9'),
            ),
        )
        return fig

    # ─────────────────────────────────────────
    # 图八：社团扩展分析
    # ─────────────────────────────────────────
    def community_expansion(self, seed_ids: list = None) -> tuple:
        """
        给定少数种子账号/姓名，找出与其属于同一社团的其他成员。
        若未指定 seed_ids，则自动对全图做社区划分，找出最大的 Top-5 社区。

        返回 (fig, community_list)：
          - fig: Plotly Figure，每个社区用不同颜色展示
          - community_list: list of dict，每个社区的成员详情
        """
        G = self.G
        if len(G.nodes()) == 0:
            return go.Figure(), []

        # 转成无向图做社区发现
        UG = G.to_undirected()

        # 如果图太大，先取 Top 300 节点（300足够展示核心社团结构，500太慢）
        MAX_COMM_NODES = 300
        if len(UG.nodes()) > MAX_COMM_NODES:
            top_nodes = sorted(
                self.nodes.keys(),
                key=lambda n: self.nodes[n].get('total_flow', 0),
                reverse=True
            )[:MAX_COMM_NODES]
            UG = UG.subgraph(top_nodes).copy()

        # 社区发现（带超时保护）
        try:
            import signal
            from networkx.algorithms import community as nx_comm
            try:
                communities_gen = nx_comm.greedy_modularity_communities(UG, weight='weight', resolution=1.0)
                all_communities = [set(c) for c in communities_gen]
            except Exception:
                # 退化方案：连通分量
                all_communities = [set(c) for c in nx.connected_components(UG)]
        except Exception:
            all_communities = [set(c) for c in nx.connected_components(UG)]

        # 按社区大小排序
        all_communities.sort(key=len, reverse=True)

        # 若指定了种子，只保留种子所在社区
        if seed_ids:
            # 先把种子ID解析成图中节点
            resolved_seeds = set()
            for sid in seed_ids:
                if sid in G.nodes():
                    resolved_seeds.add(sid)
                else:
                    matched = [nid for nid, info in self.nodes.items()
                               if str(info.get('name', '')).strip() == sid.strip()]
                    resolved_seeds.update(matched)
            # 找包含任意种子的社区
            selected_communities = [c for c in all_communities
                                     if c & resolved_seeds]
            if not selected_communities:
                selected_communities = all_communities[:5]
        else:
            selected_communities = all_communities[:5]

        # 最多展示 5 个社区，每个社区最多 50 人（防止过密）
        selected_communities = [c for c in selected_communities[:5]]

        # 布局：统一用 spring_layout（比 kamada_kawai 快 3-5x）
        all_nodes_in_plot = set()
        for comm in selected_communities:
            all_nodes_in_plot |= comm
        sub_G = UG.subgraph(all_nodes_in_plot).copy()

        n_plot = len(sub_G.nodes())
        try:
            pos = nx.spring_layout(sub_G, k=1.5 / (n_plot ** 0.5), seed=42, iterations=25)
        except Exception:
            pos = nx.random_layout(sub_G, seed=42)

        # 社区颜色
        comm_colors = ['#58a6ff', '#ff9500', '#34d399', '#f87171', '#a78bfa',
                       '#fbbf24', '#22d3ee', '#fb7185', '#86efac', '#c4b5fd']

        level_names = {
            LEVEL_AGENT1:  '一级代理',
            LEVEL_AGENT2:  '二级代理',
            LEVEL_AGENT3:  '三级代理',
            LEVEL_GAMBLER: '赌客',
            LEVEL_UNKNOWN: '待定外围',
        }

        traces = []
        # 先画边
        edge_x, edge_y = [], []
        for src, dst in sub_G.edges():
            if src in pos and dst in pos:
                x0, y0 = pos[src]
                x1, y1 = pos[dst]
                edge_x += [x0, x1, None]
                edge_y += [y0, y1, None]
        traces.append(go.Scatter(
            x=edge_x, y=edge_y,
            line=dict(width=0.8, color='rgba(80,100,120,0.4)'),
            hoverinfo='none', mode='lines', showlegend=False,
        ))

        # 每个社区一个 trace
        community_list = []
        for ci, comm in enumerate(selected_communities):
            color = comm_colors[ci % len(comm_colors)]
            comm_nodes = [n for n in comm if n in pos]
            if not comm_nodes:
                continue

            # 种子节点高亮
            resolved_seeds = set()
            if seed_ids:
                for sid in seed_ids:
                    if sid in G.nodes():
                        resolved_seeds.add(sid)
                    else:
                        matched = [nid for nid, info in self.nodes.items()
                                   if str(info.get('name', '')).strip() == sid.strip()]
                        resolved_seeds.update(matched)

            nx_list = [pos[n][0] for n in comm_nodes]
            ny_list = [pos[n][1] for n in comm_nodes]
            sizes = [max(10, min(35, self.nodes.get(n, {}).get('total_flow', 0) / 50000 + 10))
                     for n in comm_nodes]
            border_ws = [3 if n in resolved_seeds else 1 for n in comm_nodes]
            border_cs = ['#ffffff' if n in resolved_seeds else color for n in comm_nodes]
            labels = [_smart_label(self.nodes.get(n, {}).get('name', n), n, 8) for n in comm_nodes]
            hover_texts = []
            for n in comm_nodes:
                info = self.nodes.get(n, {})
                seed_mark = '🌱 种子节点  ' if n in resolved_seeds else ''
                tags = self.analyzer.identity.get_tags(n) if hasattr(self.analyzer.identity, 'custom_tags') else []
                tag_str = ('  🏷️ ' + ' '.join(f'[{t}]' for t in tags)) if tags else ''
                hover_texts.append(
                    f"{seed_mark}<b>{info.get('name', n)}</b>{tag_str}<br>"
                    f"层级：{level_names.get(info.get('level', LEVEL_UNKNOWN), '?')}<br>"
                    f"总流水：¥{info.get('total_flow', 0)/10000:.1f}万<br>"
                    f"风险评分：{info.get('risk_score', 0)}"
                )
            traces.append(go.Scatter(
                x=nx_list, y=ny_list,
                mode='markers+text',
                marker=dict(
                    size=sizes, color=color,
                    line=dict(color=border_cs, width=border_ws),
                    opacity=0.85,
                ),
                text=labels,
                textposition='top center',
                textfont=dict(size=9, color='#e6edf3'),
                hovertext=hover_texts,
                hoverinfo='text',
                name=f'社团 {ci+1}（{len(comm)}人）',
            ))

            # 社区成员统计
            members = sorted(
                [{'id': n,
                  'name': self.nodes.get(n, {}).get('name', n),
                  'level': self.nodes.get(n, {}).get('level', LEVEL_UNKNOWN),
                  'level_name': level_names.get(self.nodes.get(n, {}).get('level', LEVEL_UNKNOWN), '?'),
                  'total_flow': self.nodes.get(n, {}).get('total_flow', 0),
                  'risk_score': self.nodes.get(n, {}).get('risk_score', 0),
                  'is_seed': n in resolved_seeds,
                  'tags': self.analyzer.identity.get_tags(n) if hasattr(self.analyzer.identity, 'custom_tags') else [],
                  } for n in comm],
                key=lambda x: x['risk_score'],
                reverse=True
            )
            community_list.append({
                'id': ci + 1,
                'color': color,
                'size': len(comm),
                'members': members,
            })

        fig = go.Figure(data=traces)
        seed_title = f'（种子：{", ".join(seed_ids)}）' if seed_ids else '（全图自动划分）'
        fig.update_layout(
            title=dict(
                text=f'🔍 社团扩展分析 {seed_title}',
                font=dict(size=16, color='#e6edf3'),
                x=0.5,
            ),
            showlegend=True,
            hovermode='closest',
            template='plotly_dark',
            paper_bgcolor='#0d1117',
            plot_bgcolor='#0d1117',
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            margin=dict(l=20, r=20, t=60, b=20),
            height=640,
            legend=dict(
                bgcolor='rgba(22,27,34,0.9)',
                bordercolor='#30363d',
                borderwidth=1,
                font=dict(color='#c9d1d9'),
            ),
        )
        return fig, community_list

    # ─────────────────────────────────────────
    # 图九：交易频率分析
    # ─────────────────────────────────────────
    def transaction_frequency_analysis(self) -> go.Figure:
        """
        按月/周/日维度分析交易频次趋势，识别突然高频的异常账户。

        生成一个含两个子图的交互式图表：
          - 上方：全量交易按月频次折线图
          - 下方：Top10 账户的月度交易频次堆叠面积图
        """
        tx = self.analyzer.tx
        if 'datetime_parsed' not in tx.columns:
            return go.Figure().update_layout(
                title=dict(text='交易频率分析（缺少时间数据）', font=dict(color='#8b949e')),
                template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117')

        valid = tx.dropna(subset=['datetime_parsed']).copy()
        if valid.empty:
            return go.Figure().update_layout(
                title=dict(text='交易频率分析（无有效时间数据）', font=dict(color='#8b949e')),
                template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117')

        valid['month'] = valid['datetime_parsed'].dt.to_period('M').astype(str)

        # 上方子图：全量月度交易频次
        monthly_all = valid.groupby('month').size().reset_index(name='count')

        # 下方子图：Top10 账户月度交易频次
        top_accounts = valid['owner_id'].value_counts().head(10).index.tolist()
        valid_top = valid[valid['owner_id'].isin(top_accounts)]
        monthly_top = valid_top.groupby(['month', 'owner_id']).size().reset_index(name='count')
        # 按总量排序确定颜色分配
        account_totals = valid_top.groupby('owner_id').size().sort_values(ascending=False)

        colors = ['#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff', '#ff922b',
                  '#cc5de8', '#20c997', '#f06595', '#845ef7', '#339af0']

        fig = make_subplots(
            rows=2, cols=1, row_heights=[0.35, 0.65],
            subplot_titles=['全量月度交易频次', 'Top10 账户月度交易频次'],
            vertical_spacing=0.08)

        # 全量折线图
        fig.add_trace(go.Scatter(
            x=monthly_all['month'], y=monthly_all['count'],
            mode='lines+markers', name='全量交易',
            line=dict(color='#58a6ff', width=2.5),
            marker=dict(size=5, color='#58a6ff'),
            fill='tozeroy', fillcolor='rgba(88,166,255,0.08)',
            hovertemplate='%{x}<br>交易笔数: %{y:,}<extra></extra>'
        ), row=1, col=1)

        # Top10 面积图
        for i, acc in enumerate(account_totals.index[:10]):
            sub = monthly_top[monthly_top['owner_id'] == acc]
            label = self.analyzer.identity.resolve(acc)
            if len(label) > 10:
                label = label[:8] + '..'
            fig.add_trace(go.Scatter(
                x=sub['month'], y=sub['count'],
                mode='lines', name=label,
                line=dict(width=1.2, color=colors[i % len(colors)]),
                stackgroup='one',
                hovertemplate='%{x}<br>%{fullData.name}: %{y:,} 笔<extra></extra>'
            ), row=2, col=1)

        fig.update_layout(
            title=dict(text='交易频率趋势分析', font=dict(size=16, color='#e6edf3')),
            template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117',
            height=720, margin=dict(l=60, r=30, t=60, b=40),
            legend=dict(font=dict(size=10, color='#8b949e'), bgcolor='rgba(22,27,34,0.9)'),
            xaxis=dict(tickangle=-45, gridcolor='#21262d'),
            xaxis2=dict(tickangle=-45, gridcolor='#21262d'),
            yaxis=dict(gridcolor='#21262d', title='笔数'),
            yaxis2=dict(gridcolor='#21262d', title='笔数'),
        )
        return fig

    # ─────────────────────────────────────────
    # 图十：共同账户分析
    # ─────────────────────────────────────────
    def common_upstream_analysis(self, min_common: int = 2) -> tuple:
        """
        发现"多个人的上游完全相同"的账户群。

        逻辑：
          1. 取每个节点的上游集合（入边来源）
          2. 将上游集合相同的节点归为一组（同组 = 同一个代理的下线）
          3. 只保留组大小 >= min_common 的群组

        返回 (fig, groups)：
          - fig: Plotly Figure（水平条形图，每组一个条）
          - groups: list of dict，每组包含成员列表、共同上游列表
        """
        G = self.analyzer.G
        if len(G.nodes()) == 0:
            return go.Figure(), []

        # 计算每个节点的上游集合
        upstream_map = {}
        for node in G.nodes():
            upstreams = frozenset(str(p) for p in G.predecessors(node))
            if upstreams:
                upstream_map[node] = upstreams

        # 按上游集合分组
        from collections import defaultdict
        groups_raw = defaultdict(list)
        for node, upstreams in upstream_map.items():
            groups_raw[upstreams].append(node)

        # 过滤：组大小 >= min_common
        groups = []
        for upstreams, members in groups_raw.items():
            if len(members) >= min_common:
                groups.append({
                    'upstreams': list(upstreams),
                    'upstream_names': [self.analyzer.identity.resolve(u) for u in upstreams],
                    'members': members,
                    'member_names': [self.analyzer.identity.resolve(m) for m in members],
                    'size': len(members),
                    'total_flow': sum(
                        self.analyzer.nodes.get(m, {}).get('total_flow', 0) for m in members),
                })

        # 按组大小降序排列，取 Top 20
        groups.sort(key=lambda g: g['size'], reverse=True)
        groups = groups[:20]

        if not groups:
            return go.Figure(), []

        # 构建条形图
        labels = []
        counts = []
        flow_vals = []
        for i, g in enumerate(groups):
            # 组标签：显示成员数 + 代表性名称
            member_display = ', '.join(g['member_names'][:3])
            if g['size'] > 3:
                member_display += f' 等{g["size"]}人'
            labels.append(member_display)
            counts.append(g['size'])
            flow_vals.append(g['total_flow'] / 10000)

        fig = make_subplots(
            rows=1, cols=2, column_widths=[0.6, 0.4],
            subplot_titles=['共同资金来源群组（人数）', '群组总流水（万元）'])

        fig.add_trace(go.Bar(
            y=labels[::-1], x=counts[::-1],
            orientation='h', name='成员数',
            marker=dict(color='#ff6b6b', opacity=0.8, line=dict(color='#ff6b6b', width=0.5)),
            hovertemplate='%{y}<br>成员数: %{x}<extra></extra>'
        ), row=1, col=1)

        fig.add_trace(go.Bar(
            y=labels[::-1], x=flow_vals[::-1],
            orientation='h', name='总流水(万)',
            marker=dict(color='#4d96ff', opacity=0.8, line=dict(color='#4d96ff', width=0.5)),
            hovertemplate='%{y}<br>总流水: ¥%{x:.1f}万<extra></extra>'
        ), row=1, col=2)

        fig.update_layout(
            title=dict(text='共同资金来源分析（疑似同代理下线）', font=dict(size=16, color='#e6edf3')),
            template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117',
            height=max(500, len(groups) * 28),
            margin=dict(l=200, r=30, t=60, b=40),
            showlegend=False,
            xaxis=dict(gridcolor='#21262d', title='人数'),
            xaxis2=dict(gridcolor='#21262d', title='万元'),
        )
        return fig, groups

    # ─────────────────────────────────────────
    # 图十一：资金穿透追踪
    # ─────────────────────────────────────────
    def fund_penetration_trace(self, target_id: str = None,
                                max_depth: int = 5,
                                top_n: int = 5) -> go.Figure:
        """
        从目标账户出发，递归展开资金链路直到终端。
        若未指定 target_id，自动取 Top-N 高风险嫌疑人作为种子。

        生成 Sankey 图，展示资金从源头（上游代理）经过中间层到终端（赌客）的完整路径。
        """
        G = self.analyzer.G
        if len(G.nodes()) == 0:
            return go.Figure()

        # 自动选择种子
        if target_id is None:
            sorted_nodes = sorted(
                self.analyzer.nodes.items(),
                key=lambda x: x[1].get('risk_score', 0), reverse=True)
            seeds = [nid for nid, _ in sorted_nodes[:top_n] if nid in G]
        else:
            seeds = [target_id] if target_id in G else []

        if not seeds:
            return go.Figure().update_layout(
                title=dict(text='资金穿透追踪（无有效种子节点）', font=dict(color='#8b949e')),
                template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117')

        # BFS 逐层展开，收集所有路径
        visited_nodes = set()
        node_links = []  # (src, dst, amount, depth)

        for seed in seeds:
            queue = [(seed, 0)]  # (node, depth)
            while queue:
                current, depth = queue.pop(0)
                if current in visited_nodes or depth > max_depth:
                    continue
                visited_nodes.add(current)

                # 上游方向（谁给我转钱）
                for pred in G.predecessors(current):
                    amt = G[pred][current].get('weight', 0)
                    node_links.append((pred, current, amt, depth))
                    if pred not in visited_nodes:
                        queue.append((pred, depth + 1))

                # 下游方向（我给谁转钱）
                for succ in G.successors(current):
                    amt = G[current][succ].get('weight', 0)
                    node_links.append((current, succ, amt, depth))
                    if succ not in visited_nodes:
                        queue.append((succ, depth + 1))

        if not node_links:
            return go.Figure().update_layout(
                title=dict(text='资金穿透追踪（未发现关联路径）', font=dict(color='#8b949e')),
                template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117')

        # 构建 Sankey
        all_nodes_set = set()
        for src, dst, amt, depth in node_links:
            all_nodes_set.add(src)
            all_nodes_set.add(dst)

        all_nodes_list = list(all_nodes_set)
        node_index = {n: i for i, n in enumerate(all_nodes_list)}

        # 节点标签
        node_labels = []
        node_colors = []
        for n in all_nodes_list:
            label = self.analyzer.identity.resolve(n)
            if len(label) > 15:
                label = label[:12] + '..'
            node_labels.append(label)
            # 颜色按层级
            lv = self.analyzer.nodes.get(n, {}).get('level', LEVEL_UNKNOWN)
            lv_color = {LEVEL_AGENT1: '#ff6b6b', LEVEL_AGENT2: '#ffd93d',
                        LEVEL_AGENT3: '#6bcb77', LEVEL_GAMBLER: '#8b949e',
                        LEVEL_UNKNOWN: '#484f58'}.get(lv, '#484f58')
            node_colors.append(lv_color)

        sources = [node_links[i][0] for i in range(len(node_links))]
        targets = [node_links[i][1] for i in range(len(node_links))]
        values = [node_links[i][2] for i in range(len(node_links))]
        link_colors = ['rgba(88,166,255,0.35)'] * len(node_links)

        fig = go.Figure(go.Sankey(
            arrangement='snap',
            node=dict(
                label=node_labels,
                color=node_colors,
                pad=12,
                thickness=18,
            ),
            link=dict(
                source=[node_index[s] for s in sources],
                target=[node_index[t] for t in targets],
                value=values,
                color=link_colors,
            )
        ))

        seed_names = ', '.join(
            self.analyzer.identity.resolve(s) for s in seeds[:3])
        if len(seeds) > 3:
            seed_names += f' 等{len(seeds)}人'

        fig.update_layout(
            title=dict(text=f'资金穿透追踪 - {seed_names}', font=dict(size=16, color='#e6edf3')),
            template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117',
            height=640, margin=dict(l=10, r=10, t=60, b=10),
            font=dict(size=10, color='#c9d1d9'),
        )
        return fig

    # ─────────────────────────────────────────
    # 图十二：交叉频度矩阵
    # ─────────────────────────────────────────
    def cross_frequency_heatmap(self, top_n: int = 20) -> go.Figure:
        """
        多个账户之间的交易频率热力图。

        取 Top-N 高流水节点，计算它们两两之间的交易次数（有向 + 无向），
        用热力图展示，颜色越深 = 交易越频繁。
        """
        G = self.analyzer.G
        if len(G.nodes()) == 0:
            return go.Figure()

        # 取 Top-N 节点
        sorted_nodes = sorted(
            self.analyzer.nodes.items(),
            key=lambda x: x[1].get('total_flow', 0), reverse=True)
        top_nodes = [nid for nid, _ in sorted_nodes[:top_n] if nid in G]

        if len(top_nodes) < 2:
            return go.Figure().update_layout(
                title=dict(text='交叉频度矩阵（节点不足）', font=dict(color='#8b949e')),
                template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117')

        # 构建频度矩阵
        labels = []
        for n in top_nodes:
            name = self.analyzer.identity.resolve(n)
            if len(name) > 8:
                name = name[:6] + '..'
            labels.append(name)

        n = len(top_nodes)
        matrix = np.zeros((n, n), dtype=int)

        for i, src in enumerate(top_nodes):
            for j, dst in enumerate(top_nodes):
                if G.has_edge(src, dst):
                    matrix[i][j] = G[src][dst].get('count', 0)

        fig = go.Figure(go.Heatmap(
            z=matrix,
            x=labels,
            y=labels,
            colorscale=[
                [0, '#0d1117'],
                [0.15, '#161b22'],
                [0.3, '#1f3a5f'],
                [0.5, '#2563eb'],
                [0.7, '#ff6b6b'],
                [1.0, '#ff0000']
            ],
            showscale=True,
            colorbar=dict(title='交易次数'),
            hovertemplate='%{y} → %{x}<br>交易次数: %{z}<extra></extra>',
        ))

        fig.update_layout(
            title=dict(text=f'交叉频度矩阵（Top{top_n}节点）', font=dict(size=16, color='#e6edf3')),
            template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117',
            height=max(600, n * 25 + 120),
            margin=dict(l=120, r=40, t=60, b=120),
            xaxis=dict(tickangle=-45, tickfont=dict(size=9, color='#8b949e')),
            yaxis=dict(tickfont=dict(size=9, color='#8b949e'), autorange='reversed'),
        )
        return fig

    # ─────────────────────────────────────────
    # 图十三：账户对比分析
    # ─────────────────────────────────────────
    def account_comparison(self, account_a: str = None,
                           account_b: str = None) -> go.Figure:
        """
        两个账户的多维行为对比雷达图 + 指标对比表。

        若未指定账户，自动取风险评分最高的两名嫌疑人。
        """
        sorted_nodes = sorted(
            self.analyzer.nodes.items(),
            key=lambda x: x[1].get('risk_score', 0), reverse=True)

        if account_a is None:
            account_a = sorted_nodes[0][0] if sorted_nodes else None
        if account_b is None:
            account_b = sorted_nodes[1][0] if len(sorted_nodes) > 1 else None

        if not account_a or not account_b:
            return go.Figure().update_layout(
                title=dict(text='账户对比分析（数据不足）', font=dict(color='#8b949e')),
                template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117')

        info_a = self.analyzer.nodes.get(account_a, {})
        info_b = self.analyzer.nodes.get(account_b, {})
        name_a = info_a.get('name', account_a)
        name_b = info_b.get('name', account_b)

        G = self.analyzer.G

        # 提取多维指标
        def _metrics(acc_id, info):
            """归一化到 0-100 的多维指标"""
            max_val = max(
                max(v.get('in_flow', 0) for v in self.analyzer.nodes.values()), 1)
            max_out = max(
                max(v.get('out_flow', 0) for v in self.analyzer.nodes.values()), 1)
            max_total = max(
                max(v.get('total_flow', 0) for v in self.analyzer.nodes.values()), 1)
            max_degree = max(max(v.get('in_degree', 0) + v.get('out_degree', 0)
                                 for v in self.analyzer.nodes.values()), 1)

            in_f = info.get('in_flow', 0)
            out_f = info.get('out_flow', 0)
            in_d = info.get('in_degree', 0)
            out_d = info.get('out_degree', 0)
            total = info.get('total_flow', 0)

            # 上游重合度（与对方共享的上游比例）
            if acc_id in G:
                upstreams_a = set(G.predecessors(acc_id))
            else:
                upstreams_a = set()

            return {
                '流入规模': min(in_f / max_val * 100, 100),
                '流出规模': min(out_f / max_out * 100, 100),
                '总流水': min(total / max_total * 100, 100),
                '入度': min(in_d / max_degree * 100, 100),
                '出度': min(out_d / max_degree * 100, 100),
                '风险评分': min(info.get('risk_score', 0), 100),
            }

        m_a = _metrics(account_a, info_a)
        m_b = _metrics(account_b, info_b)

        categories = list(m_a.keys())
        vals_a = [m_a[k] for k in categories]
        vals_b = [m_b[k] for k in categories]

        fig = go.Figure()

        # 雷达图
        fig.add_trace(go.Scatterpolar(
            r=vals_a + [vals_a[0]],
            theta=categories + [categories[0]],
            fill='toself', name=name_a,
            line=dict(color='#ff6b6b', width=2),
            fillcolor='rgba(255,107,107,0.15)',
            marker=dict(size=4),
        ))
        fig.add_trace(go.Scatterpolar(
            r=vals_b + [vals_b[0]],
            theta=categories + [categories[0]],
            fill='toself', name=name_b,
            line=dict(color='#4d96ff', width=2),
            fillcolor='rgba(77,150,255,0.15)',
            marker=dict(size=4),
        ))

        fig.update_layout(
            polar=dict(
                radialaxis=dict(visible=True, range=[0, 100],
                                gridcolor='#21262d', tickfont=dict(color='#8b949e')),
                angularaxis=dict(gridcolor='#21262d', tickfont=dict(color='#8b949e')),
                bgcolor='#0d1117',
            ),
            title=dict(text=f'账户对比分析：{name_a} vs {name_b}',
                       font=dict(size=16, color='#e6edf3')),
            template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117',
            height=560, margin=dict(l=80, r=80, t=60, b=40),
            legend=dict(font=dict(color='#c9d1d9'), bgcolor='rgba(22,27,34,0.9)'),
            showlegend=True,
        )

        # 用 annotations 添加关键数值对比
        # 上游重合度计算
        if account_a in G and account_b in G:
            up_a = set(G.predecessors(account_a))
            up_b = set(G.predecessors(account_b))
            common = up_a & up_b
            overlap = len(common) / max(len(up_a | up_b), 1) * 100

            # 下游重合度
            dn_a = set(G.successors(account_a))
            dn_b = set(G.successors(account_b))
            common_dn = dn_a & dn_b
            overlap_dn = len(common_dn) / max(len(dn_a | dn_b), 1) * 100

            # 是否有直接交易
            direct = '无'
            if G.has_edge(account_a, account_b):
                direct = f'{name_a}→{name_b}: ¥{G[account_a][account_b].get("weight",0)/10000:.1f}万'
            elif G.has_edge(account_b, account_a):
                direct = f'{name_b}→{name_a}: ¥{G[account_b][account_a].get("weight",0)/10000:.1f}万'

            fig.add_annotation(
                text=(f'上游重合度: {overlap:.0f}%  |  '
                      f'下游重合度: {overlap_dn:.0f}%  |  '
                      f'直接交易: {direct}'),
                xref='paper', yref='paper', x=0.5, y=-0.05,
                showarrow=False, font=dict(color='#8b949e', size=11),
            )

        return fig

    # ─────────────────────────────────────────
    # 图十四：重点人员档案卡
    # ─────────────────────────────────────────
    def key_person_profiles(self, top_n: int = 5) -> tuple:
        """
        为 Top-N 高风险嫌疑人生成完整资金画像。

        返回 (fig, profiles)：
          - fig: Plotly Figure，多维指标对比图
          - profiles: list of dict，每个嫌疑人的详细画像数据
        """
        G = self.analyzer.G
        sorted_nodes = sorted(
            self.analyzer.nodes.items(),
            key=lambda x: x[1].get('risk_score', 0), reverse=True)

        profiles = []
        for nid, info in sorted_nodes[:top_n]:
            name = info.get('name', nid)
            lv = info.get('level', LEVEL_UNKNOWN)
            level_str = {LEVEL_AGENT1: '一级代理', LEVEL_AGENT2: '二级代理',
                         LEVEL_AGENT3: '三级代理', LEVEL_GAMBLER: '赌客',
                         LEVEL_UNKNOWN: '待定外围'}.get(lv, '?')

            # 上游 / 下游列表
            upstreams = []
            downstreams = []
            if nid in G:
                for pred in G.predecessors(nid):
                    w = G[pred][nid].get('weight', 0)
                    upstreams.append({
                        'name': self.analyzer.identity.resolve(pred),
                        'amount': w,
                        'count': G[pred][nid].get('count', 0),
                    })
                for succ in G.successors(nid):
                    w = G[nid][succ].get('weight', 0)
                    downstreams.append({
                        'name': self.analyzer.identity.resolve(succ),
                        'amount': w,
                        'count': G[nid][succ].get('count', 0),
                    })

            # 排序
            upstreams.sort(key=lambda x: x['amount'], reverse=True)
            downstreams.sort(key=lambda x: x['amount'], reverse=True)

            # 身份信息
            detail = self.analyzer.identity.get_detail(nid) or {}

            profile = {
                'id': nid,
                'name': name,
                'level': lv,
                'level_str': level_str,
                'risk_score': info.get('risk_score', 0),
                'in_flow': info.get('in_flow', 0),
                'out_flow': info.get('out_flow', 0),
                'net_flow': info.get('net_flow', 0),
                'total_flow': info.get('total_flow', 0),
                'in_degree': info.get('in_degree', 0),
                'out_degree': info.get('out_degree', 0),
                'is_launder': info.get('is_launder_suspect', False),
                'top_upstreams': upstreams[:5],
                'top_downstreams': downstreams[:5],
                'id_card': detail.get('id_card', ''),
                'phone': detail.get('phone', ''),
                'tags': self.analyzer.identity.get_tags(nid),
            }
            profiles.append(profile)

        # 可视化：横向柱状图对比关键指标
        if not profiles:
            return go.Figure(), []

        names = [p['name'] for p in profiles]
        risk_scores = [p['risk_score'] for p in profiles]
        in_flows = [p['in_flow'] / 10000 for p in profiles]
        out_flows = [p['out_flow'] / 10000 for p in profiles]

        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=['风险评分', '流入(万)', '流出(万)', '出入度'],
            vertical_spacing=0.12, horizontal_spacing=0.12)

        fig.add_trace(go.Bar(
            x=names, y=risk_scores,
            marker=dict(color=['#ff6b6b' if s >= 80 else '#ffd93d' if s >= 50 else '#6bcb77'
                              for s in risk_scores]),
            hovertemplate='%{x}: %{y:.1f}分<extra></extra>'
        ), row=1, col=1)

        fig.add_trace(go.Bar(
            x=names, y=in_flows,
            marker=dict(color='#4d96ff', opacity=0.8),
            hovertemplate='%{x}: ¥%{y:.1f}万<extra></extra>'
        ), row=1, col=2)

        fig.add_trace(go.Bar(
            x=names, y=out_flows,
            marker=dict(color='#ff922b', opacity=0.8),
            hovertemplate='%{x}: ¥%{y:.1f}万<extra></extra>'
        ), row=2, col=1)

        fig.add_trace(go.Bar(
            x=names,
            y=[p['in_degree'] for p in profiles],
            name='入度',
            marker=dict(color='#58a6ff', opacity=0.8),
        ), row=2, col=2)
        fig.add_trace(go.Bar(
            x=names,
            y=[p['out_degree'] for p in profiles],
            name='出度',
            marker=dict(color='#f06595', opacity=0.8),
        ), row=2, col=2)

        fig.update_layout(
            title=dict(text=f'重点人员档案 - Top{top_n}嫌疑人', font=dict(size=16, color='#e6edf3')),
            template='plotly_dark', paper_bgcolor='#0d1117', plot_bgcolor='#0d1117',
            height=560, margin=dict(l=60, r=30, t=70, b=80),
            showlegend=False,
            xaxis=dict(tickangle=-30, tickfont=dict(size=9, color='#8b949e')),
            xaxis2=dict(tickangle=-30, tickfont=dict(size=9, color='#8b949e')),
            xaxis3=dict(tickangle=-30, tickfont=dict(size=9, color='#8b949e')),
            xaxis4=dict(tickangle=-30, tickfont=dict(size=9, color='#8b949e')),
            legend2=dict(font=dict(color='#8b949e')),
        )
        return fig, profiles


# ═══════════════════════════════════════════════════════════
# 模块五：报告生成
# ═══════════════════════════════════════════════════════════
def generate_html_report(analyzer: GamblingAnalyzer,
                         viz: Visualizer,
                         suspect_df: pd.DataFrame) -> str:
    """生成完整HTML侦查报告"""

    import time as _time
    _t0 = _time.perf_counter()

    print("  [1/9] 资金流向网络图...", end='', flush=True)
    _t = _time.perf_counter()
    fig_network = viz.fund_flow_network()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [2/9] 层级流向桑基图...", end='', flush=True)
    _t = _time.perf_counter()
    fig_sankey  = viz.hierarchy_chart()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [3/9] 嫌疑人风险排行...", end='', flush=True)
    _t = _time.perf_counter()
    fig_ranking = viz.suspect_ranking_chart(suspect_df)
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [4/9] 时间维度分析...", end='', flush=True)
    _t = _time.perf_counter()
    fig_time    = viz.time_analysis_chart()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [5/9] 资金流入流出对比...", end='', flush=True)
    _t = _time.perf_counter()
    fig_inout   = viz.inout_comparison(suspect_df)
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [6/9] 组织架构图...", end='', flush=True)
    _t = _time.perf_counter()
    fig_org     = viz.org_structure_chart()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [7/9] 关键人物分析...", end='', flush=True)
    _t = _time.perf_counter()
    key_data    = viz.key_persons_analysis()
    print(f" {_time.perf_counter()-_t:.1f}s")


    # 新增：社团扩展分析（自动全图划分，取 Top-5 社区）
    print("  [7.5/14] 社团扩展分析...", end='', flush=True)
    _t = _time.perf_counter()
    fig_community, community_list = viz.community_expansion()
    print(f" {_time.perf_counter()-_t:.1f}s")

    # ── 新增分析模块 ──────────────────────────────────────────
    print("  [8/14] 交易频率分析...", end='', flush=True)
    _t = _time.perf_counter()
    fig_freq = viz.transaction_frequency_analysis()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [9/14] 共同账户分析...", end='', flush=True)
    _t = _time.perf_counter()
    fig_common, common_groups = viz.common_upstream_analysis()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [10/14] 资金穿透追踪...", end='', flush=True)
    _t = _time.perf_counter()
    fig_penetration = viz.fund_penetration_trace()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [11/14] 交叉频度矩阵...", end='', flush=True)
    _t = _time.perf_counter()
    fig_heatmap = viz.cross_frequency_heatmap()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [12/14] 账户对比分析...", end='', flush=True)
    _t = _time.perf_counter()
    fig_compare = viz.account_comparison()
    print(f" {_time.perf_counter()-_t:.1f}s")

    print("  [13/14] 重点人员档案...", end='', flush=True)
    _t = _time.perf_counter()
    fig_profiles, profile_data = viz.key_person_profiles()
    print(f" {_time.perf_counter()-_t:.1f}s")

    # 保存图表文件
    print(f"  [14/14] 保存图表文件（13个）...", end='', flush=True)
    _t = _time.perf_counter()

    # 保存所有独立图表文件
    _chart_files = [
        (fig_network,     'fund_flow_network.html',      '资金流向网络图'),
        (fig_sankey,      'hierarchy_chart.html',        '资金层级流向（桑基图）'),
        (fig_org,         'org_structure.html',          '团伙人员组织架构图'),
        (fig_ranking,     'suspect_ranking.html',        '嫌疑人风险评分排行'),
        (fig_time,        'time_analysis.html',          '交易时间维度分析'),
        (fig_inout,       'inout_comparison.html',       '主要嫌疑人资金流入流出对比'),
        (fig_community,   'community_expansion.html',    '社团扩展分析'),
        (fig_freq,        'transaction_frequency.html',  '交易频率趋势分析'),
        (fig_common,      'common_upstream.html',        '共同资金来源分析'),
        (fig_penetration, 'fund_penetration.html',       '资金穿透追踪'),
        (fig_heatmap,     'cross_frequency.html',        '交叉频度矩阵'),
        (fig_compare,     'account_comparison.html',     '账户对比分析'),
        (fig_profiles,    'key_person_profiles.html',    '重点人员档案'),
    ]
    for _fig, _fname, _title in _chart_files:
        _fig.write_html(
            str(OUTPUT_DIR / _fname),
            include_plotlyjs='cdn',
            full_html=True,
        )
        print(f"    → {_fname}")
    _save_time = _time.perf_counter() - _t
    print(f"  图表保存完成，耗时 {_save_time:.1f}s")

    # 统计摘要
    total_flow = sum(v['total_flow'] for v in analyzer.nodes.values())
    level_counts = {}
    for v in analyzer.nodes.values():
        lv = v.get('level', LEVEL_UNKNOWN)
        level_counts[lv] = level_counts.get(lv, 0) + 1

    # 各层级精确数量
    cnt_l1 = level_counts.get(LEVEL_AGENT1,  0)
    cnt_l2 = level_counts.get(LEVEL_AGENT2,  0)
    cnt_l3 = level_counts.get(LEVEL_AGENT3,  0)
    cnt_l4 = level_counts.get(LEVEL_GAMBLER, 0)
    cnt_l5 = level_counts.get(LEVEL_UNKNOWN, 0)
    cnt_launder  = sum(1 for v in analyzer.nodes.values() if v.get('is_launder_suspect'))
    cnt_unverify = sum(1 for v in analyzer.nodes.values()
                       if '待核查' in str(v.get('name', '')))

    keyword_hits = int(analyzer.tx['keyword_hit'].sum()) if 'keyword_hit' in analyzer.tx.columns else 0
    date_range = ''
    valid_dates = analyzer.tx['datetime_parsed'].dropna()
    if len(valid_dates) > 0:
        date_range = (f"{valid_dates.min().strftime('%Y-%m-%d')} ~ "
                      f"{valid_dates.max().strftime('%Y-%m-%d')}")

    # 主报告不再内嵌任何图表脚本，图表全部在独立 HTML 文件中
    level_name_map  = {
        LEVEL_AGENT1:  '一级代理',
        LEVEL_AGENT2:  '二级代理',
        LEVEL_AGENT3:  '三级代理',
        LEVEL_GAMBLER: '赌客',
        LEVEL_UNKNOWN: '待定外围',
    }
    level_badge_cls = {
        LEVEL_AGENT1:  'badge-1',
        LEVEL_AGENT2:  'badge-2',
        LEVEL_AGENT3:  'badge-3',
        LEVEL_GAMBLER: 'badge-4',
        LEVEL_UNKNOWN: 'badge-5',
    }

    def _badge(lv: int) -> str:
        return (f'<span class="badge {level_badge_cls.get(lv,"badge-5")}">'
                f'{level_name_map.get(lv,"?")}</span>')

    # 资金枢纽表格
    hub_rows = ''.join(
        f'<tr><td>{i+1}</td><td><b>{p["name"]}</b></td><td>{_badge(p["level"])}</td>'
        f'<td>¥{p["total_flow"]/10000:.1f}万</td>'
        f'<td>¥{p["in_flow"]/10000:.1f}万</td>'
        f'<td>¥{p["out_flow"]/10000:.1f}万</td>'
        f'<td><span style="color:#ff6b6b;font-weight:700">{p["risk_score"]}</span></td></tr>'
        for i, p in enumerate(key_data.get('top_hubs', []))
    )
    hub_html = f'''
    <table class="suspect-table">
      <thead><tr><th>#</th><th>姓名/账号</th><th>层级</th><th>总流水</th>
      <th>流入</th><th>流出</th><th>风险分</th></tr></thead>
      <tbody>{hub_rows}</tbody>
    </table>''' if hub_rows else '<p style="color:#8b949e">暂无数据</p>'

    # 中介节点表格
    broker_rows = ''.join(
        f'<tr><td>{i+1}</td><td><b>{p["name"]}</b></td><td>{_badge(p["level"])}</td>'
        f'<td>{p["betweenness"]:.5f}</td>'
        f'<td>¥{p["total_flow"]/10000:.1f}万</td>'
        f'<td><span style="color:#ff6b6b;font-weight:700">{p["risk_score"]}</span></td></tr>'
        for i, p in enumerate(key_data.get('top_brokers', []))
    )
    broker_html = f'''
    <table class="suspect-table">
      <thead><tr><th>#</th><th>姓名/账号</th><th>层级</th><th>中介中心度</th>
      <th>总流水</th><th>风险分</th></tr></thead>
      <tbody>{broker_rows}</tbody>
    </table>''' if broker_rows else '<p style="color:#8b949e">暂无数据</p>'

    # 高额关系对
    pair_rows = ''.join(
        f'<tr><td>{i+1}</td>'
        f'<td><b>{p["a_name"]}</b> {_badge(p["a_level"])}</td>'
        f'<td style="text-align:center">{"⇄" if p["is_mutual"] else "→"}</td>'
        f'<td><b>{p["b_name"]}</b> {_badge(p["b_level"])}</td>'
        f'<td>¥{p["total_amount"]/10000:.1f}万</td>'
        f'<td>{"<span style=\'color:#ff3b30\'>互转⚠️</span>" if p["is_mutual"] else ""}</td></tr>'
        for i, p in enumerate(key_data.get('top_pairs', []))
    )
    pair_html = f'''
    <table class="suspect-table">
      <thead><tr><th>#</th><th>账户A</th><th>方向</th><th>账户B</th>
      <th>累计金额</th><th>备注</th></tr></thead>
      <tbody>{pair_rows}</tbody>
    </table>''' if pair_rows else '<p style="color:#8b949e">暂无数据</p>'

    # 闭环转账
    loop_rows = ''.join(
        f'<tr><td>{i+1}</td>'
        f'<td>{"→".join(lp["chain"])} → <i>（回到起点）</i></td>'
        f'<td style="text-align:center">{lp["length"]}人</td>'
        f'<td>¥{lp["total_amount"]/10000:.1f}万</td></tr>'
        for i, lp in enumerate(key_data.get('loops', []))
    )
    loop_html = f'''
    <div style="background:rgba(255,59,48,0.08);border:1px solid rgba(255,59,48,0.25);
                border-radius:8px;padding:12px;margin-bottom:12px;">
      <p style="color:#ff6b6b;font-size:0.85em;margin-bottom:10px">
        ⚠️ 检测到以下账户存在<b>循环转账链条</b>，可能用于规避监控或资金套现，建议重点核查。
      </p>
    </div>
    <table class="suspect-table">
      <thead><tr><th>#</th><th>转账链条</th><th>环节数</th><th>涉及金额</th></tr></thead>
      <tbody>{loop_rows}</tbody>
    </table>''' if loop_rows else '<p style="color:#34c759">✅ 未检测到明显闭环转账</p>'

    # 爆发性交易
    burst_rows = ''.join(
        f'<tr><td><b>{p["name"]}</b></td><td>{_badge(p["level"])}</td>'
        f'<td>¥{p["total_flow"]/10000:.1f}万</td>'
        f'<td><span style="color:#ff6b6b;font-weight:700">{p["risk_score"]}</span></td></tr>'
        for p in key_data.get('burst_persons', [])[:10]
    )
    burst_html = f'''
    <table class="suspect-table">
      <thead><tr><th>姓名/账号</th><th>层级</th><th>总流水</th><th>风险分</th></tr></thead>
      <tbody>{burst_rows}</tbody>
    </table>''' if burst_rows else '<p style="color:#34c759">✅ 未检测到爆发性交易异常</p>'

    # 夜间交易
    night_rows = ''.join(
        f'<tr><td><b>{p["name"]}</b></td><td>{_badge(p["level"])}</td>'
        f'<td style="color:#ff9500">{p["night_ratio"]:.1%}</td>'
        f'<td><span style="color:#ff6b6b;font-weight:700">{p["risk_score"]}</span></td></tr>'
        for p in key_data.get('night_persons', [])
    )
    night_html = f'''
    <table class="suspect-table">
      <thead><tr><th>姓名/账号</th><th>层级</th><th>夜间比例(0-6时)</th><th>风险分</th></tr></thead>
      <tbody>{night_rows}</tbody>
    </table>''' if night_rows else '<p style="color:#34c759">✅ 未检测到显著夜间交易异常</p>'

    # ── 洗钱嫌疑账号专栏 ────────────────────────────────────────────────
    launder_suspects = sorted(
        [(nid, info) for nid, info in analyzer.nodes.items()
         if info.get('is_launder_suspect')],
        key=lambda x: x[1].get('launder_score', 0), reverse=True
    )
    launder_rows = ''.join(
        f'<tr>'
        f'<td>{i+1}</td>'
        f'<td><b>{info.get("name", nid)}</b></td>'
        f'<td>{_badge(info.get("level", LEVEL_UNKNOWN))}</td>'
        f'<td style="color:#ff3b30;font-weight:700">{info.get("launder_score", 0)}</td>'
        f'<td style="color:#ff6b6b">¥{info.get("total_flow", 0)/10000:.1f}万</td>'
        f'<td style="font-size:0.82em">{"; ".join(info.get("launder_flags", []))}</td>'
        f'</tr>'
        for i, (nid, info) in enumerate(launder_suspects[:20])
    )
    launder_html = f'''
    <div style="background:rgba(255,59,48,0.08);border:1px solid rgba(255,59,48,0.3);
                border-radius:8px;padding:12px;margin-bottom:12px;">
      <p style="color:#ff6b6b;font-size:0.85em;margin-bottom:10px">
        ⚠️ 以下账户具有<b>明显洗钱特征</b>，包括高过路率、境外转账、快速中转等，建议立即开展专项侦查，
        重点查明境外账户归属。
      </p>
    </div>
    <table class="suspect-table">
      <thead><tr><th>#</th><th>姓名/账号</th><th>层级</th><th>洗钱评分</th>
      <th>总流水</th><th>特征说明</th></tr></thead>
      <tbody>{launder_rows}</tbody>
    </table>''' if launder_rows else '<p style="color:#34c759">✅ 未识别到明显洗钱特征账号</p>'

    # ── 待核查账号专栏 ────────────────────────────────────────────────
    unverify_suspects = [
        (nid, info) for nid, info in analyzer.nodes.items()
        if '待核查' in str(info.get('name', ''))
    ]
    unverify_suspects.sort(key=lambda x: x[1].get('risk_score', 0), reverse=True)
    unverify_rows = ''.join(
        f'<tr>'
        f'<td>{i+1}</td>'
        f'<td style="color:#ff9500;font-family:monospace">{nid}</td>'
        f'<td>{_badge(info.get("level", LEVEL_UNKNOWN))}</td>'
        f'<td>¥{info.get("total_flow", 0)/10000:.1f}万</td>'
        f'<td>¥{info.get("in_flow", 0)/10000:.1f}万</td>'
        f'<td>¥{info.get("out_flow", 0)/10000:.1f}万</td>'
        f'<td><span style="color:#ff6b6b;font-weight:700">{info.get("risk_score", 0)}</span></td>'
        f'</tr>'
        for i, (nid, info) in enumerate(unverify_suspects[:30])
    )
    unverify_html = f'''
    <div style="background:rgba(255,149,0,0.08);border:1px solid rgba(255,149,0,0.3);
                border-radius:8px;padding:12px;margin-bottom:12px;">
      <p style="color:#ff9500;font-size:0.85em;margin-bottom:10px">
        ⚠️ 以下账号<b>未在身份对照表中登记</b>，无法确认实名信息。建议协调相关平台或银行，
        补充核查真实身份，尤其是流水大、风险评分高的账号。
      </p>
    </div>
    <table class="suspect-table">
      <thead><tr><th>#</th><th>嫌疑账号</th><th>层级</th><th>总流水</th>
      <th>流入</th><th>流出</th><th>风险分</th></tr></thead>
      <tbody>{unverify_rows}</tbody>
    </table>''' if unverify_rows else '<p style="color:#34c759">✅ 所有账号均已登记身份信息</p>'

    # 嫌疑人表格HTML
    table_html = suspect_df.head(50).to_html(
        index=False, classes='suspect-table', border=0,
        table_id='suspect-table'
    )

    # ── 社团分析 HTML ─────────────────────────────────────────────
    level_badge_cls_g = {
        LEVEL_AGENT1:  'badge-1', LEVEL_AGENT2: 'badge-2',
        LEVEL_AGENT3:  'badge-3', LEVEL_GAMBLER: 'badge-4', LEVEL_UNKNOWN: 'badge-5',
    }
    level_name_map_g = {
        LEVEL_AGENT1: '一级代理', LEVEL_AGENT2: '二级代理',
        LEVEL_AGENT3: '三级代理', LEVEL_GAMBLER: '赌客', LEVEL_UNKNOWN: '待定外围',
    }

    def _badge_g(lv: int) -> str:
        return (f'<span class="badge {level_badge_cls_g.get(lv,"badge-5")}">'
                f'{level_name_map_g.get(lv,"?")}</span>')

    community_section_html = ''
    for comm_info in community_list:
        ci    = comm_info['id']
        color = comm_info['color']
        size  = comm_info['size']
        members = comm_info['members'][:20]  # 每社区最多展示20人
        member_rows = ''.join(
            f'<tr>'
            f'<td>{j+1}</td>'
            f'<td><b style="color:{color}">{m["name"]}</b>'
            f'{"<span style=\'color:#ffd700;margin-left:5px\'>🌱</span>" if m["is_seed"] else ""}</td>'
            f'<td>{_badge_g(m["level"])}</td>'
            f'<td>¥{m["total_flow"]/10000:.1f}万</td>'
            f'<td><span style="color:#ff6b6b;font-weight:700">{m["risk_score"]}</span></td>'
            f'<td style="font-size:0.8em">{"  ".join(f"<span style=\'background:rgba(88,166,255,0.15);color:#58a6ff;padding:2px 6px;border-radius:10px;margin:1px\'>{t}</span>" for t in m["tags"]) if m["tags"] else ""}</td>'
            f'</tr>'
            for j, m in enumerate(members)
        )
        community_section_html += f'''
    <div style="background:#1c2128;border:2px solid {color}33;border-radius:10px;
                padding:18px;margin-bottom:16px;">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
        <div style="width:12px;height:12px;border-radius:50%;background:{color}"></div>
        <h3 style="color:{color};font-size:1em;margin:0">社团 {ci}（共 {size} 人）</h3>
      </div>
      <table class="suspect-table">
        <thead><tr><th>#</th><th>姓名/账号</th><th>层级</th><th>总流水</th>
        <th>风险分</th><th>标签</th></tr></thead>
        <tbody>{member_rows}</tbody>
      </table>
      {"<p style='color:#8b949e;font-size:0.8em;margin-top:8px'>（仅展示前20人，完整名单见独立图表文件）</p>" if size > 20 else ""}
    </div>'''

    if not community_section_html:
        community_section_html = '<p style="color:#8b949e">暂无社团数据（节点数不足）</p>'

    # ── 共同资金来源分析 HTML ─────────────────────────────────────
    if common_groups:
        cu_rows = ''
        for i, g in enumerate(common_groups[:10]):
            member_display = ', '.join(g['member_names'][:4])
            if g['size'] > 4:
                member_display += f' 等{g["size"]}人'
            upstream_display = ', '.join(g['upstream_names'][:3])
            if len(g['upstream_names']) > 3:
                upstream_display += f' 等{len(g["upstream_names"])}个'
            cu_rows += (
                f'<tr><td>{i+1}</td>'
                f'<td>{member_display}</td>'
                f'<td style="color:#ff6b6b;font-weight:700">{g["size"]}人</td>'
                f'<td style="font-size:0.85em">{upstream_display}</td>'
                f'<td>¥{g["total_flow"]/10000:.1f}万</td></tr>'
            )
        common_upstream_html = f'''
    <table class="suspect-table">
      <thead><tr><th>#</th><th>群组成员</th><th>人数</th><th>共同上游</th><th>群组总流水</th></tr></thead>
      <tbody>{cu_rows}</tbody>
    </table>'''
    else:
        common_upstream_html = '<p style="color:#8b949e">暂无发现共同资金来源群组</p>'

    # ── 重点人员档案 HTML ────────────────────────────────────────
    if profile_data:
        level_name_p = {
            LEVEL_AGENT1: '一级代理', LEVEL_AGENT2: '二级代理',
            LEVEL_AGENT3: '三级代理', LEVEL_GAMBLER: '赌客',
            LEVEL_UNKNOWN: '待定外围',
        }
        pr_cards = ''
        for i, p in enumerate(profile_data):
            lv_cls = f'badge-{p["level"]}'
            lv_name = level_name_p.get(p['level'], '?')
            # 上游列表
            up_str = '<br>'.join(
                f'{u["name"]}: ¥{u["amount"]/10000:.1f}万 ({u["count"]}笔)'
                for u in p['top_upstreams'][:3]) or '无'
            # 下游列表
            dn_str = '<br>'.join(
                f'{d["name"]}: ¥{d["amount"]/10000:.1f}万 ({d["count"]}笔)'
                for d in p['top_downstreams'][:3]) or '无'
            # 身份信息
            id_info = []
            if p.get('id_card'):
                id_info.append(f'身份证: {p["id_card"]}')
            if p.get('phone'):
                id_info.append(f'电话: {p["phone"]}')
            id_info_str = '<br>'.join(id_info) or '未登记'
            # 洗钱标记
            launder_badge = '<span style="background:#ff3b3020;color:#ff3b30;padding:2px 8px;border-radius:10px;font-size:0.8em">洗钱嫌疑</span>' if p['is_launder'] else ''
            # 标签
            tags_str = ' '.join(
                f'<span style="background:rgba(88,166,255,0.15);color:#58a6ff;padding:2px 6px;border-radius:10px;font-size:0.8em">{t}</span>'
                for t in p.get('tags', []))

            pr_cards += f'''
    <div style="background:#1c2128;border:1px solid #30363d;border-radius:10px;padding:18px;margin-bottom:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <div>
          <span style="font-size:1.15em;font-weight:700;color:#e6edf3">{p["name"]}</span>
          <span class="badge {lv_cls}" style="margin-left:8px">{lv_name}</span>
          {launder_badge} {tags_str}
        </div>
        <div style="text-align:right;">
          <div style="font-size:1.4em;font-weight:700;color:{"#ff6b6b" if p["risk_score"]>=80 else "#ffd93d" if p["risk_score"]>=50 else "#6bcb77"}">{p["risk_score"]:.0f}分</div>
          <div style="font-size:0.75em;color:#8b949e">风险评分</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:12px;">
        <div style="background:#0d1117;border-radius:8px;padding:10px;text-align:center;">
          <div style="color:#8b949e;font-size:0.75em">总流水</div>
          <div style="color:#e6edf3;font-weight:700;font-size:1em">¥{p["total_flow"]/10000:.1f}万</div>
        </div>
        <div style="background:#0d1117;border-radius:8px;padding:10px;text-align:center;">
          <div style="color:#8b949e;font-size:0.75em">流入 / 流出</div>
          <div style="color:#4d96ff;font-weight:700;font-size:1em">¥{p["in_flow"]/10000:.1f}万 / ¥{p["out_flow"]/10000:.1f}万</div>
        </div>
        <div style="background:#0d1117;border-radius:8px;padding:10px;text-align:center;">
          <div style="color:#8b949e;font-size:0.75em">入度 / 出度</div>
          <div style="color:#e6edf3;font-weight:700;font-size:1em">{p["in_degree"]} / {p["out_degree"]}</div>
        </div>
        <div style="background:#0d1117;border-radius:8px;padding:10px;text-align:center;">
          <div style="color:#8b949e;font-size:0.75em">身份信息</div>
          <div style="color:#e6edf3;font-size:0.82em">{id_info_str}</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div style="background:#0d1117;border-radius:8px;padding:10px;">
          <div style="color:#4d96ff;font-size:0.8em;font-weight:600;margin-bottom:6px">Top 上游（资金来源）</div>
          <div style="color:#c9d1d9;font-size:0.82em;line-height:1.6">{up_str}</div>
        </div>
        <div style="background:#0d1117;border-radius:8px;padding:10px;">
          <div style="color:#ff922b;font-size:0.8em;font-weight:600;margin-bottom:6px">Top 下游（资金去向）</div>
          <div style="color:#c9d1d9;font-size:0.82em;line-height:1.6">{dn_str}</div>
        </div>
      </div>
    </div>'''
        profiles_html = pr_cards
    else:
        profiles_html = '<p style="color:#8b949e">暂无人员档案数据</p>'

    # ── 自定义标签汇总表 ──────────────────────────────────────────
    all_tagged = analyzer.identity.get_all_tagged() if hasattr(analyzer.identity, 'custom_tags') else {}
    if all_tagged:
        tag_rows = ''.join(
            f'<tr><td style="font-family:monospace;color:#8b949e;font-size:0.85em">'
            f'{analyzer.identity.resolve(acc)}</td>'
            f'<td style="font-family:monospace;font-size:0.8em;color:#c9d1d9">{acc}</td>'
            f'<td>{"  ".join(f"<span style=\'background:rgba(88,166,255,0.15);color:#58a6ff;padding:2px 8px;border-radius:10px;margin:2px;display:inline-block\'>{t}</span>" for t in tags)}</td>'
            f'</tr>'
            for acc, tags in sorted(all_tagged.items())
        )
        tag_table_html = f'''
    <table class="suspect-table">
      <thead><tr><th>姓名</th><th>账号/ID</th><th>标签</th></tr></thead>
      <tbody>{tag_rows}</tbody>
    </table>'''
    else:
        tag_table_html = '''
    <div style="background:#1c2128;border:1px solid #30363d;border-radius:8px;padding:20px;text-align:center;">
      <p style="color:#8b949e;font-size:0.9em">当前暂无自定义标签。</p>
      <p style="color:#8b949e;font-size:0.82em;margin-top:8px">
        可在代码中调用 <code style="color:#58a6ff;background:#161b22;padding:2px 6px;border-radius:4px">
        analyzer.identity.add_tag("姓名或账号", "标签内容")</code> 为指定人员打标签，
        或使用 <code style="color:#58a6ff;background:#161b22;padding:2px 6px;border-radius:4px">
        batch_add_tags({"姓名": "重点追查,已抓获"})</code> 批量打标。
      </p>
      <p style="color:#8b949e;font-size:0.82em;margin-top:8px">
        <b>常用标签示例：</b> 重点追查 | 已抓获 | 境外嫌疑 | 洗钱嫌疑 | 已冻结账号 | 证据固定 | 待补信息
      </p>
    </div>'''

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>网赌团伙资金链分析报告</title>

<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
          background: #0d1117; color: #c9d1d9; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
             padding: 40px; text-align: center; border-bottom: 1px solid #30363d; }}
  .header h1 {{ font-size: 2.2em; color: #fff; letter-spacing: 2px; }}
  .header .subtitle {{ color: #8b949e; margin-top: 10px; font-size: 1em; }}
  .header .meta {{ display: flex; justify-content: center; flex-wrap: wrap; gap: 30px 40px; margin-top: 20px; }}
  .meta-item {{ text-align: center; }}
  .meta-item .val {{ font-size: 1.8em; font-weight: 700; color: #ff6b6b; }}
  .meta-item .lbl {{ font-size: 0.8em; color: #8b949e; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 30px 20px; }}
  .section {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px;
              padding: 25px; margin-bottom: 25px; }}
  .section h2 {{ font-size: 1.3em; color: #e6edf3; margin-bottom: 15px;
                  border-left: 4px solid #ff6b6b; padding-left: 12px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                 gap: 15px; margin-bottom: 25px; }}
  .stat-card {{ background: #1c2128; border: 1px solid #30363d; border-radius: 10px;
                padding: 20px; text-align: center; }}
  .stat-card .val {{ font-size: 2em; font-weight: 700; margin-bottom: 5px; }}
  .stat-card .lbl {{ color: #8b949e; font-size: 0.85em; }}
  .level1 {{ color: #ff3b30; }} .level2 {{ color: #ff9500; }}
  .level3 {{ color: #ffcc00; }} .level4 {{ color: #34c759; }}
  .suspect-table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
  .suspect-table th {{ background: #21262d; color: #e6edf3; padding: 10px 12px;
                        text-align: left; border-bottom: 2px solid #30363d; white-space: nowrap; }}
  .suspect-table td {{ padding: 9px 12px; border-bottom: 1px solid #21262d; color: #c9d1d9; white-space: nowrap; }}
  .suspect-table tr:hover {{ background: #1c2128; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px;
             font-size: 0.75em; font-weight: 600; }}
  .badge-1 {{ background: rgba(255,59,48,0.2);   color: #ff3b30; }}
  .badge-2 {{ background: rgba(255,149,0,0.2);   color: #ff9500; }}
  .badge-3 {{ background: rgba(255,204,0,0.2);   color: #b8960a; }}
  .badge-4 {{ background: rgba(52,199,89,0.2);   color: #34c759; }}
  .badge-5 {{ background: rgba(142,142,147,0.2); color: #8e8e93; }}
  .warning-box {{ background: rgba(255,59,48,0.1); border: 1px solid rgba(255,59,48,0.3);
                   border-radius: 8px; padding: 15px; margin-bottom: 20px; }}
  .warning-box p {{ color: #ff6b6b; font-size: 0.9em; line-height: 1.6; }}
  footer {{ text-align: center; padding: 30px; color: #8b949e; font-size: 0.8em;
             border-top: 1px solid #30363d; }}
  /* 图表占位符样式 */
  .chart-placeholder {{
    background: #1c2128; border: 1px dashed #30363d; border-radius: 8px;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 12px; color: #8b949e; font-size: 0.9em; min-height: 120px;
  }}
  .chart-placeholder .spinner {{
    width: 32px; height: 32px;
    border: 3px solid #30363d; border-top-color: #58a6ff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  @media print {{ body {{ background: white; color: black; }}
                   .header {{ background: #333; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>📋 网赌团伙资金链分析报告</h1>
  <div class="subtitle">生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')} | 数据周期：{date_range}</div>
  <div class="meta">
    <div class="meta-item"><div class="val">{len(analyzer.nodes)}</div><div class="lbl">涉案账户数</div></div>
    <div class="meta-item"><div class="val">¥{total_flow/10000:.1f}万</div><div class="lbl">涉案总流水</div></div>
    <div class="meta-item"><div class="val">{len(analyzer.tx)}</div><div class="lbl">交易记录数</div></div>
    <div class="meta-item"><div class="val">{keyword_hits}</div><div class="lbl">关键词命中</div></div>
    <div class="meta-item"><div class="val level1">{cnt_l1}</div><div class="lbl">一级代理</div></div>
    <div class="meta-item"><div class="val level2">{cnt_l2}</div><div class="lbl">二级代理</div></div>
    <div class="meta-item"><div class="val level3">{cnt_l3}</div><div class="lbl">三级代理</div></div>
    <div class="meta-item"><div class="val level4">{cnt_l4}</div><div class="lbl">赌客</div></div>
    <div class="meta-item" style="background:rgba(255,59,48,0.12);"><div class="val" style="color:#ff3b30">{cnt_launder}</div><div class="lbl">🚨洗钱嫌疑</div></div>
    <div class="meta-item" style="background:rgba(255,149,0,0.12);"><div class="val" style="color:#ff9500">{cnt_unverify}</div><div class="lbl">❓待核查账号</div></div>
  </div>
</div>

<div class="container">

<div class="warning-box">
<p>⚠️ <strong>数据保密提示</strong>：本报告包含涉案嫌疑人的账户信息及资金流水，属于侦查秘密，
   严禁在案件之外传播或共享。所有结论仅为侦查参考，最终认定须经合法证据固定程序。</p>
</div>

<!-- 统计概览 -->
<div class="section">
<h2>📊 涉案账户层级分布（赌球团伙金字塔结构）</h2>
<div class="stats-grid">
  <div class="stat-card">
    <div class="val level1">{cnt_l1}</div>
    <div class="lbl">一级代理（总代/庄家）</div>
    <div style="font-size:0.75em;color:#8b949e;margin-top:5px">流入≥50万，高出度，资金集散中心</div>
  </div>
  <div class="stat-card">
    <div class="val level2">{cnt_l2}</div>
    <div class="lbl">二级代理（中级代理）</div>
    <div style="font-size:0.75em;color:#8b949e;margin-top:5px">流入≥5万，有明确上下游，承上启下</div>
  </div>
  <div class="stat-card">
    <div class="val level3">{cnt_l3}</div>
    <div class="lbl">三级代理（基层代理）</div>
    <div style="font-size:0.75em;color:#8b949e;margin-top:5px">流入≥5千，直接管理赌客</div>
  </div>
  <div class="stat-card">
    <div class="val level4">{cnt_l4}</div>
    <div class="lbl">赌客（会员）</div>
    <div style="font-size:0.75em;color:#8b949e;margin-top:5px">净流出为主，小额充值/投注</div>
  </div>
  <div class="stat-card" style="border:1px solid rgba(255,59,48,0.4);">
    <div class="val" style="color:#ff3b30">{cnt_launder}</div>
    <div class="lbl">🚨 洗钱嫌疑账号</div>
    <div style="font-size:0.75em;color:#ff6b6b;margin-top:5px">高过路率/境外转账/快速中转</div>
  </div>
  <div class="stat-card" style="border:1px solid rgba(255,149,0,0.4);">
    <div class="val" style="color:#ff9500">{cnt_unverify}</div>
    <div class="lbl">❓ 待核查账号</div>
    <div style="font-size:0.75em;color:#ff9500;margin-top:5px">未在身份对照表登记，需重点核查</div>
  </div>
  <div class="stat-card">
    <div class="val level4">{cnt_l5}</div>
    <div class="lbl">待定外围</div>
    <div style="font-size:0.75em;color:#8b949e;margin-top:5px">无法归类至上述四类</div>
  </div>
</div>
</div>

<!-- 可视化图表入口 -->
<div class="section">
<h2>📊 可视化图表（点击打开独立窗口）</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">各图表已独立保存为 HTML 文件，点击按钮在新标签页中打开，支持交互、缩放、导出。</p>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;">
  <a href="fund_flow_network.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#1e3a5f,#0f2a4a);border:1px solid #2d6a9f;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">🌐</div>
      <div style="color:#58a6ff;font-weight:700;font-size:1em;margin-bottom:6px">资金流向网络图</div>
      <div style="color:#8b949e;font-size:0.82em">节点大小=总流水 | 连线宽度=资金量<br>红=一级代理 橙=二级代理 黄=三级代理<br>❓=待核查 🚨=洗钱嫌疑</div>
    </div>
  </a>
  <a href="hierarchy_chart.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#2a1e5f,#1a0f4a);border:1px solid #6d2d9f;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">📊</div>
      <div style="color:#a78bfa;font-weight:700;font-size:1em;margin-bottom:6px">资金层级流向（桑基图）</div>
      <div style="color:#8b949e;font-size:0.82em">流向带宽度反映资金量大小<br>直观展示多层级资金流转路径</div>
    </div>
  </a>
  <a href="org_structure.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#1e5f3a,#0f4a2a);border:1px solid #2d9f6a;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">📊</div>
      <div style="color:#34d399;font-weight:700;font-size:1em;margin-bottom:6px">团伙人员组织架构图</div>
      <div style="color:#8b949e;font-size:0.82em">纵轴=层级 横轴按流水量分散<br>悬停节点可查看详细信息</div>
    </div>
  </a>
  <a href="suspect_ranking.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#5f1e1e,#4a0f0f);border:1px solid #9f2d2d;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">🎯</div>
      <div style="color:#f87171;font-weight:700;font-size:1em;margin-bottom:6px">嫌疑人风险评分排行</div>
      <div style="color:#8b949e;font-size:0.82em">综合流水+层级+中心度+关键词<br>+夜间交易+爆发性交易评分</div>
    </div>
  </a>
  <a href="time_analysis.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#5f4a1e,#4a380f);border:1px solid #9f7d2d;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">⏰</div>
      <div style="color:#fbbf24;font-weight:700;font-size:1em;margin-bottom:6px">交易时间维度分析</div>
      <div style="color:#8b949e;font-size:0.82em">红色区域=凌晨0-6时高频交易<br>时间规律揭示隐蔽操盘特征</div>
    </div>
  </a>
  <a href="inout_comparison.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#1e4a5f,#0f3a4a);border:1px solid #2d7a9f;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">💰</div>
      <div style="color:#38bdf8;font-weight:700;font-size:1em;margin-bottom:6px">资金流入流出对比</div>
      <div style="color:#8b949e;font-size:0.82em">主要嫌疑人收支对比分析<br>资金净流向与异常比例</div>
    </div>
  </a>
  <a href="transaction_frequency.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#2d1f5e,#1a1245);border:1px solid #6d4aad;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">📈</div>
      <div style="color:#c4b5fd;font-weight:700;font-size:1em;margin-bottom:6px">交易频率分析</div>
      <div style="color:#8b949e;font-size:0.82em">按月/周/日的交易频次趋势<br>识别突然高频的异常账户</div>
    </div>
  </a>
  <a href="common_upstream.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#3d1f1f,#2a1515);border:1px solid #9f4a4a;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">🔗</div>
      <div style="color:#fca5a5;font-weight:700;font-size:1em;margin-bottom:6px">共同资金来源分析</div>
      <div style="color:#8b949e;font-size:0.82em">发现共享同一上游的账户群<br>识别同代理下线的赌客网络</div>
    </div>
  </a>
  <a href="fund_penetration.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#1f3d2d,#152a1f);border:1px solid #4a9f6a;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">🎯</div>
      <div style="color:#86efac;font-weight:700;font-size:1em;margin-bottom:6px">资金穿透追踪</div>
      <div style="color:#8b949e;font-size:0.82em">递归展开完整资金链路<br>从源头到终端的全路径追踪</div>
    </div>
  </a>
  <a href="cross_frequency.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#1f2d3d,#151f2a);border:1px solid #4a6a9f;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">🔥</div>
      <div style="color:#93c5fd;font-weight:700;font-size:1em;margin-bottom:6px">交叉频度矩阵</div>
      <div style="color:#8b949e;font-size:0.82em">账户间交易频率热力图<br>快速发现高频资金通道</div>
    </div>
  </a>
  <a href="account_comparison.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#3d2d1f,#2a1f15);border:1px solid #9f7a4a;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">⚖️</div>
      <div style="color:#fcd34d;font-weight:700;font-size:1em;margin-bottom:6px">账户对比分析</div>
      <div style="color:#8b949e;font-size:0.82em">两名嫌疑人的多维行为对比<br>雷达图展示各维度指标差异</div>
    </div>
  </a>
  <a href="key_person_profiles.html" target="_blank" style="text-decoration:none;">
    <div style="background:linear-gradient(135deg,#3d1f2d,#2a1520);border:1px solid #9f4a7a;border-radius:10px;padding:20px;transition:transform .2s;" onmouseover="this.style.transform='translateY(-3px)'" onmouseout="this.style.transform=''">
      <div style="font-size:2em;margin-bottom:8px">👤</div>
      <div style="color:#f9a8d4;font-weight:700;font-size:1em;margin-bottom:6px">重点人员档案</div>
      <div style="color:#8b949e;font-size:0.82em">Top嫌疑人完整资金画像<br>风险评分/流水/上下游关系</div>
    </div>
  </a>
</div>
</div>


<!-- 资金流向图谱 -->
<div class="section" style="display:none">
<h2>🌐 资金流向图谱（节点大小 = 总流水量）</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">
  红色=一级卡 | 橙色=二级卡 | 黄色=三级卡 | 绿色=外围 | 连线宽度=资金量 | 悬停节点查看详情
</p>
</div>

<!-- 桑基图 -->
<div class="section" style="display:none">
<h2>📊 资金层级流向（桑基图）</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">流向带宽度反映资金量大小</p>
</div>

<!-- 人员组织架构 -->
<div class="section" style="display:none">
<h2>📊 团伙人员组织架构图</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">
  纵轴表示层级（一级卡最高），横轴按流水量分散排列。节点大小=总流水，连线表示资金往来。
  悬停节点可查看详细信息。
</p>
</div>

<!-- 重点人物 & 关键关系 -->
<div class="section">
<h2>🎯 侦查重点：关键人物与可疑关系</h2>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">

  <div style="background:#1c2128;border:1px solid #30363d;border-radius:10px;padding:18px">
    <h3 style="color:#ff9500;font-size:1em;margin-bottom:12px">💰 资金枢纽 Top 10</h3>
    <p style="color:#8b949e;font-size:0.8em;margin-bottom:10px">总流水最高，是团伙资金运转核心</p>
    {hub_html}
  </div>

  <div style="background:#1c2128;border:1px solid #30363d;border-radius:10px;padding:18px">
    <h3 style="color:#ff6b6b;font-size:1em;margin-bottom:12px">🔗 中介节点 Top 10</h3>
    <p style="color:#8b949e;font-size:0.8em;margin-bottom:10px">
      中介中心度最高——处于最多"资金通道"上，抓获后可切断更多联系
    </p>
    {broker_html}
  </div>

</div>

<div style="background:#1c2128;border:1px solid #30363d;border-radius:10px;padding:18px;margin-bottom:20px">
  <h3 style="color:#0a84ff;font-size:1em;margin-bottom:12px">🔄 高额关系对 Top 15（两账户间累计转账）</h3>
  <p style="color:#8b949e;font-size:0.8em;margin-bottom:10px">
    ⇄ 互转 表示双方均有向对方转账的记录，可能存在对赌或洗钱行为
  </p>
  {pair_html}
</div>

<div style="background:#1c2128;border:1px solid #30363d;border-radius:10px;padding:18px;margin-bottom:20px">
  <h3 style="color:#ff3b30;font-size:1em;margin-bottom:12px">⚠️ 闭环转账链（疑似洗钱）</h3>
  {loop_html}
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">

  <div style="background:#1c2128;border:1px solid #30363d;border-radius:10px;padding:18px">
    <h3 style="color:#ffcc00;font-size:1em;margin-bottom:12px">⚡ 爆发性交易人员</h3>
    <p style="color:#8b949e;font-size:0.8em;margin-bottom:10px">
      短时间内大量密集交易（{BURST_TIME_WINDOW}分钟内≥{BURST_MIN_COUNT}笔），可能在快速转移资金
    </p>
    {burst_html}
  </div>

  <div style="background:#1c2128;border:1px solid #30363d;border-radius:10px;padding:18px">
    <h3 style="color:#5e5ce6;font-size:1em;margin-bottom:12px">🌙 高夜间交易人员（夜间比>=40%）</h3>
    <p style="color:#8b949e;font-size:0.8em;margin-bottom:10px">
      凌晨0-6时交易比例异常高，规避监控特征明显
    </p>
    {night_html}
  </div>

</div>
</div>

<!-- 🚨 洗钱嫌疑专栏 -->
<div class="section">
<h2>🚨 洗钱嫌疑账号专项分析</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">
  识别方法：境外转账关键词命中 + 高过路率（≥80%流入后转出）+ 快速中转（24h内）+ 整数金额拆分特征
</p>
{launder_html}
</div>

<!-- ❓ 待核查账号专栏 -->
<div class="section">
<h2>❓ 待核查账号列表（需补充核查实名信息）</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">
  这些账号在身份对照表中未登记，存在匿名/被盗用账号的可能，建议协调平台或银行完善信息。
</p>
{unverify_html}
</div>

<!-- 风险排行 -->
<div class="section" style="display:none">
<h2>🎯 嫌疑人风险评分排行</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">
  评分综合考量：总流水(30分) + 层级(25分) + 网络中心度(20分) + 关键词命中(15分) + 夜间交易(5分) + 爆发性交易(5分) + 洗钱嫌疑(+15分)
</p>
</div>

<!-- 时间维度 -->
<div class="section" style="display:none">
<h2>⏰ 交易时间维度分析</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">凌晨0-6时（红色区域）高频交易为异常信号</p>
</div>


<!-- 资金对比 -->
<div class="section" style="display:none">
<h2>💰 主要嫌疑人资金流入流出对比</h2>
</div>

<!-- 社团扩展分析 -->
<div class="section">
<h2>🔍 团伙社团扩展分析</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">
  基于最大模块度社区发现算法，自动将所有涉案账户划分为独立犯罪社团。
  同一社团内成员资金往来密切，可能属于同一组织单元。
  <a href="community_expansion.html" target="_blank" style="color:#58a6ff;text-decoration:none;margin-left:8px">
    ↗ 打开完整交互图
  </a>
</p>
{community_section_html}
</div>

<!-- 交易频率分析 -->
<div class="section">
<h2>📈 交易频率趋势分析</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">
  按月度分析交易频次变化趋势。上方展示全量交易频率，下方展示 Top10 账户的月度交易分布。
  突然增高的交易频率可能表示账户被用于批量转账或资金结算。
  <a href="transaction_frequency.html" target="_blank" style="color:#58a6ff;text-decoration:none;margin-left:8px">
    ↗ 打开完整交互图
  </a>
</p>
</div>

<!-- 共同资金来源分析 -->
<div class="section">
<h2>🔗 共同资金来源分析</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">
  发现"多个人的上游完全相同"的账户群——这些账户可能属于同一个代理的下线网络。
  同一组内的成员资金来源高度一致，是识别赌客群和代理结构的重要依据。
  <a href="common_upstream.html" target="_blank" style="color:#58a6ff;text-decoration:none;margin-left:8px">
    ↗ 打开完整交互图
  </a>
</p>
{common_upstream_html}
</div>

<!-- 资金穿透追踪 -->
<div class="section">
<h2>🎯 资金穿透追踪</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">
  从 Top5 高风险嫌疑人出发，递归展开上下游资金链路，直到终端节点。
  Sankey 图展示资金从源头（上游代理）经过中间层到终端（赌客）的完整流动路径。
  <a href="fund_penetration.html" target="_blank" style="color:#58a6ff;text-decoration:none;margin-left:8px">
    ↗ 打开完整交互图
  </a>
</p>
</div>

<!-- 交叉频度矩阵 -->
<div class="section">
<h2>🔥 交叉频度矩阵</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">
  Top20 高流水账户之间的交易频率热力图。颜色越深表示两账户之间交易越频繁，
  有助于快速定位规律性资金通道和固定结算关系。
  <a href="cross_frequency.html" target="_blank" style="color:#58a6ff;text-decoration:none;margin-left:8px">
    ↗ 打开完整交互图
  </a>
</p>
</div>

<!-- 账户对比分析 -->
<div class="section">
<h2>⚖️ 账户对比分析</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">
  将风险评分最高的两名嫌疑人进行多维行为对比。雷达图展示双方在流入/流出/流水/度数/风险评分
  等维度的差异，底栏显示上游和下游重合度（高重合 = 可能属于同一团伙或关联密切）。
  <a href="account_comparison.html" target="_blank" style="color:#58a6ff;text-decoration:none;margin-left:8px">
    ↗ 打开完整交互图
  </a>
</p>
</div>

<!-- 重点人员档案 -->
<div class="section">
<h2>👤 重点人员档案</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:16px">
  Top5 高风险嫌疑人的完整资金画像，包含基本信息、资金规模、层级判定、风险评分、
  主要上下游关系等关键指标。
  <a href="key_person_profiles.html" target="_blank" style="color:#58a6ff;text-decoration:none;margin-left:8px">
    ↗ 打开完整交互图
  </a>
</p>
{profiles_html}
</div>
<div class="section">
<h2>🏷️ 人员标签管理</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">
  自定义标签用于在分析过程中为人员添加侦查状态标记，如"重点追查""已抓获""已冻结账号"等。
  标签会同步显示在社团分析图、单人关系图的悬停提示中。
</p>
{tag_table_html}
</div>

<!-- 嫌疑人详细表格 -->
<div class="section">
<h2>👤 嫌疑人详细信息表（Top 50）</h2>
<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px">
  按风险评分降序排列 | 含身份证号码 / 手机号 / 微信号 / 所属银行 | ⚠️需核查=未登记身份 | 🚨洗钱嫌疑 | 完整数据见 suspects_ranking.csv
</p>
<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">
{table_html}
</div>
</div>

</div>
<footer>
  网赌团伙资金链分析系统 | 侦查辅助工具 | 本报告仅供执法参考，严禁外泄
</footer>


</body>
</html>"""


    # ─────────────────────────────────────────────────────────────────────────
    # 用 plotly 生成的图表 HTML 片段替换占位符
    # ─────────────────────────────────────────────────────────────────────────

    # ── 直接保存主报告 HTML（纯文本+表格，无图表脚本，加载极快）────────────────
    report_path = OUTPUT_DIR / 'analysis_report.html'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html)
    _total = _time.perf_counter() - _t0
    print(f"  → 完整报告已保存至: {report_path}（{len(html)//1024}KB）")
    print(f"  ✅ 可视化报告生成完毕，总耗时 {_total:.1f}s")
    return str(report_path)


# ═══════════════════════════════════════════════════════════
# 主程序入口
# ═══════════════════════════════════════════════════════════

# 支持的账单文件扩展名
BILL_EXTENSIONS = {'.xlsx', '.xls', '.csv'}

# 命名规则：文件名中包含这些关键词时自动忽略（身份对照表等非流水文件）
IDENTITY_KEYWORDS = ['身份', '对照', 'identity', 'personnel', '人员', '名单', '信息表']

# 程序生成的结果文件（不应被当作原始账单再次读取）
GENERATED_OUTPUT_FILES = {
    'suspects_ranking.csv',
    'analysis_report.html',
    'fund_flow_network.html',
    'hierarchy_chart.html',
    'org_structure.html',
    'suspect_ranking.html',
    'time_analysis.html',
    'inout_comparison.html',
    'community_expansion.html',
    'transaction_frequency.html',
    'common_upstream.html',
    'fund_penetration.html',
    'cross_frequency.html',
    'account_comparison.html',
    'key_person_profiles.html',
}


def is_identity_file(name: str) -> bool:
    """判断文件名是否像身份对照表而非账单"""
    nl = name.lower()
    return any(kw in nl for kw in IDENTITY_KEYWORDS)


def is_generated_output_file(name: str) -> bool:
    """判断是否为程序自己生成的结果文件"""
    return name.lower() in GENERATED_OUTPUT_FILES


def extract_owner_from_filename(filepath: Path, identity: 'IdentityMapper') -> str:
    """
    从文件名中提取账号持有人标识。
    优先级：
      0. 纯数字>=16位 → 银行卡号，直接返回整个数字（必须在手机号之前判断，
         因为19位银行卡号中可能嵌入符合 1[3-9]\\d{9} 的子串）
      1. 文件名中包含手机号（11位数字）→ 用手机号作为 owner_id
      2. 文件名中包含完整邮箱地址（含@）→ 直接提取完整邮箱（下划线属于邮箱用户名，不可拆断）
      3. 文件名中包含下划线/连字符分隔的标识段 → 取最后一个有意义的段
      4. 兜底：使用不含扩展名的文件名（去掉括号编号）
    """
    stem = filepath.stem  # 不含扩展名的文件名
    # 首先去掉文件名末尾的括号编号，如 "378282246310005 (1)" → "378282246310005"
    # 这是同一账号的多期账单分割文件，括号内数字是分期序号
    stem_clean = re.sub(r'\s*[\(（]\d+[\)）]\s*$', '', stem).strip()

    # 0. 纯数字>=16位 → 银行卡号（优先于手机号判断！）
    #    19位银行卡号中可能嵌入手机号模式，如 4367421380013800000 含 13800138000
    if re.match(r'^\d{16,}$', stem_clean):
        return stem_clean

    # 1. 提取手机号（仅当 stem 本身长度<=15 或非纯数字时才匹配）
    #    正则锚定：手机号必须前后是非数字字符或字符串边界
    if len(stem_clean) <= 15 or not stem_clean.isdigit():
        phone_match = re.search(r'(?<![0-9])1[3-9]\d{9}(?![0-9])', stem_clean)
        if phone_match:
            return phone_match.group()

    # 2. 提取完整邮箱（用户名只含 ASCII 可打印字符，避免把中文前缀也匹配进去）
    #    邮箱用户名部分：字母/数字/. + - _ 组合，不包含中文
    email_match = re.search(r'[A-Za-z0-9][A-Za-z0-9.+_\-]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', stem_clean)
    if email_match:
        email = email_match.group()
        # 去掉被前缀噪声词粘连进来的部分（如 alipay_、wechat_、statement_ 等）
        parts_of_email = re.split(r'_', email)
        at_idx = next(i for i, p in enumerate(parts_of_email) if '@' in p)
        noise_prefix = {'alipay', 'wechat', 'wx', 'bank', 'bill', 'statement',
                        'record', 'export', 'download', 'data'}
        start = 0
        for i in range(at_idx):
            if parts_of_email[i].lower() in noise_prefix:
                start = i + 1
        clean_email = '_'.join(parts_of_email[start:])
        return clean_email

    # 3. 纯数字账号（6-15位，银行卡短号或其他纯数字ID）：直接返回clean的stem
    if re.match(r'^\d{6,}$', stem_clean):
        return stem_clean

    # 4. 分段提取人名/账号
    cleaned = re.sub(r'[\(\（][^\)\）]*[\)\）]', '', stem_clean)
    cleaned = re.sub(r'\d{4}[-./]\d{2}[-./]\d{2}', '', cleaned)
    cleaned = re.sub(r'\d{8}', '', cleaned)

    parts = re.split(r'[_\-\s·]+', cleaned.strip())
    parts = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]

    noise = {'微信', '支付宝', '银行', '账单', '流水', '明细', '记录',
             'wechat', 'alipay', 'bank', 'bill', 'statement'}
    meaningful = [p for p in parts if p.lower() not in noise and not p.isdigit()]

    if meaningful:
        return meaningful[-1]

    # 5. 兜底：clean的stem（最多20字符）
    return stem_clean[:20]




def scan_folder(folder_path: str) -> list:
    """
    扫描文件夹，返回所有账单文件列表（跳过身份对照表和临时文件）
    返回: [(Path, owner_hint), ...]
    """
    folder = Path(folder_path)
    if not folder.is_dir():
        raise NotADirectoryError(f"不是有效目录: {folder_path}")

    found = []
    # 递归查找，支持一层子目录
    for p in sorted(folder.rglob('*')):
        if not p.is_file():
            continue
        if p.suffix.lower() not in BILL_EXTENSIONS:
            continue
        # 跳过临时文件（Excel打开时的锁文件）
        if p.name.startswith('~$'):
            continue
        # 跳过身份对照表
        if is_identity_file(p.name):
            continue
        # 跳过程序自己生成的输出文件
        if is_generated_output_file(p.name):
            continue
        found.append(p)

    return found


def load_multiple_folders(folder_paths: list, identity: 'IdentityMapper') -> list:
    """
    批量读取多个文件夹的账单，合并返回标准化 DataFrame 列表。
    folder_paths: Path 字符串列表
    """
    all_dfs = []
    for fp in folder_paths:
        dfs = load_folder_bills(fp, identity)
        all_dfs.extend(dfs)
    return all_dfs


def load_folder_bills(folder_path: str, identity: 'IdentityMapper') -> list:
    """
    批量读取文件夹内所有账单文件。
    返回标准化后的 DataFrame 列表。
    """
    files = scan_folder(folder_path)
    if not files:
        return []

    print(f"\n  [DIR] 共扫描到 {len(files)} 个账单文件：")
    for f in files:
        print(f"     · {f.name}")

    all_dfs = []
    loader = DataLoader()

    for idx, fpath in enumerate(files, 1):
        owner = extract_owner_from_filename(fpath, identity)
        try:
            df_raw = DataLoader.auto_read(str(fpath))
            dtype = DataLoader.detect_type(df_raw)
            type_name = {'wechat': '微信', 'alipay': '支付宝',
                         'bank': '银行', 'bilateral': '双向流水', 'unknown': '未知'}[dtype]

            if dtype == 'wechat':
                df_norm = loader.normalize_wechat(df_raw, owner)
            elif dtype == 'alipay':
                df_norm = loader.normalize_alipay(df_raw, owner)
            elif dtype == 'bilateral':
                df_norm = loader.normalize_bilateral(df_raw, owner)
            else:
                df_norm = loader.normalize_bank(df_raw, owner)

            all_dfs.append(df_norm)
            print(f"  [{idx:>2}/{len(files)}] ✅ {fpath.name[:40]:<42}"
                  f"  [{type_name}] {len(df_raw)}条 → 持有人: {owner}")
        except Exception as e:
            import traceback
            print(f"  [{idx:>2}/{len(files)}] ❌ {fpath.name[:40]:<42}  读取失败: {e}")
            traceback.print_exc()

    return all_dfs


def prompt_path(prompt_text: str, must_be: str = 'any',
                required: bool = True) -> Optional[str]:
    """
    交互式询问路径。
    must_be: 'file' | 'dir' | 'any'
    """
    while True:
        raw = input(f"\n{prompt_text}").strip().strip('"').strip("'")
        if not raw:
            if not required:
                return None
            print("  ⚠️  路径不能为空，如不需要请输入 skip")
            continue
        if raw.lower() == 'skip':
            return None
        p = Path(raw)
        if must_be == 'file' and not p.is_file():
            print(f"  ❌ 找不到文件: {raw}")
            continue
        if must_be == 'dir' and not p.is_dir():
            print(f"  ❌ 找不到文件夹: {raw}")
            continue
        if must_be == 'any' and not p.exists():
            print(f"  ❌ 路径不存在: {raw}")
            continue
        return str(p)


def main():
    print("=" * 65)
    print("  📋 网赌团伙资金链分析系统 v3.1")
    print(f"  侦查辅助工具 | 输出结果目录: ./{OUTPUT_DIR.name}/")
    print("=" * 65)
    print("\n  使用方式：")
    print("  · 可输入一个或多个文件夹，系统自动扫描并批量解析")
    print("  · 多文件夹示例：微信/支付宝放一个文件夹，银行卡放另一个")
    print("  · 支持 Excel(.xlsx/.xls) 和 CSV(.csv) 格式")
    print("  · 路径可直接将文件夹拖入终端窗口获取\n")

    # ── 身份对照表 ──
    print("─" * 45)
    print("  【第一步】身份对照表（可选）")
    print("  包含账号/手机号 ↔ 真实姓名的映射关系")
    identity_path = prompt_path(
        "  身份对照表文件路径（无则输入 skip）> ",
        must_be='file', required=False
    )

    identity = IdentityMapper(identity_path)
    if identity_path:
        n_names  = len({v for v in identity.mapping.values() if v})
        n_id     = len(identity.id_card)
        n_phone  = len(identity.phone)
        n_wechat = len(identity.wechat)
        n_bank   = len(identity.bank)
        print(f"  ✅ 已加载身份对照，共 {len(identity.mapping)} 条映射")
        print(f"     · 真实姓名: {n_names} 人   身份证: {n_id} 条   手机号: {n_phone} 条"
              f"   微信号: {n_wechat} 条   银行: {n_bank} 条")
    else:
        print("  [i]  未提供身份对照表，将使用文件名/账号作为持有人标识")


    # ── 资金流水文件夹（支持多文件夹）──
    print("\n─" * 45)
    print("  【第二步】资金流水文件夹")
    print("  支持同时输入多个文件夹，例如：")
    print("    · 微信/支付宝账单放一个文件夹，银行卡账单放另一个文件夹")
    print("  输入方式（任选其一）：")
    print("    1. 用英文逗号分隔多个路径：  C:/path/A, C:/path/B")
    print("    2. 逐行输入，空行结束")
    print("    路径可直接将文件夹拖入终端窗口获取\n")

    while True:
        raw_input = input("  账单文件夹路径（多个用逗号分隔，或逐行输入后空行结束）> ").strip()
        if not raw_input:
            print("  ❌ 路径不能为空，请重新输入。")
            continue

        # ── 解析路径列表 ──
        # 先按逗号分割，如果只有一段再尝试逐行模式
        if ',' in raw_input:
            raw_paths = [p.strip().strip('"').strip("'") for p in raw_input.split(',') if p.strip()]
        else:
            # 第一行已输入，继续读取后续行，空行结束
            raw_paths = [raw_input.strip('"').strip("'")]
            print("  继续输入更多文件夹路径（直接回车结束）：")
            while True:
                line = input("  + 路径 > ").strip().strip('"').strip("'")
                if not line:
                    break
                raw_paths.append(line)

        # ── 验证每个路径 ──
        valid_paths = []
        has_error = False
        for rp in raw_paths:
            p = Path(rp)
            if not p.is_dir():
                print(f"  ❌ 找不到文件夹: {rp}")
                has_error = True
            else:
                valid_paths.append(str(p))

        if has_error:
            retry = input("  部分路径无效，重新输入全部路径？(y/n) > ").strip().lower()
            if retry == 'y':
                continue
            else:
                sys.exit(1)

        if not valid_paths:
            print("  ❌ 未找到任何有效文件夹，请重新输入。")
            continue

        # ── 预扫描所有文件夹 ──
        all_preview = []
        for fp in valid_paths:
            try:
                files = scan_folder(fp)
                for f in files:
                    all_preview.append((f, fp))
            except Exception as e:
                print(f"  ❌ 扫描失败 [{fp}]: {e}")
                has_error = True

        if has_error:
            retry = input("  扫描出错，重新输入？(y/n) > ").strip().lower()
            if retry == 'y':
                continue
            else:
                sys.exit(1)

        if not all_preview:
            print("  ⚠️  所有文件夹内均未找到账单文件（.xlsx/.xls/.csv）")
            print("     请确认路径正确，且文件不是身份对照表")
            retry = input("  重新输入？(y/n) > ").strip().lower()
            if retry == 'y':
                continue
            else:
                sys.exit(1)

        # ── 按文件夹分组预览 ──
        print(f"\n  [--] 预览：共 {len(valid_paths)} 个文件夹，{len(all_preview)} 个账单文件：")
        from itertools import groupby
        for fp in valid_paths:
            group_files = [f for f, src in all_preview if src == fp]
            print(f"\n  [DIR] {fp}  ({len(group_files)} 个文件)")
            for f in group_files:
                owner_hint = extract_owner_from_filename(f, identity)
                print(f"     · {f.name:<45} → 持有人: {owner_hint}")

        confirm = input("\n  确认读取以上所有文件？(y/n) > ").strip().lower()
        if confirm == 'y':
            folder_paths = valid_paths
            break
        else:
            print("  请重新输入文件夹路径。")

    # ── 批量读取（多文件夹）──
    print(f"\n[READ] 正在读取账单文件（共 {len(folder_paths)} 个文件夹）...")
    all_dfs = load_multiple_folders(folder_paths, identity)

    if not all_dfs:
        print("\n❌ 未读取到任何有效数据，程序退出。")
        sys.exit(1)

    # ── 合并数据 ──
    all_tx = pd.concat(all_dfs, ignore_index=True)
    print(f"\n✅ 共加载 {len(all_tx)} 条有效交易记录，"
          f"涉及 {all_tx['owner_id'].nunique()} 个主账户")

    # ── 运行分析 ──
    print("\n[>>>] 开始分析...")
    analyzer = GamblingAnalyzer(all_tx, identity)
    analyzer.run()

    # ── 分析后诊断（帮助排查数据问题）──
    if 'cp_key' in all_tx.columns:
        cp_ratio = (all_tx['cp_key'].str.strip() != '').mean()
        print(f"  [--] cp_key 非空比例: {cp_ratio:.1%}")
        if cp_ratio < 0.5:
            print("  ⚠️  cp_key 非空率偏低，请检查账单列名是否被正确识别")
    else:
        print("  ⚠️  cp_key 列不存在，请检查账单格式")
    print(f"  [--] 图节点数: {len(analyzer.G.nodes())}, 边数: {len(analyzer.G.edges())}")
    print(f"  [--] 嫌疑人节点数: {len(analyzer.nodes)}")

    # ── 生成输出 ──
    suspect_df = analyzer.get_suspect_df()
    csv_path = OUTPUT_DIR / 'suspects_ranking.csv'

    # 兜底：若文件被占用（如 Excel 打开中），自动加时间戳另存
    try:
        suspect_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    except PermissionError:
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_path = OUTPUT_DIR / f'suspects_ranking_{timestamp}.csv'
        suspect_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"\n⚠️  原文件被占用，已另存为: {csv_path.name}")

    print(f"\n📊 嫌疑人清单已保存至: {csv_path}")

    print("\n[VIZ] 正在生成可视化报告...")
    viz = Visualizer(analyzer)
    report_path = generate_html_report(analyzer, viz, suspect_df)

    # ── 输出摘要 ──
    print("\n" + "=" * 65)
    print("  ✅ 分析完成！")
    print("=" * 65)
    print(f"\n  [DIR] 输出文件目录: {OUTPUT_DIR.absolute()}")
    print(f"  ├─ analysis_report.html        ← 完整分析报告（主文件，用浏览器打开）")
    print(f"  ├─ fund_flow_network.html      ← 资金流向图谱")
    print(f"  ├─ hierarchy_chart.html        ← 层级关系桑基图")
    print(f"  ├─ org_structure.html          ← 人员组织架构图")
    print(f"  ├─ transaction_frequency.html  ← 交易频率趋势分析")
    print(f"  ├─ common_upstream.html        ← 共同资金来源分析")
    print(f"  ├─ fund_penetration.html       ← 资金穿透追踪")
    print(f"  ├─ cross_frequency.html        ← 交叉频度矩阵")
    print(f"  ├─ account_comparison.html     ← 账户对比分析")
    print(f"  ├─ key_person_profiles.html    ← 重点人员档案")
    print(f"  └─ suspects_ranking.csv        ← 嫌疑人风险排名（可导入Excel）")

    print(f"\n  [**] 涉案统计：")
    level_name = {
        LEVEL_AGENT1:  '一级代理（总代/庄家）',
        LEVEL_AGENT2:  '二级代理（中级代理）',
        LEVEL_AGENT3:  '三级代理（基层代理）',
        LEVEL_GAMBLER: '赌客（会员）',
        LEVEL_UNKNOWN: '待定外围',
    }
    from collections import Counter
    lc = Counter(v['level'] for v in analyzer.nodes.values())
    for lv in [LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3, LEVEL_GAMBLER, LEVEL_UNKNOWN]:
        cnt = lc.get(lv, 0)
        if cnt > 0:
            print(f"     {level_name[lv]}: {cnt} 人")

    cnt_launder_final  = sum(1 for v in analyzer.nodes.values() if v.get('is_launder_suspect'))
    cnt_unverify_final = sum(1 for v in analyzer.nodes.values()
                             if '待核查' in str(v.get('name', '')))
    if cnt_launder_final:
        print(f"\n  🚨 洗钱嫌疑账号: {cnt_launder_final} 个（详见报告洗钱专栏）")
    if cnt_unverify_final:
        print(f"  ❓ 待核查账号: {cnt_unverify_final} 个（身份未登记，建议补充核查）")

    print(f"\n  [TOP] 风险最高的前5名嫌疑人：")
    for _, row in suspect_df.head(5).iterrows():
        print(f"     [{row['风险评分']:5.1f}分] {row['姓名']:10s} "
              f"{row['层级']:12s} 总流水 ¥{row['总流水(元)']:>12,.0f}")

    print("\n  [TIP] 提示：用浏览器打开 output/analysis_report.html 查看完整交互式报告")
    print("=" * 65)

    # 自动打开报告
    try:
        import webbrowser
        open_it = input("\n是否自动在浏览器中打开报告？(y/n) > ").strip().lower()
        if open_it == 'y':
            webbrowser.open(f"file:///{Path(report_path).absolute().as_posix()}")
    except Exception:
        pass


if __name__ == '__main__':
    main()
