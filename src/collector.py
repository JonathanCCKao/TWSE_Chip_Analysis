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


def _fetch_dispersion_norway(stock_id: str) -> Optional[Dict[str, Any]]:
    """
    從台灣股市資訊網解析完整集保股權分散歷程 (近 26 週):
    - >200張主力大戶持股比例、人數、週增減、人均持股張數
    - <10張純散戶持股比例、人數、週增減、人均持股張數
    - 6 個月歷史趨勢圖序列 (dates, over_200_pct, under_10_pct, prices)
    """
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={stock_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = _safe_get(url, headers=headers, timeout=12, retries=2)
        if not r or r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        tables = soup.find_all("table")

        detail_table = None
        price_table = None
        count_table = None

        for t in tables:
            txt = t.get_text()
            if "小於1張" in txt and "1000張以上" in txt:
                detail_table = t
            elif ">400張大股東持有張數" in txt and "收盤價" in txt:
                price_table = t
            elif "持股張數分級" in txt and "400 張以上" in txt:
                count_table = t

        if not detail_table or not price_table:
            return None

        price_map = {}
        for row in price_table.find_all("tr"):
            tds = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            for i, val in enumerate(tds):
                if re.match(r"^\d{8}$", val):
                    sub = tds[i:]
                    if len(sub) >= 13:
                        d_str = f"{val[:4]}-{val[4:6]}-{val[6:8]}"
                        try:
                            tot_s = float(sub[1].replace(",", ""))
                            tot_h = int(sub[2].replace(",", ""))
                            o400_h = int(sub[6].replace(",", ""))
                            p_val = float(sub[12].replace(",", ""))
                            price_map[d_str] = {
                                "price": p_val,
                                "total_holders": tot_h,
                                "over_400_cnt": o400_h,
                                "total_shares": tot_s,
                                "major_1000_pct": float(sub[11].replace(",", ""))
                            }
                        except Exception:
                            pass

        # 解析 count_table (D2) 近期週次精確人數
        exact_counts = {}
        if count_table:
            dates_row = None
            rows = count_table.find_all("tr")
            for r in rows:
                c_dates = [td.get_text(strip=True) for td in r.find_all(["td", "th"]) if re.match(r"^\d{8}$", td.get_text(strip=True))]
                if c_dates:
                    dates_row = c_dates
                    break
            if dates_row:
                for d in dates_row:
                    iso_d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                    exact_counts[iso_d] = {"u10": 0, "o200": 0}

                for row in rows:
                    tds = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                    lbl = tds[1] if len(tds) > 1 else ""
                    for i, d in enumerate(dates_row):
                        iso_d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                        col_idx = 2 + i * 4
                        if len(tds) > col_idx:
                            try:
                                c = int(tds[col_idx].replace(",", ""))
                                if lbl in ["1-999股", "1-5張", "5-10張"]:
                                    exact_counts[iso_d]["u10"] += c
                                elif lbl in ["200-400張", "400-600張", "600-800張", "800-1,000張", "1,000張以上"]:
                                    exact_counts[iso_d]["o200"] += c
                            except Exception:
                                pass

        history_weeks = []
        seen_dates = set()
        for row in detail_table.find_all("tr"):
            tds = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            for c_idx, val in enumerate(tds):
                if re.match(r"^\d{8}$", val):
                    d_str = f"{val[:4]}-{val[4:6]}-{val[6:8]}"
                    if d_str in seen_dates:
                        continue
                    sub_vals = tds[c_idx+1:c_idx+16]
                    if len(sub_vals) >= 15:
                        try:
                            floats = [float(v.replace(",", "")) for v in sub_vals]
                            u10 = round(sum(floats[0:3]), 2)
                            p_200_400 = floats[10]
                            o200 = round(sum(floats[10:15]), 2)
                            o1000 = round(floats[14], 2)
                            p_info = price_map.get(d_str, {})
                            tot_s = p_info.get("total_shares", 0)
                            tot_h = p_info.get("total_holders", 0)
                            o400_cnt = p_info.get("over_400_cnt", 0)

                            if d_str in exact_counts and exact_counts[d_str]["o200"] > 0:
                                o200_cnt = exact_counts[d_str]["o200"]
                                u10_cnt = exact_counts[d_str]["u10"]
                            else:
                                cnt_200_400 = round((tot_s * (p_200_400 / 100.0)) / 280.0) if tot_s > 0 else 0
                                o200_cnt = o400_cnt + cnt_200_400
                                u10_cnt = round(tot_h * (0.925 + (u10 / 100.0) * 0.07)) if tot_h > 0 else 0

                            history_weeks.append({
                                "date": d_str,
                                "under_10_pct": u10,
                                "over_200_pct": o200,
                                "over_1000_pct": o1000,
                                "price": p_info.get("price", 0.0),
                                "total_holders": tot_h,
                                "over_200_cnt": o200_cnt,
                                "under_10_cnt": u10_cnt
                            })
                            seen_dates.add(d_str)
                        except Exception:
                            pass

        if not history_weeks:
            return None

        history_weeks = history_weeks[:26]
        history_asc = list(reversed(history_weeks))

        latest_w = history_weeks[0]
        prev_w = history_weeks[1] if len(history_weeks) > 1 else latest_w

        o200_count_latest = latest_w.get("over_200_cnt", 0)
        o200_count_prev = prev_w.get("over_200_cnt", 0)
        u10_count_latest = latest_w.get("under_10_cnt", 0)
        u10_count_prev = prev_w.get("under_10_cnt", 0)

        latest_w = history_weeks[0]
        prev_w = history_weeks[1] if len(history_weeks) > 1 else latest_w

        o200_ratio = latest_w.get("over_200_pct", 0.0)
        o200_diff = round(o200_ratio - prev_w.get("over_200_pct", 0.0), 2)
        u10_ratio = latest_w.get("under_10_pct", 0.0)
        u10_diff = round(u10_ratio - prev_w.get("under_10_pct", 0.0), 2)

        tot_shares = price_map.get(latest_w.get("date"), {}).get("total_shares", 0)
        o200_shares = tot_shares * (o200_ratio / 100.0)
        u10_shares = tot_shares * (u10_ratio / 100.0)

        avg_shares_o200 = round(o200_shares / o200_count_latest, 1) if o200_count_latest > 0 else 0.0
        avg_shares_u10 = round(u10_shares / u10_count_latest, 2) if u10_count_latest > 0 else 0.0

        return {
            "date": latest_w.get("date", ""),
            "over_200_ratio": o200_ratio,
            "over_200_diff": o200_diff,
            "over_200_count": o200_count_latest,
            "over_200_count_diff": o200_count_latest - o200_count_prev,
            "avg_shares_over_200": avg_shares_o200,

            "under_10_ratio": u10_ratio,
            "under_10_diff": u10_diff,
            "under_10_count": u10_count_latest,
            "under_10_count_diff": u10_count_latest - u10_count_prev,
            "avg_shares_under_10": avg_shares_u10,

            # 向下相容既有欄位
            "concentration_ratio": o200_ratio,
            "concentration_diff": o200_diff,
            "major_holder_ratio": latest_w.get("over_1000_pct", 0.0),

            # 6 個月趨勢圖數據序列 (升冪排序)
            "trend_chart": {
                "dates": [w["date"] for w in history_asc],
                "over_200_pct": [w["over_200_pct"] for w in history_asc],
                "under_10_pct": [w["under_10_pct"] for w in history_asc],
                "prices": [w["price"] for w in history_asc],
                "over_200_cnt": [w["over_200_cnt"] for w in history_asc],
                "under_10_cnt": [w["under_10_cnt"] for w in history_asc]
            }
        }
    except Exception as e:
        logger.warning(f"解析 {stock_id} 完整集保歷程失敗: {e}，將嘗試 fallback 至 HiStock")
        return None


def fetch_shareholding_dispersion(stock_id: str) -> Optional[Dict[str, Any]]:
    """
    抓取集保股權分散表 (優先解析 6 個月 >200張大戶 ✕ <10張散戶歷程，失敗則自動降級至 HiStock)
    """
    res = _fetch_dispersion_norway(stock_id)
    if res and res.get("trend_chart"):
        return res

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
                        "over_200_ratio": cur_conc,
                        "over_200_diff": diff_conc,
                        "under_10_ratio": 0.0,
                        "under_10_diff": 0.0,
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


def _normalize_news_source(raw_source: str, title: str = "", link: str = "") -> str:
    """標準化權威財經媒體名稱"""
    combined = f"{raw_source} {title} {link}".lower()
    if "moneydj" in combined or "嘉實" in combined or "fubon-ebrokerdj" in combined:
        return "MoneyDJ 理財網"
    if "cnyes" in combined or "鉅亨" in combined:
        return "鉅亨網 Anue"
    if "经济日报" in combined or "經濟日報" in combined or "money.udn" in combined:
        return "經濟日報"
    if "工商時報" in combined or "工商" in combined or "ctee.com" in combined:
        return "工商時報"
    if "中央社" in combined or "cna.com" in combined:
        return "中央社財經"
    return raw_source.strip() or "權威財經媒體"


def _clean_news_title(title: str) -> str:
    """清理新聞標題尾端的媒體名稱與多餘後綴"""
    t = str(title or "").strip()
    t = re.sub(
        r"\s*[-｜|]\s*(新聞\s*[-｜|]\s*)?(MoneyDJ(\s*理財網)?|鉅亨網|鉅亨號|經濟日報|工商時報|中央社|CNA|Yahoo奇摩股市).*$",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"^鉅亨速報\s*[-—:：]\s*", "", t)
    return t.strip()


def _title_similarity_key(title: str) -> str:
    """產生去重比對鍵（移除標點符號、空白與常見前綴）"""
    cleaned = re.sub(r"[\s\W_]+", "", _clean_news_title(title).lower())
    return cleaned


def _is_duplicate_title(title: str, seen_keys: List[str]) -> bool:
    """檢查標題是否與已選新聞高度重複（例如同篇重訊或法說會公告在不同媒體重複刊登）"""
    key = _title_similarity_key(title)
    if not key:
        return True
    is_fashuo = ("法說會" in title or "法人說明會" in title)
    is_factset = ("factset" in title.lower() and "目標價" in title)
    for sk in seen_keys:
        if key == sk or (len(key) >= 8 and (key in sk or sk in key)):
            return True
        if is_fashuo and ("法說會" in sk or "法人說明會" in sk):
            return True
        if is_factset and ("factset" in sk and "目標價" in sk):
            return True
        # 計算字元集合重疊率 (Jaccard / Overlap)
        set_a, set_b = set(key), set(sk)
        overlap = len(set_a & set_b) / max(min(len(set_a), len(set_b)), 1)
        if overlap >= 0.78:
            return True
    return False


def _score_and_classify_news(
    title: str,
    source: str,
    pub_dt: Any,
    ref_dt: Any,
    scope: str,
    stock_id: str,
    stock_name: str,
    mode: str,
) -> Dict[str, Any]:
    """
    本地零 Token 量化重要性評分與多空語意分類器：
    綜合考量「媒體權威度 + 催化劑關鍵字權重 + 時效性」
    """
    score = 50.0

    # 1. 權威媒體權重 (優先加權 MoneyDJ 理財網 與 鉅亨網 Anue)
    if source == "MoneyDJ 理財網":
        score += 32.0
    elif source == "鉅亨網 Anue":
        score += 30.0
    elif source in ("經濟日報", "工商時報", "中央社財經"):
        score += 24.0

    # 2. 個股直接關聯度加權
    if scope == "stock":
        if stock_name and stock_name in title:
            score += 25.0
        if stock_id and stock_id in title:
            score += 15.0

    # 3. 重大基本面與籌碼催化劑關鍵字加權
    high_impact_kw = [
        "法說", "財報", "營收", "EPS", "毛利率", "目標價", "Factset", "FactSet",
        "澄清", "重訊", "重大訊息", "庫藏股", "股利", "配息", "資本支出", "擴產",
        "訂單", "出貨", "外資", "投信", "法人", "台積電", "輝達", "AI", "聯準會", "央行", "關稅"
    ]
    for kw in high_impact_kw:
        if kw.lower() in title.lower():
            score += 10.0

    # 4. 判斷多空語意標籤 (sentiment)
    bull_kw = [
        "創高", "新高", "大增", "成長", "調升", "上修", "買超", "看旺", "樂觀",
        "突破", "加碼", "優於預期", "強攻", "點讚", "獲認證", "滿載", "提前", "利多"
    ]
    bear_kw = [
        "衰退", "下滑", "下修", "調降", "賣超", "提款", "重挫", "暴跌", "跌破",
        "保守", "虧損", "裁員", "利空", "衝擊", "失守", "壓回", "砍單", "警示"
    ]
    official_kw = ["法說會", "法人說明會", "澄清", "公告", "董事會", "股東會", "重訊"]

    bull_hits = sum(1 for k in bull_kw if k in title)
    bear_hits = sum(1 for k in bear_kw if k in title)
    is_official = any(k in title for k in official_kw)

    if is_official and bull_hits == 0 and bear_hits == 0:
        sentiment = "📢 重訊/法說"
        sentiment_tone = "official"
        score += 8.0
    elif bull_hits > bear_hits:
        sentiment = "🔥 利多催化"
        sentiment_tone = "bull"
        score += 6.0 * bull_hits
    elif bear_hits > bull_hits:
        sentiment = "⚠️ 利空警訊"
        sentiment_tone = "bear"
        score += 6.0 * bear_hits
    else:
        sentiment = "📊 焦點動態" if scope == "stock" else "🌐 總經盤勢"
        sentiment_tone = "neutral"

    # 5. 時效性權重衰減 (日報強調近 48 小時，週報涵蓋近 7~10 日高影響力事件)
    if pub_dt and ref_dt:
        age_hours = max(0.0, (ref_dt - pub_dt).total_seconds() / 3600.0)
        if mode == "daily":
            if age_hours <= 36:
                score += 30.0
            elif age_hours <= 72:
                score += 15.0
            else:
                score -= min(30.0, (age_hours - 72) * 0.25)
        else:  # weekly
            if age_hours <= 168:  # 7 days
                score += 20.0
            else:
                score -= min(25.0, (age_hours - 168) * 0.15)

    return {
        "score": round(score, 1),
        "sentiment": sentiment,
        "sentiment_tone": sentiment_tone,
    }


def _fetch_google_rss_news(query: str, max_items: int = 25) -> List[Dict[str, Any]]:
    """透過 Google News RSS 檢索指定權威媒體網域之新聞"""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from datetime import timezone, timedelta

    tw_tz = timezone(timedelta(hours=8))
    url = "https://news.google.com/rss/search"
    params = {
        "q": query,
        "hl": "zh-TW",
        "gl": "TW",
        "ceid": "TW:zh-Hant",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    results: List[Dict[str, Any]] = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=12)
        if resp.status_code != 200:
            return results
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item")[:max_items]:
            raw_title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub_str = item.findtext("pubDate") or ""
            raw_src = item.findtext("source") or ""

            pub_dt = None
            pub_display = ""
            if pub_str:
                try:
                    dt = parsedate_to_datetime(pub_str).astimezone(tw_tz)
                    pub_dt = dt
                    pub_display = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pub_display = pub_str[:16]

            source_name = _normalize_news_source(raw_src, raw_title, link)
            clean_t = _clean_news_title(raw_title)
            if not clean_t:
                continue

            results.append({
                "title": clean_t,
                "source": source_name,
                "pub_date": pub_display,
                "pub_dt": pub_dt,
                "url": link,
            })
    except Exception as e:
        logger.warning(f"Google News RSS 抓取異常 ({query}): {e}")
    return results


def _fetch_cnyes_direct_news(stock_id: str, stock_name: str, limit: int = 15) -> List[Dict[str, Any]]:
    """從鉅亨網 Anue 官方台股新聞 API 抓取最新即時頭條與個股動態"""
    from datetime import datetime, timezone, timedelta

    tw_tz = timezone(timedelta(hours=8))
    url = f"https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit={limit}"
    headers = {"User-Agent": "Mozilla/5.0"}
    results: List[Dict[str, Any]] = []
    try:
        resp = _safe_get(url, headers=headers, timeout=10, retries=1)
        if not resp or resp.status_code != 200:
            return results
        data_list = (resp.json().get("items") or {}).get("data") or []
        for row in data_list:
            raw_title = row.get("title") or ""
            news_id = row.get("newsId")
            pub_ts = row.get("publishAt")
            if not raw_title or not news_id:
                continue
            # 過濾掉與本股無關之其他小股盤中速報
            if "盤中速報" in raw_title and stock_id not in raw_title and (not stock_name or stock_name not in raw_title):
                continue

            pub_dt = None
            pub_display = ""
            if pub_ts:
                try:
                    dt = datetime.fromtimestamp(int(pub_ts), tz=tw_tz)
                    pub_dt = dt
                    pub_display = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            clean_t = _clean_news_title(raw_title)
            results.append({
                "title": clean_t,
                "source": "鉅亨網 Anue",
                "pub_date": pub_display,
                "pub_dt": pub_dt,
                "url": f"https://news.cnyes.com/news/id/{news_id}",
            })
    except Exception as e:
        logger.debug(f"鉅亨網 API 抓取異常: {e}")
    return results


def fetch_authoritative_financial_news(
    stock_id: str,
    stock_name: str = "",
    anchor_date: Optional[str] = None,
    mode: str = "daily",
    top_k: int = 5,
    max_stock_news: int = 4,
    min_macro_news: int = 1,
) -> List[Dict[str, Any]]:
    """
    採集當日 (daily) 或當週 (weekly) 前 5 大權威財經新聞：
    - 核心媒體：MoneyDJ 理財網 (moneydj.com) ✕ 鉅亨網 (cnyes.com)
    - 備援白名單：經濟日報 (money.udn.com)、工商時報 (ctee.com.tw)、中央社 (cna.com.tw)
    - 混合配比：優先選入 3~4 則「🎯 個股焦點」新聞 + 1~2 則「🌐 台股總經」重磅新聞（不足自動互補滿 5 則）
    """
    from datetime import datetime, timezone, timedelta

    tw_tz = timezone(timedelta(hours=8))
    ref_dt = datetime.now(tz=tw_tz)
    if anchor_date:
        try:
            parsed_d = datetime.strptime(anchor_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=tw_tz
            )
            if parsed_d < ref_dt:
                ref_dt = parsed_d
        except Exception:
            pass

    name_part = f'"{stock_name}" OR "{stock_id}"' if stock_name and stock_name != stock_id else f'"{stock_id}"'
    stock_time_filter = "when:7d" if mode == "daily" else "when:14d"
    macro_time_filter = "when:2d" if mode == "daily" else "when:7d"

    # 1. 抓取個股專屬新聞 (第一優先：MoneyDJ 理財網 與 鉅亨網 Anue)
    q_stock_primary = f"({name_part}) (site:moneydj.com OR site:cnyes.com) {stock_time_filter}"
    raw_stock_news = _fetch_google_rss_news(q_stock_primary, max_items=20)

    # 若該個股在雙核心媒體近期新聞少於 3 則，自動由經濟日報/工商時報/中央社白名單遞補
    if len(raw_stock_news) < 3:
        q_stock_fallback = f"({name_part}) (site:money.udn.com OR site:ctee.com.tw OR site:cna.com.tw) {stock_time_filter}"
        raw_stock_news.extend(_fetch_google_rss_news(q_stock_fallback, max_items=10))

    # 2. 抓取台股大盤與總經重磅新聞 (MoneyDJ 理財網 ✕ 鉅亨網 Anue)
    q_macro = (
        f"台股 (外資 OR 大盤 OR 台積電 OR 聯準會 OR 央行 OR AI OR 期貨) "
        f"-盤中速報 -近5日 (site:moneydj.com OR site:cnyes.com) {macro_time_filter}"
    )
    raw_macro_news = _fetch_google_rss_news(q_macro, max_items=20)
    raw_macro_news.extend(_fetch_cnyes_direct_news(stock_id, stock_name, limit=12))

    noise_patterns = [
        "近5日買超", "近5日賣超", "盤中速報", "融資排行", "融券排行",
        "股價拉至漲停", "股價跌停", "線圖", "個股報價", "技術線圖",
        "股價很讚", "無內文", "討論區", "Yahoo奇摩股市"
    ]

    scored_stock: List[Dict[str, Any]] = []
    seen_keys: List[str] = []

    for item in raw_stock_news:
        t = item["title"]
        if any(np_str in t for np_str in noise_patterns):
            continue
        # 確保個股新聞確實與該個股或產業相關
        if stock_name and stock_name not in t and stock_id not in t:
            continue
        if _is_duplicate_title(t, seen_keys):
            continue
        seen_keys.append(_title_similarity_key(t))

        meta = _score_and_classify_news(
            t, item["source"], item.get("pub_dt"), ref_dt, "stock", stock_id, stock_name, mode
        )
        scored_stock.append({
            "title": t,
            "source": item["source"],
            "pub_date": item["pub_date"],
            "url": item["url"],
            "scope": "🎯 個股焦點",
            "sentiment": meta["sentiment"],
            "sentiment_tone": meta["sentiment_tone"],
            "score": meta["score"],
        })

    scored_stock.sort(key=lambda x: x["score"], reverse=True)

    scored_macro: List[Dict[str, Any]] = []
    macro_must_kw = ["台股", "大盤", "外資", "台積電", "聯準會", "央行", "費半", "美股", "新台幣", "關稅", "AI", "加權", "期貨", "ETF", "投信"]
    for item in raw_macro_news:
        t = item["title"]
        if any(np_str in t for np_str in noise_patterns):
            continue
        # 若總經列表剛好抓到本股新聞，自動歸類為個股焦點
        is_self = (stock_name and stock_name in t) or (stock_id in t)
        if not is_self:
            # 排除其他單一個股的 FactSet 調查、單一個股公告或非本股之法說會，確保總經頭條純度
            if "factset" in t.lower() or re.search(r"\(\d{4}-TW\)", t) or "法說會" in t:
                continue
            if not any(mk in t for mk in macro_must_kw):
                continue
        if _is_duplicate_title(t, seen_keys):
            continue
        seen_keys.append(_title_similarity_key(t))
        scope_type = "stock" if is_self else "macro"
        meta = _score_and_classify_news(
            t, item["source"], item.get("pub_dt"), ref_dt, scope_type, stock_id, stock_name, mode
        )
        entry = {
            "title": t,
            "source": item["source"],
            "pub_date": item["pub_date"],
            "url": item["url"],
            "scope": "🎯 個股焦點" if is_self else "🌐 台股總經",
            "sentiment": meta["sentiment"],
            "sentiment_tone": meta["sentiment_tone"],
            "score": meta["score"],
        }
        if is_self:
            scored_stock.append(entry)
        else:
            scored_macro.append(entry)

    scored_stock.sort(key=lambda x: x["score"], reverse=True)
    scored_macro.sort(key=lambda x: x["score"], reverse=True)

    # 3. 混合配比組裝 Top 5 (個股焦點最多 max_stock_news 則 + 台股總經至少 min_macro_news 則，不足自動互補)
    selected_stock = scored_stock[:max_stock_news]
    needed_macro = max(min_macro_news, top_k - len(selected_stock))
    selected_macro = scored_macro[:needed_macro]

    combined = selected_stock + selected_macro
    if len(combined) < top_k and len(scored_stock) > len(selected_stock):
        extra = scored_stock[len(selected_stock) : len(selected_stock) + (top_k - len(combined))]
        combined.extend(extra)

    final_list = combined[:top_k]

    def _parse_sort_key(item: Dict[str, Any]) -> str:
        raw = str(item.get("pub_date") or item.get("published_at") or "")
        m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?", raw)
        if m:
            y = m.group(1)
            mo = f"{int(m.group(2)):02d}"
            d = f"{int(m.group(3)):02d}"
            h = f"{int(m.group(4)):02d}" if m.group(4) else "00"
            mi = f"{int(m.group(5)):02d}" if m.group(5) else "00"
            s = f"{int(m.group(6)):02d}" if m.group(6) else "00"
            return f"{y}-{mo}-{d} {h}:{mi}:{s}"
        m2 = re.search(r"(\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?", raw)
        if m2:
            mo = f"{int(m2.group(1)):02d}"
            d = f"{int(m2.group(2)):02d}"
            h = f"{int(m2.group(3)):02d}" if m2.group(3) else "00"
            mi = f"{int(m2.group(4)):02d}" if m2.group(4) else "00"
            return f"9999-{mo}-{d} {h}:{mi}:00"
        return raw

    # 按照發布日期時間由新到舊排序 (越新的放越前面)
    final_list.sort(key=_parse_sort_key, reverse=True)
    for idx, n in enumerate(final_list, 1):
        n["rank"] = idx

    return final_list

