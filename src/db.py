import sqlite3
import os
import logging
from typing import List, Dict, Any, Optional

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
        """取得該股票已知的券商 (bhid, b_code) 映射表"""
        query = """
        SELECT broker_name, bhid, b_code
        FROM period_broker_ranks
        WHERE stock_id = ? AND bhid != '' AND b_code != ''
        GROUP BY broker_name
        """
        result = {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (stock_id,))
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


