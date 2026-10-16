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

            # 2. 各週期主力分點排行榜 (前 15 大買賣分點及其比重)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS period_broker_ranks (
                trade_date    TEXT NOT NULL,
                stock_id      TEXT NOT NULL,
                window        INTEGER NOT NULL,
                side          TEXT NOT NULL,
                rank          INTEGER NOT NULL,
                broker_name   TEXT NOT NULL,
                buy_shares    INTEGER DEFAULT 0,
                sell_shares   INTEGER DEFAULT 0,
                net_shares    INTEGER DEFAULT 0,
                ratio         REAL DEFAULT 0.0,
                PRIMARY KEY (trade_date, stock_id, window, side, broker_name)
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
            trade_date, stock_id, window, side, rank, broker_name, buy_shares, sell_shares, net_shares, ratio
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        ) ON CONFLICT(trade_date, stock_id, window, side, broker_name) DO UPDATE SET
            rank = excluded.rank,
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
                    b["broker_name"], b["buy_shares"], b["sell_shares"], b["net_shares"], b["ratio"]
                ))
            for idx, s in enumerate(period_data.get("top_sellers", []), 1):
                rank_rows.append((
                    trade_date, stock_id, window, "SELL", idx,
                    s["broker_name"], s["buy_shares"], s["sell_shares"], s["net_shares"], s["ratio"]
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
            sub = "(SELECT trade_date FROM period_broker_ranks WHERE stock_id = ? ORDER BY trade_date DESC LIMIT 1)"
            query += f" AND trade_date = {sub}"
            params.append(stock_id)

        query += " ORDER BY rank ASC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]
