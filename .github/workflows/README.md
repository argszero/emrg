# GitHub Actions Secrets — 构建配置要求

> 本文件记录 `build-release.yml`（一键安装包流水线）所需的全部 GitHub Secrets。
> 配置位置：GitHub 仓库 → Settings → Secrets and variables → Actions。

## 必需 Secrets（macOS 代码签名 + 公证，rant 2026-08-06T10:06:55）

| Secret | 用途 | 说明 |
|---|---|---|
| `MACOS_SIGNING_P12_BASE64` | 签名证书包 | **必须包含私钥！** `base64 < 含私钥的证书.p12` 的结果 |
| `MACOS_SIGNING_P12_PASSWORD` | p12 导出密码 | 导出 p12 时设置的密码 |
| `MACOS_SIGNING_IDENTITY` | 签名身份名称 | `security find-identity -v -p codesigning` 输出的证书 CN，如 `Developer ID Application: ... (TEAMID)` |
| `APPLE_ID` | Apple ID（公证） | notarytool 使用的 Apple ID 邮箱 |
| `MACOS_NOTARY_APP_PASSWORD` | App 专用密码 | Apple ID → 登录与安全 → App 专用密码 |
| `MACOS_NOTARY_TEAM_ID` | Team ID | 开发者账号 Team ID |

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

**命令行导出方法**（推荐，精确防错——`-t identities` 保证含私钥；错误做法 `-t certs` 只导出证书链，即 v0.2.7 四次失败根因）：
```bash
# 用证书 CN 查询准确名称（本机已有有效 identity，无需重新申请证书）
security find-identity -v -p codesigning
# 导出含私钥 p12（会提示输入钥匙串密码 + 设置导出密码）
security export -k ~/Library/Keychains/login.keychain-db -t identities -f pkcs12 \
  -P '新导出密码' -o ~/Downloads/emrg-cert/signing-with-key.p12 \
  "Developer ID Application: <你的名字> (Y55RQ6LU24)"
# 用导出的 p12 更新 MACOS_SIGNING_P12_BASE64 和 MACOS_SIGNING_P12_PASSWORD（导出密码）
```

**验证 p12 含私钥**：
```bash
security import 你的证书.p12 -k /tmp/test.keychain -P 密码
# 输出 "1 identity imported." → ✅ 含私钥
# 输出 "N certificates imported."（无 identity 行）→ ❌ 不含私钥，重新导出
```

**更新 secret**：
```bash
base64 < 含私钥的证书.p12   # 输出结果整段复制为 MACOS_SIGNING_P12_BASE64
```

## ⚠️ 需要两种证书：Developer ID Application + Developer ID Installer（v0.2.7 第 7 次构建教训）

**现象**：第 7 次构建（#461 修复私钥校验后）在 `Sign pkg` 步骤报：
```
productsign: error: Could not find appropriate signing identity for "***".
An installer signing identity (not an application signing identity) is required for signing flat-style products.
```

**根因**：macOS 代码签名需要**两种不同证书**，缺一不可：
| 证书类型 | 用途 | 产物 |
|---|---|---|
| `Developer ID Application` | 签名 .app（electron-builder codesign） | GUI 应用本体 |
| `Developer ID Installer` | 签名 .pkg（productsign） | 安装包 |

p12 若只有 Application 证书：.app 签名成功，但 productsign 报 cryptic 错误——CI 已在 import 步骤加显式校验（#462），缺失时明确报错。

**获取 Installer 证书**（developer.apple.com → Certificates → + → Software → **Developer ID Installer**）：
1. 在开发者门户创建 Developer ID Installer 证书（与 Application 是两张不同的证书，需分别申请）
2. 下载 .cer 双击导入钥匙串
3. 导出 p12 时**同时勾选两张证书**（或分别导出后合并）——`security export -t identities` 会导出所有 identity

**验证 p12 含两种证书**：
```bash
security import 你的证书.p12 -k /tmp/test.keychain -P 密码
security find-certificate -c "Developer ID Installer" -a /tmp/test.keychain   # 必须能找到
security find-certificate -c "Developer ID Application" -a /tmp/test.keychain  # 必须能找到
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
