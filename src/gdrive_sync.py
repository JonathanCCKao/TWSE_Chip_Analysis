"""
Google Drive Cloud Synchronization Module for Chip Analysis System
==================================================================
Supports headless automated file uploads and backups to Google Drive using
Google Cloud Service Account credentials in GitHub Actions / cloud runners.

Environment Variables:
- GDRIVE_SERVICE_ACCOUNT_KEY: JSON string or path to service account credentials.
- GDRIVE_FOLDER_ID: The root target folder ID in Google Drive shared with the service account.
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("GDriveSync")

# Google Client Library imports
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False


class GoogleDriveSync:
    """Manages headless file upload and folder structure in Google Drive."""

    SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive']

    def __init__(self, service_account_key: Optional[str] = None, root_folder_id: Optional[str] = None, dry_run: bool = False):
        self.dry_run = dry_run
        self.root_folder_id = root_folder_id or os.environ.get("GDRIVE_FOLDER_ID")
        self.service_account_key = service_account_key or os.environ.get("GDRIVE_SERVICE_ACCOUNT_KEY")
        self.service = None
        self._folder_cache: Dict[str, str] = {}  # "parent_id/folder_name" -> folder_id

        if not self.dry_run:
            self._init_service()

    def _init_service(self):
        if not GDRIVE_AVAILABLE:
            raise ImportError(
                "google-api-python-client or google-auth is not installed. "
                "Please run: pip install google-api-python-client google-auth"
            )

        if not self.service_account_key:
            # Check local file fallback
            local_candidates = [
                Path("service_account.json"),
                Path(".secrets/service_account.json"),
                Path("../service_account.json")
            ]
            for cand in local_candidates:
                if cand.exists():
                    self.service_account_key = str(cand)
                    break

        if not self.service_account_key:
            logger.warning("⚠️ 未檢測到 Google Service Account 金鑰 (GDRIVE_SERVICE_ACCOUNT_KEY)，進入 Dry-run 模擬模式")
            self.dry_run = True
            return

        try:
            # Check if service_account_key is a valid JSON string or a file path
            if self.service_account_key.strip().startswith("{"):
                info = json.loads(self.service_account_key)
                creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
            else:
                key_path = Path(self.service_account_key)
                if not key_path.exists():
                    raise FileNotFoundError(f"Service account file not found: {self.service_account_key}")
                creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=self.SCOPES)

            self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
            logger.info("✅ 成功連線 Google Drive API (服務帳戶鑑權完成)")
        except Exception as e:
            logger.error(f"❌ 初始化 Google Drive 服務失敗: {e}")
            raise

    def get_or_create_folder(self, folder_name: str, parent_id: Optional[str] = None) -> Optional[str]:
        """Gets or creates a subfolder under parent_id in Google Drive."""
        pid = parent_id or self.root_folder_id
        cache_key = f"{pid}/{folder_name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        if self.dry_run:
            logger.info(f"[DRY-RUN] 確保雲端資料夾存在: '{folder_name}' (父資料夾 ID: {pid})")
            folder_id = f"mock_folder_{folder_name}"
            self._folder_cache[cache_key] = folder_id
            return folder_id

        # Query existing folder
        query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        if pid:
            query += f" and '{pid}' in parents"

        try:
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                pageSize=5
            ).execute()
            files = results.get('files', [])

            if files:
                folder_id = files[0]['id']
                self._folder_cache[cache_key] = folder_id
                return folder_id

            # Create new folder
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            if pid:
                file_metadata['parents'] = [pid]

            folder = self.service.files().create(body=file_metadata, fields='id').execute()
            folder_id = folder.get('id')
            logger.info(f"📁 於 Google Drive 建立新資料夾: '{folder_name}' (ID: {folder_id})")
            self._folder_cache[cache_key] = folder_id
            return folder_id
        except Exception as e:
            logger.error(f"❌ 建立/搜尋資料夾 '{folder_name}' 失敗: {e}")
            raise

    def upload_file(self, local_path: Path, parent_folder_id: Optional[str] = None, mime_type: Optional[str] = None) -> Optional[str]:
        """Uploads or updates a file in the specified Google Drive folder."""
        if not local_path.exists():
            logger.error(f"❌ 本地檔案不存在: {local_path}")
            return None

        pid = parent_folder_id or self.root_folder_id
        file_name = local_path.name

        if self.dry_run:
            logger.info(f"[DRY-RUN] 上傳檔案 '{file_name}' ({local_path.stat().st_size:,} bytes) 至資料夾 {pid}")
            return f"mock_file_{file_name}"

        # Determine MIME type
        if not mime_type:
            if file_name.endswith('.html'):
                mime_type = 'text/html'
            elif file_name.endswith('.db') or file_name.endswith('.sqlite'):
                mime_type = 'application/octet-stream'
            elif file_name.endswith('.json'):
                mime_type = 'application/json'
            else:
                mime_type = 'application/octet-stream'

        try:
            # Check if file already exists in target parent
            query = f"name = '{file_name}' and trashed = false"
            if pid:
                query += f" and '{pid}' in parents"

            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                pageSize=5
            ).execute()
            files = results.get('files', [])

            media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

            if files:
                # Update existing file
                existing_id = files[0]['id']
                updated = self.service.files().update(
                    fileId=existing_id,
                    media_body=media,
                    fields='id, name, webViewLink'
                ).execute()
                logger.info(f"🔄 [覆蓋更新成功] {file_name} -> ID: {updated.get('id')}")
                return updated.get('id')
            else:
                # Create new file
                metadata = {'name': file_name}
                if pid:
                    metadata['parents'] = [pid]
                created = self.service.files().create(
                    body=metadata,
                    media_body=media,
                    fields='id, name, webViewLink'
                ).execute()
                logger.info(f"📤 [上傳新檔成功] {file_name} -> ID: {created.get('id')}")
                return created.get('id')
        except Exception as e:
            logger.error(f"❌ 上傳檔案 '{file_name}' 失敗: {e}")
            raise

    def sync_daily_reports(self, target_date_str: Optional[str] = None, stock_id: Optional[str] = None) -> List[str]:
        """Scans reports directory and syncs matching reports to Google Drive under YYYY-MM folder structure."""
        reports_dir = Path("reports")
        if not reports_dir.exists():
            logger.warning("⚠️ 本地 reports 資料夾不存在")
            return []

        uploaded_ids = []
        # Find HTML report files
        query_pattern = f"*{target_date_str}*.html" if target_date_str else "*.html"

        for stock_folder in reports_dir.iterdir():
            if not stock_folder.is_dir():
                continue
            if stock_id and stock_folder.name != stock_id:
                continue

            for month_folder in stock_folder.iterdir():
                if not month_folder.is_dir():
                    continue

                for html_file in month_folder.glob(query_pattern):
                    # Destination in Drive: <root>/reports/<stock_id>/<YYYY-MM>/
                    reports_root_id = self.get_or_create_folder("reports")
                    stock_gdir_id = self.get_or_create_folder(stock_folder.name, parent_id=reports_root_id)
                    month_gdir_id = self.get_or_create_folder(month_folder.name, parent_id=stock_gdir_id)

                    fid = self.upload_file(html_file, parent_folder_id=month_gdir_id, mime_type='text/html')
                    if fid:
                        uploaded_ids.append(fid)

        logger.info(f"🎉 報告同步完成，共上傳/更新 {len(uploaded_ids)} 份戰情報告")
        return uploaded_ids

    def backup_database(self, db_path: Path = Path("data/chips.db")) -> Optional[str]:
        """Backs up the SQLite database file to Google Drive under 'db_backups' folder."""
        if not db_path.exists():
            logger.warning(f"⚠️ 資料庫檔案不存在: {db_path}")
            return None

        backup_root_id = self.get_or_create_folder("db_backups")
        today_str = datetime.now().strftime("%Y%m%d")
        
        # Upload current latest db
        fid = self.upload_file(db_path, parent_folder_id=backup_root_id)
        logger.info(f"💾 資料庫主檔同步完成: {db_path.name}")
        return fid


def main():
    parser = argparse.ArgumentParser(description="Google Drive Sync for TWSE Chip Reports")
    parser.add_argument("--sync-reports", action="store_true", help="Sync latest HTML reports")
    parser.add_argument("--backup-db", action="store_true", help="Backup SQLite chips.db")
    parser.add_argument("--date", type=str, help="Target report date filter (YYYYMMDD)")
    parser.add_argument("--stock", type=str, help="Filter specific stock ID")
    parser.add_argument("--folder-id", type=str, help="Google Drive Root Folder ID")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without actual API calls")
    args = parser.parse_args()

    sync = GoogleDriveSync(root_folder_id=args.folder_id, dry_run=args.dry_run)

    if args.sync_reports or not (args.sync_reports or args.backup_db):
        sync.sync_daily_reports(target_date_str=args.date, stock_id=args.stock)

    if args.backup_db or not (args.sync_reports or args.backup_db):
        sync.backup_database()


if __name__ == "__main__":
    main()
