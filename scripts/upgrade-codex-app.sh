#!/usr/bin/env bash

set -Eeuo pipefail

DMG_PATH="${1:-$HOME/Downloads/ChatGPT.dmg}"
TARGET_APP="/Applications/Codex.app"
MOUNT_POINT=""

cleanup() {
  if [[ -n "$MOUNT_POINT" ]] && mount | grep -Fq "on $MOUNT_POINT "; then
    hdiutil detach "$MOUNT_POINT" -quiet || true
  fi
}
trap cleanup EXIT

if [[ ! -f "$DMG_PATH" ]]; then
  printf '找不到安装包: %s\n' "$DMG_PATH" >&2
  exit 1
fi

version_of() {
  /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$1/Contents/Info.plist" 2>/dev/null || printf 'unknown'
}

printf '当前版本: %s\n' "$(version_of "$TARGET_APP")"
printf '挂载安装包: %s\n' "$DMG_PATH"

attach_plist="$(hdiutil attach "$DMG_PATH" -nobrowse -readonly -plist)"
MOUNT_POINT="$(printf '%s\n' "$attach_plist" | awk '/<key>mount-point<\/key>/{getline; gsub(/.*<string>|<\/string>.*/, ""); print; exit}')"
if [[ -z "$MOUNT_POINT" || ! -d "$MOUNT_POINT" ]]; then
  printf '无法找到安装包挂载目录。\n' >&2
  exit 1
fi

SOURCE_APP="$(find "$MOUNT_POINT" -maxdepth 1 -type d -name '*.app' -print -quit)"
if [[ -z "$SOURCE_APP" ]]; then
  printf '安装包中没有找到 .app。\n' >&2
  exit 1
fi

SOURCE_NAME="$(basename "$SOURCE_APP" .app)"
printf '发现应用: %s\n' "$SOURCE_NAME"

# Quit via Apple Events instead of killing all matching processes.
osascript -e 'tell application "ChatGPT" to quit' 2>/dev/null || true
osascript -e 'tell application "Codex" to quit' 2>/dev/null || true
sleep 2

printf '正在覆盖安装到 %s\n' "$TARGET_APP"
ditto "$SOURCE_APP" "$TARGET_APP"

printf '升级完成，新版本: %s\n' "$(version_of "$TARGET_APP")"
printf '本地会话数据未修改。\n'
