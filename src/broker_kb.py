import os
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.collector import fetch_broker_daily_ledger
from src.db import ChipDatabase
from src.signals import fmt_price

logger = logging.getLogger(__name__)


def _safe_filename(name: str) -> str:
    """將券商分點名稱轉為安全的檔案名稱"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name).strip())
    return cleaned or "unknown_broker"


def _next_day_str(date_str: str) -> str:
    """回傳 YYYY-MM-DD 的隔日字串"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date_str


class BrokerKnowledgeBase:
    """
    關鍵大戶分點知識庫與增量驗證引擎 (brokers/{stock_id}_{stock_name}/*.md)
    核心功能：
    1. 針對每檔個股之「1年認證波段神算高手 (Top 5)」與「長線囤貨鎖碼大戶 (Top 3)」建立獨立 .md 檔案與總覽檔。
    2. 增量帳本同步：記錄 last_recorded_date，後續執行僅抓取未記錄的新交易日，避免重複回溯整年歷史。
    3. 判讀邏輯自我驗證 (Prediction & Verification Loop)：
       - 記錄每次大戶觸發買賣訊號時的「當下判讀立約」
       - 自動對照後續 T+5 / T+10 / T+20 交易日實際股價表現，蓋章驗證：
         ✅ 精準逃頂/抄底、⚠️ 左側提早調節(先軋後跌)、❌ 賣飛被軋/破線套牢、⏳ 追蹤驗證中
       - 將驗證結果反向注入量化引擎與日報/週報，動態修正警報權重與實戰解讀措辭。
    """

    def __init__(self, db: ChipDatabase, base_dir: str = "brokers"):
        self.db = db
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _get_stock_folder(self, stock_id: str, stock_name: str) -> str:
        safe_name = _safe_filename(stock_name) if stock_name and stock_name != stock_id else ""
        folder_name = f"{stock_id}_{safe_name}" if safe_name else stock_id
        folder_path = os.path.join(self.base_dir, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        return folder_path

    def _read_md_meta(self, md_path: str) -> Dict[str, Any]:
        """從既有 .md 檔案讀取內嵌的 BROKER_KB_META JSON 狀態標籤"""
        if not os.path.exists(md_path):
            return {}
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                head = f.read(2048)
            m = re.search(r"<!--\s*BROKER_KB_META:\s*(\{.*?\})\s*-->", head, re.S)
            if m:
                return json.loads(m.group(1))
        except Exception as e:
            logger.debug(f"讀取 {md_path} META 失敗: {e}")
        return {}

    def _build_price_df(self, kline_1y: List[Dict[str, Any]]) -> pd.DataFrame:
        """建立包含 20日高低位階 (pct_20d)、5MA、20MA 及未來 5/10/20 日表現之日 K DataFrame"""
        if not kline_1y:
            return pd.DataFrame()
        df = pd.DataFrame(kline_1y).copy()
        if "date" not in df.columns:
            return pd.DataFrame()
        if "Close" not in df.columns and "close" in df.columns:
            df["Close"] = df["close"]
        if "Close" not in df.columns:
            return pd.DataFrame()

        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
        df["Close"] = df["Close"].astype(float)
        if "High" in df.columns:
            df["High"] = df["High"].astype(float)
        elif "high" in df.columns:
            df["High"] = df["high"].astype(float)
        else:
            df["High"] = df["Close"]

        if "Low" in df.columns:
            df["Low"] = df["Low"].astype(float)
        elif "low" in df.columns:
            df["Low"] = df["low"].astype(float)
        else:
            df["Low"] = df["Close"]
        df["ma5"] = df["Close"].rolling(5, min_periods=1).mean().round(2)
        df["ma20"] = df["Close"].rolling(20, min_periods=1).mean().round(2)

        roll_min = df["Close"].rolling(20, min_periods=5).min()
        roll_max = df["Close"].rolling(20, min_periods=5).max()
        denom = (roll_max - roll_min).replace(0, np.nan)
        df["pct_20d"] = (((df["Close"] - roll_min) / denom) * 100.0).fillna(50.0).round(1)
        return df

    def _sync_broker_incremental_ledger(
        self,
        stock_id: str,
        broker_name: str,
        bhid: str,
        b_code: str,
        start_1y: str,
        anchor_date: str,
        md_meta: Dict[str, Any],
        force_refresh: bool = False,
    ) -> Tuple[List[Dict[str, Any]], str, int]:
        """
        同步單一分點之逐日交易帳本：
        - 若資料庫與 .md 已記錄至 last_recorded_date，僅向網路抓取 (last_recorded_date + 1 day) ~ anchor_date 之增量區間！
        - 回傳 (完整 1 年逐日交易列表, 同步狀態描述, 本次新增交易日數)
        """
        db_last = self.db.get_broker_ledger_last_date(stock_id, broker_name)
        meta_last = md_meta.get("last_recorded_date")
        existing_rows = self.db.get_broker_daily_ledger_rows(stock_id, broker_name, start_1y, anchor_date)

        # 若無 bhid/b_code，嘗試從資料庫已知代碼表反查
        if not bhid or not b_code:
            known_map = self.db.get_known_broker_codes(stock_id)
            if broker_name in known_map:
                bhid, b_code = known_map[broker_name]

        new_days_count = 0
        sync_mode_str = "完整建檔"

        if not force_refresh and existing_rows and db_last:
            if db_last >= anchor_date:
                sync_mode_str = f"⚡ 快取同步 (已紀錄至 {db_last}，0 網路請求)"
                return existing_rows, sync_mode_str, 0
            else:
                fetch_from = _next_day_str(db_last)
                if bhid and b_code and fetch_from <= anchor_date:
                    logger.info(
                        f"⚡ [Broker KB 增量更新] {stock_id} 「{broker_name}」既有紀錄至 {db_last}，"
                        f"僅增量抓取 {fetch_from} ~ {anchor_date} 未記錄區間..."
                    )
                    inc_trades = fetch_broker_daily_ledger(stock_id, bhid, b_code, fetch_from, anchor_date)
                    if inc_trades:
                        self.db.save_broker_daily_ledger_rows(stock_id, broker_name, bhid, b_code, inc_trades)
                        new_days_count = len([t for t in inc_trades if t["date"] > db_last])
                    sync_mode_str = f"⚡ 增量更新 ({db_last} ➔ {anchor_date}，新增 {new_days_count} 筆活躍交易日)"
                    all_rows = self.db.get_broker_daily_ledger_rows(stock_id, broker_name, start_1y, anchor_date)
                    return all_rows, sync_mode_str, new_days_count

        # 初次建檔或強制重刷：抓取過去 1 年完整逐日帳本並存入 SQLite
        if bhid and b_code:
            logger.info(f"--> [Broker KB 初次建檔] 調閱 {stock_id} 「{broker_name}」({start_1y} ~ {anchor_date}) 逐日交易帳本...")
            full_trades = fetch_broker_daily_ledger(stock_id, bhid, b_code, start_1y, anchor_date)
            if full_trades:
                self.db.save_broker_daily_ledger_rows(stock_id, broker_name, bhid, b_code, full_trades)
                new_days_count = len(full_trades)
                sync_mode_str = f"🆕 初次完整建檔 (共載入 {new_days_count} 個活躍交易日)"
                all_rows = self.db.get_broker_daily_ledger_rows(stock_id, broker_name, start_1y, anchor_date)
                return all_rows, sync_mode_str, new_days_count

        return existing_rows, "⚠️ 無分點明細代碼 (僅匯總排行紀錄)", 0

    def _cluster_wave_milestones(
        self, merged: pd.DataFrame
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        從合併後之 1 年逐日帳本與 K 線中，聚類出：
        1. 波段高檔調節/賣出戰役 (sell_events)
        2. 波段低檔抄底/建倉戰役 (buy_events)
        """
        sell_days = merged[merged["net"] < 0].copy()
        buy_days = merged[merged["net"] > 0].copy()

        maj_sell_thr = max(8.0, float(np.percentile(np.abs(sell_days["net"]), 50))) if len(sell_days) >= 3 else 5.0
        maj_sells = sell_days[sell_days["net"] <= -maj_sell_thr].copy()
        sell_events: List[Dict[str, Any]] = []
        for idx, row in maj_sells.iterrows():
            if not sell_events or (int(idx) - sell_events[-1]["last_idx"]) > 5:
                sell_events.append({
                    "type": "SELL",
                    "start_date": str(row["date"]),
                    "end_date": str(row["date"]),
                    "start_idx": int(idx),
                    "last_idx": int(idx),
                    "active_days": 1,
                    "total_net": int(row["net"]),
                    "abs_lots": abs(int(row["net"])),
                    "weighted_cost_num": abs(int(row["net"])) * float(row["Close"]),
                    "min_close": round(float(row["Close"]), 2),
                    "max_close": round(float(row["Close"]), 2),
                    "end_close": round(float(row["Close"]), 2),
                    "max_pct_20d": round(float(row["pct_20d"]), 1),
                })
            else:
                ev = sell_events[-1]
                ev["end_date"] = str(row["date"])
                ev["last_idx"] = int(idx)
                ev["active_days"] += 1
                ev["total_net"] += int(row["net"])
                ev["abs_lots"] += abs(int(row["net"]))
                ev["weighted_cost_num"] += abs(int(row["net"])) * float(row["Close"])
                ev["min_close"] = round(min(ev["min_close"], float(row["Close"])), 2)
                ev["max_close"] = round(max(ev["max_close"], float(row["Close"])), 2)
                ev["end_close"] = round(float(row["Close"]), 2)
                ev["max_pct_20d"] = round(max(ev["max_pct_20d"], float(row["pct_20d"])), 1)

        for ev in sell_events:
            ev["vwap"] = round(ev["weighted_cost_num"] / ev["abs_lots"], 2) if ev["abs_lots"] > 0 else ev["end_close"]

        maj_buy_thr = max(8.0, float(np.percentile(buy_days["net"], 50))) if len(buy_days) >= 3 else 5.0
        maj_buys = buy_days[buy_days["net"] >= maj_buy_thr].copy()
        buy_events: List[Dict[str, Any]] = []
        for idx, row in maj_buys.iterrows():
            if not buy_events or (int(idx) - buy_events[-1]["last_idx"]) > 5:
                buy_events.append({
                    "type": "BUY",
                    "start_date": str(row["date"]),
                    "end_date": str(row["date"]),
                    "start_idx": int(idx),
                    "last_idx": int(idx),
                    "active_days": 1,
                    "total_net": int(row["net"]),
                    "abs_lots": abs(int(row["net"])),
                    "weighted_cost_num": abs(int(row["net"])) * float(row["Close"]),
                    "min_close": round(float(row["Close"]), 2),
                    "max_close": round(float(row["Close"]), 2),
                    "end_close": round(float(row["Close"]), 2),
                    "min_pct_20d": round(float(row["pct_20d"]), 1),
                })
            else:
                ev = buy_events[-1]
                ev["end_date"] = str(row["date"])
                ev["last_idx"] = int(idx)
                ev["active_days"] += 1
                ev["total_net"] += int(row["net"])
                ev["abs_lots"] += abs(int(row["net"]))
                ev["weighted_cost_num"] += abs(int(row["net"])) * float(row["Close"])
                ev["min_close"] = round(min(ev["min_close"], float(row["Close"])), 2)
                ev["max_close"] = round(max(ev["max_close"], float(row["Close"])), 2)
                ev["end_close"] = round(float(row["Close"]), 2)
                ev["min_pct_20d"] = round(min(ev["min_pct_20d"], float(row["pct_20d"])), 1)

        for ev in buy_events:
            ev["vwap"] = round(ev["weighted_cost_num"] / ev["abs_lots"], 2) if ev["abs_lots"] > 0 else ev["end_close"]

        return sell_events, buy_events

    def _evaluate_and_verify_signals(
        self,
        stock_id: str,
        broker_name: str,
        role_type: str,
        role_label: str,
        merged: pd.DataFrame,
        sell_events: List[Dict[str, Any]],
        buy_events: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        核心邏輯自我驗證引擎 (Prediction & Self-Verification Loop)：
        1. 針對該分點的主力定位 (PEAK_SNIPER / BOTTOM_BUYER / HOARDER)，提取所有歷史波段訊號日與近期報告基準日之觸發紀錄。
        2. 對照訊號發出後 T+5、T+10、T+20 交易日之實際收盤價與最高順風/逆風幅度，自動蓋章驗收：
           - ✅ 精準逃頂 / ✅ 精準抄底
           - ⚠️ 左側早賣(先軋後跌) / ⚠️ 左側早買(先蹲後跳)
           - ❌ 賣飛被軋(指標鈍化) / ❌ 抄底套牢(破線失準)
           - ⏳ 追蹤驗證中 (含即時順風/逆風損益提示)
        """
        if merged.empty:
            return []

        date_to_idx = {str(r["date"]): int(i) for i, r in merged.iterrows()}
        n_total = len(merged)
        latest_date = str(merged.iloc[-1]["date"])
        latest_close = float(merged.iloc[-1]["Close"])

        candidate_signals: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # A. 納入過去 1 年關鍵波段戰役事件 (依分點定位優先納入代表性戰役，每個獨立波段取 1 個代表立約點，避免同一波連續多日重複洗版)
        target_events = []
        if role_type == "PEAK_SNIPER":
            target_events.extend(sell_events[-12:])
            target_events.extend([e for e in buy_events if e.get("min_pct_20d", 50) <= 35.0][-4:])
        else:
            target_events.extend(buy_events[-12:])
            target_events.extend([e for e in sell_events if e.get("max_pct_20d", 50) >= 75.0][-4:])

        for ev in target_events:
            sig_date = ev["end_date"]
            sig_type = ev["type"]
            if sig_date not in date_to_idx:
                continue
            idx = date_to_idx[sig_date]
            sub_5d = merged.iloc[max(0, idx - 4) : idx + 1]
            net_5d = int(sub_5d["net"].sum())
            net_1d = int(merged.iloc[idx]["net"])
            trig_price = float(ev.get("vwap") or merged.iloc[idx]["Close"])
            if sig_type == "SELL":
                pct_20d = float(ev.get("max_pct_20d", merged.iloc[idx]["pct_20d"]))
                hyp = (
                    f"波段({ev['start_date'][5:]}~{ev['end_date'][5:]})累計賣超 {ev['total_net']:,}張 "
                    f"(均價 {fmt_price(ev['vwap'])}元)，判讀：高檔調節見頂警訊"
                )
            else:
                pct_20d = float(ev.get("min_pct_20d", merged.iloc[idx]["pct_20d"]))
                hyp = (
                    f"波段({ev['start_date'][5:]}~{ev['end_date'][5:]})累計買超 +{ev['total_net']:,}張 "
                    f"(均價 {fmt_price(ev['vwap'])}元)，判讀：低檔建倉抄底支撐"
                )

            candidate_signals[(sig_date, sig_type)] = {
                "signal_date": sig_date,
                "signal_type": sig_type,
                "start_idx": int(ev.get("start_idx", idx)),
                "trigger_price": trig_price,
                "trigger_net_5d": net_5d if abs(net_5d) >= abs(ev["total_net"]) else int(ev["total_net"]),
                "trigger_net_1d": net_1d,
                "pct_20d": pct_20d,
                "hypothesis_text": hyp,
            }

            # 若該波段為正在進行中的最新波段 (距離最新交易日 <= 5 日) 且跨度 >= 3 個交易日，
            # 額外記錄該波段的「前次立約起點/中繼點 (例如 3~5 個交易日前的報告日)」，以檢驗本波是否出現「左側早賣被軋」或「左側早買」
            if (n_total - 1 - idx) <= 2 and (ev["last_idx"] - ev["start_idx"]) >= 2:
                # 尋找距離最新日 2~5 個交易日前、曾大額出手的交易日 (例如 10/19 或波段起點)
                mid_candidates = [
                    i_cand for i_cand in range(ev["start_idx"], max(ev["start_idx"] + 1, idx - 1))
                    if (sig_type == "SELL" and int(merged.iloc[i_cand]["net"]) < 0)
                    or (sig_type == "BUY" and int(merged.iloc[i_cand]["net"]) > 0)
                ]
                if mid_candidates:
                    # 優先挑選距離最新日約 3 個交易日前的立約點 (如 10/19)，或波段首日
                    best_mid_idx = min(mid_candidates, key=lambda x: abs((n_total - 1 - x) - 3))
                    mid_date = str(merged.iloc[best_mid_idx]["date"])
                    if mid_date != sig_date:
                        m_sub5 = merged.iloc[max(0, best_mid_idx - 4) : best_mid_idx + 1]
                        m_net5 = int(m_sub5["net"].sum())
                        m_net1 = int(merged.iloc[best_mid_idx]["net"])
                        m_price = float(merged.iloc[best_mid_idx]["Close"])
                        m_pct20 = float(merged.iloc[best_mid_idx]["pct_20d"])
                        if sig_type == "SELL":
                            m_hyp = f"本波前次立約({mid_date[5:]})近5日連賣 {m_net5:+,}張 (當日 {m_net1:+,}張)，檢驗短線是否提早左側調節"
                        else:
                            m_hyp = f"本波前次立約({mid_date[5:]})近5日連買 {m_net5:+,}張 (當日 {m_net1:+,}張)，檢驗短線是否提早左側承接"
                        candidate_signals[(mid_date, sig_type)] = {
                            "signal_date": mid_date,
                            "signal_type": sig_type,
                            "trigger_price": m_price,
                            "trigger_net_5d": m_net5,
                            "trigger_net_1d": m_net1,
                            "pct_20d": m_pct20,
                            "hypothesis_text": m_hyp,
                        }

        # B. 檢查最新基準日 (latest_date) 當下是否有顯著訊號但尚未歸入事件
        idx_last = n_total - 1
        sub_5d_last = merged.iloc[max(0, idx_last - 4) : idx_last + 1]
        net_5d_last = int(sub_5d_last["net"].sum())
        net_1d_last = int(merged.iloc[idx_last]["net"])
        pct_20d_last = float(merged.iloc[idx_last]["pct_20d"])
        has_recent_sell_wave = any(
            sig_t == "SELL" and (idx_last - date_to_idx.get(sig_d, 0)) <= 4
            for (sig_d, sig_t) in candidate_signals
        )
        has_recent_buy_wave = any(
            sig_t == "BUY" and (idx_last - date_to_idx.get(sig_d, 0)) <= 4
            for (sig_d, sig_t) in candidate_signals
        )

        if (
            (net_1d_last <= -10 or (net_5d_last <= -15 and not has_recent_sell_wave))
            and (latest_date, "SELL") not in candidate_signals
        ):
            candidate_signals[(latest_date, "SELL")] = {
                "signal_date": latest_date,
                "signal_type": "SELL",
                "trigger_price": latest_close,
                "trigger_net_5d": net_5d_last,
                "trigger_net_1d": net_1d_last,
                "pct_20d": pct_20d_last,
                "hypothesis_text": f"最新基準日({latest_date[5:]})近5日連賣 {net_5d_last:+,}張 (當日 {net_1d_last:+,}張)，系統立約：⚠️ 高檔賣頂調節警訊",
            }
        elif (
            (net_1d_last >= 10 or (net_5d_last >= 15 and not has_recent_buy_wave))
            and (latest_date, "BUY") not in candidate_signals
        ):
            candidate_signals[(latest_date, "BUY")] = {
                "signal_date": latest_date,
                "signal_type": "BUY",
                "trigger_price": latest_close,
                "trigger_net_5d": net_5d_last,
                "trigger_net_1d": net_1d_last,
                "pct_20d": pct_20d_last,
                "hypothesis_text": f"最新基準日({latest_date[5:]})近5日連買 {net_5d_last:+,}張 (當日 {net_1d_last:+,}張)，系統立約：🔥 波段買盤進場支撐",
            }

        # C. 逐一對照後續實際 K 線走勢進行驗證蓋章
        verified_list: List[Dict[str, Any]] = []

        for (sig_date, sig_type), item in sorted(candidate_signals.items(), key=lambda x: x[0][0], reverse=True):
            idx = date_to_idx[sig_date]
            s_idx = int(item.get("start_idx", idx))
            trig_p = item["trigger_price"]
            if trig_p <= 0:
                continue

            days_elapsed = (n_total - 1) - idx
            fwd_10 = merged.iloc[idx + 1 : min(n_total, idx + 11)]
            fwd_20 = merged.iloc[idx + 1 : min(n_total, idx + 21)]
            wave_fwd_10 = merged.iloc[s_idx + 1 : min(n_total, idx + 11)]
            wave_fwd_60 = merged.iloc[s_idx + 1 : min(n_total, idx + 61)]

            t5_date, t5_p, t5_ret = "", 0.0, 0.0
            if idx + 5 < n_total:
                r5 = merged.iloc[idx + 5]
                t5_date = str(r5["date"])
                t5_p = float(r5["Close"])
                t5_ret = round((t5_p - trig_p) / trig_p * 100.0, 2)

            t10_date, t10_p, t10_ret = "", 0.0, 0.0
            if idx + 10 < n_total:
                r10 = merged.iloc[idx + 10]
                t10_date = str(r10["date"])
                t10_p = float(r10["Close"])
                t10_ret = round((t10_p - trig_p) / trig_p * 100.0, 2)

            latest_ret = round((latest_close - trig_p) / trig_p * 100.0, 2)

            if sig_type == "SELL":
                ref_fwd = fwd_10 if not fwd_10.empty else fwd_20
                fav_window = wave_fwd_10 if not wave_fwd_10.empty else ref_fwd
                if not fav_window.empty:
                    min_after = float(fav_window["Close"].min())
                    max_fav_pct = round((min_after - trig_p) / trig_p * 100.0, 2)  # 負值代表下跌(順風)
                else:
                    max_fav_pct = 0.0
                if not ref_fwd.empty:
                    max_after = float(ref_fwd["Close"].max())
                    max_adv_pct = round((max_after - trig_p) / trig_p * 100.0, 2)  # 正值代表被軋(逆風)
                else:
                    max_adv_pct = 0.0

                if days_elapsed == 0:
                    status = "PENDING"
                    v_code = "PENDING"
                    v_label = "⏳ 剛立約待驗證"
                    v_note = f"今日({sig_date[5:]})於 {fmt_price(trig_p)}元 立約看空調節，待後續 T+5/T+10 交易日驗收。"
                elif max_fav_pct <= -3.5 and max_adv_pct < 3.5:
                    status = "VERIFIED"
                    v_code = "WIN_SHARP"
                    v_label = "✅ 精準逃頂"
                    v_note = f"賣出後 10 日內股價最大回檔達 {max_fav_pct:+.2f}% (最高僅 {max_adv_pct:+.2f}%)，高檔賣頂邏輯完全應驗！"
                elif max_adv_pct >= 3.5 and max_fav_pct <= -3.2:
                    status = "VERIFIED"
                    v_code = "EARLY_LEFT"
                    v_label = "⚠️ 左側早賣(先軋後跌)"
                    v_note = f"賣出後短線雖曾續漲 +{max_adv_pct:.2f}%，但隨後波段見頂回落 {max_fav_pct:+.2f}%，屬「主升段左側分批調節」慣性。"
                elif days_elapsed >= 10:
                    status = "VERIFIED"
                    if max_fav_pct <= -1.2 or t10_ret <= 0.5:
                        v_code = "WIN_RESIST"
                        v_label = "✅ 高檔壓制"
                        v_note = f"賣出後 10 日內上檔受阻 (最大壓回 {max_fav_pct:+.2f}%、T+10 為 {t10_ret:+.2f}%)，有效預警短線壓力區。"
                    else:
                        v_code = "LOSS_SQUEEZE"
                        v_label = "❌ 賣飛被軋(指標鈍化)"
                        v_note = f"賣出後 10 日內未見明顯回檔，反而續創高點 (最大被軋 +{max_adv_pct:.2f}%)，主升段強勢換手下該次賣訊鈍化。"
                else:
                    # 1 <= days_elapsed < 10
                    status = "PENDING"
                    if latest_ret >= 2.5 and max_fav_pct > -2.5:
                        v_code = "PENDING_SQUEEZE"
                        v_label = f"⚠️ 驗證中：短線被軋 (+{latest_ret:.2f}%)"
                        v_note = (
                            f"T+{days_elapsed}日：立約價 {fmt_price(trig_p)}元 ➔ 現價 {fmt_price(latest_close)}元 ({latest_ret:+.2f}%)。"
                            f"短線遭遇多頭軋空逆風，顯示其正進行「左側逢高分批調節」，未跌破 5MA 前勿過早摸頭看空。"
                        )
                    elif latest_ret <= -1.5 or max_fav_pct <= -2.0:
                        v_code = "PENDING_FAVOR"
                        v_label = f"⏳ 驗證中：順風回檔 ({latest_ret:+.2f}%)"
                        v_note = f"T+{days_elapsed}日：立約價 {fmt_price(trig_p)}元 ➔ 現價 {fmt_price(latest_close)}元，短線如預期受阻壓回。"
                    else:
                        v_code = "PENDING"
                        v_label = f"⏳ 驗證中 (T+{days_elapsed}日 {latest_ret:+.2f}%)"
                        v_note = f"T+{days_elapsed}日：股價於 {fmt_price(latest_close)}元 ({latest_ret:+.2f}%) 高檔震盪，持續追蹤 T+5/T+10 表現。"

            else:  # BUY signal verification
                ref_fwd = fwd_20 if not fwd_20.empty else fwd_10
                fav_window = wave_fwd_60 if not wave_fwd_60.empty else ref_fwd
                if not ref_fwd.empty:
                    max_after_20 = float(ref_fwd["Close"].max())
                    min_after = float(ref_fwd["Close"].min())
                    fav_20 = round((max_after_20 - trig_p) / trig_p * 100.0, 2)
                    max_adv_pct = round((min_after - trig_p) / trig_p * 100.0, 2)  # 負值代表套牢(逆風)
                else:
                    fav_20, max_adv_pct = 0.0, 0.0
                if not fav_window.empty:
                    max_after_60 = float(fav_window["Close"].max())
                    fav_60 = round((max_after_60 - trig_p) / trig_p * 100.0, 2)
                else:
                    fav_60 = fav_20
                max_fav_pct = fav_20 if fav_20 >= 4.0 else max(fav_20, fav_60)

                if days_elapsed == 0:
                    status = "PENDING"
                    v_code = "PENDING"
                    v_label = "⏳ 剛立約待驗證"
                    v_note = f"今日({sig_date[5:]})於 {fmt_price(trig_p)}元 立約看多建倉，待後續 T+5/T+10 交易日驗收。"
                elif max_fav_pct >= 4.0 and max_adv_pct > -4.0:
                    status = "VERIFIED"
                    v_code = "WIN_SHARP"
                    v_label = "✅ 精準抄底"
                    v_note = f"買進後波段最大拉升達 +{max_fav_pct:.2f}%，低檔建倉抄底邏輯完全應驗！"
                elif max_adv_pct <= -4.0 and max_fav_pct >= 4.0:
                    status = "VERIFIED"
                    v_code = "EARLY_LEFT"
                    v_label = "⚠️ 左側早買(先蹲後跳)"
                    v_note = f"買進後短線曾回測 {max_adv_pct:+.2f}% 洗盤，隨後波段反攻 +{max_fav_pct:.2f}%，屬「左側逢低分批佈局」慣性。"
                elif days_elapsed >= 10:
                    status = "VERIFIED"
                    if max_fav_pct >= 1.5 or t10_ret >= 0.0:
                        v_code = "WIN_SUPPORT"
                        v_label = "✅ 低檔抗跌支撐"
                        v_note = f"買進後下檔守穩並推升 +{max_fav_pct:.2f}% (T+10 為 {t10_ret:+.2f}%)，發揮底部鎖碼支撐效果。"
                    else:
                        v_code = "LOSS_TRAP"
                        v_label = "❌ 抄底套牢(破線失準)"
                        v_note = f"買進後未能止跌，波段最大回撤達 {max_adv_pct:+.2f}%，短線承接後遭遇破線賣壓。"
                else:
                    status = "PENDING"
                    if latest_ret <= -2.5 and max_fav_pct < 2.0:
                        v_code = "PENDING_TRAP"
                        v_label = f"⚠️ 驗證中：短線套牢 ({latest_ret:+.2f}%)"
                        v_note = f"T+{days_elapsed}日：立約價 {fmt_price(trig_p)}元 ➔ 現價 {fmt_price(latest_close)}元，短線暫處左側承接套牢區。"
                    elif latest_ret >= 1.5 or max_fav_pct >= 2.5:
                        v_code = "PENDING_FAVOR"
                        v_label = f"⏳ 驗證中：順風獲利 (+{latest_ret:.2f}%)"
                        v_note = f"T+{days_elapsed}日：立約價 {fmt_price(trig_p)}元 ➔ 現價 {fmt_price(latest_close)}元，買盤進場後順風推升中。"
                    else:
                        v_code = "PENDING"
                        v_label = f"⏳ 驗證中 (T+{days_elapsed}日 {latest_ret:+.2f}%)"
                        v_note = f"T+{days_elapsed}日：股價於 {fmt_price(latest_close)}元 ({latest_ret:+.2f}%) 附近築底換手，持續追蹤。"

            verified_list.append({
                "stock_id": stock_id,
                "broker_name": broker_name,
                "signal_date": sig_date,
                "signal_type": sig_type,
                "role_label": role_label,
                "trigger_price": trig_p,
                "trigger_net_5d": item["trigger_net_5d"],
                "trigger_net_1d": item["trigger_net_1d"],
                "pct_20d": item["pct_20d"],
                "hypothesis_text": item["hypothesis_text"],
                "status": status,
                "days_elapsed": days_elapsed,
                "t5_date": t5_date,
                "t5_price": t5_p,
                "t5_ret_pct": t5_ret,
                "t10_date": t10_date,
                "t10_price": t10_p,
                "t10_ret_pct": t10_ret,
                "latest_date": latest_date,
                "latest_price": latest_close,
                "latest_ret_pct": latest_ret,
                "max_favorable_pct": max_fav_pct,
                "max_adverse_pct": max_adv_pct,
                "verdict_code": v_code,
                "verdict_label": v_label,
                "verdict_note": v_note,
            })

        self.db.save_broker_signal_verifications(verified_list)
        return verified_list

    def _summarize_verification_calibration(
        self,
        broker_name: str,
        role_type: str,
        verified_list: List[Dict[str, Any]],
        net_5d: int,
        net_1d: int,
        ref_price: float,
    ) -> Dict[str, Any]:
        """
        根據「系統判讀立約 ✕ 事後驗證表」，計算：
        1. 實戰驗證勝率 (verified_win_rate) 與 左側早賣/早買比例 (left_side_count)
        2. 近期訊號狀態 (是否處於「短線被軋賣飛鈍化」或「連準高信心」)
        3. 自動產生給人類與 AI 報告的「邏輯修正評語 (calibration_note)」
        """
        completed = [v for v in verified_list if v["status"] == "VERIFIED"]
        wins = [v for v in completed if v["verdict_code"] in ("WIN_SHARP", "WIN_RESIST", "WIN_SUPPORT", "EARLY_LEFT")]
        sharp_wins = [v for v in completed if v["verdict_code"] in ("WIN_SHARP", "WIN_RESIST", "WIN_SUPPORT")]
        left_side = [v for v in completed if v["verdict_code"] == "EARLY_LEFT"]
        losses = [v for v in completed if v["verdict_code"] in ("LOSS_SQUEEZE", "LOSS_TRAP")]
        pending_squeeze = [v for v in verified_list if v["verdict_code"] in ("PENDING_SQUEEZE", "PENDING_TRAP") and v["days_elapsed"] >= 1]
        pending_favor = [v for v in verified_list if v["verdict_code"] == "PENDING_FAVOR" and v["days_elapsed"] >= 1]

        total_comp = len(completed)
        broad_win_rate = round(len(wins) / total_comp * 100.0, 1) if total_comp > 0 else 0.0
        strict_win_rate = round(len(sharp_wins) / total_comp * 100.0, 1) if total_comp > 0 else 0.0

        # 檢查最近一次非當日的立約訊號表現
        prior_signals = [v for v in verified_list if v["days_elapsed"] >= 1]
        latest_prior = prior_signals[0] if prior_signals else None

        confidence_state = "HIGH"
        calibration_badge = "✅ 邏輯驗證高準度"
        calibration_note = ""

        if latest_prior and latest_prior["verdict_code"] in ("PENDING_SQUEEZE", "LOSS_SQUEEZE"):
            p_date = latest_prior["signal_date"][5:]
            p_price = fmt_price(latest_prior["trigger_price"])
            p_ret = latest_prior["latest_ret_pct"]
            confidence_state = "LEFT_SIDE_CAUTION"
            calibration_badge = f"⚠️ 左側早賣觀察 (前次 {p_date}@{p_price}元 被軋 {p_ret:+.1f}%)"
            if net_5d <= -10 or net_1d <= -5:
                recent_sell_desc = (
                    f"近5日累計 {net_5d:+,}張、今日 {net_1d:+,}張"
                    if net_5d < 0
                    else f"今日單日調節 {net_1d:+,}張（近5日合計 {net_5d:+,}張）"
                )
                calibration_note = (
                    f"🔍 **邏輯驗證覆盤**：前次({p_date} @ {p_price}元)觸發賣頂警訊後，股價短線續漲至 {fmt_price(ref_price)}元 ({p_ret:+.1f}%)，"
                    f"驗證該分點屬**「主升段左側逢高分批調節」**而非一賣即崩（過去 {total_comp} 次出手有 {len(left_side)} 次先軋後跌、最終波段勝率 {broad_win_rate:.0f}%）。"
                    f"目前於 {fmt_price(ref_price)}元 續賣（{recent_sell_desc}），應視為高檔籌碼供給增加之**左側預警**，宜待跌破 5MA 再確認右側轉折，勿單憑賣超直接摸頭放空。"
                )
            else:
                calibration_note = (
                    f"🔍 **邏輯驗證覆盤**：前次({p_date} @ {p_price}元)調節後股價強勢軋空至 {fmt_price(ref_price)}元 ({p_ret:+.1f}%)，"
                    f"目前賣壓已趨緩（近5日 {net_5d:+,}張），顯示多頭換手力道強於該分點左側調節。"
                )
        elif latest_prior and latest_prior["verdict_code"] in ("PENDING_TRAP", "LOSS_TRAP"):
            p_date = latest_prior["signal_date"][5:]
            p_price = fmt_price(latest_prior["trigger_price"])
            p_ret = latest_prior["latest_ret_pct"]
            confidence_state = "LEFT_SIDE_CAUTION"
            calibration_badge = f"⚠️ 左側早買觀察 (前次 {p_date}@{p_price}元 逆風 {p_ret:+.1f}%)"
            calibration_note = (
                f"🔍 **邏輯驗證覆盤**：前次({p_date} @ {p_price}元)低接後股價短線續探至 {fmt_price(ref_price)}元 ({p_ret:+.1f}%)，"
                f"驗證該分點習慣於下跌段**「左側分批承接」**，需配合站回 5MA 方可確認底部止跌。"
            )
        elif pending_favor:
            pf = pending_favor[0]
            p_date = pf["signal_date"][5:]
            p_price = fmt_price(pf["trigger_price"])
            p_ret = pf["latest_ret_pct"]
            confidence_state = "HIGH"
            calibration_badge = f"🎯 近期判讀順風應驗中 ({p_date}@{p_price}元 ➔ {p_ret:+.1f}%)"
            calibration_note = (
                f"🔍 **邏輯驗證覆盤**：前次({p_date} @ {p_price}元)發出訊號後，截至目前股價走勢完全順應判讀方向 ({p_ret:+.1f}%)，"
                f"結合歷史 {total_comp} 次驗證達 {broad_win_rate:.0f}% 勝率，該分點當下動向具備極高實戰參考權重。"
            )
        else:
            if len(left_side) >= 2:
                calibration_badge = f"📊 波段勝率 {broad_win_rate:.0f}% (含 {len(left_side)} 次左側早出手)"
                calibration_note = (
                    f"🔍 **邏輯驗證覆盤**：累計 {total_comp} 次歷史訊號驗證中，直接命中 {len(sharp_wins)} 次、左側提早出手(先逆風後應驗) {len(left_side)} 次、失準 {len(losses)} 次 "
                    f"(綜合波段勝率 {broad_win_rate:.0f}%)；操作上常提前 3~5 日左側分批布局或調節。"
                )
            else:
                calibration_badge = f"✅ 歷史驗證勝率 {broad_win_rate:.0f}% ({len(wins)}/{total_comp})"
                calibration_note = (
                    f"🔍 **邏輯驗證覆盤**：累計 {total_comp} 次歷史轉折訊號驗證中，成功應驗 {len(wins)} 次 (實戰勝率 {broad_win_rate:.0f}%)，訊號穩定度高。"
                )

        return {
            "total_completed": total_comp,
            "wins_count": len(wins),
            "sharp_wins_count": len(sharp_wins),
            "left_side_count": len(left_side),
            "losses_count": len(losses),
            "broad_win_rate": broad_win_rate,
            "strict_win_rate": strict_win_rate,
            "confidence_state": confidence_state,
            "calibration_badge": calibration_badge,
            "calibration_note": calibration_note,
        }

    def _render_broker_markdown(
        self,
        stock_id: str,
        stock_name: str,
        anchor_date: str,
        ref_price: float,
        broker_info: Dict[str, Any],
        merged: pd.DataFrame,
        sell_events: List[Dict[str, Any]],
        buy_events: List[Dict[str, Any]],
        verified_list: List[Dict[str, Any]],
        calibration: Dict[str, Any],
        sync_status: str,
    ) -> str:
        """
        產出單一關鍵大戶之三層式人類易讀 Markdown 檔案：
        - 頂部：🧬 分點操盤基因與狀態卡 (5秒速讀)
        - 中部：🔍 系統判讀立約 ✕ 事後驗證覆盤表 ＋ 🏛️ 關鍵波段戰役大事紀
        - 底部：📒 逐日增量交易帳本 (近 30 筆活躍日展開 + 更早期明細 <details> 摺疊收納)
        """
        b_name = broker_info["broker_name"]
        bhid = broker_info.get("bhid", "")
        b_code = broker_info.get("b_code", "")
        role_label = broker_info.get("role_label", "🎯 關鍵主力分點")
        role_type = broker_info.get("role_type", "PEAK_SNIPER")

        active_df = merged[merged["net"] != 0].copy()
        active_days = len(active_df)
        total_buy = int(merged["buy"].sum()) if not merged.empty else 0
        total_sell = int(merged["sell"].sum()) if not merged.empty else 0
        cum_net_1y = total_buy - total_sell

        buy_vwap = float((merged["buy"] * merged["Close"]).sum() / total_buy) if total_buy > 0 else 0.0
        sell_vwap = float((merged["sell"] * merged["Close"]).sum() / total_sell) if total_sell > 0 else 0.0
        spread_val = (sell_vwap - buy_vwap) if (buy_vwap > 0 and sell_vwap > 0) else 0.0
        spread_pct = ((sell_vwap - buy_vwap) / buy_vwap * 100.0) if (buy_vwap > 0 and sell_vwap > 0) else 0.0

        r5_df = merged.tail(5)
        r20_df = merged.tail(20)
        net_1d = int(merged.iloc[-1]["net"]) if not merged.empty else 0
        net_5d = int(r5_df["net"].sum()) if not r5_df.empty else 0
        net_20d = int(r20_df["net"].sum()) if not r20_df.empty else 0

        last_recorded_date = str(merged.iloc[-1]["date"]) if not merged.empty else anchor_date
        first_recorded_date = str(merged.iloc[0]["date"]) if not merged.empty else anchor_date

        meta_payload = {
            "stock_id": stock_id,
            "stock_name": stock_name,
            "broker_name": b_name,
            "bhid": bhid,
            "b_code": b_code,
            "role_type": role_type,
            "last_recorded_date": last_recorded_date,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        meta_comment = f"<!-- BROKER_KB_META: {json.dumps(meta_payload, ensure_ascii=False)} -->"

        # 決定目前當下行動解讀
        if net_5d <= -15 or net_1d <= -10:
            cur_action_badge = f"🚨 近5日連賣 {net_5d:+,} 張 (今日 {net_1d:+,} 張) — 高檔調節中"
        elif net_5d >= 15 or net_1d >= 10:
            cur_action_badge = f"🔥 近5日連買 {net_5d:+,} 張 (今日 {net_1d:+,} 張) — 積極建倉中"
        else:
            cur_action_badge = f"⏳ 近5日小幅換手 {net_5d:+,} 張 (今日 {net_1d:+,} 張) — 按兵不動觀望"

        # 1. 組裝【🔍 系統判讀立約 ✕ 事後驗證覆盤表】
        verif_rows_md = []
        for v in verified_list[:15]:
            s_date = v["signal_date"]
            t_p = fmt_price(v["trigger_price"])
            sig_icon = "🔻賣訊" if v["signal_type"] == "SELL" else "🔺買訊"
            t5_str = f"{fmt_price(v['t5_price'])}元 ({v['t5_ret_pct']:+.2f}%)" if v["t5_price"] > 0 else "待滿5日"
            t10_str = f"{fmt_price(v['t10_price'])}元 ({v['t10_ret_pct']:+.2f}%)" if v["t10_price"] > 0 else "待滿10日"
            if v["signal_type"] == "SELL":
                wave_ext = f"最大回檔 {v['max_favorable_pct']:+.2f}% / 最高被軋 {v['max_adverse_pct']:+.2f}%"
            else:
                wave_ext = f"最大拉升 {v['max_favorable_pct']:+.2f}% / 最大回撤 {v['max_adverse_pct']:+.2f}%"
            verif_rows_md.append(
                f"| `{s_date}` | **{t_p} 元** (位階 {v['pct_20d']:.0f}%) | {sig_icon}：{v['hypothesis_text']} | {t5_str} | {t10_str} | {wave_ext} | **{v['verdict_label']}**<br>{v['verdict_note']} |"
            )
        verif_table_md = (
            "| 立約日期 | 觸發股價 (20D位階) | 當時系統判讀立約 | T+5日股價表現 | T+10日股價表現 | 波段極值檢驗 | 驗證蓋章與邏輯覆盤 |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
            + ("\n".join(verif_rows_md) if verif_rows_md else "| 尚無歷史觸發紀錄 | - | - | - | - | - | - |")
        )

        # 2. 組裝【🏛️ 關鍵波段戰役大事紀】
        all_milestones = []
        for e in sell_events:
            all_milestones.append(e)
        for e in buy_events:
            all_milestones.append(e)
        all_milestones.sort(key=lambda x: x["end_date"], reverse=True)

        milestone_rows = []
        n_total = len(merged)
        for ev in all_milestones[:15]:
            period_str = (
                f"`{ev['start_date']}` ~ `{ev['end_date'][5:]}`"
                if ev["start_date"] != ev["end_date"]
                else f"`{ev['start_date']}`"
            )
            idx = ev["last_idx"]
            s_idx = ev.get("start_idx", idx)
            fwd_10 = merged.iloc[idx + 1 : min(n_total, idx + 11)]
            fwd_20 = merged.iloc[idx + 1 : min(n_total, idx + 21)]
            wave_10 = merged.iloc[s_idx + 1 : min(n_total, idx + 11)]
            wave_60 = merged.iloc[s_idx + 1 : min(n_total, idx + 61)]
            ref_vwap = float(ev.get("vwap") or ev["end_close"])

            if ev["type"] == "SELL":
                m_type = "🔻 **波段調節/賣頂**"
                lots_str = f"**{ev['total_net']:,} 張** ({ev['active_days']}日)"
                p_zone_str = f"{fmt_price(ev['min_close'])}~{fmt_price(ev['max_close'])}元 (均 **{fmt_price(ev['vwap'])}**元)"
                pos_str = f"20D高檔 {ev.get('max_pct_20d', 50):.0f}%"
                if not wave_10.empty and ref_vwap > 0:
                    drop_10 = (float(wave_10["Close"].min()) - ref_vwap) / ref_vwap * 100.0
                    rally_10 = (
                        (float(fwd_10["Close"].max()) - ref_vwap) / ref_vwap * 100.0
                        if not fwd_10.empty
                        else 0.0
                    )
                    after_str = f"較賣均最大回檔 **{drop_10:+.2f}%** (賣後最高 {rally_10:+.2f}%)"
                else:
                    after_str = "最新進行中戰役"
            else:
                m_type = "🔺 **波段建倉/抄底**"
                lots_str = f"**+{ev['total_net']:,} 張** ({ev['active_days']}日)"
                p_zone_str = f"{fmt_price(ev['min_close'])}~{fmt_price(ev['max_close'])}元 (均 **{fmt_price(ev['vwap'])}**元)"
                pos_str = f"20D低檔 {ev.get('min_pct_20d', 50):.0f}%"
                if not wave_60.empty and ref_vwap > 0:
                    gain_20 = (
                        (float(fwd_20["Close"].max()) - ref_vwap) / ref_vwap * 100.0
                        if not fwd_20.empty
                        else (float(wave_60["Close"].max()) - ref_vwap) / ref_vwap * 100.0
                    )
                    gain_60 = (float(wave_60["Close"].max()) - ref_vwap) / ref_vwap * 100.0
                    dd_20 = (
                        (float(fwd_20["Close"].min()) - ref_vwap) / ref_vwap * 100.0
                        if not fwd_20.empty
                        else 0.0
                    )
                    if gain_60 > gain_20 + 4.0:
                        after_str = f"較買均20日 **{gain_20:+.2f}%** / 60日波段 **{gain_60:+.2f}%**"
                    else:
                        after_str = f"較買均最大拉升 **{max(gain_20, gain_60):+.2f}%** (最深 {dd_20:+.2f}%)"
                else:
                    after_str = "最新進行中戰役"

            milestone_rows.append(
                f"| {period_str} | {m_type} | {lots_str} | {p_zone_str} | {pos_str} | {after_str} |"
            )

        milestones_table_md = (
            "| 戰役期間 | 操作方向 | 累計淨買賣 (天數) | 操作價格區間 (加權均價) | 出手時位階 | 出手後實際股價驗證 |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            + ("\n".join(milestone_rows) if milestone_rows else "| 尚無顯著波段戰役 | - | - | - | - | - |")
        )

        # 3. 組裝【📒 逐日增量交易帳本 (近 30 筆展開 + 歷史摺疊)】
        if not merged.empty:
            merged_with_cum = merged.copy()
            merged_with_cum["cum_net"] = merged_with_cum["net"].cumsum()
            active_ledger = merged_with_cum[merged_with_cum["net"] != 0].iloc[::-1]  # 由新到舊排序
        else:
            active_ledger = pd.DataFrame()

        recent_30_rows = []
        older_rows = []
        for i, (_, r) in enumerate(active_ledger.iterrows()):
            d_str = str(r["date"])
            c_str = fmt_price(float(r["Close"]))
            pct_str = f"{float(r['pct_20d']):.0f}%"
            b_lots = int(r["buy"])
            s_lots = int(r["sell"])
            n_lots = int(r["net"])
            cum_lots = int(r["cum_net"])

            if n_lots <= -20:
                tag_note = "🔻 高檔顯著調節" if float(r["pct_20d"]) >= 70 else "🔻 單日大額賣超"
            elif n_lots < 0:
                tag_note = "賣超調節"
            elif n_lots >= 20:
                tag_note = "🔺 低檔積極承接" if float(r["pct_20d"]) <= 35 else "🔺 單日大額買超"
            else:
                tag_note = "買超吸籌"

            row_line = f"| `{d_str}` | {c_str} 元 | {pct_str} | {b_lots:,} | {s_lots:,} | **{n_lots:+,}** | {cum_lots:+,} | {tag_note} |"
            if i < 30:
                recent_30_rows.append(row_line)
            else:
                older_rows.append(row_line)

        ledger_header = (
            "| 交易日期 | 收盤價 | 20D位階 | 買進(張) | 賣出(張) | 淨買賣(張) | 1年累積淨庫存 | 當日操作標註 |\n"
            "| :--- | :---: | :---: | ---: | ---: | ---: | ---: | :--- |\n"
        )
        recent_ledger_md = ledger_header + (
            "\n".join(recent_30_rows) if recent_30_rows else "| 近期無進出紀錄 | - | - | 0 | 0 | 0 | 0 | - |"
        )

        older_details_md = ""
        if older_rows:
            older_table = ledger_header + "\n".join(older_rows)
            older_details_md = f"""
<details>
<summary>📂 <strong>點擊展開更早期歷史活躍交易明細（共 {len(older_rows)} 個交易日，完整保留供增量回測驗證）</strong></summary>

{older_table}

</details>
"""

        return f"""{meta_comment}
# 🏛️ 【{stock_id} {stock_name}】關鍵大戶戰略檔案：{b_name}

> **最後增量同步基準日**：`{last_recorded_date}` ｜ **最新收盤價**：`{fmt_price(ref_price)} 元` ｜ **同步模式**：{sync_status}  
> **返回個股大戶總覽**：[📋 00_關鍵大戶總覽.md](./00_關鍵大戶總覽.md)

---

## 一、🧬 分點操盤基因與狀態卡 (5秒速讀)

| 核心指標 | 量化數據與基因認證 |
| :--- | :--- |
| **分點名稱 / 代碼** | **{b_name}** (`BHID={bhid or 'N/A'}`, `b={b_code or 'N/A'}`) |
| **系統認證角色** | **{role_label}** |
| **1 年期活躍度與總量** | 過去 1 年 (`{first_recorded_date}` ~ `{last_recorded_date}`) 共出手 **{active_days}** 個交易日｜總買進 **{total_buy:,}** 張、總賣出 **{total_sell:,}** 張（淨庫存變化 **{cum_net_1y:+,}** 張） |
| **1 年期買賣均價 (VWAP) 與正價差** | 買進均價 **{buy_vwap:.2f} 元** ➔ 賣出均價 **{sell_vwap:.2f} 元**（平均每張正價差 **{spread_val:+.2f} 元 / {spread_pct:+.2f}%**） |
| **近期籌碼動向 (1D / 5D / 20D)** | 今日：`{net_1d:+,} 張` ｜ 近 5 日：`{net_5d:+,} 張` ｜ 近 20 日：`{net_20d:+,} 張` |
| **當下戰術狀態** | {cur_action_badge} |
| **判讀邏輯實戰驗證準確率** | **{calibration['calibration_badge']}**（已完成驗收 {calibration['total_completed']} 次：精準命中 {calibration['sharp_wins_count']} 次、左側早出手後應驗 {calibration['left_side_count']} 次、失準 {calibration['losses_count']} 次） |

### 💡 操盤手速讀結論與自我驗證修正
{calibration['calibration_note']}

---

## 二、🔍 系統判讀立約 ✕ 事後驗證覆盤表 (Logic Self-Verification)

> **設計目的**：系統每次於關鍵基準日對該分點發出「賣頂調節」或「抄底建倉」判讀時，自動立下紀錄並於後續 `T+5`、`T+10`、`T+20` 交易日回填實際股價走勢，誠實檢驗**「究竟是精準轉折、左側提早分批調節、還是主升段賣飛被軋」**。

{verif_table_md}

---

## 三、🏛️ 關鍵波段戰役大事紀 (過去 1 年連續出手聚合)

> 將相距 5 個交易日內的連續大額買超或賣超自動聚合成獨立「波段戰役」，一眼看清每次波段出手的張數、成本區間與事後股價反應：

{milestones_table_md}

---

## 四、📒 逐日增量交易帳本 (Incremental Daily Ledger)

- 📌 **增量更新水位**：目前已完整記錄至 **`{last_recorded_date}`**。下次執行日報或週報時，系統**僅針對 `> {last_recorded_date}` 的新交易日增量抓取並追加**，不會重複爬取整年舊資料。

### 📊 最近 30 個活躍交易日進出明細
{recent_ledger_md}
{older_details_md}
"""

    def _render_stock_overview_markdown(
        self,
        stock_id: str,
        stock_name: str,
        anchor_date: str,
        ref_price: float,
        broker_summaries: List[Dict[str, Any]],
        certified_disc: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """產出 brokers/{stock_id}_{stock_name}/00_關鍵大戶總覽.md"""
        rows_md = []
        highlights_md = []

        for b in broker_summaries:
            b_name = b["broker_name"]
            safe_file = f"{_safe_filename(b_name)}.md"
            role_label = b["role_label"]
            vwap_str = (
                f"買 {b['buy_vwap']:.1f} / 賣 {b['sell_vwap']:.1f} ({b['spread_pct']:+.1f}%)"
                if b["buy_vwap"] > 0 and b["sell_vwap"] > 0
                else f"買 {b['buy_vwap']:.1f} / 賣 {b['sell_vwap']:.1f}"
            )
            r_moves = f"今日 `{b['net_1d']:+,}` / 5D `{b['net_5d']:+,}` / 20D `{b['net_20d']:+,}`"
            cal = b["calibration"]
            rows_md.append(
                f"| **[{b_name}](./{safe_file})** | {role_label} | {vwap_str} | {r_moves} | {cal['calibration_badge']} | `{b['last_recorded_date']}` |"
            )
            highlights_md.append(f"- **[{b_name}](./{safe_file})**（{role_label}）：{cal['calibration_note']}")

        overview_table = (
            "| 關鍵大戶分點 (點擊詳閱帳本) | 認證定位 | 1年買賣均價 (正價差) | 最新進出張數 (1D / 5D / 20D) | 判讀邏輯驗證狀態 | 帳本增量基準日 |\n"
            "| :--- | :--- | :--- | :--- | :--- | :---: |\n"
            + ("\n".join(rows_md) if rows_md else "| 尚無建檔分點 | - | - | - | - | - |")
        )
        highlights_block = "\n".join(highlights_md) if highlights_md else "- 尚無特定驗證覆盤事項。"

        disc_section_md = ""
        if certified_disc:
            disc_rows = []
            for cd in certified_disc:
                c_name = cd.get("broker_name", "")
                c_file = f"{_safe_filename(c_name)}.md"
                purity = cd.get("buy_purity", 0.0)
                vwap = cd.get("vwap_buy_cost", 0.0)
                net20 = cd.get("net_shares_20d", 0)
                cert_at = cd.get("certified_at", "")
                exp_at = cd.get("expires_at", "")
                disc_rows.append(
                    f"| **[{c_name}](./{c_file})** | `+{net20:,}` 張 | `{purity:.1f}%` | `{vwap:.1f}` 元 | `{cert_at}` | `{exp_at}` (TTL有效) | 🔥 認證鎖碼中 |"
                )
            disc_table = (
                "| 認證代操席位 | 20日累計買超 | 買進純度 | 平均建倉成本 (VWAP) | 認證基準日 | 快取有效期限 (TTL) | 當前狀態 |\n"
                "| :--- | :---: | :---: | :---: | :---: | :---: | :--- |\n"
                + "\n".join(disc_rows)
            )
            disc_section_md = f"""

---

## 二、🏛️ 認證代操鎖碼席位戰情 (14~30 天 TTL 快取追蹤)

> 本區席位係經由量化引擎嚴格檢定：**近 20 日買進純度 ≥ 85% 且累計淨買超達門檻** 之機構委外代操/總公司席位，自動賦予 21 天 TTL 快取，日報產出時以此為基準執行 O(1) 輕量增量讀取。

{disc_table}
"""

        sec_three_num = "三" if disc_section_md else "二"

        return f"""# 📋 【{stock_id} {stock_name}】關鍵大戶分點戰略檔案室 (總覽)

> **最新同步基準日**：`{anchor_date}` ｜ **最新收盤價**：`{fmt_price(ref_price)} 元` ｜ **納管核心分點數**：`{len(broker_summaries)} 家`  
> **增量機制說明**：本資料夾內每家大戶皆維護獨立 `.md` 交易帳本與邏輯驗證表；每次執行僅針對 `> 帳本增量基準日` 之新交易日進行增量分析，並自動對過往判讀進行 `T+5 / T+10 / T+20` 事後股價驗收。

---

## 一、⚡ 關鍵大戶戰情一覽表 (5秒略讀)

{overview_table}
{disc_section_md}
---

## {sec_three_num}、🔍 本期「判讀邏輯自我驗證」重點覆盤

{highlights_block}
"""

    def sync_stock_knowledge_base(
        self,
        stock_id: str,
        stock_name: str,
        anchor_date: str,
        ref_price: float,
        smart_brokers: Dict[str, Any],
        profile_data: Optional[Dict[str, Any]],
        extra_tracked_brokers: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        同步指定個股下所有關鍵大戶之 Markdown 檔案與邏輯驗證結果，
        並將自我驗證後的「動態校正資訊 (calibration_note / calibration_badge)」回填至 smart_brokers。
        """
        kline_1y = (profile_data or {}).get("kline_1y", [])
        df_p = self._build_price_df(kline_1y)
        if df_p.empty:
            logger.warning(f"[Broker KB] {stock_id} 缺少 1 年日 K 資料，略過大戶 Markdown 知識庫同步")
            return {}

        start_1y = str(df_p["date"].iloc[0])
        effective_anchor = anchor_date or str(df_p["date"].iloc[-1])
        stock_folder = self._get_stock_folder(stock_id, stock_name)

        # 1. 彙整納管之關鍵大戶清單 (1年認證波段高手 Top 5 + 長線囤貨鎖碼大戶 Top 3 + 手動指定/既有建檔分點)
        tracked_map: Dict[str, Dict[str, Any]] = {}
        known_codes = self.db.get_known_broker_codes(stock_id)

        for sm in (smart_brokers or {}).get("swing_masters", []):
            b_name = sm.get("broker_name", "")
            if not b_name:
                continue
            bhid = sm.get("bhid") or known_codes.get(b_name, ("", ""))[0]
            b_code = sm.get("b_code") or known_codes.get(b_name, ("", ""))[1]
            tracked_map[b_name] = {
                "broker_name": b_name,
                "bhid": bhid,
                "b_code": b_code,
                "role_type": sm.get("role_type", "PEAK_SNIPER" if sm.get("action") == "SELL" else "BOTTOM_BUYER"),
                "role_label": sm.get("role", "🎯 波段神算高手"),
                "source_ref": sm,
            }

        for hd in (smart_brokers or {}).get("hoarders", [])[:5]:
            b_name = hd.get("broker_name", "")
            if not b_name or b_name in tracked_map:
                continue
            bhid = hd.get("bhid") or known_codes.get(b_name, ("", ""))[0]
            b_code = hd.get("b_code") or known_codes.get(b_name, ("", ""))[1]
            is_d = hd.get("is_discretionary", False) or "代操" in str(hd.get("badge", ""))
            tracked_map[b_name] = {
                "broker_name": b_name,
                "bhid": bhid,
                "b_code": b_code,
                "role_type": "DISCRETIONARY_LOCK" if is_d else "HOARDER",
                "role_label": f"💼 認證代操鎖碼 ({hd.get('period', 20)}D 純度 {hd.get('purity', 0)}%)" if is_d else f"🔒 長線囤貨大戶 ({hd.get('period', 240)}D 鎖碼純度 {hd.get('purity', 0)}%)",
                "source_ref": hd,
            }

        # 1.5 納管認證代操鎖碼席位 (Certified Discretionary Desks)
        certified_disc = self.db.get_certified_discretionary_brokers(stock_id) if hasattr(self.db, "get_certified_discretionary_brokers") else []
        for cd in certified_disc:
            b_name = cd.get("broker_name", "")
            if not b_name:
                continue
            bhid = cd.get("bhid") or known_codes.get(b_name, ("", ""))[0]
            b_code = cd.get("b_code") or known_codes.get(b_name, ("", ""))[1]
            purity = cd.get("buy_purity", 0.0)
            role_lbl = f"💼 認證代操鎖碼 (純度 {purity:.1f}%)"
            if b_name not in tracked_map:
                tracked_map[b_name] = {
                    "broker_name": b_name,
                    "bhid": bhid,
                    "b_code": b_code,
                    "role_type": "DISCRETIONARY_LOCK",
                    "role_label": role_lbl,
                    "source_ref": cd,
                }
            else:
                if "代操" not in tracked_map[b_name]["role_label"]:
                    tracked_map[b_name]["role_label"] += f" ✕ 💼認證代操"

        for extra_name in (extra_tracked_brokers or []):
            if extra_name and extra_name not in tracked_map:
                bhid, b_code = known_codes.get(extra_name, ("", ""))
                tracked_map[extra_name] = {
                    "broker_name": extra_name,
                    "bhid": bhid,
                    "b_code": b_code,
                    "role_type": "CUSTOM_TRACKED",
                    "role_label": "📌 重點觀察分點",
                    "source_ref": None,
                }

        # 2. 逐一增量同步各關鍵大戶之逐日帳本、執行邏輯自我驗證並產出 .md 檔
        broker_summaries: List[Dict[str, Any]] = []
        calibrations_by_broker: Dict[str, Dict[str, Any]] = {}

        for b_name, b_info in tracked_map.items():
            md_filename = f"{_safe_filename(b_name)}.md"
            md_path = os.path.join(stock_folder, md_filename)
            md_meta = self._read_md_meta(md_path)

            trades, sync_status, _ = self._sync_broker_incremental_ledger(
                stock_id=stock_id,
                broker_name=b_name,
                bhid=b_info["bhid"],
                b_code=b_info["b_code"],
                start_1y=start_1y,
                anchor_date=effective_anchor,
                md_meta=md_meta,
                force_refresh=force_refresh,
            )

            if trades:
                df_t = pd.DataFrame(trades)
                merged = pd.merge(df_p, df_t[["date", "buy", "sell", "net"]], on="date", how="left")
                merged[["buy", "sell", "net"]] = merged[["buy", "sell", "net"]].fillna(0).astype(int)
            else:
                merged = df_p.copy()
                merged["buy"] = 0
                merged["sell"] = 0
                merged["net"] = 0

            sell_events, buy_events = self._cluster_wave_milestones(merged)
            verified_list = self._evaluate_and_verify_signals(
                stock_id=stock_id,
                broker_name=b_name,
                role_type=b_info["role_type"],
                role_label=b_info["role_label"],
                merged=merged,
                sell_events=sell_events,
                buy_events=buy_events,
            )

            net_1d = int(merged.iloc[-1]["net"]) if not merged.empty else 0
            net_5d = int(merged.tail(5)["net"].sum()) if not merged.empty else 0
            net_20d = int(merged.tail(20)["net"].sum()) if not merged.empty else 0
            total_buy = int(merged["buy"].sum()) if not merged.empty else 0
            total_sell = int(merged["sell"].sum()) if not merged.empty else 0
            buy_vwap = float((merged["buy"] * merged["Close"]).sum() / total_buy) if total_buy > 0 else 0.0
            sell_vwap = float((merged["sell"] * merged["Close"]).sum() / total_sell) if total_sell > 0 else 0.0
            spread_pct = ((sell_vwap - buy_vwap) / buy_vwap * 100.0) if (buy_vwap > 0 and sell_vwap > 0) else 0.0

            calibration = self._summarize_verification_calibration(
                broker_name=b_name,
                role_type=b_info["role_type"],
                verified_list=verified_list,
                net_5d=net_5d,
                net_1d=net_1d,
                ref_price=ref_price,
            )
            calibrations_by_broker[b_name] = calibration

            md_content = self._render_broker_markdown(
                stock_id=stock_id,
                stock_name=stock_name,
                anchor_date=effective_anchor,
                ref_price=ref_price,
                broker_info=b_info,
                merged=merged,
                sell_events=sell_events,
                buy_events=buy_events,
                verified_list=verified_list,
                calibration=calibration,
                sync_status=sync_status,
            )

            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md_content)

            # 將邏輯自我驗證結果回填至 smart_brokers 卡片中，讓 HTML 儀表板與 AI Prompt 同步受惠
            src_ref = b_info.get("source_ref")
            if isinstance(src_ref, dict):
                src_ref["kb_md_path"] = md_path.replace("\\", "/")
                src_ref["calibration_badge"] = calibration["calibration_badge"]
                src_ref["calibration_note"] = calibration["calibration_note"]
                src_ref["confidence_state"] = calibration["confidence_state"]
                if "desc" in src_ref:
                    clean_note = calibration["calibration_note"].replace("**", "")
                    src_ref["desc"] += (
                        f"<div style='margin-top:4px;padding:4px 8px;background:#f8fafc;border-left:3px solid #6366f1;"
                        f"font-size:0.8rem;color:#334155;'>{clean_note}</div>"
                    )
                if "plain_summary" in src_ref:
                    clean_short = calibration["calibration_badge"]
                    src_ref["plain_summary"] += f"｜邏輯驗證:{clean_short}"

            broker_summaries.append({
                "broker_name": b_name,
                "role_label": b_info["role_label"],
                "buy_vwap": round(buy_vwap, 2),
                "sell_vwap": round(sell_vwap, 2),
                "spread_pct": round(spread_pct, 2),
                "net_1d": net_1d,
                "net_5d": net_5d,
                "net_20d": net_20d,
                "last_recorded_date": str(merged.iloc[-1]["date"]) if not merged.empty else effective_anchor,
                "calibration": calibration,
                "md_path": md_path.replace("\\", "/"),
            })

        # 3. 寫入個股大戶總覽檔 00_關鍵大戶總覽.md
        overview_path = os.path.join(stock_folder, "00_關鍵大戶總覽.md")
        overview_md = self._render_stock_overview_markdown(
            stock_id=stock_id,
            stock_name=stock_name,
            anchor_date=effective_anchor,
            ref_price=ref_price,
            broker_summaries=broker_summaries,
            certified_disc=certified_disc,
        )
        with open(overview_path, "w", encoding="utf-8") as f:
            f.write(overview_md)

        logger.info(
            f"📚 [Broker KB] 已更新 {stock_id} {stock_name} 大戶戰略檔案室 ({len(broker_summaries)} 家分點) -> {overview_path}"
        )

        return {
            "stock_folder": stock_folder.replace("\\", "/"),
            "overview_path": overview_path.replace("\\", "/"),
            "brokers": broker_summaries,
            "calibrations_by_broker": calibrations_by_broker,
        }
