import sqlite3
import os
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

class ChipDatabase:
    def __init__(self, db_path: str = "data/chips.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_tables()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. 各週期量化匯總表 (儲存各日期、週期之 VWAP 成本與買賣總量)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_period_summary (
                trade_date       TEXT NOT NULL,
                stock_id         TEXT NOT NULL,
                window           INTEGER NOT NULL,
                avg_buy_cost     REAL NOT NULL,
                avg_sell_cost    REAL NOT NULL,
                total_buy_shares INTEGER NOT NULL,
                total_sell_shares INTEGER NOT NULL,
                PRIMARY KEY (trade_date, stock_id, window)
            );
            """)

            # 2. 各週期主力分點排行榜 (前 15 大買賣分點及其比重與券商代碼)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS period_broker_ranks (
                trade_date    TEXT NOT NULL,
                stock_id      TEXT NOT NULL,
                window        INTEGER NOT NULL,
                side          TEXT NOT NULL,
                rank          INTEGER NOT NULL,
                broker_name   TEXT NOT NULL,
                bhid          TEXT DEFAULT '',
                b_code        TEXT DEFAULT '',
                buy_shares    INTEGER DEFAULT 0,
                sell_shares   INTEGER DEFAULT 0,
                net_shares    INTEGER DEFAULT 0,
                ratio         REAL DEFAULT 0.0,
                PRIMARY KEY (trade_date, stock_id, window, side, broker_name)
            );
            """)

            # 自動升級舊資料庫結構 (補齊 bhid, b_code 欄位)
            cursor.execute("PRAGMA table_info(period_broker_ranks);")
            existing_cols = {row["name"] for row in cursor.fetchall()}
            if "bhid" not in existing_cols:
                cursor.execute("ALTER TABLE period_broker_ranks ADD COLUMN bhid TEXT DEFAULT '';")
            if "b_code" not in existing_cols:
                cursor.execute("ALTER TABLE period_broker_ranks ADD COLUMN b_code TEXT DEFAULT '';")

            # 3. 一年期歷史波段高手回測基因庫 (常駐追蹤過去一年高勝率賣頂/抄底分點)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS smart_broker_backtest (
                stock_id      TEXT NOT NULL,
                trade_date    TEXT NOT NULL,
                broker_name   TEXT NOT NULL,
                bhid          TEXT DEFAULT '',
                b_code        TEXT DEFAULT '',
                role_type     TEXT NOT NULL,
                score         REAL DEFAULT 0.0,
                metrics_json  TEXT NOT NULL,
                PRIMARY KEY (stock_id, broker_name)
            );
            """)

            # 4. AI 報告智慧快取表 (依 stock_id + trade_date + mode + data_hash 避免重複消耗 Gemini Token)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_report_cache (
                stock_id        TEXT NOT NULL,
                trade_date      TEXT NOT NULL,
                mode            TEXT NOT NULL,
                data_hash       TEXT NOT NULL,
                ai_source_label TEXT NOT NULL,
                ai_markdown     TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (stock_id, trade_date, mode)
            );
            """)

            # 5. 權威財經新聞快取表 (依 stock_id + trade_date + mode 避免同日重複爬取新聞)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_news_cache (
                stock_id        TEXT NOT NULL,
                trade_date      TEXT NOT NULL,
                mode            TEXT NOT NULL,
                news_json       TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (stock_id, trade_date, mode)
            );
            """)

            # 6. 關鍵大戶逐日增量交易帳本 (供 brokers/ Markdown 知識庫增量續寫，避免每次重抓整年歷史)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS broker_daily_ledger (
                stock_id        TEXT NOT NULL,
                broker_name     TEXT NOT NULL,
                trade_date      TEXT NOT NULL,
                bhid            TEXT DEFAULT '',
                b_code          TEXT DEFAULT '',
                buy_shares      INTEGER DEFAULT 0,
                sell_shares     INTEGER DEFAULT 0,
                net_shares      INTEGER DEFAULT 0,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (stock_id, broker_name, trade_date)
            );
            """)

            # 7. 關鍵大戶判讀立約與事後驗證紀錄表 (追蹤每次大戶訊號發出後 T+5/10/20 日實際股價表現以驗證邏輯準確度)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS broker_signal_verifications (
                stock_id          TEXT NOT NULL,
                broker_name       TEXT NOT NULL,
                signal_date       TEXT NOT NULL,
                signal_type       TEXT NOT NULL,
                role_label        TEXT NOT NULL,
                trigger_price     REAL DEFAULT 0.0,
                trigger_net_5d    INTEGER DEFAULT 0,
                trigger_net_1d    INTEGER DEFAULT 0,
                pct_20d           REAL DEFAULT 50.0,
                hypothesis_text   TEXT NOT NULL,
                status            TEXT NOT NULL,
                days_elapsed      INTEGER DEFAULT 0,
                t5_date           TEXT DEFAULT '',
                t5_price          REAL DEFAULT 0.0,
                t5_ret_pct        REAL DEFAULT 0.0,
                t10_date          TEXT DEFAULT '',
                t10_price         REAL DEFAULT 0.0,
                t10_ret_pct       REAL DEFAULT 0.0,
                latest_date       TEXT DEFAULT '',
                latest_price      REAL DEFAULT 0.0,
                latest_ret_pct    REAL DEFAULT 0.0,
                max_favorable_pct REAL DEFAULT 0.0,
                max_adverse_pct   REAL DEFAULT 0.0,
                verdict_code      TEXT DEFAULT 'PENDING',
                verdict_label     TEXT DEFAULT '⏳ 追蹤驗證中',
                verdict_note      TEXT DEFAULT '',
                updated_at        TEXT NOT NULL,
                PRIMARY KEY (stock_id, broker_name, signal_date, signal_type)
            );
            """)

            # 8. 認證代操鎖碼席位基因庫 (附帶 14~30 天 TTL 快取機制，降低重複多週期運算)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS discretionary_broker_registry (
                stock_id          TEXT NOT NULL,
                trade_date        TEXT NOT NULL,
                broker_name       TEXT NOT NULL,
                bhid              TEXT DEFAULT '',
                b_code            TEXT DEFAULT '',
                buy_purity        REAL DEFAULT 0.0,
                net_shares_20d    INTEGER DEFAULT 0,
                ratio_20d         REAL DEFAULT 0.0,
                vwap_buy_cost     REAL DEFAULT 0.0,
                status            TEXT DEFAULT 'CERTIFIED_LOCK',
                certified_at      TEXT NOT NULL,
                expires_at        TEXT NOT NULL,
                metrics_json      TEXT NOT NULL,
                PRIMARY KEY (stock_id, broker_name)
            );
            """)

            # 9. 集保股權分散表週快取 (以週為單位，週五夜間前自動 0 請求複用)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS shareholding_dispersion_cache (
                stock_id          TEXT NOT NULL,
                week_date         TEXT NOT NULL,
                dispersion_json   TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                PRIMARY KEY (stock_id, week_date)
            );
            """)

            conn.commit()

    def save_period_data(self, period_data: Dict[str, Any]):
        """寫入指定週期的量化彙總與前 15 大買賣分點"""
        if not period_data:
            return

        trade_date = period_data["trade_date"]
        stock_id = period_data["stock_id"]
        window = period_data["window"]

        summary_sql = """
        INSERT INTO stock_period_summary (
            trade_date, stock_id, window, avg_buy_cost, avg_sell_cost, total_buy_shares, total_sell_shares
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?
        ) ON CONFLICT(trade_date, stock_id, window) DO UPDATE SET
            avg_buy_cost = excluded.avg_buy_cost,
            avg_sell_cost = excluded.avg_sell_cost,
            total_buy_shares = excluded.total_buy_shares,
            total_sell_shares = excluded.total_sell_shares;
        """

        rank_sql = """
        INSERT INTO period_broker_ranks (
            trade_date, stock_id, window, side, rank, broker_name, bhid, b_code, buy_shares, sell_shares, net_shares, ratio
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        ) ON CONFLICT(trade_date, stock_id, window, side, broker_name) DO UPDATE SET
            rank = excluded.rank,
            bhid = CASE WHEN excluded.bhid != '' THEN excluded.bhid ELSE period_broker_ranks.bhid END,
            b_code = CASE WHEN excluded.b_code != '' THEN excluded.b_code ELSE period_broker_ranks.b_code END,
            buy_shares = excluded.buy_shares,
            sell_shares = excluded.sell_shares,
            net_shares = excluded.net_shares,
            ratio = excluded.ratio;
        """

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(summary_sql, (
                trade_date, stock_id, window,
                period_data["avg_buy_cost"], period_data["avg_sell_cost"],
                period_data["total_buy_shares"], period_data["total_sell_shares"]
            ))

            rank_rows = []
            for idx, b in enumerate(period_data.get("top_buyers", []), 1):
                rank_rows.append((
                    trade_date, stock_id, window, "BUY", idx,
                    b["broker_name"], b.get("bhid", ""), b.get("b_code", ""),
                    b["buy_shares"], b["sell_shares"], b["net_shares"], b["ratio"]
                ))
            for idx, s in enumerate(period_data.get("top_sellers", []), 1):
                rank_rows.append((
                    trade_date, stock_id, window, "SELL", idx,
                    s["broker_name"], s.get("bhid", ""), s.get("b_code", ""),
                    s["buy_shares"], s["sell_shares"], s["net_shares"], s["ratio"]
                ))

            if rank_rows:
                # 同日重跑 (例如盤中 --force 後再盤後執行) 時，先清除舊排名，避免已跌出榜外的分點殘留
                cursor.execute(
                    "DELETE FROM period_broker_ranks WHERE trade_date = ? AND stock_id = ? AND window = ?",
                    (trade_date, stock_id, window)
                )
                cursor.executemany(rank_sql, rank_rows)
            conn.commit()
        logger.info(f"已儲存股票 {stock_id} 【{window}日】 籌碼彙總與分點排名")

    def get_period_summary(self, stock_id: str, window: int, trade_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """讀取指定股票、週期、日期的彙總數據"""
        query = "SELECT * FROM stock_period_summary WHERE stock_id = ? AND window = ?"
        params = [stock_id, window]
        if trade_date:
            query += " AND trade_date = ?"
            params.append(trade_date)
        else:
            query += " ORDER BY trade_date DESC LIMIT 1"

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_period_ranks(self, stock_id: str, window: int, side: str = "BUY", trade_date: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """讀取指定週期的前 N 大買賣分點"""
        query = "SELECT * FROM period_broker_ranks WHERE stock_id = ? AND window = ? AND side = ?"
        params = [stock_id, window, side]
        if trade_date:
            query += " AND trade_date = ?"
            params.append(trade_date)
        else:
            sub = "(SELECT trade_date FROM period_broker_ranks WHERE stock_id = ? AND window = ? ORDER BY trade_date DESC LIMIT 1)"
            query += f" AND trade_date = {sub}"
            params.extend([stock_id, window])

        query += " ORDER BY rank ASC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_latest_window_status(self, stock_id: str) -> Dict[int, Dict[str, Any]]:
        """取得該股票各週期在 SQLite 中的最新日期與是否已具備 bhid 券商代碼"""
        sql = """
        SELECT s.window, MAX(s.trade_date) as latest_date,
               SUM(CASE WHEN r.bhid != '' THEN 1 ELSE 0 END) as bhid_cnt
        FROM stock_period_summary s
        LEFT JOIN period_broker_ranks r
          ON s.stock_id = r.stock_id AND s.window = r.window AND s.trade_date = r.trade_date
        WHERE s.stock_id = ?
        GROUP BY s.window
        """
        status: Dict[int, Dict[str, Any]] = {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (stock_id,))
            for r in cursor.fetchall():
                status[int(r["window"])] = {
                    "trade_date": r["latest_date"],
                    "has_bhid": (r["bhid_cnt"] or 0) > 0,
                }
        return status

    def get_known_broker_codes(self, stock_id: str) -> Dict[str, tuple]:
        """取得該股票已知的券商 (bhid, b_code) 映射表 (整合區間排行與逐日帳本)"""
        query = """
        SELECT broker_name, bhid, b_code
        FROM period_broker_ranks
        WHERE stock_id = ? AND bhid != '' AND b_code != ''
        GROUP BY broker_name
        UNION
        SELECT broker_name, bhid, b_code
        FROM broker_daily_ledger
        WHERE stock_id = ? AND bhid != '' AND b_code != ''
        GROUP BY broker_name
        """
        result = {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (stock_id, stock_id))
            for r in cursor.fetchall():
                result[r["broker_name"]] = (r["bhid"], r["b_code"])
        return result

    def save_smart_broker_backtest(self, stock_id: str, trade_date: str, records: List[Dict[str, Any]]):
        """儲存一年期歷史波段高手回測基因庫"""
        import json
        sql = """
        INSERT INTO smart_broker_backtest (
            stock_id, trade_date, broker_name, bhid, b_code, role_type, score, metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, broker_name) DO UPDATE SET
            trade_date = excluded.trade_date,
            bhid = excluded.bhid,
            b_code = excluded.b_code,
            role_type = excluded.role_type,
            score = excluded.score,
            metrics_json = excluded.metrics_json;
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM smart_broker_backtest WHERE stock_id = ?", (stock_id,))
            rows = []
            for rec in records:
                rows.append((
                    stock_id,
                    trade_date,
                    rec["broker_name"],
                    rec.get("bhid", ""),
                    rec.get("b_code", ""),
                    rec.get("role_type", "PEAK_SNIPER"),
                    float(rec.get("score", 0.0)),
                    json.dumps(rec, ensure_ascii=False)
                ))
            if rows:
                cursor.executemany(sql, rows)
            conn.commit()

    def get_smart_broker_backtest(self, stock_id: str) -> List[Dict[str, Any]]:
        """讀取該股已儲存的一年期歷史波段高手回測紀錄"""
        import json
        query = "SELECT * FROM smart_broker_backtest WHERE stock_id = ? ORDER BY score DESC"
        out = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (stock_id,))
            for r in cursor.fetchall():
                try:
                    data = json.loads(r["metrics_json"])
                    data["trade_date"] = r["trade_date"]
                    out.append(data)
                except Exception:
                    continue
        return out

    def get_ai_report_cache(
        self, stock_id: str, trade_date: str, mode: str, data_hash: str
    ) -> Optional[Dict[str, str]]:
        """若同股、同日、同模式且數據指紋 (data_hash) 完全相同，直接命中快取返回 AI 報告"""
        query = """
        SELECT ai_source_label, ai_markdown, created_at
        FROM ai_report_cache
        WHERE stock_id = ? AND trade_date = ? AND mode = ? AND data_hash = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (stock_id, trade_date, mode, data_hash))
            row = cursor.fetchone()
            return dict(row) if row else None

    def save_ai_report_cache(
        self,
        stock_id: str,
        trade_date: str,
        mode: str,
        data_hash: str,
        ai_source_label: str,
        ai_markdown: str,
    ):
        """儲存 AI 分析報告與數據指紋至 SQLite 快取"""
        from datetime import datetime
        sql = """
        INSERT INTO ai_report_cache (
            stock_id, trade_date, mode, data_hash, ai_source_label, ai_markdown, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, trade_date, mode) DO UPDATE SET
            data_hash = excluded.data_hash,
            ai_source_label = excluded.ai_source_label,
            ai_markdown = excluded.ai_markdown,
            created_at = excluded.created_at;
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                sql,
                (
                    stock_id,
                    trade_date,
                    mode,
                    data_hash,
                    ai_source_label,
                    ai_markdown,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()

    def get_news_cache(self, stock_id: str, trade_date: str, mode: str) -> Optional[List[Dict[str, Any]]]:
        """讀取已快取之當日/當週前五大權威財經新聞"""
        import json
        query = "SELECT news_json FROM stock_news_cache WHERE stock_id = ? AND trade_date = ? AND mode = ?"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (stock_id, trade_date, mode))
            row = cursor.fetchone()
            if row and row["news_json"]:
                try:
                    return json.loads(row["news_json"])
                except Exception:
                    return None
        return None

    def save_news_cache(self, stock_id: str, trade_date: str, mode: str, news_list: List[Dict[str, Any]]):
        """儲存當日/當週前五大權威財經新聞至 SQLite 快取"""
        import json
        from datetime import datetime
        sql = """
        INSERT INTO stock_news_cache (
            stock_id, trade_date, mode, news_json, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, trade_date, mode) DO UPDATE SET
            news_json = excluded.news_json,
            updated_at = excluded.updated_at;
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                sql,
                (
                    stock_id,
                    trade_date,
                    mode,
                    json.dumps(news_list, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()

    def get_broker_ledger_last_date(self, stock_id: str, broker_name: str) -> Optional[str]:
        """查詢指定個股與分點於增量帳本中已記錄的最新交易日"""
        query = """
        SELECT MAX(trade_date) AS last_date
        FROM broker_daily_ledger
        WHERE stock_id = ? AND broker_name = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (stock_id, broker_name))
            row = cursor.fetchone()
            return row["last_date"] if row and row["last_date"] else None

    def get_broker_daily_ledger_rows(
        self, stock_id: str, broker_name: str, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """讀取指定分點之逐日交易帳本"""
        conditions = ["stock_id = ?", "broker_name = ?"]
        params: List[Any] = [stock_id, broker_name]
        if start_date:
            conditions.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("trade_date <= ?")
            params.append(end_date)
        where_clause = " AND ".join(conditions)
        query = f"""
        SELECT trade_date AS date, bhid, b_code, buy_shares AS buy, sell_shares AS sell, net_shares AS net
        FROM broker_daily_ledger
        WHERE {where_clause}
        ORDER BY trade_date ASC
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def save_broker_daily_ledger_rows(
        self,
        stock_id: str,
        broker_name: str,
        bhid: str,
        b_code: str,
        trades: List[Dict[str, Any]],
    ):
        """增量寫入分點逐日交易帳本 (ON CONFLICT 自動更新)"""
        if not trades:
            return
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
        INSERT INTO broker_daily_ledger (
            stock_id, broker_name, trade_date, bhid, b_code, buy_shares, sell_shares, net_shares, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, broker_name, trade_date) DO UPDATE SET
            bhid = CASE WHEN excluded.bhid != '' THEN excluded.bhid ELSE broker_daily_ledger.bhid END,
            b_code = CASE WHEN excluded.b_code != '' THEN excluded.b_code ELSE broker_daily_ledger.b_code END,
            buy_shares = excluded.buy_shares,
            sell_shares = excluded.sell_shares,
            net_shares = excluded.net_shares,
            updated_at = excluded.updated_at;
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for t in trades:
                dt = t.get("date") or t.get("trade_date")
                if not dt:
                    continue
                b = int(t.get("buy", t.get("buy_shares", 0)))
                s = int(t.get("sell", t.get("sell_shares", 0)))
                n = int(t.get("net", t.get("net_shares", b - s)))
                cursor.execute(sql, (stock_id, broker_name, dt, bhid or "", b_code or "", b, s, n, now_str))
            conn.commit()

    def get_broker_signal_verifications(
        self, stock_id: str, broker_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """讀取分點判讀立約與事後驗證紀錄"""
        if broker_name:
            query = """
            SELECT * FROM broker_signal_verifications
            WHERE stock_id = ? AND broker_name = ?
            ORDER BY signal_date DESC
            """
            params = (stock_id, broker_name)
        else:
            query = """
            SELECT * FROM broker_signal_verifications
            WHERE stock_id = ?
            ORDER BY signal_date DESC, broker_name ASC
            """
            params = (stock_id,)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def save_broker_signal_verifications(
        self, records: List[Dict[str, Any]], replace_broker: bool = True
    ):
        """批次儲存或更新分點判讀立約與事後驗證結果"""
        if not records:
            return
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
        INSERT INTO broker_signal_verifications (
            stock_id, broker_name, signal_date, signal_type, role_label,
            trigger_price, trigger_net_5d, trigger_net_1d, pct_20d,
            hypothesis_text, status, days_elapsed,
            t5_date, t5_price, t5_ret_pct,
            t10_date, t10_price, t10_ret_pct,
            latest_date, latest_price, latest_ret_pct,
            max_favorable_pct, max_adverse_pct,
            verdict_code, verdict_label, verdict_note, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?
        )
        ON CONFLICT(stock_id, broker_name, signal_date, signal_type) DO UPDATE SET
            role_label = excluded.role_label,
            trigger_price = excluded.trigger_price,
            trigger_net_5d = excluded.trigger_net_5d,
            trigger_net_1d = excluded.trigger_net_1d,
            pct_20d = excluded.pct_20d,
            hypothesis_text = excluded.hypothesis_text,
            status = excluded.status,
            days_elapsed = excluded.days_elapsed,
            t5_date = excluded.t5_date,
            t5_price = excluded.t5_price,
            t5_ret_pct = excluded.t5_ret_pct,
            t10_date = excluded.t10_date,
            t10_price = excluded.t10_price,
            t10_ret_pct = excluded.t10_ret_pct,
            latest_date = excluded.latest_date,
            latest_price = excluded.latest_price,
            latest_ret_pct = excluded.latest_ret_pct,
            max_favorable_pct = excluded.max_favorable_pct,
            max_adverse_pct = excluded.max_adverse_pct,
            verdict_code = excluded.verdict_code,
            verdict_label = excluded.verdict_label,
            verdict_note = excluded.verdict_note,
            updated_at = excluded.updated_at;
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if replace_broker:
                pairs = {(r["stock_id"], r["broker_name"]) for r in records}
                for sid, bname in pairs:
                    cursor.execute(
                        "DELETE FROM broker_signal_verifications WHERE stock_id = ? AND broker_name = ?",
                        (sid, bname),
                    )
            for r in records:
                cursor.execute(
                    sql,
                    (
                        r["stock_id"],
                        r["broker_name"],
                        r["signal_date"],
                        r["signal_type"],
                        r.get("role_label", ""),
                        float(r.get("trigger_price", 0.0)),
                        int(r.get("trigger_net_5d", 0)),
                        int(r.get("trigger_net_1d", 0)),
                        float(r.get("pct_20d", 50.0)),
                        r.get("hypothesis_text", ""),
                        r.get("status", "PENDING"),
                        int(r.get("days_elapsed", 0)),
                        r.get("t5_date", ""),
                        float(r.get("t5_price", 0.0)),
                        float(r.get("t5_ret_pct", 0.0)),
                        r.get("t10_date", ""),
                        float(r.get("t10_price", 0.0)),
                        float(r.get("t10_ret_pct", 0.0)),
                        r.get("latest_date", ""),
                        float(r.get("latest_price", 0.0)),
                        float(r.get("latest_ret_pct", 0.0)),
                        float(r.get("max_favorable_pct", 0.0)),
                        float(r.get("max_adverse_pct", 0.0)),
                        r.get("verdict_code", "PENDING"),
                        r.get("verdict_label", "⏳ 追蹤驗證中"),
                        r.get("verdict_note", ""),
                        now_str,
                    ),
                )
            conn.commit()

    def save_certified_discretionary_brokers(
        self,
        stock_id: str,
        trade_date: str,
        records: List[Dict[str, Any]],
        ttl_days: int = 21,
    ):
        """
        儲存或更新該股之認證代操鎖碼席位清單 (附帶 14~30 天 TTL 機制)
        """
        import json
        from datetime import datetime, timedelta

        try:
            d_cert = datetime.strptime(trade_date, "%Y-%m-%d")
        except Exception:
            d_cert = datetime.now()
        cert_str = d_cert.strftime("%Y-%m-%d")
        exp_str = (d_cert + timedelta(days=ttl_days)).strftime("%Y-%m-%d")

        sql = """
        INSERT INTO discretionary_broker_registry (
            stock_id, trade_date, broker_name, bhid, b_code,
            buy_purity, net_shares_20d, ratio_20d, vwap_buy_cost,
            status, certified_at, expires_at, metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, broker_name) DO UPDATE SET
            trade_date = excluded.trade_date,
            bhid = excluded.bhid,
            b_code = excluded.b_code,
            buy_purity = excluded.buy_purity,
            net_shares_20d = excluded.net_shares_20d,
            ratio_20d = excluded.ratio_20d,
            vwap_buy_cost = excluded.vwap_buy_cost,
            status = excluded.status,
            certified_at = excluded.certified_at,
            expires_at = excluded.expires_at,
            metrics_json = excluded.metrics_json;
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for r in records:
                m_json = json.dumps(r, ensure_ascii=False)
                cursor.execute(
                    sql,
                    (
                        stock_id,
                        trade_date,
                        r["broker_name"],
                        r.get("bhid", ""),
                        r.get("b_code", ""),
                        float(r.get("buy_purity", 0.0)),
                        int(r.get("net_shares_20d", 0)),
                        float(r.get("ratio_20d", 0.0)),
                        float(r.get("vwap_buy_cost", 0.0)),
                        r.get("status", "CERTIFIED_LOCK"),
                        cert_str,
                        exp_str,
                        m_json,
                    ),
                )
            conn.commit()

    def get_certified_discretionary_brokers(
        self,
        stock_id: str,
        as_of_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        讀取該股認證代操鎖碼席位清單。
        若提供 as_of_date，自動過濾 expires_at >= as_of_date (仍在 TTL 有效期內)。
        """
        import json
        sql = "SELECT * FROM discretionary_broker_registry WHERE stock_id = ?"
        params: List[Any] = [stock_id]
        if as_of_date:
            sql += " AND expires_at >= ?"
            params.append(as_of_date)
        sql += " ORDER BY net_shares_20d DESC"

        out = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            for row in cursor.fetchall():
                try:
                    data = json.loads(row["metrics_json"])
                    data["stock_id"] = row["stock_id"]
                    data["trade_date"] = row["trade_date"]
                    data["broker_name"] = row["broker_name"]
                    data["bhid"] = row["bhid"]
                    data["b_code"] = row["b_code"]
                    data["buy_purity"] = row["buy_purity"]
                    data["net_shares_20d"] = row["net_shares_20d"]
                    data["ratio_20d"] = row["ratio_20d"]
                    data["vwap_buy_cost"] = row["vwap_buy_cost"]
                    data["status"] = row["status"]
                    data["certified_at"] = row["certified_at"]
                    data["expires_at"] = row["expires_at"]
                    out.append(data)
                except Exception as e:
                    logger.debug(f"解析代操席位 JSON 異常: {e}")
                    continue
        return out

    # ==========================================
    # 9. 集保股權分散表週快取 (以週為單位智慧過期)
    # ==========================================

    def get_dispersion_cache(self, stock_id: str, week_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """取得集保股權分散表快取資料 (若無指定 week_date 則取最新一筆)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if week_date:
                query = "SELECT dispersion_json FROM shareholding_dispersion_cache WHERE stock_id = ? AND week_date = ?"
                cursor.execute(query, (stock_id, week_date))
            else:
                query = "SELECT dispersion_json FROM shareholding_dispersion_cache WHERE stock_id = ? ORDER BY week_date DESC LIMIT 1"
                cursor.execute(query, (stock_id,))
            row = cursor.fetchone()
            if row and row["dispersion_json"]:
                try:
                    return json.loads(row["dispersion_json"])
                except Exception:
                    pass
        return None

    def save_dispersion_cache(self, stock_id: str, week_date: str, dispersion_data: Dict[str, Any]):
        """儲存集保股權分散表至週快取"""
        if not stock_id or not week_date or not dispersion_data:
            return
        sql = """
        INSERT INTO shareholding_dispersion_cache (
            stock_id, week_date, dispersion_json, updated_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(stock_id, week_date) DO UPDATE SET
            dispersion_json = excluded.dispersion_json,
            updated_at = excluded.updated_at;
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                sql,
                (
                    stock_id,
                    week_date,
                    json.dumps(dispersion_data, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()

    @staticmethod
    def get_expected_tdcc_release_date(current_dt: Optional[datetime] = None) -> str:
        """
        計算截至當前時間點，TDCC 官方集保股權分散表理論上應已公布之最新基準日 (每週五)。
        官方公布規律：每週五約 19:00~20:30 陸續更新當週數據。
        - 週一至週四：最新已公布為「上週五」
        - 週五 20:30 以前：最新已公布為「上週五」
        - 週五 20:30 以後、週六、週日：最新已公布為「本週五」
        """
        from datetime import timezone, timedelta, time as dtime
        tw_tz = timezone(timedelta(hours=8))
        if current_dt is None:
            now_dt = datetime.now(tz=tw_tz)
        elif current_dt.tzinfo is None:
            now_dt = current_dt.replace(tzinfo=tw_tz)
        else:
            now_dt = current_dt.astimezone(tw_tz)

        wd = now_dt.weekday()  # 0: Mon, 4: Fri, 5: Sat, 6: Sun
        if wd < 4:
            # 週一至週四：扣掉 (wd + 3) 天即為上週五
            target_date = (now_dt - timedelta(days=wd + 3)).date()
        elif wd == 4:
            # 週五：檢查是否已過 20:30
            if now_dt.time() < dtime(20, 30):
                target_date = (now_dt - timedelta(days=7)).date()
            else:
                target_date = now_dt.date()
        else:
            # 週六 (5) 或週日 (6)：本週五
            target_date = (now_dt - timedelta(days=wd - 4)).date()

        return target_date.strftime("%Y-%m-%d")

    def is_dispersion_cache_valid(self, stock_id: str, current_dt: Optional[datetime] = None) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        檢查個股之集保股權分散表快取是否依然有效。
        若快取中最新紀錄之 week_date 等於或晚於當前預期的最新基準日，即判定快取完全有效。
        回傳 (is_valid, cached_data)
        """
        expected_date = self.get_expected_tdcc_release_date(current_dt)
        cached = self.get_dispersion_cache(stock_id)
        if not cached:
            return False, None
        cached_date = str(cached.get("date", ""))
        if cached_date and cached_date >= expected_date:
            return True, cached
        return False, None




