import os
import sys
import datetime
import argparse
import logging
from pathlib import Path
import yfinance as yf

# 將專案根目錄加入 sys.path 並切換工作目錄 (確保 Windows 工作排程器無論從何處觸發均正確載入)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

# 確保輸出編碼正確 (避免 pythonw 無終端時 NoneType 錯誤)
if sys.platform == "win32":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from run_agent import run

# 建立日誌紀錄
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

_handlers = [logging.FileHandler(LOG_DIR / "scheduler_activity.log", encoding="utf-8")]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))

# force=True：run_agent 於 import 時已呼叫過 basicConfig，若不強制覆蓋，FileHandler 會被忽略導致日誌永遠空白
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
    force=True
)
logger = logging.getLogger("SchedulerRunner")

# 截止日期與檢討提醒窗口
EXPIRY_DATE = datetime.date(2026, 12, 31)
REVIEW_START = datetime.date(2026, 12, 28)
REVIEW_END = datetime.date(2026, 12, 29)

def check_deadline_and_reminder(today: datetime.date) -> bool:
    """
    檢查是否超過設定的 2026/12/31 截止期限，並在 12/28~29 發出檢討提醒
    回傳 True 代表可繼續執行，False 代表已到期停止
    """
    # 1. 截止期限檢查
    if today > EXPIRY_DATE:
        logger.warning(
            f"⛔ [排程終止] 今日日期 ({today}) 已超過設定期限 {EXPIRY_DATE}。\n"
            "自動化排程已停止運作，請與 Jonathan 討論並評估 2027 年度的自動化安排！"
        )
        return False

    # 2. 檢討提醒檢查 (2026/12/28 ~ 2026/12/29)
    # 註：彈窗由獨立排程 TWSE_Chip_2026_Review_Reminder 負責，此處僅寫入日誌，避免重複彈窗與產出 .md 檔
    if REVIEW_START <= today <= REVIEW_END:
        logger.info(
            f"🔔【2026 年度籌碼分析自動化 - 到期檢討提醒】今日為 {today}，自動化排程將於 2026/12/31 截止，"
            "請安排年度成果檢討並討論 2027 年的自動化排程。"
        )

    return True


def notify_failure(message: str):
    """排程失敗時以獨立背景進程跳出 Windows 視窗 (不阻塞本進程，避免卡住下一次排程)"""
    try:
        import subprocess
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw) if pyw.exists() else sys.executable
        code = (
            "import ctypes,sys; ctypes.windll.user32.MessageBoxW(0, sys.argv[1], "
            "'【分點籌碼分析】排程執行異常', 0x00000030)"
        )
        subprocess.Popen([exe, "-c", code, message], close_fds=True)
    except Exception as e:
        logger.debug(f"觸發失敗通知視窗略過: {e}")


def get_latest_trade_date() -> datetime.date | None:
    """以 MoneyDJ 1 日分點頁面的資料日期判定最新交易日 (與報告資料同源)，失敗時退回 yfinance"""
    try:
        from src.collector import fetch_latest_trade_date
        d = fetch_latest_trade_date("2301")
        if d:
            return datetime.date.fromisoformat(d)
    except Exception as e:
        logger.warning(f"以 MoneyDJ 取得最新交易日失敗: {e}")
    try:
        df = yf.Ticker("2301.TW").history(period="5d")
        if not df.empty:
            return df.index[-1].date()
    except Exception as e:
        logger.warning(f"以 yfinance 取得最新交易日失敗: {e}")
    return None


def is_market_open_today(today: datetime.date, mode: str) -> bool:
    """
    檢查台股今日是否開盤／交易。
    依賴標的股票最新交易日確認是否為國定假日、補假、天然災害休市日或週末。
    """
    # 週末直接略過
    if today.weekday() >= 5:  # 5=週六, 6=週日
        logger.info(f"🏖️ 今日 ({today}) 為週末 (星期{today.weekday()+1})，台股未開盤，略過執行。")
        return False

    latest = get_latest_trade_date()
    if latest is None:
        logger.warning("⚠️ 無法取得台股最新交易日，保守繼續執行。")
        return True

    logger.info(f"📅 今日日期: {today} | 台股最新交易日: {latest}")

    if mode in ("daily", "both"):
        # 日報模式：今天必須是交易日 (若為休市日/連假，最新交易日不會等於今天)
        if latest != today:
            logger.info(f"🏖️ 今日 ({today}) 為國定假日或休市日 (最新開盤日為 {latest})，略過產出。")
            return False
        return True

    # 週報模式：只要過去一週內有任何交易日即可彙整產出
    if (today - latest).days > 7:
        logger.info("🏖️ 過去一週內無交易紀錄 (可能為春節長假)，略過週報產出。")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="分點籌碼分析 - 自動化排程專用調度器")
    parser.add_argument("--mode", choices=["daily", "weekly", "both", "auto"], default="auto",
                        help="報告模式；auto = 週一至週四日報、週五日報+週報一次產出 (避免重複抓取)")
    parser.add_argument("--force", action="store_true", help="強制執行 (忽略開盤日與週末檢查)")
    args = parser.parse_args()

    today = datetime.date.today()
    mode = args.mode
    if mode == "auto":
        mode = "both" if today.weekday() == 4 else "daily"
    logger.info(f"=== 啟動籌碼分析自動化排程 (模式: {mode}, 日期: {today}) ===")

    # 1. 期限與提醒檢查
    if not check_deadline_and_reminder(today):
        sys.exit(0)

    # 2. 假日與開盤檢查 (除非帶有 --force)
    if not args.force:
        if not is_market_open_today(today, mode):
            logger.info("=== 本次排程依交易日規則跳過，順利結束 ===")
            sys.exit(0)

    # 3. 執行分析
    logger.info(f"🚀 開始執行 {mode} 分析流程...")
    try:
        results = run(stock_ids=None, mode=mode)
        problems = [f"{sid}: {'; '.join(r['warnings'])}" for sid, r in (results or {}).items() if r.get("warnings")]
        if problems:
            logger.warning("⚠️ 部分個股資料有異常：\n" + "\n".join(problems))
            notify_failure("今日報告已產出，但以下個股資料有異常：\n\n" + "\n".join(problems)
                           + "\n\n詳見 logs/scheduler_activity.log")
        logger.info(f"✅ {mode} 分析與報告產出完成！")
    except Exception as e:
        logger.exception(f"❌ 執行過程發生異常: {e}")
        notify_failure(f"排程執行失敗：{e}\n\n詳見 logs/scheduler_activity.log")
        sys.exit(1)

if __name__ == "__main__":
    main()

