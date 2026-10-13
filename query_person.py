# -*- coding: utf-8 -*-
"""
query_person.py - 单人/单账号赌球信息快速查询工具
=================================================
用法：
    python query_person.py <账号或姓名>
    python query_person.py <账号或姓名> --html        # 同时生成HTML报告
    python query_person.py <账号或姓名> --top 20      # 关联交易显示前N条（默认50）
    python query_person.py                            # 交互模式

输出：
    控制台：关键信息摘要
    output/query_<name>_<time>.html：完整HTML报告（可选）
"""

import sys
import os
import re
import argparse
from datetime import datetime
from pathlib import Path

# ── 路径设置 ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(BASE_DIR))

# ── 导入主模块 ────────────────────────────────────────────────────────────────
try:
    from gambling_analysis import (
        GamblingAnalyzer, IdentityMapper,
        load_folder_bills,
        LEVEL_AGENT1, LEVEL_AGENT2, LEVEL_AGENT3,
        LEVEL_GAMBLER, LEVEL_UNKNOWN
    )
    import pandas as pd
except ImportError as e:
    print(f"[ERROR] 无法导入 gambling_analysis.py: {e}")
    sys.exit(1)

# ── 常量 ───────────────────────────────────────────────────────────────────────
# Never assume a private data folder is present in a checkout.  Callers must
# provide their own absolute input directory explicitly (or via the
# FUNDFLOW_DATA_DIR environment variable).
DATA_DIR      = Path(os.environ["FUNDFLOW_DATA_DIR"]).expanduser() if os.environ.get("FUNDFLOW_DATA_DIR") else None

# 身份对照表：自动搜索（支持多种文件名）
IDENTITY_FILE = None
for _candidate in [BASE_DIR / "身份信息对照表.xlsx",
                   BASE_DIR / "身份对照表.xlsx",
                   *([DATA_DIR / "身份信息对照表.xlsx", DATA_DIR / "身份对照表.xlsx"] if DATA_DIR else [])]:
    if _candidate.exists():
        IDENTITY_FILE = _candidate
        break

LEVEL_NAMES = {
    LEVEL_AGENT1: "一级代理（总代/庄家）",
    LEVEL_AGENT2: "二级代理（中级代理）",
    LEVEL_AGENT3: "三级代理（基层代理）",
    LEVEL_GAMBLER: "赌客（会员）",
    LEVEL_UNKNOWN: "待定外围人员",
}
LEVEL_COLORS = {
    LEVEL_AGENT1: "#e74c3c",
    LEVEL_AGENT2: "#e67e22",
    LEVEL_AGENT3: "#f1c40f",
    LEVEL_GAMBLER: "#3498db",
    LEVEL_UNKNOWN: "#95a5a6",
}


