import os
import sys
import json
import logging
import argparse
from typing import List

# 確保繁中終端環境正常顯示
# (pythonw / Windows 工作排程器執行時 sys.stdout 與 sys.stderr 為 None，必須防護，否則會在 import 階段直接 crash)
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from src.collector import (
    fetch_period_chip_data,
    fetch_institutional_investors,
    fetch_margin_trading,
    fetch_shareholding_dispersion,
    fetch_volume_profile_and_fundamentals
)
from src.db import ChipDatabase
from src.quant import QuantEngine
from src.reporter import ChipReporter
from src.signals import fmt_price
from src.stock_resolver import StockResolver, load_watchlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("ChipAgent")

def load_config(config_path: str = "config.json") -> dict:
    if not os.path.exists(config_path):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        alt_path = os.path.join(base_dir, config_path)
        if os.path.exists(alt_path):
            config_path = alt_path
        else:
            raise FileNotFoundError(f"找不到設定檔: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def run(stock_ids: List[str] = None, skip_gemini: bool = False, mode: str = "both"):
    config = load_config()
    db_path = config.get("db_path", "data/chips.db")
    windows = config.get("windows", [1, 5, 20, 60, 120, 240])
    gemini_key_path = config.get("gemini_key_path", "")
    broker_tags_path = config.get("broker_tags_path", "data/broker_tags.json")
    output_dir = config.get("output_dir", "reports")
    model_name = config.get("model_name", "gemini-2.5-flash")
    thresholds = config.get("thresholds")
    watchlist_path = config.get("watchlist_path", "watchlist.txt")

    # 1. 初始化股票名稱/代碼識別器、資料庫、量化引擎與報告器
    resolver = StockResolver()
    db = ChipDatabase(db_path)
    quant = QuantEngine(db, tags_path=broker_tags_path, thresholds=thresholds)
    reporter = ChipReporter(key_path=gemini_key_path, model_name=model_name, output_dir=output_dir)

    # 決定要執行的股票清單 (優先使用命令列參數，否則讀取 watchlist.txt，最後保底讀取 config.json)
    if stock_ids:
        target_stocks = []
        for raw_item in stock_ids:
            resolved = resolver.resolve(raw_item)
            if resolved:
                code, name = resolved
                if code not in target_stocks:
                    target_stocks.append(code)
                    logger.info(f"[CLI] 識別成功: 「{raw_item}」 -> {code} ({name})")
            else:
                logger.warning(f"[CLI] 無法識別股票名稱或代碼: 「{raw_item}」，已略過")
    else:
        target_stocks = load_watchlist(watchlist_path, mode=mode, resolver=resolver)
        if not target_stocks:
            target_stocks = config.get("stock_list", ["6782"])

    results_summary = {}

    # 2. 逐一處理目標個股
    for stock_id in target_stocks:
        stock_name = resolver.get_name(stock_id)
        display_label = f"{stock_id} {stock_name}" if stock_name and stock_name != stock_id else stock_id
        logger.info(f"\n==========================================")
        logger.info(f"   開始執行個股分析: {display_label} (模式: {mode})")
        logger.info(f"==========================================")

        # 2.1 自動採集各週期分點籌碼 (並取得分點資料基準日 anchor_date，確保盤中執行時不會混入當日未收盤 K 棒)
        anchor_date = None
        for w in windows:
            logger.info(f"--> [Collector] 抓取 {display_label} 【{w} 日】 券商分點進出資料...")
            p_data = fetch_period_chip_data(stock_id, w)
            if p_data:
                db.save_period_data(p_data)
                if w == 1 and p_data.get("trade_date"):
                    anchor_date = p_data["trade_date"]
            else:
                logger.warning(f"未能取得 {display_label} 【{w} 日】 資料")

        # 2.2 抓取三大法人、融資券、集保大戶、籌碼峰與基本面 (對齊 anchor_date)
        logger.info(f"--> [Collector] 抓取 {display_label} 三大法人、資券、集保大戶與 60日籌碼峰 (基準日: {anchor_date or '最新'})...")
        inst_data = fetch_institutional_investors(stock_id)
        margin_data = fetch_margin_trading(stock_id)
        disp_data = fetch_shareholding_dispersion(stock_id)
        profile_data = fetch_volume_profile_and_fundamentals(stock_id, anchor_date=anchor_date)

        # 2.3 全量量化運算 (真假外資、集保大戶、籌碼峰支撐)
        logger.info(f"--> [Quant] 正在執行多週期量化運算、籌碼峰位階與集保驗證...")
        metrics = quant.analyze_stock(
            stock_id,
            windows,
            institutional_data=inst_data,
            margin_data=margin_data,
            dispersion_data=disp_data,
            profile_data=profile_data
        )
        metrics["stock_name"] = stock_name

        results_summary[stock_id] = {
            "stock_name": stock_name,
            "trade_date": metrics.get("trade_date", ""),
            "reference_price": metrics.get("reference_price", 0.0),
            "warnings": metrics.get("data_warnings", []),
        }

        # 終端列印核心摘要
        ref_p = fmt_price(metrics.get("reference_price", 0.0))
        t_date = metrics.get("trade_date", "")
        print(f"\n【{display_label} 全維度籌碼量化矩陣摘要 (基準日: {t_date}, 收盤價: {ref_p} 元)】")
        if inst_data:
            print(f"三大法人: 外資 {inst_data.get('foreign_buy_net', 0):+,} 張 | 投信 {inst_data.get('investment_trust_net', 0):+,} 張 | 合計 {inst_data.get('institutional_total_net', 0):+,} 張")
        if margin_data:
            print(f"散戶資券: 融資增減 {margin_data.get('margin_diff', 0):+,} 張 | 融券增減 {margin_data.get('short_diff', 0):+,} 張")
        if disp_data:
            print(f"集保大戶: 最新集中度 {disp_data.get('concentration_ratio', 0)}% (週增減 {disp_data.get('concentration_diff', 0):+.2f}%) | 大戶持股 {disp_data.get('major_holder_ratio', 0)}%")
        if profile_data and profile_data.get("poc_low", 0) > 0:
            print(f"籌碼峰: 60日密集換手箱型 (POC) 為 {profile_data['poc_low']} ~ {profile_data['poc_high']} 元 (佔比 {profile_data['poc_volume_ratio']}%) | PE: {profile_data.get('pe_ratio')} 倍")
        if metrics.get("macro_signals"):
            print("宏觀警示: " + " | ".join(metrics["macro_signals"]))

        print(f"{'週期':<8} {'VWAP成本':<10} {'現價乖離':<10} {'前5大買方%':<12} {'前5大賣方%':<12} {'淨集中度%':<10}")
        print("-" * 65)
        for w, m in sorted(metrics.get("windows_metrics", {}).items()):
            print(f"{w:>3} 日   {m['vwap_buy_cost']:>8.2f}   {m['bias_pct']:>+8.2f}%   {m['buy_concentration_top5']:>10.2f}%   {m['sell_concentration_top5']:>10.2f}%   {m['net_concentration']:>+8.2f}%")
        print("-" * 65)

        # 2.4 主力意圖報告產出 (支援日報與週報)
        if not skip_gemini:
            modes_to_run = ["daily", "weekly"] if mode == "both" else [mode]
            for m in modes_to_run:
                report = reporter.generate_report(metrics, mode=m)
                label = "每日戰術快報" if m == "daily" else "週末戰略週報"
                print("\n" + "=" * 50)
                print(f"         【{display_label} {label}】摘要")
                print("=" * 50)
                print(report[:350] + ("..." if len(report) > 350 else ""))
                print("=" * 50 + "\n")
        else:
            logger.info("已略過報告生成 (skip_gemini=True)")

    return results_summary

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TWSE Multi-Period Chip Agent")
    parser.add_argument("--stock", nargs="+", help="指定個股代碼或名稱 (例: 2301 光寶科 視陽)")
    parser.add_argument("--mode", choices=["daily", "weekly", "both"], default="both", help="報告形式: daily(日報), weekly(週報), both(兩者皆產出)")
    parser.add_argument("--skip-gemini", action="store_true", help="跳過 Gemini 分析報告產出")
    args = parser.parse_args()

    run(
        stock_ids=args.stock,
        skip_gemini=args.skip_gemini,
        mode=args.mode
    )
