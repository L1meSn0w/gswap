# gswap

**语言 / Language**: [简体中文](README_zh.md) | [English](README.md)

Google 多账号交互式切换器、隔离执行器与实时额度/算力看板，一套工具统一纳管四大 Google CLI 服务：

- **Google Antigravity CLI (`agy`)** — `ccswap` 风格交互菜单与免重登秒切账号、Token 文件 `0400` 只读权限保护（防止后台旧终端面板原地覆写刚切换的账号）、系统 Keyring 同步（Linux `secret-tool` / macOS `security` 钥匙串），以及 5 小时窗口与周限模型额度实时查询。
- **Google Colab (`colab-cli`)** — 多账号独立 ADC 凭据与会话状态隔离、实时查询 Compute Units (CU) 算力余额 / 消耗速率 / 活跃 GPU 会话，以及多账号训练池注册表导出。
- **Google Drive (`rclone`)** — 多账号远端自动映射，支持 `:path` 简写自动展开为对应账号的 `<remote>:path`。
- **Google Cloud SDK (`gcloud`)** — 多账号命名配置隔离、默认 Project 自动注入，以及全局默认 `active_config` 一键切换。
- **中英双语界面 (`zh` / `en`)** — 原生支持简体中文与英文（支持根据系统环境变量自动识别、在交互菜单内按 `[l]` 一键切换并持久化，或通过 `gswap lang zh|en` 切换）。

零第三方 Python 依赖（仅使用 Python 3.10+ 标准库）。

---

## 安装步骤

### 方式一：直接软链（推荐）

```bash
git clone https://github.com/L1meSn0w/gswap.git
cd gswap
chmod +x gswap.py
mkdir -p ~/.local/bin
ln -sf "$(pwd)/gswap.py" ~/.local/bin/gswap
```

### 方式二：使用 `pipx` 或 `uv tool`

```bash
uv tool install git+https://github.com/L1meSn0w/gswap.git
```

---

## 快速上手与使用步骤

### 1. 交互式菜单（直接输入 `gswap`）

在终端直接输入 `gswap` 即可进入交互式菜单，支持 **`↑` / `↓` 方向键（或 `j` / `k`）+ 回车** 选择，也支持直接按 **`1`–`6` / `l` / `q`** 快捷键：

```text
╭──────────────────────────────────────────────────────────────────────────╮
│  gswap — Google 多账号管理中心 (AGY · Colab · Drive · GCloud)            │
╰──────────────────────────────────────────────────────────────────────────╯
  [1] default    primary@example.com        (colab drive gcloud) [train-pool] [GCP激活]
      drive=gd-main: · gcloud=main-cfg(my-gcp-project-01)
* [2] second     alice@example.com          (agy colab drive gcloud) [train-pool] [AGY激活]
      agy=slot#1 · drive=gd-second: · gcloud=second(-)
  [3] bob        bob@example.com            (agy)
      agy=slot#2

请选择操作: (↑/↓ 选择, Enter 确认, 或直接按快捷键, q 退出)
 ❯ [1] ⚡ 快速轮换下一个 AGY 账号 (alice@example.com → bob@example.com)
   [2] 🔀 指定切换账号 (AGY / GCloud 全局默认账号)
   [3] 📊 查看实时额度与算力看板 (AGY 5h/周限 + Colab CU)
   [4] 🚀 Colab 算力与 GPU 会话查询 (usage / sessions)
   [5] 🔑 账号登录 / 绑定新服务 (agy · colab · drive · gcloud)
   [6] 🔄 同步 Colab 训练池配置 (sync-trainpool)
   [l] 🌐 语言 / Language: 简体中文 (切换为 English)
   [q] 退出 (Quit)
```

---

### 2. 命令行直达用法

所有菜单功能也可直接通过命令行参数调用：

```bash
# 查看所有 Google 账号与已绑定服务状态（秒出，不发起网络请求）
gswap status
gswap list

# 并发查询所有（或指定）账号的 Colab CU 算力余额 + Antigravity 5h/周限模型额度
gswap -q
gswap quota second

# 快速轮换到下一个 Antigravity CLI 账号
gswap next

# 切换指定账号为全局默认账号（同时切换 AGY 与 gcloud，也可指定单一服务）
gswap use second
gswap use second agy
gswap use default gcloud

# 以指定账号身份运行各类工具（支持账号 ID、序号 1/2/3、邮箱前缀）
gswap second colab sessions
gswap 1 usage
gswap default drive lsd :
gswap default gcloud compute instances list
gswap second agy

# 为指定账号交互式授权绑定新服务
gswap login <acct_id> agy|colab|drive|gcloud
gswap add --discover

# 查看或切换界面语言（zh / en）
gswap lang zh
gswap lang en
gswap -q --lang en
```

---

## 配置文件说明

- **统一账号注册表 (`~/.config/google-accounts/accounts.json`)** — 仅记录账号元信息与各服务的配置/凭据路径（**绝不直接存放** OAuth Token 或密钥正文），并自动合并 Antigravity 凭据金库中的账号槽位。
- **可选全局与本机路径配置 (`~/.config/google-accounts/settings.json`)** — 可配置界面语言（`"lang": "zh"` 或 `"en"`），或自定义二进制路径（`colab_bin`、`rclone_bin`、`gcloud_bin`、`python_bin`）与目录路径（`gcloud_dir`、`rclone_conf`、`colab_secrets_dir`、`colab_sessions_dir`、`train_pool_registry`）。
- **Antigravity 凭据金库 (`~/.local/share/gswap/`)** — 以 `0600` 权限存放各槽位 OAuth 凭据（`sequence.json` 与 `credentials/.agy-credentials-<slot>-<email>.json`）。

---

## 运行单元测试

```bash
python3 -m unittest discover -s tests -v
```

## 开源协议 (License)

MIT
