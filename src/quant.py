import json
import os
import math
import logging
from typing import Dict, Any, List, Optional
from src.db import ChipDatabase
from src.signals import merge_thresholds, price_zone, derive_signals, fmt_price

logger = logging.getLogger(__name__)

class QuantEngine:
    def __init__(self, db: ChipDatabase, tags_path: str = "data/broker_tags.json", thresholds: Optional[Dict[str, Any]] = None):
        self.db = db
        self.broker_tags = self._load_tags(tags_path)
        self.thresholds = merge_thresholds(thresholds)

    def _load_tags(self, tags_path: str) -> Dict[str, List[str]]:
        if os.path.exists(tags_path):
            try:
                with open(tags_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"讀取券商標籤檔失敗: {e}")
        return {"day_traders": [], "foreign_institutions": [], "government_banks": []}

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

        if is_foreign:
            if window in [1, 5]:
                if broker_name not in long_term_buyers:
                    tags.append("假外資/短線量化")
                else:
                    tags.append("波段外資")
            else:
                tags.append("外資")

        return tags

    def _detect_smart_brokers(
        self,
        stock_id: str,
        trade_date: Optional[str],
        ref_price: float,
        profile_data: Optional[Dict[str, Any]],
        p_zone: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        偵測兩類關鍵神準主力分點：
        1. 【🔒 長線囤貨大戶】(120/240日大舉買超、純度高且跨週期持續鎖碼)
        2. 【🎯 波段神算高手】(20日領先買底拉開獲利、或1/5日於高檔大量了結/破線調節的主力)
        """
        w_ranks = {}
        for w in [1, 5, 20, 60, 120, 240]:
            w_ranks[w] = {
                "BUY": self.db.get_period_ranks(stock_id, w, side="BUY", trade_date=trade_date, limit=5),
                "SELL": self.db.get_period_ranks(stock_id, w, side="SELL", trade_date=trade_date, limit=5),
                "summary": self.db.get_period_summary(stock_id, w, trade_date=trade_date)
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

        # 1. 偵測長線囤貨大戶 (優先 240 日，其次 120 日)
        for name, b240 in b240_names.items():
            net = b240.get("net_shares", 0)
            tot = b240.get("buy_shares", 0) + b240.get("sell_shares", 0)
            purity = (b240.get("buy_shares", 0) / tot) if tot > 0 else 0
            is_in_120 = name in b120_names
            is_in_60 = name in b60_names

            if (purity >= 0.55 or is_in_120) and b240.get("ratio", 0) >= 0.3:
                hoarders.append({
                    "broker_name": name,
                    "period": 240,
                    "net_shares": net,
                    "ratio": b240.get("ratio", 0.0),
                    "purity": round(purity * 100, 1),
                    "continuous": is_in_120 or is_in_60,
                    "badge": "🔒長線囤貨"
                })
                seen_hoarders.add(name)
                broker_badges.setdefault(name, []).append("🔒長線囤貨")

        for name, b120 in b120_names.items():
            if name in seen_hoarders:
                continue
            net = b120.get("net_shares", 0)
            tot = b120.get("buy_shares", 0) + b120.get("sell_shares", 0)
            purity = (b120.get("buy_shares", 0) / tot) if tot > 0 else 0
            is_in_60 = name in b60_names

            if (purity >= 0.58 or is_in_60) and b120.get("ratio", 0) >= 0.5:
                hoarders.append({
                    "broker_name": name,
                    "period": 120,
                    "net_shares": net,
                    "ratio": b120.get("ratio", 0.0),
                    "purity": round(purity * 100, 1),
                    "continuous": is_in_60,
                    "badge": "🔒長線囤貨"
                })
                seen_hoarders.add(name)
                broker_badges.setdefault(name, []).append("🔒長線囤貨")

        # 2. 偵測波段神算高手 (結合位階判定，避免跌破月線時誤標為「高檔」)
        swing_masters = []
        seen_swing = set()
        s20_summary = w_ranks[20]["summary"]
        vwap_20 = s20_summary.get("avg_buy_cost", 0.0) if s20_summary else 0.0
        is_high_zone = bool(p_zone and p_zone.get("is_high_zone"))

        # A. 買底神手：20 日買超前列，現價較 20 日主力均價拉開獲利 >= 2.0%
        for b in w_ranks[20]["BUY"]:
            name = b["broker_name"]
            bias = ((ref_price - vwap_20) / vwap_20 * 100) if vwap_20 > 0 and ref_price > 0 else 0.0
            if b.get("ratio", 0) >= 2.5 and bias >= 2.0:
                swing_masters.append({
                    "broker_name": name,
                    "role": "🎯買底神手",
                    "action": "BUY",
                    "period": 20,
                    "ratio": b.get("ratio", 0.0),
                    "bias_pct": round(bias, 2),
                    "desc": f"20日買超佔比 {b.get('ratio', 0.0):.1f}%，現價較20日主力均價 ({vwap_20:.2f}元) 拉開 +{bias:.1f}% 獲利空間"
                })
                seen_swing.add(name)
                broker_badges.setdefault(name, []).append("🎯買底神手")

        # B. 長線大買方於 1日/5日大賣 (佔比 >= 5.0%)：依位階區分「🎯高檔了結」與「⚠️破線調節」
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
                        "role": role_tag,
                        "action": "SELL",
                        "period": w,
                        "ratio": s.get("ratio", 0.0),
                        "bias_pct": 0.0,
                        "desc": desc_str
                    })
                    seen_swing.add(name)
                    broker_badges.setdefault(name, []).append(role_tag)

        # C. 波段賣頂神手 / 逢高大拋主力 (如 6782 統一-桃園)：
        # 1日或5日賣超名列前 3 (佔比 >= 4.0%)，賣出純度 >= 85%，且在 60/120/240 日亦為主要波段賣方
        for w in [1, 5]:
            for s in w_ranks[w]["SELL"][:3]:
                name = s["broker_name"]
                if name in seen_swing:
                    continue
                tot = s.get("buy_shares", 0) + s.get("sell_shares", 0)
                sell_purity = (s.get("sell_shares", 0) / tot) if tot > 0 else 0
                is_major_swing_seller = (name in s60_names or name in s120_names or name in s240_names)

                if s.get("ratio", 0) >= 4.0 and sell_purity >= 0.85 and is_major_swing_seller:
                    if is_high_zone:
                        role_tag = "🎯波段賣頂神手"
                        badge_tag = "🎯波段賣頂"
                        desc_str = f"逢高精準拋售({w}日賣超#{s.get('rank', '-')}佔比{s.get('ratio', 0.0):.1f}%, 純度{sell_purity:.1%})，且為60/120日波段核心賣方"
                    else:
                        role_tag = "⚠️波段倒貨"
                        badge_tag = "⚠️波段倒貨"
                        desc_str = f"波段持續高純度拋售({w}日賣超#{s.get('rank', '-')}佔比{s.get('ratio', 0.0):.1f}%, 純度{sell_purity:.1%})，且為60/120日核心賣方"
                    swing_masters.append({
                        "broker_name": name,
                        "role": role_tag,
                        "action": "SELL",
                        "period": w,
                        "ratio": s.get("ratio", 0.0),
                        "bias_pct": 0.0,
                        "desc": desc_str
                    })
                    seen_swing.add(name)
                    broker_badges.setdefault(name, []).append(badge_tag)

        return {
            "hoarders": hoarders,
            "swing_masters": swing_masters,
            "broker_badges": broker_badges
        }

    def analyze_stock(
        self,
        stock_id: str,
        windows: List[int],
        trade_date: Optional[str] = None,
        institutional_data: Optional[Dict[str, Any]] = None,
        margin_data: Optional[Dict[str, Any]] = None,
        dispersion_data: Optional[Dict[str, Any]] = None,
        profile_data: Optional[Dict[str, Any]] = None
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
        smart_brokers = self._detect_smart_brokers(stock_id, anchor_date, ref_price, profile_data, p_zone=p_zone)
        analysis_result["smart_brokers"] = smart_brokers
        smart_badges_map = smart_brokers.get("broker_badges", {})

        # 若有極為顯著的神準主力動態，加入宏觀訊號提示
        for sm in smart_brokers.get("swing_masters", []):
            if sm["period"] == 1:
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

        # 4. 集保戶股權分散大戶訊號
        if dispersion_data:
            c_diff = dispersion_data.get("concentration_diff", 0.0)
            maj_ratio = dispersion_data.get("major_holder_ratio", 0.0)
            if c_diff >= 0.3:
                analysis_result["macro_signals"].append(f"📊 【集保大戶增持】最新一週籌碼集中度提升 +{c_diff:.2f}% (大戶籌碼佔 {maj_ratio}%)")
            elif c_diff <= -0.3:
                analysis_result["macro_signals"].append(f"⚠️ 【集保大戶鬆動】最新一週籌碼集中度下降 {c_diff:.2f}%，留意籌碼流向散戶")

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

        # 6. 多週期量化指標
        for w in windows:
            summary = self.db.get_period_summary(stock_id, w, anchor_date)
            used_date = anchor_date
            if not summary and anchor_date is not None:
                summary = self.db.get_period_summary(stock_id, w, None)
                if summary:
                    used_date = summary["trade_date"]
                    data_warnings.append(f"{w}日週期使用舊資料 ({used_date})，與基準日 ({anchor_date}) 不同步")
            if not summary:
                continue

            top_buyers = self.db.get_period_ranks(stock_id, w, side="BUY", trade_date=used_date, limit=5)
            top_sellers = self.db.get_period_ranks(stock_id, w, side="SELL", trade_date=used_date, limit=5)

            buy_concentration = sum(b.get("ratio", 0.0) for b in top_buyers)
            sell_concentration = sum(s.get("ratio", 0.0) for s in top_sellers)
            net_concentration = round(buy_concentration - sell_concentration, 2)

            enriched_buyers = []
            for b in top_buyers:
                tags = self._match_tags(b["broker_name"], w, list(long_term_buyers))
                for sb_tag in smart_badges_map.get(b["broker_name"], []):
                    if sb_tag not in tags:
                        tags.append(sb_tag)
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
        return analysis_result
