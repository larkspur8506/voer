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
_TEST_TIMEOUT = int(os.environ.get("AD_TEST_TIMEOUT", "0")) or 45

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


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def _is_ad_frame(frame) -> bool:
    """判断 frame 是否是广告 iframe（wormies / Google Ads）。"""
    try:
        u = (frame.url or "").lower()
        return any(k in u for k in ("wormies", "googleads", "doubleclick", "googlesyndication"))
    except Exception:
        return False


def _non_ad_frames(page) -> list:
    """返回不包含广告 iframe 的 frame 列表（主面板）。"""
    try:
        return [f for f in page.frames if not _is_ad_frame(f)]
    except Exception:
        return []


def _wormies_frames(page) -> list:
    """返回 wormies/Voer 广告 frame（不含 Google Ads）。"""
    try:
        result = []
        for f in page.frames:
            try:
                u = (f.url or "").lower()
                if "wormies" in u or "voer-ads" in u:
                    result.append(f)
            except Exception:
                pass
        return result
    except Exception:
        return []


def _wait_for_wormies_frame(page, timeout_sec: int = 60) -> list:
    """等待 wormies/Voer 广告 frame 出现，返回 frame 列表；超时返回 []。

    若检测到「Ad availability is low」提示，刷新页面后重试。
    """
    deadline = time.time() + timeout_sec
    refresh_count = 0
    max_refreshes = 3
    while time.time() < deadline:
        # 检测广告库存不足提示
        if _check_ad_low_availability(page):
            log(f"  [AD] 检测到「Ad availability is low」提示，刷新页面（第 {refresh_count + 1} 次）…")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                refresh_count += 1
                if refresh_count >= max_refreshes:
                    log("  [AD] 已刷新多次仍告库存不足，停止等待")
                    return []
                continue
            except Exception as e:
                log(f"  [AD] 刷新页面失败: {e}")
                return []
        frames = _get_wormies_frames(page)
        if frames:
            log(f"  [AD] Wormies frame 已出现: {[f.url for f in frames]}")
            return frames
        # 同时打印当前所有 frame URL 供诊断
        try:
            all_urls = [(f.url or "") for f in page.frames]
            ad_urls = [u for u in all_urls if any(k in (u or "").lower() for k in ("wormies", "googleads", "doubleclick"))]
            log(f"  [AD] 等待 Wormies frame… 当前广告帧={ad_urls}")
        except Exception:
            pass
        time.sleep(3)
    log("  [AD] 等待 Wormies frame 超时")
    return []


def _get_wormies_frames(page) -> list:
    """与 _wormies_frames 相同，但用于内部调用以避免命名冲突。"""
    return _wormies_frames(page)


def _check_ad_low_availability(page) -> bool:
    """检测是否出现「广告库存不足」提示。出现时应当刷新页面重试。"""
    try:
        src = page.content() or ""
        low = src.lower()
        return (
            "ad availability is low" in low
            or "广告库存不足" in low
            or "广告额度不足" in low
            or "your verified progress is safe" in low
        )
    except Exception:
        return False


def _log_wormies_elements(frame, tag: str = ""):
    """打印 wormies frame 内所有可见元素的详细诊断信息。"""
    log(f"  [{tag}] --- 开始诊断 ---")
    try:
        # 所有文本节点
        texts = []
        try:
            all_texts = frame.locator("body").all_text_contents()
            if isinstance(all_texts, str):
                texts = [t.strip() for t in all_texts.split('\n') if t.strip()]
            elif isinstance(all_texts, list):
                for t in all_texts:
                    s = (t or "").strip()
                    if s and s not in texts:
                        texts.append(s)
        except Exception:
            pass
        log(f"  [{tag}] body 文本节点（前30个）: {texts[:30]}")
        # 所有可见元素类型和文本
        try:
            els = frame.locator("*").all()
            visible_types = {}
            for el in els[:100]:
                try:
                    if el.is_visible():
                        tag_name = el.evaluate("el => el.tagName").upper()
                        text = (el.inner_text(timeout=500) or "").strip()
                        key = f"{tag_name}('{text[:30]}')"
                        if key not in visible_types:
                            visible_types[key] = tag_name
                except Exception:
                    pass
            log(f"  [{tag}] 可见元素类型分布: {list(visible_types.values())[:30]}")
            log(f"  [{tag}] 可见非空元素示例: {[k for k in visible_types if 'Watch' in k or 'watch' in k or 'Close' in k or 'cancel' in k][:20]}")
        except Exception as e:
            log(f"  [{tag}] 元素扫描失败: {e}")
    except Exception as e:
        log(f"  [{tag}] 诊断异常: {e}")
    log(f"  [{tag}] --- 诊断结束 ---")


