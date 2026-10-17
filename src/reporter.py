import os
import re
import time
import json
import hashlib
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from bs4 import BeautifulSoup
import markdown
from google import genai
from src.signals import fmt_price, derive_signals, merge_thresholds

logger = logging.getLogger(__name__)

HTML_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>【{stock_id}】多維度籌碼主力意圖診斷看板 - {trade_date}</title>
    <!-- ECharts CDN for lightweight interactive candlestick & volume chart -->
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <style>
        :root {{
            --bg-color: #f1f5f9;
            --card-bg: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --primary: #2563eb;
            --primary-light: #eff6ff;
            --tw-red: #dc2626;
            --tw-red-bg: #fef2f2;
            --tw-red-border: #fecaca;
            --tw-green: #16a34a;
            --tw-green-bg: #f0fdf4;
            --tw-green-border: #bbf7d0;
            --border: #e2e8f0;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang TC", "Microsoft JhengHei", sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            line-height: 1.6;
            margin: 0;
            padding: 24px 12px;
        }}
        .container {{
            max-width: 1040px;
            margin: 0 auto;
        }}
        .header-card {{
            background: var(--card-bg);
            border-radius: 12px;
            padding: 24px 30px;
            margin-bottom: 20px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
            border: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
        }}
        .header-title h1 {{
            margin: 0;
            font-size: 1.7rem;
            color: #0f172a;
        }}
        .header-subtitle {{
            color: var(--text-muted);
            font-size: 0.95rem;
            margin-top: 6px;
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .report-timestamp {{
            color: #1e40af;
            background: #dbeafe;
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 0.88rem;
            display: inline-block;
        }}
        .mode-badge {{
            padding: 2px 9px;
            border-radius: 6px;
            font-size: 0.85rem;
            font-weight: 700;
            display: inline-block;
        }}
        .mode-daily {{
            background: #fef3c7;
            color: #b45309;
            border: 1px solid #fde68a;
        }}
        .mode-weekly {{
            background: #ede9fe;
            color: #6d28d9;
            border: 1px solid #ddd6fe;
        }}
        .sep {{
            color: #cbd5e1;
            margin: 0 2px;
        }}
        .price-badge {{
            background: #f8fafc;
            border: 1px solid var(--border);
            padding: 10px 18px;
            border-radius: 10px;
            text-align: right;
        }}
        .price-label {{
            font-size: 0.8rem;
            color: var(--text-muted);
        }}
        .price-val {{
            font-size: 1.6rem;
            font-weight: 700;
            color: var(--tw-red);
        }}

        /* 3個月 K線圖與成交量樣式 */
        .kline-card {{
            background: var(--card-bg);
            border-radius: 12px;
            padding: 18px 20px 12px 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
            border: 1px solid var(--border);
        }}
        .kline-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 6px;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .kline-title {{
            font-size: 1.05rem;
            font-weight: 700;
            color: #1e293b;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .kline-tags {{
            display: inline-flex;
            gap: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            margin-left: 8px;
        }}
        .k-tag {{
            padding: 1px 6px;
            border-radius: 4px;
        }}
        .k-ma5 {{ background: #fef3c7; color: #b45309; }}
        .k-ma20 {{ background: #dbeafe; color: #1d4ed8; }}
        .k-ma60 {{ background: #f3e8ff; color: #7e22ce; }}
        .kline-note {{
            font-size: 0.8rem;
            color: var(--text-muted);
        }}

        /* 宏觀雷達卡片 (4欄 Grid) */
        .radar-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 20px;
        }}
        @media (max-width: 768px) {{
            .radar-grid {{
                grid-template-columns: 1fr;
            }}
        }}
        .radar-card {{
            background: var(--card-bg);
            border-radius: 12px;
            padding: 18px 22px;
            border: 1px solid var(--border);
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
        }}
        .radar-card-title {{
            font-size: 1.02rem;
            font-weight: 700;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .radar-items {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            text-align: center;
        }}
        .radar-item {{
            background: #f8fafc;
            padding: 8px 10px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .radar-label {{
            font-size: 0.76rem;
            color: var(--text-muted);
            margin-bottom: 2px;
        }}
        .radar-val {{
            font-size: 0.96rem;
            font-weight: 700;
        }}
        .macro-alert-box {{
            background: #fffbeb;
            border: 1px solid #fef3c7;
            border-left: 4px solid #f59e0b;
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 24px;
            font-size: 0.95rem;
            color: #92400e;
            font-weight: 600;
        }}

        /* 總覽表格 */
        .section-card {{
            background: var(--card-bg);
            border-radius: 12px;
            padding: 24px 30px;
            margin-bottom: 24px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
            border: 1px solid var(--border);
        }}
        .section-title {{
            font-size: 1.25rem;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .matrix-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.95rem;
        }}
        .matrix-table th {{
            background: #f8fafc;
            color: #475569;
            font-weight: 600;
            padding: 12px 10px;
            border-bottom: 2px solid var(--border);
            text-align: center;
        }}
        .matrix-table td {{
            padding: 12px 10px;
            border-bottom: 1px solid var(--border);
            text-align: center;
        }}
        .matrix-table tr:hover td {{
            background-color: #f8fafc;
        }}
        .val-red {{
            color: var(--tw-red);
            font-weight: 600;
        }}
        .val-green {{
            color: var(--tw-green);
            font-weight: 600;
        }}

        /* 週期 Tabs 與分點對照卡片 */
        .tabs-header {{
            display: flex;
            gap: 8px;
            margin-bottom: 18px;
            flex-wrap: wrap;
        }}
        .tab-btn {{
            padding: 8px 16px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: #f8fafc;
            color: #475569;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .tab-btn:hover {{
            background: #e2e8f0;
        }}
        .tab-btn.active {{
            background: var(--primary);
            color: #ffffff;
            border-color: var(--primary);
        }}
        .period-panel {{
            display: none;
        }}
        .period-panel.active {{
            display: block;
        }}

        .broker-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }}
        @media (max-width: 768px) {{
            .broker-grid {{
                grid-template-columns: 1fr;
            }}
        }}
        .side-card {{
            border-radius: 10px;
            padding: 18px;
            border: 1px solid var(--border);
        }}
        .side-card.buy {{
            background: var(--tw-red-bg);
            border-color: var(--tw-red-border);
        }}
        .side-card.sell {{
            background: var(--tw-green-bg);
            border-color: var(--tw-green-border);
        }}
        .side-header {{
            font-weight: 700;
            font-size: 1.05rem;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .side-header.buy {{
            color: var(--tw-red);
        }}
        .side-header.sell {{
            color: var(--tw-green);
        }}
        .broker-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
            background: #ffffff;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }}
        .broker-table th {{
            background: #f8fafc;
            padding: 8px 10px;
            font-weight: 600;
            font-size: 0.85rem;
            color: #64748b;
            border-bottom: 1px solid var(--border);
        }}
        .broker-table td {{
            padding: 9px 10px;
            border-bottom: 1px solid #f1f5f9;
        }}
        .broker-table tr:last-child td {{
            border-bottom: none;
        }}
        .badge {{
            display: inline-block;
            font-size: 0.72rem;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 600;
            margin-left: 4px;
            vertical-align: middle;
        }}
        .badge-daytrader {{
            background: #fee2e2;
            color: #b91c1c;
            border: 1px solid #fca5a5;
        }}
        .badge-fake-foreign {{
            background: #fdf4ff;
            color: #a21caf;
            border: 1px solid #f0abfc;
        }}
        .badge-foreign {{
            background: #e0f2fe;
            color: #0369a1;
            border: 1px solid #bae6fd;
        }}
        .badge-gov {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fde68a;
        }}
        .badge-hoarder {{
            background: #eff6ff;
            color: #1d4ed8;
            border: 1px solid #93c5fd;
            font-weight: 700;
        }}
        .badge-swing-buy {{
            background: #faf5ff;
            color: #7e22ce;
            border: 1px solid #d8b4fe;
            font-weight: 700;
        }}
        .badge-swing-sell {{
            background: #fff7ed;
            color: #c2410c;
            border: 1px solid #fed7aa;
            font-weight: 700;
        }}
        .badge-swing-warn {{
            background: #fef2f2;
            color: #b91c1c;
            border: 1px solid #fecaca;
            font-weight: 700;
        }}

        /* 分點雙欄對照表格容器 */
        .table-container {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }}
        @media (max-width: 768px) {{
            .table-container {{
                grid-template-columns: 1fr;
            }}
        }}
        .table-box {{
            background: #ffffff;
            border: 1px solid var(--border);
            border-radius: 10px;
            overflow: hidden;
        }}
        .table-header-title {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 14px;
            font-size: 0.92rem;
            font-weight: 700;
            border-bottom: 1px solid var(--border);
        }}
        .buyer-title {{
            background: #fef2f2;
            color: #b91c1c;
        }}
        .seller-title {{
            background: #f0fdf4;
            color: #15803d;
        }}

        /* 關鍵核心主力分點動態雷達 (Smart Brokers Grid) */
        .smart-brokers-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }}
        @media (max-width: 768px) {{
            .smart-brokers-grid {{
                grid-template-columns: 1fr;
            }}
        }}
        .sb-column {{
            background: #f8fafc;
            border-radius: 10px;
            padding: 16px;
            border: 1px solid var(--border);
        }}
        .sb-col-title {{
            font-size: 1.02rem;
            font-weight: 700;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
        }}
        .sb-list {{
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}
        .sb-item {{
            background: #ffffff;
            padding: 10px 14px;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            display: flex;
            flex-direction: column;
            gap: 4px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
        }}
        .sb-item-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .sb-item-name {{
            font-weight: 700;
            font-size: 0.95rem;
            color: #1e293b;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .sb-item-desc {{
            font-size: 0.84rem;
            color: #64748b;
        }}
        .sb-empty {{
            text-align: center;
            color: #94a3b8;
            font-size: 0.88rem;
            padding: 20px 10px;
        }}

        /* ==================== 視覺語意高亮元件 (UI Enhancement) ==================== */
        .hero-decision,
        .hero-decision-card {{
            background: linear-gradient(135deg, #f8fafc 0%, #edf2f7 100%);
            border: 1px solid #cbd5e1;
            border-left: 6px solid var(--primary);
            border-radius: 12px;
            padding: 18px 22px;
            margin-bottom: 24px;
            box-shadow: 0 3px 12px rgba(0, 0, 0, 0.05);
        }}
        .hero-decision.banner-bull {{
            background: linear-gradient(135deg, #fff5f5 0%, #fee2e2 100%);
            border-color: #fca5a5;
            border-left: 6px solid #dc2626;
        }}
        .hero-decision.banner-caution {{
            background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%);
            border-color: #fcd34d;
            border-left: 6px solid #d97706;
        }}
        .hero-title,
        .hero-decision-title {{
            font-size: 1.08rem;
            font-weight: 800;
            color: #0f172a;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .hero-desc {{
            font-size: 0.94rem;
            color: #334155;
            line-height: 1.65;
        }}
        .hero-decision-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            text-align: center;
        }}
        @media (max-width: 768px) {{
            .hero-decision-grid {{
                grid-template-columns: 1fr 1fr;
            }}
        }}
        .hero-decision-item {{
            background: #ffffff;
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }}
        .hero-decision-lbl {{
            font-size: 0.78rem;
            color: #64748b;
            font-weight: 600;
            margin-bottom: 4px;
        }}
        .hero-decision-val {{
            font-size: 1.05rem;
            font-weight: 800;
        }}

        /* 文字內容與語意高亮標籤 */
        .analysis-content {{
            font-size: 1.02rem;
            color: #334155;
            line-height: 1.85;
        }}
        .analysis-content h2 {{
            color: #0f172a;
            font-size: 1.35rem;
            margin-top: 32px;
            margin-bottom: 14px;
            border-left: 4px solid var(--primary);
            padding-left: 12px;
        }}
        .analysis-content h3 {{
            color: #1e293b;
            font-size: 1.15rem;
            margin-top: 24px;
            margin-bottom: 12px;
        }}
        .analysis-content ul {{
            padding-left: 22px;
        }}
        .analysis-content li {{
            margin-bottom: 10px;
        }}

        /* 語意高亮膠囊 */
        .broker-pill {{
            background: #e0f2fe;
            color: #0369a1;
            padding: 2px 7px;
            border-radius: 6px;
            font-weight: 600;
            border: 1px solid #bae6fd;
            display: inline-block;
            margin: 0 2px;
            font-size: 0.95em;
        }}
        .price-pill {{
            background: #fef3c7;
            color: #92400e;
            padding: 1px 7px;
            border-radius: 6px;
            font-weight: 700;
            border: 1px solid #fde68a;
            display: inline-block;
            margin: 0 2px;
            font-family: monospace, sans-serif;
            font-size: 0.96em;
        }}
        .bullish-tag {{
            color: #dc2626;
            font-weight: 700;
            background: #fef2f2;
            padding: 1px 5px;
            border-radius: 4px;
        }}
        .bearish-tag {{
            color: #16a34a;
            font-weight: 700;
            background: #f0fdf4;
            padding: 1px 5px;
            border-radius: 4px;
        }}
        .val-pos {{
            color: #dc2626;
            font-weight: 700;
        }}
        .val-neg {{
            color: #16a34a;
            font-weight: 700;
        }}
        .alert-pill {{
            background: #fee2e2;
            color: #b91c1c;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 700;
            border: 1px solid #fca5a5;
        }}

        .footer {{
            text-align: center;
            font-size: 0.85rem;
            color: var(--text-muted);
            margin-top: 30px;
            padding-bottom: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- 頂部股票基本資訊 -->
        <div class="header-card">
            <div class="header-title">
                <h1>{report_title}</h1>
                <div class="header-subtitle">
                    {mode_badge_html}
                    <span class="report-timestamp">🕒 產出時間：{generated_at_minute}</span>
                    <span class="sep">｜</span>
                    <span>資料基準日：{trade_date}</span>
                    <span class="sep">｜</span>
                    <span>產業：{sector}</span>
                    <span class="sep">｜</span>
                    <span>分析引擎：{ai_source_label}</span>
                </div>
            </div>
            <div class="price-badge">
                <div class="price-label">{price_label}</div>
                <div class="price-val">{ref_price} 元</div>
            </div>
        </div>

        <!-- 頂部 3 個月 K 線圖與均線成交量 -->
        {kline_section_html}

        <!-- 0. 宏觀籌碼看板 (日報 2欄 / 週報 4欄) -->
        <div class="radar-grid">
            {radar_cards_html}
        </div>

        {macro_alert_html}

        <!-- 1. 多週期量化指標矩陣看板 -->
        <div class="section-card">
            <div class="section-title">{matrix_section_title}</div>
            <table class="matrix-table">
                <thead>
                    <tr>
                        <th>週期</th>
                        <th>主力買超均價 (VWAP)</th>
                        <th>現價乖離率</th>
                        <th>前 5 大買方佔比</th>
                        <th>前 5 大賣方佔比</th>
                        <th>淨集中度 (買-賣)</th>
                        <th>狀態備註</th>
                    </tr>
                </thead>
                <tbody>
                    {matrix_rows}
                </tbody>
            </table>
        </div>

        <!-- 1.5 關鍵核心主力分點動態雷達 (長線囤貨大戶 ✕ 波段神算高手) -->
        {smart_brokers_html}

        <!-- 2. 分點進出紅綠雙欄對照看板 (Tabs) -->
        <div class="section-card">
            <div class="section-title">👥 各週期關鍵主力分點進出對照看板</div>
            <div class="tabs-header">
                {tabs_buttons}
            </div>
            {period_panels}
        </div>

        <!-- 3. AI 深度診斷報告 (含3秒決策Banner與語意高亮) -->
        <div class="section-card">
            <div class="section-title">{ai_section_title}</div>
            {hero_decision_html}
            <div class="analysis-content">
                {ai_content}
            </div>
        </div>

        <div class="footer">
            報告產出時間：{generated_at} ｜ 專案架構：TWSE Multi-Period Chip Agent
        </div>
    </div>

    <script>
        function openTab(tabId) {{
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.period-panel').forEach(p => p.classList.remove('active'));
            const btn = document.getElementById('btn-' + tabId);
            const panel = document.getElementById('panel-' + tabId);
            if (btn && panel) {{
                btn.classList.add('active');
                panel.classList.add('active');
            }}
        }}
    </script>
</body>
</html>
"""

class ChipReporter:
    def __init__(
        self,
        key_path: str,
        model_name: str = "gemini-2.5-flash",
        output_dir: str = "reports",
        db: Any = None,
        ai_policy: str = "cached",
    ):
        self.output_dir = output_dir
        self.model_name = model_name
        self.db = db
        self.ai_policy = ai_policy
        os.makedirs(output_dir, exist_ok=True)
        self.client = self._init_client(key_path) if ai_policy != "local" else None

    def _compute_data_hash(self, metrics: Dict[str, Any], mode: str) -> str:
        """計算核心量化數據指紋 (SHA-256)，確保數據未變動時 100% 命中 SQLite AI 快取 (0 Token)"""
        inst = metrics.get("institutional_summary", {}) or {}
        margin = metrics.get("margin_summary", {}) or {}
        disp = metrics.get("dispersion_summary", {}) or {}
        prof = metrics.get("profile_summary", {}) or {}
        tech = metrics.get("technical_summary", {}) or {}
        smart = metrics.get("smart_brokers", {}) or {}
        w_metrics = metrics.get("windows_metrics", {}) or {}

        target_windows = [1, 5] if mode == "daily" else [1, 5, 20, 60, 120, 240]
        w_fp = {}
        for w in target_windows:
            wm = w_metrics.get(w, {})
            if wm:
                w_fp[w] = {
                    "vwap": round(float(wm.get("vwap_buy_cost", 0.0)), 2),
                    "b1": wm.get("top_buyers", [{}])[0].get("net_shares", 0) if wm.get("top_buyers") else 0,
                    "s1": wm.get("top_sellers", [{}])[0].get("net_shares", 0) if wm.get("top_sellers") else 0,
                }

        payload = {
            "stock_id": metrics.get("stock_id", ""),
            "trade_date": metrics.get("trade_date", ""),
            "mode": mode,
            "ref_price": round(float(metrics.get("reference_price", 0.0)), 2),
            "inst_net": inst.get("institutional_total_net", 0),
            "margin_date": margin.get("iso_date", ""),
            "margin_diff": margin.get("margin_diff", 0),
            "ma5": round(float(tech.get("ma5", 0.0) or 0.0), 2),
            "vol_r": round(float(tech.get("vol_ratio_5", 0.0) or 0.0), 2),
            "swing_masters": [
                (s.get("broker_name"), s.get("role"), s.get("net_5d", 0), s.get("net_1d", 0))
                for s in smart.get("swing_masters", [])[:5]
            ],
            "hoarders": [
                (h.get("broker_name"), h.get("net_shares", 0))
                for h in smart.get("hoarders", [])[:3]
            ],
            "windows": w_fp,
        }
        if mode == "weekly":
            payload["disp_date"] = disp.get("date", "")
            payload["disp_conc"] = disp.get("concentration_ratio", 0.0)
            payload["poc"] = (prof.get("poc_low", 0.0), prof.get("poc_high", 0.0))

        raw_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:24]

    def _init_client(self, key_path: str) -> Optional[genai.Client]:
        if not os.path.exists(key_path):
            logger.error(f"Gemini API Key 檔案不存在: {key_path}")
            return None
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            return genai.Client(api_key=key)
        except Exception as e:
            logger.error(f"初始化 Gemini Client 失敗: {e}")
            return None

    def _format_tags_html(self, tags: List[str]) -> str:
        html = ""
        for t in tags:
            if t == "隔日沖":
                html += '<span class="badge badge-daytrader">⚠️隔日沖</span>'
            elif t == "假外資/短線量化":
                html += '<span class="badge badge-fake-foreign">⚡短線假外資</span>'
            elif t in ["外資", "波段外資"]:
                html += '<span class="badge badge-foreign">🏢波段外資</span>'
            elif t == "官股":
                html += '<span class="badge badge-gov">🏛️官股</span>'
            elif "囤貨" in t or "🔒" in t:
                html += '<span class="badge badge-hoarder">🔒長線囤貨</span>'
            elif "買底" in t:
                html += '<span class="badge badge-swing-buy">🎯買底神手</span>'
            elif "賣頂" in t:
                html += '<span class="badge badge-swing-sell">🎯波段賣頂</span>'
            elif "逃頂" in t or "了結" in t:
                html += '<span class="badge badge-swing-sell">🎯高檔了結</span>'
            elif "破線調節" in t:
                html += '<span class="badge badge-swing-warn">⚠️破線調節</span>'
            elif "波段倒貨" in t:
                html += '<span class="badge badge-swing-warn">⚠️波段倒貨</span>'
        return html

    def enhance_text_semantics(self, html_text: str, broker_names: List[str]) -> str:
        """
        利用 BeautifulSoup 遍歷文字節點，進行完全無害的台股視覺語意色彩高亮
        """
        soup = BeautifulSoup(html_text, "html.parser")
        
        # 整理常見分點名單 (依字元長度降序排序，並用單一正規表示式避免子字串重複包裹)
        sorted_brokers = [b for b in sorted(list(set(broker_names)), key=lambda x: len(x), reverse=True) if len(b) >= 2]
        broker_pattern = re.compile("|".join(re.escape(b) for b in sorted_brokers)) if sorted_brokers else None

        for node in list(soup.find_all(string=True)):
            if node.parent and node.parent.name in ['script', 'style', 'code', 'pre', 'span']:
                continue
            text = str(node)

            # 1. 價格點位高亮 (支援千分位與區間，例如: 1,050 元, 118.29 元, 114.5 ~ 116.6 元)
            text = re.sub(
                r'(\d[\d,]*(?:\.\d+)?(?:\s*~\s*\d[\d,]*(?:\.\d+)?)?\s*元)',
                r'<span class="price-pill">📌 \1</span>',
                text
            )

            # 2. 正負張數 (例如: +281 張, -15 張)
            text = re.sub(r'(\+\d+[\d,]*\s*張)', r'<span class="val-pos">\1</span>', text)
            text = re.sub(r'(-\d+[\d,]*\s*張)', r'<span class="val-neg">\1</span>', text)

            # 3. 正負百分比 (例如: +14.90%, -3.50%)
            text = re.sub(r'(\+\d+(?:\.\d+)?%)', r'<span class="val-pos">\1</span>', text)
            text = re.sub(r'(-\d+(?:\.\d+)?%)', r'<span class="val-neg">\1</span>', text)

            # 4. 券商分點名稱膠囊 (單一 pass 替換，防止「凱基-台北」與「凱基」重疊包裹)
            if broker_pattern:
                text = broker_pattern.sub(lambda m: f'<span class="broker-pill">🏢 {m.group(0)}</span>', text)

            # 5. 多空關鍵詞高亮 (加入否定前綴負向環視，避免「未突破」「不偏多」被誤標)
            text = re.sub(
                r'(?<!未)(?<!不)(?<!無)(?<!沒)(偏多|突破|站穩|多頭|鎖碼|吸籌)',
                r'<span class="bullish-tag">▲ \1</span>',
                text
            )
            text = re.sub(
                r'(?<!未)(?<!不)(?<!無)(?<!沒)(偏空|跌破|承壓|賣壓|套牢|派發|出貨)',
                r'<span class="bearish-tag">▼ \1</span>',
                text
            )

            # 6. 特殊警示標籤與神準主力標籤
            text = text.replace('隔日沖', '<span class="alert-pill">⚠️ 隔日沖</span>')
            text = text.replace('假外資', '<span class="badge badge-fake-foreign">⚡ 假外資</span>')
            text = text.replace('長線囤貨', '<span class="badge badge-hoarder">🔒 長線囤貨</span>')
            text = text.replace('買底神手', '<span class="badge badge-swing-buy">🎯 買底神手</span>')
            text = text.replace('波段賣頂', '<span class="badge badge-swing-sell">🎯 波段賣頂</span>')
            text = text.replace('高檔了結', '<span class="badge badge-swing-sell">🎯 高檔了結</span>')
            text = text.replace('破線調節', '<span class="badge badge-swing-warn">⚠️ 破線調節</span>')
            text = text.replace('波段倒貨', '<span class="badge badge-swing-warn">⚠️ 波段倒貨</span>')

            # 替換節點
            if text != str(node):
                new_node = BeautifulSoup(text, "html.parser")
                node.replace_with(new_node)

        return str(soup)

    def _build_kline_section(self, stock_id: str, kline_data: Optional[Dict[str, Any]]) -> str:
        """生成緊湊型 3 個月日 K 線圖與均線、成交量 (ECharts)"""
        if not kline_data or not kline_data.get("dates"):
            return """
            <div class="kline-card" style="text-align: center; color: var(--text-muted); padding: 20px;">
                📊 暫無近 3 個月日 K 線歷史行情數據
            </div>
            """

        dates = kline_data.get("dates", [])
        candles = kline_data.get("candles", [])
        volumes = kline_data.get("volumes", [])
        ma5 = kline_data.get("ma5", [])
        ma20 = kline_data.get("ma20", [])
        ma60 = kline_data.get("ma60", [])

        dates_json = json.dumps(dates)
        candles_json = json.dumps(candles)
        volumes_json = json.dumps(volumes)
        ma5_json = json.dumps(ma5)
        ma20_json = json.dumps(ma20)
        ma60_json = json.dumps(ma60)

        safe_id = re.sub(r"\W+", "_", str(stock_id))
        chart_id = f"kline_chart_{safe_id}"

        template = """
        <div class="kline-card">
            <div class="kline-header">
                <div class="kline-title">
                    <span>📈</span>
                    <span>【__STOCK_ID__】近 3 個月技術 K 線與均線成交量</span>
                    <span class="kline-tags">
                        <span class="k-tag k-ma5">● MA5</span>
                        <span class="k-tag k-ma20">● MA20(月線)</span>
                        <span class="k-tag k-ma60">● MA60(季線)</span>
                    </span>
                </div>
                <div class="kline-note">
                    紅漲 綠跌 ｜ 滾輪縮放 ｜ 移動游標查看逐日明細
                </div>
            </div>
            <div id="__CHART_ID__" style="width: 100%; height: 320px;"></div>
        </div>

        <script>
        (function() {
            var chartDom = document.getElementById('__CHART_ID__');
            if (!chartDom || typeof echarts === 'undefined') return;
            var myChart = echarts.init(chartDom);

            var dates = __DATES_JSON__;
            var candles = __CANDLES_JSON__;
            var volumes = __VOLUMES_JSON__;
            var ma5 = __MA5_JSON__;
            var ma20 = __MA20_JSON__;
            var ma60 = __MA60_JSON__;

            var volData = volumes.map(function(val, idx) {
                var c = candles[idx];
                var isUp = c ? (c[1] >= c[0]) : true;
                return {
                    value: val,
                    itemStyle: {
                        color: isUp ? '#ef4444' : '#22c55e',
                        borderColor: isUp ? '#ef4444' : '#22c55e'
                    }
                };
            });

            var option = {
                animation: false,
                tooltip: {
                    trigger: 'axis',
                    axisPointer: {
                        type: 'cross',
                        link: [{ xAxisIndex: 'all' }],
                        label: { backgroundColor: '#334155' }
                    },
                    backgroundColor: 'rgba(15, 23, 42, 0.92)',
                    borderColor: '#334155',
                    textStyle: { color: '#ffffff', fontSize: 12 },
                    formatter: function(params) {
                        if (!params || !params.length) return '';
                        var idx = params[0].dataIndex;
                        var d = dates[idx];
                        var c = candles[idx];
                        if (!c) return '';
                        var o = c[0], cl = c[1], l = c[2], h = c[3];
                        var v = volumes[idx] || 0;
                        var prevCl = (idx > 0 && candles[idx - 1]) ? candles[idx - 1][1] : o;
                        var pct = prevCl > 0 ? ((cl - prevCl) / prevCl * 100).toFixed(2) : '0.00';
                        var isUp = cl >= prevCl;
                        var chgColor = isUp ? '#f87171' : '#4ade80';
                        var sign = isUp ? '+' : '';

                        var res = '<div style="font-weight:700; margin-bottom:4px; border-bottom:1px solid #475569; padding-bottom:3px;">' + d + '</div>';
                        res += '開盤: <b>' + o + '</b> ｜ 最高: <b>' + h + '</b><br/>';
                        res += '收盤: <b style="color:' + chgColor + '">' + cl + ' (' + sign + pct + '%)</b> ｜ 最低: <b>' + l + '</b><br/>';
                        res += '成交量: <b>' + v.toLocaleString() + ' 張</b><br/>';
                        
                        var v5 = (ma5[idx] !== null && ma5[idx] !== undefined) ? ma5[idx] : '-';
                        var v20 = (ma20[idx] !== null && ma20[idx] !== undefined) ? ma20[idx] : '-';
                        var v60 = (ma60[idx] !== null && ma60[idx] !== undefined) ? ma60[idx] : '-';
                        res += '<span style="color:#f59e0b">MA5: ' + v5 + '</span> ｜ <span style="color:#60a5fa">MA20: ' + v20 + '</span> ｜ <span style="color:#c084fc">MA60: ' + v60 + '</span>';
                        return res;
                    }
                },
                legend: {
                    data: ['日K', 'MA5', 'MA20', 'MA60'],
                    top: 0,
                    right: 15,
                    textStyle: { color: '#64748b', fontSize: 11 },
                    itemWidth: 14,
                    itemHeight: 8
                },
                grid: [
                    { left: 52, right: 20, top: 26, height: '56%' },
                    { left: 52, right: 20, top: '72%', height: '20%' }
                ],
                xAxis: [
                    {
                        type: 'category',
                        data: dates,
                        boundaryGap: true,
                        axisLine: { lineStyle: { color: '#cbd5e1' } },
                        splitLine: { show: false },
                        axisLabel: { show: false }
                    },
                    {
                        type: 'category',
                        gridIndex: 1,
                        data: dates,
                        boundaryGap: true,
                        axisLine: { lineStyle: { color: '#cbd5e1' } },
                        axisTick: { show: false },
                        splitLine: { show: false },
                        axisLabel: {
                            color: '#64748b',
                            fontSize: 10,
                            formatter: function(val) { return val.substring(5); }
                        }
                    }
                ],
                yAxis: [
                    {
                        scale: true,
                        splitArea: { show: true, areaStyle: { color: ['rgba(248,250,252,0.5)', 'rgba(255,255,255,0.2)'] } },
                        splitLine: { lineStyle: { color: '#f1f5f9' } },
                        axisLabel: { color: '#64748b', fontSize: 11 }
                    },
                    {
                        gridIndex: 1,
                        scale: true,
                        splitNumber: 2,
                        splitLine: { show: false },
                        axisLabel: {
                            color: '#64748b',
                            fontSize: 9,
                            formatter: function(val) {
                                return val >= 1000 ? (val / 1000).toFixed(0) + 'k' : val;
                            }
                        }
                    }
                ],
                dataZoom: [
                    {
                        type: 'inside',
                        xAxisIndex: [0, 1],
                        start: 0,
                        end: 100
                    }
                ],
                series: [
                    {
                        name: '日K',
                        type: 'candlestick',
                        data: candles,
                        itemStyle: {
                            color: '#ef4444',
                            color0: '#22c55e',
                            borderColor: '#ef4444',
                            borderColor0: '#22c55e'
                        }
                    },
                    {
                        name: 'MA5',
                        type: 'line',
                        data: ma5,
                        smooth: true,
                        showSymbol: false,
                        lineStyle: { width: 1.2, color: '#f59e0b' }
                    },
                    {
                        name: 'MA20',
                        type: 'line',
                        data: ma20,
                        smooth: true,
                        showSymbol: false,
                        lineStyle: { width: 1.2, color: '#2563eb' }
                    },
                    {
                        name: 'MA60',
                        type: 'line',
                        data: ma60,
                        smooth: true,
                        showSymbol: false,
                        lineStyle: { width: 1.2, color: '#9333ea' }
                    },
                    {
                        name: '成交量',
                        type: 'bar',
                        xAxisIndex: 1,
                        yAxisIndex: 1,
                        data: volData
                    }
                ]
            };

            myChart.setOption(option);
            window.addEventListener('resize', function() {
                myChart.resize();
            });
        })();
        </script>
        """

        return (
            template
            .replace("__STOCK_ID__", stock_id)
            .replace("__CHART_ID__", chart_id)
            .replace("__DATES_JSON__", dates_json)
            .replace("__CANDLES_JSON__", candles_json)
            .replace("__VOLUMES_JSON__", volumes_json)
            .replace("__MA5_JSON__", ma5_json)
            .replace("__MA20_JSON__", ma20_json)
            .replace("__MA60_JSON__", ma60_json)
        )

    def _build_tag_badges(self, tags: List[str]) -> str:
        return self._format_tags_html(tags)

    def _generate_fallback_markdown(self, metrics: Dict[str, Any], mode: str = "daily") -> str:
        stock_id = metrics.get("stock_id", "")
        stock_name = metrics.get("stock_name", "")
        stock_label = f"{stock_id} {stock_name}" if stock_name and stock_name != stock_id else stock_id
        trade_date = metrics.get("trade_date", "")
        ref_price = metrics.get("reference_price", 0.0)
        ref_p_str = fmt_price(ref_price)
        inst = metrics.get("institutional_summary", {})
        margin = metrics.get("margin_summary", {})
        disp = metrics.get("dispersion_summary", {})
        prof = metrics.get("profile_summary", {})
        w_metrics = metrics.get("windows_metrics", {})
        derived = metrics.get("derived_signals") or derive_signals(metrics, merge_thresholds(None))

        m1 = w_metrics.get(1, {})
        m5 = w_metrics.get(5, {})
        m20 = w_metrics.get(20, {})
        m60 = w_metrics.get(60, {})

        f_net = inst.get("foreign_buy_net", 0)
        t_net = inst.get("investment_trust_net", 0)
        d_net = inst.get("dealer_net", 0)
        total_net = inst.get("institutional_total_net", 0)
        m_diff = margin.get("margin_diff", 0)
        m_bal = margin.get("margin_balance", 0)
        sm_ratio = margin.get("short_margin_ratio", 0.0)

        smart_b = metrics.get("smart_brokers", {})
        hoarders_list = smart_b.get("hoarders", [])
        swing_list = smart_b.get("swing_masters", [])
        h_str = ", ".join([f"{h['broker_name']}({h['period']}日淨買{h['net_shares']:,}張, 純度{h['purity']:.1f}%)" for h in hoarders_list[:3]]) or "無明顯單一囤貨分點"
        s_str = "；".join([f"**{s['broker_name']}**（{s['role']}：{s.get('plain_summary') or s.get('desc', '')}）" for s in swing_list[:3]]) or "無極端轉折動作"

        if mode == "daily":
            vwap_1 = m1.get("vwap_buy_cost", ref_price)
            bias_1 = m1.get("bias_pct", 0.0)
            vwap_5 = m5.get("vwap_buy_cost", ref_price)
            bias_5 = m5.get("bias_pct", 0.0)
            buyers_1 = ", ".join([b["broker_name"] for b in m1.get("top_buyers", [])[:3]]) or "無"
            sellers_1 = ", ".join([s["broker_name"] for s in m1.get("top_sellers", [])[:3]]) or "無"
            flags_1 = "; ".join(m1.get("behavior_flags", [])) if m1.get("behavior_flags") else "短線籌碼無異常集中"

            inst_attitude = derived["inst"]["desc"]
            sediment_desc = derived["sediment"]["desc"]
            vol_info = derived["volume"]
            ma_info = derived["ma"]

            tech = metrics.get("technical_summary", {})
            tech_line = (
                f"* **技術面均線與量能**：5MA {fmt_price(tech.get('ma5', 0))} 元 ｜ "
                f"20MA {fmt_price(tech.get('ma20', 0))} 元 ｜ "
                f"60MA {fmt_price(tech.get('ma60', 0))} 元 ｜ "
                f"成交放量比 {vol_info['text']}。{ma_info['desc']}。"
            )

            # 動態取得隔日沖分點名稱，若無則顯示一般短線獲利了結說明
            dt_names = []
            for w_check in (1, 5):
                for b in w_metrics.get(w_check, {}).get("top_buyers", [])[:5]:
                    if "隔日沖" in b.get("tags", []) or "假外資/短線量化" in b.get("tags", []):
                        if b["broker_name"] not in dt_names:
                            dt_names.append(b["broker_name"])
            dt_target_str = f"隔日沖或短線量化席位（如 {'、'.join(dt_names[:2])}）" if dt_names else "前日短線買盤"

            return f"""# 【{stock_label}】籌碼主力當日戰術快報

**分析日期**：{trade_date} ｜ **{derived['price_label']}**：{ref_p_str} 元

---

### 一、當日多空動態與三大法人態度
* **法人進出總覽**：外資 {f_net:+,} 張、投信 {t_net:+,} 張、自營商 {d_net:+,} 張，合計淨買賣超 {total_net:+,} 張。整體判定為：**{inst_attitude}**。
* **散戶資券消長**：融資單日增減 {m_diff:+,} 張（融資餘額 {m_bal:,} 張，券資比 {sm_ratio:.2f}%）。{sediment_desc}。
{tech_line}

---

### 二、隔日沖主力與關鍵分點監控
* **當日買超前三券商**：{buyers_1}
* **當日賣超前三券商**：{sellers_1}
* **極短線量化特徵**：{flags_1}
* **關鍵主力動態（1 年期歷史波段回測認證）**：
  * **🔒 長線囤貨大戶**：{h_str}
  * **🎯 波段神算高手**：{s_str}
* **短線成本位階**：
  * **1 日主力成本 (VWAP)**：{fmt_price(vwap_1)} 元 (現價乖離 {bias_1:+.2f}%)
  * **5 日主力成本 (VWAP)**：{fmt_price(vwap_5)} 元 (現價乖離 {bias_5:+.2f}%)

---

### 三、明日早盤實戰操盤劇本
1. **情境 A（早盤跳空開高 > 1.5%）**：
   * 需提防{dt_target_str}之獲利了結賣壓。若 9:30 前爆量卻無法守穩當日開盤價，切勿追價，宜逢高減碼。
2. **情境 B（平盤或小幅震盪回檔）**：
   * 以 5 日加權均價 **{fmt_price(vwap_5)} 元** 作為即時防守支撐生命線。若回測不破且量能萎縮，為短線多單安全防守點。
3. **情境 C（帶量破位下跌）**：
   * 若有效跌破 1 日與 5 日成本帶且法人持續賣超，短線多單應嚴格執行停損退場。
"""

        else:  # weekly
            poc_l = prof.get("poc_low", 0.0)
            poc_h = prof.get("poc_high", 0.0)
            poc_ratio = prof.get("poc_volume_ratio", 0.0)
            c_ratio = disp.get("concentration_ratio", 0.0)
            c_diff = disp.get("concentration_diff", 0.0)
            maj_ratio = disp.get("major_holder_ratio", 0.0)

            vwap_20 = m20.get("vwap_buy_cost", ref_price)
            vwap_60 = m60.get("vwap_buy_cost", ref_price)

            return f"""# 【{stock_label}】多維度籌碼主力意圖戰略週報

**分析週基準日**：{trade_date} ｜ **{derived['price_label']}**：{ref_p_str} 元 ｜ **產業**：{prof.get('sector', '台股')}

---

### 一、集保大戶週趨勢與籌碼沉澱驗證
* **集保戶股權分散動態**：最新一週籌碼集中度為 **{c_ratio:.2f}%** (週增減 **{c_diff:+.2f}%**)，千張大戶持股比高達 **{maj_ratio:.2f}%**。
* **籌碼沉澱結構判讀**：{'大戶籌碼持續集中，籌碼向強手沉澱，中長線架構健康' if c_diff >= 0 else '大戶持股比例微幅下滑，需留意部分籌碼分散至散戶端'}。
* **融資對沖格局**：當前融資餘額為 {m_bal:,} 張（單日增減 {m_diff:+,} 張），{derived['sediment']['desc']}。

---

### 二、多週期主力成本帶與 60 日籌碼密集峰 (POC) 交叉盤整
* **60 日籌碼密集換手箱型 (POC)**：核心成交價區間為 **{poc_l:.1f} ~ {poc_h:.1f} 元** (佔近 60 日成交量 {poc_ratio:.1f}%)。
* **關鍵支撐位階**：
  * **波段第一道防線**：20 日主力加權成本 (VWAP) **{fmt_price(vwap_20)} 元**。
  * **波段核心防守帶**：60 日籌碼密集箱型下緣 **{poc_l:.1f} 元** 與 60 日 VWAP **{fmt_price(vwap_60)} 元**。
* **當前位階評判**：{'現價已站穩 60 日密集換手區之上，密集區全數轉化為強力防守支撐底座' if ref_price >= poc_h else ('現價低於密集換手箱型區，上方存在解套套牢賣壓' if ref_price <= poc_l else '現價正於 60 日密集換手箱型內部進行激烈洗盤換手')}。

---

### 三、關鍵券商波段建倉與真假外資意圖
* **關鍵神準主力分點（1 年期歷史波段回測認證）**：
  * **🔒 長線囤貨大戶**：{h_str}
  * **🎯 波段神算高手**：{s_str}
* **技術面與均線結構**：{derived['ma']['desc']}，成交量為 5 日均量之 {derived['volume']['text']}。

---

### 四、下週戰略操盤指引與關鍵防守點位
* **波段多空態勢**：{derived['ma']['desc']}，結合籌碼密集峰位階進行攻守佈局。
* **波段關鍵防守點 (破位停損點)**：**{poc_l:.1f} 元**。若週線收盤跌破此箱型下緣，波段多單宜啟動停損機制。
* **突破加碼點**：帶量突破並站穩 **{poc_h:.1f} 元** (或前高)，為籌碼沉澱完畢、新一輪主升波啟動之加碼訊號。
"""

    def _build_smart_brokers_section(self, smart_brokers: Dict[str, Any]) -> str:
        hoarders = smart_brokers.get("hoarders", [])
        swing_masters = smart_brokers.get("swing_masters", [])

        # 1. 長線囤貨大戶列表
        if hoarders:
            h_items = ""
            for h in hoarders:
                cont_tag = '<span style="color:#2563eb;font-weight:600;font-size:0.78rem;">● 跨週期持續鎖碼</span>' if h.get("continuous") else '<span style="color:#64748b;font-size:0.78rem;">● 獨立波段囤貨</span>'
                purity_val = h.get("purity", 0.0)
                purity_color = "#dc2626" if purity_val >= 80 else "#2563eb"
                h_items += f"""
                <div class="sb-item">
                    <div class="sb-item-header">
                        <div class="sb-item-name">
                            <span>🏢 {h.get('broker_name')}</span>
                            <span class="badge badge-hoarder">🔒長線囤貨</span>
                        </div>
                        <div style="font-size:0.85rem;font-weight:700;color:{purity_color};">
                            純度 {purity_val:.1f}%
                        </div>
                    </div>
                    <div class="sb-item-desc">
                        {h.get('period')}日淨買入 <strong>+{h.get('net_shares', 0):,} 張</strong> (佔比 <strong>{h.get('ratio', 0):.2f}%</strong>) ｜ {cont_tag}
                    </div>
                </div>
                """
        else:
            h_items = """
            <div class="sb-empty">
                🔍 近 120~240 日未見極高純度鎖碼之單一特定分點，籌碼主要由多家常態券商平均持有。
            </div>
            """

        # 2. 波段神算高手列表 (優先呈現 1 年期歷史波段回測認證高手)
        if swing_masters:
            s_items = ""
            for s in swing_masters:
                role_str = s.get("role", "")
                if role_str == "🎯波段賣頂神手":
                    role_badge = '<span class="badge badge-swing-sell">🎯波段賣頂神手 (🚨本波出貨)</span>'
                    action_color = "#dc2626"
                elif role_str == "🎯歷史賣頂高手":
                    role_badge = '<span class="badge badge-swing-sell">🎯歷史賣頂高手 (常駐監控)</span>'
                    action_color = "#c2410c"
                elif role_str == "🎯歷史買底神手":
                    role_badge = '<span class="badge badge-swing-buy">🎯歷史買底神手 (🔥本波抄底)</span>'
                    action_color = "#2563eb"
                elif role_str == "🎯歷史抄底高手":
                    role_badge = '<span class="badge badge-swing-buy">🎯歷史抄底高手 (常駐監控)</span>'
                    action_color = "#7e22ce"
                elif role_str == "🎯買底神手":
                    role_badge = '<span class="badge badge-swing-buy">🎯買底神手</span>'
                    action_color = "#7e22ce"
                elif "賣頂" in role_str:
                    role_badge = '<span class="badge badge-swing-sell">🎯波段賣頂</span>'
                    action_color = "#dc2626"
                elif "破線調節" in role_str:
                    role_badge = '<span class="badge badge-swing-warn">⚠️破線調節</span>'
                    action_color = "#b91c1c"
                elif "波段倒貨" in role_str:
                    role_badge = '<span class="badge badge-swing-warn">⚠️波段倒貨</span>'
                    action_color = "#b91c1c"
                else:
                    role_badge = '<span class="badge badge-swing-sell">🎯高檔了結</span>'
                    action_color = "#c2410c"

                val_display = s.get("val_display") or (
                    f"較均價正乖離 +{s.get('bias_pct', 0.0):.1f}%"
                    if "買底" in role_str
                    else f"{s.get('period')}日賣超佔比 {s.get('ratio', 0.0):.1f}%"
                )

                s_items += f"""
                <div class="sb-item">
                    <div class="sb-item-header">
                        <div class="sb-item-name">
                            <span>🏢 {s.get('broker_name')}</span>
                            {role_badge}
                        </div>
                        <div style="font-size:0.85rem;font-weight:700;color:{action_color};">
                            {val_display}
                        </div>
                    </div>
                    <div class="sb-item-desc">
                        {s.get('desc')}
                    </div>
                </div>
                """
        else:
            s_items = """
            <div class="sb-empty">
                🔍 當前未偵測到極端偏離之波段轉折動作，分點進出維持常態波段換手節奏。
            </div>
            """

        return f"""
        <div class="section-card">
            <div class="section-title">🌟 關鍵核心主力分點動態雷達 (長線囤貨大戶 ✕ 1年歷史認證波段高手)</div>
            <div class="smart-brokers-grid">
                <div class="sb-column">
                    <div class="sb-col-title">
                        <span>🔒 長線囤貨大戶 (中長期鎖碼底氣)</span>
                        <span style="font-size:0.8rem;color:var(--text-muted);font-weight:normal;">120~240日囤貨純度高</span>
                    </div>
                    <div class="sb-list">
                        {h_items}
                    </div>
                </div>
                <div class="sb-column">
                    <div class="sb-col-title">
                        <span>🎯 波段神算高手 (1年期逐日帳本回測認證)</span>
                        <span style="font-size:0.8rem;color:var(--text-muted);font-weight:normal;">歷史波段轉折勝率 ✕ 本波最新動態</span>
                    </div>
                    <div class="sb-list">
                        {s_items}
                    </div>
                </div>
            </div>
        </div>
        """

    def build_dashboard_html(
        self,
        metrics: Dict[str, Any],
        ai_markdown: str,
        mode: str = "daily",
        ai_source_label: str = "Gemini 2.5 Flash",
    ) -> str:
        """組裝包含視覺語意高亮與3秒決策Banner的 HTML 儀表板 (支援日報與週報模式)"""
        stock_id = metrics.get("stock_id", "")
        stock_name = metrics.get("stock_name", "")
        stock_label = f"{stock_id} {stock_name}" if stock_name and stock_name != stock_id else stock_id
        trade_date = metrics.get("trade_date", "")
        ref_price = metrics.get("reference_price", 0.0)
        ref_price_str = fmt_price(ref_price)
        raw_windows_data = sorted(metrics.get("windows_metrics", {}).items())
        derived = metrics.get("derived_signals") or derive_signals(metrics, merge_thresholds(None))

        # 根據日報 / 週報決定顯示的週期範圍
        if mode == "daily":
            active_windows = [1, 5]
            report_title = f"【{stock_label}】籌碼主力當日戰術快報 (Daily)"
            mode_badge_html = '<span class="mode-badge mode-daily">🌅 每日戰術快報</span>'
            matrix_section_title = "⚡ 當日與短線 (1日 / 5日) 主力加權成本與集中度矩陣"
            ai_section_title = "🎯 AI 操盤手明日早盤實戰劇本"
        else:
            active_windows = [5, 20, 60, 120, 240] if any(w in [60, 120] for w, _ in raw_windows_data) else [w for w, _ in raw_windows_data]
            report_title = f"【{stock_label}】多維度籌碼主力意圖戰略週報 (Weekly)"
            mode_badge_html = '<span class="mode-badge mode-weekly">🌌 週末戰略週報</span>'
            matrix_section_title = "📊 多週期主力加權成本階梯 (VWAP) 與集中度矩陣 (全維度)"
            ai_section_title = "🤖 AI 機構籌碼分析師波段戰略深度報告"

        windows_data = [item for item in raw_windows_data if item[0] in active_windows]
        if not windows_data:
            windows_data = raw_windows_data

        inst = metrics.get("institutional_summary", {})
        margin = metrics.get("margin_summary", {})
        disp = metrics.get("dispersion_summary", {})
        prof = metrics.get("profile_summary", {})
        macro_signals = metrics.get("macro_signals", [])

        # 收集所有買賣券商名稱 (供文章自動高亮，含 1 年認證高手與囤貨大戶)
        all_brokers = []
        for w, m in raw_windows_data:
            for b in m.get("top_buyers", []):
                all_brokers.append(b["broker_name"])
            for s in m.get("top_sellers", []):
                all_brokers.append(s["broker_name"])
        for h in metrics.get("smart_brokers", {}).get("hoarders", []):
            all_brokers.append(h["broker_name"])
        for s in metrics.get("smart_brokers", {}).get("swing_masters", []):
            all_brokers.append(s["broker_name"])

        # 1. 三大法人與融資券 (統一使用 derived_signals 確保全報告零矛盾)
        f_net = inst.get("foreign_buy_net", 0)
        t_net = inst.get("investment_trust_net", 0)
        d_net = inst.get("dealer_net", 0)
        total_net = inst.get("institutional_total_net", 0)
        f_ratio = inst.get("foreign_ratio", 0.0)
        inst_ratio = inst.get("institutional_ratio", 0.0)

        m_balance = margin.get("margin_balance", 0)
        m_diff = margin.get("margin_diff", 0)
        s_balance = margin.get("short_balance", 0)
        s_diff = margin.get("short_diff", 0)
        sm_ratio = margin.get("short_margin_ratio", 0.0)

        sediment_status = derived["sediment"]["label"]

        # 2. 集保股權分散
        disp_conc = disp.get("concentration_ratio", 0.0)
        disp_diff = disp.get("concentration_diff", 0.0)
        disp_major = disp.get("major_holder_ratio", 0.0)
        disp_foreign = disp.get("foreign_ratio", 0.0)
        disp_dir = disp.get("director_ratio", 0.0)
        disp_date = disp.get("date", "最新週")

        # 3. 籌碼峰與基本面
        poc_l = prof.get("poc_low", 0.0)
        poc_h = prof.get("poc_high", 0.0)
        poc_ratio = prof.get("poc_volume_ratio", 0.0)
        pe_ratio = prof.get("pe_ratio", 0.0)
        pb_ratio = prof.get("pb_ratio", 0.0)
        yield_pct = prof.get("yield_pct", 0.0)
        sector = prof.get("sector", "台股上市公司")

        poc_status = "整理中"
        if poc_l > 0 and poc_h > 0 and ref_price > 0:
            if ref_price >= poc_h:
                poc_status = "🧱站穩支撐"
            elif ref_price <= poc_l:
                poc_status = "⚠️承壓套牢"

        # 組裝雷達卡片：日報顯示 3 欄 (法人 + 資券 + 技術)，週報顯示完整 5 欄
        card_inst = f"""
            <div class="radar-card">
                <div class="radar-card-title">🏛️ 三大法人單日買賣超</div>
                <div class="radar-items">
                    <div class="radar-item">
                        <div class="radar-label">外資</div>
                        <div class="radar-val {'val-red' if f_net >= 0 else 'val-green'}">{f_net:+,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">投信</div>
                        <div class="radar-val {'val-red' if t_net >= 0 else 'val-green'}">{t_net:+,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">自營商</div>
                        <div class="radar-val {'val-red' if d_net >= 0 else 'val-green'}">{d_net:+,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">法人合計</div>
                        <div class="radar-val {'val-red' if total_net >= 0 else 'val-green'}">{total_net:+,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">外資持股比</div>
                        <div class="radar-val">{f_ratio:.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">法人總持股</div>
                        <div class="radar-val">{inst_ratio:.2f}%</div>
                    </div>
                </div>
            </div>
        """

        card_margin = f"""
            <div class="radar-card">
                <div class="radar-card-title">📈 融資融券散戶動態</div>
                <div class="radar-items">
                    <div class="radar-item">
                        <div class="radar-label">融資餘額</div>
                        <div class="radar-val">{m_balance:,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">融資增減</div>
                        <div class="radar-val {'val-red' if m_diff >= 0 else 'val-green'}">{m_diff:+,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">融券餘額</div>
                        <div class="radar-val">{s_balance:,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">融券增減</div>
                        <div class="radar-val {'val-red' if s_diff >= 0 else 'val-green'}">{s_diff:+,} 張</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">券資比</div>
                        <div class="radar-val">{sm_ratio:.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">籌碼沉澱訊號</div>
                        <div class="radar-val" style="font-size:0.85rem;">{sediment_status}</div>
                    </div>
                </div>
            </div>
        """

        card_disp = f"""
            <div class="radar-card">
                <div class="radar-card-title">📊 集保大戶週持股動態 (TDCC)</div>
                <div class="radar-items">
                    <div class="radar-item">
                        <div class="radar-label">最新集中度</div>
                        <div class="radar-val">{disp_conc:.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">週增減變化</div>
                        <div class="radar-val {'val-red' if disp_diff >= 0 else 'val-green'}">{disp_diff:+.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">大戶持股比</div>
                        <div class="radar-val">{disp_major:.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">外資持股比</div>
                        <div class="radar-val">{disp_foreign:.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">董監持股比</div>
                        <div class="radar-val">{disp_dir:.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">集保週基準日</div>
                        <div class="radar-val" style="font-size:0.82rem;">{disp_date}</div>
                    </div>
                </div>
            </div>
        """

        card_poc = f"""
            <div class="radar-card">
                <div class="radar-card-title">🧱 60日籌碼密集峰 (Volume Profile) & 估值</div>
                <div class="radar-items">
                    <div class="radar-item">
                        <div class="radar-label">密集換手區 (POC)</div>
                        <div class="radar-val" style="color:var(--primary);">{poc_l:.1f}~{poc_h:.1f} 元</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">密集峰成交佔比</div>
                        <div class="radar-val">{poc_ratio:.1f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">本益比 (PE)</div>
                        <div class="radar-val">{pe_ratio:.1f} 倍</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">股價淨值比 (PB)</div>
                        <div class="radar-val">{pb_ratio:.2f}</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">殖利率 (Yield)</div>
                        <div class="radar-val">{yield_pct:.2f}%</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">箱型支撐位階</div>
                        <div class="radar-val" style="font-size:0.85rem;">{poc_status}</div>
                    </div>
                </div>
            </div>
        """

        # 4. 技術面 5MA/20MA/60MA 均線交叉與放量共振卡片
        tech = metrics.get("technical_summary", {})
        t_ma5 = tech.get("ma5", 0.0)
        t_ma20 = tech.get("ma20", 0.0)
        t_ma60 = tech.get("ma60", 0.0)
        t_vol = tech.get("volume", 0)
        vol_r5 = tech.get("vol_ratio_5", 1.0)
        is_surge = tech.get("is_vol_surge", False)
        ma20_up = tech.get("ma20_slope_up", False)
        is_bull_align = tech.get("is_bullish_alignment", False)
        c_5_20 = tech.get("cross_5_20", False)

        tech_pattern_text = derived["ma"]["label"]
        tech_pattern_class = derived["ma"]["css_class"]
        vol_status_text = derived["volume"]["text"]
        vol_status_class = derived["volume"]["css_class"]

        card_tech = f"""
            <div class="radar-card">
                <div class="radar-card-title">⚡ 技術面均線與量能共振</div>
                <div class="radar-items">
                    <div class="radar-item">
                        <div class="radar-label">均線型態</div>
                        <div class="radar-val {tech_pattern_class}" style="font-weight:700;">{tech_pattern_text}</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">成交放量比 (vs 5MA量)</div>
                        <div class="radar-val {vol_status_class}">{vol_status_text}</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">5MA / 20MA (月線)</div>
                        <div class="radar-val">{fmt_price(t_ma5)} / {fmt_price(t_ma20)} 元</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">60MA (季線生命線)</div>
                        <div class="radar-val">{fmt_price(t_ma60)} 元</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">20MA 月線斜率</div>
                        <div class="radar-val {'val-red' if ma20_up else 'val-green'}">{'↗️ 翻揚助漲' if ma20_up else '↘️ 下彎反壓'}</div>
                    </div>
                    <div class="radar-item">
                        <div class="radar-label">當日成交張數</div>
                        <div class="radar-val">{t_vol:,} 張</div>
                    </div>
                </div>
            </div>
        """

        if mode == "daily":
            radar_cards_html = f"{card_inst}\n{card_margin}\n{card_tech}"
        else:
            radar_cards_html = f"{card_inst}\n{card_margin}\n{card_tech}\n{card_disp}\n{card_poc}"

        macro_alert_html = ""
        if macro_signals and mode == "weekly":
            alerts_text = "<br>".join(macro_signals)
            macro_alert_html = f'<div class="macro-alert-box">{alerts_text}</div>'
        elif mode == "daily":
            # 日報顯示短線隔日沖、1年認證波段高手出貨/抄底、法人異常與技術面共振警示
            daily_alerts = [
                s for s in macro_signals
                if any(k in s for k in ("隔日沖", "量化", "法人", "金叉", "多頭排列", "共振", "出量", "沉澱", "警戒", "賣頂", "抄底", "高檔了結", "破線調節"))
            ]
            if daily_alerts:
                macro_alert_html = f'<div class="macro-alert-box">{"<br>".join(daily_alerts)}</div>'

        # 5. 矩陣表格列
        matrix_rows = ""
        w20_vwap = 0.0
        w5_vwap = 0.0
        day_trader_alert = False
        day_trader_brokers = []

        for w, m in windows_data:
            bias = m.get("bias_pct", 0.0)
            bias_class = "val-red" if bias >= 0 else "val-green"
            net_conc = m.get("net_concentration", 0.0)
            conc_class = "val-red" if net_conc >= 0 else "val-green"

            if w == 5:
                w5_vwap = m.get("vwap_buy_cost", 0.0)
            if w == 20:
                w20_vwap = m.get("vwap_buy_cost", 0.0)

            tag_note = ""
            if w in [1, 5] and any("隔日沖" in f for f in m.get("behavior_flags", [])):
                tag_note = '<span style="color:#dc2626;font-weight:600;">⚠️隔日沖重倉</span>'
                day_trader_alert = True
                for b in m.get("top_buyers", [])[:3]:
                    if "隔日沖" in b.get("tags", []) or "假外資/短線量化" in b.get("tags", []):
                        if b["broker_name"] not in day_trader_brokers:
                            day_trader_brokers.append(b["broker_name"])
            elif w in [20, 60] and net_conc > 5.0:
                tag_note = '<span style="color:#2563eb;font-weight:600;">🔒波段鎖碼</span>'

            row = f"""
            <tr>
                <td><strong>{w} 日</strong></td>
                <td>{fmt_price(m.get('vwap_buy_cost', 0.0))} 元</td>
                <td class="{bias_class}">{bias:+.2f}%</td>
                <td>{m.get('buy_concentration_top5', 0.0):.2f}%</td>
                <td>{m.get('sell_concentration_top5', 0.0):.2f}%</td>
                <td class="{conc_class}">{net_conc:+.2f}%</td>
                <td>{tag_note}</td>
            </tr>
            """
            matrix_rows += row

        dt_names_str = f" (如 {'、'.join(day_trader_brokers[:2])})" if day_trader_brokers else ""

        # 6. 英雄決策 Banner (整合籌碼面與技術面共振燈號)
        if mode == "daily":
            if day_trader_alert:
                if c_5_20 or is_bull_align:
                    hero_decision_html = f"""
                    <div class="hero-decision banner-caution">
                        <div class="hero-title">⚠️ 【隔日沖假突破預警】均線型態偏多但隔日沖重倉介入，早盤切勿追高</div>
                        <div class="hero-desc">今日技術面雖呈現均線偏多或放量表態，但前段買超偵測到隔日沖券商{dt_names_str}重倉介入，隔日早盤極易開高走低倒貨！明日以防守為主，觀察回測 5日成本 {fmt_price(w5_vwap)} 元支撐力道。</div>
                    </div>
                    """
                else:
                    hero_decision_html = f"""
                    <div class="hero-decision banner-caution">
                        <div class="hero-title">⚠️ 【明日早盤劇本】短線隔日沖重倉介入，開高慎防急倒貨賣壓</div>
                        <div class="hero-desc">今日 1~5 日短線分點偵測到隔日沖或高頻量化帳戶{dt_names_str}買超佔比偏高。明日早盤開高切勿追價，觀察 9:30 前賣壓釋放力道，並以 5 日主力成本 {fmt_price(w5_vwap)} 元為短線防守線。</div>
                    </div>
                    """
            elif is_bull_align and is_surge and (total_net > 0 or m_diff < 0):
                chip_reason = (
                    f"三大法人大買 {total_net:+,} 張"
                    if total_net > 0
                    else f"融資減少 {abs(m_diff):,} 張、浮額有效沉澱"
                )
                hero_decision_html = f"""
                <div class="hero-decision banner-bull">
                    <div class="hero-title">👑 【技術 ✕ 籌碼共振強攻】三線多頭放量攻擊，籌碼結構同步轉強</div>
                    <div class="hero-desc">現價強勢站上 5MA > 20MA > 60MA，成交量放大至 5MA 量之 {vol_r5:.2f} 倍，且{chip_reason}，多方主升攻擊波確立！</div>
                </div>
                """
            elif c_5_20 and is_surge:
                hero_decision_html = f"""
                <div class="hero-decision banner-bull">
                    <div class="hero-title">🚀 【帶量黃金交叉】5MA 強勢向上穿越月線 (20MA)，短線動能引爆</div>
                    <div class="hero-desc">5MA 向上黃金交叉 20MA，成交量有效放大至 {vol_r5:.2f} 倍攻擊量，宣告短期整理結束。明日早盤回測 5MA ({fmt_price(t_ma5)} 元) 守穩為強勢攻擊點。</div>
                </div>
                """
            elif derived["inst"]["tier"] == "strong_buy":
                hero_decision_html = f"""
                <div class="hero-decision banner-bull">
                    <div class="hero-title">🔥 【當日戰術訊號】三大法人顯著買超，短線多頭動能強勁</div>
                    <div class="hero-desc">今日三大法人合計大舉加碼 {total_net:+,} 張，短線多方掌握盤面主控權。明日早盤回測 1日/5日成本區間不破可持續偏多操作。</div>
                </div>
                """
            else:
                hero_decision_html = f"""
                <div class="hero-decision" style="border-left: 4px solid #64748b; background: #f8fafc;">
                    <div class="hero-title">🔄 【當日戰術訊號】短線多空籌碼交織，維持區間震盪節奏</div>
                    <div class="hero-desc">當前現價 {ref_price_str} 元緊貼短期主力成本，多空雙方互有進退，明日早盤操作宜順勢應對、切忌追高殺低。</div>
                </div>
                """
        else:  # weekly
            swing_list_w = metrics.get("smart_brokers", {}).get("swing_masters", [])
            cert_sell_w = [
                s for s in swing_list_w
                if s.get("is_1y_certified") and s.get("action") == "SELL" and (s.get("net_5d", 0) <= -15 or s.get("net_1d", 0) <= -10)
            ]
            cert_buy_w = [
                s for s in swing_list_w
                if s.get("is_1y_certified") and s.get("action") == "BUY" and (s.get("net_5d", 0) >= 15 or s.get("net_1d", 0) >= 10)
            ]
            cert_watch_w = [s for s in swing_list_w if s.get("is_1y_certified")]
            watch_note = (
                f" ｜ 1年認證高手監控：{'、'.join(s['broker_name'] for s in cert_watch_w[:3])}"
                if cert_watch_w else ""
            )

            if cert_sell_w:
                top_sw = cert_sell_w[0]
                hero_decision_html = f"""
                <div class="hero-decision banner-danger">
                    <div class="hero-title">🚨 【週報波段轉折警報】1 年歷史認證賣頂高手「{top_sw['broker_name']}」本週密集調節出貨！</div>
                    <div class="hero-desc">過去 1 年 {top_sw.get('high_zone_events_count', 0)} 度於波段高點精準逃頂 (回檔勝率 {top_sw.get('sell_win_rate', 0):.0f}%、正價差 +{top_sw.get('spread_pct', 0):.1f}%) 之神準分點，近 5 日再度賣超 {top_sw.get('net_5d', 0):+,} 張。下週切勿盲目追高，嚴守 20D VWAP ({fmt_price(w20_vwap)} 元) 與 60D POC ({poc_l:.1f}~{poc_h:.1f}元) 防線！</div>
                </div>
                """
            elif cert_buy_w:
                top_bw = cert_buy_w[0]
                hero_decision_html = f"""
                <div class="hero-decision banner-bull">
                    <div class="hero-title">🔥 【週報波段抄底訊號】1 年歷史認證買底高手「{top_bw['broker_name']}」本週進場佈局！</div>
                    <div class="hero-desc">具歷史低檔精準建倉基因 (正價差 +{top_bw.get('spread_pct', 0):.1f}%) 之波段高手近 5 日積極買超 {top_bw.get('net_5d', 0):+,} 張，搭配 60 日籌碼密集峰 ({poc_l:.1f}~{poc_h:.1f}元) 形成中長線底部支撐。</div>
                </div>
                """
            elif is_bull_align and poc_l > 0 and ref_price >= poc_h:
                hero_decision_html = f"""
                <div class="hero-decision banner-bull">
                    <div class="hero-title">👑 【波段主升共振訊號】三線多頭排列 ＋ 突破 60 日籌碼密集峰</div>
                    <div class="hero-desc">現價站穩 60 日換手箱型 ({poc_l:.1f}~{poc_h:.1f}元) 之上，且均線呈現 5MA > 20MA > 60MA 完美多頭架構，波段趨勢極度強勢{watch_note}。</div>
                </div>
                """
            elif poc_l > 0 and ref_price >= poc_h:
                hero_decision_html = f"""
                <div class="hero-decision banner-bull">
                    <div class="hero-title">🚀 【大戶戰略訊號】強勢突破 60 日籌碼密集峰，開啟全新波段格局</div>
                    <div class="hero-desc">現價已站穩近 60 日密集換手箱型區 ({poc_l:.1f}~{poc_h:.1f}元) 之上，該區間已由上檔壓力轉化為實質防守鐵板{watch_note}。</div>
                </div>
                """
            elif total_net > 0 and m_diff < 0:
                hero_decision_html = f"""
                <div class="hero-decision banner-bull">
                    <div class="hero-title">🔥 【大戶戰略訊號】極致籌碼沉澱（大戶吸籌 ＋ 散戶退場）</div>
                    <div class="hero-desc">三大法人波段積極買超，同時散戶融資減少，且集保大戶持股高達 {disp_major:.1f}%。籌碼流向強手{watch_note}。</div>
                </div>
                """
            elif poc_l > 0 and ref_price <= poc_l:
                hero_decision_html = f"""
                <div class="hero-decision banner-caution">
                    <div class="hero-title">⚠️ 【大戶戰略警示】現價承壓於 60 日籌碼峰下方，注意解套賣壓</div>
                    <div class="hero-desc">現價位於核心換手箱型區 ({poc_l:.1f}~{poc_h:.1f}元) 之下，上方累積大量套牢籌碼。反彈遇 POC 箱型下緣宜減碼應對{watch_note}。</div>
                </div>
                """
            else:
                hero_decision_html = f"""
                <div class="hero-decision" style="border-left: 4px solid #64748b; background: #f8fafc;">
                    <div class="hero-title">🔄 【大戶戰略訊號】處於核心籌碼密集峰內部，波段震盪洗盤換手</div>
                    <div class="hero-desc">股價正於 60 日換手箱型 ({poc_l:.1f}~{poc_h:.1f}元) 激烈交戰，建議等待帶量突破上緣或回測 20日 VWAP ({fmt_price(w20_vwap)} 元) 止跌再行佈局{watch_note}。</div>
                </div>
                """

        # 6. 組裝分點頁籤 (Tabs)
        tab_buttons = ""
        period_panels = ""
        first_tab = True

        for w, m in windows_data:
            active_cls = "active" if first_tab else ""
            tab_buttons += f'<button class="tab-btn {active_cls}" id="btn-{w}" onclick="openTab(\'{w}\')">{w} 日週期</button>'

            buy_tbody = ""
            for b in m.get("top_buyers", []):
                tags_badge = self._build_tag_badges(b.get("tags", []))
                buy_tbody += f"""
                <tr>
                    <td><strong>#{b.get('rank', '-')}</strong></td>
                    <td>{b.get('broker_name', '')} {tags_badge}</td>
                    <td style="text-align:right;" class="val-red">+{b.get('net_shares', 0):,}</td>
                    <td style="text-align:right;">{b.get('ratio', 0.0):.2f}%</td>
                </tr>
                """

            sell_tbody = ""
            for s in m.get("top_sellers", []):
                tags_badge = self._build_tag_badges(s.get("tags", []))
                sell_tbody += f"""
                <tr>
                    <td><strong>#{s.get('rank', '-')}</strong></td>
                    <td>{s.get('broker_name', '')} {tags_badge}</td>
                    <td style="text-align:right;" class="val-green">-{s.get('net_shares', 0):,}</td>
                    <td style="text-align:right;">{s.get('ratio', 0.0):.2f}%</td>
                </tr>
                """

            panel = f"""
            <div id="panel-{w}" class="period-panel {active_cls}">
                <div class="table-container">
                    <div class="table-box">
                        <div class="table-header-title buyer-title">
                            <span>🔴 前 5 大買超券商</span>
                            <span>買方前五佔比: {m.get('buy_concentration_top5', 0.0):.2f}%</span>
                        </div>
                        <table class="broker-table">
                            <thead>
                                <tr>
                                    <th style="width:12%;">名次</th>
                                    <th>券商分點名稱</th>
                                    <th style="text-align:right;width:25%;">淨買超</th>
                                    <th style="text-align:right;width:20%;">比重</th>
                                </tr>
                            </thead>
                            <tbody>
                                {buy_tbody}
                            </tbody>
                        </table>
                    </div>
                    <div class="table-box">
                        <div class="table-header-title seller-title">
                            <span>🟢 前 5 大賣超券商</span>
                            <span>賣方前五佔比: {m.get('sell_concentration_top5', 0.0):.2f}%</span>
                        </div>
                        <table class="broker-table">
                            <thead>
                                <tr>
                                    <th style="width:12%;">名次</th>
                                    <th>券商分點名稱</th>
                                    <th style="text-align:right;width:25%;">淨賣超</th>
                                    <th style="text-align:right;width:20%;">比重</th>
                                </tr>
                            </thead>
                            <tbody>
                                {sell_tbody}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            """
            period_panels += panel
            first_tab = False

        # 7. 轉換 Markdown 並套用全套視覺語意文字高亮！
        base_ai_html = markdown.markdown(ai_markdown, extensions=["tables", "fenced_code"])
        enhanced_ai_html = self.enhance_text_semantics(base_ai_html, all_brokers)

        now = datetime.now()
        generated_at_minute = now.strftime("%Y-%m-%d %H:%M")
        generated_at_sec = now.strftime("%Y-%m-%d %H:%M:%S")

        kline_section_html = self._build_kline_section(stock_label, prof.get("kline_data"))
        smart_brokers_html = self._build_smart_brokers_section(metrics.get("smart_brokers", {}))

        return HTML_DASHBOARD_TEMPLATE.format(
            stock_id=stock_id,
            trade_date=trade_date,
            ref_price=ref_price_str,
            price_label=derived["price_label"],
            ai_source_label=ai_source_label,
            sector=sector,
            report_title=report_title,
            mode_badge_html=mode_badge_html,
            radar_cards_html=radar_cards_html,
            matrix_section_title=matrix_section_title,
            smart_brokers_html=smart_brokers_html,
            ai_section_title=ai_section_title,
            kline_section_html=kline_section_html,
            generated_at_minute=generated_at_minute,
            macro_alert_html=macro_alert_html,
            matrix_rows=matrix_rows,
            hero_decision_html=hero_decision_html,
            tabs_buttons=tab_buttons,
            period_panels=period_panels,
            ai_content=enhanced_ai_html,
            generated_at=generated_at_sec
        )

    def build_prompt(self, metrics: Dict[str, Any], mode: str = "daily") -> str:
        """高密度結構化 Prompt 生成器 (已針對日報與週報壓縮 ~45% Input Token 並強化 1 年回測高手基因解析)"""
        stock_id = metrics.get("stock_id", "")
        stock_name = metrics.get("stock_name", "")
        stock_label = f"{stock_id} {stock_name}" if stock_name and stock_name != stock_id else stock_id
        trade_date = metrics.get("trade_date", "")
        ref_price = metrics.get("reference_price", 0.0)
        ref_p_str = fmt_price(ref_price)
        inst = metrics.get("institutional_summary", {})
        margin = metrics.get("margin_summary", {})
        disp = metrics.get("dispersion_summary", {})
        prof = metrics.get("profile_summary", {})
        macro_signals = metrics.get("macro_signals", [])
        derived = metrics.get("derived_signals") or derive_signals(metrics, merge_thresholds(None))

        smart_brokers = metrics.get("smart_brokers", {})
        hoarders = smart_brokers.get("hoarders", [])
        swing_masters = smart_brokers.get("swing_masters", [])
        hoarders_summary = ", ".join([f"{h['broker_name']}({h['period']}D+{h['net_shares']:,}張/{h['ratio']}%,純度{h['purity']:.0f}%)" for h in hoarders[:3]]) or "無單一高純度囤貨分點"
        swing_summary = "；".join([f"{s['broker_name']}[{s['role']}: {s.get('plain_summary') or s.get('desc', '')}]" for s in swing_masters[:4]]) or "維持常態換手"

        if mode == "daily":
            m1 = metrics.get("windows_metrics", {}).get(1, {})
            m5 = metrics.get("windows_metrics", {}).get(5, {})
            b1_str = ", ".join([f"{b['broker_name']}(+{b['net_shares']}張/{b['tags']})" for b in m1.get("top_buyers", [])[:3]])
            s1_str = ", ".join([f"{s['broker_name']}(-{s['net_shares']}張/{s['tags']})" for s in m1.get("top_sellers", [])[:3]])
            b5_str = ", ".join([f"{b['broker_name']}(+{b['net_shares']}張/{b['tags']})" for b in m5.get("top_buyers", [])[:3]])
            s5_str = ", ".join([f"{s['broker_name']}(-{s['net_shares']}張/{s['tags']})" for s in m5.get("top_sellers", [])[:3]])

            tech = metrics.get("technical_summary", {})
            tech_desc = (
                f"5MA={fmt_price(tech.get('ma5', 0))}, 20MA={fmt_price(tech.get('ma20', 0))}, 60MA={fmt_price(tech.get('ma60', 0))}, "
                f"量比={derived['volume']['text']}, 均線={derived['ma']['label']}({derived['ma']['desc']})"
            )

            return f"""# 台股當日籌碼戰術快報 ({stock_label} | {trade_date} | {derived['price_label']}:{ref_p_str}元)
[量化事實(嚴禁矛盾)]
- 法人: 外資{inst.get('foreign_buy_net', 0):+,}/投信{inst.get('investment_trust_net', 0):+,}/自營{inst.get('dealer_net', 0):+,}/合計{inst.get('institutional_total_net', 0):+,}張 ({derived['inst']['label']}:{derived['inst']['desc']})
- 資券: 融資{margin.get('margin_diff', 0):+,}張(餘{margin.get('margin_balance', 0):,})/融券{margin.get('short_diff', 0):+,}張 ({derived['sediment']['label']}:{derived['sediment']['desc']})
- 技術: {tech_desc}
- 🔒長線囤貨大戶: {hoarders_summary}
- 🎯1年回測認證波段高手: {swing_summary}
- 1D主力: VWAP={fmt_price(m1.get('vwap_buy_cost', 0.0))}元({m1.get('bias_pct', 0.0):+.2f}%), 買前3:[{b1_str}], 賣前3:[{s1_str}], 警示:{'; '.join(m1.get('behavior_flags', [])) or '無'}
- 5D主力: VWAP={fmt_price(m5.get('vwap_buy_cost', 0.0))}元({m5.get('bias_pct', 0.0):+.2f}%), 買前3:[{b5_str}], 賣前3:[{s5_str}]

請以台股實戰操盤手視角輸出精煉 Markdown (300~420字):
1. **當日多空動態與技術/籌碼共振**(須與上方系統判定一致)
2. **關鍵分點與1年認證波段高手意圖**(點名1年回測高手歷史勝率、前波高低點實績與近5日最新動作，及隔日沖/囤貨大戶動態)
3. **明日早盤實戰操盤劇本**(開高因應、平盤/回檔之5D VWAP即時防守價與停損點)
"""

        else:  # weekly (矩陣式壓縮 Prompt，省下 50% Input Token)
            matrix_lines = ["|週期|買方VWAP(乖離)|買前5%/賣前5%/淨集中%|買超前3|賣超前3|", "|---|---|---|---|---|"]
            for w, m in sorted(metrics.get("windows_metrics", {}).items()):
                vwap = fmt_price(m.get("vwap_buy_cost", 0.0))
                bias = m.get("bias_pct", 0.0)
                b_str = ",".join([f"{b['broker_name']}(+{b['net_shares']})" for b in m.get("top_buyers", [])[:3]])
                s_str = ",".join([f"{s['broker_name']}(-{s['net_shares']})" for s in m.get("top_sellers", [])[:3]])
                matrix_lines.append(
                    f"|{w}D|{vwap}({bias:+.1f}%)|{m.get('buy_concentration_top5',0):.1f}%/{m.get('sell_concentration_top5',0):.1f}%/{m.get('net_concentration',0):+.1f}%|{b_str}|{s_str}|"
                )
            matrix_md = "\n".join(matrix_lines)

            return f"""# 台股機構籌碼戰略週報 ({stock_label} | {trade_date} | {derived['price_label']}:{ref_p_str}元 | {prof.get('sector', '台股')})
[宏觀與1年回測主力結構(嚴禁矛盾)]
- 法人: 外資{inst.get('foreign_buy_net', 0):+,}/投信{inst.get('investment_trust_net', 0):+,}/合計{inst.get('institutional_total_net', 0):+,}張 ({derived['inst']['label']}), 外資持股{inst.get('foreign_ratio', 0)}%
- 資券與集保(TDCC): 融資餘{margin.get('margin_balance', 0):,}張({margin.get('margin_diff', 0):+,}) | 集保集中度{disp.get('concentration_ratio', 0)}%(週增減{disp.get('concentration_diff', 0):+.2f}%), 千張大戶{disp.get('major_holder_ratio', 0)}%
- 60D籌碼密集峰(POC): {prof.get('poc_low', 0)}~{prof.get('poc_high', 0)}元 (量佔比{prof.get('poc_volume_ratio', 0)}%) | 估值: PE={prof.get('pe_ratio', 'N/A')}, PB={prof.get('pb_ratio', 'N/A')}, 殖利率={prof.get('yield_pct', 'N/A')}%
- 🔒長線囤貨大戶(120~240D): {hoarders_summary}
- 🎯1年回測認證波段高手: {swing_summary}
- 系統宏觀訊號: {'; '.join(macro_signals) if macro_signals else '中性'}

[全週期主力成本與集中度矩陣]
{matrix_md}

請以機構籌碼分析師視角輸出結構化 Markdown 週報 (650~850字):
1. **集保大戶週趨勢與融資對沖結構驗證**
2. **多週期主力成本階梯(20/60/120/240D VWAP) ✕ 60日籌碼峰(POC)支撐壓力位階**
3. **關鍵分點深度解析(長線囤貨大戶鎖碼底氣 ✕ 1年歷史認證波段高手之過往高低點戰績與本週最新動向)**
4. **下週波段實戰操盤指引(多空態勢、關鍵破位停損價與突破加碼點)**
"""

    def generate_report(
        self,
        metrics: Dict[str, Any],
        mode: str = "daily",
        force_ai: bool = False,
        ai_policy_override: Optional[str] = None,
    ) -> str:
        """
        生成報告 (支援四層省流機制):
        - ai_policy == 'cached' (預設): 同股、同日、同模式且數據指紋未變動時，100% 命中 SQLite 快取 (0 Token)
        - ai_policy == 'signal_only': 僅當觸發重大籌碼/高手/技術異動時呼叫 Gemini，平淡日自動使用本地零矛盾量化引擎 (0 Token)
        - ai_policy == 'local': 100% 使用本地零矛盾量化引擎 (0 Token)
        """
        mode_label = "每日戰術快報" if mode == "daily" else "週末戰略週報"
        stock_id = metrics.get("stock_id", "stock")
        trade_date = metrics.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
        policy = ai_policy_override or self.ai_policy or "cached"
        data_hash = self._compute_data_hash(metrics, mode)

        logger.info(f"正在生成 {stock_id} 【{mode_label}】 (AI策略: {policy}, 指紋: {data_hash[:8]})...")

        report_markdown = None
        ai_source_label = "本地量化引擎 (0-Token)"

        # Layer 1: 優先檢查 SQLite AI 報告快取 (同日且核心數據指紋未變 -> 0 Token 秒讀)
        if self.db is not None and not force_ai and policy != "local":
            cached_rep = self.db.get_ai_report_cache(stock_id, trade_date, mode, data_hash)
            if cached_rep and cached_rep.get("ai_markdown"):
                report_markdown = cached_rep["ai_markdown"]
                base_lbl = cached_rep.get("ai_source_label", f"Gemini ({self.model_name})")
                ai_source_label = f"{base_lbl} ⚡[SQLite快取複用 0-Token]" if "快取" not in base_lbl else base_lbl
                logger.info(
                    f"⚡ [AI Cache Hit] {stock_id} ({mode}, {trade_date}) 數據指紋未變動，"
                    f"直接載入 SQLite 快取報告 (節省 100% Gemini Token)"
                )

        # Layer 4: 根據 ai_policy 決定是否呼叫 Gemini API
        if not report_markdown:
            should_call_ai = False
            if policy == "local" or not self.client:
                should_call_ai = False
                ai_source_label = "本地量化引擎 (Local 0-Token)"
            elif policy == "signal_only":
                mat = metrics.get("material_signal", {})
                if mat.get("is_material", False) or mode == "weekly":
                    should_call_ai = True
                    logger.info(
                        f"🔥 [Signal Triggered] {stock_id} 觸發重大訊號 ({', '.join(mat.get('reasons', ['週報深度解析']))})，啟動 Gemini 深度分析"
                    )
                else:
                    should_call_ai = False
                    ai_source_label = "本地量化引擎 (平淡日智慧省流 0-Token)"
                    logger.info(f"🌱 [Signal Quiet] {stock_id} 今日無極端主力/技術異動，自動採用本地零矛盾量化快報 (0 Token)")
            else:  # 'cached'
                should_call_ai = True

            if should_call_ai and self.client:
                prompt = self.build_prompt(metrics, mode=mode)
                for attempt in range(1, 3):
                    try:
                        response = self.client.models.generate_content(
                            model=self.model_name,
                            contents=prompt
                        )
                        report_markdown = response.text
                        ai_source_label = f"Gemini ({self.model_name})"
                        if self.db is not None and report_markdown:
                            self.db.save_ai_report_cache(
                                stock_id, trade_date, mode, data_hash, ai_source_label, report_markdown
                            )
                        break
                    except Exception as e:
                        logger.warning(f"Gemini API 呼叫第 {attempt} 次遇到異常: {e}")
                        if attempt < 2:
                            time.sleep(2)
                        else:
                            logger.info("自動切換為本地量化專業報告產生器。")

        if not report_markdown:
            report_markdown = self._generate_fallback_markdown(metrics, mode=mode)
            if self.db is not None and policy == "signal_only":
                self.db.save_ai_report_cache(
                    stock_id, trade_date, mode, data_hash, ai_source_label, report_markdown
                )

        date_clean = trade_date.replace("-", "")

        # 依各股代碼與月份分類存放 (例如: reports/2301/2026-10/)
        month_str = trade_date[:7] if len(trade_date) >= 7 and "-" in trade_date else datetime.now().strftime("%Y-%m")
        target_dir = os.path.join(self.output_dir, stock_id, month_str)
        os.makedirs(target_dir, exist_ok=True)

        file_prefix = "daily_report" if mode == "daily" else "weekly_report"
        dashboard_html = self.build_dashboard_html(
            metrics,
            report_markdown,
            mode=mode,
            ai_source_label=ai_source_label,
        )
        html_path = os.path.join(target_dir, f"{file_prefix}_{stock_id}_{date_clean}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(dashboard_html)

        logger.info(f"【{mode_label}】已成功產出 HTML 儀表板: {html_path}")
        return report_markdown
