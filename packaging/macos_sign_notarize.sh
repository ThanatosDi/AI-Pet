#!/usr/bin/env bash
# 簽章、公證並 staple dist/AI Pet.app，產出可以直接發佈的 zip。
#
# 用法（在專案根目錄、先跑完 pyinstaller）：
#   AI_PET_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   NOTARY_PROFILE=ai-pet \
#   packaging/macos_sign_notarize.sh [輸出 zip，預設 dist/AI-Pet-macos.zip]
#
# 公證帳密二選一：
#   NOTARY_PROFILE                 先用 xcrun notarytool store-credentials 存進鑰匙圈的名稱（本機建議用這個）
#   NOTARY_KEY_PATH / NOTARY_KEY_ID / NOTARY_ISSUER
#                                  App Store Connect API 金鑰（CI 建議用這個）
set -euo pipefail

APP="dist/AI Pet.app"
OUT="${1:-dist/AI-Pet-macos.zip}"
ENT="packaging/entitlements.plist"
ID="${AI_PET_SIGN_IDENTITY:?請設定 AI_PET_SIGN_IDENTITY，可用 security find-identity -v -p codesigning 查詢}"

[ -d "$APP" ] || { echo "找不到 $APP，請先執行 pyinstaller"; exit 1; }

sign() {
  codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$ID" "$@"
}

echo "== 簽章：由內而外，先簽所有 Mach-O 檔案，再簽 framework，最後簽整個 App"
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    sign "$f"
  fi
done < <(find "$APP/Contents" -type f -print0)

# 深的 framework 先簽（路徑長的排前面）
while IFS= read -r fw; do
  sign "$fw"
done < <(find "$APP/Contents" -type d -name "*.framework" | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)

sign "$APP"

echo "== 驗證簽章"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "Authority|flags|TeamIdentifier"

echo "== 送交公證（通常幾分鐘）"
SUBMIT="$(mktemp -d)/submit.zip"
ditto -c -k --keepParent "$APP" "$SUBMIT"
if [ -n "${NOTARY_PROFILE:-}" ]; then
  AUTH=(--keychain-profile "$NOTARY_PROFILE")
else
  AUTH=(--key "${NOTARY_KEY_PATH:?}" --key-id "${NOTARY_KEY_ID:?}" --issuer "${NOTARY_ISSUER:?}")
fi
RESULT="$(xcrun notarytool submit "$SUBMIT" "${AUTH[@]}" --wait --output-format json)"
echo "$RESULT"
if ! echo "$RESULT" | grep -q '"status" *: *"Accepted"'; then
  SUB_ID="$(echo "$RESULT" | sed -n 's/.*"id" *: *"\([^"]*\)".*/\1/p' | head -1)"
  echo "== 公證未通過，Apple 的詳細紀錄："
  xcrun notarytool log "$SUB_ID" "${AUTH[@]}" || true
  exit 1
fi

echo "== Staple：把公證結果寫進 App，使用者離線也能通過檢查"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose=4 "$APP"

echo "== 打包"
rm -f "$OUT"
ditto -c -k --keepParent "$APP" "$OUT"
echo "完成：$OUT"
