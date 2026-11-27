import sys
import json
import base64
import pathlib
import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from nacl import encoding, public

SCOPES = ['https://www.googleapis.com/auth/drive']

def main():
    secret_file = pathlib.Path(r'C:\Users\jonathancc_kao\Vibe_Coding\Key_No_Release\gdrive_client_secret.json')
    if not secret_file.exists():
        print(f"Error: {secret_file} not found.", flush=True)
        return

    print("啟動本機授權伺服器 (port: 8085)...", flush=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
    
    # Run local server
    creds = flow.run_local_server(
        port=8085,
        prompt='consent',
        access_type='offline',
        open_browser=True
    )

    if not creds or not creds.refresh_token:
        print("❌ 未取得 refresh_token", flush=True)
        return

    print("🎉 成功取得 Google 永久 Refresh Token！", flush=True)

    # Save user token locally in Key_No_Release
    token_save_path = pathlib.Path(r'C:\Users\jonathancc_kao\Vibe_Coding\Key_No_Release\gdrive_user_token.json')
    token_data = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }
    token_save_path.write_text(json.dumps(token_data, indent=2), encoding='utf-8')
    print(f"💾 本地憑證已備份至: {token_save_path}", flush=True)

    # Push to GitHub Secrets automatically
    token_file = pathlib.Path(r'C:\Users\jonathancc_kao\Vibe_Coding\Key_No_Release\git_token.txt')
    git_token = token_file.read_text(encoding='utf-8').strip()

    owner = 'JonathanCCKao'
    repo = 'TWSE_Chip_Analysis'

    headers = {
        'Authorization': f'Bearer {git_token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    res = requests.get(f'https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key', headers=headers)
    data = res.json()
    key_id = data['key_id']
    pub_key = data['key']
    public_key_obj = public.PublicKey(pub_key.encode('utf-8'), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key_obj)

    secrets_to_set = {
        'GDRIVE_REFRESH_TOKEN': creds.refresh_token,
        'GDRIVE_CLIENT_ID': creds.client_id,
        'GDRIVE_CLIENT_SECRET': creds.client_secret
    }

    for name, val in secrets_to_set.items():
        enc = sealed_box.encrypt(val.encode('utf-8'))
        enc_b64 = base64.b64encode(enc).decode('utf-8')
        put_res = requests.put(
            f'https://api.github.com/repos/{owner}/{repo}/actions/secrets/{name}',
            headers=headers,
            json={'encrypted_value': enc_b64, 'key_id': key_id}
        )
        print(f"🔒 設定 GitHub Secret [{name}] 狀態: {put_res.status_code}", flush=True)

    print("✨ 全部 Google Drive 雲端金鑰已全自動填入 GitHub Secrets！", flush=True)

if __name__ == '__main__':
    main()