# ─────────────────────────────────────────────────────────────────────────────
# 核心查询类
# ─────────────────────────────────────────────────────────────────────────────
class PersonQuery:
    """
    查询逻辑完全基于 GamblingAnalyzer 的输出：
      - suspect_df：嫌疑人信息表（与主报告完全一致）
      - G：资金流向图（上下游）
      - tx：交易记录（合并后，owner_id/cp_key 均为规范ID）
    """

    def __init__(self, analyzer: GamblingAnalyzer):
        self.az = analyzer
        self.mapper: IdentityMapper = analyzer.identity
        # 主数据源：suspect_df（get_suspect_df 输出的完整表）
        self._sdf = analyzer.get_suspect_df()

    # ── 搜索：在 suspect_df 的多列上查找 ─────────────────────────────────
    def search(self, query: str) -> pd.DataFrame:
        """
        输入姓名/账号/手机号等，在 suspect_df 的多列上搜索，
        返回匹配的 DataFrame 子集（可能与主表列完全一致）。
        """
        q = query.strip().lower()
        sdf = self._sdf
        if sdf.empty:
            return sdf

        # 搜索列：姓名、账号/ID、身份证号码、手机号、微信号
        search_cols = ['姓名', '账号/ID', '身份证号码', '手机号', '微信号']

        mask = pd.Series(False, index=sdf.index)
        for col in search_cols:
            if col in sdf.columns:
                mask = mask | sdf[col].astype(str).str.lower().str.contains(q, na=False)

        # 也支持精确匹配账号/ID
        mask = mask | (sdf['账号/ID'].str.strip().str.lower() == q)

        return sdf[mask].reset_index(drop=True)

    # ── 获取完整信息（直接从 sdf 行取，不做二次计算） ───────────────────
    def get_row(self, node_id: str) -> dict:
        """根据 账号/ID 从 suspect_df 中精确取出一行，转为 dict"""
        row = self._sdf[self._sdf['账号/ID'] == node_id]
        if row.empty:
            return {}
        return row.iloc[0].to_dict()

    # ── 上下游（从 G 图边查询）──────────────────────────────────────────
    def get_neighbors(self, node_id: str, direction: str, top: int = 10) -> list:
        """
        direction='upstream'  -> 谁给 node_id 转过账
        direction='downstream'-> node_id 给谁转过账
        返回 [(neighbor_node_id, resolved_name, total_amount, level), ...]
        """
        G = self.az.G
        if not G.has_node(node_id):
            return []

        result = {}
        if direction == "upstream":
            edges = G.in_edges(node_id, data=True)
            for src, _, d in edges:
                result[src] = result.get(src, 0) + d.get("weight", 0)
        else:
            edges = G.out_edges(node_id, data=True)
            for _, dst, d in edges:
                result[dst] = result.get(dst, 0) + d.get("weight", 0)

        sorted_result = sorted(result.items(), key=lambda x: x[1], reverse=True)[:top]
        return [
            {
                "node": n,
                "name": self.mapper.resolve(n),
                "amount": amt,
                "level": self.az.nodes.get(n, {}).get("level", LEVEL_UNKNOWN),
            }
            for n, amt in sorted_result
        ]

    # ── 交易明细（从 tx 查询，合并后直接用 node_id 匹配即可） ───────────
    def get_transactions(self, node_id: str, top: int = 50) -> pd.DataFrame:
        """
        获取与 node_id（规范ID）相关的交易记录。
        合并后 tx.owner_id 和 tx.cp_key 都是规范ID。
        
        关键：is_income 是 owner_id 的视角（owner 收到=True，owner 支出=False）。
        当 cp_key == node_id 但 owner_id != node_id 时，方向需要翻转。
        """
        tx = self.az.tx
        mask = (tx["owner_id"] == node_id) | (tx["cp_key"] == node_id)
        result = tx[mask].copy()
        if result.empty:
            return result
        # 翻转方向：cp_key==node_id 但 owner_id!=node_id 的行，
        # is_income 需要取反（因为原始 is_income 是从 owner 视角定义的）
        flip = (result["cp_key"] == node_id) & (result["owner_id"] != node_id)
        result.loc[flip, "is_income"] = ~result.loc[flip, "is_income"]
        result = result.sort_values("amount_clean", ascending=False)
        return result.head(top) if top else result

    def get_all_tx_count(self, node_id: str) -> int:
        """获取与 node_id 相关的全部交易数量"""
        tx = self.az.tx
        mask = (tx["owner_id"] == node_id) | (tx["cp_key"] == node_id)
        return int(mask.sum())

    # ── 关联账号（从 IdentityMapper 获取）─────────────────────────────
    def get_aliases(self, node_id: str) -> list:
        """获取同一人名下所有原始账号"""
        # 尝试从 name_to_accounts 反查
        if node_id in self.mapper.name_to_accounts:
            return list(self.mapper.name_to_accounts[node_id])
        # 尝试 resolve 后反查
        resolved = self.mapper.resolve(node_id)
        if resolved != node_id and resolved in self.mapper.name_to_accounts:
            return list(self.mapper.name_to_accounts[resolved])
        return [node_id]

    # ── 社区信息 ──────────────────────────────────────────────────────────
    def get_community_info(self, node_id: str) -> dict:
        """获取节点所在社区及统计"""
        G = self.az.G
        if not G.has_node(node_id) or len(G.nodes()) == 0:
            return {}

        try:
            import networkx.algorithms.community as nx_comm
            UG = G.to_undirected()
            try:
                all_communities = [set(c) for c in nx_comm.greedy_modularity_communities(UG, weight='weight')]
            except Exception:
                import networkx as nx
                all_communities = [set(c) for c in nx.connected_components(UG)]
        except Exception:
            return {}

        for cid, members in enumerate(all_communities):
            if node_id in members:
                level_cnt = {}
                for m in members:
                    lv = self.az.nodes.get(m, {}).get("level", LEVEL_UNKNOWN)
                    level_cnt[lv] = level_cnt.get(lv, 0) + 1
                scored = [(m, self.az.nodes.get(m, {}).get("risk_score", 0)) for m in members]
                scored.sort(key=lambda x: x[1], reverse=True)
                return {
                    "community_id": cid,
                    "size": len(members),
                    "level_distribution": level_cnt,
                    "top_members": [
                        {"node": m, "name": self.mapper.resolve(m), "score": s}
                        for m, s in scored[:5]
                    ],
                }
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 控制台输出
# ─────────────────────────────────────────────────────────────────────────────
def print_person_report(qobj: PersonQuery, row: dict, top_tx: int = 50):
    """把查询结果打印到控制台（row 直接来自 suspect_df 行）"""
    node_id   = str(row.get('账号/ID', ''))
    display   = str(row.get('姓名', node_id))
    level_str = str(row.get('层级', '未知'))
    in_flow   = float(row.get('流入金额(元)', 0) or 0)
    out_flow  = float(row.get('流出金额(元)', 0) or 0)
    total_flow= float(row.get('总流水(元)', 0) or 0)
    in_deg    = int(row.get('上游账户数', 0) or 0)
    out_deg   = int(row.get('下游账户数', 0) or 0)
    score     = float(row.get('风险评分', 0) or 0)
    is_ml     = str(row.get('洗钱嫌疑', '否')) == '🚨 是'
    ml_detail = str(row.get('洗钱特征', '无'))
    need_verify = str(row.get('⚠️需核查', '否')) == '是'

    txs = qobj.get_transactions(node_id, top=top_tx)
    all_tx_count = qobj.get_all_tx_count(node_id)
    comm = qobj.get_community_info(node_id)

    sep = "=" * 65
    print(f"\n{sep}")
    print(f"  [人员档案] {display}  ({node_id})")
    if need_verify:
        print(f"  [!] 该人员未在身份对照表中登记，信息待核查")
    print(sep)

    # 身份信息（直接从 suspect_df 行读取，与主报告一致）
    id_card = str(row.get('身份证号码', '') or '')
    phone   = str(row.get('手机号', '') or '')
    wechat  = str(row.get('微信号', '') or '')
    bank    = str(row.get('所属银行', '') or '')
    if any([id_card, phone, wechat, bank]):
        print(f"  身份信息：")
        if id_card: print(f"    身份证：{id_card}")
        if phone:   print(f"    手机号：{phone}")
        if wechat:  print(f"    微信号：{wechat}")
        if bank:    print(f"    银  行：{bank}")
    print()

    print(f"  角  色：{level_str}")
    print(f"  风险评分：{score:.1f}" if score else "  风险评分：-")
    print(f"  洗钱嫌疑：{'[!!] 是' if is_ml else '否'}")
    if is_ml and ml_detail and ml_detail != '无':
        print(f"    洗钱依据：{ml_detail}")
    print()

    print(f"  资金流水")
    print(f"    总流水  ：{total_flow:>15,.2f} 元")
    print(f"    流  入  ：{in_flow:>15,.2f} 元")
    print(f"    流  出  ：{out_flow:>15,.2f} 元")
    print(f"    净流入  ：{in_flow - out_flow:>+15,.2f} 元")
    print()

    print(f"  网络关系")
    print(f"    上游账户（付款方）：{in_deg} 个")
    print(f"    下游账户（收款方）：{out_deg} 个")
    print()

    # 关联账号
    aliases = qobj.get_aliases(node_id)
    if len(aliases) > 1:
        print(f"  关联账号（同一人）：")
        for a in aliases:
            print(f"    - {a}")
        print()

    # 上游
    upstream = qobj.get_neighbors(node_id, direction="upstream", top=10)
    if upstream:
        print(f"  Top 上游（向本人转账最多）：")
        for u in upstream:
            lv_tag = LEVEL_NAMES.get(u["level"], "?")[:6]
            print(f"    [{lv_tag}] {u['name']:<20s}  {u['amount']:>12,.2f} 元")
        print()

    # 下游
    downstream = qobj.get_neighbors(node_id, direction="downstream", top=10)
    if downstream:
        print(f"  Top 下游（本人转账最多的对象）：")
        for d in downstream:
            lv_tag = LEVEL_NAMES.get(d["level"], "?")[:6]
            print(f"    [{lv_tag}] {d['name']:<20s}  {d['amount']:>12,.2f} 元")
        print()

    # 社区
    if comm:
        print(f"  所在团伙（社区 #{comm['community_id']}）：")
        print(f"    规模：{comm['size']} 人")
        dist_str = "  ".join(
            f"{LEVEL_NAMES.get(k,'?')[:4]}x{v}"
            for k, v in sorted(comm["level_distribution"].items())
        )
        print(f"    层级分布：{dist_str}")
        print(f"    核心成员：")
        for m in comm.get("top_members", []):
            print(f"      {m['name']}  评分{m['score']:.1f}")
        print()

    # 交易明细
    n_show = len(txs) if not txs.empty else 0
    print(f"  交易明细（金额最大 {n_show} 条，共 {all_tx_count} 条）：")
    print(f"  {'时间':<22}  {'方向':<5}  {'对方':<22}  {'金额(元)':>12}  {'备注'}")
    print(f"  {'-'*22}  {'-'*5}  {'-'*22}  {'-'*12}  {'-'*20}")
    if not txs.empty:
        for _, trow in txs.iterrows():
            owner  = str(trow.get("owner_id", ""))
            cp_key = str(trow.get("cp_key", ""))
            amount = float(trow.get("amount_clean", 0) or 0)
            is_inc = str(trow.get("is_income", "")).strip().lower() in ("true", "1", "yes")
            dt_col = trow.get("datetime_parsed", None)
            t      = str(dt_col)[:19] if pd.notna(dt_col) and dt_col else ""
            remark = str(trow.get("remark", ""))[:20]

            # is_income 在 get_transactions 中已翻转为查询对象视角
            # owner_id == node_id 时：对方 = cp_key（自己账单中的对方）
            # cp_key == node_id 时：对方 = owner_id（别人账单中的owner）
            if owner == node_id:
                counterpart = qobj.mapper.resolve(str(cp_key))[:22]
            else:
                counterpart = qobj.mapper.resolve(str(owner))[:22]
            direction = "[收入]" if is_inc else "[支出]"

            print(f"  {t:<22}  {direction:<5}  {counterpart:<22}  {amount:>12,.2f}  {remark}")
    else:
        print(f"  （无交易数据）")
    print(sep)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# HTML 报告生成
