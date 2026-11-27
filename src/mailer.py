"""
Automated Email Dispatcher for TWSE Chip Reports
===============================================
Sends rich HTML summary emails with full interactive HTML report attachments
using Gmail SMTP (App Password) for daily盘後 and weekly集保 reports.
"""

import os
import sys
import smtplib
import logging
import argparse
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("ChipMailer")


class ChipReportMailer:
    """Handles parsing report summaries, building responsive HTML emails, and sending via Gmail SMTP."""

    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 465

    def __init__(
        self,
        sender_email: Optional[str] = None,
        app_password: Optional[str] = None,
        recipient_email: Optional[str] = None
    ):
        self.sender_email = sender_email or os.environ.get("GMAIL_USER") or "iamjonathan.tw@gmail.com"
        self.app_password = app_password or os.environ.get("GMAIL_APP_PASSWORD")

        if not self.app_password:
            local_pass_cand = Path(r"C:\Users\jonathancc_kao\Vibe_Coding\Key_No_Release\gmail_app_password.txt")
            if local_pass_cand.exists():
                self.app_password = local_pass_cand.read_text(encoding='utf-8').strip()

        self.recipient_email = recipient_email or os.environ.get("GMAIL_RECIPIENT") or self.sender_email

    def _extract_report_summary(self, html_path: Path) -> Dict[str, Any]:
        """Extracts high-level summary cards from the generated HTML report."""
        summary = {
            "file_name": html_path.name,
            "stock_code": html_path.parent.parent.name,
            "title": html_path.name,
            "date": "",
            "mode": "daily",
            "close_price": "",
            "over_200_pct": "",
            "under_10_pct": "",
            "over_200_cnt": "",
            "under_10_cnt": "",
            "inst_net": "",
            "headline": ""
        }

        try:
            content = html_path.read_text(encoding='utf-8', errors='ignore')
            soup = BeautifulSoup(content, 'html.parser')

            # Title
            h1 = soup.find('h1')
            if h1:
                summary["title"] = h1.get_text(strip=True)

            # Mode
            if "weekly" in html_path.name.lower():
                summary["mode"] = "weekly"

            # Date
            badge = soup.find('span', class_='report-timestamp')
            if badge:
                summary["date"] = badge.get_text(strip=True).replace("🕒 產出時間：", "")

            # Look for dispersion stats
            for stat in soup.find_all('div', class_='stat-card'):
                text = stat.get_text(separator=' ', strip=True)
                if ">200" in text or "主力大戶" in text:
                    summary["over_200_pct"] = text
                elif "<10" in text or "散戶" in text:
                    summary["under_10_pct"] = text

            # Look for AI content teaser
            analysis = soup.find('div', class_='analysis-content')
            if analysis:
                paragraphs = analysis.find_all('p')
                for p in paragraphs:
                    p_text = p.get_text(strip=True)
                    if len(p_text) > 30 and not summary["headline"]:
                        summary["headline"] = p_text[:140] + "..."
                        break
        except Exception as e:
            logger.warning(f"解析報告 {html_path.name} 摘要失敗: {e}")

        return summary

    def build_email_content(self, target_date_str: str, report_files: List[Path], mode: str = "daily") -> tuple:
        """Constructs subject and HTML body for email."""
        is_weekly = (mode == "weekly")
        mode_label = "【集保大戶籌碼戰略週報】" if is_weekly else "【主力分點當日戰術快報】"
        date_formatted = f"{target_date_str[:4]}-{target_date_str[4:6]}-{target_date_str[6:]}" if len(target_date_str) == 8 else target_date_str

        stock_codes = sorted(list(set([p.parent.parent.name for p in report_files])))
        stock_list_str = "、".join(stock_codes)
        subject = f"{mode_label} {date_formatted} ｜ 自選股追蹤：{stock_list_str}"

        # Build Stock Cards HTML
        stock_cards_html = ""
        for p in report_files:
            item = self._extract_report_summary(p)
            stock_code = item["stock_code"]
            card_title = item["title"] or f"{stock_code} 籌碼戰情報告"
            teaser = item["headline"] or "已完成各週期主力鎖碼、大戶散戶人數、八大官股與代操席位數據整合分析。"

            stock_cards_html += f"""
            <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:10px; padding:18px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,0.04);">
                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #f1f5f9; padding-bottom:10px; margin-bottom:12px;">
                    <span style="font-size:16px; font-weight:bold; color:#0f172a;">{card_title}</span>
                    <span style="font-size:12px; background:#eff6ff; color:#2563eb; padding:3px 10px; border-radius:12px; font-weight:600;">代號 {stock_code}</span>
                </div>
                <div style="font-size:14px; color:#334155; line-height:1.6; margin-bottom:10px;">
                    {teaser}
                </div>
                <div style="font-size:12px; color:#64748b; background:#f8fafc; padding:8px 12px; border-radius:6px;">
                    📎 附件包含完整全互動戰報：<b>{p.name}</b> (點擊附件可直接在電腦或手機中以瀏覽器開啟縮放圖表)
                </div>
            </div>
            """

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color:#f1f5f9; margin:0; padding:24px 16px; color:#0f172a;">
            <div style="max-width:680px; margin:0 auto; background:#ffffff; border-radius:14px; overflow:hidden; border:1px solid #cbd5e1; box-shadow:0 4px 16px rgba(15,23,42,0.08);">
                <!-- Header Banner -->
                <div style="background:linear-gradient(135deg, #1e293b 0%, #0f172a 100%); padding:28px 24px; color:#ffffff;">
                    <div style="font-size:13px; text-transform:uppercase; letter-spacing:1px; color:#94a3b8; font-weight:700; margin-bottom:6px;">
                        TWSE Chip Strategy Intelligence
                    </div>
                    <div style="font-size:22px; font-weight:800; line-height:1.3;">
                        {mode_label}
                    </div>
                    <div style="font-size:14px; color:#cbd5e1; margin-top:8px;">
                        📅 資料基準日：{date_formatted} ｜ 追蹤個股數：{len(stock_codes)} 檔
                    </div>
                </div>

                <!-- Main Content -->
                <div style="padding:24px 20px; background:#f8fafc;">
                    <div style="font-size:15px; font-weight:600; color:#334155; margin-bottom:16px;">
                        📊 今日重點戰況快速瀏覽：
                    </div>

                    {stock_cards_html}

                    <!-- Footer Tip -->
                    <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:14px 16px; font-size:13px; color:#166534; line-height:1.5; margin-top:20px;">
                        💡 <b>溫馨提醒</b>：本郵件已隨信附上所有個股的 <b>.html 互動戰情報告</b>。在電腦上直接雙擊打開，即可全螢幕查閱 <b>ECharts 4 軸多尺度大戶人數趨勢圖、分點波段高手勝率評級與實戰操盤劇本</b>。
                    </div>
                </div>

                <!-- Footer -->
                <div style="background:#ffffff; border-top:1px solid #e2e8f0; padding:16px 20px; text-align:center; font-size:12px; color:#94a3b8;">
                    此郵件由 GitHub Actions 雲端台股籌碼分析系統自動產出並寄送 ｜ 專案架構：TWSE Chip Agent
                </div>
            </div>
        </body>
        </html>
        """

        return subject, html_body

    def send_reports_email(self, target_date_str: str, mode: str = "daily", stock_id: Optional[str] = None) -> bool:
        """Collects reports, composes email with attachments, and sends via Gmail SMTP."""
        if not self.app_password:
            logger.error("❌ 未設定 Gmail 應用程式密碼 (GMAIL_APP_PASSWORD)，無法發信")
            return False

        reports_dir = Path("reports")
        if not reports_dir.exists():
            logger.warning("⚠️ reports 資料夾不存在")
            return False

        # Gather target HTML files
        report_files: List[Path] = []
        prefix = "weekly_report" if mode == "weekly" else "daily_report"
        query_pattern = f"*{prefix}*{target_date_str}*.html"

        for stock_folder in reports_dir.iterdir():
            if not stock_folder.is_dir():
                continue
            if stock_id and stock_folder.name != stock_id:
                continue

            for month_folder in stock_folder.iterdir():
                if not month_folder.is_dir():
                    continue

                for f in month_folder.glob(query_pattern):
                    report_files.append(f)

        if not report_files:
            # Fallback search any report matching date
            fallback_pattern = f"*{target_date_str}*.html"
            for f in reports_dir.glob(f"*/*/{fallback_pattern}"):
                report_files.append(f)

        if not report_files:
            logger.warning(f"⚠️ 在 reports/ 查無符合日期 {target_date_str} (模式: {mode}) 的戰情報告檔案")
            return False

        logger.info(f"📬 準備寄送 {len(report_files)} 份戰情報告至: {self.recipient_email}...")

        subject, html_body = self.build_email_content(target_date_str, report_files, mode=mode)

        # Create MIME Message
        msg = MIMEMultipart("mixed")
        msg["From"] = f"台股籌碼戰情報告 <{self.sender_email}>"
        msg["To"] = self.recipient_email
        msg["Subject"] = subject

        # HTML Part
        msg_alternative = MIMEMultipart("alternative")
        msg_alternative.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(msg_alternative)

        # Attach HTML Report Files
        for f in report_files:
            try:
                part = MIMEApplication(f.read_bytes(), Name=f.name)
                part['Content-Disposition'] = f'attachment; filename="{f.name}"'
                msg.attach(part)
                logger.info(f"📎 已夾帶附件: {f.name} ({f.stat().st_size:,} bytes)")
            except Exception as e:
                logger.error(f"夾帶附件 {f.name} 失敗: {e}")

        # Send via SMTP_SSL
        try:
            logger.info(f"正在連線 Gmail SMTP 伺服器 ({self.SMTP_SERVER}:{self.SMTP_PORT})...")
            with smtplib.SMTP_SSL(self.SMTP_SERVER, self.SMTP_PORT) as server:
                server.login(self.sender_email, self.app_password)
                server.sendmail(self.sender_email, [self.recipient_email], msg.as_string())
            logger.info(f"🎉 成功寄出戰情報告郵件！收件者: {self.recipient_email}")
            return True
        except Exception as e:
            logger.error(f"❌ 寄送郵件失敗: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(description="Send TWSE Chip Reports via Gmail")
    parser.add_argument("--date", type=str, required=True, help="Target date (YYYYMMDD)")
    parser.add_argument("--mode", type=str, default="daily", choices=["daily", "weekly"], help="Report mode")
    parser.add_argument("--stock", type=str, help="Specific stock ID")
    parser.add_argument("--to", type=str, help="Recipient email override")
    args = parser.parse_args()

    mailer = ChipReportMailer(recipient_email=args.to)
    mailer.send_reports_email(target_date_str=args.date, mode=args.mode, stock_id=args.stock)


if __name__ == "__main__":
    main()