def click_in_frames(frames, texts, timeout_ms, exact=True):
    """仅在指定 frame 列表中搜索并点击。也支持非 button/link 元素（通过 get_by_text）。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
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
                    lambda t=t, f=frame: f.locator(f"text='{t}'").first,
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


def click_close_in_wormies(page, timeout_ms: int = 60000):
    """仅在 wormies/Voer 广告 frame 中找 Close/×，不触碰 Google Ads iframe。

    用于广告播放完成后的关闭操作。Google Ads iframe 里的 X/Close 可能是
    广告自带控件，绝对不能在这里点击。
    """
    frames = _wormies_frames(page)
    if not frames:
        return None
    return click_in_frames(
        frames,
        ["Close", "關閉", "关闭", "×", "X", "Done", "完成"],
        timeout_ms,
        exact=False,
    )


def _wait_for_ad_cleanup(page, timeout_sec: int = 30) -> bool:
    """等待广告 iframe（wormies/googleads）从页面中移除，确认回到主面板。

    返回 True 表示广告帧已清理/主面板已恢复；False 表示超时。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            frames = list(page.frames)
        except Exception:
            break
        if not any(_is_ad_frame(f) for f in frames):
            log("  [AD] 广告 iframe 已全部卸载，主面板已恢复")
            return True
        # 打印当前仍存在的广告 frame 供诊断
        ad_frames = [(f.url or "") for f in frames if _is_ad_frame(f)]
        log(f"  [AD] 广告帧仍存在: {ad_frames}")
        time.sleep(2)
    log("  [AD] 等待广告帧清理超时，可能未正常关闭")
    return False


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
    """判断当前是否存在尚未解决的 Cloudflare Turnstile Challenge。

    不能仅凭 page_source 里有 "cf-turnstile" / "challenge-platform" 就判定为需要处理，
    这些 JS/DOM 字符串在 Turnstile 通过后仍然存在于源码中。
    优先通过 WebDriver execute_script 检测实际渲染的 iframe/元素，
    失败再回退到文本关键词匹配。
    """
    solved_keywords = [
        "cf-mark-solved",
        "cf-circle-checked",
        "challenge-solved",
        "security-check-passed",
        "turnstile-success",
        "cf-challenge-running/solved",
    ]

    try:
        src = sb.get_page_source() or ""
        low = src.lower()
        if any(kw in low for kw in solved_keywords):
            return False
    except Exception:
        pass

    # 用 JavaScript 在已渲染的 DOM 中查询实际存在的 Turnstile 元素
    # 这比源码关键词更可靠，因为 solved 后的页面源码仍含 cf-turnstile 字符串
    js_checks = []
    try:
        driver = getattr(sb, "driver", None)
        if driver is not None:
            try:
                # 1) Turnstile challenge iframe（challenges.cloudflare.com 渲染出的 challenge）
                n = driver.execute_script(
                    "return document.querySelectorAll('iframe[src*=\"challenges.cloudflare.com\"]').length"
                )
                if int(n or 0) > 0:
                    log("  [CF] 检测到 challenges.cloudflare.com iframe")
                    return True
            except Exception:
                pass
            try:
                # 2) Turnstile response hidden input（challenge 未完成时存在）
                n = driver.execute_script(
                    "return document.querySelectorAll('[name=\"cf-turnstile-response\"]').length"
                )
                if int(n or 0) > 0:
                    log("  [CF] 检测到 cf-turnstile-response 元素")
                    return True
            except Exception:
                pass
            try:
                # 3) .cf-turnstile wrapper 元素（Turnstile 渲染容器）
                n = driver.execute_script(
                    "return document.querySelectorAll('.cf-turnstile').length"
                )
                if int(n or 0) > 0:
                    log("  [CF] 检测到 .cf-turnstile 元素")
                    return True
            except Exception:
                pass
            try:
                # 4) [data-sitekey] 元素（Turnstile widget 标记）
                n = driver.execute_script(
                    "return document.querySelectorAll('[data-sitekey]').length"
                )
                if int(n or 0) > 0:
                    log("  [CF] 检测到 [data-sitekey] 元素")
                    return True
            except Exception:
                pass
    except Exception as e:
        log(f"  [CF] WebDriver 检测异常，回退到文本检测: {e}")

    # 回退：文本关键词（作为最后手段）
    try:
        src = sb.get_page_source() or ""
        low = src.lower()
        if any(kw in low for kw in solved_keywords):
            return False
        if (
            "verify you are human" in low
            or "security verification" in low
            or "challenge-platform" in low
        ):
            log("  [CF] 通过文本关键词检测到 Challenge")
            return True
    except Exception:
        pass

    return False


def _sb_handle_turnstile(sb, max_retry: int = 4) -> bool:
    """参考 SkyMC：SeleniumBase UC 点击 Cloudflare Turnstile。"""
    if not _sb_challenge_visible(sb):
        log("未检测到需要处理的 Cloudflare Challenge，跳过")
        return True
    log("检测到 Cloudflare Challenge，开始处理")
    for i in range(max_retry):
        log(f"  Turnstile 第 {i + 1}/{max_retry} 次尝试")
        try:
            sb.uc_gui_click_captcha()
            log("  已调用 uc_gui_click_captcha")
            time.sleep(5)
            if not _sb_challenge_visible(sb):
                log("Cloudflare Challenge 已通过")
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
                    log("登录后仍见 Turnstile Challenge，再次处理…")
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


def click_start_button(page) -> str | None:
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


