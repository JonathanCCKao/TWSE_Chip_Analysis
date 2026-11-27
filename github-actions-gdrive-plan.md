# GitHub Actions 雲端自動排程與 Google 雲端硬碟同步架構規劃

## Goal
建立全自動化無伺服器架構：每日盤後與週五夜間由 GitHub Actions 雲端喚醒執行台股籌碼分析，產出 HTML 互動戰報並透過 Google Drive API 自動同步備份至個人 Google 雲端硬碟，供手機與電腦隨時查閱。

## Tasks
- [ ] Task 1: 建立專案相依性清單 `requirements.txt` (包含爬蟲、量化、Gemini 與 Google Drive 官方 API 套件) → Verify: 在獨立 Python 環境中 `pip install -r requirements.txt` 成功
- [ ] Task 2: 開發 Google Drive 無人值守同步模組 `src/gdrive_sync.py` (支援服務帳戶 Service Account 鑑權、自動建立遠端年月目錄、上傳 HTML 戰報與 DB 備份) → Verify: 執行 `python src/gdrive_sync.py --dry-run` 格式與路徑檢查通過
- [ ] Task 3: 設計 GitHub Actions 工作流程 `.github/workflows/daily_chip_report.yml` (配置平日 17:30 與週五 22:00 Cron 排程、Secrets 金鑰注入、自動執行 run_agent.py 與 gdrive_sync.py) → Verify: YAML 語法驗證無誤，排程時間對應台北時間 (UTC+8) 精確無誤
- [ ] Task 4: 整合本機至 GitHub 私有儲存庫 (建立 `.gitignore` 白名單確保機密憑證與金鑰不外洩，設定 GitHub Secrets: `GEMINI_API_KEY`、`GDRIVE_SERVICE_ACCOUNT_KEY`、`GDRIVE_FOLDER_ID`) → Verify: `git status` 確認敏感檔案已安全排除
- [ ] Task 5: 驗證雲端手動觸發 (workflow_dispatch) 與 Google 雲端硬碟實際落地 → Verify: 在 GitHub 介面點擊「Run workflow」，確認 Actions 執行成功，且 Google 雲端硬碟目標資料夾出現最新 HTML 戰報與 DB

## Done When
- [ ] GitHub 倉庫可在平日 17:30 與週五 22:00 自動無人值守執行完整分析流程
- [ ] 產出的 HTML 報告自動上傳到 Google Drive 指定目錄，手機與電腦隨時可開啟查閱
- [ ] 本地電腦無需開機，所有雲端運算與儲存皆在 100% 免費額度內穩定運行

## Notes
- Google Drive API 採用 **GCP 服務帳戶 (Service Account)** 憑證，不依賴瀏覽器 OAuth 登入彈窗，完全適配 GitHub Actions 雲端無頭 (headless) 環境。
- 專案機密（Gemini Key、Google 憑證 JSON）一律使用 GitHub Repository Secrets 加密保護，安全零風險。
