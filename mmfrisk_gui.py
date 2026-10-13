#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MMFrisk 网赌案件侦查工具 v2.0 — GUI 版
============================================
基于 tkinter + ttkbootstrap 的图形界面
打包命令: pyinstaller mmfrisk_gui.py --name MMFrisk --onedir --windowed --noconfirm
"""

import os
import sys
import io
import threading
import webbrowser
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# ── 路径设置 ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# ── 环境设置 ──────────────────────────────────────────────────────────
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# 仅当目标目录真实存在（打包场景）时才设置 TCL_LIBRARY；
# 开发/venv 下该目录不存在，误设会导致 Tk 找不到 init.tcl、启动失败
_tcl_dir = Path(sys.executable).parent / "tcl" / "tcl8.6"
if "TCL_LIBRARY" not in os.environ and _tcl_dir.is_dir():
    os.environ["TCL_LIBRARY"] = str(_tcl_dir)

import warnings
warnings.filterwarnings('ignore')

# ── GUI 框架 ──────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import ttkbootstrap as tb
from ttkbootstrap.constants import *

# ── 数据分析引擎 ──────────────────────────────────────────────────────
import pandas as pd
from gambling_analysis import (
    GamblingAnalyzer, IdentityMapper, Visualizer,
    load_multiple_folders, scan_folder, extract_owner_from_filename,
    OUTPUT_DIR, BILL_EXTENSIONS, is_identity_file,
)

# ══════════════════════════════════════════════════════════════════════
# 主题 & 常量
# ══════════════════════════════════════════════════════════════════════
APP_TITLE = "MMFrisk 网赌案件侦查系统 v2.0"
ICON_TEXT = "♠"  # 窗口图标（纯文本，无需 ico 文件）

LEVEL_NAMES = {
    1: "一级代理（总代/庄家）",
    2: "二级代理（中级代理）",
    3: "三级代理（基层代理）",
    4: "赌客（会员）",
    5: "待定外围人员",
}
LEVEL_COLORS = {
    1: "#e74c3c", 2: "#e67e22", 3: "#f1c40f", 4: "#3498db", 5: "#95a5a6",
}


# ══════════════════════════════════════════════════════════════════════
# 输出重定向：print → Text 控件
# ══════════════════════════════════════════════════════════════════════
class TextRedirector:
    """将 sys.stdout / sys.stderr 重定向到 tkinter Text 控件"""

    def __init__(self, text_widget: tk.Text, tag: str = "stdout"):
        self.text = text_widget
        self.tag = tag

    def write(self, s: str):
        if not s:
            return
        try:
            self.text.configure(state="normal")
            self.text.insert(tk.END, s, self.tag)
            self.text.see(tk.END)
            self.text.configure(state="disabled")
        except tk.TclError:
            pass

    def flush(self):
        pass


# ══════════════════════════════════════════════════════════════════════
# 主窗口
# ══════════════════════════════════════════════════════════════════════
class MMFriskApp(tb.Window):
    """MMFrisk 主窗口"""

    def __init__(self):
        super().__init__(
            title=APP_TITLE,
            themename="cosmo",       # cosmo / flatly / darkly / solar
            size=(1200, 780),
            minsize=(900, 600),
        )
        self.iconname(ICON_TEXT)

        # ── 状态变量 ──────────────────────────────────────────────────
        self.analyzer: GamblingAnalyzer = None
        self.identity: IdentityMapper = None
        self.suspect_df = None
        self.report_path: str = ""
        self.is_running = False
        self._stop_flag = False
        self._pq = None  # PersonQuery 缓存实例

        # ── 构建界面 ──────────────────────────────────────────────────
        self._build_toolbar()
        self._build_main_area()
        self._build_statusbar()

        # ── 重定向 stdout ────────────────────────────────────────────
        self._redirector = TextRedirector(self.log_text, "stdout")
        sys.stdout = self._redirector
        sys.stderr = TextRedirector(self.log_text, "stderr")

        # ── 欢迎信息 ────────────────────────────────────────────────
        self._log(f"{APP_TITLE}")
        self._log("=" * 60)
        self._log("准备就绪。请选择账单数据文件夹和身份对照表，然后点击 [开始分析]。")
        self._log("")

    # ─────────────────────────────────────────────────────────────
    # 界面构建
    # ─────────────────────────────────────────────────────────────
    def _build_toolbar(self):
        """顶部工具栏"""
        toolbar = tb.Frame(self, bootstyle="primary")
        toolbar.pack(fill=X, padx=8, pady=(8, 2))

        # Logo / 标题
        tb.Label(
            toolbar, text="♠ MMFrisk",
            font=("Microsoft YaHei UI", 16, "bold"),
            bootstyle="inverse-primary"
        ).pack(side=LEFT, padx=(8, 20))

        # 按钮区
        self.btn_load_identity = tb.Button(
            toolbar, text="📋 身份对照表", command=self._on_load_identity,
            bootstyle="secondary", width=14
        )
        self.btn_load_identity.pack(side=LEFT, padx=3)

        self.btn_add_folder = tb.Button(
            toolbar, text="📁 添加账单文件夹", command=self._on_add_folder,
            bootstyle="secondary", width=16
        )
        self.btn_add_folder.pack(side=LEFT, padx=3)

        self.btn_start = tb.Button(
            toolbar, text="🚀 开始分析", command=self._on_start_analysis,
            bootstyle="success", width=14
        )
        self.btn_start.pack(side=LEFT, padx=3)

        self.btn_stop = tb.Button(
            toolbar, text="⏹ 停止", command=self._on_stop,
            bootstyle="danger", width=8, state="disabled"
        )
        self.btn_stop.pack(side=LEFT, padx=3)

        self.btn_open_report = tb.Button(
            toolbar, text="📊 打开报告", command=self._on_open_report,
            bootstyle="info-outline", width=12, state="disabled"
        )
        self.btn_open_report.pack(side=LEFT, padx=3)

        self.btn_open_output = tb.Button(
            toolbar, text="📂 打开输出目录", command=self._on_open_output_dir,
            bootstyle="info-outline", width=14
        )
        self.btn_open_output.pack(side=LEFT, padx=3)

        # 右侧：主题切换
        self._theme_var = tk.StringVar(value="cosmo")
        theme_menu = tb.OptionMenu(
            toolbar, self._theme_var, "cosmo",
            ["cosmo", "flatly", "darkly", "solar", "superhero", "cyborg"],
            command=self._on_theme_change
        )
        theme_menu.pack(side=RIGHT, padx=8)

    def _build_main_area(self):
        """中间主区域：左侧控制面板 + 右侧 Notebook"""
        main = tb.Panedwindow(self, orient=HORIZONTAL)
        main.pack(fill=BOTH, expand=True, padx=8, pady=4)

        # ── 左侧面板 ─────────────────────────────────────────────────
        left = tb.Frame(main, width=340)
        main.add(left, weight=0)

        self._build_left_panel(left)

        # ── 右侧 Notebook ────────────────────────────────────────────
        right = tb.Frame(main)
        main.add(right, weight=1)

        self.notebook = tb.Notebook(right)
        self.notebook.pack(fill=BOTH, expand=True)

        # Tab 1: 日志
        self._build_log_tab()
        # Tab 2: 查询
        self._build_query_tab()
        # Tab 3: 嫌疑人列表
        self._build_suspect_tab()

    def _build_left_panel(self, parent):
        """左侧控制面板"""
        # ── 数据配置 ──────────────────────────────────────────────────
        config_frame = tb.LabelFrame(parent, text=" 数据配置 ")
        config_frame.pack(fill=X, padx=4, pady=4)
        config_inner = tb.Frame(config_frame)
        config_inner.pack(fill=X, padx=10, pady=6)

        # 身份对照表
        tb.Label(config_inner, text="身份对照表（可选）:").pack(anchor="w")
        id_row = tb.Frame(config_inner)
        id_row.pack(fill=X, pady=(0, 6))
        self.lbl_identity = tb.Label(
            id_row, text="未选择", anchor="w",
            bootstyle="secondary", width=28
        )
        self.lbl_identity.pack(side=LEFT, fill=X, expand=True)
        tb.Button(
            id_row, text="…", width=3, command=self._on_load_identity
        ).pack(side=RIGHT, padx=(4, 0))

        # 账单文件夹列表
        tb.Label(config_inner, text="账单文件夹:").pack(anchor="w")
        folder_frame = tb.Frame(config_inner)
        folder_frame.pack(fill=BOTH, expand=True, pady=(0, 6))

        self.folder_listbox = tk.Listbox(
            folder_frame, height=4, font=("Consolas", 9),
            selectbackground="#0078d4"
        )
        scrollbar = tb.Scrollbar(folder_frame, orient=VERTICAL,
                                 command=self.folder_listbox.yview)
        self.folder_listbox.configure(yscrollcommand=scrollbar.set)
        self.folder_listbox.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        btn_row = tb.Frame(config_inner)
        btn_row.pack(fill=X)
        tb.Button(btn_row, text="+ 添加", command=self._on_add_folder,
                  width=10).pack(side=LEFT, padx=2)
        tb.Button(btn_row, text="- 移除", command=self._on_remove_folder,
                  width=10).pack(side=LEFT, padx=2)
        tb.Button(btn_row, text="清空", command=self._on_clear_folders,
                  width=10).pack(side=LEFT, padx=2)

        # ── 数据预览 ──────────────────────────────────────────────────
        preview_frame = tb.LabelFrame(parent, text=" 数据预览 ")
        preview_frame.pack(fill=X, padx=4, pady=4)
        preview_inner = tb.Frame(preview_frame)
        preview_inner.pack(fill=X, padx=10, pady=6)

        self.lbl_stats = tb.Label(
            preview_inner, text="尚未加载数据",
            wraplength=300, justify="left",
            font=("Microsoft YaHei UI", 9)
        )
        self.lbl_stats.pack(anchor="w")

        # ── 分析结果摘要 ──────────────────────────────────────────────
        result_frame = tb.LabelFrame(parent, text=" 分析结果 ")
        result_frame.pack(fill=BOTH, expand=True, padx=4, pady=4)
        result_inner = tb.Frame(result_frame)
        result_inner.pack(fill=BOTH, expand=True, padx=10, pady=6)

        self.result_text = tb.Text(
            result_inner, height=12, font=("Microsoft YaHei UI", 9),
            state="disabled", wrap="word"
        )
        self.result_text.pack(fill=BOTH, expand=True)

    def _build_log_tab(self):
        """日志标签页"""
        log_frame = tb.Frame(self.notebook)
        self.notebook.add(log_frame, text=" 📋 分析日志 ")

        self.log_text = tb.Text(
            log_frame, font=("Consolas", 9),
            state="disabled", wrap="word", bg="#1e1e2e", fg="#cdd6f4"
        )
        self.log_text.tag_configure("stdout", foreground="#cdd6f4")
        self.log_text.tag_configure("stderr", foreground="#f38ba8")
        self.log_text.tag_configure("success", foreground="#a6e3a1")
        self.log_text.tag_configure("warn", foreground="#fab387")
        self.log_text.tag_configure("info", foreground="#89b4fa")

        scrollbar = tb.Scrollbar(log_frame, orient=VERTICAL,
                                 command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

    def _build_query_tab(self):
        """人员查询标签页"""
        query_frame = tb.Frame(self.notebook)
        self.notebook.add(query_frame, text=" 🔍 人员查询 ")

        # 搜索栏
        search_bar = tb.Frame(query_frame)
        search_bar.pack(fill=X, padx=8, pady=8)

        tb.Label(search_bar, text="姓名/账号/手机号:",
                 font=("Microsoft YaHei UI", 10)).pack(side=LEFT, padx=(0, 8))
        self.query_entry = tb.Entry(search_bar, width=30,
                                    font=("Microsoft YaHei UI", 10))
        self.query_entry.pack(side=LEFT, padx=(0, 8))
        self.query_entry.bind("<Return>", lambda e: self._on_query_search())

        tb.Button(search_bar, text="搜索", command=self._on_query_search,
                  bootstyle="primary", width=8).pack(side=LEFT, padx=2)
        tb.Button(search_bar, text="HTML报告", command=self._on_query_html,
                  bootstyle="info-outline", width=10).pack(side=LEFT, padx=2)

        # 搜索结果列表
        list_frame = tb.LabelFrame(query_frame, text=" 搜索结果 ")
        list_frame.pack(fill=BOTH, expand=True, padx=8, pady=(0, 4))
        list_inner = tb.Frame(list_frame)
        list_inner.pack(fill=BOTH, expand=True, padx=4, pady=4)

        columns = ("name", "account", "level", "flow", "score", "ml")
        self.query_tree = tb.Treeview(
            list_inner, columns=columns, show="headings", height=6
        )
        self.query_tree.heading("name", text="姓名")
        self.query_tree.heading("account", text="账号/ID")
        self.query_tree.heading("level", text="层级")
        self.query_tree.heading("flow", text="总流水(元)")
        self.query_tree.heading("score", text="风险评分")
        self.query_tree.heading("ml", text="洗钱嫌疑")

        self.query_tree.column("name", width=80, anchor="w")
        self.query_tree.column("account", width=200, anchor="w")
        self.query_tree.column("level", width=140, anchor="w")
        self.query_tree.column("flow", width=110, anchor="e")
        self.query_tree.column("score", width=70, anchor="center")
        self.query_tree.column("ml", width=70, anchor="center")

        tree_scroll = tb.Scrollbar(list_inner, orient=VERTICAL,
                                   command=self.query_tree.yview)
        self.query_tree.configure(yscrollcommand=tree_scroll.set)
        self.query_tree.pack(side=LEFT, fill=BOTH, expand=True)
        tree_scroll.pack(side=RIGHT, fill=Y)
        self.query_tree.bind("<<TreeviewSelect>>", self._on_query_select)

        # 详情区
        detail_frame = tb.LabelFrame(query_frame, text=" 人员详情 ")
        detail_frame.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))
        detail_inner = tb.Frame(detail_frame)
        detail_inner.pack(fill=BOTH, expand=True, padx=4, pady=4)

        self.query_detail = tb.Text(
            detail_inner, height=10, font=("Microsoft YaHei UI", 9),
            state="disabled", wrap="word", bg="#fafafa"
        )
        detail_scroll = tb.Scrollbar(detail_inner, orient=VERTICAL,
                                     command=self.query_detail.yview)
        self.query_detail.configure(yscrollcommand=detail_scroll.set)
        self.query_detail.pack(side=LEFT, fill=BOTH, expand=True)
        detail_scroll.pack(side=RIGHT, fill=Y)

    def _build_suspect_tab(self):
        """嫌疑人列表标签页"""
        suspect_frame = tb.Frame(self.notebook)
        self.notebook.add(suspect_frame, text=" ⚠️ 嫌疑人排行 ")

        # 工具栏
        tool_bar = tb.Frame(suspect_frame)
        tool_bar.pack(fill=X, padx=8, pady=8)

        tb.Label(tool_bar, text="筛选层级:").pack(side=LEFT, padx=(0, 4))
        self.filter_level = tb.Combobox(
            tool_bar, values=["全部", "一级代理", "二级代理", "三级代理",
                              "赌客", "洗钱嫌疑"],
            state="readonly", width=12
        )
        self.filter_level.set("全部")
        self.filter_level.pack(side=LEFT, padx=(0, 8))
        self.filter_level.bind("<<ComboboxSelected>>", self._on_filter_suspects)

        tb.Button(tool_bar, text="导出 CSV",
                  command=self._on_export_csv,
                  bootstyle="info-outline", width=12).pack(side=RIGHT)

        tb.Button(tool_bar, text="刷新",
                  command=self._on_refresh_suspects,
                  bootstyle="secondary", width=8).pack(side=RIGHT, padx=4)

        # 列表
        columns = ("rank", "name", "account", "level", "in_flow", "out_flow",
                   "score", "ml")
        self.suspect_tree = tb.Treeview(
            suspect_frame, columns=columns, show="headings", height=25
        )
        self.suspect_tree.heading("rank", text="#")
        self.suspect_tree.heading("name", text="姓名")
        self.suspect_tree.heading("account", text="账号/ID")
        self.suspect_tree.heading("level", text="层级")
        self.suspect_tree.heading("in_flow", text="流入(元)")
        self.suspect_tree.heading("out_flow", text="流出(元)")
        self.suspect_tree.heading("score", text="风险评分")
        self.suspect_tree.heading("ml", text="洗钱嫌疑")

        self.suspect_tree.column("rank", width=40, anchor="center")
        self.suspect_tree.column("name", width=80, anchor="w")
        self.suspect_tree.column("account", width=180, anchor="w")
        self.suspect_tree.column("level", width=140, anchor="w")
        self.suspect_tree.column("in_flow", width=110, anchor="e")
        self.suspect_tree.column("out_flow", width=110, anchor="e")
        self.suspect_tree.column("score", width=70, anchor="center")
        self.suspect_tree.column("ml", width=70, anchor="center")

        scroll = tb.Scrollbar(suspect_frame, orient=VERTICAL,
                              command=self.suspect_tree.yview)
        self.suspect_tree.configure(yscrollcommand=scroll.set)
        self.suspect_tree.pack(side=LEFT, fill=BOTH, expand=True, padx=(8, 0), pady=(0, 8))
        scroll.pack(side=RIGHT, fill=Y, padx=(0, 8), pady=(0, 8))

    def _build_statusbar(self):
        """底部状态栏"""
        status = tb.Frame(self, bootstyle="secondary")
        status.pack(fill=X, padx=8, pady=(2, 8))

        self.lbl_status = tb.Label(
            status, text="就绪", bootstyle="secondary",
            font=("Microsoft YaHei UI", 9), anchor="w"
        )
        self.lbl_status.pack(side=LEFT, padx=8, fill=X, expand=True)

        # 进度条
        self.progress = tb.Progressbar(
            status, bootstyle="success-striped", mode="indeterminate", length=200
        )
        self.progress.pack(side=RIGHT, padx=8)

        self.lbl_time = tb.Label(
            status, text="", bootstyle="secondary",
            font=("Consolas", 9)
        )
        self.lbl_time.pack(side=RIGHT, padx=8)

    # ─────────────────────────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────────────────────────
    def _log(self, msg: str, tag: str = "stdout"):
        """向日志区输出一行"""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, line, tag)
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _set_status(self, text: str):
        self.lbl_status.configure(text=text)
        self.update_idletasks()

    def _start_progress(self):
        self.progress.start(15)
        self.btn_stop.configure(state="normal")
        self.btn_start.configure(state="disabled")

    def _stop_progress(self):
        self.progress.stop()
        self.btn_stop.configure(state="disabled")
        self.btn_start.configure(state="normal")

    def _set_result(self, text: str):
        """向左侧结果区写入"""
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert("1.0", text)
        self.result_text.configure(state="disabled")

    def _format_amount(self, val) -> str:
        """格式化金额"""
        try:
            v = float(val)
            if v >= 10_000:
                return f"{v/10_000:,.2f}万"
            return f"{v:,.2f}"
        except (ValueError, TypeError):
            return str(val)

    # ─────────────────────────────────────────────────────────────
    # 事件处理
    # ─────────────────────────────────────────────────────────────
    def _on_load_identity(self):
        """选择身份对照表"""
        path = filedialog.askopenfilename(
            title="选择身份对照表",
            filetypes=[("Excel 文件", "*.xlsx *.xls"), ("所有文件", "*.*")]
        )
        if not path:
            return
        self.lbl_identity.configure(text=Path(path).name)
        self.identity = IdentityMapper(path)

        n_names = len({v for v in self.identity.mapping.values() if v})
        n_total = len(self.identity.mapping)
        self._log(f"✅ 身份对照表已加载: {Path(path).name}", "success")
        self._log(f"   共 {n_total} 条映射, {n_names} 个真实姓名", "info")
        self._set_result(
            f"📋 身份对照表: {Path(path).name}\n"
            f"   映射条数: {n_total}\n"
            f"   真实姓名: {n_names} 人\n"
            f"   身份证: {len(self.identity.id_card)} 条\n"
            f"   手机号: {len(self.identity.phone)} 条\n"
            f"   微信号: {len(self.identity.wechat)} 条\n"
            f"   银行: {len(self.identity.bank)} 条"
        )

    def _on_add_folder(self):
        """添加账单文件夹"""
        path = filedialog.askdirectory(title="选择账单文件夹")
        if not path:
            return
        # 检查是否已添加
        existing = self.folder_listbox.get(0, tk.END)
        if path in existing:
            self._log(f"⚠️ 文件夹已存在: {path}", "warn")
            return
        self.folder_listbox.insert(tk.END, path)

        # 预扫描
        try:
            files = scan_folder(path)
            self._log(f"📁 已添加: {path} ({len(files)} 个账单文件)", "info")
        except Exception as e:
            self._log(f"❌ 扫描失败: {e}", "stderr")

        self._update_folder_stats()

    def _on_remove_folder(self):
        """移除选中文件夹"""
        sel = self.folder_listbox.curselection()
        if sel:
            self.folder_listbox.delete(sel[0])
            self._update_folder_stats()

    def _on_clear_folders(self):
        """清空文件夹列表"""
        self.folder_listbox.delete(0, tk.END)
        self._update_folder_stats()

    def _update_folder_stats(self):
        """更新文件夹统计"""
        folders = self.folder_listbox.get(0, tk.END)
        if not folders:
            self.lbl_stats.configure(text="尚未选择账单文件夹")
            return
        total_files = 0
        for f in folders:
            try:
                total_files += len(scan_folder(f))
            except Exception:
                pass
        self.lbl_stats.configure(
            text=f"已选 {len(folders)} 个文件夹, 预计 {total_files} 个账单文件"
        )

    def _on_theme_change(self, theme_name):
        """切换主题"""
        try:
            self.style.theme_use(theme_name)
            self._theme_var.set(theme_name)
            # 日志区颜色适配暗色主题
            dark_themes = {"darkly", "solar", "superhero", "cyborg"}
            if theme_name in dark_themes:
                self.log_text.configure(bg="#1e1e2e", fg="#cdd6f4",
                                        insertbackground="#cdd6f4")
                self.query_detail.configure(bg="#2d2d3f", fg="#cdd6f4",
                                            insertbackground="#cdd6f4")
            else:
                self.log_text.configure(bg="#fafafa", fg="#333333",
                                        insertbackground="#333333")
                self.query_detail.configure(bg="#fafafa", fg="#333333",
                                            insertbackground="#333333")
        except Exception as e:
            self._log(f"⚠️ 主题切换失败: {e}", "warn")

    def _on_stop(self):
        """停止分析"""
        self._stop_flag = True
        self._log("⏹ 正在停止分析…", "warn")
        self._set_status("正在停止…")

    def _on_open_report(self):
        """打开 HTML 报告"""
        if self.report_path and os.path.isfile(self.report_path):
            webbrowser.open(f"file:///{self.report_path}")
        else:
            messagebox.showinfo("提示", "尚未生成报告，请先完成分析。")

    def _on_open_output_dir(self):
        """打开输出目录"""
        path = str(OUTPUT_DIR)
        os.startfile(path)  # Windows only

    # ─────────────────────────────────────────────────────────────
    # 核心分析流程（后台线程）
    # ─────────────────────────────────────────────────────────────
    def _on_start_analysis(self):
        """点击开始分析"""
        # 验证
        folders = self.folder_listbox.get(0, tk.END)
        if not folders:
            messagebox.showwarning("提示", "请先添加至少一个账单文件夹。")
            return

        self.is_running = True
        self._stop_flag = False
        self._start_progress()
        self.notebook.select(0)  # 切到日志 tab

        # 启动后台线程
        t = threading.Thread(target=self._run_analysis, args=(list(folders),),
                             daemon=True)
        t.start()

        # 计时器
        self._t0 = datetime.now()
        self._tick_timer()

    def _tick_timer(self):
        """更新计时器"""
        if not self.is_running:
            return
        elapsed = (datetime.now() - self._t0).seconds
        m, s = divmod(elapsed, 60)
        self.lbl_time.configure(text=f"⏱ {m:02d}:{s:02d}")
        self.after(1000, self._tick_timer)

    def _run_analysis(self, folder_paths: list):
        """分析主流程（在后台线程执行）"""
        try:
            self._set_status("正在读取账单数据…")
            self._log(f"\n{'='*60}")
            self._log(f"开始分析 ({len(folder_paths)} 个文件夹)", "info")

            # ── 身份对照表 ──
            if self.identity is None:
                self.identity = IdentityMapper()

            # ── 预扫描 ──
            all_preview = []
            for fp in folder_paths:
                try:
                    files = scan_folder(fp)
                    for f in files:
                        all_preview.append((f, fp))
                except Exception as e:
                    self._log(f"❌ 扫描失败 [{fp}]: {e}", "stderr")

            if not all_preview:
                self.after(0, lambda: messagebox.showwarning(
                    "提示", "未找到任何账单文件(.xlsx/.xls/.csv)"))
                self._finish_analysis(success=False)
                return

            self._log(f"📂 预扫描: {len(all_preview)} 个账单文件", "info")
            for fp in folder_paths:
                group_files = [f for f, src in all_preview if src == fp]
                self._log(f"   [{fp}] {len(group_files)} 个文件", "info")

            if self._stop_flag:
                self._finish_analysis(success=False)
                return

            # ── 读取数据 ──
            self._set_status("正在读取账单…")
            self._log(f"\n[READ] 正在读取账单数据…", "info")

            all_dfs = load_multiple_folders(folder_paths, self.identity)

            if not all_dfs:
                self.after(0, lambda: messagebox.showerror(
                    "错误", "未读取到任何有效数据。"))
                self._finish_analysis(success=False)
                return

            import pandas as pd
            all_tx = pd.concat(all_dfs, ignore_index=True)
            n_accounts = all_tx['owner_id'].nunique()

            self._log(f"✅ 共加载 {len(all_tx):,} 条交易记录, "
                      f"{n_accounts} 个主账户", "success")

            if self._stop_flag:
                self._finish_analysis(success=False)
                return

            # ── 运行分析 ──
            self._set_status("正在构建资金网络图…")
            self._log(f"\n[>>>] 开始构建资金流向图…", "info")

            analyzer = GamblingAnalyzer(all_tx, self.identity)
            analyzer.run()

            self._log(f"   图节点: {len(analyzer.G.nodes()):,}, "
                      f"边: {len(analyzer.G.edges()):,}", "info")

            # 诊断
            if 'cp_key' in all_tx.columns:
                cp_ratio = (all_tx['cp_key'].str.strip() != '').mean()
                self._log(f"   cp_key 非空率: {cp_ratio:.1%}", "info")

            if self._stop_flag:
                self._finish_analysis(success=False)
                return

            # ── 嫌疑人清单 ──
            self._set_status("正在生成嫌疑人清单…")
            suspect_df = analyzer.get_suspect_df()

            csv_path = OUTPUT_DIR / 'suspects_ranking.csv'
            try:
                suspect_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            except PermissionError:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                csv_path = OUTPUT_DIR / f'suspects_ranking_{ts}.csv'
                suspect_df.to_csv(csv_path, index=False, encoding='utf-8-sig')

            # ── 生成报告 ──
            self._set_status("正在生成可视化报告…")
            self._log(f"\n[VIZ] 正在生成可视化报告…", "info")

            from gambling_analysis import generate_html_report
            report_path = generate_html_report(analyzer, Visualizer(analyzer), suspect_df)

            # ── 统计结果 ──
            n_ml = suspect_df[suspect_df['洗钱嫌疑'] == '🚨 是'].shape[0] \
                if '洗钱嫌疑' in suspect_df.columns else 0

            # 层级分布
            level_dist = {}
            if '层级' in suspect_df.columns:
                for lv_name in suspect_df['层级'].unique():
                    level_dist[lv_name] = int((suspect_df['层级'] == lv_name).sum())

            summary_lines = [
                f"{'='*40}",
                f"  分析完成 ✅",
                f"{'='*40}",
                f"",
                f"  交易记录: {len(all_tx):,} 条",
                f"  资金节点: {len(analyzer.G.nodes()):,} 个",
                f"  嫌疑人:   {len(suspect_df):,} 人",
                f"  洗钱嫌疑: {n_ml} 人",
                f"",
                f"  ── 层级分布 ──",
            ]
            for lv_name, cnt in sorted(level_dist.items(),
                                       key=lambda x: x[1], reverse=True):
                summary_lines.append(f"  {lv_name}: {cnt} 人")

            summary_lines += [
                f"",
                f"  ── Top 5 高风险 ──",
            ]
            if '风险评分' in suspect_df.columns:
                top5 = suspect_df.nlargest(5, '风险评分')
                for _, row in top5.iterrows():
                    name = row.get('姓名', '?')
                    score = row.get('风险评分', 0)
                    summary_lines.append(f"  {name}: {score:.1f}分")

            summary_text = "\n".join(summary_lines)
            self._log(summary_text, "success")
            self._log(f"\n📊 报告已保存: {report_path}", "success")

            # ── 更新 GUI（主线程） ──
            self.after(0, lambda: self._set_result(summary_text))
            self.after(0, lambda: self.btn_open_report.configure(
                state="normal"))

            # 保存到实例
            self.analyzer = analyzer
            self.suspect_df = suspect_df
            self.report_path = report_path
            self._pq = None  # 重置查询缓存（数据已更新）

            # 刷新嫌疑人列表
            self.after(0, self._on_refresh_suspects)

            # 切到嫌疑人 tab
            self.after(0, lambda: self.notebook.select(2))

            self._finish_analysis(success=True)

        except Exception as e:
            import traceback
            self._log(f"\n❌ 分析失败: {e}", "stderr")
            self._log(traceback.format_exc(), "stderr")
            self.after(0, lambda: messagebox.showerror(
                "分析错误", f"分析过程中发生错误:\n{e}"))
            self._finish_analysis(success=False)

    def _finish_analysis(self, success: bool):
        """分析完成清理"""
        self.is_running = False
        self._stop_flag = False
        self.after(0, self._stop_progress)
        elapsed = (datetime.now() - self._t0).seconds if hasattr(self, '_t0') else 0
        m, s = divmod(elapsed, 60)
        if success:
            self.after(0, lambda: self._set_status(
                f"分析完成 ✅  耗时 {m:02d}:{s:02d}"))
        else:
            self.after(0, lambda: self._set_status("分析已停止/失败"))

    # ─────────────────────────────────────────────────────────────
    # 人员查询
    # ─────────────────────────────────────────────────────────────
    def _on_query_search(self):
        """搜索人员"""
        if self.analyzer is None:
            messagebox.showinfo("提示", "请先完成数据分析。")
            return

        query = self.query_entry.get().strip()
        if not query:
            return

        # 复用 PersonQuery（自动缓存 suspect_df）
        from query_person import PersonQuery
        if self._pq is None:
            self._pq = PersonQuery(self.analyzer)
        results = self._pq.search(query)

        # 清空列表
        for item in self.query_tree.get_children():
            self.query_tree.delete(item)

        if results.empty:
            self._set_detail_text("未找到匹配结果。")
            return

        for _, row in results.iterrows():
            name = str(row.get('姓名', ''))
            account = str(row.get('账号/ID', ''))
            level = str(row.get('层级', ''))
            flow = float(row.get('总流水(元)', 0) or 0)
            score = float(row.get('风险评分', 0) or 0)
            ml = str(row.get('洗钱嫌疑', '否'))

            self.query_tree.insert("", tk.END, iid=account, values=(
                name, account, level,
                self._format_amount(flow),
                f"{score:.1f}",
                "🚨" if ml == "🚨 是" else "否"
            ))

        self._set_detail_text(f"找到 {len(results)} 条匹配结果，点击查看详情。")

    def _on_query_select(self, event=None):
        """选中查询结果时显示详情"""
        sel = self.query_tree.selection()
        if not sel:
            return

        node_id = sel[0]
        # 复用缓存的 PersonQuery（数据一致性 + 性能）
        if not self._pq:
            return
        row_data = self._pq._sdf[self._pq._sdf['账号/ID'] == node_id]

        if row_data.empty:
            self._set_detail_text("未找到详细信息。")
            return

        row = row_data.iloc[0]
        lines = []
        lines.append(f"{'='*50}")
        name = row.get('姓名', node_id)
        lines.append(f"  👤 {name}  ({node_id})")
        need_verify = str(row.get('⚠️需核查', '否')) == '是'
        if need_verify:
            lines.append(f"  [!] 该人员未在身份对照表中登记，信息待核查")
        lines.append(f"{'='*50}")
        lines.append(f"")

        # 基本信息
        for col in ['层级', '身份证号码', '手机号', '微信号', '所属银行']:
            val = row.get(col, '')
            if val and str(val).strip() not in ('', 'nan', 'None', '未知'):
                lines.append(f"  {col}: {val}")

        lines.append(f"")
        lines.append(f"  ── 资金指标 ──")
        for col, label in [('总流水(元)', '总流水'), ('流入金额(元)', '流入'),
                           ('流出金额(元)', '流出'), ('净收入(元)', '净收入')]:
            val = row.get(col, 0)
            lines.append(f"  {label}: {self._format_amount(val)}")

        lines.append(f"")
        lines.append(f"  ── 网络指标 ──")
        for col, label in [('上游账户数', '上游'), ('下游账户数', '下游')]:
            val = row.get(col, 0)
            lines.append(f"  {label}: {int(val)} 个")

        # 风险信息
        score = row.get('风险评分', 0)
        ml = row.get('洗钱嫌疑', '否')
        ml_detail = row.get('洗钱特征', '无')
        lines.append(f"")
        lines.append(f"  ── 风险评估 ──")
        lines.append(f"  风险评分: {float(score):.1f}")
        lines.append(f"  洗钱嫌疑: {ml}")
        if ml == '🚨 是' and str(ml_detail) not in ('无', 'nan', 'None'):
            lines.append(f"  洗钱特征: {ml_detail}")

        # 上下游 & 关联账号 & 社区（复用缓存的 PersonQuery）
        if self.analyzer and self._pq:
            # 关联账号
            aliases = self._pq.get_aliases(node_id)
            if len(aliases) > 1:
                lines.append(f"\n  ── 关联账号（同一人） ──")
                for a in aliases:
                    lines.append(f"  · {a}")

            upstream = self._pq.get_neighbors(node_id, "upstream", top=10)
            downstream = self._pq.get_neighbors(node_id, "downstream", top=10)

            if upstream:
                lines.append(f"\n  ── Top10 上游 ──")
                for i, u in enumerate(upstream, 1):
                    lines.append(f"  {i}. {u['name']} "
                                 f"({self._format_amount(u['amount'])})")

            if downstream:
                lines.append(f"\n  ── Top10 下游 ──")
                for i, d in enumerate(downstream, 1):
                    lines.append(f"  {i}. {d['name']} "
                                 f"({self._format_amount(d['amount'])})")

            # 社区团伙信息
            comm = self._pq.get_community_info(node_id)
            if comm:
                lines.append(f"\n  ── 所在团伙（社区 #{comm['community_id']}） ──")
                lines.append(f"  规模: {comm['size']} 人")
                dist_str = "  ".join(
                    f"{LEVEL_NAMES.get(k, '?')[:4]}x{v}"
                    for k, v in sorted(comm['level_distribution'].items())
                )
                lines.append(f"  层级分布: {dist_str}")
                if comm.get('top_members'):
                    lines.append(f"  核心成员:")
                    for m in comm['top_members']:
                        lines.append(f"    · {m['name']}  评分{m['score']:.1f}")

        self._set_detail_text("\n".join(lines))

    def _on_query_html(self):
        """生成 HTML 查询报告（复用 query_person.py 的完整报告）"""
        if self.analyzer is None:
            messagebox.showinfo("提示", "请先完成数据分析。")
            return

        query = self.query_entry.get().strip()
        if not query:
            messagebox.showinfo("提示", "请输入要查询的姓名或账号。")
            return

        # 在后台线程生成报告
        def gen():
            try:
                self._set_status("正在生成查询报告…")
                from query_person import PersonQuery, generate_html_report
                pq = self._pq if self._pq else PersonQuery(self.analyzer)
                results = pq.search(query)

                if results.empty:
                    self.after(0, lambda: messagebox.showinfo(
                        "提示", "未找到匹配结果。"))
                    return

                # 取第一条（或全部）
                for _, row in results.iterrows():
                    row_dict = row.to_dict()
                    html_path = generate_html_report(pq, row_dict, top_tx=50)
                    self._log(f"📊 查询报告已生成: {html_path}", "success")
                    self._set_status("查询报告已生成")
                    # 自动打开报告
                    webbrowser.open(f"file:///{html_path}")

            except Exception as e:
                import traceback
                self._log(f"❌ 报告生成失败: {e}", "stderr")
                self._log(traceback.format_exc(), "stderr")

        threading.Thread(target=gen, daemon=True).start()

    def _set_detail_text(self, text: str):
        self.query_detail.configure(state="normal")
        self.query_detail.delete("1.0", tk.END)
        self.query_detail.insert("1.0", text)
        self.query_detail.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────
    # 嫌疑人列表
    # ─────────────────────────────────────────────────────────────
    def _on_refresh_suspects(self):
        """刷新嫌疑人列表"""
        if self.suspect_df is None or self.suspect_df.empty:
            return

        # 根据筛选条件过滤
        filter_text = self.filter_level.get()
        df = self.suspect_df.copy()

        if filter_text == "洗钱嫌疑":
            df = df[df.get('洗钱嫌疑', pd.Series()) == '🚨 是']
        elif filter_text != "全部":
            df = df[df.get('层级', pd.Series()) == filter_text]

        # 按风险评分排序
        if '风险评分' in df.columns:
            df = df.sort_values('风险评分', ascending=False)

        # 清空列表
        for item in self.suspect_tree.get_children():
            self.suspect_tree.delete(item)

        # 填充数据
        for rank, (_, row) in enumerate(df.iterrows(), 1):
            name = str(row.get('姓名', ''))
            account = str(row.get('账号/ID', ''))
            level = str(row.get('层级', ''))
            in_flow = float(row.get('流入金额(元)', 0) or 0)
            out_flow = float(row.get('流出金额(元)', 0) or 0)
            score = float(row.get('风险评分', 0) or 0)
            ml = str(row.get('洗钱嫌疑', '否'))

            self.suspect_tree.insert("", tk.END, values=(
                rank, name, account, level,
                self._format_amount(in_flow),
                self._format_amount(out_flow),
                f"{score:.1f}",
                "🚨" if ml == "🚨 是" else "否"
            ))

        self._log(f"📋 嫌疑人列表已刷新: {len(df)} 条记录", "info")

    def _on_filter_suspects(self, event=None):
        self._on_refresh_suspects()

    def _on_export_csv(self):
        """导出嫌疑人列表"""
        if self.suspect_df is None:
            messagebox.showinfo("提示", "请先完成分析。")
            return

        path = filedialog.asksaveasfilename(
            title="导出嫌疑人列表",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")]
        )
        if path:
            self.suspect_df.to_csv(path, index=False, encoding='utf-8-sig')
            self._log(f"✅ 已导出: {path}", "success")
            messagebox.showinfo("成功", f"已导出至:\n{path}")


# ══════════════════════════════════════════════════════════════════════
# 启动
# ══════════════════════════════════════════════════════════════════════
def main():
    import traceback
    try:
        app = MMFriskApp()
        app.mainloop()
    except Exception as e:
        # 全局异常兜底——打包后无控制台，弹窗展示错误
        try:
            import tkinter.messagebox as mb
            mb.showerror("MMFrisk 启动失败",
                         f"程序发生未预期错误:\n\n{e}\n\n{traceback.format_exc()}")
        except Exception:
            # 连 messagebox 都弹不出来，写入文件
            import sys
            err_log = os.path.join(os.environ.get('TEMP', '.'), 'mmfrisk_error.log')
            with open(err_log, 'w', encoding='utf-8') as f:
                f.write(f"{traceback.format_exc()}\n")
            sys.exit(1)


if __name__ == "__main__":
    main()
