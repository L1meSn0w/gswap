#!/usr/bin/env python3
"""gswap — Google Multi-Account Switcher, Runner & Quota Center.

Unifies Google multi-account management across:
  - Antigravity CLI (`agy`) — zero-relogin OAuth token vault, 0400 read-only mode
    to block in-place overwrites from stale background panels, keyring/Keychain sync,
    and real-time 5h / weekly model quota inspection.
  - Google Colab (`colab`) — per-account ADC credentials, isolated session state,
    Compute Units (CU) balance / burn rate / active GPU sessions, and train-pool sync.
  - Google Drive (`rclone`) — per-account remote mapping (`:path` -> `<remote>:path`).
  - Google Cloud (`gcloud`) — per-account named configurations, active config switching,
    and default project routing.

Usage:
  gswap                              Launch interactive menu (or print status if non-TTY)
  gswap menu | -i                    Launch interactive menu explicitly
  gswap status | -s                  Unified status across all Google accounts
  gswap quota | -q [acct]            Unified status + Colab CU + Antigravity model quotas
  gswap list                         Compact table of accounts and wired services
  gswap next | sw                    Rotate to the next Antigravity CLI account
  gswap use <acct> [all|agy|gcloud]  Switch active default account (agy & gcloud)
  gswap <acct> colab <args...>       Run colab-cli as <acct>
  gswap <acct> usage                 Show Colab CU balance / burn rate / active sessions
  gswap <acct> drive <args...>       Run rclone as <acct> (":path" -> "<remote>:path")
  gswap <acct> gcloud <args...>      Run gcloud under <acct>'s configuration & project
  gswap <acct> agy [args...]         Switch active agy to <acct> (and optionally exec agy)
  gswap login <acct> <service>       Interactive OAuth for agy | colab | drive | gcloud
  gswap add [--discover|--from-...]  Import Antigravity account into vault
  gswap remove <slot|email>          Remove an Antigravity account slot
  gswap sync-trainpool               Regenerate train-pool Colab account registry
  gswap lang [zh|en]                 Show or switch UI language (Simplified Chinese / English)

Options:
  --lang, -L <zh|en>                 Override display language for this invocation
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"
CODE_ASSIST_BASE = "https://daily-cloudcode-pa.googleapis.com"
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]


class AgySwapError(RuntimeError):
    """User-facing error in gswap / agyswap."""


_LANG_OVERRIDE: str | None = None
SUPPORTED_LANGS = ("zh", "en")

HELP_ZH = """\
gswap — Google 多账号切换、服务分发与算力/额度管理中心

统一纳管四类 Google 服务：
  - Antigravity CLI (`agy`) — 免重登 OAuth 凭据金库、0400 只读权限防止后台旧面板
    原地覆盖写回、系统 Keyring 同步，以及 5h / 周限模型额度实时查询。
  - Google Colab (`colab`) — 多账号独立 ADC 凭据与会话状态隔离、CU 算力余额/消耗速率/
    活跃 GPU 会话查询，以及训练池注册表生成。
  - Google Drive (`rclone`) — 多账号远端自动映射 (`:path` -> `<remote>:path`)。
  - Google Cloud (`gcloud`) — 多账号命名配置隔离、全局默认配置切换与默认项目路由。

用法:
  gswap                              启动交互式菜单 (非 TTY 环境则输出统一状态)
  gswap menu | -i                    显式启动交互式菜单
  gswap status | -s                  查看所有 Google 账号的统一绑定与激活状态
  gswap quota | -q [acct]            并发查询所有(或指定)账号的 Colab CU 与 AGY 模型额度
  gswap list                         紧凑表格列出账号与已绑定服务
  gswap next | sw                    快速轮换到下一个 Antigravity CLI 账号
  gswap use <acct> [all|agy|gcloud]  切换全局默认账号 (同时或单独切换 agy 与 gcloud)
  gswap <acct> colab <args...>       以 <acct> 身份运行 colab-cli
  gswap <acct> usage                 查看 <acct> 的 Colab CU 算力余额与活跃会话
  gswap <acct> drive <args...>       以 <acct> 身份运行 rclone (":path" 自动展开为 "<remote>:path")
  gswap <acct> gcloud <args...>      以 <acct> 的配置与默认 project 运行 gcloud
  gswap <acct> agy [args...]         切换当前 agy 到 <acct> (若有后续参数则直接透传执行 agy)
  gswap login <acct> <service>       交互式 OAuth 授权绑定 (agy | colab | drive | gcloud)
  gswap add [--discover|--from-...]  导入 Antigravity 账号凭据到金库
  gswap remove <slot|email>          移除指定 Antigravity 账号槽位
  gswap sync-trainpool               生成训练池 Colab 账号注册表
  gswap lang [zh|en]                 查看或切换界面语言 (简体中文 / English)

全局选项:
  --lang, -L <zh|en>                 临时指定本次命令输出语言
"""


def _normalize_lang(val: str | None) -> str | None:
    if not val:
        return None
    v = val.strip().lower()
    if v.startswith("zh") or v in ("cn", "chs", "chinese"):
        return "zh"
    if v.startswith("en") or v in ("eng", "english"):
        return "en"
    return None


def set_lang(lang: str | None) -> None:
    """Override active UI language in memory for the current process."""
    global _LANG_OVERRIDE
    if lang is None:
        _LANG_OVERRIDE = None
        return
    norm = _normalize_lang(lang)
    if norm not in SUPPORTED_LANGS:
        raise AgySwapError(
            _t(
                f"不支持的语言 '{lang}'（可选: zh 或 en）",
                f"Unsupported language '{lang}' (choose 'zh' or 'en')",
            )
        )
    _LANG_OVERRIDE = norm


def get_lang(home: Path | None = None) -> str:
    """Resolve active UI language (`zh` or `en`)."""
    if _LANG_OVERRIDE in SUPPORTED_LANGS:
        return _LANG_OVERRIDE
    env_lang = _normalize_lang(os.environ.get("GSWAP_LANG"))
    if env_lang:
        return env_lang
    for spath in (gacct_settings_path(home), swap_dir(home) / "settings.json"):
        if spath.is_file():
            try:
                doc = json.loads(spath.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    cfg_lang = _normalize_lang(str(doc.get("lang") or ""))
                    if cfg_lang:
                        return cfg_lang
            except Exception:
                pass
    for env_key in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        loc = _normalize_lang(os.environ.get(env_key))
        if loc:
            return loc
    return "en"


def save_lang_setting(lang: str, home: Path | None = None) -> str:
    """Persist language preference (`zh` or `en`) to settings.json and update process override."""
    norm = _normalize_lang(lang)
    if norm not in SUPPORTED_LANGS:
        raise AgySwapError(
            _t(
                f"不支持的语言 '{lang}'（可选: zh 或 en）",
                f"Unsupported language '{lang}' (choose 'zh' or 'en')",
                home=home,
            )
        )
    spath = gacct_settings_path(home)
    data: dict[str, Any] = {}
    if spath.is_file():
        try:
            loaded = json.loads(spath.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AgySwapError(
                _t(
                    f"配置文件 JSON 格式损坏，拒绝覆盖 {spath}: {exc}",
                    f"Refusing to overwrite malformed JSON in {spath}: {exc}",
                    home=home,
                )
            ) from exc
        if not isinstance(loaded, dict):
            raise AgySwapError(
                _t(
                    f"配置文件根节点必须是 JSON 对象，拒绝覆盖 {spath}",
                    f"Refusing to overwrite non-object JSON in {spath}",
                    home=home,
                )
            )
        data = loaded
    data["lang"] = norm
    atomic_write_json(spath, data, mode=0o600)
    set_lang(norm)
    return norm


def _t(zh: str, en: str, home: Path | None = None) -> str:
    return zh if get_lang(home) == "zh" else en


def get_help_text(home: Path | None = None) -> str:
    return HELP_ZH if get_lang(home) == "zh" else (__doc__ or "")


_PASSTHROUGH_SERVICES = ("colab", "usage", "drive", "gcloud", "agy")


def _strip_lang_flags(argv: list[str], home: Path | None = None) -> list[str]:
    """Extract `--lang/-L <zh|en>` or `--lang=<zh|en>` for gswap without touching downstream service args."""
    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        # Once we reach `--` or `<acct> <service>`, pass all remaining args verbatim
        if argv[i] == "--":
            cleaned.extend(argv[i + 1 :])
            break
        if len(cleaned) >= 2 and cleaned[1] in _PASSTHROUGH_SERVICES and cleaned[0] not in (
            "use",
            "switch",
            "sw",
            "login",
        ):
            cleaned.extend(argv[i:])
            break
        arg = argv[i]
        if arg in ("--lang", "-L"):
            if i + 1 >= len(argv):
                raise AgySwapError(
                    _t(
                        f"选项 `{arg}` 缺少语言参数（可选: zh 或 en）",
                        f"Option `{arg}` requires a language argument ('zh' or 'en')",
                        home=home,
                    )
                )
            set_lang(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--lang="):
            set_lang(arg.split("=", 1)[1])
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    return cleaned


# ---------------------------------------------------------------------------
# Paths & Atomic File Helpers
# ---------------------------------------------------------------------------


def swap_dir(home: Path | None = None) -> Path:
    env = os.environ.get("GSWAP_DIR") or os.environ.get("AGY_SWAP_DIR")
    if env:
        return Path(env).expanduser()
    base = home or Path.home()
    new_dir = base / ".local" / "share" / "gswap"
    old_dir = base / ".local" / "share" / "agy-swap"
    if not new_dir.exists() and old_dir.exists():
        try:
            old_dir.rename(new_dir)
        except OSError:
            return old_dir
    return new_dir


def gacct_dir(home: Path | None = None) -> Path:
    env = os.environ.get("GACCT_DIR")
    if env:
        return Path(env).expanduser()
    base = home or Path.home()
    return base / ".config" / "google-accounts"


def gacct_registry_path(home: Path | None = None) -> Path:
    env = os.environ.get("GACCT_REGISTRY")
    if env:
        return Path(env).expanduser()
    return gacct_dir(home) / "accounts.json"


def gacct_settings_path(home: Path | None = None) -> Path:
    env = os.environ.get("GACCT_SETTINGS")
    if env:
        return Path(env).expanduser()
    return gacct_dir(home) / "settings.json"


def cli_token_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def standalone_token_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".gemini" / "jetski-standalone-oauth-token"


def cli_marker_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".gemini" / "antigravity-cli" / "cache" / "antigravity-keyring-unavailable"


def standalone_marker_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".gemini" / "cache" / "antigravity-keyring-unavailable"


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def touch_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    # composite_token_storage.go checks time.Since(info.ModTime()) < 1*time.Hour;
    # setting mtime 1 year in the future keeps time.Since(modTime) < 0 so it never expires.
    future = dt.datetime.now(dt.timezone.utc).timestamp() + 365 * 86400
    os.utime(path, (future, future))


# ---------------------------------------------------------------------------
# Antigravity OAuth Client & Token Normalization
# ---------------------------------------------------------------------------


def discover_oauth_client(home: Path | None = None) -> tuple[str, str]:
    """Return (client_id, client_secret) without hardcoding secrets in git."""
    env_id = os.environ.get("AGYSWAP_CLIENT_ID")
    env_secret = os.environ.get("AGYSWAP_CLIENT_SECRET")
    if env_id and env_secret:
        return env_id, env_secret

    cache_file = swap_dir(home) / ".oauth_client.json"
    if cache_file.is_file():
        try:
            doc = json.loads(cache_file.read_text(encoding="utf-8"))
            cid = doc.get("client_id") or DEFAULT_CLIENT_ID
            csec = doc.get("client_secret") or ""
            if csec.startswith("GOCSPX-"):
                return cid, csec
        except Exception:
            pass

    candidates: list[Path] = []
    which_agy = shutil.which("agy")
    if which_agy:
        candidates.append(Path(which_agy).resolve())
    base = home or Path.home()
    candidates.extend([
        base / ".local" / "bin" / "agy",
        base / ".antigravity_tools" / "agy",
    ])
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(
            Path(local_app)
            / "Programs"
            / "antigravity"
            / "resources"
            / "app"
            / "extensions"
            / "antigravity"
            / "bin"
            / "language_server_windows_x64.exe"
        )

    secret_pat = re.compile(rb"GOCSPX-[A-Za-z0-9_-]{28}")
    id_bytes = DEFAULT_CLIENT_ID.encode("ascii")
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            blob = cand.read_bytes()
        except OSError:
            continue
        idx = blob.find(id_bytes)
        if idx != -1:
            window = blob[max(0, idx - 4096) : min(len(blob), idx + 4096)]
            m = secret_pat.search(window)
            if m:
                secret = m.group(0).decode("ascii")
                try:
                    atomic_write_json(cache_file, {"client_id": DEFAULT_CLIENT_ID, "client_secret": secret})
                except OSError:
                    pass
                return DEFAULT_CLIENT_ID, secret
        m = secret_pat.search(blob)
        if m:
            secret = m.group(0).decode("ascii")
            try:
                atomic_write_json(cache_file, {"client_id": DEFAULT_CLIENT_ID, "client_secret": secret})
            except OSError:
                pass
            return DEFAULT_CLIENT_ID, secret

    return DEFAULT_CLIENT_ID, ""


def parse_token_blob(raw: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Normalize an Antigravity token JSON or base64-encoded JSON into standard format."""
    if isinstance(raw, dict):
        doc = dict(raw)
    else:
        text = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not text:
            raise AgySwapError(_t("Token 数据为空", "Empty token data"))
        if not text.startswith("{"):
            try:
                text = base64.b64decode(text).decode("utf-8")
            except Exception as exc:
                raise AgySwapError(
                    _t(
                        f"Token 既不是有效的 JSON 也不是 Base64 编码的 JSON: {exc}",
                        f"Token is neither JSON nor base64 JSON: {exc}",
                    )
                ) from exc
        try:
            doc = json.loads(text)
        except Exception as exc:
            raise AgySwapError(_t(f"无效的 JSON Token: {exc}", f"Invalid JSON token: {exc}")) from exc

    if isinstance(doc.get("token"), dict):
        inner = doc["token"]
        email = doc.get("id_token_email") or doc.get("email") or inner.get("email")
        doc = {
            "token": inner.get("access_token") or inner.get("token") or "",
            "refresh_token": inner.get("refresh_token") or "",
            "token_uri": inner.get("token_uri") or TOKEN_URI,
            "client_id": inner.get("client_id") or DEFAULT_CLIENT_ID,
            "client_secret": inner.get("client_secret") or "",
            "scopes": inner.get("scopes") or DEFAULT_SCOPES,
            "expiry": inner.get("expiry") or "",
            "auth_method": doc.get("auth_method") or "consumer",
        }
        if email:
            doc["email"] = email

    access_token = doc.get("token") or doc.get("access_token") or ""
    refresh_token = doc.get("refresh_token") or ""
    if not refresh_token and not access_token:
        raise AgySwapError(
            _t(
                "Token 文件同时缺少 refresh_token 与 token",
                "Token file missing both refresh_token and token",
            )
        )

    normalized: dict[str, Any] = {
        "token": access_token,
        "refresh_token": refresh_token,
        "token_uri": doc.get("token_uri") or TOKEN_URI,
        "client_id": doc.get("client_id") or DEFAULT_CLIENT_ID,
        "client_secret": doc.get("client_secret") or "",
        "scopes": doc.get("scopes") or list(DEFAULT_SCOPES),
        "expiry": doc.get("expiry") or "1970-01-01T00:00:00Z",
        "auth_method": doc.get("auth_method") or "consumer",
    }
    email_val = doc.get("id_token_email") or doc.get("email")
    if email_val:
        normalized["email"] = str(email_val).strip().lower()
    return normalized


