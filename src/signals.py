"""
統一判讀模組 (Single Source of Truth)

所有「法人態度 / 籌碼沉澱 / 量能 / 均線型態 / 股價位階 / 價格格式」的判讀邏輯集中於此，
quant (宏觀訊號)、reporter (本地備援文字、HTML 看板、Gemini prompt) 一律讀取 derive_signals() 的結果，
避免同一份報告中不同區塊互相矛盾。
"""
import math
from typing import Any, Dict, Optional

# 預設門檻 (可由 config.json 的 "thresholds" 覆寫)
DEFAULT_THRESHOLDS: Dict[str, float] = {
    # 法人/融資「顯著」門檻：取 max(絕對張數, 佔當日成交量 %)，避免小型股與權值股套用同一套絕對值
    "inst_min_lots": 100,
    "inst_vol_pct": 5.0,
    "margin_min_lots": 50,
    "margin_vol_pct": 2.0,
    # 量能
    "vol_surge_ratio": 1.3,
    # 高檔區判定：現價 >= 20MA 且 (距 60 日高點 <= X% 或 60 日主力成本乖離 >= Y%)
    "high_zone_from_high_pct": 10.0,
    "high_zone_bias60_pct": 8.0,
    # 分點行為
    "day_trader_ratio": 5.0,
    "fake_foreign_ratio": 4.0,
    "swing_lock_net_conc": 3.0,
    "trust_buy_lots": 200,
}


def merge_thresholds(custom: Optional[Dict[str, Any]]) -> Dict[str, float]:
    th = dict(DEFAULT_THRESHOLDS)
    if custom:
        th.update({k: v for k, v in custom.items() if k in th})
    return th


def _valid(x: Any) -> bool:
    return isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))


def fmt_price(x: Any) -> str:
    """交易所價格格式：251 / 314.5 / 121.25；無效值顯示「—」"""
    if not _valid(x) or x <= 0:
        return "—"
    if float(x) == int(x):
        return f"{int(x)}"
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _big(value: float, vol_lots: float, min_lots: float, pct: float) -> bool:
    limit = max(min_lots, (vol_lots or 0) * pct / 100.0)
    return abs(value or 0) >= limit


def classify_institutional(inst: Dict[str, Any], vol_lots: float, th: Dict[str, float]) -> Dict[str, str]:
    if not inst:
        return {"code": "na", "tier": "neutral", "label": "法人資料缺漏", "desc": "法人資料缺漏", "tone": "neutral"}
    f, t, d = inst.get("foreign_buy_net", 0), inst.get("investment_trust_net", 0), inst.get("dealer_net", 0)
    total = inst.get("institutional_total_net", 0)
    if not _big(total, vol_lots, th["inst_min_lots"], th["inst_vol_pct"]):
        lbl = "三大法人買賣互見，態度偏向中性觀望"
        return {"code": "neutral", "tier": "neutral", "label": "中性觀望", "desc": lbl, "tone": "neutral"}
    side = "買超" if total > 0 else "賣超"
    tier = "strong_buy" if total > 0 else "strong_sell"
    signs = {(1 if v > 0 else -1) for v in (f, t, d) if v != 0}
    if len(signs) == 1:
        desc = f"三大法人同步{side}，{'多方推升力道強勁' if total > 0 else '短線賣壓沉重'}"
        short_lbl = f"🏛️三大法人同步{side}"
    else:
        dom = max((("外資", f), ("投信", t), ("自營商", d)), key=lambda kv: abs(kv[1]))[0]
        desc = f"法人合計{side}（由{dom}主導，三大法人方向分歧）"
        short_lbl = f"🏛️{dom}主導{side}"
    return {
        "code": "buy" if total > 0 else "sell",
        "tier": tier,
        "label": short_lbl,
        "desc": desc,
        "tone": "bull" if total > 0 else "bear",
    }


