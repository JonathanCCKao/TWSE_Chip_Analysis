"""
台股代碼 ⇄ 股票名稱 雙向識別與自選股清單 (watchlist.txt) 解析模組

支援：
1. 於 watchlist.txt 中直接輸入「股票名稱」(如：視陽、光寶科)、「股票代碼」(如：6782、2301) 或「代碼+名稱」(如：6782 視陽)。
2. 支援於 watchlist.txt 中使用選用標籤區分：
   - 預設 (無標籤)：同時納入【每日戰術快報】與【週末戰略週報】
   - 行尾加上 @daily 或 @日報：僅產出日報
   - 行尾加上 @weekly 或 @週報：僅產出週報
   - `#` 或 `//` 開頭視為註解，可隨時暫停追蹤某檔個股。
3. 自動從證交所 (TWSE) 與櫃買中心 (TPEx) 官方開放資料同步全市場上市櫃股票代碼與名稱對照表，
   並快取於 `data/tw_stock_map.json`，離線或 API 異常時仍可穩定運作。
"""
import os
import re
import json
import logging
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)

# 內建核心預設保底對照表 (避免初次完全斷網時無法識別常用個股)
BUILTIN_STOCK_MAP: Dict[str, str] = {
    "6782": "視陽",
    "2301": "光寶科",
    "1303": "南亞",
    "3005": "神基",
    "2330": "台積電",
    "2317": "鴻海",
    "2454": "聯發科",
    "2308": "台達電",
    "2382": "廣達",
    "3231": "緯創",
    "6669": "緯穎",
    "3017": "奇鋐",
    "3324": "雙鴻",
    "2603": "長榮",
    "2609": "陽明",
    "2615": "萬海",
    "5347": "世界",
}


def _normalize_name(name: str) -> str:
    """正規化股票名稱 (統一 臺->台、移除空白與全形空白、轉大寫)"""
    if not name:
        return ""
    s = str(name).strip().replace("　", "").replace(" ", "")
    s = s.replace("臺", "台").replace("－", "-").upper()
    return s


def _is_valid_stock_or_etf_code(code: str) -> bool:
    """過濾掉 6 碼權證與牛熊證，僅保留 4 碼上市櫃普通股與 00 開頭之 ETF"""
    if not code:
        return False
    if re.match(r"^[1-9]\d{3}[A-Za-z]?$", code):
        return True
    if re.match(r"^00\d{2,4}[A-Za-z]?$", code):
        return True
    return False