def clean_cli_token_payload(doc: dict[str, Any], home: Path | None = None) -> dict[str, Any]:
    """Return the exact StoredToken JSON structure expected by Antigravity CLI/Desktop."""
    norm = parse_token_blob(doc)
    out: dict[str, Any] = {
        "token": {
            "access_token": norm.get("token") or "",
            "token_type": "Bearer",
            "refresh_token": norm.get("refresh_token") or "",
            "expiry": norm.get("expiry") or "1970-01-01T00:00:00Z",
            "expires_in": 3599,
        },
        "auth_method": norm.get("auth_method") or "consumer",
    }
    email_val = doc.get("id_token_email") or norm.get("email")
    if email_val:
        out["id_token_email"] = str(email_val).strip().lower()
    return out


def read_keyring_token() -> dict[str, Any] | None:
    """Read token from OS keyring (Linux secret-tool or macOS security CLI)."""
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return parse_token_blob(proc.stdout.strip())
        except Exception:
            pass
    if not shutil.which("secret-tool"):
        return None
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", "service", "gemini", "username", "antigravity"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return parse_token_blob(proc.stdout.strip())
    except AgySwapError:
        return None


def write_keyring_token(payload: dict[str, Any]) -> bool:
    """Write StoredToken JSON to OS keyring so keyring and file never diverge."""
    if os.environ.get("AGYSWAP_SKIP_KEYRING") == "1":
        return False
    raw_json = json.dumps(payload, ensure_ascii=False)
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            proc = subprocess.run(
                ["security", "add-generic-password", "-U", "-s", "gemini", "-a", "antigravity", "-w", raw_json],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return proc.returncode == 0
        except Exception:
            return False
    if not shutil.which("secret-tool"):
        return False
    try:
        proc = subprocess.run(
            ["secret-tool", "store", "--label=antigravity", "service", "gemini", "username", "antigravity"],
            input=raw_json,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def refresh_access_token(doc: dict[str, Any], home: Path | None = None, timeout: int = 12) -> dict[str, Any]:
    """Refresh OAuth access token using refresh_token."""
    refresh_tok = doc.get("refresh_token")
    if not refresh_tok:
        raise AgySwapError(
            _t("无法刷新 Token：缺少 refresh_token", "Cannot refresh token: missing refresh_token", home=home)
        )
    cid, csec = discover_oauth_client(home)
    client_id = doc.get("client_id") or cid or DEFAULT_CLIENT_ID
    client_secret = doc.get("client_secret") or csec
    form = {
        "client_id": client_id,
        "RefreshToken": refresh_tok,
        "refresh_token": refresh_tok,
        "grant_type": "refresh_token",
    }
    if client_secret:
        form["client_secret"] = client_secret
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        doc.get("token_uri") or TOKEN_URI,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        raise AgySwapError(
            _t(
                f"OAuth Token 刷新失败 (HTTP {exc.code}): {body}",
                f"OAuth token refresh failed (HTTP {exc.code}): {body}",
                home=home,
            )
        ) from exc
    except Exception as exc:
        raise AgySwapError(
            _t(f"OAuth Token 刷新失败: {exc}", f"OAuth token refresh failed: {exc}", home=home)
        ) from exc

    updated = dict(doc)
    updated["token"] = res["access_token"]
    expires_in = int(res.get("expires_in", 3599))
    expiry_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in)
    updated["expiry"] = expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    updated["client_id"] = client_id
    if client_secret:
        updated["client_secret"] = client_secret
    return updated


def token_needs_refresh(doc: dict[str, Any]) -> bool:
    if not doc.get("token"):
        return True
    expiry = doc.get("expiry")
    if not expiry:
        return True
    try:
        exp_str = str(expiry).replace("Z", "+00:00")
        exp_dt = dt.datetime.fromisoformat(exp_str)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=dt.timezone.utc)
        return (exp_dt - dt.datetime.now(dt.timezone.utc)).total_seconds() < 120
    except Exception:
        return True


