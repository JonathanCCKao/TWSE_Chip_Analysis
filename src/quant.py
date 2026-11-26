import json
import os
import math
import logging
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd
from src.db import ChipDatabase
from src.collector import fetch_historical_range_brokers, fetch_broker_daily_ledger
from src.signals import merge_thresholds, price_zone, derive_signals, fmt_price

from datetime import datetime

logger = logging.getLogger(__name__)

class QuantEngine:
    def __init__(
        self,
        db: ChipDatabase,
        tags_path: str = "data/broker_tags.json",
        thresholds: Optional[Dict[str, Any]] = None,
        backtest_ttl_days: int = 7,
    ):
        self.db = db
        self.broker_tags = self._load_tags(tags_path)
        self.thresholds = merge_thresholds(thresholds)
        self.backtest_ttl_days = backtest_ttl_days

    def _load_tags(self, tags_path: str) -> Dict[str, List[str]]:
        if os.path.exists(tags_path):
            try:
                with open(tags_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"讀取券商標籤檔失敗: {e}")
        return {
            "day_traders": [],
            "foreign_institutions": [],
            "government_banks": [],
            "discretionary_keywords": [],
            "discretionary_known_brokers": []
        }

    def _match_tags(self, broker_name: str, window: int, long_term_buyers: List[str]) -> List[str]:
        tags = []
        is_foreign = False

        for dt in self.broker_tags.get("day_traders", []):
            if dt in broker_name:
                tags.append("隔日沖")
                break
        for fi in self.broker_tags.get("foreign_institutions", []):
            if fi in broker_name:
                is_foreign = True
                break
        for gb in self.broker_tags.get("government_banks", []):
            if gb in broker_name:
                tags.append("官股")
                break

        is_disc = False
        for dkb in self.broker_tags.get("discretionary_known_brokers", []):
            if dkb in broker_name or broker_name == dkb:
                is_disc = True
                break
        if not is_disc:
            for dkw in self.broker_tags.get("discretionary_keywords", []):
                if dkw in broker_name:
                    is_disc = True
                    break
        if is_disc:
            tags.append("💼代操鎖碼")

        if is_foreign:
            if window in [1, 5]:
                if broker_name not in long_term_buyers:
                    tags.append("假外資/短線量化")
                else:
                    tags.append("波段外資")
            else:
                tags.append("外資")

        return tags

    def _detect_kline_swings(self, kline_1y: List[Dict[str, Any]]) -> tuple:
        """
        從過去 1 年 (約 240 個交易日) 日 K 線自動標記：
        1. 每日 20D 與 60D 區間價格位階百分位 (pct_20d, pct_60d: 0% ~ 100%)
        2. 每日往後 10 日內最大回檔幅度 (fwd_10d_drop_pct)
        3. 每日往後 20 日與 60 日內最大上漲幅度 (fwd_20d_gain_pct, fwd_60d_gain_pct)
        4. 歷史波段高點區間清單 (peaks) 與波段低點區間清單 (troughs)
        """
        df_p = pd.DataFrame(kline_1y).reset_index(drop=True)
        n = len(df_p)
        if n < 20:
            return df_p, [], []

        for i in range(n):
            w_start = max(0, i - 19)
            window_closes = df_p["Close"].iloc[w_start:i + 1]
            c_now = float(df_p["Close"].iloc[i])
            lo, hi = float(window_closes.min()), float(window_closes.max())
            df_p.loc[i, "pct_20d"] = (c_now - lo) / (hi - lo) * 100.0 if hi > lo else 50.0

            w60_start = max(0, i - 59)
            w60_closes = df_p["Close"].iloc[w60_start:i + 1]
            lo60, hi60 = float(w60_closes.min()), float(w60_closes.max())
            df_p.loc[i, "pct_60d"] = (c_now - lo60) / (hi60 - lo60) * 100.0 if hi60 > lo60 else 50.0

            fwd10 = df_p["Low"].iloc[i + 1:min(n, i + 11)]
            df_p.loc[i, "fwd_10d_drop_pct"] = ((float(fwd10.min()) - c_now) / c_now * 100.0) if not fwd10.empty else 0.0

            fwd20 = df_p["High"].iloc[i + 1:min(n, i + 21)]
            df_p.loc[i, "fwd_20d_gain_pct"] = ((float(fwd20.max()) - c_now) / c_now * 100.0) if not fwd20.empty else 0.0

            fwd60 = df_p["High"].iloc[i + 1:min(n, i + 61)]
            df_p.loc[i, "fwd_60d_gain_pct"] = ((float(fwd60.max()) - c_now) / c_now * 100.0) if not fwd60.empty else 0.0

        peaks = []
        last_p = -12
        for i in range(8, n):
            loc_max = float(df_p["Close"].iloc[max(0, i - 7):min(n, i + 6)].max())
            is_latest_high = (i >= n - 4 and df_p.loc[i, "pct_20d"] >= 88.0 and df_p.loc[i, "Close"] == loc_max)
            if df_p.loc[i, "Close"] == loc_max and df_p.loc[i, "pct_20d"] >= 85.0:
                if df_p.loc[i, "fwd_10d_drop_pct"] <= -3.8 or is_latest_high:
                    if i - last_p >= 8:
                        s_idx = max(0, i - 1)
                        e_idx = min(n - 1, i + 2)
                        peaks.append({
                            "peak_date": df_p.loc[i, "date"],
                            "start_date": df_p.loc[s_idx, "date"],
                            "end_date": df_p.loc[e_idx, "date"],
                            "close": round(float(df_p.loc[i, "Close"]), 2),
                            "drop_10d": round(float(df_p.loc[i, "fwd_10d_drop_pct"]), 2)
                        })
                        last_p = i

        troughs = []
        last_t = -12
        for i in range(8, n - 4):
            loc_min = float(df_p["Close"].iloc[max(0, i - 7):min(n, i + 6)].min())
            if df_p.loc[i, "Close"] == loc_min and df_p.loc[i, "pct_20d"] <= 20.0:
                if df_p.loc[i, "fwd_20d_gain_pct"] >= 5.0 or df_p.loc[i, "fwd_60d_gain_pct"] >= 8.0:
                    if i - last_t >= 8:
                        s_idx = max(0, i - 2)
                        e_idx = min(n - 1, i + 2)
                        troughs.append({
                            "trough_date": df_p.loc[i, "date"],
                            "start_date": df_p.loc[s_idx, "date"],
                            "end_date": df_p.loc[e_idx, "date"],
                            "close": round(float(df_p.loc[i, "Close"]), 2),
                            "gain_20d": round(float(df_p.loc[i, "fwd_20d_gain_pct"]), 2)
                        })
                        last_t = i

        return df_p, peaks, troughs

    def _score_broker_1y_ledger(
        self,
        broker_name: str,
        bhid: str,
        b_code: str,
        stock_id: str,
        df_p: pd.DataFrame,
        ref_price: float
    ) -> Optional[Dict[str, Any]]:
        """
        調閱單一分點過去 1 年逐日買賣帳本 (zco0.djhtm)，與 1 年日 K 線進行回測評分：
        - 計算 1 年加權買進均價 (buy_vwap) vs 加權賣出均價 (sell_vwap) 及正價差 (spread_pct)
        - 將大額賣超日聚類為獨立波段賣出事件 (sell_events)，計算高檔狙擊率 (high_zone_lot_rate) 與賣後回檔勝率 (sell_win_rate)
        - 將大額買超日聚類為獨立波段買進事件 (buy_events)，計算低檔抄底率 (low_zone_lot_rate) 與買後上漲勝率 (buy_win_rate)
        - 同時支援「高頻波段轉折神手 (PEAK_SNIPER / BOTTOM_BUYER)」與「大波段價差巨鱷 (WAVE_WHALE，如低檔重倉數千張、高檔集中出貨賺取 >6% 大價差之主力)」
        """
        start_d = str(df_p["date"].iloc[0])
        end_d = str(df_p["date"].iloc[-1])

        # 優先檢查本地增量帳本，僅向網路抓取未記錄到的新日期區間
        existing_rows = self.db.get_broker_daily_ledger_rows(stock_id, broker_name, start_d, end_d)
        last_recorded = self.db.get_broker_ledger_last_date(stock_id, broker_name)
        min_recorded = existing_rows[0]["date"] if existing_rows else None

        if existing_rows and min_recorded and min_recorded <= start_d and last_recorded:
            if last_recorded < end_d:
                new_trades = fetch_broker_daily_ledger(stock_id, bhid, b_code, last_recorded, end_d)
                if new_trades:
                    self.db.save_broker_daily_ledger_rows(stock_id, broker_name, bhid, b_code, new_trades)
                trades = self.db.get_broker_daily_ledger_rows(stock_id, broker_name, start_d, end_d)
            else:
                trades = existing_rows
        else:
            trades = fetch_broker_daily_ledger(stock_id, bhid, b_code, start_d, end_d)
            if trades:
                self.db.save_broker_daily_ledger_rows(stock_id, broker_name, bhid, b_code, trades)

        if not trades:
            return None

        df_t = pd.DataFrame(trades)
        merged = pd.merge(df_p, df_t, on="date", how="left")
        merged[["buy", "sell", "net"]] = merged[["buy", "sell", "net"]].fillna(0).astype(int)

        total_buy = int(merged["buy"].sum())
        total_sell = int(merged["sell"].sum())
        if total_buy + total_sell == 0:
            return None

        buy_vwap = float((merged["buy"] * merged["Close"]).sum() / total_buy) if total_buy > 0 else 0.0
        sell_vwap = float((merged["sell"] * merged["Close"]).sum() / total_sell) if total_sell > 0 else 0.0
        spread_val = (sell_vwap - buy_vwap) if (buy_vwap > 0 and sell_vwap > 0) else 0.0
        spread_pct = ((sell_vwap - buy_vwap) / buy_vwap * 100.0) if (buy_vwap > 0 and sell_vwap > 0) else 0.0

        sell_days = merged[merged["net"] < 0].copy()
        buy_days = merged[merged["net"] > 0].copy()
        active_days = int(len(merged[merged["net"] != 0]))
        n_merged = len(merged)

        # 1. 聚類波段大賣事件 (相距 5 個交易日內的顯著賣超合併為同一波高點出手)
        maj_sell_thr = max(8.0, float(np.percentile(np.abs(sell_days["net"]), 55))) if len(sell_days) >= 3 else 8.0
        maj_sells = sell_days[sell_days["net"] <= -maj_sell_thr].copy()
        sell_events = []
        for idx, row in maj_sells.iterrows():
            abs_n = abs(int(row["net"]))
            c_val = float(row["Close"])
            if not sell_events or (idx - sell_events[-1]["last_idx"]) > 5:
                sell_events.append({
                    "start_date": str(row["date"]),
                    "end_date": str(row["date"]),
                    "start_idx": int(idx),
                    "last_idx": int(idx),
                    "total_net_sell": abs_n,
                    "cost_num": abs_n * c_val,
                    "min_close": round(c_val, 2),
                    "max_close": round(c_val, 2),
                    "max_pct_20d": round(float(row["pct_20d"]), 1),
                    "max_pct_60d": round(float(row.get("pct_60d", row["pct_20d"])), 1),
                    "max_drop_10d": round(float(row["fwd_10d_drop_pct"]), 2),
                    "is_latest": bool(idx >= n_merged - 5)
                })
            else:
                ev = sell_events[-1]
                ev["end_date"] = str(row["date"])
                ev["last_idx"] = int(idx)
                ev["total_net_sell"] += abs_n
                ev["cost_num"] += abs_n * c_val
                ev["min_close"] = round(min(ev["min_close"], c_val), 2)
                ev["max_close"] = round(max(ev["max_close"], c_val), 2)
                ev["max_pct_20d"] = round(max(ev["max_pct_20d"], float(row["pct_20d"])), 1)
                ev["max_pct_60d"] = round(max(ev["max_pct_60d"], float(row.get("pct_60d", row["pct_20d"]))), 1)
                ev["max_drop_10d"] = round(min(ev["max_drop_10d"], float(row["fwd_10d_drop_pct"])), 2)
                ev["is_latest"] = ev["is_latest"] or bool(idx >= n_merged - 5)

        # 修正連續分批出貨之波段回檔計算：納入「波段最高賣點/加權賣均價 ➔ 波段期間及結束後10日內最低價」之真實壓回幅度
        for ev in sell_events:
            ev["vwap"] = round(ev["cost_num"] / ev["total_net_sell"], 2) if ev["total_net_sell"] > 0 else ev["max_close"]
            s_i = ev["start_idx"]
            e_i = min(n_merged, ev["last_idx"] + 11)
            window_after = merged.iloc[s_i + 1:e_i]
            if not window_after.empty:
                low_after = float(window_after["Low"].min()) if "Low" in window_after.columns else float(window_after["Close"].min())
                ref_high = max(ev["vwap"], ev["max_close"])
                wave_drop = round((low_after - ref_high) / ref_high * 100.0, 2) if ref_high > 0 else 0.0
                ev["max_drop_10d"] = round(min(ev["max_drop_10d"], wave_drop), 2)
            del ev["cost_num"]

        total_ev_sell_lots = sum(e["total_net_sell"] for e in sell_events)
        high_zone_events = [e for e in sell_events if e["max_pct_20d"] >= 70.0 or e.get("max_pct_60d", 50.0) >= 75.0]
        high_zone_lots = sum(e["total_net_sell"] for e in high_zone_events)
        high_zone_lot_rate = (high_zone_lots / total_ev_sell_lots * 100.0) if total_ev_sell_lots > 0 else 0.0

        completed_sell_ev = [e for e in sell_events if not e["is_latest"] or e["max_drop_10d"] <= -3.5]
        win_sell_ev = [
            e for e in completed_sell_ev
            if (e["max_pct_20d"] >= 70.0 or e.get("max_pct_60d", 50.0) >= 75.0) and e["max_drop_10d"] <= -3.5
        ]
        sell_win_rate = (len(win_sell_ev) / len(completed_sell_ev) * 100.0) if completed_sell_ev else 0.0

        # 2. 聚類波段大買事件 (相距 5 個交易日內的顯著買超合併為同一波低點抄底)
        maj_buy_thr = max(8.0, float(np.percentile(buy_days["net"], 55))) if len(buy_days) >= 3 else 8.0
        maj_buys = buy_days[buy_days["net"] >= maj_buy_thr].copy()
        buy_events = []
        for idx, row in maj_buys.iterrows():
            b_n = int(row["net"])
            c_val = float(row["Close"])
            if not buy_events or (idx - buy_events[-1]["last_idx"]) > 5:
                buy_events.append({
                    "start_date": str(row["date"]),
                    "end_date": str(row["date"]),
                    "start_idx": int(idx),
                    "last_idx": int(idx),
                    "total_net_buy": b_n,
                    "cost_num": b_n * c_val,
                    "min_close": round(c_val, 2),
                    "max_close": round(c_val, 2),
                    "min_pct_20d": round(float(row["pct_20d"]), 1),
                    "min_pct_60d": round(float(row.get("pct_60d", row["pct_20d"])), 1),
                    "max_gain_20d": round(float(row["fwd_20d_gain_pct"]), 2),
                    "max_gain_60d": round(float(row.get("fwd_60d_gain_pct", row["fwd_20d_gain_pct"])), 2),
                    "is_latest": bool(idx >= n_merged - 5)
                })
            else:
                ev = buy_events[-1]
                ev["end_date"] = str(row["date"])
                ev["last_idx"] = int(idx)
                ev["total_net_buy"] += b_n
                ev["cost_num"] += b_n * c_val
                ev["min_close"] = round(min(ev["min_close"], c_val), 2)
                ev["max_close"] = round(max(ev["max_close"], c_val), 2)
                ev["min_pct_20d"] = round(min(ev["min_pct_20d"], float(row["pct_20d"])), 1)
                ev["min_pct_60d"] = round(min(ev["min_pct_60d"], float(row.get("pct_60d", row["pct_20d"]))), 1)
                ev["max_gain_20d"] = round(max(ev["max_gain_20d"], float(row["fwd_20d_gain_pct"])), 2)
                ev["max_gain_60d"] = round(max(ev["max_gain_60d"], float(row.get("fwd_60d_gain_pct", row["fwd_20d_gain_pct"]))), 2)
                ev["is_latest"] = ev["is_latest"] or bool(idx >= n_merged - 5)

        # 修正中長線底部建倉之波段漲幅：納入「波段買進均價 (vwap) ➔ 往後 20 日與 60 日最高價」之波段拉升幅度
        for ev in buy_events:
            ev["vwap"] = round(ev["cost_num"] / ev["total_net_buy"], 2) if ev["total_net_buy"] > 0 else ev["min_close"]
            s_i = ev["start_idx"]
            e_20 = min(n_merged, ev["last_idx"] + 21)
            e_60 = min(n_merged, ev["last_idx"] + 61)
            w20 = merged.iloc[s_i + 1:e_20]
            w60 = merged.iloc[s_i + 1:e_60]
            if not w20.empty and ev["vwap"] > 0:
                hi20 = float(w20["High"].max()) if "High" in w20.columns else float(w20["Close"].max())
                ev["max_gain_20d"] = round(max(ev["max_gain_20d"], (hi20 - ev["vwap"]) / ev["vwap"] * 100.0), 2)
            if not w60.empty and ev["vwap"] > 0:
                hi60 = float(w60["High"].max()) if "High" in w60.columns else float(w60["Close"].max())
                ev["max_gain_60d"] = round(max(ev["max_gain_60d"], (hi60 - ev["vwap"]) / ev["vwap"] * 100.0), 2)
            del ev["cost_num"]

        total_ev_buy_lots = sum(e["total_net_buy"] for e in buy_events)
        low_zone_events = [e for e in buy_events if e["min_pct_20d"] <= 35.0 or e.get("min_pct_60d", 50.0) <= 30.0]
        low_zone_lots = sum(e["total_net_buy"] for e in low_zone_events)
        low_zone_lot_rate = (low_zone_lots / total_ev_buy_lots * 100.0) if total_ev_buy_lots > 0 else 0.0

        completed_buy_ev = [
            e for e in buy_events
            if not e["is_latest"] or e["max_gain_20d"] >= 4.5 or e.get("max_gain_60d", 0.0) >= 8.0
        ]
        win_buy_ev = [
            e for e in completed_buy_ev
            if (e["min_pct_20d"] <= 35.0 or e.get("min_pct_60d", 50.0) <= 30.0)
            and (e["max_gain_20d"] >= 4.5 or e.get("max_gain_60d", 0.0) >= 8.0)
        ]
        buy_win_rate = (len(win_buy_ev) / len(completed_buy_ev) * 100.0) if completed_buy_ev else 0.0

        recent_5d = merged.tail(5)
        net_5d = int(recent_5d["net"].sum())
        net_1d = int(merged.iloc[-1]["net"])
        net_20d = int(merged.tail(20)["net"].sum())
        r5_min_p = round(float(recent_5d["Close"].min()), 2)
        r5_max_p = round(float(recent_5d["Close"].max()), 2)
        r5_start = str(recent_5d["date"].iloc[0])[5:]
        r5_end = str(recent_5d["date"].iloc[-1])[5:]

        # 排除純高頻隔日沖或買高賣低之噪音券商
        is_day_trader = any(dt in broker_name for dt in self.broker_tags.get("day_traders", []))
        if is_day_trader and spread_pct < 3.0 and active_days > 180:
            return None

        # 實質波段價差獲利規模加分 (考量已實現買賣配對張數 min(total_buy, total_sell) 與正價差 %)
        paired_lots = min(total_buy, total_sell)
        # 價差與量能綜合加成：例如正價差 +14% 且配對超過 1000 張可得滿分 100
        spread_score = min(max(spread_pct, 0.0) * 6.5, 100.0)
        scale_bonus = min((paired_lots / 500.0) * max(spread_pct, 0.0), 25.0) if spread_pct > 1.5 else 0.0

        # 通道 A：波段賣頂神手 (PEAK_SNIPER) — 支援多次精準賣頂，或重倉巨鱷於高檔集中大舉出貨
        is_peak_sniper = (
            (
                (len(high_zone_events) >= 2 and high_zone_lot_rate >= 60.0 and sell_win_rate >= 55.0)
                or (len(high_zone_events) >= 1 and high_zone_lot_rate >= 70.0 and sell_win_rate >= 65.0 and spread_pct >= 5.0 and total_sell >= 300)
            )
            and (spread_pct >= 0.5 or total_buy == 0)
        )

        # 通道 B：波段買底神手 (BOTTOM_BUYER) — 支援多次精準抄底，或重倉巨鱷於底部集中大舉建倉
        is_bottom_buyer = (
            (
                (len(low_zone_events) >= 2 and low_zone_lot_rate >= 58.0 and buy_win_rate >= 55.0)
                or (len(low_zone_events) >= 1 and low_zone_lot_rate >= 65.0 and buy_win_rate >= 65.0 and spread_pct >= 5.0 and total_buy >= 300)
            )
            and spread_pct >= 0.5
        )

        # 通道 C：大波段價差巨鱷 (WAVE_WHALE) — 專抓「底部吃貨數千張、波段高點集中倒貨」賺取大額正價差之主力 (如神基 凱基-永華)
        is_wave_whale = (
            spread_pct >= 5.0
            and paired_lots >= 150
            and (high_zone_lot_rate >= 55.0 or low_zone_lot_rate >= 55.0)
            and max(sell_win_rate, buy_win_rate) >= 50.0
        )

        if not is_peak_sniper and not is_bottom_buyer and not is_wave_whale:
            return None

        peak_score = (
            0.30 * high_zone_lot_rate
            + 0.25 * sell_win_rate
            + 0.15 * min(len(high_zone_events), 6) * 16.6
            + 0.30 * spread_score
            + scale_bonus
        ) if (is_peak_sniper or is_wave_whale) else 0.0

        bottom_score = (
            0.30 * low_zone_lot_rate
            + 0.25 * buy_win_rate
            + 0.15 * min(len(low_zone_events), 6) * 16.6
            + 0.30 * spread_score
            + scale_bonus
        ) if (is_bottom_buyer or is_wave_whale) else 0.0

        # 近期正在大舉出貨或買進的實戰活躍主力給予即時戰術加權，確保當下正在動手的大鱷優先浮現
        if net_5d <= -50 or net_20d <= -200:
            peak_score += min(abs(net_5d) / 40.0 + abs(net_20d) / 150.0, 12.0)
        elif net_5d >= 50 or net_20d >= 200:
            bottom_score += min(abs(net_5d) / 40.0 + abs(net_20d) / 150.0, 12.0)

        role_type = "PEAK_SNIPER" if peak_score >= bottom_score else "BOTTOM_BUYER"
        score = round(max(peak_score, bottom_score), 1)

        # 擷取最具代表性的前波歷史紀錄 (從最近 3 次已完成的歷史波段轉折中，挑選出手張數最重大的代表作)
        past_high_sells = [e for e in high_zone_events if not e["is_latest"]]
        if past_high_sells:
            prev_peak_ev = max(past_high_sells[-3:], key=lambda x: x["total_net_sell"])
        else:
            prev_peak_ev = high_zone_events[-1] if high_zone_events else None

        past_low_buys = [e for e in low_zone_events if not e["is_latest"]]
        if past_low_buys:
            prev_trough_ev = max(past_low_buys[-3:], key=lambda x: x["total_net_buy"])
        else:
            prev_trough_ev = low_zone_events[-1] if low_zone_events else None

        return {
            "broker_name": broker_name,
            "bhid": bhid,
            "b_code": b_code,
            "role_type": role_type,
            "is_wave_whale": is_wave_whale,
            "score": score,
            "active_days": active_days,
            "total_buy": total_buy,
            "total_sell": total_sell,
            "buy_vwap": round(buy_vwap, 2),
            "sell_vwap": round(sell_vwap, 2),
            "spread_val": round(spread_val, 2),
            "spread_pct": round(spread_pct, 2),
            "sell_events_count": len(sell_events),
            "high_zone_events_count": len(high_zone_events),
            "high_zone_lot_rate": round(high_zone_lot_rate, 1),
            "sell_win_rate": round(sell_win_rate, 1),
            "buy_events_count": len(buy_events),
            "low_zone_events_count": len(low_zone_events),
            "low_zone_lot_rate": round(low_zone_lot_rate, 1),
            "buy_win_rate": round(buy_win_rate, 1),
            "prev_peak_ev": prev_peak_ev,
            "prev_trough_ev": prev_trough_ev,
            "sell_events": sell_events[-12:],
            "buy_events": buy_events[-12:],
            "net_20d": net_20d,
            "net_5d": net_5d,
            "net_1d": net_1d,
            "r5_min_p": r5_min_p,
            "r5_max_p": r5_max_p,
            "r5_range": f"{r5_start}~{r5_end}",
        }

    def _incremental_update_cached_masters(
        self,
        stock_id: str,
        anchor_date: str,
        cached: List[Dict[str, Any]],
        kline_1y: List[Dict[str, Any]],
        w_ranks: Optional[Dict[int, Dict[str, Any]]] = None,
        ref_price: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        平日輕量增量更新 ＋ 動態踢館補位機制：
        1. 沿用 7 日內已認證之 1 年期波段高手基因庫，透過本地增量帳本重算最新均價、正價差與近 5 日動向。
        2. 同時掃描當日 1D / 5D / 20D 買賣超前 3 大分點與長線大戶：若有未在榜單內的新興主力大舉出手，
           即時調閱其 1 年帳本進行動態評分踢館，一旦分數達標立即晉升納入 Top 5，無須苦等 7 天重盤點！
        """
        if not kline_1y or len(kline_1y) < 20:
            return cached

        df_p, _, _ = self._detect_kline_swings(kline_1y)
        if df_p.empty:
            return cached

        r5_klines = [k for k in kline_1y if k.get("date", "") <= anchor_date][-5:]
        if not r5_klines:
            r5_klines = kline_1y[-5:]

        start_5d = r5_klines[0]["date"]
        end_5d = r5_klines[-1]["date"]
        r5_range = f"{start_5d[5:]}~{end_5d[5:]}"

        gene_built_date = cached[0].get("gene_built_date") or cached[0].get("trade_date", anchor_date)
        logger.info(
            f"--> [Quant 1Y-Backtest 輕量增量+動態踢館] {stock_id} 沿用基因庫 (建檔: {gene_built_date})，"
            f"同步 {len(cached)} 家高手近5日 ({r5_range}) 動向並掃描新興大戶踢館..."
        )

        known_codes = self.db.get_known_broker_codes(stock_id)
        eval_pool: Dict[str, tuple] = {}

        # A. 既有快取高手
        for rec in cached:
            b_name = rec.get("broker_name", "")
            bhid = rec.get("bhid") or known_codes.get(b_name, ("", ""))[0]
            b_code = rec.get("b_code") or known_codes.get(b_name, ("", ""))[1]
            if b_name and bhid and b_code:
                eval_pool[b_name] = (bhid, b_code)

        # B. 動態踢館候選：當日 1D / 5D / 20D 買賣超前 3 名 + 120D/240D 囤貨大戶
        if w_ranks:
            for w in [1, 5, 20]:
                for side in ["BUY", "SELL"]:
                    for item in w_ranks.get(w, {}).get(side, [])[:3]:
                        b_name = item.get("broker_name", "")
                        bhid = item.get("bhid") or known_codes.get(b_name, ("", ""))[0]
                        b_code = item.get("b_code") or known_codes.get(b_name, ("", ""))[1]
                        if b_name and bhid and b_code and b_name not in eval_pool:
                            eval_pool[b_name] = (bhid, b_code)
            for w in [120, 240]:
                for item in w_ranks.get(w, {}).get("BUY", [])[:3]:
                    b_name = item.get("broker_name", "")
                    bhid = item.get("bhid") or known_codes.get(b_name, ("", ""))[0]
                    b_code = item.get("b_code") or known_codes.get(b_name, ("", ""))[1]
                    if b_name and bhid and b_code and b_name not in eval_pool:
                        eval_pool[b_name] = (bhid, b_code)

        scored_records: List[Dict[str, Any]] = []
        for b_name, (bhid, b_code) in eval_pool.items():
            rec = self._score_broker_1y_ledger(b_name, bhid, b_code, stock_id, df_p, ref_price)
            if rec:
                rec["trade_date"] = anchor_date
                rec["gene_built_date"] = gene_built_date
                scored_records.append(rec)

        if not scored_records:
            return cached

        scored_records.sort(key=lambda x: x["score"], reverse=True)
        top_records = scored_records[:5]
        self.db.save_smart_broker_backtest(stock_id, anchor_date, top_records)
        return top_records

    def _backtest_historical_swing_masters(
        self,
        stock_id: str,
        anchor_date: Optional[str],
        ref_price: float,
        profile_data: Optional[Dict[str, Any]],
        w_ranks: Dict[int, Dict[str, Any]],
        force_rebuild: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        執行 1 年期歷史波段基因回測與定期重盤點：
        - 同基準日 (trade_date == anchor_date)：0 請求直接命中 SQLite 快取
        - 7 日內新交易日 (gene_built_date 未超過 backtest_ttl_days 且非強制重算)：增量更新認證高手並執行當日踢館掃描
        - 超過 7 日或週報強制重算：完整執行 1 年 K 線高低點與全週期主力候選池深度重盤點
        """
        cached = self.db.get_smart_broker_backtest(stock_id)
        kline_1y = profile_data.get("kline_1y", []) if profile_data else []

        if cached and anchor_date and not force_rebuild:
            if all(c.get("trade_date") == anchor_date for c in cached):
                logger.info(f"⚡ [1Y-Backtest Cache Hit] {stock_id} ({anchor_date}) 已存在當日高手回測紀錄，0 網頁請求秒讀快取")
                return cached

            gene_date_str = cached[0].get("gene_built_date") or cached[0].get("trade_date", "")
            if gene_date_str:
                try:
                    d_anchor = datetime.strptime(anchor_date, "%Y-%m-%d")
                    d_gene = datetime.strptime(gene_date_str, "%Y-%m-%d")
                    if 0 <= (d_anchor - d_gene).days <= self.backtest_ttl_days:
                        return self._incremental_update_cached_masters(
                            stock_id, anchor_date, cached, kline_1y, w_ranks=w_ranks, ref_price=ref_price
                        )
                except Exception:
                    pass

        if not kline_1y or len(kline_1y) < 40:
            return cached

        logger.info(f"--> [Quant 1Y-Backtest 完整基因重盤點] 正在對 {stock_id} 執行 1 年期 K 線轉折與全週期大戶帳本深度回測...")
        df_p, peaks, troughs = self._detect_kline_swings(kline_1y)
        if df_p.empty:
            return cached

        candidates: Dict[str, tuple] = self.db.get_known_broker_codes(stock_id)
        swing_hits: Dict[str, int] = {}

        # 1. 將既有已認證的高手優先納入候選
        for c in cached:
            if c.get("bhid") and c.get("b_code"):
                candidates[c["broker_name"]] = (c["bhid"], c["b_code"])
                swing_hits[c["broker_name"]] = swing_hits.get(c["broker_name"], 0) + 3

        # 2. 將全週期 (1D/5D/20D/60D/120D/240D) 買賣超前 5 大主力無條件納入重盤點候選池 (避免遺漏長線建倉後集中出貨之波段大鱷)
        for w in [240, 120, 60, 20, 5, 1]:
            w_data = (w_ranks or {}).get(w, {})
            for side in ["BUY", "SELL"]:
                for rank_idx, item in enumerate(w_data.get(side, [])[:5]):
                    b_name = item.get("broker_name", "")
                    bhid = item.get("bhid") or candidates.get(b_name, ("", ""))[0]
                    b_code = item.get("b_code") or candidates.get(b_name, ("", ""))[1]
                    if b_name and bhid and b_code:
                        candidates[b_name] = (bhid, b_code)
                        weight = 2 if (rank_idx < 3 or w in (240, 120, 20, 5)) else 1
                        swing_hits[b_name] = swing_hits.get(b_name, 0) + weight

        # 3. 查詢過去最近 7 個波段高點區間的賣方前 10 名
        for pk in peaks[-7:]:
            res = fetch_historical_range_brokers(stock_id, pk["start_date"], pk["end_date"])
            for s in res.get("top_sellers", [])[:10]:
                if s.get("bhid") and s.get("b_code"):
                    candidates[s["broker_name"]] = (s["bhid"], s["b_code"])
                    swing_hits[s["broker_name"]] = swing_hits.get(s["broker_name"], 0) + 1

        # 4. 查詢過去最近 5 個波段低點區間的買方前 10 名
        for tr in troughs[-5:]:
            res = fetch_historical_range_brokers(stock_id, tr["start_date"], tr["end_date"])
            for b in res.get("top_buyers", [])[:10]:
                if b.get("bhid") and b.get("b_code"):
                    candidates[b["broker_name"]] = (b["bhid"], b["b_code"])
                    swing_hits[b["broker_name"]] = swing_hits.get(b["broker_name"], 0) + 1

        # 篩選權重得分 >= 2 的前 22 名嫌疑主力進行 1 年期逐日帳本深度回測
        ranked_cands = [
            name for name, cnt in sorted(swing_hits.items(), key=lambda x: x[1], reverse=True)
            if cnt >= 2 and name in candidates
        ][:22]

        scored_records: List[Dict[str, Any]] = []
        for name in ranked_cands:
            bhid, b_code = candidates[name]
            rec = self._score_broker_1y_ledger(name, bhid, b_code, stock_id, df_p, ref_price)
            if rec:
                rec["trade_date"] = anchor_date or ""
                rec["gene_built_date"] = anchor_date or ""
                scored_records.append(rec)

        scored_records.sort(key=lambda x: x["score"], reverse=True)
        top_records = scored_records[:5]

        if anchor_date and top_records:
            self.db.save_smart_broker_backtest(stock_id, anchor_date, top_records)

        return top_records

    def _format_backtest_master_card(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """將 1 年期歷史回測高手數據轉化為結構化三行戰績與當下雷達描述"""
        name = rec["broker_name"]
        role_type = rec.get("role_type", "PEAK_SNIPER")
        is_wave_whale = bool(rec.get("is_wave_whale", False) and rec.get("spread_pct", 0.0) >= 8.0)
        net_5d = rec.get("net_5d", 0)
        net_1d = rec.get("net_1d", 0)
        buy_vwap = rec.get("buy_vwap", 0.0)
        sell_vwap = rec.get("sell_vwap", 0.0)
        spread_val = rec.get("spread_val", 0.0)
        spread_pct = rec.get("spread_pct", 0.0)
        r5_range = rec.get("r5_range", "近5日")
        r5_min_p = fmt_price(rec.get("r5_min_p", 0.0))
        r5_max_p = fmt_price(rec.get("r5_max_p", 0.0))
        p_range_str = f"{r5_min_p}~{r5_max_p}元" if r5_min_p != r5_max_p else f"{r5_max_p}元"

        spread_str = (
            f"1年買均 <strong>{buy_vwap:.1f}元</strong> ➔ 賣均 <strong>{sell_vwap:.1f}元</strong> "
            f"(正價差 <strong style='color:#dc2626;'>+{spread_val:.1f}元 / +{spread_pct:.1f}%</strong>)"
            if buy_vwap > 0 and sell_vwap > 0
            else f"1年賣出均價 <strong>{sell_vwap:.1f}元</strong>"
        )

        # 前波歷史戰績描述
        prev_pk = rec.get("prev_peak_ev")
        prev_tr = rec.get("prev_trough_ev")
        history_parts = []
        if prev_tr and rec.get("low_zone_events_count", 0) >= 1:
            tr_d = (
                f"{prev_tr['start_date'][5:]}~{prev_tr['end_date'][5:]}"
                if prev_tr["start_date"] != prev_tr["end_date"]
                else prev_tr["start_date"][5:]
            )
            g20 = float(prev_tr.get("max_gain_20d", 0.0))
            g60 = float(prev_tr.get("max_gain_60d", g20))
            gain_label = f"後20日漲 +{g20:.1f}%" if g20 >= 4.5 else f"後波段漲 +{max(g20, g60):.1f}%"
            tr_p_str = fmt_price(prev_tr.get("vwap", prev_tr["min_close"]))
            history_parts.append(
                f"低檔 {tr_d} 均價 {tr_p_str}元 抄底 +{prev_tr['total_net_buy']:,}張 ({gain_label})"
            )
        if prev_pk:
            pk_d = (
                f"{prev_pk['start_date'][5:]}~{prev_pk['end_date'][5:]}"
                if prev_pk["start_date"] != prev_pk["end_date"]
                else prev_pk["start_date"][5:]
            )
            pk_p_str = fmt_price(prev_pk.get("vwap", prev_pk["max_close"]))
            history_parts.append(
                f"高檔 {pk_d} 均價 {pk_p_str}元 (最高 {fmt_price(prev_pk['max_close'])}元) 賣出 -{prev_pk['total_net_sell']:,}張 (波段回檔 {prev_pk['max_drop_10d']:.1f}%)"
            )
        prev_wave_str = " ➔ ".join(history_parts) if history_parts else "具多次波段高低點轉折操作紀錄"

        # 近 5 日最新動態描述
        if net_5d <= -10 or net_1d <= -10:
            current_action_str = (
                f"<span style='color:#dc2626;font-weight:700;'>🚨 本波最新動態：近5日({r5_range})於 {p_range_str} "
                f"再度密集賣超 {net_5d:+,}張 (今日 {net_1d:+,}張)，重現歷史高勝率賣頂訊號！</span>"
            )
        elif net_5d >= 10 or net_1d >= 10:
            current_action_str = (
                f"<span style='color:#2563eb;font-weight:700;'>🔥 本波最新動態：近5日({r5_range})於 {p_range_str} "
                f"積極買超 {net_5d:+,}張 (今日 {net_1d:+,}張)，展開波段低檔建倉佈局！</span>"
            )
        else:
            current_action_str = (
                f"<span style='color:#64748b;'>⏳ 本波最新動態：近5日({r5_range})累計 {net_5d:+,}張 (今日 {net_1d:+,}張)，"
                f"目前按兵不動，常駐監控下次關鍵轉折出手訊號。</span>"
            )

        if role_type == "PEAK_SNIPER":
            hz_cnt = rec.get("high_zone_events_count", 0)
            hz_rate = rec.get("high_zone_lot_rate", 0.0)
            win_rate = rec.get("sell_win_rate", 0.0)
            is_selling_now = (net_5d <= -10 or net_1d <= -10)
            if is_wave_whale:
                role_tag = "🐋波段賣頂巨鱷" if is_selling_now else "🐋波段價差巨鱷"
            else:
                role_tag = "🎯波段賣頂神手" if is_selling_now else "🎯歷史賣頂高手"
            badge_tag = "🎯波段賣頂"
            val_display = f"1年賣頂勝率 {win_rate:.0f}%｜正價差 +{spread_pct:.1f}%"
            desc_str = (
                f"<div><strong>① 1年回測基因</strong>：過去1年 <strong>{hz_cnt} 度</strong>精準於波段高點大賣 "
                f"(高檔狙擊率 <strong>{hz_rate:.0f}%</strong>、賣後回檔勝率 <strong>{win_rate:.0f}%</strong>)，{spread_str}</div>"
                f"<div style='margin-top:3px;'><strong>② 歷史波段實績</strong>：{prev_wave_str}</div>"
                f"<div style='margin-top:3px;'>{current_action_str}</div>"
            )
            plain_summary = (
                f"過去1年{hz_cnt}度於波段高點大賣(高檔狙擊率{hz_rate:.0f}%/回檔勝率{win_rate:.0f}%/買低賣高+{spread_pct:.1f}%)，"
                f"{prev_wave_str}；近5日({r5_range})淨進出{net_5d:+,}張(今日{net_1d:+,}張)"
            )
        else:
            lz_cnt = rec.get("low_zone_events_count", 0)
            lz_rate = rec.get("low_zone_lot_rate", 0.0)
            win_rate = rec.get("buy_win_rate", 0.0)
            is_buying_now = (net_5d >= 10 or net_1d >= 10)
            if is_wave_whale:
                role_tag = "🐋波段抄底巨鱷" if is_buying_now else "🐋波段價差巨鱷"
            else:
                role_tag = "🎯歷史買底神手" if is_buying_now else "🎯歷史抄底高手"
            badge_tag = "🎯買底神手"
            val_display = f"1年抄底勝率 {win_rate:.0f}%｜正價差 +{spread_pct:.1f}%"
            desc_str = (
                f"<div><strong>① 1年回測基因</strong>：過去1年 <strong>{lz_cnt} 度</strong>於波段低檔精準抄底 "
                f"(低檔出手率 <strong>{lz_rate:.0f}%</strong>、買後上漲勝率 <strong>{win_rate:.0f}%</strong>)，{spread_str}</div>"
                f"<div style='margin-top:3px;'><strong>② 歷史波段實績</strong>：{prev_wave_str}</div>"
                f"<div style='margin-top:3px;'>{current_action_str}</div>"
            )
            plain_summary = (
                f"過去1年{lz_cnt}度於波段低點抄底(低檔出手率{lz_rate:.0f}%/上漲勝率{win_rate:.0f}%/買低賣高+{spread_pct:.1f}%)，"
                f"{prev_wave_str}；近5日({r5_range})淨進出{net_5d:+,}張(今日{net_1d:+,}張)"
            )

        return {
            "broker_name": name,
            "bhid": rec.get("bhid", ""),
            "b_code": rec.get("b_code", ""),
            "role_type": role_type,
            "role": role_tag,
            "badge": badge_tag,
            "action": "SELL" if role_type == "PEAK_SNIPER" else "BUY",
            "period": 5 if abs(net_5d) >= abs(net_1d) else 1,
            "ratio": 0.0,
            "bias_pct": spread_pct,
            "val_display": val_display,
            "desc": desc_str,
            "plain_summary": plain_summary,
            "net_5d": net_5d,
            "net_1d": net_1d,
            "high_zone_events_count": rec.get("high_zone_events_count", 0),
            "sell_win_rate": rec.get("sell_win_rate", 0.0),
            "low_zone_events_count": rec.get("low_zone_events_count", 0),
            "buy_win_rate": rec.get("buy_win_rate", 0.0),
            "buy_vwap": buy_vwap,
            "sell_vwap": sell_vwap,
            "spread_val": spread_val,
            "spread_pct": spread_pct,
            "is_1y_certified": True,
            "raw_backtest": rec,
        }

    def _detect_smart_brokers(
        self,
        stock_id: str,
        trade_date: Optional[str],
        ref_price: float,
        profile_data: Optional[Dict[str, Any]],
        p_zone: Optional[Dict[str, Any]] = None,
        force_rebuild_backtest: bool = False,
    ) -> Dict[str, Any]:
        """
        偵測兩類關鍵神準主力分點：
        1. 【🔒 長線囤貨大戶】(120/240日大舉買超、純度高且跨週期持續鎖碼)
        2. 【🎯 波段神算高手】(優先透過 1 年期 K 線轉折與分點逐日帳本回測認證之常駐高手，輔以當下波段異動監控)
        """
        w_ranks = {}
        for w in [1, 5, 20, 60, 120, 240]:
            b_list = self.db.get_period_ranks(stock_id, w, side="BUY", trade_date=trade_date, limit=5)
            s_list = self.db.get_period_ranks(stock_id, w, side="SELL", trade_date=trade_date, limit=5)
            summ = self.db.get_period_summary(stock_id, w, trade_date=trade_date)
            # 冷數據 (60/120/240D) 若當日沿用近期快取未重抓，自動 fallback 讀取最新一筆快取
            if not b_list and trade_date and w in (60, 120, 240):
                b_list = self.db.get_period_ranks(stock_id, w, side="BUY", trade_date=None, limit=5)
            if not s_list and trade_date and w in (60, 120, 240):
                s_list = self.db.get_period_ranks(stock_id, w, side="SELL", trade_date=None, limit=5)
            if not summ and trade_date and w in (60, 120, 240):
                summ = self.db.get_period_summary(stock_id, w, trade_date=None)

            w_ranks[w] = {
                "BUY": b_list,
                "SELL": s_list,
                "summary": summ,
            }

        hoarders = []
        seen_hoarders = set()
        broker_badges = {}

        b60_names = {b["broker_name"]: b for b in w_ranks[60]["BUY"]}
        b120_names = {b["broker_name"]: b for b in w_ranks[120]["BUY"]}
        b240_names = {b["broker_name"]: b for b in w_ranks[240]["BUY"]}

        s60_names = {s["broker_name"]: s for s in w_ranks[60]["SELL"]}
        s120_names = {s["broker_name"]: s for s in w_ranks[120]["SELL"]}
        s240_names = {s["broker_name"]: s for s in w_ranks[240]["SELL"]}

        disc_brokers = self.broker_tags.get("discretionary_known_brokers", [])
        disc_keywords = self.broker_tags.get("discretionary_keywords", [])

        def _is_disc(bname: str) -> bool:
            return (
                bname in disc_brokers or
                any(k in bname for k in disc_keywords) or
                bname in ["富邦證券", "元大證券", "凱基證券", "國泰證券", "臺灣銀行"]
            )

        # 1. 偵測長線囤貨大戶 (優先 240 日，其次 120 日)
        for name, b240 in b240_names.items():
            net = b240.get("net_shares", 0)
            tot = b240.get("buy_shares", 0) + b240.get("sell_shares", 0)
            purity = (b240.get("buy_shares", 0) / tot) if tot > 0 else 0
            is_in_120 = name in b120_names
            is_in_60 = name in b60_names
            is_d = _is_disc(name)

            if (purity >= 0.55 or is_in_120 or is_d) and b240.get("ratio", 0) >= 0.3:
                hoarders.append({
                    "broker_name": name,
                    "bhid": b240.get("bhid", ""),
                    "b_code": b240.get("b_code", ""),
                    "period": 240,
                    "net_shares": net,
                    "buy_shares": b240.get("buy_shares", 0),
                    "sell_shares": b240.get("sell_shares", 0),
                    "ratio": b240.get("ratio", 0.0),
                    "purity": round(purity * 100, 1),
                    "continuous": is_in_120 or is_in_60,
                    "badge": "🔒長線囤貨",
                    "is_discretionary": is_d
                })
                seen_hoarders.add(name)
                broker_badges.setdefault(name, []).append("🔒長線囤貨")
                if is_d:
                    broker_badges.setdefault(name, []).append("💼代操鎖碼")

        for name, b120 in b120_names.items():
            if name in seen_hoarders:
                continue
            net = b120.get("net_shares", 0)
            tot = b120.get("buy_shares", 0) + b120.get("sell_shares", 0)
            purity = (b120.get("buy_shares", 0) / tot) if tot > 0 else 0
            is_in_60 = name in b60_names
            is_d = _is_disc(name)

            if (purity >= 0.58 or is_in_60 or is_d) and b120.get("ratio", 0) >= 0.5:
                hoarders.append({
                    "broker_name": name,
                    "bhid": b120.get("bhid", ""),
                    "b_code": b120.get("b_code", ""),
                    "period": 120,
                    "net_shares": net,
                    "buy_shares": b120.get("buy_shares", 0),
                    "sell_shares": b120.get("sell_shares", 0),
                    "ratio": b120.get("ratio", 0.0),
                    "purity": round(purity * 100, 1),
                    "continuous": is_in_60,
                    "badge": "🔒長線囤貨",
                    "is_discretionary": is_d
                })
                seen_hoarders.add(name)
                broker_badges.setdefault(name, []).append("🔒長線囤貨")
                if is_d:
                    broker_badges.setdefault(name, []).append("💼代操鎖碼")

        # 1.5 偵測 20D / 60D 委外代操與法人總部鎖碼大戶
        for w in [20, 60]:
            if w in w_ranks and w_ranks[w].get("BUY"):
                for b in w_ranks[w]["BUY"]:
                    name = b["broker_name"]
                    if name in seen_hoarders:
                        continue
                    tot = b.get("buy_shares", 0) + b.get("sell_shares", 0)
                    purity = (b.get("buy_shares", 0) / tot) if tot > 0 else 0
                    if _is_disc(name) and purity >= 0.80 and b.get("net_shares", 0) >= 50:
                        hoarders.append({
                            "broker_name": name,
                            "bhid": b.get("bhid", ""),
                            "b_code": b.get("b_code", ""),
                            "period": w,
                            "net_shares": b.get("net_shares", 0),
                            "buy_shares": b.get("buy_shares", 0),
                            "sell_shares": b.get("sell_shares", 0),
                            "ratio": b.get("ratio", 0.0),
                            "purity": round(purity * 100, 1),
                            "continuous": True,
                            "badge": "💼代操鎖碼",
                            "is_discretionary": True
                        })
                        seen_hoarders.add(name)
                        broker_badges.setdefault(name, []).append("💼代操鎖碼")

        # 2. 偵測波段神算高手：第一優先執行「1 年期歷史波段基因回測」
        swing_masters = []
        seen_swing = set()

        backtest_records = self._backtest_historical_swing_masters(
            stock_id, trade_date, ref_price, profile_data, w_ranks, force_rebuild=force_rebuild_backtest
        )
        for rec in backtest_records:
            card = self._format_backtest_master_card(rec)
            bname = card["broker_name"]
            if _is_disc(bname):
                card["is_discretionary"] = True
                broker_badges.setdefault(bname, []).append("💼代操席位")
            swing_masters.append(card)
            seen_swing.add(bname)
            broker_badges.setdefault(bname, []).append(card["badge"])

        # 3. 補充當下長線大買方突發調節訊號 (長線囤貨大戶於 1日/5日大賣佔比 >= 5.0%)
        s20_summary = w_ranks[20]["summary"]
        vwap_20 = s20_summary.get("avg_buy_cost", 0.0) if s20_summary else 0.0
        is_high_zone = bool(p_zone and p_zone.get("is_high_zone"))

        for w in [1, 5]:
            for s in w_ranks[w]["SELL"][:5]:
                name = s["broker_name"]
                if name in seen_swing:
                    continue
                is_long_buyer = (name in b60_names or name in b120_names or name in b240_names)
                if s.get("ratio", 0) >= 5.0 and is_long_buyer:
                    if is_high_zone:
                        role_tag = "🎯高檔了結"
                        desc_str = f"先前為長線累積大買方，今日({w}日)於波段相對高檔大賣調節，賣超佔比達 {s.get('ratio', 0.0):.1f}%"
                    else:
                        role_tag = "⚠️破線調節"
                        desc_str = f"先前為長線買方，現價跌破月線後今日({w}日)大賣調節，賣超佔比達 {s.get('ratio', 0.0):.1f}%，留意停損或轉空賣壓"
                    swing_masters.append({
                        "broker_name": name,
                        "bhid": s.get("bhid", ""),
                        "b_code": s.get("b_code", ""),
                        "role": role_tag,
                        "action": "SELL",
                        "period": w,
                        "ratio": s.get("ratio", 0.0),
                        "bias_pct": 0.0,
                        "val_display": f"{w}日賣超佔比 {s.get('ratio', 0.0):.1f}%",
                        "desc": desc_str,
                        "plain_summary": desc_str,
                        "is_1y_certified": False
                    })
                    seen_swing.add(name)
                    broker_badges.setdefault(name, []).append(role_tag)

        # 4. 若 1 年期回測認證名單少於 2 家，補充 20 日買底正乖離領先分點
        if len(swing_masters) < 2:
            for b in w_ranks[20]["BUY"]:
                name = b["broker_name"]
                if name in seen_swing:
                    continue
                bias = ((ref_price - vwap_20) / vwap_20 * 100) if vwap_20 > 0 and ref_price > 0 else 0.0
                if b.get("ratio", 0) >= 2.5 and bias >= 2.0:
                    desc_str = f"20日買超佔比 {b.get('ratio', 0.0):.1f}%，現價較20日主力均價 ({vwap_20:.2f}元) 拉開 +{bias:.1f}% 獲利空間"
                    swing_masters.append({
                        "broker_name": name,
                        "bhid": b.get("bhid", ""),
                        "b_code": b.get("b_code", ""),
                        "role": "🎯買底神手",
                        "action": "BUY",
                        "period": 20,
                        "ratio": b.get("ratio", 0.0),
                        "bias_pct": round(bias, 2),
                        "val_display": f"較20D均價正乖離 +{bias:.1f}%",
                        "desc": desc_str,
                        "plain_summary": desc_str,
                        "is_1y_certified": False
                    })
                    seen_swing.add(name)
                    broker_badges.setdefault(name, []).append("🎯買底神手")

        return {
            "hoarders": hoarders,
            "swing_masters": swing_masters,
            "broker_badges": broker_badges
        }

    def _evaluate_material_signal(self, analysis_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        評估當日是否觸發「重大籌碼或技術異動訊號」(供 ai_policy == 'signal_only' 智慧節流判斷)：
        1. 1 年期認證波段高手近 5 日或今日有顯著買賣出手 (|net_5d| >= 15 或 |net_1d| >= 10)
        2. 隔日沖或短線假外資重倉警報 (behavior_flags 非空)
        3. 三大法人單日大買或大賣
        4. 均線黃金交叉或出量突破 (is_vol_surge / cross_5_20 / cross_5_60)
        5. 集保大戶週集中度顯著變動 (|concentration_diff| >= 0.3%)
        """
        reasons: List[str] = []
        for sm in analysis_result.get("smart_brokers", {}).get("swing_masters", []):
            if sm.get("is_1y_certified"):
                n5 = sm.get("net_5d", 0)
                n1 = sm.get("net_1d", 0)
                if abs(n5) >= 15 or abs(n1) >= 10:
                    reasons.append(f"1年認證高手「{sm['broker_name']}」出手(5日{n5:+,}張/今日{n1:+,}張)")
            elif sm.get("period") == 1:
                reasons.append(f"長線主力「{sm['broker_name']}」單日大幅調節({sm.get('ratio', 0):.1f}%)")

        w1_flags = analysis_result.get("windows_metrics", {}).get(1, {}).get("behavior_flags", [])
        if w1_flags:
            reasons.append("短線隔日沖/量化席位異常重倉")

        derived = analysis_result.get("derived_signals", {})
        inst_tone = derived.get("inst", {}).get("tone", "neutral")
        if inst_tone in ("bull", "bear"):
            reasons.append(f"法人顯著表態({derived.get('inst', {}).get('label', '')})")

        tech = analysis_result.get("technical_summary", {}) or {}
        if tech.get("is_vol_surge") or tech.get("cross_5_20") or tech.get("cross_5_60") or tech.get("cross_20_60"):
            reasons.append("技術面帶量攻擊或均線交叉")

        disp = analysis_result.get("dispersion_summary", {}) or {}
        if abs(disp.get("concentration_diff", 0.0)) >= 0.3:
            reasons.append(f"集保大戶集中度週變動({disp.get('concentration_diff', 0.0):+.2f}%)")

        gov_disc = analysis_result.get("gov_discretionary_summary", {})
        if "護盤" in gov_disc.get("gov_banks", {}).get("action_label", ""):
            reasons.append("八大官股逆勢護盤支撐")
        if "重倉" in gov_disc.get("discretionary", {}).get("action_label", ""):
            reasons.append("代操機構波段重倉鎖碼")

        return {
            "is_material": len(reasons) > 0,
            "reasons": reasons,
        }

    def _evaluate_government_and_discretionary(
        self,
        stock_id: str,
        anchor_date: Optional[str],
        tech: Dict[str, Any],
        institutional_data: Optional[Dict[str, Any]] = None,
        force_rebuild: bool = False,
    ) -> Dict[str, Any]:
        """
        全量多週期八大官股與法人/基金代操特定分點動態分析：
        1. 八大官股 (臺銀、土銀、合庫、兆豐、一銀、華南、彰銀、企銀) 多週期累計買賣超與逆勢/順勢態度
        2. 法人/基金委外代操 (認證機制 + 14~30天 TTL 快取)：
           - 認證條件：近20日買進純度 >= 85% 且累計淨買超 >= 100 張
           - 輕量增量讀取：命中快取時 O(1) 增量追蹤 1D/5D 進出，大幅降低重複多週期運算
        """
        gov_keywords = self.broker_tags.get("government_banks", [])
        disc_keywords = self.broker_tags.get("discretionary_keywords", [])
        disc_brokers = self.broker_tags.get("discretionary_known_brokers", [])

        # 1. 優先檢查 SQLite 認證代操快取表 (TTL 21 天)
        cached_certified = self.db.get_certified_discretionary_brokers(stock_id, as_of_date=anchor_date)
        is_cache_hit = bool(cached_certified) and not force_rebuild
        certified_desks: List[Dict[str, Any]] = []

        gov_stats = {1: 0, 5: 0, 20: 0}
        disc_stats = {1: 0, 5: 0, 20: 0}
        active_gov_branches = []
        active_disc_desks = []

        # 取得 1D 與 5D 分點排行 (輕量增量讀取核心)
        r1_buy = self.db.get_period_ranks(stock_id, 1, side="BUY", trade_date=anchor_date, limit=15)
        r1_sell = self.db.get_period_ranks(stock_id, 1, side="SELL", trade_date=anchor_date, limit=15)
        r5_buy = self.db.get_period_ranks(stock_id, 5, side="BUY", trade_date=anchor_date, limit=15)
        r5_sell = self.db.get_period_ranks(stock_id, 5, side="SELL", trade_date=anchor_date, limit=15)

        if is_cache_hit:
            exp_date = cached_certified[0].get("expires_at", "")
            logger.info(
                f"⚡ [Discretionary Cache Hit] {stock_id} 命中認證代操清單 "
                f"({len(cached_certified)} 家: {', '.join([c['broker_name'] for c in cached_certified])}，"
                f"有效至 {exp_date})，採用 O(1) 增量追蹤"
            )
            certified_desks = cached_certified
            certified_names = {c["broker_name"] for c in certified_desks}
            # 20D 基準直接命中快取累計張數
            disc_stats[20] = sum(c.get("net_shares_20d", 0) for c in certified_desks)
        else:
            # 完整 20D 認證檢驗與晉升
            r20_buy = self.db.get_period_ranks(stock_id, 20, side="BUY", trade_date=anchor_date, limit=15)
            r20_sell = self.db.get_period_ranks(stock_id, 20, side="SELL", trade_date=anchor_date, limit=15)
            s20 = self.db.get_period_summary(stock_id, 20, anchor_date) or self.db.get_period_summary(stock_id, 20, None)
            vwap_20 = float(s20.get("avg_buy_cost", 0.0)) if s20 else float(tech.get("close", 0.0) or 0.0)

            # 掃描 20D 買方席位中符合「純度 >= 85%」且「買超 >= 100 張」之代操特徵席位
            for b in r20_buy:
                b_name = b["broker_name"]
                buy_sh = b.get("buy_shares", 0)
                sell_sh = b.get("sell_shares", 0)
                net_sh = b.get("net_shares", 0)
                tot = buy_sh + sell_sh
                purity = (buy_sh / tot * 100.0) if tot > 0 else 0.0

                is_target_desk = (
                    any(k in b_name for k in disc_brokers) or
                    any(k in b_name for k in disc_keywords) or
                    b_name in ["富邦證券", "元大證券", "凱基證券", "國泰證券", "臺灣銀行"]
                )
                if purity >= 85.0 and net_sh >= 100 and (is_target_desk or net_sh >= 200):
                    certified_desks.append({
                        "broker_name": b_name,
                        "bhid": b.get("bhid", ""),
                        "b_code": b.get("b_code", ""),
                        "buy_purity": round(purity, 1),
                        "net_shares_20d": int(net_sh),
                        "ratio_20d": round(float(b.get("ratio", 0.0)), 2),
                        "vwap_buy_cost": round(vwap_20, 1),
                        "status": "CERTIFIED_LOCK",
                    })

            if certified_desks:
                self.db.save_certified_discretionary_brokers(stock_id, anchor_date or datetime.now().strftime("%Y-%m-%d"), certified_desks, ttl_days=21)
                logger.info(
                    f"💼 [Discretionary Promoted] {stock_id} 自動晉升 {len(certified_desks)} 家認證代操鎖碼席位: "
                    f"{', '.join([c['broker_name'] for c in certified_desks])} (TTL: 21天)"
                )
            certified_names = {c["broker_name"] for c in certified_desks}
            # 統計 20D 總量
            disc_buy_20 = sum(b.get("net_shares", 0) for b in r20_buy if b["broker_name"] in certified_names or any(k in b["broker_name"] for k in disc_brokers))
            disc_sell_20 = sum(s.get("net_shares", 0) for s in r20_sell if s["broker_name"] in certified_names or any(k in s["broker_name"] for k in disc_brokers))
            disc_stats[20] = disc_buy_20 - disc_sell_20

        # 計算 1D 與 5D 代操增量進出
        for w, (b_ranks, s_ranks) in [(1, (r1_buy, r1_sell)), (5, (r5_buy, r5_sell))]:
            w_disc_buy = 0
            w_disc_sell = 0
            for b in b_ranks:
                b_name = b["broker_name"]
                shares = b.get("net_shares", 0)
                is_disc = (
                    b_name in certified_names or
                    any(k in b_name for k in disc_brokers) or
                    any(k in b_name for k in disc_keywords) or
                    b_name in ["富邦證券", "元大證券", "凱基證券", "國泰證券", "臺灣銀行"]
                )
                if is_disc:
                    w_disc_buy += shares
                    if w == 1:
                        active_disc_desks.append(f"{b_name}(+{shares})")
            for s in s_ranks:
                s_name = s["broker_name"]
                shares = s.get("net_shares", 0)
                is_disc = (
                    s_name in certified_names or
                    any(k in s_name for k in disc_brokers) or
                    any(k in s_name for k in disc_keywords) or
                    s_name in ["富邦證券", "元大證券", "凱基證券", "國泰證券", "臺灣銀行"]
                )
                if is_disc:
                    w_disc_sell += shares
                    if w == 1:
                        active_disc_desks.append(f"{s_name}(-{shares})")
            disc_stats[w] = w_disc_buy - w_disc_sell

        # 官股多週期計算 (1D, 5D, 20D)
        for w, (b_ranks, s_ranks) in [(1, (r1_buy, r1_sell)), (5, (r5_buy, r5_sell))]:
            g_b = sum(b.get("net_shares", 0) for b in b_ranks if any(k in b["broker_name"] for k in gov_keywords))
            g_s = sum(s.get("net_shares", 0) for s in s_ranks if any(k in s["broker_name"] for k in gov_keywords))
            gov_stats[w] = g_b - g_s
            if w == 1:
                for b in b_ranks:
                    if any(k in b["broker_name"] for k in gov_keywords):
                        active_gov_branches.append(f"{b['broker_name']}(+{b.get('net_shares', 0)})")
                for s in s_ranks:
                    if any(k in s["broker_name"] for k in gov_keywords):
                        active_gov_branches.append(f"{s['broker_name']}(-{s.get('net_shares', 0)})")

        r20_b_all = self.db.get_period_ranks(stock_id, 20, side="BUY", trade_date=anchor_date, limit=15)
        r20_s_all = self.db.get_period_ranks(stock_id, 20, side="SELL", trade_date=anchor_date, limit=15)
        gov_stats[20] = (
            sum(b.get("net_shares", 0) for b in r20_b_all if any(k in b["broker_name"] for k in gov_keywords)) -
            sum(s.get("net_shares", 0) for s in r20_s_all if any(k in s["broker_name"] for k in gov_keywords))
        )

        gov_1d = gov_stats[1]
        gov_5d = gov_stats[5]
        gov_20d = gov_stats[20]

        disc_1d = disc_stats[1]
        disc_5d = disc_stats[5]
        disc_20d = disc_stats[20]

        c_price = tech.get("close", 0.0) if tech else 0.0
        ma5_price = tech.get("ma5", 0.0) if tech else 0.0
        is_weak = c_price < ma5_price if (c_price > 0 and ma5_price > 0) else False

        gov_action_label = "⚪ 官股中性觀望"
        gov_action_desc = "當日官股進出未見明顯單向干預，維持常態換手"
        if gov_1d >= 15 or (gov_5d >= 30 and gov_1d > 0):
            if is_weak:
                gov_action_label = "🟢 國家隊逆勢護盤"
                gov_action_desc = f"股價偏弱回檔之際，官股券商今日逆勢逢低吸籌 +{gov_1d:,} 張 (近5日累計 +{gov_5d:,} 張)，發揮下檔護盤支撐力道"
            else:
                gov_action_label = "🟢 官股買超護航"
                gov_action_desc = f"官股券商今日偏多承接 +{gov_1d:,} 張 (近5日累計 +{gov_5d:,} 張)，穩健看多"
        elif gov_1d <= -15 or (gov_5d <= -30 and gov_1d < 0):
            if not is_weak:
                gov_action_label = "🟠 官股逢高調節"
                gov_action_desc = f"逢股價反彈或強勢，官股券商單日獲利調節 {gov_1d:,} 張 (近5日累計 {gov_5d:,} 張)，符合官方逆勢逢高提款慣性"
            else:
                gov_action_label = "🟠 官股微幅結算"
                gov_action_desc = f"官股券商今日賣超 {gov_1d:,} 張 (近5日累計 {gov_5d:,} 張)"
        elif gov_5d >= 50:
            gov_action_label = "🟢 官股波段偏多"
            gov_action_desc = f"官股近5日累計買超 +{gov_5d:,} 張，維持護盤基調"
        elif gov_5d <= -50:
            gov_action_label = "🟠 官股波段調節"
            gov_action_desc = f"官股近5日累計調節 {gov_5d:,} 張，高檔逢高獲利了結"

        trust_net = institutional_data.get("investment_trust_net", 0) if institutional_data else 0
        disc_action_label = "⚪ 代操常態進出"
        disc_action_desc = "委外代操與總公司法人席位維持常態進出，無單向重度持倉"
        trust_resonance = False

        if disc_5d >= 100 or disc_20d >= 300:
            disc_action_label = "🔥 代操波段重倉鎖碼"
            disc_action_desc = f"總公司與委外代操席位近5日大買 +{disc_5d:,} 張 (近20日累計高達 +{disc_20d:,} 張)，籌碼呈單向高度純度囤貨鎖碼"
            if trust_net > 0:
                trust_resonance = True
                disc_action_desc += "，並與投信買超同向共振，波段底氣扎實！"
        elif disc_1d >= 30 or disc_5d >= 50:
            disc_action_label = "🟢 代操積極吸籌"
            disc_action_desc = f"委外代操/總部法人單日買超 +{disc_1d:,} 張 (近5日累計 +{disc_5d:,} 張)，展現積極布局意圖"
            if trust_net > 0:
                trust_resonance = True
                disc_action_desc += "，且與投信買盤同步"
        elif disc_5d <= -100 or disc_20d <= -300:
            disc_action_label = "🚨 代操波段退場調節"
            disc_action_desc = f"委外代操/總部法人近5日調節 {disc_5d:,} 張 (近20日累計 {disc_20d:,} 張)，留意法人委外帳戶獲利結算賣壓"
        elif disc_1d <= -30:
            disc_action_label = "⚠️ 代操逢高減碼"
            disc_action_desc = f"委外代操/總部法人單日賣超 {disc_1d:,} 張，短線浮現調節動作"

        macro_alert = None
        if "護盤" in gov_action_label:
            macro_alert = f"🏛️ 【八大官股逆勢護盤】官股分點今日逆勢買超 +{gov_1d:,} 張 (近5日累計 +{gov_5d:,} 張)，低檔發揮國家隊穩盤緩衝效果！"
        elif "逢高調節" in gov_action_label:
            macro_alert = f"🏛️ 【八大官股逢高調節】官股分點今日順勢調節 {gov_1d:,} 張 (近5日累計 {gov_5d:,} 張)，符合官方逆勢不追高慣性。"

        disc_alert = None
        if "重倉" in disc_action_label or "吸籌" in disc_action_label:
            disc_alert = f"💼 【代操機構波段重倉】總公司/委外代操席位近5日累計淨買超 +{disc_5d:,} 張 (近20日累計 +{disc_20d:,} 張)，籌碼展現高度穩定度！"
        elif "波段退場" in disc_action_label:
            disc_alert = f"🚨 【代操機構持續調節】總公司/委外代操席位近5日累計賣超 {disc_5d:,} 張，留意法人委外帳戶換手賣壓！"

        return {
            "gov_banks": {
                "net_1d": gov_1d,
                "net_5d": gov_5d,
                "net_20d": gov_20d,
                "action_label": gov_action_label,
                "action_desc": gov_action_desc,
                "active_branches": active_gov_branches[:4],
            },
            "discretionary": {
                "net_1d": disc_1d,
                "net_5d": disc_5d,
                "net_20d": disc_20d,
                "action_label": disc_action_label,
                "action_desc": disc_action_desc,
                "trust_resonance": trust_resonance,
                "active_desks": active_disc_desks[:4],
                "certified_desks": certified_desks,
                "is_cache_hit": is_cache_hit,
            },
            "gov_macro_alert": macro_alert,
            "disc_macro_alert": disc_alert,
        }

        return {
            "gov_banks": {
                "net_1d": gov_1d,
                "net_5d": gov_5d,
                "net_20d": gov_20d,
                "action_label": gov_action_label,
                "action_desc": gov_action_desc,
                "active_branches": active_gov_branches[:4],
            },
            "discretionary": {
                "net_1d": disc_1d,
                "net_5d": disc_5d,
                "net_20d": disc_20d,
                "action_label": disc_action_label,
                "action_desc": disc_action_desc,
                "trust_resonance": trust_resonance,
                "active_desks": active_disc_desks[:4],
            },
            "gov_macro_alert": macro_alert,
            "disc_macro_alert": disc_alert,
        }

    def analyze_stock(
        self,
        stock_id: str,
        windows: List[int],
        trade_date: Optional[str] = None,
        institutional_data: Optional[Dict[str, Any]] = None,
        margin_data: Optional[Dict[str, Any]] = None,
        dispersion_data: Optional[Dict[str, Any]] = None,
        profile_data: Optional[Dict[str, Any]] = None,
        force_rebuild_backtest: bool = False,
    ) -> Dict[str, Any]:
        """
        全量多週期量化運算、真假外資、資券背離、集保大戶趨勢與籌碼峰 (Volume Profile) 判定
        """
        data_warnings: List[str] = []
        analysis_result = {
            "stock_id": stock_id,
            "trade_date": trade_date,
            "reference_price": 0.0,
            "windows_metrics": {},
            "institutional_summary": institutional_data or {},
            "margin_summary": margin_data or {},
            "dispersion_summary": dispersion_data or {},
            "profile_summary": profile_data or {},
            "macro_signals": [],
            "data_warnings": data_warnings
        }

        # 1. 以 1 日分點資料日期作為基準日 (Anchor Date)，驗證跨來源資料新鮮度
        day1_summary = self.db.get_period_summary(stock_id, 1, trade_date)
        anchor_date = trade_date
        if day1_summary:
            anchor_date = day1_summary["trade_date"]
            analysis_result["trade_date"] = anchor_date
            analysis_result["reference_price"] = day1_summary["avg_buy_cost"]

        # 驗證法人與融資融券日期是否與基準日同步 (防止 21:45 前誤用 T-1 資料卻未標示)
        if anchor_date:
            if margin_data and margin_data.get("iso_date") and margin_data["iso_date"] != anchor_date:
                w_msg = f"融資融券資料日期 ({margin_data['iso_date']}) 早於分點基準日 ({anchor_date})，為前一日 (T-1) 結算數據"
                data_warnings.append(w_msg)
                analysis_result["macro_signals"].append(f"⏳ 【資券為前日資料】{w_msg}")
            if institutional_data and institutional_data.get("iso_date") and institutional_data["iso_date"] != anchor_date:
                w_msg = f"三大法人資料日期 ({institutional_data['iso_date']}) 與分點基準日 ({anchor_date}) 不一致"
                data_warnings.append(w_msg)

        # 1.5 取得長線核心買方分點清單 (統一涵蓋 60 / 120 / 240 日)
        long_term_buyers = set()
        for lw in [60, 120, 240]:
            ranks = self.db.get_period_ranks(stock_id, lw, side="BUY", trade_date=anchor_date, limit=5)
            if not ranks and anchor_date:
                ranks = self.db.get_period_ranks(stock_id, lw, side="BUY", trade_date=None, limit=5)
            for r in ranks:
                long_term_buyers.add(r["broker_name"])

        # 2. 優先從技術面 (含證交所官方行情校正) 取得正式收盤價 (Close)
        close_price = None
        tech = profile_data.get("technical_signals", {}) if profile_data else {}
        if profile_data:
            if tech and tech.get("close") and not math.isnan(tech.get("close")):
                close_price = float(tech.get("close"))
            elif profile_data.get("kline_data") and profile_data["kline_data"].get("candles"):
                candles = profile_data["kline_data"]["candles"]
                if candles and len(candles[-1]) >= 2 and not math.isnan(candles[-1][1]):
                    close_price = float(candles[-1][1])

        if close_price is not None and not math.isnan(close_price) and close_price > 0:
            analysis_result["reference_price"] = close_price
            analysis_result["close_price"] = close_price

        ref_price = analysis_result["reference_price"]

        # 2.2 計算股價位階 (高檔區 vs 月線下回檔區)
        s60 = self.db.get_period_summary(stock_id, 60, anchor_date) or self.db.get_period_summary(stock_id, 60, None)
        vwap_60 = s60.get("avg_buy_cost", 0.0) if s60 else 0.0
        p_zone = price_zone(ref_price, tech, profile_data, vwap_60, self.thresholds)
        analysis_result["price_zone"] = p_zone

        # 2.5 偵測長線囤貨大戶與波段神算高手
        smart_brokers = self._detect_smart_brokers(
            stock_id,
            anchor_date,
            ref_price,
            profile_data,
            p_zone=p_zone,
            force_rebuild_backtest=force_rebuild_backtest,
        )
        analysis_result["smart_brokers"] = smart_brokers
        smart_badges_map = smart_brokers.get("broker_badges", {})

        # 若有極為顯著的神準主力動態 (含 1 年期認證高手本波出貨/抄底)，加入宏觀訊號提示
        for sm in smart_brokers.get("swing_masters", []):
            if sm.get("is_1y_certified"):
                n5 = sm.get("net_5d", 0)
                n1 = sm.get("net_1d", 0)
                if sm["action"] == "SELL" and (n5 <= -15 or n1 <= -10):
                    move_txt = (
                        f"近5日再度連賣 {abs(n5):,} 張 (今日 {n1:+,} 張)"
                        if n5 < 0
                        else f"今日單日轉賣 {n1:+,} 張 (近5日累計 {n5:+,} 張)"
                    )
                    analysis_result["macro_signals"].append(
                        f"🎯 【1年認證賣頂高手出貨】過去1年 {sm.get('high_zone_events_count', 0)} 度精準賣頂之「{sm['broker_name']}」"
                        f"(勝率 {sm.get('sell_win_rate', 0):.0f}%、正價差 +{sm.get('spread_pct', 0):.1f}%)，{move_txt}，嚴防高檔見頂賣壓！"
                    )
                elif sm["action"] == "BUY" and (n5 >= 15 or n1 >= 10):
                    move_txt = (
                        f"近5日積極買超 +{n5:,} 張 (今日 {n1:+,} 張)"
                        if n5 > 0
                        else f"今日單日反手買超 +{n1:,} 張 (近5日累計 {n5:+,} 張)"
                    )
                    analysis_result["macro_signals"].append(
                        f"🎯 【1年認證抄底高手進場】歷史低檔抄底高手「{sm['broker_name']}」(正價差 +{sm.get('spread_pct', 0):.1f}%)，"
                        f"{move_txt}，留意波段底部支撐！"
                    )
            elif sm["period"] == 1:
                if sm["role"] == "🎯高檔了結":
                    analysis_result["macro_signals"].append(
                        f"🎯 【長線主力高檔了結】長線買方分點「{sm['broker_name']}」今日於相對高檔大賣超佔比達 {sm['ratio']:.1f}%，留意波段獲利調節壓力！"
                    )
                elif sm["role"] == "⚠️破線調節":
                    analysis_result["macro_signals"].append(
                        f"⚠️ 【長線主力破線調節】長線買方分點「{sm['broker_name']}」於月線下方單日大賣超佔比達 {sm['ratio']:.1f}%，留意停損或轉弱賣壓！"
                    )
                elif sm["role"] == "🎯波段賣頂神手":
                    analysis_result["macro_signals"].append(
                        f"🎯 【波段賣頂神手出擊】逢高拋售分點「{sm['broker_name']}」今日單日大賣超佔比達 {sm['ratio']:.1f}%，短線慎防高檔壓回！"
                    )

        # 3. 三大法人與融資券宏觀背離訊號 (依成交量動態調整門檻)
        vol_lots = tech.get("volume", 0) if tech else 0
        inst_limit = max(self.thresholds["inst_min_lots"], vol_lots * self.thresholds["inst_vol_pct"] / 100.0)
        margin_limit = max(self.thresholds["margin_min_lots"], vol_lots * self.thresholds["margin_vol_pct"] / 100.0)

        if institutional_data and margin_data:
            inst_net = institutional_data.get("institutional_total_net", 0)
            margin_diff = margin_data.get("margin_diff", 0)
            trust_net = institutional_data.get("investment_trust_net", 0)

            if inst_net <= -inst_limit and margin_diff >= margin_limit:
                analysis_result["macro_signals"].append("🚨 【法人倒貨散戶接刀】法人單日大幅賣超但融資逆勢增加，短線浮額沉重")
            elif inst_net >= inst_limit and margin_diff <= -margin_limit:
                analysis_result["macro_signals"].append("🔥 【籌碼極度沉澱】法人大舉買超且融資顯著退場，籌碼向大戶高度集中")
            elif inst_net >= inst_limit * 1.5:
                analysis_result["macro_signals"].append(f"🏛️ 【三大法人大舉買超】單日合計淨買超 {inst_net:+,} 張")

            if trust_net >= self.thresholds["trust_buy_lots"]:
                analysis_result["macro_signals"].append(f"🌟 【投信積極認養】投信單日加碼 {trust_net:+,} 張")

        # 4. 集保戶股權分散大戶訊號 (>200張主力大戶 ✕ <10張純散戶 巨細對照)
        if dispersion_data:
            c_diff = dispersion_data.get("concentration_diff", 0.0)
            maj_ratio = dispersion_data.get("major_holder_ratio", 0.0)
            o200_ratio = dispersion_data.get("over_200_ratio", dispersion_data.get("concentration_ratio", 0.0))
            o200_diff = dispersion_data.get("over_200_diff", c_diff)
            u10_ratio = dispersion_data.get("under_10_ratio", 0.0)
            u10_diff = dispersion_data.get("under_10_diff", 0.0)
            o200_cnt = dispersion_data.get("over_200_count", 0)
            u10_cnt = dispersion_data.get("under_10_count", 0)
            o200_cnt_diff = dispersion_data.get("over_200_count_diff", 0)
            u10_cnt_diff = dispersion_data.get("under_10_count_diff", 0)

            if o200_diff >= 0.3 and u10_diff <= -0.2:
                analysis_result["macro_signals"].append(
                    f"🔥 【集保籌碼極度沉澱】最新一週 >200張大戶加碼 +{o200_diff:.2f}% (佔 {o200_ratio:.1f}%) ✕ <10張散戶浮額退場 {u10_diff:+.2f}% (人數 {u10_cnt_diff:+,} 人)，籌碼高度鎖定！"
                )
            elif o200_diff <= -0.3 and u10_diff >= 0.2:
                analysis_result["macro_signals"].append(
                    f"🚨 【高檔籌碼散戶化】最新一週 >200張大戶調節 {o200_diff:.2f}% ✕ <10張散戶追高進場 +{u10_diff:.2f}% (人數 {u10_cnt_diff:+,} 人)，嚴防主力拉高倒貨！"
                )
            elif o200_diff >= 0.3:
                analysis_result["macro_signals"].append(
                    f"📊 【集保大戶增持】最新一週 >200張大戶籌碼集中度提升 +{o200_diff:.2f}% (大戶籌碼佔 {o200_ratio:.1f}%)"
                )
            elif o200_diff <= -0.3:
                analysis_result["macro_signals"].append(
                    f"⚠️ 【集保大戶鬆動】最新一週 >200張大戶籌碼集中度下降 {o200_diff:.2f}%，留意籌碼流向散戶"
                )

        # 5. 籌碼峰 (Volume Profile / POC) 箱型區間位階判定
        if profile_data:
            poc_low = profile_data.get("poc_low", 0.0)
            poc_high = profile_data.get("poc_high", 0.0)
            poc_ratio = profile_data.get("poc_volume_ratio", 0.0)
            if poc_low > 0 and poc_high > 0 and ref_price > 0:
                if ref_price >= poc_high:
                    analysis_result["macro_signals"].append(f"🧱 【突破 60日籌碼密集峰】現價站穩密集換手箱型區 ({poc_low:.1f}~{poc_high:.1f}元，累積量佔 {poc_ratio}%)，轉化為強力防守支撐帶")
                elif ref_price <= poc_low:
                    analysis_result["macro_signals"].append(f"⚠️ 【承壓 60日籌碼密集峰】現價低於密集換手箱型區 ({poc_low:.1f}~{poc_high:.1f}元)，上方存在較大套牢解套賣壓")
                else:
                    analysis_result["macro_signals"].append(f"🔄 【身處 60日籌碼峰內部】股價正於核心換手箱型區間 ({poc_low:.1f}~{poc_high:.1f}元) 激烈震盪整理")

        # 5.5 八大官股與法人/基金委外代操專屬量化動態分析 (含 14~30天 TTL 認證快取與 O(1) 增量追蹤)
        gov_disc_summary = self._evaluate_government_and_discretionary(
            stock_id=stock_id,
            anchor_date=anchor_date,
            tech=tech,
            institutional_data=institutional_data,
            force_rebuild=force_rebuild_backtest,
        )
        analysis_result["gov_discretionary_summary"] = gov_disc_summary
        if gov_disc_summary.get("gov_macro_alert"):
            analysis_result["macro_signals"].append(gov_disc_summary["gov_macro_alert"])
        if gov_disc_summary.get("disc_macro_alert"):
            analysis_result["macro_signals"].append(gov_disc_summary["disc_macro_alert"])

        # 6. 多週期量化指標
        for w in windows:
            summary = self.db.get_period_summary(stock_id, w, anchor_date)
            used_date = anchor_date
            if not summary and anchor_date is not None:
                summary = self.db.get_period_summary(stock_id, w, None)
                if summary:
                    used_date = summary["trade_date"]
                    if w in (1, 5, 20):
                        data_warnings.append(f"{w}日週期使用舊資料 ({used_date})，與基準日 ({anchor_date}) 不同步")
            if not summary:
                continue

            top_buyers = self.db.get_period_ranks(stock_id, w, side="BUY", trade_date=used_date, limit=5)
            top_sellers = self.db.get_period_ranks(stock_id, w, side="SELL", trade_date=used_date, limit=5)

            buy_concentration = sum(b.get("ratio", 0.0) for b in top_buyers)
            sell_concentration = sum(s.get("ratio", 0.0) for s in top_sellers)
            net_concentration = round(buy_concentration - sell_concentration, 2)

            certified_disc_names = {c["broker_name"]: c for c in gov_disc_summary.get("discretionary", {}).get("certified_desks", [])}

            enriched_buyers = []
            for b in top_buyers:
                tags = self._match_tags(b["broker_name"], w, list(long_term_buyers))
                for sb_tag in smart_badges_map.get(b["broker_name"], []):
                    if sb_tag not in tags:
                        tags.append(sb_tag)
                if b["broker_name"] in certified_disc_names and not any("代操" in t for t in tags):
                    tags.append("💼代操鎖碼")
                enriched_buyers.append({
                    "rank": b["rank"],
                    "broker_name": b["broker_name"],
                    "net_shares": b["net_shares"],
                    "ratio": b["ratio"],
                    "tags": tags
                })

            enriched_sellers = []
            for s in top_sellers:
                tags = self._match_tags(s["broker_name"], w, list(long_term_buyers))
                for sb_tag in smart_badges_map.get(s["broker_name"], []):
                    if sb_tag not in tags:
                        tags.append(sb_tag)
                if s["broker_name"] in certified_disc_names and not any("代操" in t for t in tags):
                    tags.append("💼代操調節")
                enriched_sellers.append({
                    "rank": s["rank"],
                    "broker_name": s["broker_name"],
                    "net_shares": s["net_shares"],
                    "ratio": s["ratio"],
                    "tags": tags
                })

            vwap = summary["avg_buy_cost"]
            bias_pct = round(((ref_price - vwap) / vwap) * 100, 2) if vwap > 0 and ref_price > 0 else 0.0

            behavior_flags = []
            if w in [1, 5]:
                day_trader_ratio = sum(b["ratio"] for b in enriched_buyers if "隔日沖" in b["tags"])
                fake_foreign_ratio = sum(b["ratio"] for b in enriched_buyers if "假外資/短線量化" in b["tags"])
                if day_trader_ratio >= self.thresholds["day_trader_ratio"]:
                    behavior_flags.append(f"⚠️ 隔日沖主力重倉 (佔比 {day_trader_ratio:.1f}%)，隔日開高防賣壓")
                if fake_foreign_ratio >= self.thresholds["fake_foreign_ratio"]:
                    behavior_flags.append(f"⚡ 短線量化/假外資席位重倉 (佔比 {fake_foreign_ratio:.1f}%)，注意短線快進快出")

            if w in [20, 60]:
                if net_concentration > self.thresholds["swing_lock_net_conc"]:
                    if -3.0 <= bias_pct <= 5.0:
                        behavior_flags.append(f"🔒 波段主力成本防守區 (成本 {vwap}，乖離 {bias_pct:+.2f}%)")
                    elif bias_pct > 5.0:
                        behavior_flags.append(f"🚀 波段主力已拉開獲利區 (成本 {vwap}，獲利 {bias_pct:+.2f}%)")
                elif net_concentration < -self.thresholds["swing_lock_net_conc"]:
                    behavior_flags.append(f"🚨 主力籌碼發散調節 (賣方佔比高於買方 {abs(net_concentration):.1f}%)")

            if w in [120, 240]:
                top_b_names = [b["broker_name"] for b in enriched_buyers[:3]]
                behavior_flags.append(f"🏦 長期核心大戶均價 {vwap} (核心分點: {', '.join(top_b_names)})")

            analysis_result["windows_metrics"][w] = {
                "window": w,
                "trade_date": summary["trade_date"],
                "vwap_buy_cost": vwap,
                "vwap_sell_cost": summary["avg_sell_cost"],
                "total_buy_shares": summary["total_buy_shares"],
                "total_sell_shares": summary["total_sell_shares"],
                "buy_concentration_top5": round(buy_concentration, 2),
                "sell_concentration_top5": round(sell_concentration, 2),
                "net_concentration": net_concentration,
                "bias_pct": bias_pct,
                "top_buyers": enriched_buyers,
                "top_sellers": enriched_sellers,
                "behavior_flags": behavior_flags
            }

        # 7. 技術面 5MA/20MA/60MA 均線交叉、放量與籌碼共振判定
        analysis_result["technical_summary"] = tech

        if tech:
            c_5_20 = tech.get("cross_5_20", False)
            c_5_60 = tech.get("cross_5_60", False)
            c_20_60 = tech.get("cross_20_60", False)
            is_bull = tech.get("is_bullish_alignment", False)
            vol_surge = tech.get("is_vol_surge", False)
            v_ratio = tech.get("vol_ratio_5", 1.0)

            w1 = analysis_result["windows_metrics"].get(1, {})
            day_trader_ratio = sum(b.get("ratio", 0.0) for b in w1.get("top_buyers", []) if "隔日沖" in b.get("tags", []))
            inst_net = institutional_data.get("institutional_total_net", 0) if institutional_data else 0
            margin_diff = margin_data.get("margin_diff", 0) if margin_data else 0

            # A. 5MA 向上金叉 20MA
            if c_5_20:
                if day_trader_ratio >= self.thresholds["day_trader_ratio"]:
                    analysis_result["macro_signals"].append(
                        f"⚠️ 【技術面金叉防隔日沖】5MA 向上黃金交叉 20MA，但買方隔日沖佔比達 {day_trader_ratio:.1f}%，明日早盤留意開高出貨賣壓！"
                    )
                elif vol_surge:
                    analysis_result["macro_signals"].append(
                        f"🚀 【帶量黃金交叉】5MA 向上穿越月線 (20MA)，成交量放大至 {v_ratio:.2f} 倍攻擊量，短線攻擊訊號確立！"
                    )
                else:
                    analysis_result["macro_signals"].append(
                        "🔍 【量縮黃金交叉】5MA 向上穿越 20MA，惟成交量尚未明顯放大，觀察次日是否補量確認。"
                    )

            # B. 5MA 向上金叉 60MA 季線
            if c_5_60:
                if vol_surge:
                    analysis_result["macro_signals"].append(
                        f"🧱 【帶量突破生命季線】5MA 強勢金叉 60MA 季線且出量 {v_ratio:.2f} 倍，中長線多空分水嶺翻多表態！"
                    )
                else:
                    analysis_result["macro_signals"].append(
                        "📈 【突破季線】5MA 向上穿越 60MA 季線，短線挑戰中長期均線壓力。"
                    )

            # C. 20MA 向上金叉 60MA (中長線大多頭確立)
            if c_20_60:
                analysis_result["macro_signals"].append(
                    "🌌 【波段大黃金交叉】月線 (20MA) 向上穿越季線 (60MA)，中長線波段主升段多頭架構確立！"
                )

            # D. 三線多頭排列 (Close > 5MA > 20MA > 60MA)
            if is_bull:
                if vol_surge and inst_net > 0:
                    analysis_result["macro_signals"].append(
                        f"👑 【技術 ✕ 籌碼共振強攻】三線多頭排列 (現價>5MA>20MA>60MA) 且放量 {v_ratio:.2f} 倍，法人合計買超 {inst_net:+,} 張，主升段動能強勁！"
                    )
                elif vol_surge and margin_diff < 0:
                    analysis_result["macro_signals"].append(
                        f"👑 【技術 ✕ 籌碼沉澱強攻】三線多頭排列 (現價>5MA>20MA>60MA) 且放量 {v_ratio:.2f} 倍，融資退場 {margin_diff:+,} 張，籌碼安定上攻！"
                    )
                elif vol_surge:
                    analysis_result["macro_signals"].append(
                        f"⚡ 【多頭排列放量攻擊】三線多頭排列且出量 {v_ratio:.2f} 倍，多方動能積極。"
                    )
                else:
                    analysis_result["macro_signals"].append(
                        "📊 【三線多頭排列】均線呈多頭排列架構 (現價>5MA>20MA>60MA)，多方控盤整理中。"
                    )

        # 8. 統一計算所有衍生判讀訊號 (供 reporter 各區塊共用，確保文字與看板 100% 一致)
        analysis_result["derived_signals"] = derive_signals(analysis_result, self.thresholds)
        analysis_result["material_signal"] = self._evaluate_material_signal(analysis_result)
        return analysis_result

    def evaluate_chip_news_resonance(
        self, analysis_result: Dict[str, Any], news_list: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        """
        評估「權威財經新聞催化劑 ✕ 主力籌碼實際動向」之共振或背離訊號 (防止散戶被利多新聞誘多接刀)
        """
        stock_news = [n for n in (news_list or []) if "個股" in n.get("scope", "")]
        bull_news_cnt = sum(1 for n in stock_news if n.get("sentiment_tone") == "bull")
        bear_news_cnt = sum(1 for n in stock_news if n.get("sentiment_tone") == "bear")

        swing_masters = analysis_result.get("smart_brokers", {}).get("swing_masters", [])
        cert_selling = [
            s["broker_name"]
            for s in swing_masters
            if s.get("is_1y_certified") and s.get("action") == "SELL" and (s.get("net_5d", 0) <= -15 or s.get("net_1d", 0) <= -10)
        ]
        cert_buying = [
            s["broker_name"]
            for s in swing_masters
            if s.get("is_1y_certified") and s.get("action") == "BUY" and (s.get("net_5d", 0) >= 15 or s.get("net_1d", 0) >= 10)
        ]
        inst_net = (analysis_result.get("institutional_summary") or {}).get("institutional_total_net", 0)

        if bull_news_cnt > 0 and (cert_selling or inst_net <= -500):
            sellers_txt = f"1年認證賣頂高手（{'、'.join(cert_selling[:2])}）" if cert_selling else f"三大法人（淨賣超 {inst_net:+,} 張）"
            return {
                "tone": "warn",
                "label": "⚠️ 利多出盡／主力逢高調節警訊",
                "desc": f"媒體釋出個股利多催化消息，惟 {sellers_txt} 正趁勢逢高調節出貨，切勿盲目聽信新聞追高，嚴防主力藉利多倒貨！",
            }
        if bull_news_cnt > 0 and (cert_buying or inst_net >= 200):
            return {
                "tone": "bull",
                "label": "🔥 消息利多 ✕ 籌碼買盤共振",
                "desc": "基本面利多消息獲法人或關鍵波段主力實質買盤背書，消息面與籌碼面同向偏多共振。",
            }
        if (bear_news_cnt > 0 or bull_news_cnt == 0) and (cert_buying or inst_net >= 500):
            return {
                "tone": "bull",
                "label": "🛡️ 消息平淡／偏空下主力逆勢吸籌",
                "desc": "儘管盤勢或消息面偏保守，核心法人與波段抄底高手仍於低檔逆勢承接鎖碼，籌碼面領先基本面表態。",
            }
        if bear_news_cnt > 0 and (cert_selling or inst_net < 0):
            return {
                "tone": "bear",
                "label": "🚨 消息偏空 ✕ 籌碼調節雙重承壓",
                "desc": "消息面與外圍環境偏空，且主力分點同步調節，短線宜保守看待並嚴守關鍵成本支撐。",
            }
        return {
            "tone": "neutral",
            "label": "📊 消息與籌碼中性驗證",
            "desc": "近期權威財經消息與籌碼進出維持常態換手，建議以 5D/20D 主力成本 (VWAP) 與 1 年認證高手動向為核心操作依據。",
        }

