# GitHub Actions Secrets — 构建配置要求

> 本文件记录 `build-release.yml`（一键安装包流水线）所需的全部 GitHub Secrets。
> 配置位置：GitHub 仓库 → Settings → Secrets and variables → Actions。

## 必需 Secrets（macOS 代码签名 + 公证，rant 2026-08-06T10:06:55）

| Secret | 用途 | 说明 |
|---|---|---|
| `MACOS_SIGNING_P12_BASE64` | 签名证书包（Application） | **必须包含私钥！** `base64 < 含私钥的证书.p12` 的结果 |
| `MACOS_SIGNING_P12_PASSWORD` | p12 导出密码 | 导出 p12 时设置的密码 |
| `MACOS_SIGNING_IDENTITY` | 签名身份名称（可选） | `.app` 签名用 `Developer ID Application: ... (TEAMID)`（electron-builder 从 p12 自动发现）；pkg 签名自动检测 `Developer ID Installer` 身份，无需填此值 |
| `MACOS_INSTALLER_IDENTITY` | Installer 身份（可选，双 p12 方案） | `Developer ID Installer: ... (TEAMID)`，Sign pkg 优先用此值 |
| `MACOS_INSTALLER_P12_BASE64` | Installer p12（可选，双 p12 方案） | 含 Developer ID Installer 证书+私钥的 p12 的 base64；配置后 Import step 会额外导入它 |
| `MACOS_INSTALLER_P12_PASSWORD` | Installer p12 密码（可选，双 p12 方案） | 导出 Installer p12 时设置的密码 |
| `APPLE_ID` | Apple ID（公证） | notarytool 使用的 Apple ID 邮箱 |
| `MACOS_NOTARY_APP_PASSWORD` | App 专用密码 | Apple ID → 登录与安全 → App 专用密码 |
| `MACOS_NOTARY_TEAM_ID` | Team ID | 开发者账号 Team ID |

**两种方案**（rant 2026-08-06T15:26 起支持）：
- **单 p12 方案**（默认）：只配 `MACOS_SIGNING_*`，p12 内含双证书（Application + Installer），`security export -t identities` 导出
- **双 p12 方案**（宿主已采用）：`MACOS_SIGNING_*` 含 Application，另配 `MACOS_INSTALLER_*` 含 Installer——CI Import step 分别导入两个 p12，Sign pkg 优先用 `MACOS_INSTALLER_IDENTITY`

## ⚠️ p12 必须包含两种证书（Application + Installer）

**`.app` 签名**需要 **Developer ID Application** 证书；**pkg 签名**（`productsign`）需要 **Developer ID Installer** 证书——两者是独立的证书类型，缺一不可：

- 只有 Application 证书 → `.app` 签名成功，但 `Sign pkg` 步骤报错 `An installer signing identity (not an application signing identity) is required`（实测 #462）
- 只有 Installer 证书 → electron-builder codesign `.app` 找不到 Application 身份 → 跳过签名 → 公证失败（#467 对称校验）
- **CI 早检**（#464/#467）：Import step 会校验**双证书 + 私钥**三者齐备，任一缺失即明确报错，构建立即失败（不等到 Sign pkg/公证）
- 两个证书都要在 Apple Developer 后台生成（Certificates → 分别创建两种类型），下载安装到钥匙串后**一并导出**到 p12（`security export -t identities` 会导出全部证书+私钥对）

**检查本机已有哪些身份**：
```bash
security find-identity -v -p codesigning   # 列出 Developer ID Application
security find-identity -v                  # 列出全部（含 Developer ID Installer）
```

## ⚠️ p12 必须包含私钥（v0.2.7 四次构建失败的教训）

**现象**：`Import signing certificate` 步骤报 `SecItemCopyMatching: The specified item could not be found in the keychain`（早期）或 CI 明确报错 `MACOS_SIGNING_P12_BASE64 未包含可签名私钥`（#456 后）。

**根因**：导出的 p12 只含证书链（`7 certificates imported`），不含私钥——`security import` 后 keychain 里 0 个可签名身份，`set-key-partition-list` 无法匹配任何私钥。

**正确导出方法**（macOS 钥匙串访问）：
1. 打开"钥匙串访问"（Keychain Access）
2. 找到签名证书（Developer ID Application: ...）
3. **右键证书 → 导出"..."**（⚠️ 必须右键证书本身，不是仅证书的 .cer）
4. 格式选 **"个人信息交换 (.p12)"**
5. **勾选"包含私钥"**（Export 对话框底部）
6. 设置导出密码 → 得到含私钥的 .p12

**⭐ 一键导出脚本（推荐，v0.2.7 九次失败教训固化，杜绝手敲命令出错）**：
```bash
# 前置：双证书（Application + Installer）都已导入本机钥匙串
bash packaging/export-signing-p12.sh
# 脚本自动完成：检查双证书 → 导出 p12（-t identities）→ 生成 b64 → 导入验证（与 CI 一致）
# 输出含更新 Secret 的指引（MACOS_SIGNING_P12_BASE64 / MACOS_SIGNING_P12_PASSWORD）
```

**命令行导出方法**（精确防错——`-t identities` 保证含私钥；错误做法 `-t certs` 只导出证书链，即 v0.2.7 四次失败根因）：
```bash
# 用证书 CN 查询准确名称（本机已有有效 identity，无需重新申请证书）
security find-identity -v -p codesigning
# 导出含私钥 p12（会提示输入钥匙串密码 + 设置导出密码）
# ⚠️ 不指定证书名称 = 导出全部身份（Application + Installer 一并包含）
security export -k ~/Library/Keychains/login.keychain-db -t identities -f pkcs12 \
  -P '新导出密码' -o ~/Downloads/emrg-cert/signing-with-key.p12
# 用导出的 p12 更新 MACOS_SIGNING_P12_BASE64 和 MACOS_SIGNING_P12_PASSWORD（导出密码）
```

**验证 p12 含私钥 + 双证书**（与 CI #467 检查对称，更新 Secret 前本地确认，避免一轮构建浪费）：
```bash
security import 你的证书.p12 -k /tmp/test.keychain -P 密码
# 输出 "1 identity imported." → ✅ 含私钥
# 输出 "N certificates imported."（无 identity 行）→ ❌ 不含私钥，重新导出
# 双证书齐备检查（与 CI Import step 完全一致；任一为空 = 缺证书，重新导出）
security find-certificate -c "Developer ID Application" -a /tmp/test.keychain 2>/dev/null  # 非空 ✅
security find-certificate -c "Developer ID Installer" -a /tmp/test.keychain 2>/dev/null   # 非空 ✅
```

**更新 secret**：
```bash
base64 < 含私钥的证书.p12   # 输出结果整段复制为 MACOS_SIGNING_P12_BASE64
```

## 降级行为

- **Secret 未配置**（空字符串）：对应步骤跳过，构建不失败（不签名、不公证）。
- **p12 不含私钥**（#456 起）：CI 明确报错 `::error::MACOS_SIGNING_P12_BASE64 未包含可签名私钥...`，构建失败——这是有意行为，防止静默产出未签名安装包。

## 触发构建

`build-release.yml` 在 **tag `v*` push** 时触发。修复代码合并后，移动 tag 即可重新构建：
```bash
git tag -f v0.2.7 <修复后commit> && git push --force origin v0.2.7
```
（仅当该 tag 的 release 尚未发布时安全；已发布则需 bump 版本号。）