class StockResolver:
    def __init__(self, cache_path: str = "data/tw_stock_map.json"):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_path = cache_path if os.path.isabs(cache_path) else os.path.join(base_dir, cache_path)
        self.code_to_name: Dict[str, str] = dict(BUILTIN_STOCK_MAP)
        self.name_to_code: Dict[str, str] = {}
        self._load_or_refresh_cache()

    def _rebuild_reverse_index(self):
        self.name_to_code = {}
        # 優先讓 4 碼普通股寫入反向索引
        sorted_items = sorted(self.code_to_name.items(), key=lambda kv: (len(kv[0]) != 4, kv[0]))
        for code, name in sorted_items:
            norm = _normalize_name(name)
            if norm and norm not in self.name_to_code:
                self.name_to_code[norm] = code

    def _load_or_refresh_cache(self):
        loaded = False
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and 50 < len(data) < 5000:
                    filtered = {
                        str(k).strip(): str(v).strip()
                        for k, v in data.items()
                        if _is_valid_stock_or_etf_code(str(k).strip())
                    }
                    self.code_to_name.update(filtered)
                    loaded = True
            except Exception as e:
                logger.warning(f"讀取股票對照快取失敗: {e}")

        self._rebuild_reverse_index()
        if not loaded:
            self.refresh_from_official()

    def refresh_from_official(self) -> int:
        """從 TWSE 與 TPEx 官方 OpenAPI 更新全市場上市櫃股票代碼與名稱 (排除權證)"""
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        new_map: Dict[str, str] = dict(BUILTIN_STOCK_MAP)
        added = 0

        # 1. TWSE 上市股票與 ETF
        try:
            r1 = requests.get(
                "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                headers=headers,
                timeout=12,
            )
            if r1.status_code == 200:
                for item in r1.json():
                    code = str(item.get("Code", "")).strip()
                    name = str(item.get("Name", "")).strip()
                    if code and name and _is_valid_stock_or_etf_code(code):
                        new_map[code] = name
                        added += 1
        except Exception as e:
            logger.debug(f"更新 TWSE 股票清單失敗: {e}")

        # 2. TPEx 上櫃股票與 ETF
        try:
            r2 = requests.get(
                "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                headers=headers,
                timeout=12,
            )
            if r2.status_code == 200:
                for item in r2.json():
                    code = str(item.get("SecuritiesCompanyCode", "")).strip()
                    name = str(item.get("CompanyName", "")).strip()
                    if code and name and "+++++" not in name and _is_valid_stock_or_etf_code(code):
                        new_map[code] = name
                        added += 1
        except Exception as e:
            logger.debug(f"更新 TPEx 股票清單失敗: {e}")

        if added > 0:
            self.code_to_name = new_map
            self._rebuild_reverse_index()
            try:
                os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(self.code_to_name, f, ensure_ascii=False, indent=2)
                logger.info(f"已同步全市場上市櫃股票與 ETF 對照表，共 {len(self.code_to_name)} 檔 ({self.cache_path})")
            except Exception as e:
                logger.warning(f"寫入股票對照快取失敗: {e}")
        return added

    def _probe_moneydj_name(self, code: str) -> Optional[str]:
        """若為新掛牌或興櫃個股，直接從 MoneyDJ 分點頁面解析股名"""
        url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{code}_1.djhtm"
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code == 200:
                html = r.content.decode("big5", errors="ignore")
                m = re.search(rf"{re.escape(code)}\s*([^\s<()\"'主力進出]+)", html)
                if m:
                    name = m.group(1).strip()
                    if name:
                        self.code_to_name[code] = name
                        self._rebuild_reverse_index()
                        return name
        except Exception:
            pass
        return None

    def resolve(self, token: str) -> Optional[Tuple[str, str]]:
        """
        將使用者輸入的「股票代碼」、「股票名稱」或「代碼+名稱」(如 '6782', '視陽', '6782 視陽', '視陽(6782)')
        解析為 (stock_code, stock_name)。若無法識別則回傳 None。
        """
        raw = str(token or "").strip()
        if not raw:
            return None

        # 1. 若同時包含 4~6 碼代號與文字 (例如 "6782 視陽" 或 "視陽(6782)")
        code_match = re.search(r"\b([0-9]{4,6}[A-Za-z]?)\b", raw)
        if code_match:
            candidate_code = code_match.group(1).upper()
            if candidate_code in self.code_to_name:
                return candidate_code, self.code_to_name[candidate_code]
            # 嘗試即時更新官方表或探測 MoneyDJ
            self.refresh_from_official()
            if candidate_code in self.code_to_name:
                return candidate_code, self.code_to_name[candidate_code]
            probed = self._probe_moneydj_name(candidate_code)
            return candidate_code, (probed or candidate_code)

        # 2. 純代碼 (例如 "6782" 或 "0050")
        if re.match(r"^[0-9]{4,6}[A-Za-z]?$", raw):
            code = raw.upper()
            name = self.code_to_name.get(code)
            if not name:
                self.refresh_from_official()
                name = self.code_to_name.get(code) or self._probe_moneydj_name(code) or code
            return code, name

        # 3. 股票名稱精確比對 (例如 "視陽", "光寶科", "台積電")
        norm = _normalize_name(raw)
        if norm in self.name_to_code:
            code = self.name_to_code[norm]
            return code, self.code_to_name.get(code, raw)

        # 若快取未命中，重新同步一次官方清單再比對
        self.refresh_from_official()
        if norm in self.name_to_code:
            code = self.name_to_code[norm]
            return code, self.code_to_name.get(code, raw)

        # 4. 模糊前綴/包含比對 (僅在唯一匹配時採用，若有歧義則列出候選提醒)
        candidates = [
            (code, name)
            for code, name in self.code_to_name.items()
            if norm in _normalize_name(name) and len(code) == 4 and code.isdigit()
        ]
        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            cand_str = ", ".join([f"{c}({n})" for c, n in candidates[:5]])
            logger.warning(f"股票名稱「{raw}」匹配到多檔個股: {cand_str}，請輸入完整名稱或代碼！")
            return None

        logger.error(f"無法識別股票名稱或代碼：「{raw}」")
        return None

    def get_name(self, stock_id: str) -> str:
        res = self.resolve(stock_id)
        return res[1] if res else stock_id