def classify_sediment(inst_net: float, margin_diff: float, vol_lots: float, th: Dict[str, float],
                      has_inst: bool = True, has_margin: bool = True) -> Dict[str, str]:
    """法人 × 融資 四象限判讀"""
    if not has_margin:
        return {"code": "na", "label": "資料缺漏", "desc": "融資資料缺漏", "short": "資料缺漏", "tone": "neutral"}
    inst_big = has_inst and _big(inst_net, vol_lots, th["inst_min_lots"], th["inst_vol_pct"])
    m_big = _big(margin_diff, vol_lots, th["margin_min_lots"], th["margin_vol_pct"])
    if inst_big and inst_net > 0:
        if margin_diff < 0:
            desc = "散戶融資退場、法人買超承接（籌碼趨於沉澱）"
            return {"code": "sediment", "label": "🔥籌碼沉澱", "desc": desc, "short": "🔥籌碼沉澱", "tone": "bull"}
        if m_big:
            desc = "法人與融資同步加碼（多方追價積極，惟須留意短線浮額增加）"
            return {"code": "chase", "label": "⚡多方追價", "desc": desc, "short": "⚡多方追價", "tone": "neutral"}
        desc = "法人買超主導、融資水位持平（籌碼結構偏多安定）"
        return {"code": "inst_lead", "label": "🏛️法人主導", "desc": desc, "short": "🏛️法人主導", "tone": "bull"}
    if inst_big and inst_net < 0:
        if margin_diff > 0 and m_big:
            desc = "法人賣超調節、散戶融資逆勢承接（浮額沉重，短線需高度警戒）"
            return {"code": "dump", "label": "🚨浮額沉重", "desc": desc, "short": "🚨浮額沉重", "tone": "bear"}
        if margin_diff < 0:
            desc = "法人與融資同步撤出（多殺多或停損賣壓宣洩，非散戶接刀）"
            return {"code": "exit", "label": "📉同步撤出", "desc": desc, "short": "📉同步撤出", "tone": "bear"}
        desc = "法人賣超調節、融資變化不大（短線受法人調節賣壓影響）"
        return {"code": "inst_sell", "label": "⚠️法人調節", "desc": desc, "short": "⚠️法人調節", "tone": "bear"}
    if m_big and margin_diff > 0:
        desc = "法人態度中性、散戶融資主動加碼（短線散戶追價意願較高）"
        return {"code": "retail_in", "label": "⚠️散戶追價", "desc": desc, "short": "⚠️散戶追價", "tone": "neutral"}
    if m_big and margin_diff < 0:
        desc = "法人態度中性、散戶融資主動退場（浮額清洗，籌碼稍趨乾淨）"
        return {"code": "retail_out", "label": "🧹融資退場", "desc": desc, "short": "🧹融資退場", "tone": "neutral"}
    desc = "法人與散戶融資變動皆不大，籌碼維持中性觀望"
    return {"code": "neutral", "label": "中性觀望", "desc": desc, "short": "中性觀望", "tone": "neutral"}


def classify_volume(tech: Dict[str, Any], th: Dict[str, float]) -> Dict[str, Any]:
    ratio = tech.get("vol_ratio_5") if tech else None
    if not _valid(ratio) or ratio <= 0:
        return {"label": "—", "ratio_str": "—", "text": "—", "css_class": "", "tone": "neutral"}
    if tech.get("is_vol_surge"):
        label = "🔥出量攻擊"
        tone = "bull"
        css_class = "val-red"
    elif ratio >= 1.0:
        label = "溫和量增"
        tone = "neutral"
        css_class = ""
    else:
        label = "量縮整理"
        tone = "neutral"
        css_class = ""
    return {
        "label": label,
        "ratio_str": f"{ratio:.2f} 倍",
        "text": f"{ratio:.2f} 倍 ({label})",
        "css_class": css_class,
        "tone": tone,
    }


