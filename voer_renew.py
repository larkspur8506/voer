#!/usr/bin/env python3
"""
Voer.host 免费服务器会话续期（Playwright 版）

原理：免费档会话制（默认 4h），续期需看完 3 个 Google 激励广告 -> +4h。
按钮在跨进程 iframe（wormies.voer.host / googleads.g.doubleclick.net）里，
必须用 Playwright（原生支持 OOPIF）才能点到，Selenium/JS 无法穿透。

限制：每 UTC 日最多 4 次、每会话最多 4 次（每次 +4h）。

优先读取环境变量（适合 GitHub Actions / Docker / cron）：
    VOER_SERVER_ID        服务器 UUID（必须）
    VOER_EMAIL            登录邮箱（优先使用邮箱密码登录）
    VOER_PASSWORD         登录密码
    VOER_TOKEN            Cookie 里的 token JWT（邮箱登录失败时回退使用）
    TELEGRAM_BOT_TOKEN    Telegram Bot Token（可选，用于通知）
    TELEGRAM_CHAT_ID      Telegram Chat ID（可选，用于通知）

也支持本地 config.json（环境变量优先级更高）。

VPS / CI 无图形界面时必须用虚拟显示：
    xvfb-run -a python3 voer_renew.py

用法：
    python3 voer_renew.py            关机则开机；有次数则续期；无次数则跳过
    python3 voer_renew.py --status   只看当前状态，不看广告
"""
import json
import os
import sys
import time
import pathlib
import urllib.request
import urllib.error
import urllib.parse
import base64
import mimetypes
import re
from datetime import datetime, timezone, timedelta

BASE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "server_id": "在这里填服务器 UUID（面板地址 /panel/server/ 后面那串）",
    "token": "在这里填浏览器 Cookie 里 voer.host 的 token 值（JWT）",
    "email": "",          # 可选；token 过期时用邮箱+密码登录并自动刷新 token
    "password": "",       # 可选；对应 VOER_PASSWORD
    "ads_per_extension": 3,
    "ad_duration_sec": 32,
    "extensions_per_run": 4,   # 单次运行内最多连续续期几次（受平台每日/每会话 4 次上限约束）
    "restart_if_stopped": True,  # 关机/离线时先重启，就绪后再续期
    "headless": False,
    "use_system_chrome": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tg_title": "Godlike 续期通知",   # TG 通知标题，可用环境变量 TG_TITLE 覆盖
}

from playwright.sync_api import sync_playwright

MAX_DAILY_EXTENSIONS = 4
MAX_SESSION_EXTENSIONS = 4

RUNNING_STATUSES = {"running", "online"}
STOPPED_STATUSES = {"stopped", "offline"}
CRASHED_STATUSES = {"crashed", "error", "provisioning_error", "supervisor_error"}
STARTING_STATUSES = {
    "starting",
    "provisioning",
    "pending",
    "starting_node",
    "restarting",
    "migrating",
}
STOPPING_STATUSES = {"stopping"}

# ---------------------------------------------------------------------------
# 网站访问解锁弹窗（登录后偶现）：独立标签集，与开机广告 / Extend 续期 3 广告完全隔离。
# 该广告只是「网站级 24h 访问解锁」，绝不计入任何 adsCompleted / 广告计数。
# 检测文案来自实际页面确认的中文原文；英文文案待真实 DOM 确认后再补充。
SITE_UNLOCK_DETECT = [
    "解锁更多内容",
    "请做出选择以便继续访问此网站上的内容",
    "获得 24 小时的网站级访问权限",
]
SITE_UNLOCK_CLICK = [
    "观看一则短广告",
]

# ---------------------------------------------------------------------------
# 「区域不可用」提示检测（API 主检测 + UI 辅助检测；命中后优先自动换区，无候选才终止）。
# UI 文案来自 Voer 前端 bundle（ServerDetail 组件）确认的原文，仅作辅助检测；
# 主检测为 API 字段（见 _api_region_unavailable）。绝不用于 UI 点击区域按钮。
REGION_SHORTAGE_DETECT = [
    "Region unavailable",
    "No providers are currently available in this region",
    "No providers are currently available in the selected region.",
    "No other regions currently have capacity",
]

# 广告播放结束后的手动关闭按钮（网站解锁 / 开机 / 续期广告共用同一套，不重复定义）。
# 注意：绝对不要把 "Close ad gate"（关闭整个广告门、会中止 Start 流程）加入此列表。
AD_CLOSE_LABELS = [
    "Close",
    "關閉",
    "关闭",
    "×",
    "X",
    "Done",
    "完成",
]

# live.regionUnavailable / regionCapacityLow 的有效期窗口（与前端 ct() 的 300 秒窗口一致）
REGION_UNAVAILABLE_WINDOW_SEC = 300

# 单次运行内最大自动换区次数（仅防止无限循环，不是区域优先级）
MAX_REGION_SWITCHES = 3


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# ---------------------------------------------------------------------------
# Telegram 通知
# ---------------------------------------------------------------------------
def _tg_enabled(cfg) -> bool:
    return bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"))


def tg_send_message(cfg, text: str) -> bool:
    """发送纯文本消息到 Telegram。"""
    if not _tg_enabled(cfg):
        return False
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        api,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            if data.get("ok"):
                log("Telegram 文本通知已发送")
                return True
            log(f"Telegram 发送失败: {data}")
            return False
    except Exception as e:
        log(f"Telegram 发送异常: {e}")
        return False


def tg_send_photo(cfg, photo_path: pathlib.Path, caption: str = "") -> bool:
    """发送图片（截图）到 Telegram。"""
    if not _tg_enabled(cfg):
        return False
    if not photo_path.exists():
        log(f"截图不存在，跳过发图: {photo_path}")
        return False
    token = cfg["telegram_bot_token"]
    chat_id = str(cfg["telegram_chat_id"])
    api = f"https://api.telegram.org/bot{token}/sendPhoto"

    boundary = f"----VoerBoundary{int(time.time())}"
    filename = photo_path.name
    file_data = photo_path.read_bytes()
    mime = mimetypes.guess_type(filename)[0] or "image/png"

    parts = []
    # chat_id
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n".encode()
    )
    # caption
    if caption:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}\r\n".encode()
        )
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
            f"HTML\r\n".encode()
        )
    # photo
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
        + file_data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        api,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
            if data.get("ok"):
                log("Telegram 截图已发送")
                return True
            log(f"Telegram 发图失败: {data}")
            return False
    except Exception as e:
        log(f"Telegram 发图异常: {e}")
        return False


def notify(cfg, title: str, lines: list, photo: pathlib.Path | None = None):
    """统一通知入口：有 TG 配置就发，没有就只打日志。"""
    text = f"<b>{title}</b>\n" + "\n".join(lines)
    log("通知内容:\n" + text.replace("<b>", "").replace("</b>", ""))
    if not _tg_enabled(cfg):
        log("未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳过 TG 通知")
        return
    if photo and photo.exists():
        # 图片 caption 最长约 1024，超长则先发图再发文字
        if len(text) <= 1000:
            tg_send_photo(cfg, photo, caption=text)
        else:
            tg_send_photo(cfg, photo, caption=title)
            tg_send_message(cfg, text)
    else:
        tg_send_message(cfg, text)


# ---------------------------------------------------------------------------
# 通知内容格式化（Godlike 风格）
# ---------------------------------------------------------------------------
def fmt_local_time(dt=None) -> str:
    """本地时间，格式 2026-09-14 11:10:00。"""
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds) -> str:
    """把秒数格式化成 23h 59m / 3h 05m。"""
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def parse_iso(iso_str):
    """解析 ISO 时间为带时区的 datetime；失败返回 None。"""
    if not iso_str:
        return None
    try:
        t = str(iso_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def seconds_until(iso_str) -> int | None:
    """距离某个 ISO 时间还有多少秒（已过期返回 0，解析失败返回 None）。"""
    dt = parse_iso(iso_str)
    if dt is None:
        return None
    return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))


def is_expired(iso_str) -> bool:
    """会话是否已到期。无法解析时视为未到期（避免误续期）。"""
    dt = parse_iso(iso_str)
    if dt is None:
        return False
    return dt <= datetime.now(timezone.utc)


def fmt_next_renewal(iso_str) -> str:
    """下次续期准确时间：绝对时间（UTC + 本地）+ 剩余时长。"""
    dt = parse_iso(iso_str)
    if dt is None:
        return "—"
    now = datetime.now(timezone.utc)
    sec = max(0, int((dt - now).total_seconds()))
    utc_s = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    # 本地时区：优先 Asia/Shanghai 展示，失败则用系统本地
    try:
        from zoneinfo import ZoneInfo
        local_s = dt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S CST")
    except Exception:
        local_s = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    if sec <= 0:
        return f"{utc_s} / {local_s}（已到期，可立即续期）"
    return f"{utc_s} / {local_s}（剩余 {fmt_duration(sec)}）"


def status_text(status) -> str:
    """把服务器 status 映射成中文说明。"""
    s = (status or "").lower()
    if s in ("running", "online"):
        return "✅ 服务器已在运行中，无需开机"
    if s in ("stopped", "offline"):
        return "⏹️ 服务器已停止"
    if s in ("starting", "provisioning", "pending", "starting_node"):
        return "🔄 服务器启动中"
    if s in ("restarting", "migrating"):
        return "🔄 服务器重启中"
    if s == "maintenance":
        return "🛠️ 系统维护中"
    if s in ("crashed", "error", "provisioning_error", "supervisor_error"):
        return "❌ 服务器异常"
    return f"ℹ️ {status or '未知'}"


def account_email_from_server(server) -> str:
    """从服务器信息里取账号邮箱（server.access.owner.email）。"""
    if not server:
        return ""
    owner = (server.get("access") or {}).get("owner") or {}
    if isinstance(owner, dict):
        for k in ("email", "displayName", "username"):
            if owner.get(k):
                return str(owner[k])
    for k in ("ownerEmail", "email"):
        if server.get(k):
            return str(server[k])
    return ""


