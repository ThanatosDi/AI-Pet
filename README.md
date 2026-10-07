# AI Pet

置頂在桌面上的吉祥物，透過 Claude Code hooks 顯示 Claude Code 目前的狀態。支援 Windows / macOS / Linux（PySide6）。

| 狀態 | 觸發事件 | 吉祥物 |
|---|---|---|
| 待命 | `SessionStart`、閒置提醒 | 呼吸、眨眼 |
| 思考中 | `UserPromptSubmit`、`PostToolUse` | 左右晃、眼睛往上看 |
| 工作中 | `PreToolUse` | 彈跳，泡泡顯示工具與參數 |
| 需要你確認 | `PermissionRequest`、權限通知 | 舉手、抖動、紅框泡泡 |
| 完成 | `Stop` | 開心跳一下（20 秒後回到待命） |
| 整理記憶 | `PreCompact` | 壓扁 |
| 睡覺 | 待命超過 10 分鐘 / 沒有 session | Zzz |

多個 session 同時跑時，顯示最需要注意的那個（等待確認 > 工作 > 思考 > …），右上角藍色數字是 session 數。滑鼠移上去看全部 session，右鍵選單可清除紀錄或結束。左鍵拖曳移動，位置會記住。

### 自訂吉祥物

右鍵 →「設定」→「選擇吉祥物圖片…」，選好圖片後會跳出視窗，可以描述各狀態想要的動作（沒寫的狀態用預設動作），然後選：

- **用 Claude 畫成會動的吉祥物**：在背景執行 `claude -p`，讓 Claude Code 參考圖片重畫成分部件的向量圖（`mascot.svg`），並寫出各狀態的動畫設定（`rig.json`）。約 1–5 分鐘，會用到你的 Claude 訂閱額度；吉祥物底下會顯示進度，右鍵可以取消。產生的檔案會先經過驗證，有錯就讓 Claude 自己修，最多修兩次。
- **直接使用圖片**：不經過 Claude，直接顯示圖片（PNG、JPG、GIF 動畫等），靠泡泡、彈跳、透明度表現狀態。

用 Claude 畫好之後，右鍵 →「設定」→「修改動作並重新繪製…」可以改動作描述再畫一次。每次畫的造型都會保留，右鍵 →「設定」→「切換吉祥物」會列出全部（附縮圖，滑鼠移上去可看當時的動作描述），也能換回「預設吉祥物」或開啟資料夾。

App 只讀取 Claude 產生的 SVG 和 JSON，不會執行它產生的程式碼。執行 `claude -p` 時只開放 Read、Write、Edit 三個工具，工作目錄是 `~/.ai-pet/mascots/<時間>/`，這次執行也不會觸發吉祥物的 hook。

### 在 Claude Code 裡用 skill 繪製

專案內附 `draw-mascot` skill（`.claude/skills/draw-mascot/`）。在這個專案目錄開 Claude Code，輸入：

```
/draw-mascot D:/pictures/cat.png 工作中：敲鍵盤；完成：轉一圈
```

或直接說「幫我把這張圖做成吉祥物」。和右鍵選單的背景繪製相比，skill 會用 `ai-pet preview` 把各狀態渲染成圖片，讓 Claude 看過再修正，品質通常比較好，過程也看得到、可以隨時給意見。完成後會自動套用到執行中的吉祥物。

skill 用到的指令（也可以手動使用）：

| 指令 | 用途 |
|---|---|
| `uv run ai-pet spec` | 印出 `mascot.svg` / `rig.json` 格式規格 |
| `uv run ai-pet new <圖片> [--actions 描述]` | 建立新的吉祥物資料夾，印出路徑 |
| `uv run ai-pet validate <資料夾>` | 檢查格式，七個狀態都要定義 |
| `uv run ai-pet preview <資料夾>` | 把各狀態在不同時間點的樣子畫成 `preview.png` |
| `uv run ai-pet use <資料夾>` | 切換造型，執行中的吉祥物會自動套用 |

## 安裝

```bash
# 方式 A：裝成全域工具（推薦，hook 不依賴這個資料夾）
uv tool install .
ai-pet install      # 把 hooks 寫進 ~/.claude/settings.json（會先備份）
ai-pet              # 啟動吉祥物

# 方式 B：直接在專案裡跑
uv run ai-pet install
uv run ai-pet
```

`install` 寫入的 hook 指令是目前 Python 的絕對路徑加 `-m ai_pet.hook`，所以用哪種方式安裝，就要用同一種方式執行 `install`。改用另一種方式時，重跑一次 `install` 即可覆蓋舊的設定。只想裝在某個專案：`ai-pet install --settings <專案>/.claude/settings.json`。

移除：`ai-pet uninstall`。

## 打包成應用程式（PyInstaller）

```bash
uv sync
uv run pyinstaller packaging/ai_pet.spec --noconfirm
```

- Windows：產出 `dist/AI Pet/`，把整個資料夾壓縮後發佈，執行 `AI Pet.exe`。
- macOS：產出 `dist/AI Pet.app`，用 `ditto -c -k --keepParent "dist/AI Pet.app" AI-Pet.zip` 壓縮。App 不會出現在 Dock，只有桌面上的吉祥物。
- PyInstaller 不能跨平台打包。推送 `v*` 標籤或手動執行 GitHub Actions 的 `build` workflow，會在 Windows、macOS（Apple Silicon、Intel）各打包一份並上傳。

打包版的使用者不需要終端機：第一次啟動會詢問是否連接 Claude Code（寫入 hooks），之後也能從右鍵 →「設定」→「連接 Claude Code」切換。hook 指令會指向 App 本身（`"<App 路徑>" ai-pet-hook`），所以 **App 搬家後要重新連接**，選單會顯示「重新連接 Claude Code（原本的連接已失效）」。

注意事項：

- 沒有程式碼簽章。Windows 可能跳出 SmartScreen，選「其他資訊」→「仍要執行」。macOS 第一次要在 App 上按右鍵 →「打開」，或執行 `xattr -dr com.apple.quarantine "AI Pet.app"`。正式發佈建議用 Apple Developer ID 簽章並公證。
- 打包版的錯誤訊息寫在 `~/.ai-pet/ai-pet.log`。
- 用 onedir 而不是 onefile：onefile 每次啟動都要先解壓縮，hook 每次觸發都會多等一兩秒。

## 測試

```bash
uv run ai-pet set working "Bash: npm test"
uv run ai-pet set waiting
uv run ai-pet status
uv run ai-pet clear
```

## 資料位置

`~/.ai-pet/`（可用環境變數 `AI_PET_HOME` 覆蓋）：`sessions/*.json` 是各 session 狀態，`config.json` 存視窗位置和目前使用的吉祥物，`mascots/` 是 Claude 畫的吉祥物（原圖、動作描述、`mascot.svg`、`rig.json`）。

## 已知限制

- Linux Wayland：部分 compositor 不允許程式自己置頂或記住位置（GNOME 尤其如此），X11 正常。
- 按 Esc 中斷或遇到 API 錯誤時 Claude Code 不會觸發 `Stop`，吉祥物會讀對話紀錄偵測，約 2 秒後回到待命。
- 狀態不對時可以查 `~/.ai-pet/events.log`，每次 hook 觸發都會記一行（時間、session、事件、寫入的狀態）。