def classify_ma(tech: Dict[str, Any], ref_price: float) -> Dict[str, str]:
    if not tech or not _valid(tech.get("ma20")) or not tech.get("ma20"):
        return {"label": "技術面資料不足", "desc": "技術面均線資料不足", "css_class": "", "tone": "neutral"}
    ma5, ma20, ma60 = tech.get("ma5", 0), tech.get("ma20", 0), tech.get("ma60", 0)
    if tech.get("is_bullish_alignment"):
        return {"label": "👑 三線多頭排列", "desc": "👑 現價 > 5MA > 20MA > 60MA 呈三線多頭排列", "css_class": "val-red", "tone": "bull"}
    if tech.get("cross_20_60"):
        return {"label": "🌌 20MA 金叉 60MA", "desc": "🌌 20MA 向上黃金交叉 60MA，中長線趨勢翻多", "css_class": "val-red", "tone": "bull"}
    if tech.get("cross_5_60"):
        return {"label": "🧱 5MA 金叉 60MA", "desc": "🧱 5MA 向上黃金交叉 60MA 季線", "css_class": "val-red", "tone": "bull"}
    if tech.get("cross_5_20"):
        return {"label": "🚀 5MA 金叉 20MA", "desc": "🚀 5MA 帶量黃金交叉 20MA 月線", "css_class": "val-red", "tone": "bull"}
    if ma60 and ref_price < ma60:
        return {"label": "⚠️ 跌破季線偏弱", "desc": "⚠️ 現價位於 60MA 季線下方，中線架構偏弱整理", "css_class": "val-green", "tone": "bear"}
    if ma20 and ref_price < ma20:
        if ma5 and ma5 < ma20:
            return {"label": "📉 跌破月線整理", "desc": "📉 現價與 5MA 皆位於 20MA 月線下方，短線偏弱整理", "css_class": "val-green", "tone": "bear"}
        return {"label": "⚠️ 月線下方整理", "desc": "⚠️ 現價位於 20MA 月線下方進行修正整理", "css_class": "val-green", "tone": "bear"}
    return {"label": "均線區間整理", "desc": "現價守於月線與季線之上，均線於區間收斂整理", "css_class": "", "tone": "neutral"}


def price_zone(ref_price: float, tech: Dict[str, Any], profile: Dict[str, Any],
               vwap60: Optional[float], th: Dict[str, float]) -> Dict[str, Any]:
    """判定股價是否位於「高檔區」(供 🎯高檔了結 / 🎯波段賣頂 的位階驗證)"""
    out = {"is_high_zone": False, "high60": None, "pct_from_high": None, "bias60": None, "desc": "位階資料不足"}
    if not _valid(ref_price) or ref_price <= 0:
        return out
    kline = (profile or {}).get("kline_data") or {}
    candles = kline.get("candles") or []
    highs = [c[3] for c in candles[-60:] if len(c) >= 4 and _valid(c[3])]
    if highs:
        h60 = max(highs)
        out["high60"] = h60
        out["pct_from_high"] = round((h60 - ref_price) / h60 * 100, 2) if h60 > 0 else None
    if vwap60 and vwap60 > 0:
        out["bias60"] = round((ref_price - vwap60) / vwap60 * 100, 2)
    ma20 = (tech or {}).get("ma20")
    above_ma20 = _valid(ma20) and ma20 > 0 and ref_price >= ma20
    near_high = out["pct_from_high"] is not None and out["pct_from_high"] <= th["high_zone_from_high_pct"]
    rich_bias = out["bias60"] is not None and out["bias60"] >= th["high_zone_bias60_pct"]
    out["is_high_zone"] = bool(above_ma20 and (near_high or rich_bias))
    parts = []
    if out["pct_from_high"] is not None:
        parts.append(f"距60日高點 {out['pct_from_high']:.1f}%")
    if out["bias60"] is not None:
        parts.append(f"60日主力成本乖離 {out['bias60']:+.1f}%")
    parts.append("站上月線" if above_ma20 else "位於月線下")
    out["desc"] = ("高檔區" if out["is_high_zone"] else "非高檔區") + "（" + "、".join(parts) + "）"
    return out


def derive_signals(metrics: Dict[str, Any], th: Dict[str, float]) -> Dict[str, Any]:
    tech = metrics.get("technical_summary") or {}
    inst = metrics.get("institutional_summary") or {}
    margin = metrics.get("margin_summary") or {}
    ref_price = metrics.get("reference_price") or 0.0
    vol_lots = tech.get("volume") or 0
    has_close = bool(metrics.get("close_price"))
    return {
        "price_str": fmt_price(ref_price),
        "price_label": "當日收盤價" if has_close else "參考價（分點均價，非收盤價）",
        "inst": classify_institutional(inst, vol_lots, th),
        "sediment": classify_sediment(inst.get("institutional_total_net", 0), margin.get("margin_diff", 0),
                                      vol_lots, th, has_inst=bool(inst), has_margin=bool(margin)),
        "volume": classify_volume(tech, th),
        "ma": classify_ma(tech, ref_price),
        "zone": metrics.get("price_zone") or {},
    }