def fetch_email(access_token: str, timeout: int = 10) -> str:
    req = urllib.request.Request(
        USERINFO_URI,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        email = str(info.get("email") or "").strip().lower()
        if not email:
            raise AgySwapError(
                _t("Google userinfo 响应中未包含邮箱", "Google userinfo response did not include an email")
            )
        return email
    except AgySwapError:
        raise
    except Exception as exc:
        raise AgySwapError(
            _t(f"查询 Google userinfo 失败: {exc}", f"Failed to query Google userinfo: {exc}")
        ) from exc


def resolve_token_and_email(doc: dict[str, Any], home: Path | None = None) -> tuple[dict[str, Any], str]:
    updated = dict(doc)
    if token_needs_refresh(updated):
        updated = refresh_access_token(updated, home=home)
    try:
        email = fetch_email(updated["token"])
    except AgySwapError:
        updated = refresh_access_token(updated, home=home)
        email = fetch_email(updated["token"])
    updated["email"] = email
    return updated, email


def fetch_quota_summary(
    doc: dict[str, Any], home: Path | None = None, timeout: int = 12
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (possibly_refreshed_doc, quota_info)."""
    updated = dict(doc)
    if token_needs_refresh(updated):
        updated = refresh_access_token(updated, home=home, timeout=timeout)

    headers = {
        "Authorization": f"Bearer {updated['token']}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/1.4.9 linux/amd64",
    }

    def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        assist = post_json(
            f"{CODE_ASSIST_BASE}/v1internal:loadCodeAssist",
            {"metadata": {"ideType": "ANTIGRAVITY"}},
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            updated = refresh_access_token(updated, home=home, timeout=timeout)
            headers["Authorization"] = f"Bearer {updated['token']}"
            assist = post_json(
                f"{CODE_ASSIST_BASE}/v1internal:loadCodeAssist",
                {"metadata": {"ideType": "ANTIGRAVITY"}},
            )
        else:
            raise AgySwapError(f"loadCodeAssist HTTP {exc.code}") from exc
    except Exception as exc:
        raise AgySwapError(f"loadCodeAssist failed: {exc}") from exc

    proj = assist.get("cloudaicompanionProject")
    if isinstance(proj, dict):
        proj = proj.get("id") or ""
    tier = (
        (assist.get("paidTier") or {}).get("name")
        or (assist.get("currentTier") or {}).get("name")
        or "Unknown Tier"
    )
    quota_raw = post_json(
        f"{CODE_ASSIST_BASE}/v1internal:retrieveUserQuotaSummary",
        {"project": proj} if proj else {},
    )
    models_raw: dict[str, Any] = {}
    try:
        models_raw = post_json(
            f"{CODE_ASSIST_BASE}/v1internal:fetchAvailableModels",
            {"project": proj} if proj else {},
        )
    except Exception:
        models_raw = {}

    groups: list[dict[str, Any]] = []
    models_map = models_raw.get("models") or {}
    gemini_model = models_map.get("gemini-3.8-flash-tiered") or models_map.get("gemini-2.5-pro")
    if gemini_model and isinstance(gemini_model.get("quotaInfo"), dict):
        qi = gemini_model["quotaInfo"]
        rem = qi.get("remainingFraction")
        rem_pct = round(float(rem) * 100.0, 1) if rem is not None else 0.0
        groups.append(
            {
                "name": "Gemini Models",
                "windows": {
                    "current": {
                        "remaining_fraction": float(rem) if rem is not None else 0.0,
                        "remaining_percent": rem_pct,
                        "used_percent": round(100.0 - rem_pct, 1),
                        "reset_time": qi.get("resetTime") or "",
                    }
                },
            }
        )

    for g in quota_raw.get("groups") or []:
        buckets = g.get("buckets") or []
        if not buckets:
            continue
        name = g.get("displayName") or buckets[0].get("displayName") or "Models"
        windows: dict[str, Any] = {}
        for b in buckets:
            win = b.get("window") or "unknown"
            rem = b.get("remainingFraction")
            rem_pct = round(float(rem) * 100.0, 1) if rem is not None else 0.0
            windows[win] = {
                "remaining_fraction": float(rem) if rem is not None else 0.0,
                "remaining_percent": rem_pct,
                "used_percent": round(100.0 - rem_pct, 1),
                "reset_time": b.get("resetTime") or "",
            }
        groups.append({"name": name, "windows": windows})

    return updated, {
        "tier": tier,
        "project": proj or "",
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# Antigravity Slot Vault (sequence.json & credentials/)
# ---------------------------------------------------------------------------


def load_sequence(home: Path | None = None) -> dict[str, Any]:
    seq_file = swap_dir(home) / "sequence.json"
    if not seq_file.is_file():
        return {
            "activeAccountNumber": None,
            "lastUpdated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sequence": [],
            "accounts": {},
        }
    return json.loads(seq_file.read_text(encoding="utf-8"))


def save_sequence(seq: dict[str, Any], home: Path | None = None) -> None:
    seq["lastUpdated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    atomic_write_json(swap_dir(home) / "sequence.json", seq, mode=0o600)


def cred_file_for(num: int | str, email: str, home: Path | None = None) -> Path:
    return swap_dir(home) / "credentials" / f".agy-credentials-{num}-{email}.json"


def load_slot_token(num: int | str, email: str, home: Path | None = None) -> dict[str, Any]:
    path = cred_file_for(num, email, home)
    if not path.is_file():
        raise AgySwapError(
            _t(
                f"槽位 #{num} ({email}) 的凭据文件缺失: {path}",
                f"Credential file missing for slot #{num} ({email}): {path}",
                home=home,
            )
        )
    doc = parse_token_blob(path.read_text(encoding="utf-8"))
    if email:
        doc["email"] = email.strip().lower()
    return doc


def save_slot_token(num: int | str, email: str, doc: dict[str, Any], home: Path | None = None) -> Path:
    doc_with_email = dict(doc)
    if email:
        doc_with_email["email"] = email.strip().lower()
    payload = clean_cli_token_payload(doc_with_email, home=home)
    path = cred_file_for(num, email, home)
    atomic_write_json(path, payload, mode=0o600)
    return path


def read_live_cli_token(home: Path | None = None) -> dict[str, Any] | None:
    for p in (cli_token_path(home), standalone_token_path(home)):
        if p.is_file():
            try:
                return parse_token_blob(p.read_text(encoding="utf-8"))
            except AgySwapError:
                continue
    return None


def sync_live_stores(doc: dict[str, Any], home: Path | None = None) -> dict[str, Any]:
    """Write token to CLI file & standalone file (mode 0o400 blocks in-place overwrites), touch markers, and update keyring."""
    payload = clean_cli_token_payload(doc, home=home)
    cli_path = cli_token_path(home)
    std_path = standalone_token_path(home)
    prev_cli = cli_path.read_bytes() if cli_path.is_file() else None
    atomic_write_json(cli_path, payload, mode=0o400)
    try:
        atomic_write_json(std_path, payload, mode=0o400)
    except Exception:
        if prev_cli is not None:
            try:
                atomic_write_json(cli_path, json.loads(prev_cli.decode("utf-8")), mode=0o400)
            except Exception:
                pass
        raise
    touch_marker(cli_marker_path(home))
    touch_marker(standalone_marker_path(home))
    keyring_updated = write_keyring_token(payload)
    return {
        "cli_token_path": str(cli_path),
        "standalone_token_path": str(std_path),
        "keyring_updated": keyring_updated,
    }


def detect_active_slot(seq: dict[str, Any], home: Path | None = None) -> str | None:
    """Match live CLI token against managed slots by refresh_token or access token."""
    live = read_live_cli_token(home)
    if not live:
        active = seq.get("activeAccountNumber")
        return str(active) if active is not None and str(active) in (seq.get("accounts") or {}) else None

    accounts = seq.get("accounts") or {}
    for num_str, meta in accounts.items():
        email = meta.get("email") or ""
        try:
            slot_doc = load_slot_token(num_str, email, home=home)
        except AgySwapError:
            continue
        if live.get("refresh_token") and live.get("refresh_token") == slot_doc.get("refresh_token"):
            return str(num_str)
        if live.get("token") and live.get("token") == slot_doc.get("token"):
            return str(num_str)
    # A live token file exists on disk but does not match any managed slot
    return None


def save_current_live_to_slot(seq: dict[str, Any], home: Path | None = None) -> str | None:
    """If live CLI token belongs to a managed slot, persist any newer token back to that slot."""
    live = read_live_cli_token(home)
    if not live:
        return None
    accounts = seq.get("accounts") or {}
    for num_str, meta in accounts.items():
        email = meta.get("email") or ""
        try:
            slot_doc = load_slot_token(num_str, email, home=home)
        except AgySwapError:
            continue
        if live.get("refresh_token") and live.get("refresh_token") == slot_doc.get("refresh_token"):
            save_slot_token(num_str, email, live, home=home)
            return str(num_str)
    return None


def upsert_account(
    doc: dict[str, Any],
    alias: str | None = None,
    email_override: str | None = None,
    home: Path | None = None,
    make_active: bool = False,
) -> tuple[int, str, bool]:
    """Add or update an account in sequence.json. Returns (slot_num, email, is_new)."""
    if email_override:
        email = email_override.strip().lower()
        resolved_doc = dict(doc)
        resolved_doc["email"] = email
    elif doc.get("email"):
        email = str(doc["email"]).strip().lower()
        resolved_doc = dict(doc)
    else:
        resolved_doc, email = resolve_token_and_email(doc, home=home)

    seq = load_sequence(home)
    accounts: dict[str, Any] = seq.setdefault("accounts", {})
    order: list[int] = seq.setdefault("sequence", [])

    existing_num: int | None = None
    for num_str, meta in accounts.items():
        if str(meta.get("email") or "").lower() == email:
            existing_num = int(num_str)
            break

    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if existing_num is not None:
        num = existing_num
        is_new = False
        if alias:
            accounts[str(num)]["alias"] = alias
        accounts[str(num)]["updated"] = now_iso
    else:
        used_nums = [int(k) for k in accounts.keys()]
        num = (max(used_nums) + 1) if used_nums else 1
        is_new = True
        accounts[str(num)] = {
            "email": email,
            "alias": alias or email.split("@")[0],
            "added": now_iso,
        }
        if num not in order:
            order.append(num)

    save_slot_token(num, email, resolved_doc, home=home)
    if make_active or seq.get("activeAccountNumber") is None:
        seq["activeAccountNumber"] = num
    save_sequence(seq, home=home)

    # Sync into unified google-accounts registry if present
    reg_path = gacct_registry_path(home)
    if reg_path.is_file():
        try:
            load_gacct_registry(home=home, sync_back=True)
        except Exception:
            pass

    return num, email, is_new


def resolve_target_slot(seq: dict[str, Any], target: str | None, home: Path | None = None) -> int:
    accounts: dict[str, Any] = seq.get("accounts") or {}
    order: list[int] = [int(x) for x in (seq.get("sequence") or []) if str(x) in accounts]
    if not order:
        raise AgySwapError(
            _t(
                "尚未添加任何 Antigravity 账号，请先运行 `gswap add --discover` 或 `gswap login <acct> agy`。",
                "No Antigravity accounts configured yet. Run `gswap add --discover` or `gswap login <acct> agy` first.",
                home=home,
            )
        )

    if target is None or target == "":
        cur_str = detect_active_slot(seq, home=home)
        if cur_str is not None and int(cur_str) in order:
            idx = order.index(int(cur_str))
            return order[(idx + 1) % len(order)]
        return order[0]

    q = target.strip().lower()
    if q.isdigit() and q in accounts:
        return int(q)
    for num_str, meta in accounts.items():
        email = str(meta.get("email") or "").lower()
        alias = str(meta.get("alias") or "").lower()
        if q in (email, alias, email.split("@")[0]):
            return int(num_str)
    # Also allow matching by unified registry `id` (e.g. `second`)
    reg_path = gacct_registry_path(home)
    if reg_path.is_file():
        try:
            for item in json.loads(reg_path.read_text(encoding="utf-8")):
                if str(item.get("id") or "").lower() == q:
                    remail = str(item.get("email") or "").lower()
                    for num_str, meta in accounts.items():
                        if str(meta.get("email") or "").lower() == remail:
                            return int(num_str)
        except Exception:
            pass
    for num_str, meta in accounts.items():
        email = str(meta.get("email") or "").lower()
        alias = str(meta.get("alias") or "").lower()
        if email.startswith(q) or alias.startswith(q):
            return int(num_str)
    raise AgySwapError(
        _t(f"未找到匹配的账号: {target}", f"No matching account found: {target}", home=home)
    )


def switch_to(target: str | None = None, home: Path | None = None) -> dict[str, Any]:
    seq = load_sequence(home)
    save_current_live_to_slot(seq, home=home)
    target_num = resolve_target_slot(seq, target, home=home)
    meta = seq["accounts"][str(target_num)]
    email = meta["email"]
    doc = load_slot_token(target_num, email, home=home)
    refresh_warning: str | None = None
    if token_needs_refresh(doc):
        try:
            doc = refresh_access_token(doc, home=home)
            save_slot_token(target_num, email, doc, home=home)
        except AgySwapError as exc:
            if not doc.get("token"):
                raise
            refresh_warning = str(exc)
            print(
                _t(
                    f"⚠ Token 刷新失败，已回退写入本地缓存凭据: {exc}",
                    f"⚠ Token refresh failed, falling back to cached credentials: {exc}",
                    home=home,
                ),
                file=sys.stderr,
            )
    sync_info = sync_live_stores(doc, home=home)
    seq["activeAccountNumber"] = target_num
    save_sequence(seq, home=home)
    out = {
        "slot": target_num,
        "email": email,
        "alias": meta.get("alias") or email.split("@")[0],
        **sync_info,
    }
    if refresh_warning:
        out["refresh_warning"] = refresh_warning
    return out


def format_reset_local(iso_str: str, home: Path | None = None) -> str:
    if not iso_str:
        return "—"
    try:
        dt_utc = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        bj = dt_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))
        diff_s = (dt_utc - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if diff_s <= 0:
            rem = _t("即将重置", "resetting soon", home=home)
        elif diff_s >= 86400:
            days = int(diff_s // 86400)
            hours = int((diff_s % 86400) // 3600)
            rem = f"{days}d{hours}h"
        else:
            hours = int(diff_s // 3600)
            mins = int((diff_s % 3600) // 60)
            rem = f"{hours}h{mins}m"
        return f"{bj.strftime('%m-%d %H:%M')} ({rem})"
    except Exception:
        return iso_str


def collect_status(include_quota: bool = True, home: Path | None = None) -> dict[str, Any]:
    seq = load_sequence(home)
    save_current_live_to_slot(seq, home=home)
    active_str = detect_active_slot(seq, home=home)
    accounts = seq.get("accounts") or {}
    order = [int(x) for x in (seq.get("sequence") or []) if str(x) in accounts]
    rows: list[dict[str, Any]] = []

    for num in order:
        num_str = str(num)
        meta = accounts[num_str]
        email = meta["email"]
        is_active = num_str == active_str
        row: dict[str, Any] = {
            "slot": num,
            "email": email,
            "alias": meta.get("alias") or email.split("@")[0],
            "active": is_active,
            "added": meta.get("added"),
        }
        if include_quota:
            try:
                doc = load_slot_token(num, email, home=home)
                refreshed, quota = fetch_quota_summary(doc, home=home)
                if refreshed.get("token") != doc.get("token") or refreshed.get("expiry") != doc.get("expiry"):
                    save_slot_token(num, email, refreshed, home=home)
                    if is_active:
                        sync_live_stores(refreshed, home=home)
                row["quota"] = quota
            except Exception as exc:
                row["quota_error"] = str(exc)
        rows.append(row)

    return {
        "active_slot": int(active_str) if active_str and active_str.isdigit() else None,
        "accounts": rows,
    }


def _print_agy_quota_groups(groups: list[dict[str, Any]], home: Path | None = None) -> None:
    zh = get_lang(home) == "zh"
    for g in groups:
        wins = g.get("windows") or {}
        if "current" in wins and "weekly" not in wins:
            wc = wins["current"]
            rst = format_reset_local(wc.get("reset_time", ""), home=home)
            if zh:
                print(f"      - {g['name']:<22}: 剩余 {wc.get('remaining_percent', 0):5.1f}% (重置 {rst})")
            else:
                print(f"      - {g['name']:<22}: {wc.get('remaining_percent', 0):5.1f}% left (resets {rst})")
            continue
        w5 = wins.get("5h") or {}
        ww = wins.get("weekly") or {}
        if zh:
            s5 = f"5h 剩余 {w5.get('remaining_percent', 0):5.1f}%" if w5 else "5h —"
            sw = (
                f"周剩余 {ww.get('remaining_percent', 0):5.1f}% (重置 {format_reset_local(ww.get('reset_time', ''), home=home)})"
                if ww
                else "周 —"
            )
        else:
            s5 = f"5h {w5.get('remaining_percent', 0):5.1f}% left" if w5 else "5h —"
            sw = (
                f"weekly {ww.get('remaining_percent', 0):5.1f}% left (resets {format_reset_local(ww.get('reset_time', ''), home=home)})"
                if ww
                else "weekly —"
            )
        print(f"      - {g['name']:<22}: {s5:<18} | {sw}")


def print_status_table(status_data: dict[str, Any], include_quota: bool = True, home: Path | None = None) -> None:
    rows = status_data.get("accounts") or []
    if not rows:
        print(
            _t(
                "尚未配置任何 Antigravity 账号。运行 `gswap add --discover` 或 `gswap login <acct> agy` 添加。",
                "No Antigravity accounts configured. Run `gswap add --discover` or `gswap login <acct> agy` to add one.",
                home=home,
            )
        )
        return

    active_tag = _t(" [当前激活]", " [ACTIVE]", home=home)
    for row in rows:
        mark = "*" if row["active"] else " "
        state_lbl = active_tag if row["active"] else ""
        quota = row.get("quota") or {}
        tier_lbl = f" · {quota['tier']}" if quota.get("tier") else ""
        print(f"{mark} [{row['slot']}] {row['email']} ({row['alias']}){tier_lbl}{state_lbl}")
        if not include_quota:
            continue
        if row.get("quota_error"):
            print(
                _t(
                    f"      额度查询异常: {row['quota_error']}",
                    f"      Quota query error: {row['quota_error']}",
                    home=home,
                )
            )
            continue
        _print_agy_quota_groups(quota.get("groups") or [], home=home)


def remove_slot(target: str, home: Path | None = None) -> dict[str, Any]:
    seq = load_sequence(home)
    active_before = detect_active_slot(seq, home=home)
    num = resolve_target_slot(seq, target, home=home)
    num_str = str(num)
    meta = seq["accounts"].pop(num_str)
    seq["sequence"] = [x for x in (seq.get("sequence") or []) if int(x) != num]
    cred_path = cred_file_for(num, meta["email"], home=home)
    if cred_path.exists():
        cred_path.unlink()
    was_active = seq.get("activeAccountNumber") == num or active_before == num_str
    if was_active:
        seq["activeAccountNumber"] = seq["sequence"][0] if seq["sequence"] else None
    save_sequence(seq, home=home)
    if was_active and seq["activeAccountNumber"] is not None:
        nxt_num = seq["activeAccountNumber"]
        nxt_meta = seq["accounts"].get(str(nxt_num)) or {}
        if nxt_meta.get("email"):
            try:
                nxt_doc = load_slot_token(nxt_num, nxt_meta["email"], home=home)
                sync_live_stores(nxt_doc, home=home)
            except AgySwapError:
                pass
    return {"removed_slot": num, "email": meta["email"]}


# ---------------------------------------------------------------------------
# Unified Google Multi-Account Registry & Services (Colab / Drive / GCloud / AGY)
# ---------------------------------------------------------------------------


def _p(path_str: str) -> str:
    return str(Path(path_str).expanduser())


def load_gacct_settings(home: Path | None = None) -> dict[str, str]:
    """Load machine-specific tool binary and config path defaults."""
    base = home or Path.home()
    gcloud_sdk = base / ".local" / "opt" / "google-cloud-sdk" / "bin" / "gcloud"

    defaults = {
        "colab_bin": shutil.which("colab") or "colab",
        "rclone_bin": shutil.which("rclone") or "rclone",
        "python_bin": sys.executable,
        "gcloud_bin": str(gcloud_sdk) if gcloud_sdk.is_file() else (shutil.which("gcloud") or "gcloud"),
        "gcloud_dir": "~/.config/gcloud",
        "rclone_conf": "~/.config/rclone/rclone.conf",
        "colab_secrets_dir": "~/.config/google-accounts/secrets",
        "colab_sessions_dir": "~/.config/google-accounts/sessions",
        "train_pool_registry": "~/.config/google-accounts/colab_accounts.json",
    }
    spath = gacct_settings_path(home)
    if spath.is_file():
        try:
            custom = json.loads(spath.read_text(encoding="utf-8"))
            if isinstance(custom, dict):
                for k, v in custom.items():
                    if isinstance(v, str) and v:
                        defaults[k] = v
        except Exception:
            pass
    return defaults


def _default_id_for_email(email: str, existing_ids: set[str]) -> str:
    prefix = email.split("@", 1)[0].lower()
    prefix = re.sub(r"[^a-z0-9_-]+", "-", prefix).strip("-") or "acct"
    if prefix not in existing_ids:
        return prefix
    idx = 2
    while f"{prefix}-{idx}" in existing_ids:
        idx += 1
    return f"{prefix}-{idx}"


def load_gacct_registry(home: Path | None = None, sync_back: bool = False) -> list[dict[str, Any]]:
    """Load ~/.config/google-accounts/accounts.json and merge Antigravity slots."""
    rpath = gacct_registry_path(home)
    accounts: list[dict[str, Any]] = []
    if rpath.is_file():
        try:
            raw = json.loads(rpath.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                accounts = [dict(a) for a in raw if isinstance(a, dict) and a.get("email")]
        except Exception:
            accounts = []

    seq = load_sequence(home)
    seq_accounts = seq.get("accounts") or {}
    order = [int(x) for x in (seq.get("sequence") or []) if str(x) in seq_accounts]

    by_email = {str(a["email"]).lower(): a for a in accounts if a.get("email")}
    existing_ids = {str(a.get("id") or "") for a in accounts}
    changed = False

    for num in order:
        meta = seq_accounts[str(num)]
        email = str(meta.get("email") or "").strip().lower()
        if not email:
            continue
        agy_info = {
            "slot": num,
            "alias": meta.get("alias") or email.split("@")[0],
        }
        acct = by_email.get(email)
        if acct is None:
            aid = _default_id_for_email(email, existing_ids)
            existing_ids.add(aid)
            acct = {
                "id": aid,
                "email": email,
                "note": _t("Antigravity CLI 账号", "Antigravity CLI account", home=home),
                "agy": agy_info,
            }
            accounts.append(acct)
            by_email[email] = acct
            changed = True
        else:
            if acct.get("agy") != agy_info:
                acct["agy"] = agy_info
                changed = True

    if changed and sync_back and rpath.is_file():
        save_gacct_registry(accounts, home=home)

    return accounts


def save_gacct_registry(accounts: list[dict[str, Any]], home: Path | None = None) -> None:
    atomic_write_json(gacct_registry_path(home), accounts, mode=0o600)


def resolve_gacct_account(accounts: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Resolve an account by exact id, 1-based index, exact email, or prefix."""
    q = query.strip()
    if not q:
        return None
    ql = q.lower()
    for a in accounts:
        if str(a.get("id") or "").lower() == ql:
            return a
    if q.isdigit():
        idx = int(q)
        if 1 <= idx <= len(accounts):
            return accounts[idx - 1]
    for a in accounts:
        if str(a.get("email") or "").lower() == ql:
            return a
    for a in accounts:
        aid = str(a.get("id") or "").lower()
        email = str(a.get("email") or "").lower()
        alias = str((a.get("agy") or {}).get("alias") or "").lower()
        if aid.startswith(ql) or email.startswith(ql) or (alias and alias.startswith(ql)) or ql in email:
            return a
    return None


def _need_service(acct: dict[str, Any], svc: str, home: Path | None = None) -> dict[str, Any]:
    block = acct.get(svc)
    if not isinstance(block, dict) or not block:
        aid = acct.get("id") or acct.get("email") or "account"
        email = acct.get("email") or ""
        sys.exit(
            _t(
                f"gswap: 账号 '{aid}' ({email}) 尚未绑定 {svc}；请运行: gswap login {aid} {svc}",
                f"gswap: {svc} is not wired for '{aid}' ({email}); run: gswap login {aid} {svc}",
                home=home,
            )
        )
    return block


def _exec_service(cmd: list[str], env: dict[str, str] | None = None) -> None:
    os.execvpe(cmd[0], cmd, {**os.environ, **(env or {})})


def run_colab_service(acct: dict[str, Any], args: list[str], home: Path | None = None) -> None:
    settings = load_gacct_settings(home)
    c = _need_service(acct, "colab", home=home)
    Path(_p(c["sessions"])).parent.mkdir(parents=True, exist_ok=True)
    _exec_service(
        [_p(settings["colab_bin"]), "--auth", "adc", "--config", _p(c["sessions"]), *args],
        {"GOOGLE_APPLICATION_CREDENTIALS": _p(c["adc"])},
    )


def run_drive_service(acct: dict[str, Any], args: list[str], home: Path | None = None) -> None:
    settings = load_gacct_settings(home)
    d = _need_service(acct, "drive", home=home)
    mapped = [d["remote"] + ":" + x[1:] if x.startswith(":") else x for x in args]
    _exec_service([_p(settings["rclone_bin"]), "--config", _p(d["rclone_config"]), *mapped])


def run_gcloud_service(acct: dict[str, Any], args: list[str], home: Path | None = None) -> None:
    settings = load_gacct_settings(home)
    g = _need_service(acct, "gcloud", home=home)
    extra = [f"--configuration={g['configuration']}"] if g.get("configuration") else []
    if g.get("project") and not any(x.startswith("--project") for x in args):
        extra.append(f"--project={g['project']}")
    _exec_service([_p(settings["gcloud_bin"]), *extra, *args], {"CLOUDSDK_CONFIG": _p(g["config_dir"])})


def fetch_colab_usage_summary(acct: dict[str, Any], home: Path | None = None, timeout: int = 25) -> dict[str, Any] | None:
    """Query Colab compute units balance, burn rate, and active assignments."""
    c = acct.get("colab")
    if not isinstance(c, dict) or not c.get("adc") or not c.get("sessions"):
        return None
    settings = load_gacct_settings(home)
    adc_path = Path(_p(c["adc"]))
    sessions_path = Path(_p(c["sessions"]))
    if not adc_path.is_file():
        return {"error": f"missing ADC ({adc_path})"}
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [_p(settings["colab_bin"]), "--auth", "adc", "--config", str(sessions_path), "usage"],
            env={**os.environ, "GOOGLE_APPLICATION_CREDENTIALS": str(adc_path)},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {"error": str(exc)}

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return {"error": lines[-1] if lines else f"exit {proc.returncode}"}

    bal_m = re.search(r"Current balance:\s*([0-9.]+)\s*compute units", out)
    rate_m = re.search(r"Usage rate:\s*([^\r\n]+)", out)
    act_m = re.search(r"Active assignments:\s*(\d+)", out)
    return {
        "balance_cu": float(bal_m.group(1)) if bal_m else None,
        "rate": rate_m.group(1).strip() if rate_m else "0/hr",
        "active_assignments": int(act_m.group(1)) if act_m else 0,
    }


def detect_active_gcloud_config(accounts: list[dict[str, Any]], home: Path | None = None) -> str | None:
    settings = load_gacct_settings(home)
    cdirs = {
        _p(a["gcloud"]["config_dir"])
        for a in accounts
        if isinstance(a.get("gcloud"), dict) and a["gcloud"].get("config_dir")
    }
    if not cdirs:
        cdirs = {_p(settings["gcloud_dir"])}
    for cdir in cdirs:
        af = Path(cdir) / "active_config"
        if af.is_file():
            try:
                return af.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    return None


def switch_gcloud_default(acct: dict[str, Any]) -> bool:
    g = acct.get("gcloud")
    if not isinstance(g, dict) or not g.get("config_dir") or not g.get("configuration"):
        return False
    cdir = Path(_p(g["config_dir"]))
    cdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (cdir / "active_config").write_text(str(g["configuration"]) + "\n", encoding="utf-8")
    return True


def _authorize_colab_adc_inline(
    aid: str,
     adc_path: Path,
    python_bin: str,
    expected_email: str | None = None,
) -> None:
    """Inlined interactive Colab OAuth flow (no external script dependency)."""
    if adc_path.exists():
        raise SystemExit(f"File exists; refusing to replace {adc_path}")
    code = (
        "import json, os, sys, urllib.request\n"
        "from importlib import resources\n"
        "from pathlib import Path\n"
        "from colab_cli.auth import _run_remote_flow\n"
        "path = Path(sys.argv[1]).expanduser()\n"
        "expected = (sys.argv[2] if len(sys.argv) > 2 else '').strip().lower()\n"
        "client = json.loads(resources.files('colab_cli').joinpath('oauth_config.json').read_text())\n"
        "credentials = _run_remote_flow(client)\n"
        "if not credentials.refresh_token:\n"
        "    raise SystemExit('No refresh token received; consent must grant offline access')\n"
        "if expected and getattr(credentials, 'token', None):\n"
        "    req = urllib.request.Request(\n"
        "        'https://www.googleapis.com/oauth2/v2/userinfo',\n"
        "        headers={'Authorization': f'Bearer {credentials.token}'},\n"
        "    )\n"
        "    with urllib.request.urlopen(req, timeout=15) as resp:\n"
        "        info = json.loads(resp.read().decode('utf-8'))\n"
        "    actual = str(info.get('email') or '').strip().lower()\n"
        "    if actual and actual != expected:\n"
        "        raise SystemExit(f'Authorized email {actual} does not match expected {expected}')\n"
        "payload = json.loads(credentials.to_json())\n"
        "payload['type'] = 'authorized_user'\n"
        "path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
        "with os.fdopen(fd, 'w', encoding='utf-8') as handle:\n"
        "    if hasattr(os, 'fchmod'):\n"
        "        os.fchmod(handle.fileno(), 0o600)\n"
        "    json.dump(payload, handle)\n"
        "print('Saved authorized_user ADC (0600): ' + str(path))\n"
    )
    subprocess.run([python_bin, "-c", code, str(adc_path), expected_email or ""], check=True)


def login_service(aid: str, svc: str, home: Path | None = None) -> int:
    settings = load_gacct_settings(home)
    accounts = load_gacct_registry(home)
    a = resolve_gacct_account(accounts, aid)
    if a is None:
        sys.exit(
            _t(
                f"gswap: 请先在 {gacct_registry_path(home)} 中添加账号 '{aid}' (id/email)",
                f"gswap: add '{aid}' to {gacct_registry_path(home)} first (id/email)",
                home=home,
            )
        )

    canonical_id = a["id"]
    if svc == "colab":
        adc_rel = f"{settings['colab_secrets_dir']}/colab-adc-{canonical_id}.json"
        _authorize_colab_adc_inline(
            canonical_id,
            Path(_p(adc_rel)),
            _p(settings["python_bin"]),
            expected_email=a.get("email"),
        )
        a["colab"] = {
            "adc": adc_rel,
            "sessions": f"{settings['colab_sessions_dir']}/{canonical_id}/sessions.json",
            "max_per_gpu": 3,
            "train_pool": False,
        }
    elif svc == "gcloud":
        gcloud_dir = settings["gcloud_dir"]
        gcloud_bin = _p(settings["gcloud_bin"])
        env = {**os.environ, "CLOUDSDK_CONFIG": _p(gcloud_dir)}
        subprocess.run(
            [gcloud_bin, "config", "configurations", "create", canonical_id, "--no-activate"],
            env=env,
            check=False,
        )
        subprocess.run(
            [gcloud_bin, f"--configuration={canonical_id}", "auth", "login", "--no-launch-browser", a["email"]],
            env=env,
            check=True,
        )
        a["gcloud"] = {"config_dir": gcloud_dir, "configuration": canonical_id, "project": None}
    elif svc == "drive":
        existing = a.get("drive") or {}
        remote = existing.get("remote", f"gd-{canonical_id}")
        config = existing.get("rclone_config", settings["rclone_conf"])
        base = [_p(settings["rclone_bin"]), "--config", _p(config)]
        remotes = subprocess.run(base + ["listremotes"], check=True, text=True, stdout=subprocess.PIPE).stdout.splitlines()
        if remote + ":" not in remotes:
            subprocess.run(
                base + ["config", "create", remote, "drive", "scope=drive", "config_is_local=false", "--no-output"],
                check=True,
            )
        print(
            _t(
                f"gswap: 请在浏览器中授权 {a['email']}",
                f"gswap: authorize {a['email']} in the browser",
                home=home,
            ),
            flush=True,
        )
        subprocess.run(base + ["config", "reconnect", remote + ":"], check=True)
        subprocess.run(base + ["lsf", remote + ":", "--max-depth", "1"], check=True, stdout=subprocess.DEVNULL)
        a["drive"] = {"rclone_config": config, "remote": remote}
    elif svc == "agy":
        seq = load_sequence(home)
        save_current_live_to_slot(seq, home=home)
        prev_doc = read_live_cli_token(home)
        for p in (cli_token_path(home), standalone_token_path(home)):
            if p.exists():
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass
        subprocess.run(["agy", "logout"], check=False)
        rc = subprocess.run(["agy", "login"]).returncode
        if rc != 0:
            if prev_doc:
                sync_live_stores(prev_doc, home=home)
            print(
                _t(f"✗ `agy login` 退出码非零: {rc}", f"✗ `agy login` exited with non-zero status: {rc}", home=home),
                file=sys.stderr,
            )
            return rc
        live_doc = read_live_cli_token(home) or read_keyring_token()
        if not live_doc:
            if prev_doc:
                sync_live_stores(prev_doc, home=home)
            print(
                _t("✗ 登录后未检测到新的 Antigravity 凭据", "✗ No new Antigravity credentials detected after login", home=home),
                file=sys.stderr,
            )
            return 1
        logged_email = resolve_email(live_doc, home=home).strip().lower()
        expected_email = str(a.get("email") or "").strip().lower()
        if expected_email and logged_email != expected_email:
            if prev_doc:
                sync_live_stores(prev_doc, home=home)
            print(
                _t(
                    f"✗ 登录邮箱 ({logged_email}) 与目标账号 '{canonical_id}' ({expected_email}) 不一致，已回滚。",
                    f"✗ Logged-in email ({logged_email}) does not match target '{canonical_id}' ({expected_email}); rolled back.",
                    home=home,
                ),
                file=sys.stderr,
            )
            return 1
        num, email, _ = upsert_account(live_doc, alias=canonical_id, make_active=True, home=home)
        doc = load_slot_token(num, email, home=home)
        sync_live_stores(doc, home=home)
        load_gacct_registry(home, sync_back=True)
        print(
            _t(
                f"✓ 已为 {canonical_id} ({a['email']}) 绑定 agy (槽位 #{num})",
                f"✓ agy wired for {canonical_id} ({a['email']}) (slot #{num})",
                home=home,
            )
        )
        return 0
    else:
        sys.exit(
            _t(
                "gswap: 登录服务必须是 colab|drive|gcloud|agy 之一",
                "gswap: login service must be colab|drive|gcloud|agy",
                home=home,
            )
        )

    save_gacct_registry(accounts, home=home)
    print(
        _t(
            f"✓ 已为 {canonical_id} ({a['email']}) 绑定 {svc}",
            f"✓ {svc} wired for {canonical_id} ({a['email']})",
            home=home,
        )
    )
    return 0


def sync_trainpool(home: Path | None = None) -> int:
    settings = load_gacct_settings(home)
    out_path = Path(_p(settings["train_pool_registry"]))
    rows = [
        {
            "id": a["id"],
            "adc_path": a["colab"]["adc"],
            "enabled": True,
            "max_per_gpu": int(a["colab"].get("max_per_gpu", 3)),
        }
        for a in load_gacct_registry(home)
        if a.get("colab") and a["colab"].get("train_pool")
    ]
    atomic_write_json(out_path, rows, mode=0o600)
    print(
        _t(
            f"✓ 已写入 {len(rows)} 个训练池账号至 {out_path}",
            f"✓ wrote {len(rows)} train-pool accounts to {out_path}",
            home=home,
        )
    )
    return 0


def print_gacct_list(home: Path | None = None) -> int:
    accounts = load_gacct_registry(home, sync_back=False)
    seq = load_sequence(home)
    active_slot = detect_active_slot(seq, home=home)
    active_gcloud = detect_active_gcloud_config(accounts, home=home)

    for a in accounts:
        wired = " ".join(s for s in ("colab", "drive", "gcloud", "agy") if a.get(s)) or "-"
        tags: list[str] = []
        if (a.get("colab") or {}).get("train_pool"):
            tags.append("[train-pool]")
        agy_slot = (a.get("agy") or {}).get("slot")
        if agy_slot is not None and str(agy_slot) == str(active_slot):
            tags.append("[active:agy]")
        gcfg = (a.get("gcloud") or {}).get("configuration")
        if gcfg and gcfg == active_gcloud:
            tags.append("[active:gcloud]")
        tag_str = (" " + " ".join(tags)) if tags else ""
        print(f"{a['id']:<10} {a['email']:<26} {wired}{tag_str}")
    return 0


def print_unified_status(
    include_quota: bool = False,
    target: str | None = None,
    as_json: bool = False,
    home: Path | None = None,
) -> int:
    accounts = load_gacct_registry(home, sync_back=False)
    if not accounts:
        data = collect_status(include_quota=include_quota, home=home)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print_status_table(data, include_quota=include_quota, home=home)
        return 0

    seq = load_sequence(home)
    save_current_live_to_slot(seq, home=home)
    active_slot = detect_active_slot(seq, home=home)
    active_gcloud = detect_active_gcloud_config(accounts, home=home)

    filtered = accounts
    if target:
        chosen = resolve_gacct_account(accounts, target)
        if not chosen:
            print(
                _t(
                    f"✗ 未找到匹配的 Google 账号: {target}",
                    f"✗ No matching Google account found: {target}",
                    home=home,
                ),
                file=sys.stderr,
            )
            return 1
        filtered = [chosen]

    colab_futures: dict[int, Any] = {}
    agy_futures: dict[int, Any] = {}
    pool: ThreadPoolExecutor | None = None
    if include_quota:
        pool = ThreadPoolExecutor(max_workers=6)
        for idx, a in enumerate(accounts, start=1):
            if a not in filtered:
                continue
            if a.get("colab"):
                colab_futures[idx] = pool.submit(fetch_colab_usage_summary, a, home)
            agy_slot = (a.get("agy") or {}).get("slot")
            if agy_slot is not None:
                email = a.get("email") or ""

                def _fetch_agy(slot_num: Any = agy_slot, acct_email: str = email) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
                    d = load_slot_token(slot_num, acct_email, home=home)
                    ref, q = fetch_quota_summary(d, home=home)
                    return d, ref, q

                agy_futures[idx] = pool.submit(_fetch_agy)

    out_rows: list[dict[str, Any]] = []
    for idx, a in enumerate(accounts, start=1):
        if a not in filtered:
            continue
        aid = a.get("id") or f"acct-{idx}"
        email = a.get("email") or ""
        agy_meta = a.get("agy") or {}
        agy_slot = agy_meta.get("slot")
        is_agy_active = agy_slot is not None and str(agy_slot) == str(active_slot)
        gblock = a.get("gcloud") or {}
        is_gcloud_active = bool(active_gcloud and gblock.get("configuration") == active_gcloud)
        wired = [s for s in ("agy", "colab", "drive", "gcloud") if a.get(s)]

        row_info: dict[str, Any] = {
            "index": idx,
            "id": aid,
            "email": email,
            "services": wired,
            "active_agy": is_agy_active,
            "active_gcloud": is_gcloud_active,
            "train_pool": bool((a.get("colab") or {}).get("train_pool")),
        }

        if include_quota:
            if idx in colab_futures:
                row_info["colab_usage"] = colab_futures[idx].result()
            if idx in agy_futures and agy_slot is not None:
                try:
                    doc, refreshed, qsummary = agy_futures[idx].result()
                    if refreshed.get("token") != doc.get("token") or refreshed.get("expiry") != doc.get("expiry"):
                        save_slot_token(agy_slot, email, refreshed, home=home)
                        if is_agy_active:
                            sync_live_stores(refreshed, home=home)
                    row_info["agy_quota"] = qsummary
                except Exception as exc:
                    row_info["agy_quota_error"] = str(exc)

        out_rows.append(row_info)

        if as_json:
            continue

        mark = "*" if (is_agy_active or is_gcloud_active) else " "
        svc_str = " ".join(wired) or "-"
        badges: list[str] = []
        if row_info["train_pool"]:
            badges.append("[train-pool]")
        if is_agy_active:
            badges.append(_t("[AGY激活]", "[AGY Active]", home=home))
        if is_gcloud_active:
            badges.append(_t("[GCP激活]", "[GCP Active]", home=home))
        badge_str = (" " + " ".join(badges)) if badges else ""
        print(f"{mark} [{idx}] {aid:<10} {email:<26} ({svc_str}){badge_str}")

        meta_parts: list[str] = []
        if agy_slot is not None:
            tier_lbl = (row_info.get("agy_quota") or {}).get("tier") or ""
            meta_parts.append(f"agy=slot#{agy_slot}" + (f"({tier_lbl})" if tier_lbl else ""))
        if a.get("drive"):
            meta_parts.append(f"drive={a['drive'].get('remote')}:")
        if a.get("gcloud"):
            gcfg = gblock.get("configuration") or "-"
            gproj = gblock.get("project") or "-"
            meta_parts.append(f"gcloud={gcfg}({gproj})")
        if meta_parts:
            print(f"      {' · '.join(meta_parts)}")

        if include_quota:
            cu = row_info.get("colab_usage")
            if isinstance(cu, dict):
                if cu.get("error"):
                    print(
                        _t(
                            f"      - [Colab] 异常: {cu['error']}",
                            f"      - [Colab] Error: {cu['error']}",
                            home=home,
                        )
                    )
                elif cu.get("balance_cu") is not None:
                    print(
                        _t(
                            f"      - {'Colab Compute Units':<22}: 余额 {cu['balance_cu']:7.2f} CU   | "
                            f"速率 {cu['rate']} · 活跃会话 {cu['active_assignments']}",
                            f"      - {'Colab Compute Units':<22}: Balance {cu['balance_cu']:7.2f} CU | "
                            f"Rate {cu['rate']} · Active sessions {cu['active_assignments']}",
                            home=home,
                        )
                    )
            if row_info.get("agy_quota_error"):
                print(
                    _t(
                        f"      - [AGY] 额度查询异常: {row_info['agy_quota_error']}",
                        f"      - [AGY] Quota query error: {row_info['agy_quota_error']}",
                        home=home,
                    )
                )
            elif row_info.get("agy_quota"):
                _print_agy_quota_groups(row_info["agy_quota"].get("groups") or [], home=home)

    if as_json:
        print(json.dumps({"accounts": out_rows}, ensure_ascii=False, indent=2))
    return 0


def use_unified_account(target: str | None = None, service: str = "all", home: Path | None = None) -> int:
    """Switch active default account across agy and/or gcloud."""
    if service not in ("all", "agy", "gcloud"):
        raise AgySwapError(
            _t(
                f"无效的全局切换服务 '{service}' (可选: all, agy, gcloud)",
                f"Invalid global switch service '{service}' (choose from: all, agy, gcloud)",
                home=home,
            )
        )
    if not target:
        if service == "gcloud":
            raise AgySwapError(
                _t(
                    "切换 gcloud 必须指定目标账号，例如: `gswap use <acct> gcloud`",
                    "Switching gcloud requires a target account, e.g.: `gswap use <acct> gcloud`",
                    home=home,
                )
            )
        res = switch_to(None, home=home)
        if res.get("refresh_warning"):
            print(f"! {res['refresh_warning']}", file=sys.stderr)
        print(
            _t(
                f"✓ 已切换 Antigravity CLI 账号 → [{res['slot']}] {res['email']} ({res['alias']})",
                f"✓ Switched Antigravity CLI account → [{res['slot']}] {res['email']} ({res['alias']})",
                home=home,
            )
        )
        return 0

    accounts = load_gacct_registry(home, sync_back=True)
    acct = resolve_gacct_account(accounts, target) if accounts else None
    if acct is None:
        if service == "gcloud":
            raise AgySwapError(
                _t(
                    f"未找到配置了 gcloud 的账号: {target}",
                    f"No gcloud account found matching: {target}",
                    home=home,
                )
            )
        res = switch_to(target, home=home)
        if res.get("refresh_warning"):
            print(f"! {res['refresh_warning']}", file=sys.stderr)
        print(
            _t(
                f"✓ 已切换 Antigravity CLI 账号 → [{res['slot']}] {res['email']} ({res['alias']})",
                f"✓ Switched Antigravity CLI account → [{res['slot']}] {res['email']} ({res['alias']})",
                home=home,
            )
        )
        return 0

    aid = acct.get("id") or acct["email"]
    email = acct["email"]
    switched: list[str] = []

    if service in ("all", "agy"):
        if acct.get("agy"):
            res = switch_to(email, home=home)
            if res.get("refresh_warning"):
                print(f"! {res['refresh_warning']}", file=sys.stderr)
            switched.append(f"agy(slot#{res['slot']})")
        elif service == "agy":
            print(
                _t(
                    f"✗ 账号 '{aid}' ({email}) 尚未绑定 Antigravity CLI，请运行: gswap login {aid} agy",
                    f"✗ Account '{aid}' ({email}) is not wired for Antigravity CLI; run: gswap login {aid} agy",
                    home=home,
                ),
                file=sys.stderr,
            )
            return 1

    if service in ("all", "gcloud"):
        if switch_gcloud_default(acct):
            switched.append(f"gcloud({acct['gcloud']['configuration']})")
        elif service == "gcloud":
            print(
                _t(
                    f"✗ 账号 '{aid}' ({email}) 尚未配置 gcloud，请运行: gswap login {aid} gcloud",
                    f"✗ Account '{aid}' ({email}) is not wired for gcloud; run: gswap login {aid} gcloud",
                    home=home,
                ),
                file=sys.stderr,
            )
            return 1

    if not switched:
        print(
            _t(
                f"✗ 账号 '{aid}' ({email}) 未配置可全局切换的服务 (agy/gcloud)",
                f"✗ Account '{aid}' ({email}) has no globally switchable services (agy/gcloud)",
                home=home,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        _t(
            f"✓ 已切换当前 Google 账号 → {aid} ({email}) [{' + '.join(switched)}]",
            f"✓ Switched active Google account → {aid} ({email}) [{' + '.join(switched)}]",
            home=home,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Interactive TUI Menu (ccswap-style)
# ---------------------------------------------------------------------------

_RAW_KEY_PENDING: dict[int, bytearray] = {}


def _read_single_byte(fd: int) -> bytes:
    buf = _RAW_KEY_PENDING.get(fd)
    if buf:
        b = bytes([buf.pop(0)])
        if not buf:
            _RAW_KEY_PENDING.pop(fd, None)
        return b
    return os.read(fd, 1)


def _has_input_ready(fd: int, timeout: float) -> bool:
    import select

    if _RAW_KEY_PENDING.get(fd):
        return True
    return bool(select.select([fd], [], [], timeout)[0])


def _read_raw_key(fd: int | None = None) -> str:
    """Read a single keypress or ANSI escape sequence directly from the raw TTY fd.

    Must use `os.read(fd, ...)` rather than `sys.stdin.read(1)` because Python's
    `TextIOWrapper` buffers multi-byte escape sequences (`\\x1b[B`), which would
    otherwise cause `select.select` on the underlying fd to miss `[B` and misread
    an arrow key as a bare `ESC`. Also uses `termios.TCSANOW` so queued bytes are
    not flushed when entering raw mode.
    """
    import termios
    import tty

    if fd is None:
        fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd, when=termios.TCSANOW)
        buf = _read_single_byte(fd)
        if not buf:
            return "QUIT"
        if buf == b"\x1b":
            # Read ANSI escape sequence from the kernel fd without swallowing
            # a subsequent regular keystroke (e.g. Esc followed by 'q').
            if _has_input_ready(fd, 0.25):
                nxt = _read_single_byte(fd)
                if nxt in (b"[", b"O"):
                    buf += nxt
                    while _has_input_ready(fd, 0.25):
                        c = _read_single_byte(fd)
                        if not c:
                            break
                        buf += c
                        if 0x40 <= c[0] <= 0x7E:
                            break
                elif nxt:
                    _RAW_KEY_PENDING.setdefault(fd, bytearray()).extend(nxt)
                    return "ESC"
        elif buf[0] >= 0xC0:
            # Read remaining UTF-8 continuation bytes if a non-ASCII key was pressed
            more_len = 1 if buf[0] < 0xE0 else (2 if buf[0] < 0xF0 else 3)
            for _ in range(more_len):
                if _has_input_ready(fd, 0.05):
                    buf += _read_single_byte(fd)

        if buf.startswith((b"\x1b[A", b"\x1bOA", b"\x1b[D", b"\x1bOD")):
            return "UP"
        if buf.startswith((b"\x1b[B", b"\x1bOB", b"\x1b[C", b"\x1bOC")):
            return "DOWN"
        if buf == b"\x1b":
            return "ESC"
        if buf.startswith(b"\x1b"):
            return "IGNORE"
        ch = buf.decode("utf-8", errors="ignore")[:1]
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
            return "QUIT"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def select_from_menu(
    title: str,
    options: list[tuple[str, str]],
    default_idx: int = 0,
    home: Path | None = None,
) -> str | None:
    """Render an interactive menu and return the chosen key.

    Supports both ↑/↓/j/k + Enter on a real TTY and direct shortcut keys (1-9, l, q, b).
    Falls back to line `input()` when termios is unavailable or GSWAP_SIMPLE_MENU=1.
    """
    if not options:
        return None

    use_raw = (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("GSWAP_SIMPLE_MENU") != "1"
    )
    if use_raw:
        try:
            import termios  # noqa: F401
            import tty  # noqa: F401
        except ImportError:
            use_raw = False

    idx = max(0, min(default_idx, len(options) - 1))
    key_map = {k.lower(): i for i, (k, _) in enumerate(options)}

    if not use_raw:
        print(f"\n{title}")
        for key, label in options:
            print(f"  [{key}] {label}")
        try:
            raw = input(
                _t(
                    "请选择 (直接回车默认第 1 项, q 退出): ",
                    "Select an option (Enter for default #1, q to quit): ",
                    home=home,
                )
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return options[idx][0]
        if raw in ("q", "quit", "exit"):
            return None
        if raw in key_map:
            return options[key_map[raw]][0]
        print(
            _t(f"✗ 无效选项: {raw}", f"✗ Invalid choice: {raw}", home=home),
            file=sys.stderr,
        )
        return None

    hint = _t(
        "(↑/↓ 选择, Enter 确认, 或直接按快捷键, q 退出)",
        "(↑/↓ to move, Enter to confirm, or press shortcut key, q to quit)",
        home=home,
    )
    print(f"\n\x1b[1m{title}\x1b[0m \x1b[2m{hint}\x1b[0m")
    for i, (key, label) in enumerate(options):
        if i == idx:
            print(f" \x1b[1;36m❯ [{key}] {label}\x1b[0m")
        else:
            print(f"   [{key}] {label}")

    while True:
        k = _read_raw_key()
        if k == "QUIT" or k.lower() == "q":
            return None
        if k == "ESC":
            # In a submenu (`b` present), Esc goes back; in the main menu, ignore bare Esc
            # so stray escape sequences never accidentally exit the program.
            if "b" in key_map:
                return "b"
            continue
        if k in ("UP", "k", "K"):
            idx = (idx - 1) % len(options)
        elif k in ("DOWN", "j", "J"):
            idx = (idx + 1) % len(options)
        elif k == "ENTER":
            return options[idx][0]
        elif k.lower() in key_map:
            idx = key_map[k.lower()]
            has_prefix_collision = sum(1 for ok in key_map if ok.startswith(k.lower())) > 1
            if not has_prefix_collision:
                # Redraw highlight on chosen row before returning
                sys.stdout.write(f"\x1b[{len(options)}A\r")
                for i, (okey, label) in enumerate(options):
                    sys.stdout.write("\x1b[2K")
                    if i == idx:
                        sys.stdout.write(f" \x1b[1;36m❯ [{okey}] {label}\x1b[0m\n")
                    else:
                        sys.stdout.write(f"   [{okey}] {label}\n")
                sys.stdout.flush()
                return options[idx][0]
        else:
            continue

        sys.stdout.write(f"\x1b[{len(options)}A\r")
        for i, (okey, label) in enumerate(options):
            sys.stdout.write("\x1b[2K")
            if i == idx:
                sys.stdout.write(f" \x1b[1;36m❯ [{okey}] {label}\x1b[0m\n")
            else:
                sys.stdout.write(f"   [{okey}] {label}\n")
        sys.stdout.flush()


def interactive_menu(home: Path | None = None) -> int:
    """Launch the ccswap-style interactive multi-account menu."""
    while True:
        zh = get_lang(home) == "zh"
        print("\n╭──────────────────────────────────────────────────────────────────────────╮")
        if zh:
            print("│  gswap — Google 多账号管理中心 (AGY · Colab · Drive · GCloud)            │")
        else:
            print("│  gswap — Google Multi-Account Center (AGY · Colab · Drive · GCloud)      │")
        print("╰──────────────────────────────────────────────────────────────────────────╯")
        print_unified_status(include_quota=False, home=home)

        seq = load_sequence(home)
        active_slot = detect_active_slot(seq, home=home)
        seq_accounts = seq.get("accounts") or {}
        order = [int(x) for x in (seq.get("sequence") or []) if str(x) in seq_accounts]
        next_hint = ""
        if len(order) >= 2:
            cur_idx = order.index(int(active_slot)) if (active_slot and int(active_slot) in order) else 0
            nxt_slot = order[(cur_idx + 1) % len(order)]
            cur_email = seq_accounts.get(str(order[cur_idx]), {}).get("email", "")
            nxt_email = seq_accounts.get(str(nxt_slot), {}).get("email", "")
            next_hint = f" ({cur_email} → {nxt_email})"

        choice = select_from_menu(
            _t("请选择操作:", "Select an action:", home=home),
            [
                (
                    "1",
                    _t(
                        f"⚡ 快速轮换下一个 AGY 账号{next_hint}",
                        f"⚡ Rotate to next AGY account{next_hint}",
                        home=home,
                    ),
                ),
                (
                    "2",
                    _t(
                        "🔀 指定切换账号 (AGY / GCloud 全局默认账号)",
                        "🔀 Switch active account (AGY / GCloud default)",
                        home=home,
                    ),
                ),
                (
                    "3",
                    _t(
                        "📊 查看实时额度与算力看板 (AGY 5h/周限 + Colab CU)",
                        "📊 Inspect real-time quotas & compute (AGY 5h/weekly + Colab CU)",
                        home=home,
                    ),
                ),
                (
                    "4",
                    _t(
                        "🚀 Colab 算力与 GPU 会话查询 (usage / sessions)",
                        "🚀 Colab compute & GPU sessions (usage / sessions)",
                        home=home,
                    ),
                ),
                (
                    "5",
                    _t(
                        "🔑 账号登录 / 绑定新服务 (agy · colab · drive · gcloud)",
                        "🔑 Login / wire service (agy · colab · drive · gcloud)",
                        home=home,
                    ),
                ),
                (
                    "6",
                    _t(
                        "🔄 同步 Colab 训练池配置 (sync-trainpool)",
                        "🔄 Sync Colab train-pool registry (sync-trainpool)",
                        home=home,
                    ),
                ),
                (
                    "l",
                    _t(
                        "🌐 语言 / Language: 简体中文 (切换为 English)",
                        "🌐 Language / 语言: English (Switch to 简体中文)",
                        home=home,
                    ),
                ),
                ("q", _t("退出 (Quit)", "Quit", home=home)),
            ],
            home=home,
        )

        if choice is None or choice == "q":
            return 0

        if choice == "l":
            new_lang = "en" if get_lang(home) == "zh" else "zh"
            try:
                save_lang_setting(new_lang, home=home)
            except AgySwapError as exc:
                print(f"✗ {exc}", file=sys.stderr)
                continue
            print(
                _t(
                    "✓ 界面语言已切换为: 简体中文 (zh)",
                    "✓ Display language switched to: English (en)",
                    home=home,
                )
            )
            continue

        if choice == "1":
            try:
                use_unified_account(None, "agy", home=home)
            except AgySwapError as exc:
                print(f"✗ {exc}", file=sys.stderr)
            continue

        if choice == "2":
            accounts = load_gacct_registry(home, sync_back=False)
            if not accounts:
                print(_t("尚未配置任何 Google 账号。", "No Google accounts configured yet.", home=home))
                continue
            acct_opts: list[tuple[str, str]] = []
            for idx, a in enumerate(accounts, start=1):
                svcs = [s for s in ("agy", "gcloud") if a.get(s)]
                svc_lbl = (
                    " + ".join(svcs)
                    if svcs
                    else _t("无可切换全局服务", "no switchable global service", home=home)
                )
                tag_lbl = _t(f"[可切: {svc_lbl}]", f"[switchable: {svc_lbl}]", home=home)
                acct_opts.append((str(idx), f"{a['id']:<10} {a['email']:<26} {tag_lbl}"))
            acct_opts.append(("b", _t("返回上一级", "Back", home=home)))

            picked = select_from_menu(
                _t("选择要切换的目标账号:", "Select target account to activate:", home=home),
                acct_opts,
                home=home,
            )
            if not picked or picked == "b" or not picked.isdigit() or not (1 <= int(picked) <= len(accounts)):
                continue
            target_acct = accounts[int(picked) - 1]
            has_agy = bool(target_acct.get("agy"))
            has_gcloud = bool(target_acct.get("gcloud"))
            svc_target = "all"
            if has_agy and has_gcloud:
                s_choice = select_from_menu(
                    _t(
                        f"选择要为 {target_acct['id']} ({target_acct['email']}) 切换的服务范围:",
                        f"Select services to switch for {target_acct['id']} ({target_acct['email']}):",
                        home=home,
                    ),
                    [
                        ("1", _t("全部切换 (AGY + GCloud)", "Switch all (AGY + GCloud)", home=home)),
                        ("2", _t("仅切换 Antigravity CLI (AGY)", "Antigravity CLI only (AGY)", home=home)),
                        ("3", _t("仅切换 Google Cloud (GCloud)", "Google Cloud only (GCloud)", home=home)),
                        ("b", _t("返回", "Back", home=home)),
                    ],
                    home=home,
                )
                if not s_choice or s_choice == "b":
                    continue
                svc_target = {"1": "all", "2": "agy", "3": "gcloud"}.get(s_choice, "all")
            elif has_agy:
                svc_target = "agy"
            elif has_gcloud:
                svc_target = "gcloud"
            try:
                use_unified_account(target_acct["id"], svc_target, home=home)
            except AgySwapError as exc:
                print(f"✗ {exc}", file=sys.stderr)
            continue

        if choice == "3":
            print(
                _t(
                    "\n⏳ 正在并发查询所有账号的 Colab CU 余额与 Antigravity 模型额度...",
                    "\n⏳ Querying Colab CU balances and Antigravity model quotas concurrently...",
                    home=home,
                )
            )
            print_unified_status(include_quota=True, home=home)
            if os.environ.get("GSWAP_SIMPLE_MENU") == "1":
                continue
            try:
                input(_t("\n按回车键返回菜单...", "\nPress Enter to return to menu...", home=home))
            except (EOFError, KeyboardInterrupt):
                return 0
            continue

        if choice == "4":
            accounts = [a for a in load_gacct_registry(home, sync_back=False) if a.get("colab")]
            if not accounts:
                print(_t("尚未配置任何绑定了 Colab 的账号。", "No accounts wired with Colab yet.", home=home))
                continue
            c_opts = [(str(i), f"{a['id']:<10} {a['email']}") for i, a in enumerate(accounts, start=1)]
            c_opts.append(("b", _t("返回上一级", "Back", home=home)))
            c_pick = select_from_menu(
                _t("选择要查看的 Colab 账号:", "Select Colab account:", home=home),
                c_opts,
                home=home,
            )
            if not c_pick or c_pick == "b" or not c_pick.isdigit() or not (1 <= int(c_pick) <= len(accounts)):
                continue
            c_acct = accounts[int(c_pick) - 1]
            act_pick = select_from_menu(
                _t(f"选择 Colab 操作 ({c_acct['id']}):", f"Select Colab action ({c_acct['id']}):", home=home),
                [
                    ("1", _t("查看算力余额与消耗速率 (colab usage)", "Show CU balance & burn rate (colab usage)", home=home)),
                    ("2", _t("查看活跃 GPU 会话列表 (colab sessions)", "List active GPU sessions (colab sessions)", home=home)),
                    ("b", _t("返回", "Back", home=home)),
                ],
                home=home,
            )
            if not act_pick or act_pick == "b" or act_pick not in ("1", "2"):
                continue
            subcmd = "usage" if act_pick == "1" else "sessions"
            settings = load_gacct_settings(home)
            c_cfg = c_acct["colab"]
            Path(_p(c_cfg["sessions"])).parent.mkdir(parents=True, exist_ok=True)
            print()
            subprocess.run(
                [_p(settings["colab_bin"]), "--auth", "adc", "--config", _p(c_cfg["sessions"]), subcmd],
                env={**os.environ, "GOOGLE_APPLICATION_CREDENTIALS": _p(c_cfg["adc"])},
                check=False,
            )
            if os.environ.get("GSWAP_SIMPLE_MENU") != "1":
                try:
                    input(_t("\n按回车键返回菜单...", "\nPress Enter to return to menu...", home=home))
                except (EOFError, KeyboardInterrupt):
                    return 0
            continue

        if choice == "5":
            accounts = load_gacct_registry(home, sync_back=True)
            l_opts = [(str(i), f"{a['id']:<10} {a['email']}") for i, a in enumerate(accounts, start=1)]
            l_opts.extend([
                ("n", _t("➕ 新增 Google 账号到注册表", "➕ Add new Google account to registry", home=home)),
                ("d", _t("🔍 自动扫描导入本机已登录 AGY 凭据 (--discover)", "🔍 Auto-discover local AGY credentials (--discover)", home=home)),
                ("b", _t("返回上一级", "Back", home=home)),
            ])
            l_pick = select_from_menu(
                _t("选择要绑定服务的账号:", "Select account to wire service:", home=home),
                l_opts,
                home=home,
            )
            if not l_pick or l_pick == "b":
                continue
            if l_pick == "d":
                main_agyswap(["add", "--discover"])
                continue
            if l_pick == "n":
                try:
                    new_id = input(
                        _t("请输入新账号简称 ID (如 third / work): ", "Enter new account short ID (e.g. third / work): ", home=home)
                    ).strip()
                    new_email = input(
                        _t("请输入新账号 Google 邮箱: ", "Enter new account Google email: ", home=home)
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    continue
                if not new_id or not new_email:
                    continue
                accounts.append({"id": new_id, "email": new_email, "note": ""})
                save_gacct_registry(accounts, home=home)
                target_id = new_id
            elif l_pick.isdigit() and 1 <= int(l_pick) <= len(accounts):
                target_id = accounts[int(l_pick) - 1]["id"]
            else:
                continue

            svc_pick = select_from_menu(
                _t(
                    f"选择要为 '{target_id}' 授权的服务:",
                    f"Select service to authorize for '{target_id}':",
                    home=home,
                ),
                [
                    ("1", "Antigravity CLI (agy)"),
                    ("2", "Google Colab (colab)"),
                    ("3", "Google Drive (drive / rclone)"),
                    ("4", "Google Cloud (gcloud)"),
                    ("b", _t("返回", "Back", home=home)),
                ],
                home=home,
            )
            svc_map = {"1": "agy", "2": "colab", "3": "drive", "4": "gcloud"}
            if svc_pick in svc_map:
                login_service(target_id, svc_map[svc_pick], home=home)
            continue

        if choice == "6":
            sync_trainpool(home=home)
            continue

    return 0


# ---------------------------------------------------------------------------
# CLI Parsers & Entry Points
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gswap",
        description=_t(
            "Google 多账号 (AGY · Colab · Drive · GCloud) 切换与额度查看工具",
            "Google multi-account (AGY · Colab · Drive · GCloud) switcher & quota tool",
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="switch",
        help=_t(
            "子命令: switch / next / list / status / add / login / remove / lang",
            "Subcommand: switch / next / list / status / add / login / remove / lang",
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help=_t("目标账号序号、邮箱或别名", "Target account slot, email, or alias"),
    )
    parser.add_argument("--alias", help=_t("为账号指定别名", "Assign an alias to the account"))
    parser.add_argument("--from-file", help=_t("从指定 token 文件导入账号", "Import account from a token file"))
    parser.add_argument(
        "--from-keyring",
        action="store_true",
        help=_t("从系统 Keyring 导入账号", "Import account from OS keyring"),
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help=_t("自动扫描当前 CLI token 文件和 Keyring 并导入全部账号", "Auto-discover and import accounts from CLI token file and OS keyring"),
    )
    parser.add_argument(
        "--no-quota",
        action="store_true",
        help=_t("list/status 时跳过实时额度查询", "Skip real-time quota query during list/status"),
    )
    parser.add_argument("--json", action="store_true", help=_t("以 JSON 格式输出", "Output in JSON format"))
    parser.add_argument("--lang", "-L", help=_t("界面语言 (zh 或 en)", "Display language (zh or en)"))
    return parser


def handle_lang_command(arg: str | None = None, home: Path | None = None) -> int:
    if not arg:
        cur = get_lang(home)
        lbl = "简体中文 (zh)" if cur == "zh" else "English (en)"
        print(_t(f"当前语言: {lbl} (使用 `gswap lang en` 或 `gswap lang zh` 切换)", f"Current language: {lbl} (use `gswap lang zh` or `gswap lang en` to switch)", home=home))
        return 0
    try:
        norm = save_lang_setting(arg, home=home)
    except AgySwapError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(
        _t(
            f"✓ 默认语言已保存为: 简体中文 ({norm})",
            f"✓ Default language saved as: English ({norm})",
            home=home,
        )
    )
    return 0


def main_agyswap(argv: list[str] | None = None) -> int:
    try:
        if argv is not None:
            argv = _strip_lang_flags(list(argv))
        parser = build_parser()
        args = parser.parse_args(argv)
        if getattr(args, "lang", None):
            set_lang(args.lang)
    except AgySwapError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    cmd = (args.command or "switch").strip().lower()

    try:
        if cmd == "lang":
            return handle_lang_command(args.target)

        if cmd in ("list", "ls", "status", "quota"):
            data = collect_status(include_quota=not args.no_quota)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print_status_table(data, include_quota=not args.no_quota)
            return 0

        if cmd == "add":
            added: list[dict[str, Any]] = []
            if args.discover:
                kr_doc = read_keyring_token()
                if kr_doc:
                    num, email, is_new = upsert_account(kr_doc, make_active=False)
                    added.append({"slot": num, "email": email, "source": "keyring", "new": is_new})
                live_doc = read_live_cli_token()
                if live_doc:
                    num, email, is_new = upsert_account(live_doc, make_active=True)
                    added.append({"slot": num, "email": email, "source": "cli_file", "new": is_new})
                if not added:
                    raise AgySwapError(
                        _t(
                            "未在 CLI token 文件或 Keyring 中发现有效账号",
                            "No valid account found in CLI token file or OS keyring",
                        )
                    )
            elif args.from_keyring:
                kr_doc = read_keyring_token()
                if not kr_doc:
                    raise AgySwapError(
                        _t(
                            "Keyring 中未找到 gemini/antigravity 凭据",
                            "No gemini/antigravity credentials found in OS keyring",
                        )
                    )
                num, email, is_new = upsert_account(kr_doc, alias=args.alias)
                added.append({"slot": num, "email": email, "source": "keyring", "new": is_new})
            elif args.from_file:
                raw = Path(args.from_file).expanduser().read_text(encoding="utf-8")
                doc = parse_token_blob(raw)
                num, email, is_new = upsert_account(doc, alias=args.alias)
                added.append({"slot": num, "email": email, "source": args.from_file, "new": is_new})
            else:
                live_doc = read_live_cli_token()
                if not live_doc:
                    raise AgySwapError(
                        _t(
                            "未找到当前 Antigravity CLI 登录凭据，请先运行 `agy login` 或使用 `--from-keyring`",
                            "No active Antigravity CLI credentials found; run `agy login` first or pass `--from-keyring`",
                        )
                    )
                num, email, is_new = upsert_account(live_doc, alias=args.alias, make_active=True)
                added.append({"slot": num, "email": email, "source": "cli_file", "new": is_new})

            if args.json:
                print(json.dumps({"ok": True, "added": added}, ensure_ascii=False, indent=2))
            else:
                for item in added:
                    if get_lang() == "zh":
                        action = "已添加" if item["new"] else "已更新"
                        print(f"✓ {action} 槽位 [{item['slot']}] {item['email']} (来源: {item['source']})")
                    else:
                        action = "Added" if item["new"] else "Updated"
                        print(f"✓ {action} slot [{item['slot']}] {item['email']} (source: {item['source']})")
            return 0

        if cmd == "login":
            seq = load_sequence()
            save_current_live_to_slot(seq)
            for p in (cli_token_path(), standalone_token_path()):
                if p.exists():
                    try:
                        os.chmod(p, 0o600)
                    except OSError:
                        pass
            subprocess.run(["agy", "logout"], check=False)
            rc = subprocess.run(["agy", "login"]).returncode
            if rc != 0:
                raise AgySwapError(
                    _t(f"`agy login` 退出码非零: {rc}", f"`agy login` exited with non-zero status: {rc}")
                )
            live_doc = read_live_cli_token() or read_keyring_token()
            if not live_doc:
                raise AgySwapError(
                    _t("登录后未检测到新的 Antigravity 凭据", "No new Antigravity credentials detected after login")
                )
            num, email, is_new = upsert_account(live_doc, alias=args.alias, make_active=True)
            doc = load_slot_token(num, email)
            sync_live_stores(doc)
            print(
                _t(
                    f"✓ 已登录并保存至槽位 [{num}] {email}",
                    f"✓ Logged in and saved to slot [{num}] {email}",
                )
            )
            return 0

        if cmd in ("remove", "rm", "delete"):
            if not args.target:
                raise AgySwapError(
                    _t(
                        "请指定要移除的账号序号或邮箱，例如: `gswap remove 2`",
                        "Specify the slot number or email to remove, e.g.: `gswap remove 2`",
                    )
                )
            res = remove_slot(args.target)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(
                    _t(
                        f"✓ 已移除槽位 [{res['removed_slot']}] {res['email']}",
                        f"✓ Removed slot [{res['removed_slot']}] {res['email']}",
                    )
                )
            return 0

        if cmd in ("switch", "sw", "next"):
            res = switch_to(args.target if cmd != "next" else None)
        else:
            res = switch_to(cmd)

        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            if res.get("refresh_warning"):
                print(f"! {res['refresh_warning']}", file=sys.stderr)
            print(
                _t(
                    f"✓ 已切换 Antigravity CLI 账号 → [{res['slot']}] {res['email']} ({res['alias']})",
                    f"✓ Switched Antigravity CLI account → [{res['slot']}] {res['email']} ({res['alias']})",
                )
            )
        return 0

    except AgySwapError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1


def main_gswap(argv: list[str]) -> int:
    try:
        argv = _strip_lang_flags(argv)
    except AgySwapError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if not argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return interactive_menu()
        return print_unified_status(include_quota=False)

    first = argv[0]
    if first in ("-h", "--help", "help"):
        print(get_help_text())
        return 0
    if first == "lang":
        return handle_lang_command(argv[1] if len(argv) > 1 else None)
    if first in ("menu", "-i", "--interactive"):
        return interactive_menu()
    if first == "list":
        return print_gacct_list()
    if first in ("status", "-s", "--status"):
        with_q = "-q" in argv[1:] or "--quota" in argv[1:]
        as_json = "--json" in argv[1:]
        rest = [x for x in argv[1:] if x not in ("-q", "--quota", "--json", "--no-quota")]
        return print_unified_status(include_quota=with_q, target=rest[0] if rest else None, as_json=as_json)
    if first in ("quota", "-q", "--quota"):
        as_json = "--json" in argv[1:]
        rest = [x for x in argv[1:] if x != "--json"]
        return print_unified_status(include_quota=True, target=rest[0] if rest else None, as_json=as_json)
    if first == "sync-trainpool":
        return sync_trainpool()
    if first == "login" and len(argv) == 3 and not argv[1].startswith("-"):
        return login_service(argv[1], argv[2])
    if first in ("use", "switch", "sw", "next"):
        try:
            if first == "next" or len(argv) < 2:
                return use_unified_account(None, "agy")
            svc = argv[2] if len(argv) > 2 else "all"
            return use_unified_account(argv[1], svc)
        except AgySwapError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    if first in ("add", "remove", "rm", "delete"):
        return main_agyswap(argv)

    # Account-scoped dispatch: `gswap <id> colab|usage|drive|gcloud|agy ...`
    accounts = load_gacct_registry(sync_back=True)
    acct = resolve_gacct_account(accounts, first)
    if acct is None:
        sys.exit(
            _t(
                f"gswap: 未知账号 '{first}' (请查看: gswap list)",
                f"gswap: unknown account '{first}' (see: gswap list)",
            )
        )

    if len(argv) == 1:
        try:
            return use_unified_account(acct["id"], "all")
        except AgySwapError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1

    svc, rest = argv[1], argv[2:]
    if svc == "colab":
        run_colab_service(acct, rest)
    elif svc == "usage":
        run_colab_service(acct, ["usage"])
    elif svc == "drive":
        run_drive_service(acct, rest)
    elif svc == "gcloud":
        run_gcloud_service(acct, rest)
    elif svc == "agy":
        rc = use_unified_account(acct["id"], "agy")
        if rc == 0 and rest:
            agy_bin = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
            _exec_service([agy_bin, *rest])
        return rc
    else:
        sys.exit(
            _t(
                f"gswap: 未知服务 '{svc}' (可选: colab|usage|drive|gcloud|agy)",
                f"gswap: unknown service '{svc}' (colab|usage|drive|gcloud|agy)",
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    # When called programmatically by unit tests (`main([...])`), preserve agyswap CLI parser
    if argv is not None:
        return main_agyswap(argv)
    prog = Path(sys.argv[0]).name.lower()
    if prog in ("aswap", "agyswap", "agyswap.py"):
        return main_agyswap(sys.argv[1:])
    return main_gswap(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