def load_watchlist(
    watchlist_path: str = "watchlist.txt",
    mode: str = "both",
    resolver: Optional[StockResolver] = None,
) -> List[str]:
    """
    讀取 watchlist.txt 並根據執行模式 ('daily', 'weekly', 'both') 回傳對應的股票代碼清單 (保持順序且不重複)。

    支援語法：
    - 每行一檔，或以逗號 `,`、`、` 分隔多檔
    - 可寫股票名稱 (`視陽`)、股票代碼 (`6782`) 或兩者並列 (`6782 視陽`)
    - `#` 或 `//` 開頭(或行內 `#` 之後)為註解
    - 支援區段標題：`[daily]` / `[日報]`、`[weekly]` / `[週報]`、`[both]` / `[全部]`
    - 支援行尾標籤：`@daily` / `@日報`、`@weekly` / `@週報`
    """
    if resolver is None:
        resolver = StockResolver()

    if not os.path.exists(watchlist_path):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        alt = os.path.join(base_dir, watchlist_path)
        if os.path.exists(alt):
            watchlist_path = alt
        else:
            logger.warning(f"找不到自選股清單檔案: {watchlist_path}")
            return []

    current_section = "both"  # 'both', 'daily', 'weekly'
    selected_codes: List[str] = []

    with open(watchlist_path, "r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            # 移除 # 或 // 註解
            line = re.split(r"#|//", raw_line, maxsplit=1)[0].strip()
            if not line:
                continue

            # 檢查是否為區段標題 [daily] / [weekly] / [both]
            sec_match = re.match(r"^\[(.+?)\]$", line)
            if sec_match:
                sec_name = sec_match.group(1).strip().lower()
                if sec_name in ("daily", "日報", "每日"):
                    current_section = "daily"
                elif sec_name in ("weekly", "週報", "周報", "週末"):
                    current_section = "weekly"
                else:
                    current_section = "both"
                continue

            # 檢查行尾是否有 @daily / @weekly 標籤
            line_mode = current_section
            if re.search(r"@(daily|日報)\b", line, flags=re.IGNORECASE):
                line_mode = "daily"
                line = re.sub(r"@(daily|日報)\b", "", line, flags=re.IGNORECASE).strip()
            elif re.search(r"@(weekly|週報|周報)\b", line, flags=re.IGNORECASE):
                line_mode = "weekly"
                line = re.sub(r"@(weekly|週報|周報)\b", "", line, flags=re.IGNORECASE).strip()
            elif re.search(r"@(both|全部)\b", line, flags=re.IGNORECASE):
                line_mode = "both"
                line = re.sub(r"@(both|全部)\b", "", line, flags=re.IGNORECASE).strip()

            # 判斷是否符合當前要求的 mode
            if mode == "daily" and line_mode == "weekly":
                continue
            if mode == "weekly" and line_mode == "daily":
                continue

            # 支援同一行以逗號或頓號分隔多檔股票
            items = [x.strip() for x in re.split(r"[,，、;；]+", line) if x.strip()]
            for item in items:
                resolved = resolver.resolve(item)
                if resolved:
                    code, name = resolved
                    if code not in selected_codes:
                        selected_codes.append(code)
                        logger.info(f"[Watchlist] 識別成功: 「{item}」 -> {code} ({name}) [模式: {line_mode}]")
                else:
                    logger.warning(f"[Watchlist 第 {line_no} 行] 無法識別股票: 「{item}」，已跳過")

    return selected_codes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sr = StockResolver()
    for test_q in ["視陽", "2301", "南亞", "3005 神基", "台積電"]:
        print(test_q, "->", sr.resolve(test_q))
