---
name: live2d-motion
description: 替 AI Pet 的 Live2D 模型（含 VTube Studio 模型）產生或修改各狀態的動作檔 aipet.json：先看模型每個參數的實際效果，再依使用者描述寫動作、驗證、預覽檢查後套用。使用者想讓 Live2D 吉祥物做特定動作、調整 Live2D 動作、或替新放進 Live2DModels 的模型設定動作時使用。
argument-hint: [模型名稱或路徑] [各狀態想要的動作]
allowed-tools: Read, Write, Edit, Bash(uv run ai-pet live2d:*), Bash(*AI Pet* live2d *)
---

# 產生 Live2D 動作檔

使用者輸入：$ARGUMENTS

## 執行指令的方式

下面步驟裡的 `ai-pet …` 依環境換成以下其中一種，整個流程固定用同一種：

- **原始碼專案**：目前目錄有 AI Pet 的 `pyproject.toml`，而且有 `uv` → 在專案根目錄執行 `uv run ai-pet …`。若指令回報 live2d-py 無法載入，請使用者先執行 `uv sync --extra live2d`。
- **打包版 App**：不在原始碼專案裡、或沒有 `uv` → 直接呼叫 App 的執行檔，參數相同：
  - Windows：`"<資料夾>/AI Pet/AI Pet.exe" …`
  - macOS：`"<位置>/AI Pet.app/Contents/MacOS/AI Pet" …`（不能用 `open`，它傳不了參數也拿不到輸出）

  執行檔位置：讀 `~/.claude/settings.json`，AI Pet 的 hook 指令是 `"<執行檔路徑>" ai-pet-hook`，引號裡就是路徑。找不到 hook、或路徑已不存在，就問使用者 App 放在哪裡。執行檔回報不認得子指令（`invalid choice`）代表 App 版本太舊，請使用者更新。

## 步驟

1. **找模型**：`ai-pet live2d list` 列出 `~/.ai-pet/Live2DModels` 裡的模型。使用者沒指定、或有多個符合時先問。之後的 `<模型>` 可以用清單裡的路徑或資料夾名稱。

2. **讀規格**：`ai-pet live2d spec`，完整讀完。`aipet.json` 必須完全照這份規格寫。

3. **了解模型**：
   - `ai-pet live2d info <模型>`：所有參數的 ID、作者取的名稱、群組、範圍、預設值。
   - `ai-pet live2d sheet <模型>`：輸出參數對照圖的路徑，用 Read 看圖。每個參數左邊是最小值、右邊是最大值。名稱可能不準，以圖為準。特別留意需要開關才看得到的參數（例如手部要 `…Found = 1`）。

4. **看現有動作**：模型資料夾（`.model3.json` 所在的資料夾）裡如果已經有 `aipet.json`，先讀它，裡面的 `request` 是上次的需求。修改時以它為起點，只改使用者要求的部分。覆寫之前，把原內容寫到同資料夾的 `aipet.json.bak`。

5. **寫動作**：在模型資料夾寫 `aipet.json`。
   - 七個狀態都要定義；使用者有描述的狀態照描述做，沒提到的照規格裡的預設行為。
   - `request` 欄位放使用者的原始描述。
   - 不要修改模型資料夾裡的其他檔案。

6. **驗證**：`ai-pet live2d validate <模型>`，沒印出 `OK` 就依錯誤修正。

7. **預覽檢查**：`ai-pet live2d preview <模型>`，用 Read 看輸出的圖。每一列是一個狀態，每一欄是不同時間點。逐項確認：
   - 每個狀態的動作有沒有做出使用者要的效果，相鄰時間點之間看得出在動
   - 動作方向對不對（抬頭是不是真的往上、舉的是不是那隻手）
   - 有沒有穿幫：手腳扭曲、部件錯位、超出格子
   - 睡覺時眼睛閉著、需要確認時眼睛張開，眨眼設定合理
   - 動作幅度在約 140 px 的大小下看得出來，但不會誇張到讓人分心

   有問題就修改後重新驗證、預覽，最多三輪。無法解決的問題如實告訴使用者。

8. **套用**：`ai-pet live2d use <模型>`。執行中的吉祥物會自動換成這個模型；動作檔被修改後一秒內也會自動重新載入，不必重開。

9. **回報**：說明各狀態做了什麼動作、動作檔位置，以及之後可以從吉祥物右鍵 →「設定」→「設定 Live2D 動作…」再修改，或選「還原成內建動作」。
