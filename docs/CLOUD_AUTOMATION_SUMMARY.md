# 台股籌碼戰情報告 — 雲端自動化系統成果總覽

> **建置完成日期**：2026-11-28  
> **GitHub 倉儲**：`https://github.com/JonathanCCKao/TWSE_Chip_Analysis`  
> **線上戰報網頁 (GitHub Pages)**：`https://jonathancckao.github.io/TWSE_Chip_Analysis/`

---

## 1. 核心架構與自動化排程

系統已全面遷移至 **GitHub Actions 雲端自動化執行**（免開電腦），排程如下（台北時間 UTC+8）：

| 任務類型 | 執行時間 (台北時間) | Cron 設定 (UTC) | 執行內容 |
| :--- | :--- | :--- | :--- |
| **每日盤後戰術快報** | 週一至週五 `17:30` | `30 9 * * 1-5` | 抓取當日分點買賣超、三大法人、K線與籌碼分析，產出 `daily_report` |
| **每週集保戰略週報** | **每週六 `10:00`** | `0 2 * * 6` | 待台灣集保結算所釋出當週最新股權分散表後執行，產出 `weekly_report` |

> **註**：亦支援隨時至 GitHub Actions 頁面手動觸發（`workflow_dispatch`），可自由選擇 `daily`、`weekly` 或 `both`，並可指定單一股票或全自選股（`watchlist.txt`）。

---

## 2. 四大整合功能成果

1. **自動化雲端運算與資料庫持久化**
   - Workflow：`.github/workflows/daily_chip_report.yml`
   - 每次分析完成後，自動將最新報告（`reports/`）、資料庫（`data/chips.db`）與主力分點畫像（`brokers/`）提交並推送回 GitHub `main` 分支。
   - 週六執行時會自動以最新交易日（如週五日期）作為比對基準，確保報告檔名、Google Drive 同步與 Email 寄送日期一致。

2. **GitHub Pages 一鍵線上閱讀（電腦 / 手機通用）**
   - 已啟用 GitHub Pages 靜態網頁託管（含 `.nojekyll`）。
   - 手機或電腦點擊網址即可直接在瀏覽器開啟標準 HTTPS 網頁，**完全免下載附件、不被預覽沙盒封鎖、互動 K 線圖與集保趨勢圖 100% 正常顯示**。
   - `src/reporter.py` 亦已加入 ECharts 雙 CDN 備援（`jsdelivr` + `cdnjs`）與沙盒環境友善提示。

3. **Gmail 自動寄送通知與報告**
   - 模組：`src/mailer.py`
   - 收件信箱：`iamjonathan.tw@gmail.com`
   - 郵件內容包含：
     - 每檔自選股的摘要卡片。
     - **「📱 一鍵線上看完整戰報（含互動 K 線圖）」** 藍色按鈕（直連 GitHub Pages）。
     - 夾帶完整 `.html` 報告作為離線備份附件（將信件轉寄給親友時，對方同樣可直接點開按鈕或看摘要）。

4. **Google Drive 雲端同步備份**
   - 模組：`src/gdrive_sync.py`
   - 採用個人帳號 **OAuth 2.0 Refresh Token**（已於 Google Cloud「目標對象」完成設定，不受 7 天過期限制）。
   - 自動同步每日/每週報告與 `chips.db` 至 Google Drive 資料夾 `台股籌碼戰情報告`（Folder ID：`1ab3Sd9Lh38dBe2_WtukJMXR50oS_lSK3`）。

---

## 3. 追蹤標的與安全管理

- **自選股清單**：維護於專案根目錄 `watchlist.txt`。
- **機密金鑰管理**：
  - 雲端執行所需金鑰（`GEMINI_API_KEY`、`GDRIVE_*`、`GMAIL_*`）全部加密存放於 **GitHub Secrets**，未包含於程式碼內。
  - 本機備份憑證存放於工作區外的 `Key_No_Release/` 目錄（已受 `.gitignore` 保護）。