def _dump_ad_state(page, tag: str = ""):
    """采集广告流程关键节点的完整页面状态，供现场取证。"""
    sep = f"----- 广告状态诊断 [{tag}] -----"
    log(sep)
    log(f"  page.url = {page.url!r}")
    try:
        log(f"  page.title = {page.title()!r}")
    except Exception:
        pass
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for idx, f in enumerate(frames):
        try:
            url = f.url or ""
            title = f.title()
            is_ad = _is_ad_frame(f)
            # visible buttons
            btns = []
            try:
                for role in ("button", "link"):
                    try:
                        for loc in f.get_by_role(role).all()[:30]:
                            try:
                                if loc.is_visible():
                                    t = (loc.inner_text(timeout=500) or "").strip()
                                    if t and t not in btns:
                                        btns.append(t)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            # keyword elements
            kw_sels = [
                "[class*='ad']", "[class*='Ad']", "[data-ad]",
                "[role='button']", "button", "a",
            ]
            kw_elems = []
            try:
                for sel in kw_sels:
                    try:
                        for loc in f.locator(sel).all()[:20]:
                            try:
                                if loc.is_visible():
                                    t = (loc.inner_text(timeout=500) or "").strip()
                                    if t and t.lower() not in [x.lower() for x in kw_elems]:
                                        kw_elems.append(t)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            # video/audio
            vid_cnt = 0
            vid_info = []
            try:
                vids = f.locator("video").all()
                vid_cnt = len(vids)
                for v in vids[:3]:
                    try:
                        ci = v.evaluate("el => el.currentTime")
                        du = v.evaluate("el => el.duration")
                        pa = v.evaluate("el => el.paused")
                        ed = v.evaluate("el => el.ended")
                        src = v.evaluate("el => (el.src || '')")
                        vid_info.append(f"currentTime={ci}s duration={du}s paused={pa} ended={ed} src={src[:80]}")
                    except Exception:
                        pass
            except Exception:
                pass
            aud_cnt = 0
            try:
                auds = f.locator("audio").all()
                aud_cnt = len(auds)
            except Exception:
                pass
            log(f"  frame[{idx}] url={url!r} title={title!r} is_ad={is_ad} btns={btns[:15]} kw={kw_elems[:10]} video={vid_cnt} audio={aud_cnt}")
            for vi in vid_info:
                log(f"    video: {vi}")
        except Exception as e:
            log(f"  frame[{idx}] error: {e}")
    log(f"  --- 诊断结束 [{tag}] ---")


def _find_google_ads_close(page, timeout_sec: int) -> str | None:
    """在 Google Ads 全屏层中查找并点击 Close / CLOSE 按钮。

    只搜索含有 googleads / doubleclick / googlesyndication 的 frame，
    只点击文本为 Close 或 CLOSE 的按钮（不点击 RESUME）。
    返回描述字符串（如 "Close@https://googleads..."），超时返回 None。
    """
    deadline = time.time() + timeout_sec
    elapsed = 0
    while time.time() < deadline:
        elapsed += 1
        all_frames = []
        try:
            all_frames = list(page.frames)
        except Exception:
            pass
        ga_frames = [f for f in all_frames if any(
            k in (f.url or "").lower()
            for k in ("googleads", "doubleclick", "googlesyndication")
        )]
        if not ga_frames:
            log(f"  [TEST] {elapsed}/{timeout_sec}s Google Ads frame 未出现")
            time.sleep(1)
            continue
        for ga_frame in ga_frames:
            found = False
            # 1) 标准 button/link role
            for role in ("button", "link"):
                try:
                    for loc in ga_frame.get_by_role(role).all():
                        try:
                            if not loc.is_visible():
                                continue
                            t = (loc.inner_text(timeout=500) or "").strip()
                            if t in ("Close", "CLOSE"):
                                loc.click(timeout=3000)
                                return f"{t}@{ga_frame.url[:80]}"
                        except Exception:
                            pass
                except Exception:
                    pass
            # 2) 文本匹配：所有可见元素
            if not found:
                try:
                    for sel in ("[class*='close']", "[class*='Close']",
                                "[data-close]", "[aria-label='Close']",
                                "[aria-label='CLOSE']", "[title='Close']",
                                "[title='CLOSE']", "div", "span", "a"):
                        try:
                            for loc in ga_frame.locator(sel).all():
                                try:
                                    if not loc.is_visible():
                                        continue
                                    t = (loc.inner_text(timeout=500) or "").strip()
                                    if t in ("Close", "CLOSE"):
                                        loc.click(timeout=3000)
                                        return f"{t}@{ga_frame.url[:80]}"
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass
            # 3) get_by_text 兜底
            if not found:
                try:
                    for t in ("Close", "CLOSE"):
                        try:
                            loc = ga_frame.get_by_text(t, exact=True).first
                            if loc.count() and loc.is_visible():
                                loc.click(timeout=3000)
                                return f"{t}@{ga_frame.url[:80]}"
                        except Exception:
                            pass
                except Exception:
                    pass
        log(f"  [TEST] {elapsed}/{timeout_sec}s close_candidates=0，继续等待…")
        time.sleep(1)
    return None


def _is_google_fullscreen_visible(page) -> bool:
    """判断真正的 Google Ads 全屏广告层是否仍然可见。

    核心依据：wormies frame 的实际 window.location.href 是否包含
    #goog_fullscreen_ad。注意：frame.url 属性不含 hash，必须用 evaluate
    读取完整 URL；不能靠 frame 是否存在来判断。
    返回 True = 全屏广告层仍可见；False = 已关闭或根本不存在。
    """
    try:
        all_frames = list(page.frames)
    except Exception:
        return False
    for f in all_frames:
        try:
            url = (f.evaluate("location.href") or "").lower()
            if "wormies" in url and "goog_fullscreen_ad" in url:
                return True
        except Exception:
            pass
    return False


