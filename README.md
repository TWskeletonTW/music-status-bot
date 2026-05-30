# Music Status Bot (v1.2.1)

Windows 本機用的 Discord 音樂狀態與語音轉播機器人。它讀取 Windows 目前播放中的媒體資訊、從指定音訊輸入裝置抓取 PCM 音訊轉播到 Discord 語音頻道，並建立可自動更新的音樂狀態面板（含同步 / 完整歌詞，歌詞來源為 LRCLIB）。

> 這不是 Lavalink 點歌機器人，而是把「你電腦正在播放的東西」轉送到 Discord。依賴 Windows 媒體控制 API 與本機音訊裝置，**僅適合 Windows 本機環境**。

## 需求

- Windows 10 / 11
- Python 3.12（3.10+ 應該也可）
- 一個 Discord Bot 帳號與 Token
- 音訊輸入裝置（建議 VB-Audio Virtual Cable）

## 安裝

```bash
pip install -r requirements.txt
```

## 設定

1. 複製 `.env.example` 為 `.env`
2. 填入真實 `DISCORD_TOKEN`、`DISCORD_GUILD_ID`
3. 確認 `MEDIA_INPUT_NAME` 與你的音訊輸入裝置名稱一致

> ⚠️ `.env` 含真實 Token，已被 `.gitignore` 排除，**切勿上傳或外流**。若曾外流，請到 Discord Developer Portal 重新產生 Token。

## 啟動

```bash
python music_bot.py
```

`.env`、`panel_state.json`、`lyrics_overrides.json` 都會固定以 `music_bot.py` 所在資料夾為基準。

## 指令

| 指令 | 功能 |
|---|---|
| `/join` | 加入你所在的語音頻道並開始轉播本機音訊 |
| `/leave` | 離開語音頻道 |
| `/panel` | 建立可自動更新的音樂狀態面板 |
| `/status` | 查看目前連線與面板狀態 |
| `/play` `/pause` `/next` `/prev` | 控制本機播放器 |

各項 `.env` 設定的詳細說明都寫在 [`.env.example`](.env.example) 的註解裡。
