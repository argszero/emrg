#!/usr/bin/env bash
# EMRG — macOS 签名 p12 一键导出脚本
#
# 背景：v0.2.7 签名链 9 次构建失败，根因全是宿主侧 p12 导出问题：
#   - 用 -t certs 导出 → 只有证书链无私钥（4 次失败）
#   - 只导出 Application 证书 → 缺 Installer 证书，productsign 失败
#   - 双重 base64 编码 → Secret 值错误
# 本脚本把"检查双证书 → 导出 p12 → 生成 base64 → 导入验证"全流程固化，
# 宿主只需：1) 创建 Installer 证书（developer.apple.com） 2) 跑本脚本。
#
# 用法：
#   bash packaging/export-signing-p12.sh [输出目录] [导出密码]
#   默认输出目录 ~/Downloads/emrg-cert，默认密码 emrg-sign-2026
#
# 前置：Developer ID Application + Developer ID Installer 证书都已导入本机钥匙串。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$HOME/Downloads/emrg-cert}"
EXPORT_PASS="${2:-emrg-sign-2026}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
P12="$OUT_DIR/signing-with-key.p12"
B64="$OUT_DIR/signing-with-key.p12.b64"
TMP_KEYCHAIN="$(mktemp -d)/test.keychain"

echo "==> [1/5] 检查本机双证书（Application + Installer 缺一不可）"
APP_OK="$(security find-certificate -c "Developer ID Application" -a "$KEYCHAIN" 2>/dev/null)"
INST_OK="$(security find-certificate -c "Developer ID Installer" -a "$KEYCHAIN" 2>/dev/null)"
[ -n "$APP_OK" ] && echo "    ✅ Developer ID Application 证书存在" || { echo "    ❌ 缺少 Developer ID Application 证书！"; exit 1; }
[ -n "$INST_OK" ] && echo "    ✅ Developer ID Installer 证书存在" || { echo "    ❌ 缺少 Developer ID Installer 证书！请在 developer.apple.com → Certificates 创建后下载导入钥匙串，再重跑本脚本。"; exit 1; }

echo "==> [2/5] 导出含双证书+私钥的 p12（-t identities 保证含私钥，不再有 -t certs 坑）"
mkdir -p "$OUT_DIR"
security export -k "$KEYCHAIN" -t identities -f pkcs12 -P "$EXPORT_PASS" -o "$P12"
echo "    ✅ 已导出: $P12 ($(stat -f%z "$P12" 2>/dev/null || wc -c < "$P12") bytes)"

echo "==> [3/5] 生成 base64（供 GitHub Secret MACOS_SIGNING_P12_BASE64）"
base64 < "$P12" > "$B64"
echo "    ✅ 已生成: $B64 ($(wc -c < "$B64" | tr -d ' ') chars)"

echo "==> [4/5] 导入临时 keychain 验证（与 CI 完全一致的检查）"
security create-keychain -p 'ci-temp' "$TMP_KEYCHAIN" >/dev/null 2>&1
IMPORT_OUTPUT="$(security import "$P12" -k "$TMP_KEYCHAIN" -P "$EXPORT_PASS" 2>&1)"
echo "    $IMPORT_OUTPUT"
if [[ ! "$IMPORT_OUTPUT" =~ identit(y|ies)\ imported ]]; then
  echo "    ❌ p12 不含私钥（输出无 identity 行）——导出失败，请检查钥匙串权限后重试。"
  exit 1
fi
for CERT_NAME in "Developer ID Application" "Developer ID Installer"; do
  if [ -z "$(security find-certificate -c "$CERT_NAME" -a "$TMP_KEYCHAIN" 2>/dev/null)" ]; then
    echo "    ❌ p12 缺少 $CERT_NAME 证书——导出不完整，请重试。"
    exit 1
  fi
done
echo "    ✅ p12 含私钥 + 双证书齐备（与 CI #467 检查一致）"

echo "==> [5/5] 完成！更新 GitHub Secrets"
echo ""
echo "    1. MACOS_SIGNING_P12_BASE64  ← 以下内容（整段复制）:"
echo "       cat $B64"
echo "    2. MACOS_SIGNING_P12_PASSWORD ← $EXPORT_PASS"
echo "    3. 重打 tag 触发构建: git tag -f v0.2.7 <master commit> && git push --force origin v0.2.7"
echo ""
echo "    (b64 内容同时已写入 $B64)"