# ─────────────────────────────────────────────────────────────────────────────
def generate_html_report(qobj: PersonQuery, row: dict,
                         top_tx: int = 50) -> Path:
    """生成单人完整 HTML 报告（row 直接来自 suspect_df 行），返回文件路径"""
    node_id    = str(row.get('账号/ID', ''))
    display    = str(row.get('姓名', node_id))
    level_str  = str(row.get('层级', '未知'))
    in_flow    = float(row.get('流入金额(元)', 0) or 0)
    out_flow   = float(row.get('流出金额(元)', 0) or 0)
    total_flow = float(row.get('总流水(元)', 0) or 0)
    in_deg     = int(row.get('上游账户数', 0) or 0)
    out_deg    = int(row.get('下游账户数', 0) or 0)
    score      = float(row.get('风险评分', 0) or 0)
    is_ml      = str(row.get('洗钱嫌疑', '否')) == '🚨 是'
    ml_detail  = str(row.get('洗钱特征', '无'))
    ml_score   = float(row.get('洗钱评分', 0) or 0)
    need_verify= str(row.get('⚠️需核查', '否')) == '是'

    # 身份信息
    id_card = str(row.get('身份证号码', '') or '')
    phone   = str(row.get('手机号', '') or '')
    wechat  = str(row.get('微信号', '') or '')
    bank    = str(row.get('所属银行', '') or '')

    # 判断层级数值（用于图标）
    level = LEVEL_UNKNOWN
    if '一级' in level_str: level = LEVEL_AGENT1
    elif '二级' in level_str: level = LEVEL_AGENT2
    elif '三级' in level_str: level = LEVEL_AGENT3
    elif '赌客' in level_str: level = LEVEL_GAMBLER
    level_color = LEVEL_COLORS.get(level, "#95a5a6")

    # 上下游
    upstream   = qobj.get_neighbors(node_id, direction="upstream", top=10)
    downstream = qobj.get_neighbors(node_id, direction="downstream", top=10)
    txs = qobj.get_transactions(node_id, top=top_tx)
    all_tx_count = qobj.get_all_tx_count(node_id)
    comm = qobj.get_community_info(node_id)
    aliases = qobj.get_aliases(node_id)

    # ── 上下游表格 ────────────────────────────────────────────────────────────
    def neighbor_rows(neighbors: list) -> str:
        if not neighbors:
            return "<tr><td colspan='3' style='text-align:center;color:#aaa'>无数据</td></tr>"
        rows = []
        for n in neighbors:
            lc = LEVEL_COLORS.get(n["level"], "#95a5a6")
            ln = LEVEL_NAMES.get(n["level"], "?")
            rows.append(
                f"<tr>"
                f"<td><span class='badge' style='background:{lc}'>{ln[:4]}</span></td>"
                f"<td>{n['name']}</td>"
                f"<td style='text-align:right'>{n['amount']:,.2f}</td>"
                f"</tr>"
            )
        return "\n".join(rows)

    # ── 交易明细表格 ──────────────────────────────────────────────────────────
    tx_rows = []
    if not txs.empty:
        for _, trow in txs.iterrows():
            owner  = str(trow.get("owner_id", ""))
            cp_key = str(trow.get("cp_key", ""))
            amount = float(trow.get("amount_clean", 0) or 0)
            is_inc = str(trow.get("is_income", "")).strip().lower() in ("true", "1", "yes")
            dt_col = trow.get("datetime_parsed", None)
            t      = str(dt_col)[:19] if pd.notna(dt_col) and dt_col else ""
            remark = str(trow.get("remark", ""))[:40]

            # is_income 在 get_transactions 中已翻转为查询对象视角
            # owner_id == node_id 时：对方 = cp_key
            # cp_key == node_id 时：对方 = owner_id
            if owner == node_id:
                counterpart = qobj.mapper.resolve(str(cp_key))
            else:
                counterpart = qobj.mapper.resolve(str(owner))
            direction = "<span class='inc'>收入</span>" if is_inc else "<span class='out'>支出</span>"

            tx_rows.append(
                f"<tr>"
                f"<td>{t}</td>"
                f"<td>{direction}</td>"
                f"<td>{counterpart}</td>"
                f"<td style='text-align:right'>{amount:,.2f}</td>"
                f"<td>{remark}</td>"
                f"</tr>"
            )
    tx_html = "\n".join(tx_rows) if tx_rows else \
        "<tr><td colspan='5' style='text-align:center;color:#aaa'>无交易数据</td></tr>"

    # ── 关联账号标签 ─────────────────────────────────────────────────────────
    alias_tags = " ".join(
        f"<span class='alias-tag'>{a}</span>"
        for a in aliases if a != node_id
    )

    # ── 社区部分 ──────────────────────────────────────────────────────────────
    comm_html = ""
    if comm:
        dist_items = "".join(
            f"<li>{LEVEL_NAMES.get(k,'?')}: <b>{v}</b> 人</li>"
            for k, v in sorted(comm["level_distribution"].items())
        )
        member_rows = "".join(
            f"<tr><td>{m['name']}</td><td style='text-align:right'>{m['score']:.1f}</td></tr>"
            for m in comm.get("top_members", [])
        )
        comm_html = f"""
        <section class="card">
          <h2>🏘️ 所在团伙（社区 #{comm['community_id']}）</h2>
          <p>团伙规模：<strong>{comm['size']}</strong> 人</p>
          <div style="display:flex;gap:40px">
            <div>
              <h3>层级分布</h3>
              <ul>{dist_items}</ul>
            </div>
            <div>
              <h3>核心成员（风险排名前5）</h3>
              <table class="data-table">
                <thead><tr><th>姓名/账号</th><th>风险评分</th></tr></thead>
                <tbody>{member_rows}</tbody>
              </table>
            </div>
          </div>
        </section>
        """

    # ── 洗钱警告 ──────────────────────────────────────────────────────────────
    ml_html = ""
    if is_ml:
        reasons_li = "".join(
            f"<li>{r.strip()}</li>" for r in ml_detail.split('；') if r.strip()
        )
        ml_html = f"""
        <div class="ml-alert">
          ⚠️ <strong>洗钱嫌疑账号</strong> — 洗钱评分：{ml_score} 分
          <ul>{reasons_li}</ul>
        </div>
        """

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 身份信息块 ────────────────────────────────────────────────────────────
    id_items = []
    if id_card: id_items.append(f"<div><span class='label' style='font-size:0.85rem;color:#888'>身份证</span><div>{id_card}</div></div>")
    if phone:   id_items.append(f"<div><span class='label' style='font-size:0.85rem;color:#888'>手机号</span><div>{phone}</div></div>")
    if wechat:  id_items.append(f"<div><span class='label' style='font-size:0.85rem;color:#888'>微信号</span><div>{wechat}</div></div>")
    if bank:    id_items.append(f"<div><span class='label' style='font-size:0.85rem;color:#888'>银行</span><div>{bank}</div></div>")
    identity_block = ""
    if id_items:
        identity_block = (
            "<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;"
            "margin-top:12px;padding:12px;background:#f8f9fa;border-radius:10px'>"
            + "".join(id_items) + "</div>"
        )
    if need_verify:
        identity_block += "<div style='color:#e67e22;margin-top:8px;font-size:0.9rem'>⚠️ 该人员未在身份对照表中登记，以上信息待核查</div>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>人员查询报告 - {display}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: #f0f2f5;
    color: #333;
    padding: 24px;
  }}
  h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
  h2 {{ font-size: 1.1rem; margin-bottom: 12px; color: #444; border-bottom: 2px solid #e0e0e0; padding-bottom: 6px; }}
  h3 {{ font-size: 0.95rem; margin: 10px 0 6px; color: #555; }}
  .header {{ background:#1a1a2e; color:#fff; padding:24px 32px; border-radius:12px; margin-bottom:20px; }}
  .header .sub {{ opacity:0.65; font-size:0.85rem; margin-top:4px; }}
  .meta-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin:18px 0; }}
  .meta-item {{ background:#fff; border-radius:10px; padding:14px 18px; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
  .meta-item .label {{ font-size:0.78rem; color:#888; margin-bottom:4px; }}
  .meta-item .value {{ font-size:1.15rem; font-weight:600; }}
  .card {{ background:#fff; border-radius:12px; padding:20px 24px; box-shadow:0 2px 8px rgba(0,0,0,.07); margin-bottom:18px; }}
  .badge {{
    display:inline-block; padding:2px 8px; border-radius:12px;
    color:#fff; font-size:0.78rem; font-weight:600;
  }}
  .alias-tag {{
    display:inline-block; background:#eef2ff; color:#4f46e5;
    border:1px solid #c7d2fe; border-radius:8px; padding:2px 10px;
    font-size:0.82rem; margin:2px;
  }}
  .ml-alert {{
    background:#fff3cd; border:1px solid #ffc107; border-radius:8px;
    padding:12px 16px; margin-bottom:18px; color:#856404;
  }}
  .ml-alert ul {{ margin-left:20px; margin-top:6px; }}
  .data-table {{ width:100%; border-collapse:collapse; font-size:0.88rem; }}
  .data-table th {{ background:#f5f5f5; padding:8px 10px; text-align:left; font-weight:600; border-bottom:2px solid #e0e0e0; }}
  .data-table td {{ padding:7px 10px; border-bottom:1px solid #f0f0f0; }}
  .data-table tr:hover td {{ background:#fafafa; }}
  .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
  .out {{ color:#e74c3c; font-weight:600; }}
  .inc {{ color:#27ae60; font-weight:600; }}
  .tag-risk {{ background:#fee2e2; color:#b91c1c; padding:2px 10px; border-radius:12px; font-size:0.8rem; }}
  .tag-ml  {{ background:#fef3c7; color:#92400e; padding:2px 10px; border-radius:12px; font-size:0.8rem; }}
  .footer {{ text-align:center; color:#aaa; font-size:0.78rem; margin-top:24px; }}
  @media(max-width:700px) {{ .two-col {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>

<!-- 页头 -->
<div class="header">
  <h1>🔍 人员查询报告</h1>
  <div class="sub">查询时间：{now_str} &nbsp;|&nbsp; 数据库：全量流水</div>
</div>

<!-- 洗钱警告 -->
{ml_html}

<!-- 基本信息 -->
<section class="card">
  <h2>👤 基本信息</h2>
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:14px">
    <div style="font-size:2rem;background:{level_color};color:#fff;border-radius:50%;width:56px;height:56px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
      {'👑' if level==LEVEL_AGENT1 else '🧑‍💼' if level in (LEVEL_AGENT2,LEVEL_AGENT3) else '🎲' if level==LEVEL_GAMBLER else '❓'}
    </div>
    <div>
      <div style="font-size:1.4rem;font-weight:700">{display}</div>
      <div style="color:#888;font-size:0.9rem;margin-top:2px">{node_id}</div>
    </div>
    <div style="margin-left:auto;text-align:right">
      <span class="badge" style="background:{level_color};font-size:0.9rem;padding:4px 14px">{level_str}</span>
      {'<br><span class="tag-risk" style="margin-top:6px;display:inline-block">⚠️ 高风险</span>' if score>=60 else ''}
      {'<br><span class="tag-ml" style="margin-top:6px;display:inline-block">💸 洗钱嫌疑</span>' if is_ml else ''}
    </div>
  </div>
  {'<div style="margin-top:8px"><strong>关联账号（同一人）：</strong>' + alias_tags + '</div>' if alias_tags else ''}
  {identity_block}
</section>

<!-- 关键指标 -->
<div class="meta-grid">
  <div class="meta-item">
    <div class="label">总流水</div>
    <div class="value" style="color:#1a1a2e">{total_flow:,.0f} 元</div>
  </div>
  <div class="meta-item">
    <div class="label">流入金额</div>
    <div class="value" style="color:#27ae60">{in_flow:,.0f} 元</div>
  </div>
  <div class="meta-item">
    <div class="label">流出金额</div>
    <div class="value" style="color:#e74c3c">{out_flow:,.0f} 元</div>
  </div>
  <div class="meta-item">
    <div class="label">净流入</div>
    <div class="value" style="color:{'#27ae60' if in_flow>=out_flow else '#e74c3c'}">{in_flow-out_flow:+,.0f} 元</div>
  </div>
  <div class="meta-item">
    <div class="label">上游账户数</div>
    <div class="value">{in_deg}</div>
  </div>
  <div class="meta-item">
    <div class="label">下游账户数</div>
    <div class="value">{out_deg}</div>
  </div>
  <div class="meta-item">
    <div class="label">风险评分</div>
    <div class="value" style="color:{'#e74c3c' if score>=60 else '#e67e22' if score>=30 else '#27ae60'}">{f'{score:.1f}' if score else '-'}</div>
  </div>
  <div class="meta-item">
    <div class="label">关联交易数</div>
    <div class="value">{all_tx_count}</div>
  </div>
</div>

<!-- 上下游 -->
<div class="two-col">
  <section class="card">
    <h2>⬆️ Top 上游（向本人转账最多）</h2>
    <table class="data-table">
      <thead><tr><th>层级</th><th>账号/姓名</th><th style="text-align:right">金额（元）</th></tr></thead>
      <tbody>{neighbor_rows(upstream)}</tbody>
    </table>
  </section>
  <section class="card">
    <h2>⬇️ Top 下游（本人转账最多的对象）</h2>
    <table class="data-table">
      <thead><tr><th>层级</th><th>账号/姓名</th><th style="text-align:right">金额（元）</th></tr></thead>
      <tbody>{neighbor_rows(downstream)}</tbody>
    </table>
  </section>
</div>

<!-- 团伙信息 -->
{comm_html}

<!-- 交易明细 -->
<section class="card">
  <h2>💳 交易明细（显示金额最大 {len(txs)} 条 / 共 {all_tx_count} 条）</h2>
  <table class="data-table">
    <thead>
      <tr>
        <th>时间</th>
        <th>方向</th>
        <th>交易对方</th>
        <th style="text-align:right">金额（元）</th>
        <th>备注</th>
      </tr>
    </thead>
    <tbody>{tx_html}</tbody>
  </table>
</section>

<div class="footer">
  网赌团伙资金链分析系统 · 查询报告 · {now_str}
</div>
</body>
</html>
"""

    # 写文件
    safe_name = re.sub(r"[\\/:*?\"<>|]", "_", display)[:30]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"query_{safe_name}_{ts}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────────────────────────────────────────
def build_analyzer(data_dir: Path | None = None) -> GamblingAnalyzer:
    """构建并返回完整分析对象（支持缓存加速）"""
    selected_dir = data_dir or DATA_DIR
    if selected_dir is None:
        raise ValueError("未指定数据目录。请使用 --data-dir <绝对路径> 或设置 FUNDFLOW_DATA_DIR；仓库不包含真实流水。")
    selected_dir = selected_dir.resolve()
    if not selected_dir.is_dir():
        raise FileNotFoundError(f"数据目录不存在：{selected_dir}")
    import hashlib
    cache_path = OUTPUT_DIR / ("_analysis_cache_" + hashlib.sha256(str(selected_dir).encode('utf-8')).hexdigest()[:16] + ".pkl")
    identity_file = str(IDENTITY_FILE) if IDENTITY_FILE else None

    # ── 优先从缓存加载（秒级） ─────────────────────────────────────
    if cache_path.exists():
        print("[>>] 发现分析缓存，正在快速加载...")
        try:
            az = GamblingAnalyzer.load_cache(str(cache_path),
                                             identity_file=identity_file)
            print("[OK] 已从缓存恢复分析结果，可直接查询。\n"
                  "     （如需重新分析，请删除 output/_analysis_cache.pkl）")
            return az
        except Exception as e:
            print(f"[WARN] 缓存加载失败（{e}），将重新执行全量分析...")

    # ── 无缓存：走完整分析流程 ──────────────────────────────────────
    print("[>>] 正在加载数据并构建分析模型，首次分析约需1-3分钟...")

    # 身份对照表
    identity = IdentityMapper(identity_file)
    if identity_file:
        print(f"[OK] 已加载身份对照表：{len(identity.mapping)} 条映射")

    # 读取账单
    all_dfs = load_folder_bills(str(selected_dir), identity)
    if not all_dfs:
        print("[ERROR] 未读取到任何有效数据，请检查数据目录。")
        sys.exit(1)

    all_tx = pd.concat(all_dfs, ignore_index=True)
    print(f"[OK] 共加载 {len(all_tx)} 条交易记录")

    # 构建分析器
    az = GamblingAnalyzer(all_tx, identity)
    az.run()

    n_suspects = sum(1 for n in az.nodes.values() if n.get("risk_score", 0) >= 30)
    print(f"[OK] 分析完成：{len(az.nodes)} 个节点，{n_suspects} 名嫌疑人（score>=30）")

    # ── 自动保存缓存 ────────────────────────────────────────────────
    try:
        az.save_cache(str(cache_path), identity_file=identity_file)
    except Exception as e:
        print(f"[WARN] 缓存保存失败（{e}），不影响查询功能。")

    return az


def run_query(az: GamblingAnalyzer, query_str: str,
              gen_html: bool = False, top_tx: int = 50):
    """对一个查询词执行查询 + 输出"""
    qobj = PersonQuery(az)
    matched = qobj.search(query_str)

    if matched.empty:
        print(f"\n[!!] 未找到与 '{query_str}' 匹配的账号或人员。")
        print("     请检查：1) 输入是否正确  2) 该人员是否在数据库中  3) 尝试姓名/账号的部分字符")
        return

    if len(matched) > 1:
        print(f"\n[>>] 找到 {len(matched)} 个匹配结果：")
        for i, (_, r) in enumerate(matched.iterrows(), 1):
            print(f"  {i}. {r.get('姓名', '?')}  ({r.get('账号/ID', '?')})  [{r.get('层级', '?')}]  风险评分:{r.get('风险评分', 0)}")
        choice = input("\n请输入序号选择（直接回车查询全部，输入 q 退出）：").strip()
        if choice.lower() == "q":
            return
        if choice.isdigit() and 1 <= int(choice) <= len(matched):
            matched = matched.iloc[[int(choice) - 1]]

    for _, row in matched.iterrows():
        row_dict = row.to_dict()
        print_person_report(qobj, row_dict, top_tx=top_tx)
        if gen_html:
            path = generate_html_report(qobj, row_dict, top_tx=top_tx)
            print(f"[OK] HTML 报告已生成：{path}")


def main():
    parser = argparse.ArgumentParser(
        description="赌球信息单人查询工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python query_person.py 张三
  python query_person.py 13800138000 --html
  python query_person.py zhangsan_wx --top 100 --html
  python query_person.py             # 交互模式
""",
    )
    parser.add_argument("query", nargs="?", help="账号或姓名（留空进入交互模式）")
    parser.add_argument("--html",  action="store_true", help="同时生成 HTML 报告")
    parser.add_argument("--top",   type=int, default=50, help="显示交易条数（默认50）")
    parser.add_argument("--data-dir", type=Path, help="账单目录（必须显式指定绝对路径；仅本机读取）")
    args = parser.parse_args()

    # 构建分析器（统一加载一次）
    az = build_analyzer(args.data_dir.resolve() if args.data_dir else None)

    if args.query:
        run_query(az, args.query, gen_html=args.html, top_tx=args.top)
    else:
        # 交互模式
        print("\n[交互模式] 输入账号或姓名查询，输入 q 退出。")
        while True:
            try:
                q = input("\n请输入查询词（账号 / 姓名）：").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[>>] 退出。")
                break
            if q.lower() in ("q", "quit", "exit"):
                print("[>>] 退出。")
                break
            if not q:
                continue
            gen_html = input("是否生成 HTML 报告？(y/N) ").strip().lower() == "y"
            run_query(az, q, gen_html=gen_html, top_tx=args.top)


if __name__ == "__main__":
    main()
