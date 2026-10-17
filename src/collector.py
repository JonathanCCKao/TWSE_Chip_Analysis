import re
import time
import logging
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup
import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)

# 週期天數對應 Moneydj URL 參數碼
PERIOD_CODE_MAP = {
    1: "1",
    5: "2",
    10: "3",
    20: "4",
    40: "5",
    60: "6",
    120: "7",
    240: "8",
}

def _clean_number(val_str: str) -> float:
    """清理包含逗號或百分比的字串並轉為浮點數"""
    if not val_str:
        return 0.0
    cleaned = str(val_str).replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def _safe_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15, retries: int = 2) -> Optional[requests.Response]:
    """具備自動重試機制的 HTTP GET"""
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            logger.warning(f"請求 {url} 回傳狀態碼: {resp.status_code} (嘗試 {attempt + 1}/{retries + 1})")
        except Exception as e:
            if attempt == retries:
                logger.error(f"請求 {url} 失敗: {e}")
                return None
            time.sleep(1)
    return None


def roc_to_iso(roc_date: str) -> str:
    """民國日期 (115/10/15) 或西元斜線日期 (2026/10/15) 轉西元 ISO 日期 (2026-10-15)；格式不符時回傳空字串"""
    m = re.match(r"^\s*(\d{2,4})[/\-](\d{1,2})[/\-](\d{1,2})\s*$", str(roc_date or ""))
    if not m:
        return ""
    y = int(m.group(1))
    if y < 1911:
        y += 1911
    return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def fetch_official_daily_quote(stock_id: str, target_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    抓取交易所官方日成交資料 (開高低收、成交股數)。
    若指定 target_date (YYYY-MM-DD)，優先匹配該日期，否則回傳最新一日。
    先查證交所 (上市)，查無資料再查櫃買中心 (上櫃)。
    """
    import datetime as _dt
    today = _dt.date.today()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    def _num(s):
        v = _clean_number(str(s).replace("+", ""))
        return v

    def _pick_row(rows):
        if target_date:
            matched = [r for r in rows if roc_to_iso(r[0]) == target_date]
            if matched:
                return matched[-1]
            le_rows = [r for r in rows if roc_to_iso(r[0]) and roc_to_iso(r[0]) <= target_date]
            if le_rows:
                return le_rows[-1]
        return rows[-1]

    # 1. TWSE 上市
    try:
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={today:%Y%m%d}&stockNo={stock_id}&response=json"
        r = _safe_get(url, headers=headers, timeout=15, retries=1)
        if r is not None:
            j = r.json()
            rows = [row for row in (j.get("data") or []) if _num(row[6]) > 0]
            if j.get("stat") == "OK" and rows:
                row = _pick_row(rows)
                return {
                    "date": roc_to_iso(row[0]),
                    "open": _num(row[3]), "high": _num(row[4]), "low": _num(row[5]), "close": _num(row[6]),
                    "volume_shares": _num(row[1]),
                    "source": "TWSE"
                }
    except Exception as e:
        logger.debug(f"TWSE 官方行情抓取失敗 {stock_id}: {e}")

    # 2. TPEx 上櫃
    try:
        url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={stock_id}&date={today:%Y/%m/%d}&response=json"
        r = _safe_get(url, headers=headers, timeout=15, retries=1)
        if r is not None:
            tables = r.json().get("tables") or []
            data = tables[0].get("data") if tables else []
            rows = [row for row in (data or []) if _num(row[6]) > 0]
            if rows:
                row = _pick_row(rows)
                return {
                    "date": roc_to_iso(row[0]),
                    "open": _num(row[3]), "high": _num(row[4]), "low": _num(row[5]), "close": _num(row[6]),
                    "volume_shares": _num(row[1]) * 1000,  # 櫃買單位為「張」
                    "source": "TPEx"
                }
    except Exception as e:
        logger.debug(f"TPEx 官方行情抓取失敗 {stock_id}: {e}")

    logger.warning(f"無法取得 {stock_id} 交易所官方日成交資料，改用 yfinance")
    return None


def fetch_latest_trade_date(stock_id: str = "2301") -> Optional[str]:
    """以 MoneyDJ 1 日分點頁面的資料日期作為最新交易日 (YYYY-MM-DD)"""
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{stock_id}_1.djhtm"
    r = _safe_get(url, timeout=15, retries=2)
    if r is None:
        return None
    html = r.content.decode("big5", errors="ignore")
    m = re.search(r"(?:最後更新日?：|toDay\s*=\s*')(\d{4})/(\d{1,2})/(\d{1,2})", html)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

def fetch_period_chip_data(stock_id: str, window: int) -> Optional[Dict[str, Any]]:
    """
    從公開端點採集指定股票與週期之券商分點買賣統計與加權成本 (VWAP)
    """
    code = PERIOD_CODE_MAP.get(window)
    if not code:
        logger.error(f"不支援的週期天數: {window} 日")
        return None

    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{stock_id}_{code}.djhtm"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        resp = _safe_get(url, headers=headers, timeout=15, retries=2)
        if not resp or resp.status_code != 200:
            logger.error(f"抓取 {stock_id} 【{window}日】 失敗")
            return None

        html = resp.content.decode("big5", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")

        # 1. 萃取資料基準日期
        date_match = re.search(r"(?:最後更新日?：|toDay\s*=\s*')(\d{4})/(\d{1,2})/(\d{1,2})", html)
        if not date_match:
            logger.error(f"抓取 {stock_id} 【{window}日】 找不到資料日期，視為失敗 (避免寫入無日期資料)")
            return None
        trade_date = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"

        # 2. 解析統計數據與分點名單
        rows = soup.find_all("tr")
        top_buyers: List[Dict[str, Any]] = []
        top_sellers: List[Dict[str, Any]] = []
        total_buy_shares = 0
        total_sell_shares = 0
        avg_buy_cost = 0.0
        avg_sell_cost = 0.0

        for row in rows:
            raw_cells = row.find_all(["td", "th"])
            tds = [td.get_text(strip=True) for td in raw_cells]
            if not tds:
                continue

            if "合計買超張數" in tds and len(tds) >= 4:
                total_buy_shares = int(_clean_number(tds[1]))
                total_sell_shares = int(_clean_number(tds[3]))
            elif "平均買超成本" in tds and len(tds) >= 4:
                avg_buy_cost = _clean_number(tds[1])
                avg_sell_cost = _clean_number(tds[3])

            if len(tds) == 10 and tds[0] not in ["買超券商", "合計買超張數", "平均買超成本"]:
                b_name = tds[0]
                if b_name:
                    bhid_b, bcode_b = _extract_broker_link_codes(raw_cells[0])
                    top_buyers.append({
                        "broker_name": b_name,
                        "bhid": bhid_b,
                        "b_code": bcode_b,
                        "buy_shares": int(_clean_number(tds[1])),
                        "sell_shares": int(_clean_number(tds[2])),
                        "net_shares": int(_clean_number(tds[3])),
                        "ratio": _clean_number(tds[4])
                    })
                s_name = tds[5]
                if s_name:
                    bhid_s, bcode_s = _extract_broker_link_codes(raw_cells[5])
                    top_sellers.append({
                        "broker_name": s_name,
                        "bhid": bhid_s,
                        "b_code": bcode_s,
                        "buy_shares": int(_clean_number(tds[6])),
                        "sell_shares": int(_clean_number(tds[7])),
                        "net_shares": int(_clean_number(tds[8])),
                        "ratio": _clean_number(tds[9])
                    })

        return {
            "stock_id": stock_id,
            "window": window,
            "trade_date": trade_date,
            "avg_buy_cost": avg_buy_cost,
            "avg_sell_cost": avg_sell_cost,
            "total_buy_shares": total_buy_shares,
            "total_sell_shares": total_sell_shares,
            "top_buyers": top_buyers,
            "top_sellers": top_sellers
        }

    except Exception as e:
        logger.exception(f"解析 {stock_id} 【{window}日】 時發生異常: {e}")
        return None


def _extract_broker_link_codes(cell) -> tuple:
    """從券商分點 <td> 內的 <a href='...'> 擷取 (bhid, b_code)"""
    if not cell:
        return "", ""
    a_tag = cell.find("a")
    if not a_tag or not a_tag.get("href"):
        return "", ""
    href = a_tag["href"]
    m_bhid = re.search(r"BHID=([^&]+)", href, re.I)
    m_b = re.search(r"[?&]b=([^&]+)", href, re.I)
    return (m_bhid.group(1) if m_bhid else ""), (m_b.group(1) if m_b else "")


def fetch_historical_range_brokers(stock_id: str, start_date: str, end_date: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    查詢過去任意歷史日期區間 (start_date ~ end_date) 的買賣超前 15 名分點與代碼
    用於反查歷史每次波段高點 (Swing High) 是誰在賣頂、波段低點 (Swing Low) 是誰在抄底
    """
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco.djhtm?a={stock_id}&e={start_date}&f={end_date}"
    headers = {"User-Agent": "Mozilla/5.0"}
    buyers: List[Dict[str, Any]] = []
    sellers: List[Dict[str, Any]] = []
    try:
        resp = _safe_get(url, headers=headers, timeout=12, retries=2)
        if not resp or resp.status_code != 200:
            return {"top_buyers": buyers, "top_sellers": sellers}
        html = resp.content.decode("big5", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.find_all("tr"):
            raw_cells = row.find_all(["td", "th"])
            tds = [td.get_text(strip=True) for td in raw_cells]
            if len(tds) == 10 and tds[0] not in ["買超券商", "合計買超張數", "平均買超成本", ""]:
                b_name = tds[0]
                if b_name:
                    bhid_b, bcode_b = _extract_broker_link_codes(raw_cells[0])
                    buyers.append({
                        "broker_name": b_name,
                        "bhid": bhid_b,
                        "b_code": bcode_b,
                        "buy_shares": int(_clean_number(tds[1])),
                        "sell_shares": int(_clean_number(tds[2])),
                        "net_shares": int(_clean_number(tds[3])),
                        "ratio": _clean_number(tds[4]),
                    })
                s_name = tds[5]
                if s_name:
                    bhid_s, bcode_s = _extract_broker_link_codes(raw_cells[5])
                    sellers.append({
                        "broker_name": s_name,
                        "bhid": bhid_s,
                        "b_code": bcode_s,
                        "buy_shares": int(_clean_number(tds[6])),
                        "sell_shares": int(_clean_number(tds[7])),
                        "net_shares": int(_clean_number(tds[8])),
                        "ratio": _clean_number(tds[9]),
                    })
    except Exception as e:
        logger.warning(f"查詢 {stock_id} 歷史區間 ({start_date}~{end_date}) 分點異常: {e}")
    return {"top_buyers": buyers, "top_sellers": sellers}


def fetch_broker_daily_ledger(
    stock_id: str, bhid: str, b_code: str, start_date: str, end_date: str
) -> List[Dict[str, Any]]:
    """
    調閱單一分點於指定期間 (例如過去 1 年) 的逐日買賣超明細帳本 (zco0.djhtm)
    """
    if not bhid or not b_code:
        return []
    url = (
        f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco0/zco0.djhtm"
        f"?A={stock_id}&BHID={bhid}&b={b_code}&C=1&D={start_date}&E={end_date}&ver=V3"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    trades: List[Dict[str, Any]] = []
    try:
        resp = _safe_get(url, headers=headers, timeout=12, retries=2)
        if not resp or resp.status_code != 200:
            return trades
        html = resp.content.decode("big5", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) == 5 and "/" in tds[0] and tds[0][0].isdigit():
                try:
                    dt_iso = roc_to_iso(tds[0])
                    b = int(_clean_number(tds[1]))
                    s = int(_clean_number(tds[2]))
                    net = int(_clean_number(tds[4]))
                    trades.append({"date": dt_iso, "buy": b, "sell": s, "net": net})
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"調閱 {stock_id} 分點 ({bhid}/{b_code}) 逐日帳本異常: {e}")
    return trades


def fetch_institutional_investors(stock_id: str) -> Optional[Dict[str, Any]]:
    """抓取最新三大法人買賣超動態"""
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcl/zcl.djhtm?a={stock_id}"
    try:
        r = _safe_get(url, timeout=15, retries=2)
        if not r or r.status_code != 200:
            return None
        r.encoding = "big5"
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.find_all("tr")
        found_header = False
        for row in rows:
            tds = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(tds) >= 11 and tds[0] == "日期" and "外資" in tds[1]:
                found_header = True
                continue
            if found_header and len(tds) >= 11:
                return {
                    "date": tds[0],
                    "iso_date": roc_to_iso(tds[0]),
                    "foreign_buy_net": int(_clean_number(tds[1])),
                    "investment_trust_net": int(_clean_number(tds[2])),
                    "dealer_net": int(_clean_number(tds[3])),
                    "institutional_total_net": int(_clean_number(tds[4])),
                    "foreign_ratio": _clean_number(tds[9]),
                    "institutional_ratio": _clean_number(tds[10])
                }
    except Exception as e:
        logger.error(f"抓取 {stock_id} 三大法人資料異常: {e}")
    return None


def fetch_margin_trading(stock_id: str) -> Optional[Dict[str, Any]]:
    """抓取最新融資融券動態"""
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcn/zcn.djhtm?a={stock_id}"
    try:
        r = _safe_get(url, timeout=15, retries=2)
        if not r or r.status_code != 200:
            return None
        r.encoding = "big5"
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.find_all("tr")
        found_header = False
        for row in rows:
            tds = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(tds) >= 15 and tds[0] == "日期" and "買進" in tds[1]:
                found_header = True
                continue
            if found_header and len(tds) >= 15:
                return {
                    "date": tds[0],
                    "iso_date": roc_to_iso(tds[0]),
                    "margin_balance": int(_clean_number(tds[4])),
                    "margin_diff": int(_clean_number(tds[5])),
                    "short_balance": int(_clean_number(tds[11])),
                    "short_diff": int(_clean_number(tds[12])),
                    "short_margin_ratio": _clean_number(tds[13])
                }
    except Exception as e:
        logger.error(f"抓取 {stock_id} 融資融券資料異常: {e}")
    return None


def fetch_shareholding_dispersion(stock_id: str) -> Optional[Dict[str, Any]]:
    """
    抓取集保股權分散表 (大戶籌碼 vs 散戶籌碼週變化)
    """
    url = f"https://histock.tw/stock/large.aspx?no={stock_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = _safe_get(url, headers=headers, timeout=15, retries=2)
        if r and r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")
                data_rows = []
                for row in rows:
                    tds = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                    if len(tds) >= 5 and tds[0] != "日期":
                        data_rows.append(tds)

                if len(data_rows) >= 2:
                    # 比較最新週與前一週
                    latest = data_rows[0]
                    prev = data_rows[1]
                    cur_conc = _clean_number(latest[1])
                    prev_conc = _clean_number(prev[1])
                    diff_conc = round(cur_conc - prev_conc, 2)

                    return {
                        "date": latest[0],
                        "concentration_ratio": cur_conc,
                        "concentration_diff": diff_conc,
                        "foreign_ratio": _clean_number(latest[2]),
                        "major_holder_ratio": _clean_number(latest[3]),
                        "director_ratio": _clean_number(latest[4])
                    }
    except Exception as e:
        logger.error(f"抓取 {stock_id} 集保大戶籌碼分散異常: {e}")
    return None


def fetch_volume_profile_and_fundamentals(stock_id: str, anchor_date: Optional[str] = None) -> Dict[str, Any]:
    """
    計算 60 日價量分佈 (Volume Profile / 籌碼峰)、基本面估值 (PE, PB, Yield)
    以及近 3 個月日 K 線與均線資料 (MA5, MA20, MA60, Volume)
    """
    result = {
        "poc_low": 0.0,
        "poc_high": 0.0,
        "poc_volume_ratio": 0.0,
        "pe_ratio": 0.0,
        "pb_ratio": 0.0,
        "yield_pct": 0.0,
        "sector": "",
        "kline_data": None,
        "kline_1y": []
    }
    
    # 判斷上市或上櫃後綴
    ticker_symbols = [f"{stock_id}.TW", f"{stock_id}.TWO"]
    for sym in ticker_symbols:
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(period="1y")
            if not df.empty and len(df) >= 10:
                import pandas as pd
                # 若指定 anchor_date (例如盤中重跑前一日籌碼報告)，剔除尚未收盤或晚於 anchor_date 的盤中 K 棒
                if anchor_date:
                    df = df[df.index.strftime("%Y-%m-%d") <= anchor_date]
                    if df.empty or len(df) < 5:
                        continue

                # 0. 以交易所官方日成交資料 (含開高低收量) 校正最新一根 K 棒
                #    yfinance 盤後常出現當日 OHLC 為 NaN 或延遲，官方資料為收盤價唯一可信來源
                price_source = "yfinance"
                ohlc_estimated = False
                official = fetch_official_daily_quote(stock_id, target_date=anchor_date)
                if official:
                    o_date = pd.Timestamp(official["date"]).date()
                    last_date = df.index[-1].date()
                    row_vals = {
                        "Open": official["open"], "High": official["high"],
                        "Low": official["low"], "Close": official["close"],
                        "Volume": official["volume_shares"]
                    }
                    if o_date == last_date:
                        for k, v in row_vals.items():
                            df.loc[df.index[-1], k] = v
                        price_source = "official"
                    elif o_date > last_date and (not anchor_date or official["date"] <= anchor_date):
                        new_idx = pd.Timestamp(o_date).tz_localize(df.index.tz) if df.index.tz else pd.Timestamp(o_date)
                        df.loc[new_idx] = {**{c: 0.0 for c in df.columns}, **row_vals}
                        price_source = "official"

                if np.isnan(df["Close"].iloc[-1]):
                    # 官方資料不可得時，僅以 fast_info 補收盤價；開高低無法得知，標記為估計值
                    last_price = None
                    try:
                        last_price = float(ticker.fast_info.last_price)
                    except Exception:
                        pass
                    if last_price is not None and not np.isnan(last_price):
                        for k in ("Open", "High", "Low", "Close"):
                            if np.isnan(df[k].iloc[-1]):
                                df.loc[df.index[-1], k] = last_price
                        ohlc_estimated = True
                        price_source = "yfinance_fast_info"
                    else:
                        df = df.dropna(subset=["Close"])
                result["price_source"] = price_source
                result["price_date"] = df.index[-1].strftime("%Y-%m-%d")

                # 0.5 儲存完整 1 年期日 K 序列 (供 1 年期波段轉折高低點偵測與分點逐日帳本回測使用)
                result["kline_1y"] = [
                    {
                        "date": d.strftime("%Y-%m-%d"),
                        "Open": round(float(r["Open"]), 2),
                        "High": round(float(r["High"]), 2),
                        "Low": round(float(r["Low"]), 2),
                        "Close": round(float(r["Close"]), 2),
                        "Volume": int(r["Volume"] / 1000)
                    }
                    for d, r in df.iterrows()
                    if not np.isnan(r["Close"])
                ]

                # 1. 計算 60 日籌碼密集峰 (POC)
                df_60 = df.tail(60)
                p_min = df_60["Low"].min()
                p_max = df_60["High"].max()
                bins = np.linspace(p_min, p_max, 11)
                hist, edges = np.histogram(df_60["Close"], bins=bins, weights=df_60["Volume"])
                max_idx = int(np.argmax(hist))

                total_vol = hist.sum()
                poc_ratio = round((hist[max_idx] / total_vol) * 100, 2) if total_vol > 0 else 0.0

                result["poc_low"] = round(float(edges[max_idx]), 2)
                result["poc_high"] = round(float(edges[max_idx + 1]), 2)
                result["poc_volume_ratio"] = poc_ratio

                # 2. 基本面估值
                info = ticker.info or {}
                result["pe_ratio"] = round(float(info.get("trailingPE") or 0.0), 2)
                result["pb_ratio"] = round(float(info.get("priceToBook") or 0.0), 2)
                result["yield_pct"] = round(float(info.get("dividendYield") or 0.0), 2)
                result["sector"] = info.get("sector") or ""

                # 3. 近 3 個月日 K 線 (含 MA5, MA20, MA60 與成交量)
                ma5 = df["Close"].rolling(5).mean()
                ma20 = df["Close"].rolling(20).mean()
                ma60 = df["Close"].rolling(60).mean()

                df_3mo = df.tail(63)  # 約 3 個月交易日
                dates = [d.strftime("%Y-%m-%d") for d in df_3mo.index]
                candles = [
                    [
                        round(float(r["Open"]), 2),
                        round(float(r["Close"]), 2),
                        round(float(r["Low"]), 2),
                        round(float(r["High"]), 2)
                    ]
                    for _, r in df_3mo.iterrows()
                ]
                volumes = [int(v / 1000) for v in df_3mo["Volume"]]  # 單位：張

                tail_len = len(df_3mo)
                ma5_vals = [round(float(x), 2) if not np.isnan(x) else None for x in ma5.tail(tail_len)]
                ma20_vals = [round(float(x), 2) if not np.isnan(x) else None for x in ma20.tail(tail_len)]
                ma60_vals = [round(float(x), 2) if not np.isnan(x) else None for x in ma60.tail(tail_len)]

                result["kline_data"] = {
                    "dates": dates,
                    "candles": candles,
                    "volumes": volumes,
                    "ma5": ma5_vals,
                    "ma20": ma20_vals,
                    "ma60": ma60_vals
                }

                # 4. 技術面分析：5MA / 20MA / 60MA 均線交叉、多頭排列與放量偵測
                c = df["Close"]
                v = df["Volume"] / 1000  # 單位：張
                o = df["Open"]

                vol_ma5 = v.rolling(5).mean()
                vol_ma20 = v.rolling(20).mean()

                t_close = float(c.iloc[-1])
                t_open = float(o.iloc[-1])
                t_vol = float(v.iloc[-1])

                t_ma5 = float(ma5.iloc[-1]) if not np.isnan(ma5.iloc[-1]) else t_close
                y_ma5 = float(ma5.iloc[-2]) if len(ma5) >= 2 and not np.isnan(ma5.iloc[-2]) else t_ma5

                t_ma20 = float(ma20.iloc[-1]) if not np.isnan(ma20.iloc[-1]) else t_close
                y_ma20 = float(ma20.iloc[-2]) if len(ma20) >= 2 and not np.isnan(ma20.iloc[-2]) else t_ma20

                t_ma60 = float(ma60.iloc[-1]) if not np.isnan(ma60.iloc[-1]) else t_close
                y_ma60 = float(ma60.iloc[-2]) if len(ma60) >= 2 and not np.isnan(ma60.iloc[-2]) else t_ma60

                t_vol5 = float(vol_ma5.iloc[-1]) if not np.isnan(vol_ma5.iloc[-1]) else 0.0
                t_vol20 = float(vol_ma20.iloc[-1]) if not np.isnan(vol_ma20.iloc[-1]) else 0.0

                # 均線交叉判斷
                cross_5_20 = bool((y_ma5 <= y_ma20) and (t_ma5 > t_ma20))
                cross_5_60 = bool((y_ma5 <= y_ma60) and (t_ma5 > t_ma60))
                cross_20_60 = bool((y_ma20 <= y_ma60) and (t_ma20 > t_ma60))

                # 多頭排列判斷 (現價 > 5MA > 20MA > 60MA)
                is_bullish_alignment = bool(t_close >= t_ma5 >= t_ma20 >= t_ma60)

                # 放量出量判斷
                vol_ratio_5 = round(t_vol / t_vol5, 2) if t_vol5 > 0 else 0.0
                is_vol_surge = bool((vol_ratio_5 >= 1.3) and (t_vol >= t_vol20))

                # 月線是否翻揚或走平
                ma20_slope_up = bool(t_ma20 >= y_ma20)
                is_red_k = None if ohlc_estimated else bool(t_close >= t_open)

                result["technical_signals"] = {
                    "close": round(t_close, 2),
                    "open": round(t_open, 2),
                    "volume": int(t_vol),
                    "ma5": round(t_ma5, 2),
                    "ma20": round(t_ma20, 2),
                    "ma60": round(t_ma60, 2),
                    "vol_ma5": int(t_vol5),
                    "vol_ma20": int(t_vol20),
                    "vol_ratio_5": vol_ratio_5,
                    "cross_5_20": cross_5_20,
                    "cross_5_60": cross_5_60,
                    "cross_20_60": cross_20_60,
                    "is_bullish_alignment": is_bullish_alignment,
                    "is_vol_surge": is_vol_surge,
                    "ma20_slope_up": ma20_slope_up,
                    "is_red_k": is_red_k,
                    "ohlc_estimated": ohlc_estimated,
                    "price_source": price_source,
                    "price_date": result["price_date"]
                }
                break
        except Exception as e:
            logger.warning(f"以 {sym} 抓取籌碼峰與基本面異常: {e}")
            continue

    return result