def _confirm_close_click(page, timeout_sec: int) -> bool:
    """点击 Close 后等待真正的全屏广告层消失。

    使用 _is_google_fullscreen_visible 精确判断，不把普通 Google Ads
    tracking iframe 误认为全屏广告层仍然存在。
    返回 True 表示全屏层已清理；False 表示超时仍未关闭。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not _is_google_fullscreen_visible(page):
            log("  [TEST] 全屏广告层已消失，Close 确认成功")
            return True
        try:
            for f in list(page.frames):
                url = (f.evaluate("location.href") or "").lower()
                if "wormies" in url and "goog_fullscreen_ad" in url:
                    log(f"  [TEST] fullscreen 仍可见: {url[:80]}")
                    break
            else:
                # 所有 wormies frame 的 hash 都已清除，但检测还没触发
                log("  [TEST] wormies frame hash 已清除，确认成功")
                return True
        except Exception:
            pass
        time.sleep(1)
    log("  [TEST] 等待全屏广告层消失超时")
    return False


def _dismiss_unlock_modal(page) -> bool:
    """检测并处理「Unlock more content」全站广告 modal。

    若存在该 modal，点击「View a short ad」完成单条广告后返回 True；
    若 modal 不存在则返回 False（不阻塞后续流程）。
    """
    try:
        body_text = page.locator("body").inner_text(timeout=5000) or ""
    except Exception:
        return False
    low = body_text.lower()
    if not ("unlock more content" in low or "view a short ad" in low or "site-wide access" in low):
        return False
    log("检测到「Unlock more content」全站广告 modal，开始处理…")
    # 点击 View a short ad
    ad_hit = click_anywhere(
        page, ["View a short ad", "View short ad", "view a short ad"], 15000
    )
    if not ad_hit:
        log("未找到 View a short ad 按钮，modal 可能已变化，跳过")
        return False
    log(f"已点击 View a short ad: {ad_hit}")
    # 等待 Wormies frame
    wormies = _wait_for_wormies_frame(page, timeout_sec=120)
    if not wormies:
        log("等待 Wormies frame 超时，modal 广告流程中断")
        return True
    log("Wormies frame 已出现，等待广告播放…")
    # 等待约 60 秒
    time.sleep(60)
    # 找 Google Ads Close
    close_hit = _find_google_ads_close(page, 10)
    if close_hit:
        log(f"找到 Google Ads Close，已点击: {close_hit}")
        _confirm_close_click(page, 10)
    else:
        log("Google Ads 层中未找到 Close，继续…")
    page.wait_for_timeout(2500)
    log("全站广告 modal 处理完成")
    return True


def _test_dump_final(page, tag: str = "TEST_FINAL"):
    """测试失败时的最终状态快照。"""
    log(f"----- [TEST] 最终状态诊断 [{tag}] -----")
    try:
        log(f"  page.url = {page.url!r}")
    except Exception:
        pass
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for idx, f in enumerate(frames):
        try:
            url = f.url or ""
            is_ad = any(k in url.lower() for k in ("wormies", "googleads", "doubleclick"))
            btns = []
            try:
                for role in ("button", "link"):
                    for loc in f.get_by_role(role).all()[:20]:
                        try:
                            if loc.is_visible():
                                t = (loc.inner_text(timeout=500) or "").strip()
                                if t and t not in btns:
                                    btns.append(t)
                        except Exception:
                            pass
            except Exception:
                pass
            log(f"  frame[{idx}] is_ad={is_ad} btns={btns[:10]} url={url[:80]!r}")
        except Exception:
            pass
    log(f"----- [TEST] 诊断结束 [{tag}] -----")


def watch_rewarded_ads(cfg, page, reason: str = "开机/续期") -> int:
    """点击 Watch ad 并等待播放，返回实际完成的广告数。

    流程：
      1. 进入广告流程（点击面板上的 Watch ad 入口）
      2. 等待 wormies frame 出现（最长 120s）
      3. 对每条广告：
          - 若 wormies frame 有 Watch ad 按钮 → 点击开始播放
          - 若已无 Watch ad 按钮（广告已开始）→ 等待帧消失
          - 等待 wormies frame 卸载（确认广告完成）
      4. 返回实际完成数
    """
    watch_labels = [
        "Watch ad",
        "觀看廣告",
        "观看广告",
        "Watch Ad",
        "Watch ads",
        "Watch Ads",
        "Watch",
        "开始",
        "開始",
    ]
    total = int(cfg.get("ads_per_extension") or 3)
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

    log(f"{reason}: 等待 Wormies frame 出现…")
    wormies = _wait_for_wormies_frame(page, timeout_sec=120)
    if not wormies:
        log(f"{reason}: 等待 Wormies frame 超时，广告流程无法开始")
        return watched

    log(f"{reason}: 检测到 Wormies frame，开始逐条广告")

    for i in range(1, total + 1):
        # ── 进入当前广告前：确认 wormies frame 存在 ──
        wormies = _get_wormies_frames(page)
        if not wormies:
            log(f"{reason}: 第 {i}/{total} 个广告前 Wormies frame 不存在，等待出现…")
            wormies = _wait_for_wormies_frame(page, timeout_sec=60)
            if not wormies:
                log(f"{reason}: 第 {i} 个广告等待 Wormies frame 超时，停止")
                break

        # ── 在 wormies frame 里判断当前状态 ──
        # 先检测 wormies frame 是否已经在播放广告（无需再点 Watch ad）
        ad_playing = False
        try:
            body_text = wormies[0].locator("body").inner_text(timeout=5000)
            if "Rewarded ad is playing" in body_text or "广告播放中" in body_text:
                ad_playing = True
                log(f"{reason}: 第 {i}/{total} 个广告：广告已在播放中，跳过点击")
        except Exception:
            pass

        if not ad_playing:
            # 在 wormies frame 里找 Watch ad 按钮
            ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 15000)
            if ad_hit:
                log(f"[AD] 已点击第 {i}/{total} 个 Watch ad，click result={ad_hit}")
                log(f"{reason}: 已点击第 {i}/{total} 个 Watch ad（{ad_hit}），播放中…")
            else:
                # 兜底：用 get_by_text 搜索
                try:
                    for t in ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"]:
                        try:
                            loc = wormies[0].get_by_text(t, exact=False).first
                            if loc.count() and loc.is_visible():
                                loc.click(timeout=3000)
                                ad_hit = f"{t}@{wormies[0].url[:60]}"
                                log(f"{reason}: 通过 get_by_text 找到并点击第 {i}/{total} 个 Watch ad: {ad_hit}")
                                break
                        except Exception:
                            pass
                except Exception:
                    pass
                if not ad_hit:
                    btns = []
                    try:
                        for role in ("button", "link"):
                            try:
                                for loc in wormies[0].get_by_role(role).all()[:20]:
                                    try:
                                        if loc.is_visible():
                                            txt = (loc.inner_text(timeout=500) or "").strip()
                                            if txt and txt not in btns:
                                                btns.append(txt)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception:
                        pass
                    log(f"{reason}: 第 {i} 个广告：Wormies frame 中未找到 Watch ad，可见按钮={btns}")

            # ── 检测「Ad availability is low」错误 ──
            if _check_ad_low_availability(page):
                log(f"{reason}: 第 {i} 个广告检测到「Ad availability is low」，刷新重试…")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                wormies = _wait_for_wormies_frame(page, timeout_sec=120)
                if not wormies:
                    log(f"{reason}: 刷新后仍无法进入广告，跳过本条")
                    continue
                # 重新检测播放状态
                try:
                    body_text = wormies[0].locator("body").inner_text(timeout=5000)
                    if "Rewarded ad is playing" in body_text or "广告播放中" in body_text:
                        ad_playing = True
                        log(f"{reason}: 第 {i}/{total} 个广告：刷新后广告已在播放中")
                except Exception:
                    pass
                if not ad_playing:
                    ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 30000)
                    if ad_hit:
                        log(f"{reason}: 刷新后已点击第 {i}/{total} 个 Watch ad: {ad_hit}")
                    else:
                        log(f"{reason}: 第 {i} 个广告刷新后仍无 Watch ad，跳过")
                        page.wait_for_timeout(2000)
                        continue

        # ── 检测「Ad availability is low」错误 ──
        if _check_ad_low_availability(page):
            log(f"{reason}: 第 {i} 个广告检测到「Ad availability is low」，刷新重试…")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            wormies = _wait_for_wormies_frame(page, timeout_sec=120)
            if not wormies:
                log(f"{reason}: 刷新后仍无法进入广告，跳过本条")
                continue

        # ── 等待广告正常播放约 60 秒，不依赖 Wormies body 文本变化判断完成 ──
        log(f"{reason}: 第 {i} 个广告：等待广告播放…")
        time.sleep(60)
        close_hit = _find_google_ads_close(page, 10)
        if close_hit:
            log(f"{reason}: 第 {i} 个广告：找到 Google Ads Close，已点击（{close_hit}）")
            _confirm_close_click(page, 10)
        else:
            log(f"{reason}: 第 {i} 个广告：Google Ads 层中未找到 Close")
        ad_done = True

        watched += 1
        log(f"{reason}: 第 {i}/{total} 个广告已完成")

        # 短暂停顿让页面稳定
        page.wait_for_timeout(2000)

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

    # ── 临时测试模式：AD_TEST_MODE=first → 验证第一条广告完整流程（含 Google Ads Close）──
    test_mode = os.environ.get("AD_TEST_MODE", "").strip().lower()
    if test_mode == "first":
        wormies = _wait_for_wormies_frame(page, timeout_sec=120)
        if not wormies:
            log("[TEST] 等待 Wormies frame 超时，测试失败")
            return 0
        log("[TEST] 已检测到 Wormies frame")
        ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 30000)
        if ad_hit:
            log(f"[TEST] 已点击第 1 个 Watch ad: {ad_hit}")
        else:
            try:
                for t in ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"]:
                    try:
                        loc = wormies[0].get_by_text(t, exact=False).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=3000)
                            log(f"[TEST] 通过 get_by_text 点击 Watch ad: {t}@{wormies[0].url[:60]}")
                            break
                    except Exception:
                        pass
            except Exception:
                pass
        log("[TEST] Watch ad 已点击，等待 Google Ads Close…")
        close_hit = _find_google_ads_close(page, _TEST_TIMEOUT)
        if close_hit:
            log(f"[TEST] 找到 Google Ads Close，已点击: {close_hit}")
            ok = _confirm_close_click(page, _TEST_TIMEOUT)
            if ok:
                log("[TEST] 第一条广告完成，全屏层已消失，测试成功")
            else:
                log("[TEST] Close 已点击但全屏层未消失，测试失败")
                raise RuntimeError("TEST_FAILED")
        else:
            log(f"[TEST] {_TEST_TIMEOUT}s 内未找到 Google Ads Close，测试失败")
            raise RuntimeError("TEST_FAILED")
        return 0  # 测试模式：提前退出，不触发 API 和状态等待

    if test_mode == "first":
        # 测试模式：不触发正式 API start，只跑广告流程
        pass
    elif test_mode == "all":
        # ── AD_TEST_MODE=all：完整跑通 3 条广告流程（不含正式续期）──
        total_ads = int(cfg.get("ads_per_extension") or 3)
        log(f"[TEST] all 模式：将测试 {total_ads} 条广告完整流程")
        for i in range(1, total_ads + 1):
            log(f"[TEST] ========== 第 {i}/{total_ads} 条广告 ==========")
            wormies = _wait_for_wormies_frame(page, timeout_sec=120)
            if not wormies:
                log(f"[TEST] 第 {i} 条广告：等待 Wormies frame 超时，测试失败")
                raise RuntimeError("TEST_FAILED")
            log(f"[TEST] 第 {i} 条广告：Wormies frame 已出现")
            ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 30000)
            if ad_hit:
                log(f"[TEST] 第 {i} 条广告：已点击 Watch ad: {ad_hit}")
            else:
                try:
                    for t in ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"]:
                        try:
                            loc = wormies[0].get_by_text(t, exact=False).first
                            if loc.count() and loc.is_visible():
                                loc.click(timeout=3000)
                                ad_hit = f"{t}@{wormies[0].url[:60]}"
                                log(f"[TEST] 第 {i} 条广告：通过 get_by_text 点击 Watch ad: {ad_hit}")
                                break
                        except Exception:
                            pass
                except Exception:
                    pass
                if not ad_hit:
                    log(f"[TEST] 第 {i} 条广告：未找到 Watch ad，测试失败")
                    raise RuntimeError("TEST_FAILED")
            log(f"[TEST] 第 {i} 条广告：等待 Google Ads Close…")
            close_hit = _find_google_ads_close(page, _TEST_TIMEOUT)
            if not close_hit:
                log(f"[TEST] 第 {i} 条广告：{_TEST_TIMEOUT}s 内未找到 Google Ads Close，测试失败")
                raise RuntimeError("TEST_FAILED")
            log(f"[TEST] 第 {i} 条广告：找到并点击 Close: {close_hit}")
            ok = _confirm_close_click(page, _TEST_TIMEOUT)
            if not ok:
                log(f"[TEST] 第 {i} 条广告：Close 后全屏层未消失，测试失败")
                raise RuntimeError("TEST_FAILED")
            log(f"[TEST] 第 {i}/{total_ads} 条广告完成 ✓")
            page.wait_for_timeout(2000)
        log(f"[TEST] {total_ads} 条广告全部完成，测试成功 ✓")
        return 0  # 测试模式：提前退出，不触发 API 和状态等待
    elif test_mode == "rounds":
        log("[TEST] rounds 模式将在 run_server 广告循环里执行")

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
    """仅检查是否关机：若为 stopped/offline 则开机或重启。

    其他状态（running / starting / crashed 等）一律不处理。
    返回 (server, did_power_on)。开机失败只记日志，不抛异常。
    """
    skip = str(os.environ.get("VOER_SKIP_RESTART", "")).strip().lower() in ("1", "true", "yes")
    if skip or not cfg.get("restart_if_stopped", True):
        return api_state(cfg, server_id), False

    server = api_state(cfg, server_id)
    st = server_status_key(server)
    log(f"开机状态: {st or '未知'} — {status_text(st)}")

    # 只处理明确关机/离线
    if not is_stopped(server):
        if is_running(server):
            log("服务器已在运行中，无需开机")
        elif is_starting(server):
            log("服务器启动中，等待就绪…")
            server = wait_for_status(cfg, server_id, RUNNING_STATUSES, timeout=360) or server
        else:
            log(f"当前状态非关机（{st or '未知'}），跳过开机/重启")
        return server, False

    log("检测到关机/离线：执行开机或重启")
    accepted = False
    need_ads = False
    for act in ("start", "restart"):
        log(f"发送电源指令: {act}")
        extra = {"adsCompleted": 0} if act == "start" else {}
        code, data = api_power(cfg, server_id, act, extra)
        if code in (200, 201, 202, 204):
            accepted = True
            if isinstance(data, dict) and data.get("server"):
                server = data["server"]
            break
        if ads_required_error(code, data):
            need_ads = True
            log("开机需要先看激励广告，改为在面板点击 Start 并播放广告")
            break
        log(f"{act} 未成功，尝试下一指令")

    if need_ads or not accepted:
        if page is not None:
            restart_via_panel(cfg, page, reason="关机后开机", server_id=server_id)
        elif need_ads:
            log("开机需要看广告，但当前没有浏览器会话，无法完成开机")
            return server, False

    # ── 测试模式：跳过开机状态等待 ──
    if os.environ.get("AD_TEST_MODE", "").strip().lower() in ("first", "all", "rounds"):
        log(f"[TEST] 测试模式跳过状态等待，直接返回")
        return server, True

    # 广告/指令后等待进入 running（缩短空等：若一直 stopped 则提前结束再重试）
    server = wait_for_status(cfg, server_id, RUNNING_STATUSES, timeout=180) or api_state(
        cfg, server_id
    )

    if not is_running(server) and page is not None:
        log("开机后仍未运行，再试一次面板开机+广告")
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        except Exception:
            pass
        restart_via_panel(cfg, page, reason="关机后开机（重试）", server_id=server_id)
        server = wait_for_status(
            cfg, server_id, RUNNING_STATUSES, timeout=180
        ) or api_state(cfg, server_id)

    if is_running(server):
        log("开机完成，服务器已在运行")
        return server, True

    log(
        f"开机后服务器仍未运行（当前状态: {server_status_key(server) or '未知'}），"
        "不报错，继续后续流程"
    )
    return server, False


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


def test_ad_flow(cfg, page, reason: str = "广告测试"):
    """临时测试模式：验证广告入口和轮次切换，不修改正式逻辑。

    AD_TEST_MODE=first   → 只测第 1 个广告入口（进入→等 wormies→点 Watch ad→退出）
    AD_TEST_MODE=rounds  → 测完整 3 轮（每轮完成后重新等待新 wormies frame）
    """
    test_mode = os.environ.get("AD_TEST_MODE", "").strip().lower()
    if test_mode not in ("first", "rounds"):
        return True  # 非测试模式，正常继续

    log(f"[TEST] AD_TEST_MODE={test_mode}，开始广告流程测试")

    # 找并点击面板上的 Watch ad 入口
    watch_labels = ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"]
    hit = click_anywhere(page, watch_labels, 30000) or click_anywhere(
        page, watch_labels, 15000, exact=False
    )
    if not hit:
        log(f"[TEST] 未找到 Watch ad 入口，测试失败")
        return False
    log(f"[TEST] 已点击 Watch ad 入口: {hit}")

    # 等待 Wormies frame 出现
    wormies = _wait_for_wormies_frame(page, timeout_sec=120)
    if not wormies:
        log("[TEST] 等待 Wormies frame 超时，测试失败")
        return False
    log("[TEST] 已检测到 Wormies frame")

    if test_mode == "first":
        # 在 wormies frame 里找 Watch ad 并点击
        ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 30000)
        if ad_hit:
            log(f"[TEST] 已找到并点击第 1 个 Watch ad: {ad_hit}")
        else:
            # 看 wormies frame 里有哪些按钮
            btns = []
            try:
                for role in ("button", "link"):
                    try:
                        for loc in wormies[0].get_by_role(role).all()[:20]:
                            try:
                                if loc.is_visible():
                                    t = (loc.inner_text(timeout=500) or "").strip()
                                    if t and t not in btns:
                                        btns.append(t)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            log(f"[TEST] Watch ad 未找到，wormies frame 可见按钮={btns}")
            log("[TEST] 第一条广告入口测试完成（Watch ad 未出现，可能已自动开始）")
        log("[TEST] 第一条广告入口测试成功，停止测试")
        return True

    # ── rounds 模式：循环 3 条广告，验证每轮 wormies frame 重新出现 ──
    for i in range(1, 4):
        # 等待 wormies frame
        wormies = _get_wormies_frames(page)
        if not wormies:
            log(f"[TEST] 第 {i}/3 条广告前：等待新的 Wormies frame…")
            wormies = _wait_for_wormies_frame(page, timeout_sec=60)
            if not wormies:
                log(f"[TEST] 第 {i}/3 条广告等待 Wormies frame 超时，测试停止")
                break
        log(f"[TEST] 第 {i}/3 条：Wormies frame 已就绪")

        # 在 wormies frame 里找 Watch ad
        ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 30000)
        if ad_hit:
            log(f"[TEST] 第 {i}/3 条：已点击 Watch ad: {ad_hit}")
        else:
            log(f"[TEST] 第 {i}/3 条：Wormies frame 中未找到 Watch ad（可能已开始）")

        # 等待 wormies frame 卸载（广告完成）
        log(f"[TEST] 第 {i}/3 条：等待 Wormies frame 卸载…")
        end = time.time() + 180
        gone = False
        while time.time() < end:
            if not _get_wormies_frames(page):
                gone = True
                log(f"[TEST] 第 {i}/3 条：Wormies frame 已卸载，广告完成")
                break
            time.sleep(3)
        if not gone:
            log(f"[TEST] 第 {i}/3 条：等待 wormies frame 卸载超时")
            break

        # 短暂停顿后进入下一轮（frame 消失后会自动重新出现）
        page.wait_for_timeout(3000)

    log("[TEST] 广告轮次切换测试完成")
    return True


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
    reason = "续期"

    watch_labels = [
        "觀看廣告",
        "观看广告",
        "Watch ad",
        "Watch Ad",
        "Watch ads",
        "Watch Ads",
        "Watch",
        "开始",
        "開始",
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

            # 只检查是否关机：关机则开机/重启（不抛错）
            before, did_power = ensure_running(cfg, server_id, page=page)

            # ── 测试模式：确保运行后直接退出，不走后续续期流程 ──
            if os.environ.get("AD_TEST_MODE", "").strip().lower() in ("first", "all", "rounds"):
                log("[TEST] 测试模式：跳过后续续期流程，正常退出")
                return True

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

                # ── 处理全站广告 modal（会触发单条广告）──
                _dismiss_unlock_modal(page)

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
                page.wait_for_timeout(3000)

                # ── 临时测试模式：在广告流程入口处拦截（仅 rounds 模式）──
                if os.environ.get("AD_TEST_MODE", "").strip().lower() == "rounds":
                    test_ad_flow(cfg, page, reason="关机后开机")
                    log("[TEST] 测试模式结束，正常退出")
                    return True

                total = int(cfg["ads_per_extension"])
                # 记住本轮开始时的主面板 URL，用于校验是否成功回到主面板
                main_url_at_start = page.url
                log(f"{reason}: 等待 Wormies frame 出现…")
                wormies = _wait_for_wormies_frame(page, timeout_sec=120)
                if not wormies:
                    log(f"{reason}: 等待 Wormies frame 超时，无法进入广告流程")
                else:
                    for i in range(1, total + 1):
                        # 进入每条广告前：确认 wormies frame 存在
                        wormies = _get_wormies_frames(page)
                        if not wormies:
                            log(f"[AD] 第 {i}/{total} 个广告前等待 Wormies frame…")
                            wormies = _wait_for_wormies_frame(page, timeout_sec=60)
                            if not wormies:
                                log(f"[AD] 第 {i} 个广告等待 Wormies frame 超时，停止")
                                break
                        # ── 在 wormies frame 里判断当前状态 ──
                        # 先检测是否已在播放广告
                        ad_playing = False
                        try:
                            body_text = wormies[0].locator("body").inner_text(timeout=5000)
                            if "Rewarded ad is playing" in body_text or "广告播放中" in body_text:
                                ad_playing = True
                                log(f"[AD] 第 {i}/{total} 个广告：广告已在播放中，跳过点击")
                        except Exception:
                            pass

                        if not ad_playing:
                            # 在 wormies frame 里找 Watch ad
                            ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 15000)
                            if ad_hit:
                                log(f"[AD] 已点击第 {i}/{total} 个 Watch ad，click result={ad_hit}")
                                log(f"已点击第 {i}/{total} 个 Watch ad（{ad_hit}），播放中…")
                            else:
                                # 兜底：用 get_by_text 搜索
                                try:
                                    for t in ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"]:
                                        try:
                                            loc = wormies[0].get_by_text(t, exact=False).first
                                            if loc.count() and loc.is_visible():
                                                loc.click(timeout=3000)
                                                ad_hit = f"{t}@{wormies[0].url[:60]}"
                                                log(f"[AD] 通过 get_by_text 点击第 {i}/{total} 个 Watch ad: {ad_hit}")
                                                break
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                if not ad_hit:
                                    btns = []
                                    try:
                                        for role in ("button", "link"):
                                            try:
                                                for loc in wormies[0].get_by_role(role).all()[:20]:
                                                    try:
                                                        if loc.is_visible():
                                                            txt = (loc.inner_text(timeout=500) or "").strip()
                                                            if txt and txt not in btns:
                                                                btns.append(txt)
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    log(f"第 {i} 个广告：Wormies frame 中未找到 Watch ad，可见按钮={btns}")

                            # ── 检测「Ad availability is low」错误 ──
                            if _check_ad_low_availability(page):
                                log(f"[AD] 第 {i} 个广告检测到「Ad availability is low」，刷新页面重试…")
                                try:
                                    page.reload(wait_until="domcontentloaded", timeout=30000)
                                    page.wait_for_timeout(3000)
                                except Exception:
                                    pass
                                wormies = _wait_for_wormies_frame(page, timeout_sec=60)
                                if not wormies:
                                    log(f"[AD] 刷新后仍无法进入广告流程，停止")
                                    break
                                # 重新检测播放状态
                                try:
                                    body_text = wormies[0].locator("body").inner_text(timeout=5000)
                                    if "Rewarded ad is playing" in body_text or "广告播放中" in body_text:
                                        ad_playing = True
                                        log(f"[AD] 刷新后广告已在播放中")
                                except Exception:
                                    pass
                                if not ad_playing:
                                    ad_hit = click_in_frames(wormies, ["Watch ad", "觀看廣告", "观看广告", "Watch Ad"], 15000)
                                    if not ad_hit:
                                        log(f"[AD] 第 {i} 个广告刷新后仍无 Watch ad，跳过本轮")
                                        page.wait_for_timeout(2000)
                                        continue
                                    log(f"[AD] 刷新后重新点击第 {i} 个 Watch ad: {ad_hit}")
                        # ── 等待广告正常播放约 60 秒，不依赖 Wormies body 文本变化判断完成 ──
                        log(f"[AD] 第 {i} 个广告：等待广告播放…")
                        time.sleep(60)
                        close_hit = _find_google_ads_close(page, 10)
                        if close_hit:
                            log(f"[AD] 第 {i} 个广告：找到 Google Ads Close，已点击（{close_hit}）")
                            _confirm_close_click(page, 10)
                        else:
                            log(f"[AD] 第 {i} 个广告：Google Ads 层中未找到 Close")
                        ad_done = True
                        page.wait_for_timeout(2000)

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