def fetch_account_email(cfg) -> str:
    """调用 /api/auth/me 取账号邮箱（失败不影响续期）。"""
    req = urllib.request.Request(
        "https://voer.host/api/auth/me",
        headers={
            "Cookie": f"token={cfg['token']}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        user = data.get("user") or {}
        return str(
            user.get("email") or user.get("displayName") or user.get("username") or ""
        )
    except Exception as e:
        log(f"获取账号信息失败（不影响续期）: {e}")
        return ""


def notify_godlike(cfg, account, server_id, result, uptime_sec, status, photo=None, remaining_text=""):
    """按固定模板发送续期通知；若传入 photo 则附带真实面板截图。"""
    lines = [
        f"⏰运行时间: {fmt_local_time()}",
        f"🖥️账号: {account or '—'}",
        f"🖥️服务器: {server_id}",
        f"🔢下次可续期: {fmt_duration(uptime_sec)}",
        f"🔢可续期次数: {remaining_text or '—'}",
        f"📊续期结果: {result}",
        f"📊开机状态: {status_text(status)}",
    ]
    shot = None
    if photo is not None:
        shot = photo if isinstance(photo, pathlib.Path) else pathlib.Path(photo)
        if not shot.exists():
            log(f"通知截图不存在: {shot}")
            shot = None
    notify(cfg, cfg.get("tg_title") or "Godlike 续期通知", lines, photo=shot)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _jwt_hint(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return "空"
    parts = t.split(".")
    hint = f"长度={len(t)}, 段数={len(parts)}, 开头={t[:8]}..., 结尾=...{t[-6:]}"
    if len(parts) != 3:
        hint += "  【警告：标准 JWT 应有 3 段用 . 分隔，可能复制不完整】"
    if not t.startswith("eyJ"):
        hint += "  【警告：正常 JWT 一般以 eyJ 开头】"
    try:
        if len(parts) >= 2:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(pad))
            exp = payload.get("exp")
            if exp:
                import datetime

                exp_dt = datetime.datetime.utcfromtimestamp(exp)
                now = datetime.datetime.utcnow()
                if exp_dt < now:
                    hint += f"  【已过期！过期时间 UTC {exp_dt.isoformat()}Z】"
                else:
                    left = exp_dt - now
                    hours = int(left.total_seconds() // 3600)
                    hint += f"  【未过期，剩余约 {hours} 小时，过期 UTC {exp_dt.isoformat()}Z】"
    except Exception:
        pass
    return hint



def _jwt_exp(token: str):
    """解析 JWT exp（UTC datetime）；失败返回 None。"""
    t = (token or "").strip()
    parts = t.split(".")
    if len(parts) < 2:
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except Exception:
        return None


def token_looks_valid(token: str, skew_sec: int = 120) -> bool:
    """仅根据 JWT 形态与 exp 判断是否仍可用（不发起网络请求）。"""
    t = (token or "").strip()
    if not t or "在这里填" in t or len(t) < 20:
        return False
    if t.count(".") != 2 or not t.startswith("eyJ"):
        return False
    exp = _jwt_exp(t)
    if exp is None:
        # 解不出 exp 时仍尝试使用（由 API 再验证）
        return True
    return exp > datetime.now(timezone.utc) + timedelta(seconds=skew_sec)


def probe_token(cfg) -> bool:
    """用 /api/auth/me 探测 token 是否真正可用。"""
    token = (cfg.get("token") or "").strip()
    if not token_looks_valid(token):
        return False
    url = "https://voer.host/api/auth/me"
    req = urllib.request.Request(
        url,
        headers={
            "Cookie": f"token={token}",
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status != 200:
                return False
            raw = r.read().decode(errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            return bool(data.get("user") or data.get("email") or data.get("id") or data)
    except urllib.error.HTTPError as e:
        log(f"token 探测失败: HTTP {e.code}")
        return False
    except Exception as e:
        log(f"token 探测异常: {e}")
        return False


def persist_token(cfg, new_token: str) -> None:
    """把新 token 写回内存 / config.json / GITHUB_ENV（无法改 GitHub Secrets）。"""
    new_token = (new_token or "").strip()
    if not new_token:
        return
    cfg["token"] = new_token
    os.environ["VOER_TOKEN"] = new_token
    log(f"已更新内存中的 VOER_TOKEN: {_jwt_hint(new_token)}")

    # 本地 config.json
    try:
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["token"] = new_token
            CONFIG_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
            log(f"已写入 {CONFIG_PATH.name} 的 token 字段")
    except Exception as e:
        log(f"写入 config.json 失败（不影响续期）: {e}")

    # GitHub Actions：写入 GITHUB_ENV，供同 job 后续步骤使用
    gh_env = os.environ.get("GITHUB_ENV", "").strip()
    if gh_env:
        try:
            with open(gh_env, "a", encoding="utf-8") as f:
                f.write(f"VOER_TOKEN={new_token}\n")
            log("已写入 GITHUB_ENV（同 job 后续步骤可读取新 token）")
        except Exception as e:
            log(f"写入 GITHUB_ENV 失败（不影响续期）: {e}")


def _sb_challenge_visible(sb) -> bool:
    try:
        src = sb.get_page_source() or ""
        low = src.lower()
        return (
            "verify you are human" in low
            or "security verification" in low
            or "cf-turnstile" in low
            or "challenge-platform" in low
            or "turnstile" in low and "cloudflare" in low
        )
    except Exception:
        return False


def _sb_handle_turnstile(sb, max_retry: int = 4) -> bool:
    """参考 SkyMC：SeleniumBase UC 点击 Cloudflare Turnstile。"""
    if not _sb_challenge_visible(sb):
        # 即使不可见也尝试一次（有时 widget 已渲染）
        try:
            sb.uc_gui_click_captcha()
            time.sleep(3)
        except Exception:
            pass
        return True
    log("检测到 Cloudflare Turnstile，开始绕过…")
    for i in range(max_retry):
        log(f"  Turnstile 第 {i + 1}/{max_retry} 次尝试")
        try:
            sb.uc_gui_click_captcha()
            log("  已调用 uc_gui_click_captcha")
            time.sleep(5)
            if not _sb_challenge_visible(sb):
                log("  Turnstile 已通过")
                return True
        except Exception as e:
            log(f"  uc_gui_click_captcha 异常: {e}")
        time.sleep(2)
    log("  Turnstile 可能未完全通过，继续尝试登录")
    return False


def login_with_password(cfg) -> str | None:
    """邮箱+密码登录 voer.host，绕过 Turnstile，返回新 token；失败返回 None。

    使用 SeleniumBase UC 模式（与 SkyMC 脚本同一套思路）。
    """
    email = (cfg.get("email") or "").strip()
    password = (cfg.get("password") or "").strip()
    if not email or not password:
        log("未配置 VOER_EMAIL / VOER_PASSWORD，无法邮箱登录")
        return None

    try:
        from seleniumbase import SB
    except ImportError:
        log("未安装 seleniumbase，无法邮箱登录。请 pip install seleniumbase")
        return None

    log(f"使用邮箱密码登录: {email}")
    headless = bool(cfg.get("headless", False))
    # UC 模式在无头环境需配合 xvfb；与续期一致默认非 headless
    sb_kwargs = {"uc": True, "headless": headless, "locale_code": "en"}
    new_token = None

    try:
        with SB(**sb_kwargs) as sb:
            try:
                sb.uc_open_with_reconnect("https://voer.host/login", reconnect_time=6)
            except Exception:
                sb.open("https://voer.host/login")
            try:
                sb.wait_for_ready_state_complete()
            except Exception:
                pass
            time.sleep(3)
            _sb_handle_turnstile(sb)
            time.sleep(1)

            # 填写邮箱
            filled_email = False
            for sel in (
                "#login-email",
                'input[name="email"]',
                'input[type="email"]',
                'input[autocomplete="email"]',
            ):
                try:
                    sb.wait_for_element_visible(sel, timeout=8)
                    sb.clear(sel)
                    sb.type(sel, email)
                    filled_email = True
                    log(f"已填写邮箱（{sel}）")
                    break
                except Exception:
                    continue
            if not filled_email:
                try:
                    sb.execute_script(
                        """
                        var v = arguments[0];
                        var sels = ['#login-email','input[name="email"]','input[type="email"]'];
                        for (var i=0;i<sels.length;i++){
                          var el = document.querySelector(sels[i]);
                          if(!el) continue;
                          el.focus(); el.value=v;
                          el.dispatchEvent(new Event('input',{bubbles:true}));
                          el.dispatchEvent(new Event('change',{bubbles:true}));
                          return sels[i];
                        }
                        return null;
                        """,
                        email,
                    )
                    filled_email = True
                    log("已通过 JS 填写邮箱")
                except Exception as e:
                    log(f"填写邮箱失败: {e}")
            if not filled_email:
                log("无法填写邮箱")
                return None

            time.sleep(0.5)

            # 填写密码
            filled_pw = False
            for sel in (
                "#login-password",
                'input[name="password"]',
                'input[type="password"]',
            ):
                try:
                    sb.wait_for_element_visible(sel, timeout=8)
                    sb.clear(sel)
                    sb.type(sel, password)
                    filled_pw = True
                    log(f"已填写密码（{sel}）")
                    break
                except Exception:
                    continue
            if not filled_pw:
                try:
                    sb.execute_script(
                        """
                        var v = arguments[0];
                        var sels = ['#login-password','input[name="password"]','input[type="password"]'];
                        for (var i=0;i<sels.length;i++){
                          var el = document.querySelector(sels[i]);
                          if(!el) continue;
                          el.focus(); el.value=v;
                          el.dispatchEvent(new Event('input',{bubbles:true}));
                          el.dispatchEvent(new Event('change',{bubbles:true}));
                          return sels[i];
                        }
                        return null;
                        """,
                        password,
                    )
                    filled_pw = True
                    log("已通过 JS 填写密码")
                except Exception as e:
                    log(f"填写密码失败: {e}")
            if not filled_pw:
                log("无法填写密码")
                return None

            time.sleep(1)
            _sb_handle_turnstile(sb)
            time.sleep(2)

            # 点击登录
            clicked = False
            for sel in (
                'button:contains("Sign in")',
                'button:contains("Login")',
                'button:contains("登录")',
                'button[type="submit"]',
            ):
                try:
                    if sb.is_element_visible(sel):
                        sb.uc_click(sel)
                        log(f"已点击登录（{sel}）")
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                try:
                    sb.execute_script(
                        """
                        var btns = document.querySelectorAll('button');
                        for (var i=0;i<btns.length;i++){
                          var t=(btns[i].innerText||'').toLowerCase();
                          if(t.indexOf('sign in')>=0 || t.indexOf('login')>=0 || t.indexOf('登录')>=0){
                            btns[i].click(); return true;
                          }
                        }
                        var s=document.querySelector('button[type="submit"]');
                        if(s){ s.click(); return true; }
                        return false;
                        """
                    )
                    clicked = True
                    log("已通过 JS 点击登录")
                except Exception as e:
                    log(f"点击登录失败: {e}")
                    return None

            # 等待跳转 / 处理二次验证
            for attempt in range(12):
                time.sleep(2)
                if _sb_challenge_visible(sb):
                    log("登录后仍见 Turnstile，再次处理…")
                    _sb_handle_turnstile(sb, max_retry=3)
                url = (sb.get_current_url() or "").lower()
                # 读 cookie
                try:
                    cookies = sb.driver.get_cookies()
                except Exception:
                    cookies = []
                for c in cookies:
                    if c.get("name") == "token" and c.get("value"):
                        new_token = c["value"].strip()
                        break
                if new_token and ("panel" in url or "login" not in url):
                    log(f"登录成功，已取得 token（URL={sb.get_current_url()}）")
                    break
                if new_token and attempt >= 3:
                    # 有 token 即使还在中间页也接受
                    log("已取得 token cookie")
                    break
                if attempt == 5:
                    # 再点一次登录
                    try:
                        sb.uc_click('button[type="submit"]')
                    except Exception:
                        pass
            if not new_token:
                log(f"登录后未拿到 token，当前 URL: {sb.get_current_url()}")
                try:
                    sb.save_screenshot("login_failed.png")
                    log("已保存 login_failed.png")
                except Exception:
                    pass
                return None
    except Exception as e:
        log(f"邮箱登录异常: {e}")
        return None

    return new_token


def ensure_token(cfg) -> bool:
    """优先邮箱密码登录；失败再使用 VOER_TOKEN。"""
    email = (cfg.get("email") or "").strip()
    password = (cfg.get("password") or "").strip()
    token = (cfg.get("token") or "").strip()
    if token and "在这里填" in token:
        token = ""

    # 1) 优先邮箱密码
    if email and password:
        log(f"优先使用邮箱密码登录: {email}")
        new_token = login_with_password(cfg)
        if new_token:
            persist_token(cfg, new_token)
            if probe_token(cfg):
                log("邮箱登录成功，token 探测通过")
                return True
            log("邮箱登录已取得 token，/api/auth/me 探测未通过（仍将尝试使用）")
            return True
        log("邮箱密码登录失败，回退到 VOER_TOKEN")
    else:
        log("未配置 VOER_EMAIL / VOER_PASSWORD，跳过邮箱登录")

    # 2) 回退 VOER_TOKEN
    if token:
        cfg["token"] = token
        if probe_token(cfg):
            log(f"VOER_TOKEN 有效: {_jwt_hint(token)}")
            return True
        if token_looks_valid(token):
            log(f"VOER_TOKEN JWT 未过期，尝试直接使用: {_jwt_hint(token)}")
            return True
        log(f"VOER_TOKEN 无效或已过期: {_jwt_hint(token)}")
    else:
        log("未配置可用的 VOER_TOKEN")

    log("无法通过邮箱密码或 VOER_TOKEN 完成认证")
    return False


def today_used(server: dict) -> int:
    """返回「今日（UTC）已续期次数」。

    注意：API 里的 sessionExtensionsToday 是「上次记录时」的当日次数，
    必须配合 sessionExtensionsDate 判断是否属于今天（UTC）。
    日期不匹配时它已过期，应视为 0。
    这与 voer.host 前端逻辑一致：
        F = (String(sessionExtensionsDate).slice(0,10) === todayUTC) ? sessionExtensionsToday : 0
    旧脚本只看 sessionExtensionsToday，跨 UTC 日会读到过期值（如 4）而误判「今日已满」。
    """
    import datetime

    raw = server.get("sessionExtensionsToday") or 0
    try:
        raw = int(raw)
    except Exception:
        raw = 0
    date_val = server.get("sessionExtensionsDate")
    if not date_val:
        # 没有日期字段时无法确认是否属于今天：保守返回 0，宁可尝试续期也不误跳过
        return 0
    try:
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        if str(date_val)[:10] == today:
            return max(0, raw)
        return 0
    except Exception:
        return 0


def quota(server: dict) -> dict:
    """计算可续期次数。

    平台同时限制：每 UTC 日最多 4 次、每个会话最多 4 次。
    可续期次数 = min(今日剩余, 本会话剩余)。
    必须用 today_used() 结合 sessionExtensionsDate，跨日时当日计数会过期。
    """
    used_today = today_used(server)
    try:
        session_ext = int(server.get("sessionExtensions") or 0)
    except Exception:
        session_ext = 0
    session_ext = max(0, session_ext)
    daily_left = max(0, MAX_DAILY_EXTENSIONS - used_today)
    session_left = max(0, MAX_SESSION_EXTENSIONS - session_ext)
    remaining = min(daily_left, session_left)
    return {
        "used_today": used_today,
        "session_ext": session_ext,
        "daily_left": daily_left,
        "session_left": session_left,
        "remaining": remaining,
        "daily_max": MAX_DAILY_EXTENSIONS,
        "session_max": MAX_SESSION_EXTENSIONS,
    }


def fmt_quota(q: dict) -> str:
    return (
        f"可续期次数: {q['remaining']} 次"
        f"（今日剩余 {q['daily_left']}/{q['daily_max']}"
        f" · 本会话剩余 {q['session_left']}/{q['session_max']}"
        f" | 今日已用 {q['used_today']}/{q['daily_max']}"
        f"，会话已用 {q['session_ext']}/{q['session_max']}）"
    )


def log_quota(server: dict, prefix: str = "") -> dict:
    q = quota(server)
    head = prefix if prefix else "检查"
    log(f"{head}{fmt_quota(q)}")
    return q


def server_status_key(server) -> str:
    if not server:
        return ""
    return str(server.get("status") or "").strip().lower()


def is_running(server) -> bool:
    return server_status_key(server) in RUNNING_STATUSES


def is_stopped(server) -> bool:
    return server_status_key(server) in STOPPED_STATUSES


def is_crashed(server) -> bool:
    return server_status_key(server) in CRASHED_STATUSES


def is_starting(server) -> bool:
    return server_status_key(server) in STARTING_STATUSES


def api_post(cfg, path: str, payload=None, timeout: int = 60):
    """POST voer.host API。返回 (status_code, body_dict)。失败不 sys.exit。"""
    url = path if path.startswith("http") else f"https://voer.host{path}"
    body = json.dumps(payload if payload is not None else {}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Cookie": f"token={cfg['token']}",
            "Authorization": f"Bearer {cfg['token']}",
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode(errors="replace")
            try:
                data = json.loads(raw) if raw.strip() else {}
            except Exception:
                data = {"raw": raw[:300]}
            return r.status, data
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode(errors="replace")[:800]
        except Exception:
            pass
        data = {}
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"error": raw or str(e.reason)}
        return e.code, data
    except Exception as e:
        log(f"API POST 异常 {url}: {e}")
        return 0, {"error": str(e)}


def api_power(cfg, server_id: str, action: str, extra=None):
    """电源操作：start / stop / restart / kill。"""
    payload = dict(extra or {})
    code, data = api_post(cfg, f"/api/servers/{server_id}/{action}", payload)
    err = ""
    if isinstance(data, dict):
        err = str(data.get("error") or data.get("message") or "")
    log(f"电源指令 {action}: HTTP {code}" + (f" {err}" if err else ""))
    return code, data


def wait_for_status(cfg, server_id: str, want, timeout: int = 300, poll: float = 5):
    """轮询直到 status 落入 want 集合，超时返回最后一次状态。"""
    want = {str(x).lower() for x in want}
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = api_state(cfg, server_id)
        except SystemExit:
            time.sleep(poll)
            continue
        except Exception as e:
            log(f"轮询状态失败: {e}")
            time.sleep(poll)
            continue
        st = server_status_key(last)
        log(f"等待开机: 当前状态={st or '未知'}")
        if st in want:
            return last
        time.sleep(poll)
    return last


def _text_visible_anywhere(page, texts, exact: bool = True):
    """跨主页面与所有 iframe 检测某组文本是否可见（只检测，绝不点击）。

    返回命中的第一条文本；未命中返回 None；页面不可用时返回 None。
    """
    try:
        frames = list(page.frames)
    except Exception:
        return None
    for frame in frames:
        for t in texts:
            try:
                loc = frame.get_by_text(t, exact=exact).first
                if loc.count() and loc.is_visible():
                    return t
            except Exception:
                continue
    return None


def _site_unlock_present(page):
    """是否仍显示「网站访问解锁」提示。返回命中文本，未出现返回 None。"""
    hit = _text_visible_anywhere(page, SITE_UNLOCK_DETECT, exact=True)
    if hit:
        return hit
    return _text_visible_anywhere(page, SITE_UNLOCK_DETECT, exact=False)


def ensure_site_unlock(cfg, page) -> bool:
    """处理登录后偶现的「网站访问解锁」弹窗（独立流程）。

    页面可能显示：解锁更多内容 / 请做出选择以便继续访问此网站上的内容 /
    观看一则短广告 / 获得 24 小时的网站级访问权限。

    这是「网站级 24h 访问解锁」广告，与开机广告、Extend 续期 3 广告完全无关：
    - 使用独立 SITE_UNLOCK_DETECT / SITE_UNLOCK_CLICK 标签集，不复用 watch_labels
    - 不调用 watch_rewarded_ads()，不修改、不计入任何 adsCompleted / 广告计数
    无提示时直接返回 True；解锁失败返回 False（调用方应终止本次服务器任务）。
    """
    if page is None:
        return True

    hit = _site_unlock_present(page)
    if not hit:
        log("[站点解锁] 未检测到网站解锁提示，无需处理")
        return True

    log(f"[站点解锁] 检测到网站解锁提示（{hit}），执行独立解锁流程（此广告不计入任何广告计数）")
    try:
        take_screenshot(page, "debug_screenshot_site_unlock_before.png")
    except Exception:
        pass

    clicked = click_anywhere(page, SITE_UNLOCK_CLICK, 20000, exact=True)
    if not clicked:
        clicked = click_anywhere(page, SITE_UNLOCK_CLICK, 10000, exact=False)
    if not clicked:
        log("[站点解锁] 未找到「观看一则短广告」入口，解锁失败")
        try:
            take_screenshot(page, "debug_screenshot_site_unlock.png")
        except Exception:
            pass
        return False

    log(f"[站点解锁] 已点击解锁入口（{clicked}），等待广告播放完成…")
    ad_sec = int(cfg.get("ad_duration_sec") or 32)
    page.wait_for_timeout(ad_sec * 1000)

    # 广告播完后必须手动点击 Close（与开机/续期广告同一套标签，不计任何广告计数）
    closed = click_anywhere(page, AD_CLOSE_LABELS, 60000) or click_anywhere(
        page, AD_CLOSE_LABELS, 15000, exact=False
    )
    if closed:
        log(f"[站点解锁] 已点击广告 Close（{closed}），等待广告层卸载…")
        page.wait_for_timeout(5000)
    else:
        log("[站点解锁] 未找到广告 Close 按钮（可能已自动关闭或仍在播放），继续等待解锁提示消失")

    # 等待解锁提示消失
    deadline = time.time() + 120
    while time.time() < deadline:
        if not _site_unlock_present(page):
            log("[站点解锁] 解锁提示已消失，网站访问已解锁")
            return True
        page.wait_for_timeout(3000)

    # 必要时 reload 后再次确认
    log("[站点解锁] 提示仍在，reload 后再次确认…")
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
    except Exception:
        pass
    if _site_unlock_present(page):
        log("[站点解锁] reload 后解锁提示仍存在，判定解锁失败")
        try:
            take_screenshot(page, "debug_screenshot_site_unlock.png")
        except Exception:
            pass
        return False
    log("[站点解锁] reload 后解锁提示已消失，网站访问已解锁")
    return True


def _region_shortage_present(page):
    """是否出现「区域不可用」提示（UI 辅助检测）。REGION_SHORTAGE_DETECT 为空时不检测。"""
    if not REGION_SHORTAGE_DETECT:
        return None
    hit = _text_visible_anywhere(page, REGION_SHORTAGE_DETECT, exact=True)
    if hit:
        return hit
    return _text_visible_anywhere(page, REGION_SHORTAGE_DETECT, exact=False)


def _api_region_unavailable(server) -> bool:
    """纯读取 api_state() 已返回的 server dict，判断后端是否报告区域不可用。

    判定依据（来自 Voer 前端 bundle ServerDetail 组件确认的字段与窗口）：
    - server.provisioningStatus == "region_unavailable" → 立即 True（主证据）
    - server.live.regionUnavailable / server.live.regionCapacityLow
      且 updatedAt 在 REGION_UNAVAILABLE_WINDOW_SEC 内 → True（辅助证据，与前端 ct() 一致）
    普通 {} / None / 缺字段一律不误判；不发起任何网络请求。
    """
    if not isinstance(server, dict):
        return False
    if str(server.get("provisioningStatus") or "").strip().lower() == "region_unavailable":
        return True
    live = server.get("live")
    if not isinstance(live, dict):
        return False
    for key in ("regionUnavailable", "regionCapacityLow"):
        val = live.get(key)
        if not isinstance(val, dict):
            continue
        updated = parse_iso(val.get("updatedAt"))
        if updated is not None:
            age = time.time() - updated.timestamp()
            if 0 <= age <= REGION_UNAVAILABLE_WINDOW_SEC:
                return True
    return False


def _wait_running_or_region_failure(cfg, server_id: str, timeout: int = 180, poll: float = 5):
    """轮询等待 running；API 报告区域不可用时提前返回（不空等剩余时间）。

    返回 (server, region_failed)。轮询行为与 wait_for_status 一致（超时返回最后一次状态），
    仅新增 _api_region_unavailable(last) 早退分支。不修改 wait_for_status / api_state。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = api_state(cfg, server_id)
        except SystemExit:
            time.sleep(poll)
            continue
        except Exception as e:
            log(f"轮询状态失败: {e}")
            time.sleep(poll)
            continue
        st = server_status_key(last)
        if st in RUNNING_STATUSES:
            return last, False
        if _api_region_unavailable(last):
            log(f"等待开机: 当前状态={st or '未知'}（API 报告区域不可用，提前结束等待）")
            return last, True
        log(f"等待开机: 当前状态={st or '未知'}")
        time.sleep(poll)
    return last, False


def _region_failure_info(server):
    """提取区域失败信息：当前失败 region + Voer 提供的可用候选 region id。

    字段来自 Voer 前端 bundle（ServerDetail 组件）确认的结构：
    live.regionUnavailable / live.regionCapacityLow，均含 region 与 availableRegions。
    availableRegions 兼容 ["US", "EU"] 与 [{"id": "US", "available": true}] 两种格式。
    无法确定失败区域时返回 (None, [])，调用方按 region_shortage 失败处理。
    """
    live = server.get("live") if isinstance(server, dict) else None
    if not isinstance(live, dict):
        return None, []
    for key in ("regionUnavailable", "regionCapacityLow"):
        info = live.get(key)
        if not isinstance(info, dict):
            continue
        failed = info.get("region")
        if not isinstance(failed, str) or not failed.strip():
            failed = None
        candidates = []
        raw = info.get("availableRegions")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())
                elif isinstance(item, dict) and isinstance(item.get("id"), str):
                    if item.get("available") is False:
                        continue
                    candidates.append(str(item["id"]).strip())
        return failed, list(dict.fromkeys(candidates))
    return None, []


def _get_available_regions(cfg):
    """GET /api/servers/regions，返回经过基础校验的区域列表。

    复用现有 api_post（不新增 API 层、不改 api_state 行为）；响应非 2xx 或格式异常
    时返回 None（调用方按 region_shortage 失败处理，不做任何猜测）。
    """
    code, data = api_post(cfg, "/api/servers/regions", timeout=30)
    if code not in (200, 201, 202, 204):
        log(f"[自动换区] 获取区域列表失败：HTTP {code}，取消换区")
        return None
    regions = data.get("regions") if isinstance(data, dict) else None
    if not isinstance(regions, list):
        log("[自动换区] 获取区域列表失败：响应格式异常，取消换区")
        return None
    return regions


def _compute_region_candidates(server, regions, failed_regions):
    """按 Voer 前端规则求候选：服务器给出的候选 ∩ API available=true 且 checked!=false
    - 当前失败区域 - 本次运行已失败区域；保持 API 返回顺序，不排序、不设优先级。
    """
    failed, offered = _region_failure_info(server)
    if not failed or not offered:
        return failed, []
    failed_regions = set(failed_regions or set())
    api_ok = set()
    for r in regions:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not isinstance(rid, str) or not rid.strip():
            continue
        if r.get("available") is not True:
            continue
        if r.get("checked") is False:
            continue
        api_ok.add(rid.strip())
    candidates = []
    for rid in offered:
        rid = rid.strip()
        if rid not in api_ok:
            continue
        if rid == failed:
            continue
        if rid in failed_regions:
            continue
        candidates.append(rid)
    return failed, candidates


def _api_region_switch(cfg, server_id: str, region: str):
    """PATCH /api/servers/{id}/region，2xx 才算成功；不重新 Start。"""
    code, data = api_post(
        cfg, f"/api/servers/{server_id}/region", {"region": region}, timeout=60
    )
    if code not in (200, 201, 202, 204):
        log(f"[自动换区] 区域切换失败：HTTP {code}，取消换区")
        return None
    if isinstance(data, dict) and isinstance(data.get("server"), dict):
        return data["server"]
    return api_state(cfg, server_id)


def _switch_region_and_wait(cfg, server_id: str, server, failed_regions, switch_count):
    """区域不可用后的自动换区：获取候选 → 选第一个 → PATCH → 继续等 running。

    返回 (server, ok, switch_count)。失败时返回当前 server 与 False，调用方按
    region_shortage 终止。全程不调用 api_power / click_start_button /
    watch_rewarded_ads，不重新看 3 个开机广告。
    """
    failed_regions = failed_regions if isinstance(failed_regions, set) else set()
    if switch_count >= MAX_REGION_SWITCHES:
        log(f"[自动换区] 已达到本次运行最大换区次数（{MAX_REGION_SWITCHES}），停止换区")
        return server, False, switch_count

    regions = _get_available_regions(cfg)
    if regions is None:
        return server, False, switch_count
    failed, candidates = _compute_region_candidates(server, regions, failed_regions)
    if not failed:
        log("[自动换区] 当前区域不可用，但无法确定失败区域，取消换区")
        return server, False, switch_count
    log(f"[自动换区] 检测到当前区域不可用")
    log(f"[自动换区] 当前失败区域: {failed}")
    if candidates:
        log(f"[自动换区] Voer 当前提供候选区域: {', '.join(candidates)}")
    else:
        log("[自动换区] 当前区域不可用，但 Voer 没有提供其他可用区域")
        return server, False, switch_count
    api_ok = [
        r.get("id")
        for r in regions
        if isinstance(r, dict)
        and isinstance(r.get("id"), str)
        and r.get("available") is True
        and r.get("checked") is not False
    ]
    if api_ok:
        log(f"[自动换区] API 当前可用区域: {', '.join(api_ok)}")
    pick = candidates[0]
    log(f"[自动换区] 最终选择区域: {pick}")
    log(f"[自动换区] 正在切换区域: {failed} -> {pick}")

    new_server = _api_region_switch(cfg, server_id, pick)
    if new_server is None:
        return server, False, switch_count
    failed_regions.add(failed)
    log("[自动换区] 区域切换成功，Voer 将继续自动启动服务器")
    log("[自动换区] 不重新执行 Start，不重新观看开机广告")
    log(f"[自动换区] 继续等待服务器进入 running")

    server, region_failed = _wait_running_or_region_failure(cfg, server_id, timeout=180)
    if server is None:
        server = api_state(cfg, server_id)
    if region_failed or _api_region_unavailable(server):
        log(f"[自动换区] 新区域 {pick} 仍然不可用")
        log("[自动换区] 重新获取 Voer 最新区域容量")
        return server, True, switch_count + 1
    return server, True, switch_count + 1


def _region_shortage_check(page, server=None):
    """区域资源不足检测：API 主检测（server 字段）+ UI 辅助检测（页面文案）。

    命中时记录日志 + 截图 + dump_page_debug。自动换区无候选时调用方据此终止本轮。
    本函数自身不调用 regions/region API、不点击任何区域按钮。返回命中标识文本；
    未命中返回 None。
    """
    hit = None
    if server is not None and _api_region_unavailable(server):
        hit = "api:provisioningStatus=region_unavailable / live.regionUnavailable(CapacityLow)"
    if hit is None:
        hit = _region_shortage_present(page)
    if hit:
        log(f"[区域资源不足] 检测到提示（{hit}）")
        try:
            take_screenshot(page, "debug_screenshot_region_shortage.png")
        except Exception:
            pass
        try:
            dump_page_debug(page, "区域资源不足")
        except Exception:
            pass
        log("[区域资源不足] 无可用候选区域，任务安全终止，等待下一轮/人工处理")
    return hit


def click_start_button(page) -> str | None:
    if _site_unlock_present(page):
        log("[站点解锁] 网站解锁提示仍在，不寻找 Start/开机 按钮（避免误点）")
        return None
    start_labels = [
        "Start",
        "启动",
        "开机",
        "開機",
        "開始",
        "Recover",
        "恢复",
        "恢復",
        "Start server",
    ]
    hit = click_anywhere(page, start_labels, 20000)
    if not hit:
        hit = click_anywhere(page, start_labels, 15000, exact=False)
    return hit


def watch_rewarded_ads(cfg, page, reason: str = "开机/续期") -> int:
    """点击 Watch ad 并等待播放，返回实际完成的广告数。

    与续期流程保持一致：进入广告页 → 循环点击 Watch ad → 等待播放 → Close。
    若第一次点击后广告已在播放，则先等播完再 Close，计入第 1 条。

    注意：这是「开机广告」专用流程，与网站访问解锁广告完全无关。
    不含 "Watch"/"开始"/"開始" 等泛化标签，避免 has-text 子串匹配
    误点「观看一则短广告」解锁入口；检测到解锁提示仍在时直接中止。
    """
    if page is None:
        return 0
    if _site_unlock_present(page):
        log(f"[站点解锁] {reason}: 网站解锁提示仍在，中止广告流程（避免误点/误计解锁广告）")
        return 0
    watch_labels = [
        "Watch ad",
        "觀看廣告",
        "观看广告",
        "Watch Ad",
        "Watch ads",
        "Watch Ads",
    ]
    close_labels = AD_CLOSE_LABELS
    total = int(cfg.get("ads_per_extension") or 3)
    ad_sec = int(cfg.get("ad_duration_sec") or 32)
    watched = 0

    page.wait_for_timeout(3000)
    # 进入广告流程
    hit = click_anywhere(page, watch_labels, 30000) or click_anywhere(
        page, watch_labels, 15000, exact=False
    )
    if hit:
        log(f"{reason}: 已进入广告流程（{hit}）")
    else:
        log(f"{reason}: 未找到入口 Watch ad，尝试直接寻找可点广告按钮")
    log(f"{reason}: 等待 Ad ready…")
    page.wait_for_timeout(8000)

    for i in range(1, total + 1):
        # 优先点 Watch ad；若点不到，可能上一条还在播，先尝试 Close 再重试
        hit = click_anywhere(page, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 75000)
        if not hit:
            hit = click_anywhere(page, watch_labels, 20000, exact=False)
        if not hit:
            # 广告可能已自动开始播放：等待时长后关
            log(f"{reason}: 第 {i} 个 Watch ad 未立刻出现，等待播放/关闭按钮…")
            page.wait_for_timeout(ad_sec * 1000)
            closed = click_anywhere(page, close_labels, 45000) or click_anywhere(
                page, close_labels, 15000, exact=False
            )
            if closed:
                watched += 1
                log(f"{reason}: 第 {i}/{total} 个广告：已按播放完成关闭（{closed}）")
                page.wait_for_timeout(5000)
                continue
            log(f"{reason}: 第 {i} 个 Watch ad 未找到，停止广告流程")
            break

        watched += 1
        log(f"{reason}: 已点击第 {i}/{total} 个 Watch ad（{hit}），播放中…")
        page.wait_for_timeout(ad_sec * 1000)
        closed = click_anywhere(page, close_labels, 60000) or click_anywhere(
            page, close_labels, 15000, exact=False
        )
        log(
            f"{reason}: 第 {i} 个广告:",
            f"已关闭（{closed}）" if closed else "未找到 Close（可能自动关闭）",
        )
        page.wait_for_timeout(6000)

    log(f"{reason}: 广告流程结束，完成 {watched}/{total} 条")
    return watched


def restart_via_panel(cfg, page, reason: str = "关机后重启", server_id: str | None = None) -> int:
    """在面板点击 Start/开机并看激励广告。

    返回完成的广告数。若传入 server_id，广告结束后会带 adsCompleted 再调一次 start API。
    """
    if page is None:
        return 0
    hit = click_start_button(page)
    if not hit:
        log(f"{reason}: 面板上未找到 Start/开机 按钮")
        return 0
    log(f"{reason}: 已点击开机入口: {hit}")
    page.wait_for_timeout(2500)
    watched = watch_rewarded_ads(cfg, page, reason=reason)

    # 广告看完后，用 adsCompleted 再请求一次开机（平台要求）
    if server_id:
        n = max(watched, int(cfg.get("ads_per_extension") or 3))
        for ads_n in (n, 3, 2, 1):
            log(f"{reason}: 广告后再次 API start（adsCompleted={ads_n}）")
            code, data = api_power(cfg, server_id, "start", {"adsCompleted": ads_n})
            if code in (200, 201, 202, 204):
                log(f"{reason}: API start 已接受（adsCompleted={ads_n}）")
                break
            if not ads_required_error(code, data):
                # 非广告错误，再试 restart
                code2, _ = api_power(cfg, server_id, "restart")
                if code2 in (200, 201, 202, 204):
                    log(f"{reason}: API restart 已接受")
                    break
            page.wait_for_timeout(1500)
    return watched


def ads_required_error(code, data) -> bool:
    if code == 403:
        return True
    blob = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data or "")
    return "Ad requirement" in blob or "adsCompleted" in blob or "rewarded" in blob.lower()


def ensure_running(cfg, server_id: str, page=None):
    """检查电源状态；若为 stopped/offline 则开机。

    返回 (server, did_power, outcome)，outcome 为：
      already_running    本来就是 running（脚本未做任何动作）
      external_running   脚本未确认发起过开机，但观察到 running（用户/其他因素）
      powered_by_script  脚本自身发起的开机动作被接受（API 2xx，或面板 Start 后状态
                         实际进入 provisioning/starting），并最终进入 running
      region_shortage    provisioning 期间检测到「区域资源不足」提示（本轮不自动换区）
      start_failed       最终未进入 running（含跳过开机且未 running 的情况）

    因果原则：「最终看到 running」不等于「脚本成功开机」。
    只有脚本自己的开机动作被接受、状态随后离开 stopped 进入 starting/provisioning、
    再最终进入 running，才返回 did_power=True；否则一律归为 external_running。
    """
    skip = str(os.environ.get("VOER_SKIP_RESTART", "")).strip().lower() in ("1", "true", "yes")
    if skip or not cfg.get("restart_if_stopped", True):
        server = api_state(cfg, server_id)
        if is_running(server):
            return server, False, "already_running"
        log("已配置跳过开机，且服务器未运行 → 本次任务终止")
        return server, False, "start_failed"

    server = api_state(cfg, server_id)
    st = server_status_key(server)
    log(f"开机状态: {st or '未知'} — {status_text(st)}")

    # 只处理明确关机/离线
    if not is_stopped(server):
        if is_running(server):
            log("服务器已在运行中，无需开机（脚本未发起开机）")
            return server, False, "already_running"
        if is_starting(server):
            log("服务器启动中（非脚本发起），等待就绪…")
            server, region_failed = _wait_running_or_region_failure(
                cfg, server_id, timeout=360
            )
            if server is None:
                server = api_state(cfg, server_id)
            if is_running(server):
                log("等待后进入 running：非脚本发起的开机 → external_running")
                return server, False, "external_running"
            shortage = _region_shortage_check(page, server)
            if shortage or region_failed:
                return server, False, "region_shortage"
            log(f"等待后仍未 running（当前: {server_status_key(server) or '未知'}）→ 本次任务终止")
            return server, False, "start_failed"
        log(f"当前状态非关机（{st or '未知'}），不执行开机/重启 → 本次任务终止")
        return server, False, "start_failed"

    log("检测到关机/离线：执行开机或重启")
    own_action_accepted = False  # 脚本自身的开机动作是否被平台接受（API 2xx / 状态实际离开 stopped）
    need_ads = False
    for act in ("start", "restart"):
        log(f"发送电源指令: {act}")
        extra = {"adsCompleted": 0} if act == "start" else {}
        code, data = api_power(cfg, server_id, act, extra)
        if code in (200, 201, 202, 204):
            own_action_accepted = True
            if isinstance(data, dict) and data.get("server"):
                server = data["server"]
            break
        if ads_required_error(code, data):
            need_ads = True
            log("开机需要先看激励广告，改为在面板点击 Start 并播放广告")
            break
        log(f"{act} 未成功，尝试下一指令")

    if need_ads or not own_action_accepted:
        if page is not None:
            restart_via_panel(cfg, page, reason="关机后开机", server_id=server_id)
            # 仅点击 Start 不足以证明开机成功：用状态变化确认动作被接受
            try:
                cur = api_state(cfg, server_id)
            except (SystemExit, Exception):
                cur = server
            if server_status_key(cur) in STARTING_STATUSES:
                own_action_accepted = True
                log("面板开机后状态已进入 provisioning/starting，确认脚本动作被接受")
                server = cur
        elif need_ads:
            log("开机需要看广告，但当前没有浏览器会话，无法完成开机 → 本次任务终止")
            return server, False, "start_failed"

    # 广告/指令后等待进入 running（区域不可用则自动换区后继续等待）
    failed_regions = set()
    switch_count = 0
    log("[自动换区] 开机流程完成，服务器进入 provisioning")
    server, region_failed = _wait_running_or_region_failure(cfg, server_id, timeout=180)
    if server is None:
        server = api_state(cfg, server_id)

    # 保持原逻辑：非区域原因失败时允许一次面板重试；区域失败直接进入换区
    if not is_running(server) and not region_failed and not _api_region_unavailable(server) and page is not None:
        log("开机后仍未运行，再试一次面板开机+广告")
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        except Exception:
            pass
        restart_via_panel(cfg, page, reason="关机后开机（重试）", server_id=server_id)
        try:
            cur = api_state(cfg, server_id)
        except (SystemExit, Exception):
            cur = server
        if server_status_key(cur) in STARTING_STATUSES:
            own_action_accepted = True
        server, region_failed = _wait_running_or_region_failure(
            cfg, server_id, timeout=180
        )
        if server is None:
            server = api_state(cfg, server_id)

    # 统一处理：running → 结束；region_unavailable → 换区后继续等；其他 → 失败
    while not is_running(server):
        if region_failed or _api_region_unavailable(server):
            server, ok, switch_count = _switch_region_and_wait(
                cfg, server_id, server, failed_regions, switch_count
            )
            if not ok:
                shortage = _region_shortage_check(page, server)
                return server, False, "region_shortage"
            region_failed = False
            continue
        shortage = _region_shortage_check(page, server)
        if shortage:
            return server, False, "region_shortage"
        break

    if is_running(server):
        if own_action_accepted:
            log("开机完成：脚本发起的开机被接受并最终 running（did_power=True）")
            return server, True, "powered_by_script"
        log("观察到 running，但无法确认由脚本开机导致（did_power=False, external_running）")
        return server, False, "external_running"

    log(
        f"开机最终失败：服务器仍未 running（当前状态: {server_status_key(server) or '未知'}），"
        "本次任务终止（不再进入 Extend/续期）"
    )
    return server, False, "start_failed"


def load_config():
    cfg = dict(DEFAULT_CONFIG)

    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(file_cfg)
        except Exception as e:
            log(f"读取 config.json 失败: {e}")

    env_sid = os.environ.get("VOER_SERVER_ID", "").strip().strip('"').strip("'")
    env_token = os.environ.get("VOER_TOKEN", "").strip().strip('"').strip("'")
    env_email = (
        os.environ.get("VOER_EMAIL", "").strip().strip('"').strip("'")
        or os.environ.get("EMAIL", "").strip().strip('"').strip("'")
    )
    env_password = (
        os.environ.get("VOER_PASSWORD", "").strip().strip('"').strip("'")
        or os.environ.get("PASSWORD", "").strip().strip('"').strip("'")
    )
    if env_sid:
        cfg["server_id"] = env_sid
    if env_token:
        cfg["token"] = env_token
    if env_email:
        cfg["email"] = env_email
    if env_password:
        cfg["password"] = env_password

    # Telegram（环境变量优先）
    env_tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    env_tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")
    if env_tg_token:
        cfg["telegram_bot_token"] = env_tg_token
    if env_tg_chat:
        cfg["telegram_chat_id"] = env_tg_chat

    if os.environ.get("VOER_ADS_PER_EXTENSION"):
        cfg["ads_per_extension"] = int(os.environ["VOER_ADS_PER_EXTENSION"])
    if os.environ.get("VOER_AD_DURATION_SEC"):
        cfg["ad_duration_sec"] = int(os.environ["VOER_AD_DURATION_SEC"])
    if os.environ.get("VOER_EXTENSIONS_PER_RUN"):
        cfg["extensions_per_run"] = int(os.environ["VOER_EXTENSIONS_PER_RUN"])
    skip_restart = os.environ.get("VOER_SKIP_RESTART", "").strip().lower()
    if skip_restart in ("1", "true", "yes"):
        cfg["restart_if_stopped"] = False
    env_tg_title = os.environ.get("TG_TITLE", "").strip().strip('"').strip("'")
    if env_tg_title:
        cfg["tg_title"] = env_tg_title

    sid = cfg.get("server_id", "")
    token = cfg.get("token", "")
    email = (cfg.get("email") or "").strip()
    password = (cfg.get("password") or "").strip()
    token_ok = token and "在这里填" not in token
    creds_ok = bool(email and password)
    if not sid or "在这里填" in sid:
        log("=" * 60)
        log("缺少必要配置！请设置 VOER_SERVER_ID")
        log("=" * 60)
        sys.exit(1)
    if not token_ok and not creds_ok:
        log("=" * 60)
        log("缺少认证配置！请设置 VOER_TOKEN，或同时设置 VOER_EMAIL + VOER_PASSWORD")
        log("可选：TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID 用于通知")
        log("=" * 60)
        sys.exit(1)

    # 多服务器支持：VOER_SERVER_ID / server_id 可填多个 UUID，
    # 用逗号、分号或空白分隔（同一账号的 token 对所有服务器通用）
    ids = [x for x in re.split(r"[,;\s]+", sid.strip()) if x]
    seen = set()
    server_ids = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            server_ids.append(x)
    cfg["server_ids"] = server_ids

    log(f"server_id 数量={len(server_ids)}")
    for x in server_ids:
        log(f"  - {x[:8]}…")
    log(f"token 诊断: {_jwt_hint(token)}")
    # 诊断环境变量是否真正传入（不打印完整 secret）
    raw_tg_t = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_tg_c = os.environ.get("TELEGRAM_CHAT_ID")
    log(
        f"环境变量探测: TELEGRAM_BOT_TOKEN={'已设置 len='+str(len(raw_tg_t)) if raw_tg_t else '空/未传入'}, "
        f"TELEGRAM_CHAT_ID={'已设置 len='+str(len(raw_tg_c)) if raw_tg_c else '空/未传入'}"
    )
    if _tg_enabled(cfg):
        log("Telegram 通知: 已启用")
    else:
        log("Telegram 通知: 未配置（需同时设置 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID）")
        log("  GitHub: Settings → Secrets and variables → Actions")
        log("  名称必须一字不差：TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        log("  并确保仓库里的 .github/workflows/voer-renew.yml 已更新（会把 secrets 注入 env）")
    if os.environ.get("VOER_SERVER_ID"):
        log("配置来源: 环境变量")
    elif CONFIG_PATH.exists():
        log("配置来源: 本地 config.json")

    return cfg


def api_state(cfg, server_id=None):
    sid = server_id or cfg["server_id"]
    url = f"https://voer.host/api/servers/{sid}"
    req = urllib.request.Request(
        url,
        headers={
            "Cookie": f"token={cfg['token']}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            if "server" not in data:
                raise RuntimeError(f"API 返回格式异常: {list(data.keys())}")
            return data["server"]
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            pass
        log("=" * 60)
        log(f"API 请求失败: HTTP {e.code} {e.reason}")
        log(f"请求地址: {url}")
        if body:
            log(f"响应内容: {body}")
        if e.code in (401, 403):
            log("")
            log("【401/403】token 过期或错误")
            log(f"当前 token 诊断: {_jwt_hint(cfg.get('token', ''))}")
            # 尝试邮箱密码重新登录一次（避免整次任务直接退出）
            if not cfg.get("_relogin_attempted"):
                cfg["_relogin_attempted"] = True
                email = (cfg.get("email") or "").strip()
                password = (cfg.get("password") or "").strip()
                if email and password:
                    log("尝试邮箱密码重新登录…")
                    new_token = login_with_password(cfg)
                    if new_token:
                        persist_token(cfg, new_token)
                        log("重新登录成功，重试 API…")
                        return api_state(cfg, server_id)
                log("请设置有效的 VOER_TOKEN，或配置 VOER_EMAIL + VOER_PASSWORD")
            else:
                log("已尝试过重新登录仍失败")
        log("=" * 60)
        raise SystemExit(1) from e
    except urllib.error.URLError as e:
        log(f"网络错误: {e.reason}")
        raise SystemExit(1) from e


def click_anywhere(page, texts, timeout_ms, exact=True):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        frames = list(page.frames)
        # 优先广告相关 iframe（wormies / googleads）
        def _frame_key(f):
            u = (f.url or "").lower()
            if "wormies" in u or "voer-ads" in u:
                return 0
            if "doubleclick" in u or "googleads" in u or "googlesyndication" in u:
                return 1
            return 2
        try:
            frames.sort(key=_frame_key)
        except Exception:
            pass
        for frame in frames:
            for t in texts:
                makers = [
                    lambda t=t, f=frame: f.get_by_role("button", name=t, exact=exact).first,
                    lambda t=t, f=frame: f.get_by_role("link", name=t, exact=exact).first,
                    lambda t=t, f=frame: f.get_by_text(t, exact=exact).first,
                    lambda t=t, f=frame: f.locator(f"button:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"[role=button]:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"a:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"[title='{t}']").first,
                ]
                for maker in makers:
                    try:
                        loc = maker()
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=3000)
                            return f"{t}@{frame.url[:60]}"
                    except Exception:
                        pass
        time.sleep(1.2)
    return None


def _shot_name(base: str) -> str:
    """多服务器时给截图文件名加编号后缀：renew_screenshot.png -> renew_screenshot_2.png"""
    tag = os.environ.get("VOER_SHOT_TAG", "1")
    if tag == "1":
        return base
    p = pathlib.Path(base)
    return f"{p.stem}_{tag}{p.suffix}"


def take_screenshot(page, name="screenshot.png") -> pathlib.Path:
    """截取当前浏览器真实页面（面板状态），供 Telegram 发送。"""
    path = pathlib.Path(_shot_name(name))
    try:
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        try:
            page.screenshot(path=str(path), full_page=True, type="png")
        except Exception:
            page.screenshot(path=str(path), full_page=False, type="png")
        size = path.stat().st_size if path.exists() else 0
        log(f"截图已保存: {path.resolve()} ({size} bytes)")
        if size < 1000:
            log("警告: 截图文件过小，可能是空白页")
    except Exception as e:
        log(f"截图失败: {e}")
    return path


def capture_panel_shot(cfg, server_id: str, name: str = "skip_screenshot.png"):
    """短暂打开面板截一张真实截图（用于跳过时的 TG 通知）。失败返回 None。"""
    url = f"https://voer.host/panel/server/{server_id}"
    path = pathlib.Path(_shot_name(name))
    try:
        with sync_playwright() as p:
            launch = dict(
                headless=cfg.get("headless", False),
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1400,1000",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            if cfg.get("use_system_chrome"):
                launch["channel"] = "chrome"
            browser = p.chromium.launch(**launch)
            try:
                ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
                ctx.add_cookies(
                    [
                        {
                            "name": "token",
                            "value": cfg["token"],
                            "domain": "voer.host",
                            "path": "/",
                            "secure": True,
                        }
                    ]
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                take_screenshot(page, name)
            finally:
                browser.close()
        if path.exists() and path.stat().st_size >= 1000:
            return path
        return path if path.exists() else None
    except Exception as e:
        log(f"跳过流程截图失败（不影响跳过本身）: {e}")
        return None


def notify_skip(cfg, account, server_id, server: dict, reason: str):
    """跳过时发送 TG 通知并尽量附带面板截图。不抛错。"""
    short_id = (server_id or "")[:8] + "…"
    q = quota(server) if server else {}
    uptime = seconds_until((server or {}).get("sessionExpiresAt"))
    remaining_text = fmt_quota(q) if q else "—"
    next_text = fmt_next_renewal((server or {}).get("sessionExpiresAt"))
    log(f"跳过原因: {reason}")
    log(f"发送跳过通知（含截图）… 下次续期: {next_text}")
    shot = capture_panel_shot(cfg, server_id, name="skip_screenshot.png")
    notify_godlike(
        cfg,
        account,
        short_id,
        f"⏭️跳过（{reason}）",
        uptime,
        (server or {}).get("status"),
        photo=shot,
        remaining_text=remaining_text + "\n📅下次续期: " + next_text,
    )


def dump_page_debug(page, tag="debug"):
    log(f"----- 页面诊断 ({tag}) -----")
    log(f"URL: {page.url}")
    try:
        log(f"Title: {page.title()}")
    except Exception:
        pass
    texts = []
    try:
        for frame in page.frames:
            for role in ("button", "link"):
                try:
                    for loc in frame.get_by_role(role).all()[:40]:
                        try:
                            if loc.is_visible():
                                t = (loc.inner_text(timeout=500) or "").strip()
                                if t and t not in texts:
                                    texts.append(t)
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception as e:
        log(f"收集按钮失败: {e}")
    if texts:
        log("可见按钮/链接文字:")
        for t in texts[:50]:
            log(f"  - {t!r}")
    else:
        log("未收集到可见按钮文字")
    take_screenshot(page, "debug_screenshot.png")
    log("----- 诊断结束 -----")


def run_server(cfg, server_id, account=""):
    """对单台服务器执行一次完整续期流程。返回 True=成功，False/抛异常=失败，None=跳过。"""
    url = f"https://voer.host/panel/server/{server_id}"
    short_id = server_id[:8] + "…"

    if "--status" in sys.argv:
        s = api_state(cfg, server_id)
        for k in (
            "status",
            "sessionExpiresAt",
            "sessionExtensions",
            "sessionExtensionsToday",
            "sessionExtensionsDate",
            "sessionDuration",
            "adsWatched",
        ):
            print(f"{k} = {s.get(k)}")
        print(f"今日已续期(UTC, 计算值) = {today_used(s)} / {MAX_DAILY_EXTENSIONS}")
        q = quota(s)
        print(f"本会话已续期 = {q['session_ext']} / {MAX_SESSION_EXTENSIONS}")
        print(fmt_quota(q))
        print(f"开机状态 = {s.get('status')} ({status_text(s.get('status'))})")
        print(f"下次续期准确时间 = {fmt_next_renewal(s.get('sessionExpiresAt'))}")
        # status 也可发一条简短通知（可选）
        if os.environ.get("TG_NOTIFY_STATUS") == "1":
            notify(
                cfg,
                "📊 Voer 状态查询",
                [
                    f"服务器: <code>{short_id}</code>",
                    f"状态: {s.get('status')}",
                    f"到期: {s.get('sessionExpiresAt')}",
                    f"下次续期: {fmt_next_renewal(s.get('sessionExpiresAt'))}",
                    f"累计续期: {s.get('sessionExtensions')}",
                    f"今日续期: {today_used(s)} / {MAX_DAILY_EXTENSIONS} (UTC)",
                    f"{fmt_quota(q)}",
                ],
            )
        return True

    # ===== 浏览器启动前：轻量检查，无次数 / 未到期则跳过（不报错）=====
    try:
        pre = api_state(cfg, server_id)
    except SystemExit:
        raise
    except Exception as e:
        log(f"预检 API 失败: {e}，将继续尝试完整流程")
        pre = {}

    if pre:
        q_pre = quota(pre)
        log_quota(pre, prefix="预检")
        log(f"下次续期准确时间: {fmt_next_renewal(pre.get('sessionExpiresAt'))}")
        log(f"开机状态: {pre.get('status')} — {status_text(pre.get('status'))}")

        # 关机优先开机：预检只提示，真正开机在面板流程里执行
        if is_stopped(pre):
            log("检测到关机/离线，将优先执行开机")

        # 无续期次数且已在运行 → 跳过，不报错（关机时仍去开机）
        if q_pre["remaining"] <= 0 and not is_stopped(pre):
            log(
                f"可续期次数为 0（今日 {q_pre['used_today']}/{MAX_DAILY_EXTENSIONS}"
                f" · 会话 {q_pre['session_ext']}/{MAX_SESSION_EXTENSIONS}），跳过，不报错"
            )
            try:
                notify_skip(
                    cfg,
                    account,
                    server_id,
                    pre,
                    "无可用次数",
                )
            except Exception as e:
                log(f"跳过通知发送失败（不影响结果）: {e}")
            return None

        if q_pre["remaining"] > 0:
            log(f"有可续期次数（{q_pre['remaining']}），将执行续期")

    success = False
    before = {}
    now = {}
    last_shot = None  # 结束时填入真实截图
    last_shot = pathlib.Path(_shot_name("renew_screenshot.png"))
    # 单次运行内最多连续续期几次（受平台每日 4 次 / 每会话 4 次上限约束）
    max_ext = max(1, int(cfg.get("extensions_per_run", 4)))
    rounds_ok = 0
    stop_reason = ""

    watch_labels = [
        "觀看廣告",
        "观看广告",
        "Watch ad",
        "Watch Ad",
        "Watch ads",
        "Watch Ads",
    ]
    extend_labels = [
        "延伸",
        "延长",
        "延長",
        "续期",
        "續期",
        "Extend",
        "Extend session",
        "Extend Session",
        "Renew",
        "Watch ads",
        "Watch Ads",
    ]

    with sync_playwright() as p:
        launch = dict(
            headless=cfg["headless"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1400,1000",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        if cfg["use_system_chrome"]:
            launch["channel"] = "chrome"
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        ctx.add_cookies(
            [
                {
                    "name": "token",
                    "value": cfg["token"],
                    "domain": "voer.host",
                    "path": "/",
                    "secure": True,
                }
            ]
        )
        page = ctx.new_page()
        try:
            log(f"打开页面: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(5000)

            before = api_state(cfg, server_id)
            log(
                "当前到期:",
                before.get("sessionExpiresAt"),
                "| 已续期:",
                before.get("sessionExtensions"),
                "| 今日:",
                before.get("sessionExtensionsToday"),
                f"(sessionExtensionsDate={before.get('sessionExtensionsDate')})",
            )
            log(f"开机状态: {before.get('status')} — {status_text(before.get('status'))}")
            log(f"下次续期准确时间: {fmt_next_renewal(before.get('sessionExpiresAt'))}")
            log_quota(before, prefix="检查")

            for accept_txt in ("Accept", "Accept all", "同意", "接受", "I agree", "OK"):
                hit = click_anywhere(page, [accept_txt], 3000)
                if hit:
                    log(f"已点同意弹窗: {hit}")
                    break

            # ===== 网站访问解锁（独立流程，绝不计入开机/续期广告计数）=====
            if not ensure_site_unlock(cfg, page):
                log("[站点解锁] 解锁失败，终止本次服务器任务（不进入开机/续期）")
                try:
                    dump_page_debug(page, "站点解锁失败")
                except Exception:
                    pass
                try:
                    notify(
                        cfg,
                        "❌ Voer 站点解锁失败",
                        [
                            f"服务器: <code>{short_id}</code>",
                            "原因: 「观看一则短广告」解锁未完成，面板不可操作",
                            "请查看 Actions 日志或 site_unlock 截图",
                        ],
                        photo=pathlib.Path(_shot_name("debug_screenshot_site_unlock.png")),
                    )
                except Exception as e:
                    log(f"解锁失败通知发送失败（不影响结果）: {e}")
                return False

            # 只检查是否关机：关机则开机/重启（不抛错）
            before, did_power, outcome = ensure_running(cfg, server_id, page=page)
            log(f"开机结果: outcome={outcome}, did_power={did_power}")

            if outcome in ("start_failed", "region_shortage"):
                fail_title = (
                    "❌ Voer 开机失败，本次任务终止"
                    if outcome == "start_failed"
                    else "⚠️ Voer 检测到区域资源不足，本次任务终止"
                )
                log(f"{fail_title}（不进入 Extend/续期）")
                shot = take_screenshot(page, "debug_screenshot_start_failed.png")
                try:
                    notify_godlike(
                        cfg,
                        account,
                        short_id,
                        fail_title,
                        seconds_until(before.get("sessionExpiresAt")),
                        before.get("status"),
                        photo=shot,
                        remaining_text="—",
                    )
                except Exception as e:
                    log(f"终止通知发送失败（不影响结果）: {e}")
                return False
            if did_power:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                except Exception:
                    pass
                before = api_state(cfg, server_id)
                log("开机之后再次检查可续期次数与到期时间")
                log_quota(before, prefix="开机后检查")
                log(f"下次续期准确时间: {fmt_next_renewal(before.get('sessionExpiresAt'))}")

            # 开机成功后：以本次开机时间为准刷新状态
            if did_power:
                before = api_state(cfg, server_id)
                log("以本次开机时间为准，刷新会话/次数状态")
                log(
                    f"sessionExtensionsDate={before.get('sessionExtensionsDate')} "
                    f"（UTC 今日={datetime.now(timezone.utc).strftime('%Y-%m-%d')}；"
                    f"非今日则今日已用按 0）"
                )
                log_quota(before, prefix="开机后基准")
                log(f"下次续期准确时间: {fmt_next_renewal(before.get('sessionExpiresAt'))}")

            q_now = quota(before)

            # 无续期次数 → 跳过，不报错
            if q_now["remaining"] <= 0:
                log(
                    f"可续期次数为 0（今日 {q_now['used_today']}/{MAX_DAILY_EXTENSIONS}"
                    f" · 会话 {q_now['session_ext']}/{MAX_SESSION_EXTENSIONS}），跳过，不报错"
                )
                shot = take_screenshot(page, "skip_screenshot.png")
                try:
                    notify_godlike(
                        cfg,
                        account,
                        short_id,
                        "⏭️跳过（无可用次数）",
                        seconds_until(before.get("sessionExpiresAt")),
                        before.get("status"),
                        photo=shot,
                        remaining_text=fmt_quota(q_now)
                        + "\n📅下次续期: "
                        + fmt_next_renewal(before.get("sessionExpiresAt")),
                    )
                except Exception as e:
                    log(f"跳过通知发送失败（不影响结果）: {e}")
                return None

            if int(before.get("sessionExtensionsToday") or 0) >= MAX_DAILY_EXTENSIONS and q_now["used_today"] == 0:
                log(
                    "sessionExtensionsDate="
                    f"{before.get('sessionExtensionsDate')} 不是今天（UTC），"
                    "今日计数以开机/当前 UTC 日为准（已用按 0）"
                )

            # 有续期次数 → 执行续期
            log(
                f"有可续期次数，开始续期。"
                f"{fmt_quota(q_now)} | {fmt_next_renewal(before.get('sessionExpiresAt'))}"
            )

            # ===== 单次运行内连续续期：每轮 = 点延伸 + 看 3 个广告 + 验证 +4h =====
            for round_no in range(1, max_ext + 1):
                cur = api_state(cfg, server_id)

                # Extend 前置守卫：服务器必须真正 running 才允许续期
                if not is_running(cur):
                    shortage = _region_shortage_check(page, cur)
                    if shortage:
                        stop_reason = f"检测到区域资源不足（{shortage}），本轮不自动换区，任务终止"
                        log(f"{stop_reason}（不点击 Extend、不执行任何广告）")
                        now = cur
                        break
                    stop_reason = (
                        f"服务器未运行（当前: {server_status_key(cur) or '未知'}），"
                        "不进入 Extend/续期"
                    )
                    log(f"{stop_reason}，结束续期流程")
                    now = cur
                    break

                q = quota(cur)
                used_today = q["used_today"]
                session_ext = q["session_ext"]
                log("-" * 60)
                log(
                    f"第 {round_no}/{max_ext} 轮：{fmt_quota(q)} | 到期 {cur.get('sessionExpiresAt')}"
                )
                if q["remaining"] <= 0:
                    if used_today >= MAX_DAILY_EXTENSIONS:
                        stop_reason = f"今日已达上限（{used_today}/{MAX_DAILY_EXTENSIONS}）"
                    elif session_ext >= MAX_SESSION_EXTENSIONS:
                        stop_reason = f"本会话已达上限（{session_ext}/{MAX_SESSION_EXTENSIONS}）"
                    else:
                        stop_reason = "可续期次数为 0"
                    log(f"到达平台限制：{stop_reason}，停止续期")
                    log_quota(cur, prefix="当前")
                    break

                # 点「延伸 / Extend」
                log("正在寻找「续期/延伸」按钮…")
                hit = click_anywhere(page, extend_labels, 45000)
                if not hit:
                    page.wait_for_timeout(5000)
                    # 弹窗（Cookie/公告）可能中途弹出挡住按钮，再点一次
                    for accept_txt in ("Accept", "Accept all", "同意", "接受", "OK"):
                        if click_anywhere(page, [accept_txt], 2000):
                            log(f"再次点掉弹窗: {accept_txt}")
                    hit = click_anywhere(page, extend_labels, 30000, exact=False)
                entered_direct = False
                if not hit:
                    # 部分版本面板没有「延伸」入口，续期入口就是 Watch ad 按钮本身
                    log("未找到「延伸」入口，尝试直接点击 Watch ad…")
                    hit2 = click_anywhere(page, watch_labels, 30000) or click_anywhere(
                        page, watch_labels, 15000, exact=False
                    )
                    if hit2:
                        log(f"已直接点击 Watch ad 作为续期入口: {hit2}")
                        hit = "Watch ad(直入)"
                        entered_direct = True
                if not hit:
                    log("未找到续期入口按钮")
                    if round_no == 1:
                        dump_page_debug(page, "找不到延伸按钮")
                        notify(
                            cfg,
                            "❌ Voer 续期失败",
                            [
                                f"服务器: <code>{short_id}</code>",
                                "原因: 未找到「延伸/续期」按钮",
                                "请查看 Actions 日志或 debug 截图",
                            ],
                            photo=pathlib.Path(_shot_name("debug_screenshot.png")),
                        )
                        return False
                    stop_reason = "找不到「延伸/续期」按钮"
                    break
                log(f"已点击续期入口: {hit}")
                page.wait_for_timeout(3000)

                if not entered_direct:
                    hit2 = click_anywhere(page, watch_labels, 30000)
                    if not hit2:
                        hit2 = click_anywhere(page, watch_labels, 20000, exact=False)
                    if not hit2:
                        log("未找到「观看广告」按钮（可能已直接进入广告流程）")
                    else:
                        log(f"已点击观看广告: {hit2}")
                log("已打开广告流程，等待 Ad ready…")
                page.wait_for_timeout(8000)

                total = int(cfg["ads_per_extension"])
                for i in range(1, total + 1):
                    hit = click_anywhere(
                        page, ["Watch ad", "觀看廣告", "观看广告"], 75000
                    )
                    if not hit:
                        log(f"第 {i} 个 Watch ad 未找到，停止")
                        break
                    log(f"已点击第 {i}/{total} 个 Watch ad（{hit}），播放中…")
                    page.wait_for_timeout(int(cfg["ad_duration_sec"]) * 1000)
                    closed = click_anywhere(page, ["Close", "關閉", "关闭"], 60000)
                    log(
                        f"第 {i} 个广告:",
                        f"已关闭（{closed}）" if closed else "未找到 Close（可能自动关闭）",
                    )
                    page.wait_for_timeout(6000)

                # 等待本轮 +4h 生效
                end = time.time() + 180
                round_ok = False
                while time.time() < end:
                    try:
                        now = api_state(cfg, server_id)
                    except SystemExit:
                        now = None
                    except Exception:
                        now = None
                    if now and (
                        now.get("sessionExtensions", 0) > cur.get("sessionExtensions", 0)
                        or now.get("sessionExpiresAt") != cur.get("sessionExpiresAt")
                    ):
                        rounds_ok += 1
                        round_ok = True
                        success = True
                        q_after = quota(now)
                        log(
                            f"第 {round_no} 轮续期成功 -> 新到期: {now.get('sessionExpiresAt')}"
                            f" | 累计: {now.get('sessionExtensions')} | 今日: {today_used(now)}"
                        )
                        log(f"本轮完成后实时{fmt_quota(q_after)}")
                        break
                    time.sleep(10)
                if not round_ok:
                    log(f"第 {round_no} 轮未检测到续期生效（广告未播完 / 页面卡住 / 已达上限），停止后续轮次")
                    stop_reason = stop_reason or "本轮未检测到续期生效"
                    now = now or cur
                    break

                # 给页面一点时间回到可再次「延伸」的状态
                page.wait_for_timeout(3000)

            # 结束前截一张最终画面
            last_shot = take_screenshot(page, "renew_screenshot.png")

        except SystemExit:
            raise
        except Exception as e:
            log(f"运行异常: {e}")
            try:
                dump_page_debug(page, "异常")
            except Exception:
                pass
            notify(
                cfg,
                "❌ Voer 续期异常",
                [
                    f"服务器: <code>{short_id}</code>",
                    f"错误: <code>{e}</code>",
                ],
                photo=pathlib.Path(_shot_name("debug_screenshot.png")),
            )
            return False
        finally:
            page.wait_for_timeout(1500)
            browser.close()

    # 结束后发通知
    if success:
        final_state = now or before
        # 下次可续期 = 本会话到期 − 现在
        uptime = seconds_until(final_state.get("sessionExpiresAt"))
        if uptime is None:
            uptime = seconds_until(before.get("sessionExpiresAt"))
        result = f"✅续期成功（+{rounds_ok * 4}h，共 {rounds_ok} 次）"
        photo = last_shot if (last_shot is not None and last_shot.exists()) else None
        if photo is None:
            cand = pathlib.Path(_shot_name("renew_screenshot.png"))
            photo = cand if cand.exists() else None
        remaining_text = fmt_quota(quota(final_state))
        next_text = fmt_next_renewal(final_state.get("sessionExpiresAt"))
        log(f"下次续期准确时间: {next_text}")
        notify_godlike(
            cfg,
            account,
            short_id,
            result,
            uptime,
            final_state.get("status"),
            photo=photo,
            remaining_text=remaining_text + "\n📅下次续期: " + next_text,
        )
        return True
    else:
        final_state = now or before
        uptime = seconds_until(final_state.get("sessionExpiresAt"))
        reason = stop_reason or "未检测到续期生效"
        photo = last_shot if (last_shot is not None and last_shot.exists()) else None
        if photo is None:
            for name in ("renew_screenshot.png", "debug_screenshot.png"):
                cand = pathlib.Path(_shot_name(name))
                if cand.exists():
                    photo = cand
                    break
        remaining_text = fmt_quota(quota(final_state)) if final_state else ""
        notify_godlike(
            cfg,
            account,
            short_id,
            f"⚠️续期未生效（{reason}）",
            uptime,
            final_state.get("status"),
            photo=photo,
            remaining_text=remaining_text,
        )
        return False


def main():
    cfg = load_config()
    server_ids = cfg.get("server_ids") or [cfg["server_id"]]

    # 优先邮箱密码登录；失败再使用 VOER_TOKEN
    if not ensure_token(cfg):
        log("无法获得有效 VOER_TOKEN，退出")
        sys.exit(1)

    # 取账号邮箱（用于通知；失败不影响续期）
    account = ""
    if "--status" not in sys.argv:
        account = fetch_account_email(cfg)
        if account:
            log(f"账号: {account}")
    else:
        try:
            account = account_email_from_server(api_state(cfg, server_ids[0]))
        except Exception:
            account = ""

    total = len(server_ids)
    results = []
    for idx, sid in enumerate(server_ids, 1):
        log("=" * 60)
        log(f"[{idx}/{total}] 开始处理服务器 {sid[:8]}…")
        log("=" * 60)
        # 每台服务器用独立截图文件名，避免互相覆盖
        os.environ["VOER_SHOT_TAG"] = str(idx)
        try:
            ok = run_server(cfg, sid, account=account)
        except SystemExit as e:
            # api_state 里 401/403 会 SystemExit(1)：token 失效对所有服务器一样，直接终止
            log(f"服务器 {sid[:8]}… 触发致命错误（exit={e.code}），停止全部任务")
            raise
        except Exception as e:
            log(f"服务器 {sid[:8]}… 发生未预期异常: {e}")
            ok = False
        results.append((sid, ok))

    log("=" * 60)
    log("全部服务器处理完毕，结果汇总:")
    fail = 0
    for sid, ok in results:
        if ok is True:
            mark = "✅ 成功"
        elif ok is None:
            mark = "⏭️ 跳过（无可用次数）"
        else:
            mark = "❌ 失败"
        if ok is False:
            fail += 1
        log(f"  {mark}  {sid[:8]}…")
    log(f"合计: 成功/跳过 {total - fail}/{total} 台（失败 {fail} 台）")
    log("=" * 60)
    if fail:
        sys.exit(3)


if __name__ == "__main__":
    main()
