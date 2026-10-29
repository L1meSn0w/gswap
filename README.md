# gswap

**Language / 语言**: [English](README.md) | [简体中文](README_zh.md)

Interactive multi-account switcher, isolated service runner, and real-time quota/compute center for Google CLI tools:

- **Google Antigravity CLI (`agy`)** — `ccswap`-style interactive menu & instant account switching without relogin, `0400` read-only token file protection (prevents background TUI panels from overwriting your switched token in-place), OS keyring sync (Linux `secret-tool` / macOS `security` Keychain), and live 5-hour & weekly model quota inspection.
- **Google Colab (`colab-cli`)** — per-account ADC credentials, isolated session state files, real-time Compute Units (CU) balance / burn rate / active GPU session inspection, and multi-account training pool registry export.
- **Google Drive (`rclone`)** — per-account remote mapping with `:path` shorthand expansion (`:datasets` $\rightarrow$ `<remote>:datasets`).
- **Google Cloud SDK (`gcloud`)** — per-account named configurations, default project routing, and global active configuration switching.
- **Bilingual UI (`zh` / `en`)** — supports both English and Simplified Chinese out of the box (auto-detected from locale, togglable inside the interactive menu via `[l]`, or persisted via `gswap lang en|zh`).

Zero third-party Python dependencies (Python 3.10+ standard library only).

---

## Installation

### Option 1: Direct Symlink (Recommended)

```bash
git clone https://github.com/L1meSn0w/gswap.git
cd gswap
chmod +x gswap.py
mkdir -p ~/.local/bin
ln -sf "$(pwd)/gswap.py" ~/.local/bin/gswap
```

### Option 2: `pipx` or `uv tool`

```bash
uv tool install git+https://github.com/L1meSn0w/gswap.git
```

---

## Quick Start & Usage

### 1. Interactive Menu (`gswap`)

Run `gswap` with no arguments in your terminal to launch the interactive menu. Navigate with **`↑` / `↓` (or `j` / `k`) + `Enter`**, or press shortcut keys **`1`–`6` / `l` / `q`** directly:

```text
╭──────────────────────────────────────────────────────────────────────────╮
│  gswap — Google Multi-Account Center (AGY · Colab · Drive · GCloud)      │
╰──────────────────────────────────────────────────────────────────────────╯
  [1] default    primary@example.com        (colab drive gcloud) [train-pool] [GCP Active]
      drive=gd-main: · gcloud=main-cfg(my-gcp-project-01)
* [2] second     alice@example.com          (agy colab drive gcloud) [train-pool] [AGY Active]
      agy=slot#1 · drive=gd-second: · gcloud=second(-)
  [3] bob        bob@example.com            (agy)
      agy=slot#2

Select an action: (↑/↓ to move, Enter to confirm, or press shortcut key, q to quit)
 ❯ [1] ⚡ Rotate to next AGY account (alice@example.com → bob@example.com)
   [2] 🔀 Switch active account (AGY / GCloud default)
   [3] 📊 Inspect real-time quotas & compute (AGY 5h/weekly + Colab CU)
   [4] 🚀 Colab compute & GPU sessions (usage / sessions)
   [5] 🔑 Login / wire service (agy · colab · drive · gcloud)
   [6] 🔄 Sync Colab train-pool registry (sync-trainpool)
   [l] 🌐 Language / 语言: English (Switch to 简体中文)
   [q] Quit
```

---

### 2. Direct CLI Commands

All operations can also be invoked directly from the command line:

```bash
# View all registered Google accounts and wired services (instant, no network calls)
gswap status
gswap list

# Query live Colab Compute Units + Antigravity 5h/weekly model quotas concurrently
gswap -q
gswap quota second

# Rotate to the next Antigravity CLI account immediately
gswap next

# Switch active default account (switches both AGY and gcloud, or a single service)
gswap use second
gswap use second agy
gswap use default gcloud

# Run a service under a specific account (by ID, 1-based number, or email prefix)
gswap second colab sessions
gswap 1 usage
gswap default drive lsd :
gswap default gcloud compute instances list
gswap second agy

# Wire / authorize a service for an account
gswap login <acct_id> agy|colab|drive|gcloud
gswap add --discover

# Switch or override display language (en / zh)
gswap lang en
gswap lang zh
gswap -q --lang en
```

---

## Configuration

- **Account Registry (`~/.config/google-accounts/accounts.json`)** — Stores account metadata and file paths only (**never** raw OAuth tokens or secrets). Automatically merges accounts imported into the Antigravity vault.
- **Optional Settings (`~/.config/google-accounts/settings.json`)** — Configure display language (`"lang": "en"` or `"zh"`), override binary paths (`colab_bin`, `rclone_bin`, `gcloud_bin`, `python_bin`), or customize config directories (`gcloud_dir`, `rclone_conf`, `colab_secrets_dir`, `colab_sessions_dir`, `train_pool_registry`).
- **Antigravity Credential Vault (`~/.local/share/gswap/`)** — Stores per-slot OAuth credentials with `0600` permissions (`sequence.json` and `credentials/.agy-credentials-<slot>-<email>.json`).

---

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
