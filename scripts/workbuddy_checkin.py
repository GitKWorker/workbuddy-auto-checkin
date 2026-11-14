# -*- coding: utf-8 -*-
# workbuddy-auto-checkin: WorkBuddy(Buddy加油站)每日签到 + 企微推送
#
# 命令: run / status / checkin / point / cat_status / cat_depart / cat_claim
#       / streak / streak_claim / push_server / tasks
#   run         一键: 查状态 -> 未签则签到 -> 查积分 -> 领连登档位 -> 派猫(领奖/自动出发) -> 推送企微
#               若存在 WORKBUDDY_CONFIG 环境变量(JSON)则多账号模式: 逐台执行并分别推送
#   run quiet   静默模式: 只有真领到奖励或出错时才推送(适合一天多档补充巡检)
#   status      仅查询签到状态(连签天数/本周进度)
#   checkin     未签到则签到, 已签到幂等跳过
#   point       查询积分余额(接口已下线时会明确提示)
#   cat_status  查询猫猫(昵称/稀有度)与旅行行程(目的地/ETA/奖励), 空闲时列出可选目的地
#   cat_depart  派猫出发: 空闲且有当日名额就出发(地点按 tasks.cat_travel.location, 默认随机)
#   cat_claim   尝试领取派猫旅行奖励(未到达时明确提示, 不算失败)
#   streak      查看连登天数与各档位状态
#   streak_claim 领取已达标(available)的连登档位
#   push_server 把本机最新登录态推送到青龙面板(单一 WORKBUDDY_CONFIG JSON 环境变量, 按 MAC 幂等合并)
#
# 凭证自动解析(按优先级, 全程不落盘):
#   1. 环境变量 WORKBUDDY_ACCESS_TOKEN / CODEBUDDY_ACCESS_TOKEN (明文 JWT)
#   2. 自动解密本机 WorkBuddy 客户端登录态:
#      用 ELECTRON_RUN_AS_NODE=1 借 WorkBuddy.exe 运行时执行同目录 wb_auth_helper.js,
#      从客户端原生绑定取得 atRestSecretKey 并 AES-256-GCM 解密 auth 文件里的信封。
#      要求: WorkBuddy 客户端正在运行且已登录, 与脚本同一 Windows 用户。
#   3. 失败时提示配置环境变量。
#
# 可选环境变量:
#   QW_WEBHOOK         企业微信群机器人 webhook key(仅 run 推送时需要)
#   WB_ACCOUNT_NOTE    账号备注(推送里额外的 🏷️ 备注行, 可留空)
#   WB_ACCOUNT_NAME    手动指定账号名(仅在用 access_token 环境变量时生效; 自动解密会取昵称)
#   WB_DEVICE_NAME     设备名(默认自动取系统主机名)
#   WB_DEVICE_IP       设备 IP(默认自动取内网 IP + 公网出口 IP, 优先 IPv4)
#   WB_CLIENT_VERSION  客户端版本号(默认读 install-manifest.json, 失败退回 exe 文件版本)
#   WORKBUDDY_USER_ID  手动指定 uid(一般自动从 auth 文件读取)
#
# 推送内容包含: 账号名(客户端昵称 + 掩码手机号) / 设备名 / 设备 IP /
#               签到状态与本周进度 / 剩余积分(含额度分项)

import base64
import ipaddress
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

API_BASE = "https://www.codebuddy.cn/v2/billing/meter"
API_ORIGIN = "https://www.codebuddy.cn"
TIMEOUT = 30

# 积分汇总接口: 与签到不同, 它在 /v2 之外 (客户端 resourcePrefix 为空)
BALANCE_PATH = "/billing/meter/get-user-resource-summary"

# 派猫旅行(2026-11 客户端新版接口, 旧 /v2/billing/meter/cat-travel-* 已下线):
#   GET  /v2/activity/growth/buddy/info              猫猫档案(服务端自动旅行, 客户端每 poll_interval 秒轮询)
#   GET  /activity/growth/buddy/travel/status        行程状态(state/location/arrive_at/reward_credit)
#   POST /activity/growth/buddy/travel/claim         领取到达奖励(未到达返回 403 "not arrived yet")
BUDDY_INFO_URL = "https://www.codebuddy.cn/v2/activity/growth/buddy/info"
TRAVEL_STATUS_URL = "https://www.codebuddy.cn/activity/growth/buddy/travel/status"
TRAVEL_CLAIM_URL = "https://www.codebuddy.cn/activity/growth/buddy/travel/claim"

# 幂等错误码: 不算失败
CODE_OK = 0
CODE_ALREADY_CHECKIN = 10001

WIN_AUTH_FILE = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
)
MAC_AUTH_FILE = (
    Path.home() / "Library" / "Application Support" /
    "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
)
HELPER_JS = Path(__file__).resolve().parent / "wb_auth_helper.js"
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "logs" / "checkin.log"      # 脚本自写日志(带日期, 自动滚动)
DEBUG_LOG = SCRIPT_DIR / "debug.log"                # Electron crashpad 噪音日志


def _log_keep_days():
    """日志保留天数: config.log_keep_days, 默认 30。"""
    try:
        v = int(_load_config().get("log_keep_days", 30))
        return v if v > 0 else 30
    except Exception:
        return 30


def log(msg):
    """打印并在 logs/checkin.log 追加一行(带完整日期, 便于按天滚动清理)。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _cutoff(days):
    return date.today() - timedelta(days=max(0, int(days)))


def _trim_file(path, parser):
    """按行时间戳清理: 返回 (保留行数, 删除行数)。无时间戳的行保留。"""
    try:
        if not path.exists():
            return 0, 0
        cutoff = _cutoff(_log_keep_days())
        kept, removed = [], 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines(True):
            d = parser(line)
            if d is None or d >= cutoff:
                kept.append(line)
            else:
                removed += 1
        if removed:
            path.write_text("".join(kept), encoding="utf-8")
        return len(kept), removed
    except Exception:
        return -1, -1


def _log_date(line):
    m = re.match(r"\[(\d{4}-\d{2}-\d{2})[ T]", line)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _crashpad_date(line):
    # debug.log 行形如 [1112/192110.865:ERROR:...], MMDD/HHMMSS, 无年份
    m = re.match(r"\[(\d{2})(\d{2})/\d{6}\.", line)
    if not m:
        return None
    try:
        d = date(date.today().year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    # 月日比今天靠后说明是去年同期的记录
    if d > date.today():
        d = date(d.year - 1, d.month, d.day)
    return d


def trim_old_logs():
    """启动时自动清理: 只保留最近 N 天(config.log_keep_days, 默认 30)的日志。"""
    k1, r1 = _trim_file(LOG_FILE, _log_date)
    k2, r2 = _trim_file(DEBUG_LOG, _crashpad_date)
    if r1 > 0:
        print(f"[log] checkin.log 清理 {r1} 行(> {_log_keep_days()} 天), 保留 {k1} 行", flush=True)
    if r2 > 0:
        print(f"[log] debug.log 清理 {r2} 行(> {_log_keep_days()} 天), 保留 {k2} 行", flush=True)


# ---------- 凭证解析 ----------

def _token_from_env():
    """手动指定的明文 token。账号名取 WB_ACCOUNT_NAME 或 config.account_name。"""
    cfg = _load_config()
    account = (os.environ.get("WB_ACCOUNT_NAME", "").strip()
               or str(cfg.get("account_name", "")).strip())
    for name in ("WORKBUDDY_ACCESS_TOKEN", "CODEBUDDY_ACCESS_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            uid = os.environ.get("WORKBUDDY_USER_ID", "").strip()
            return token, uid, f"env:{name}", account
    cfg_token = str(cfg.get("access_token", "")).strip()
    if cfg_token:
        uid = (os.environ.get("WORKBUDDY_USER_ID", "").strip()
               or str(cfg.get("user_id", "")).strip())
        return cfg_token, uid, f"config:{CONFIG_FILE.name}", account
    return None


def _decrypt_local_auth():
    """借 WorkBuddy.exe 运行时解密本机登录态, 返回 (token, uid, source, account) 或 None。

    account 为可读账号名: 昵称 > 掩码手机号 > uid 前 8 位。
    """
    exe = _find_workbuddy_exe()
    auth_file = next((p for p in (WIN_AUTH_FILE, MAC_AUTH_FILE) if p and p.exists()), None)
    if not exe or not auth_file or not HELPER_JS.exists():
        return None
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
    try:
        proc = subprocess.run(
            [exe, str(HELPER_JS), str(auth_file), "accessToken", "nickname", "phoneNumber"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        out = proc.stdout.strip().splitlines()
        if not out:
            return None
        result = json.loads(out[-1])
        if not result.get("ok"):
            return None
        fields = result.get("fields", {})
        token = fields.get("accessToken", "").strip()
        if not token:
            return None
        uid = fields.get("uid", "")
        account = _format_account(fields.get("nickname"), fields.get("phoneNumber"), uid)
        return token, uid, f"local:{auth_file.name}", account
    except Exception:
        return None


def _format_account(nickname, phone, uid):
    """拼可读账号名: 昵称 (掩码手机号) > 掩码手机号 > uid 前 8 位。"""
    nickname = (nickname or "").strip()
    phone = (phone or "").strip()
    masked = f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 8 else ""
    if nickname and masked:
        return f"{nickname} ({masked})"
    if nickname:
        return nickname
    if masked:
        return masked
    return f"uid:{uid[:8]}..." if uid else "未知账号"


def get_device_name():
    """设备名: WB_DEVICE_NAME 环境变量 > config.device_name > 系统主机名。"""
    name = (os.environ.get("WB_DEVICE_NAME", "").strip()
            or str(_load_config().get("device_name", "")).strip())
    if name:
        return name
    name = (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "").strip()
    if not name:
        try:
            import socket
            name = socket.gethostname()
        except Exception:
            name = ""
    return name or "未知设备"


def _is_v4(addr):
    """是否合法 IPv4(用标准库判断, 不靠正则)。"""
    try:
        ipaddress.IPv4Address(addr)
        return True
    except Exception:
        return False


def _is_v6(addr):
    try:
        ipaddress.IPv6Address(addr)
        return True
    except Exception:
        return False


def get_local_ip():
    """本机内网 IP: 优先 IPv4, 只有 IPv6 时才返回 IPv6。

    UDP connect 只取路由选中的出口地址, 不实际发包。
    """
    import socket
    # 1) IPv4: 连 IPv4 地址, 内核会挑 IPv4 出口
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            if _is_v4(ip) and not ip.startswith("127."):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    # 2) IPv4 兜底: 主机名解析
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if _is_v4(ip) and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # 3) 只有 IPv6 的网络: 连 IPv6 地址(阿里 DNS v6)
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            s.connect(("2400:3200::1", 80))
            ip = s.getsockname()[0]
            if _is_v6(ip) and not ip.startswith("fe80") and ip != "::1":
                return ip
        finally:
            s.close()
    except Exception:
        pass
    return ""


PUBLIC_IP_V4_URLS = (
    "https://ipv4.icanhazip.com",   # 只有 A 记录, 强制走 IPv4
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)
PUBLIC_IP_V6_URLS = (
    "https://api6.ipify.org",       # 只有 AAAA 记录, 强制走 IPv6
    "https://ipv6.icanhazip.com",
)


def get_public_ip(timeout=3):
    """公网出口 IP: 优先 IPv4 接口, 全部失败才试 IPv6 专用接口。

    返回纯地址(不带标签); 都取不到返回空串, 不影响推送。
    """
    def fetch(url, want_v6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                addr = resp.read().decode("utf-8", errors="replace").strip()
            if len(addr) > 45 or " " in addr:
                return ""
            return addr if (_is_v6(addr) if want_v6 else _is_v4(addr)) else ""
        except Exception:
            return ""

    for url in PUBLIC_IP_V4_URLS:
        addr = fetch(url, False)
        if addr:
            return addr
    for url in PUBLIC_IP_V6_URLS:
        addr = fetch(url, True)
        if addr:
            return addr
    return ""


def get_device_ip():
    """设备 IP: WB_DEVICE_IP / config.device_ip 覆盖, 否则 '内网 / 公网'。

    默认只推 IPv4; 本机没有 IPv4 时才退回 IPv6(会标注 [IPv6])。
    """
    fixed = (os.environ.get("WB_DEVICE_IP", "").strip()
             or str(_load_config().get("device_ip", "")).strip())
    if fixed:
        return fixed
    local = get_local_ip()
    show_public = _load_config().get("public_ip", True)
    public = get_public_ip() if show_public else ""
    local_s = f"[IPv6] {local}" if local and _is_v6(local) else local
    public_s = f"公网[IPv6] {public}" if public and _is_v6(public) else f"公网 {public}" if public else ""
    if local_s and public_s and public != local:
        return f"{local_s} / {public_s}"
    return local_s or public_s or "未知IP"


def _exe_file_version(path):
    """Windows PE 文件版本(ctypes 读 VS_FIXEDFILEINFO), 取不到返回空串。"""
    try:
        import ctypes
        from ctypes import wintypes

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD), ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD), ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return ""
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return ""
        info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
        out = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        return out[:-2] if out.endswith(".0") else out
    except Exception:
        return ""


_CLIENT_VERSION = None


def get_client_version():
    """WorkBuddy 客户端版本号: WB_CLIENT_VERSION 覆盖 > install-manifest.json > exe 文件版本。

    读不到(如服务器/青龙面板没有客户端)返回空串, 推送里就不显示这一行。
    """
    global _CLIENT_VERSION
    if _CLIENT_VERSION is not None:
        return _CLIENT_VERSION
    ver = os.environ.get("WB_CLIENT_VERSION", "").strip()
    exe = _find_workbuddy_exe()
    if not ver and exe:
        base = Path(exe).resolve().parent
        for manifest in (base / "resources" / "install-manifest.json",
                         base / "Contents" / "Resources" / "install-manifest.json"):
            if not manifest.exists():
                continue
            try:
                ver = str(json.loads(manifest.read_text(encoding="utf-8")).get("appVersion") or "").strip()
            except Exception:
                ver = ""
            if ver:
                break
        if not ver:
            ver = _exe_file_version(exe)
    _CLIENT_VERSION = ver
    return ver


def _find_workbuddy_exe():
    candidates = [
        Path("C:/Program Files/WorkBuddy/WorkBuddy.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
        Path("/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"),
        Path("/Applications/WorkBuddy.app/Contents/MacOS/Electron"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def load_credentials():
    """返回 (token, uid, source, account, note, device, ip)。

    account 账号名(昵称/掩码手机号) / note 备注 / device 设备名 / ip 设备 IP。
    """
    creds = _token_from_env() or _decrypt_local_auth()
    if creds:
        token, uid, source, account = creds
        cfg = _load_config()
        note = (os.environ.get("WB_ACCOUNT_NOTE", "").strip()
                or str(cfg.get("account_note", "")).strip())
        account = account or note or _format_account("", "", uid)
        return token, uid, source, account, note, get_device_name(), get_device_ip()
    return None


# ---------- 接口调用 ----------

def api(path, token, uid, payload=None):
    """调 /v2/billing/meter 下的签到接口。"""
    return _post(f"{API_BASE}/{path}", token, uid, payload)


def api_origin(path, token, uid, payload=None):
    """调站点根路径下的接口(积分汇总等)。"""
    return _post(f"{API_ORIGIN}{path}", token, uid, payload)


def _post(url, token, uid, payload=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid or "",
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy-Checkin-Bot/1.0",  # 缺失会被网关 403
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # 注意: 幂等情况(如 code=10001 今日已签到)会以 HTTP 4xx 返回, body 里仍是业务 JSON
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"code": e.code, "msg": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return -1, {"code": -1, "msg": str(e)}


def _get(url, token, uid):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid or "",
        "Accept": "application/json",
        "User-Agent": "WorkBuddy-Checkin-Bot/1.0",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"code": e.code, "msg": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return -1, {"code": -1, "msg": str(e)}


def checkin_status(token, uid):
    st, body = api("checkin-activity-status", token, uid)
    if st == 200 and body.get("code") == 0:
        return True, body.get("data") or {}
    return False, body


def do_checkin(token, uid):
    st, body = api("daily-checkin", token, uid)
    code = body.get("code")
    if code == 0 or code == CODE_ALREADY_CHECKIN:
        detail = "今日已签到(跳过)" if code == CODE_ALREADY_CHECKIN else "签到成功"
        data = body.get("data") or {}
        return True, detail, data
    return False, f"签到失败: code={body.get('code')} {body.get('msg', '')}", body.get("data") or {}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_credit(v):
    """786.46000014 -> '786.46', 400.0 -> '400'"""
    if v is None:
        return "?"
    if abs(v - round(v)) < 0.01:
        return str(int(round(v)))
    return f"{v:.2f}"


def get_balance(token, uid):
    """剩余积分。返回 (info, raw); info 为 None 表示查询失败。

    info = {total, plan, paid, groups: [(名称, 剩余, 总额)]}
    """
    st, body = api_origin(BALANCE_PATH, token, uid)
    data = body.get("data")
    if body.get("code") != 0 or not isinstance(data, dict):
        return None, body
    total = _num(data.get("TotalCredits"))
    plan = (data.get("SubscriptionPackageName") or "").strip()
    if not plan:
        plan = "付费版" if data.get("IsPaidUser") else "免费版"
    groups = []
    for g in data.get("CreditGroups") or []:
        rem = _num(g.get("CycleRemainCapacity"))
        tot = _num(g.get("CycleTotalCapacity"))
        if not tot:  # 只列有额度的分组
            continue
        groups.append((g.get("Name") or g.get("Kind") or "积分", rem, tot))
    # 汇总缺失时以分组剩余之和兜底
    if total is None and groups:
        total = sum(r or 0 for _, r, _ in groups)
    return {"total": total, "plan": plan, "paid": bool(data.get("IsPaidUser")),
            "groups": groups}, body


def balance_line(info):
    """推送用的一行积分摘要。"""
    if not info:
        return "💰 积分: 查询失败"
    return f"💰 剩余积分: {fmt_credit(info['total'])} ({info['plan']})"


def balance_detail(info):
    """分项额度, 如 '套餐基础积分 386.46/500 · 平台奖励积分 400/400'。"""
    if not info or not info["groups"]:
        return ""
    return " · ".join(f"{name} {fmt_credit(rem)}/{fmt_credit(tot)}"
                      for name, rem, tot in info["groups"])


# ---------- 消耗统计(接口只有"本周期已用", 按账号本地记账做差分) ----------
USAGE_STATE = SCRIPT_DIR / "logs" / "usage_state.json"


def _load_usage_state():
    try:
        return json.loads(USAGE_STATE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_usage_state(state):
    try:
        USAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def used_credits(info):
    """本周期已消耗积分(套餐内额度). 接口无"今日已用", 用 总额-剩余 求和。"""
    if not info:
        return None
    return round(sum(max(0.0, tot - (rem or 0)) for _, rem, tot in info["groups"]), 2)


def consume_line(uid, used_now):
    """消耗一行: 距上次统计之间消耗了多少积分。

    接口只给"本周期已用"累计值, 所以用上次运行时的快照做差分。
    单日一次定时任务时, 该差值≈上次统计至今(约24小时)的消耗; 首次运行只记基线不出行。
    """
    if used_now is None:
        return None
    state = _load_usage_state()
    key = uid or "default"
    prev = state.get(key) or {}
    ts_prev, used_prev = prev.get("ts"), prev.get("used")
    state[key] = {"ts": int(time.time()), "used": used_now,
                  "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _save_usage_state(state)
    if used_prev is None:
        return None
    delta = round(used_now - float(used_prev), 2)
    if delta < 0:
        # 月度额度重置或退积分, 差值无意义, 本次不出行
        return None
    span = ""
    if ts_prev:
        hours = (time.time() - float(ts_prev)) / 3600
        span = "近 24 小时" if 20 <= hours <= 28 else (
            f"近 {hours:.0f} 小时" if hours >= 1 else "一会儿")
        span += f", 上次统计 {datetime.fromtimestamp(float(ts_prev)).strftime('%m-%d %H:%M')}"
    return f"📉 消耗: {fmt_credit(delta)} 积分" + (f" ({span})" if span else "")


def get_buddy_info(token, uid):
    """猫猫档案。返回 dict 或 None。buddy 含 name/rarity/personality 等字段。"""
    st, body = _get(BUDDY_INFO_URL, token, uid)
    data = body.get("data")
    if body.get("code") != 0 or not isinstance(data, dict):
        return None
    buddy = data.get("buddy") or {}
    if not isinstance(buddy, dict):
        return None
    return {
        "name": (buddy.get("name") or "").strip(),
        "rarity": (buddy.get("rarity") or "").strip(),
        "personality": (buddy.get("personality") or "").strip(),
        "poll_seconds": data.get("poll_interval_seconds"),
    }


def get_travel_status(token, uid):
    """行程状态。返回 dict 或 None。

    实测字段: state(traveling/...), location{name, duration_hours},
    depart_at/arrive_at/server_now(Unix 秒), reward_credit, daily_limit_reached
    """
    st, body = _get(TRAVEL_STATUS_URL, token, uid)
    data = body.get("data")
    if body.get("code") != 0 or not isinstance(data, dict):
        return None
    return data


def fmt_eta(seconds):
    """秒数 -> '2小时29分' / '45分钟' / '不到1分钟'。"""
    if seconds is None or seconds < 0:
        return "?"
    m = int(seconds // 60)
    if m < 1:
        return "不到1分钟"
    if m < 60:
        return f"{m}分钟"
    return f"{m // 60}小时{m % 60:02d}分"


def claim_travel(token, uid):
    """尝试领取旅行奖励。返回 (claimed, msg)。

    claimed=True  -> 领取成功(奖励已入账)
    claimed=False -> 旅行未到达/已领取/接口异常, msg 给出原因, 不算脚本失败
    """
    st, body = _post(TRAVEL_CLAIM_URL, token, uid)
    code = body.get("code")
    if st == 200 and code == 0:
        data = body.get("data") or {}
        credit = _num(data.get("credit")) or _num(data.get("reward"))
        return True, (f"领取成功, 奖励 {fmt_credit(credit)} 积分" if credit else "领取成功")
    msg = str(body.get("msg") or body.get("error_msg") or "").strip()
    if "not arrived" in msg.lower():
        return False, "旅行中, 尚未到达"
    if "claim" in msg.lower() and ("claimed" in msg.lower() or "already" in msg.lower()):
        return False, "奖励已领取过"
    return False, f"未领取: HTTP {st} code={code} {msg[:60]}"


# ---------- 派猫出发(主动, 2026-11 新增) ----------
#   GET  /activity/growth/buddy/travel/config   地点表(4 个: 咖啡馆/商场/健身房/古镇)
#   POST /activity/growth/buddy/travel/depart   {"location_id": 1} 出发, 每日 1 次
# 实测: 今日已用完 -> HTTP 403 msg="daily limit reached"; 空 body -> 400 invalid request
TRAVEL_CONFIG_URL = "https://www.codebuddy.cn/activity/growth/buddy/travel/config"
TRAVEL_DEPART_URL = "https://www.codebuddy.cn/activity/growth/buddy/travel/depart"


def get_travel_locations(token, uid):
    """目的地列表: [{id, code, name, duration_hours_min/max, reward_credit_min/max}]"""
    st, body = _get(TRAVEL_CONFIG_URL, token, uid)
    if body.get("code") != 0:
        return []
    data = body.get("data") or {}
    locs = data.get("locations")
    return locs if isinstance(locs, list) else []


def pick_travel_location(locs, pref=""):
    """按偏好挑目的地。pref 支持 id/code/名称, 留空或 random 则随机。"""
    if not locs:
        return None
    pref = (pref or "").strip().lower()
    if pref and pref not in ("random", "auto", "随机"):
        for loc in locs:
            keys = (str(loc.get("id")), str(loc.get("code") or "").lower(),
                    str(loc.get("name") or "").lower())
            if pref in keys:
                return loc
    return random.choice(locs)


def depart_travel(token, uid, location_id):
    """主动出发。返回 (ok, msg, data): ok=False 时 msg 说明原因(次数用完/已在旅行)。"""
    st, body = _post(TRAVEL_DEPART_URL, token, uid, {"location_id": int(location_id)})
    if st == 200 and body.get("code") == 0:
        return True, "已出发", (body.get("data") or {})
    msg = str(body.get("msg") or body.get("error_msg") or "").strip()
    low = msg.lower()
    if "daily limit" in low:
        return False, "今日次数已用完", {}
    if "traveling" in low or "already" in low:
        return False, "已在旅行中", {}
    if "invalid" in low:
        return False, "参数无效(地点不存在)", {}
    if "not available" in low:
        return False, "该地点暂不可用", {}
    return False, f"出发失败: HTTP {st} {msg[:40]}", {}


def task_option(name, key, default=None):
    """取任务的扩展配置, 如 tasks.cat_travel={"enabled":true,"location":"coffee"}。"""
    src = panel_config().get("tasks")
    if not isinstance(src, dict):
        src = _load_config().get("tasks") or {}
    val = src.get(name) if isinstance(src, dict) else None
    if isinstance(val, dict):
        return val.get(key, default)
    return default


def _eta_of(trip):
    """返回 (剩余秒, 到达 HH:MM) —— 用服务端 server_now 对齐, 避免本机时区/时钟偏差。"""
    arrive_at = trip.get("arrive_at")
    now = trip.get("server_now")
    if not arrive_at:
        return None, "?"
    remain = max(0, int(arrive_at) - int(now)) if now else None
    arrive_str = datetime.fromtimestamp(int(arrive_at)).strftime("%H:%M")
    return remain, arrive_str


def travel_flow(token, uid, cat_name="猫猫", location_pref=""):
    """一次完整派猫处理。返回 (推送行, flags)。

    顺序: 途中 -> 报 ETA; 否则先试领奖; 领不到且今日还有名额 -> 自动出发。
    flags: traveling/departed/claimed/limited/error, 供静默推送判断。
    """
    trip = get_travel_status(token, uid)
    if trip is None:
        return f"🐱 派猫旅行: {cat_name} 行程查询失败", {"error": True}

    state = str(trip.get("state") or "")
    loc = ((trip.get("location") or {}).get("name")) or ""
    reward = trip.get("reward_credit")

    if state == "traveling":
        remain, arrive_str = _eta_of(trip)
        return (f"🐱 派猫旅行: {cat_name} 在「{loc or '路上'}」旅行中, "
                f"预计 {fmt_eta(remain)}后({arrive_str})到达"
                + (f", 奖励 {fmt_credit(reward)} 积分" if reward else "")), {"traveling": True}

    # 不在途中: 可能已到达待领奖
    claimed, claim_msg = claim_travel(token, uid)
    if claimed:
        return (f"🎁 派猫旅行: {cat_name} 已到达「{loc or '目的地'}」, {claim_msg}"), {"claimed": True}

    if trip.get("daily_limit_reached"):
        detail = "奖励已领取" if "已领取" in claim_msg else ""
        return (f"🐱 派猫旅行: {cat_name} 今日已完成"
                + (f"({detail})" if detail else "")), {"limited": True}

    # 空闲且还有名额 -> 主动出发
    locs = get_travel_locations(token, uid)
    target = pick_travel_location(locs, location_pref)
    if not target:
        return f"🐱 派猫旅行: {cat_name} 空闲, 但地点列表获取失败", {"error": True}

    ok, dep_msg, _ = depart_travel(token, uid, target.get("id"))
    if not ok:
        if "次数已用完" in dep_msg or "已在旅行中" in dep_msg:
            return f"🐱 派猫旅行: {cat_name} {dep_msg}", {"limited": True}
        return f"🐱 派猫旅行: {cat_name} {dep_msg}", {"error": True}

    trip2 = get_travel_status(token, uid) or {}
    remain, arrive_str = _eta_of(trip2)
    reward2 = trip2.get("reward_credit")
    loc2 = ((trip2.get("location") or {}).get("name")) or target.get("name") or "目的地"
    eta = f", 预计 {fmt_eta(remain)}后({arrive_str})到达" if remain is not None else ""
    return (f"🐱 派猫旅行: {cat_name} 已出发「{loc2}」{eta}"
            + (f", 奖励 {fmt_credit(reward2)} 积分" if reward2 else "")), {"departed": True}


# ---------- 连登奖励(成长中心, 网页版 GrowthCenter 用同一套接口) ----------
#   GET  /activity/growth/streak   连登天数/档位状态(available/claimed/locked)
#   POST /activity/growth/redeem   {tier:"7d", client_token:<uuid>} 兑换档位
# 档位: 7d(能量2/补登卡1/抽奖1) / 14d(+50积分) / 28d(+150积分), 每档每月限兑1次
STREAK_URL = "https://www.codebuddy.cn/activity/growth/streak"
STREAK_REDEEM_URL = "https://www.codebuddy.cn/activity/growth/redeem"


def get_streak(token, uid):
    """连登信息: {streak:{days,...}, redemption_status:{tier_7d_status,...}, ...}"""
    st, body = _get(STREAK_URL, token, uid)
    if st == 200 and body.get("code") == 0 and isinstance(body.get("data"), dict):
        return body["data"]
    return None


def _streak_remaining(streak, red):
    """距下一档还差几天: 按档位表自己算(档位天数 - 当前连登天数)。

    不用服务端的 next_tier_remaining: 连登中断后它不会跟着重置,
    实测出现过 days=1 但服务端仍报 remaining=3(按中断前的 4 天算的)。
    """
    days = _num(streak.get("days")) or 0
    next_tier = str(streak.get("next_tier") or "")
    # 优先从档位表取该档的达标天数
    for t in (red or {}).get("tiers") or []:
        if str(t.get("tier") or "") == next_tier and _num(t.get("days")):
            return max(0, int(_num(t["days"])) - int(days))
    # 档位表没有时从名字解析(如 "7d" -> 7)
    if next_tier.endswith("d") and next_tier[:-1].isdigit():
        return max(0, int(next_tier[:-1]) - int(days))
    # 解析不了才退回服务端值
    remain = _num(streak.get("next_tier_remaining"))
    return int(remain) if remain else None


def claim_streak_rewards(token, uid):
    """领取可领(available)的连登档位。返回 (领取列表, 状态描述)。

    档位可裁剪: tasks.streak_reward={"enabled":true,"tiers":["14d","28d"]}
    只领列出的档位(未配置则三档全领), 让用户能把 7 天档留着自己手动点。
    """
    d = get_streak(token, uid)
    if not d:
        return [], "连登信息查询失败"
    red = d.get("redemption_status") or {}
    tiers = red.get("tiers") or []

    only = task_option("streak_reward", "tiers")
    only = {str(x) for x in only} if isinstance(only, (list, tuple)) else None

    got = []
    for t in tiers:
        name = str(t.get("tier") or "")
        if not name:
            continue
        if only is not None and name not in only:
            continue
        if red.get(f"tier_{name}_status") != "available":
            continue
        st, body = _post(STREAK_REDEEM_URL, token, uid,
                         {"tier": name, "client_token": str(uuid.uuid4())})
        label = f"{int(_num(t.get('days')) or 0)}天档" if t.get("days") else f"{name}档"
        if st == 200 and body.get("code") == 0:
            data = body.get("data") or {}
            parts = []
            for k, unit in (("credit", "积分"), ("energy", "能量"),
                            ("cards", "补登卡"), ("chances", "抽奖次数")):
                v = _num(data.get(k)) or _num(t.get(k))
                if v:
                    parts.append(f"{unit}+{fmt_credit(v) if k == 'credit' else int(v)}")
            if not parts:
                parts = [f"能量+{t.get('energy', 0)}", f"补登卡+{t.get('cards', 0)}"]
            got.append(label + "(" + " ".join(parts) + ")")
        else:
            msg = str(body.get("msg") or body.get("error_msg") or "").strip()
            got.append(f"{label}领取失败({msg[:30] or 'code=' + str(body.get('code'))})")

    if got:
        return got, "已领取 " + "、".join(got)
    # 无可领档位: 返回空消息, 调用方只记日志不推送
    # (服务端 next_tier_remaining 口径混乱, 连登天数/差几天不再进推送)
    return [], ""


def _load_config():
    """技能目录下的 config.json(与 SKILL.md 同级), 可选。
    支持字段: qw_webhook / access_token / account_note / user_id / tasks(任务开关)
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# ---------- 任务开关(全局一份, 对所有账号/机器生效) ----------

# 注册表: 名称 -> (显示名, 默认是否执行, 说明)
# 新增活动时: 在这里加一行 + 在 process_account 里用 task_enabled("<名称>") 包住执行块
TASK_REGISTRY = [
    ("checkin",       "每日签到",   True, "查状态并幂等签到, 拿每日积分"),
    ("streak_reward", "连登奖励",   True, "连登档位(7/14/28天)达标后自动兑换能量/补登卡/积分/抽奖次数"),
    ("cat_travel",    "派猫旅行",   True, "途中报 ETA, 到达自动领奖, 空闲且有当日名额时自动出发"),
]


def task_config():
    """生效的任务开关: 默认值 <- config.json 的 tasks <- 面板/环境变量(最高优先)。

    面板统一配置 WORKBUDDY_CONFIG 里的 tasks 覆盖 config.json; 也兼容旧的 WB_TASKS。
    未在配置里出现的任务一律按默认值(启用)处理。
    """
    cfg = {name: default for name, _label, default, _desc in TASK_REGISTRY}
    src = panel_config().get("tasks")
    if src is None:
        src = _load_config().get("tasks") or {}
    if isinstance(src, dict):
        for k, v in src.items():
            if isinstance(v, dict):
                v = v.get("enabled", True)
            if isinstance(v, bool):
                cfg[str(k)] = v
    return cfg


def task_enabled(name):
    """任务是否执行; 未注册/未配置的名称按启用处理(方便灰度加新任务)。"""
    return task_config().get(name, True)


def print_tasks():
    """列出所有任务及当前开关状态, 方便核对配置。"""
    on = task_config()
    pc = panel_config()
    if pc.get("tasks") is not None:
        src = pc.get("source") or "环境变量"
    elif _load_config().get("tasks"):
        src = f"config:{CONFIG_FILE.name}"
    else:
        src = "默认值"
    print(f"任务开关(来源: {src})")
    for name, label, default, desc in TASK_REGISTRY:
        state = "✅ 启用" if on.get(name, default) else "⏸ 禁用"
        extra = "" if on.get(name, default) == default else "  (已覆盖默认)"
        print(f"  {name:<12} {label:<8} {state}{extra}   {desc}")
    print("\n修改方式: config.json 里加 \"tasks\": {\"cat_travel\": false},")
    print(f"          或面板环境变量 {CONF_ENV_NAME} 的 tasks 字段, 如"
          ' {"tasks":{"cat_travel":false}}')


# ---------- 青龙面板推送(方案一: 单一 WB_ACCOUNTS JSON 环境变量) ----------

QL_ENV_NAME = "WB_ACCOUNTS"          # 旧: 多账号凭证单独一个变量(仅兼容读取)
CONF_ENV_NAME = "WORKBUDDY_CONFIG"   # 新: 统一配置(accounts + qw_webhook + tasks)


def _ql_conf():
    """青龙面板连接配置: 环境变量 > config.json。"""
    cfg = _load_config()
    url = (os.environ.get("QL_URL") or str(cfg.get("ql_url", ""))).strip().rstrip("/")
    cid = (os.environ.get("QL_CLIENT_ID") or str(cfg.get("ql_client_id", ""))).strip()
    csec = (os.environ.get("QL_CLIENT_SECRET") or str(cfg.get("ql_client_secret", ""))).strip()
    return url, cid, csec


def _physical_macs():
    """枚举物理网卡 MAC(12位大写十六进制, 无分隔符), 按可用性排序。"""
    out = []
    if os.name == "nt":
        try:
            proc = subprocess.run(["getmac", "/fo", "csv", "/nh"],
                                  capture_output=True, timeout=10)
            # 中文 Windows 下 getmac 输出是 GBK 编码
            try:
                text = proc.stdout.decode("utf-8")
            except UnicodeDecodeError:
                text = proc.stdout.decode("gbk", errors="replace")
            for line in text.splitlines():
                m = line.split(",")[0].strip().strip('"')
                m = m.replace("-", "").replace(":", "").upper()
                if len(m) == 12 and all(c in "0123456789ABCDEF" for c in m) \
                        and m != "0" * 12:
                    out.append(m)
        except Exception:
            pass
    else:
        try:
            for d in Path("/sys/class/net").iterdir():
                if d.name == "lo" or (d / "virtual").exists():
                    continue
                addr = (d / "address").read_text().strip().replace(":", "").upper()
                if len(addr) == 12 and addr != "0" * 12:
                    out.append(addr)
        except Exception:
            pass
    # 过滤随机化 Wi-Fi MAC(组播位=1)但保留在最后兜底, 真实网卡(本地管理/ universally administered)优先
    real = [m for m in out if not (int(m[:2], 16) & 0x01)]
    return real + [m for m in out if int(m[:2], 16) & 0x01]


def token_age_desc(token):
    """解析 JWT 的 iat/exp, 返回如 '签发 2026-11-12 16:03:45, 剩 41 天'。解析失败返回空串。

    用于企微推送里直观区分新旧 token: 多台机器推送时看签发时刻即知哪台是刚推的。
    """
    try:
        p = str(token).split(".")[1]
        p += "=" * (-len(p) % 4)
        claims = json.loads(base64.urlsafe_b64decode(p))
        iat, exp = claims.get("iat"), claims.get("exp")
        if not exp:
            return ""
        left_days = int(int(exp) - datetime.now().timestamp()) // 86400
        issued = (f"签发 {datetime.fromtimestamp(int(iat)).strftime('%Y-%m-%d %H:%M:%S')}, "
                  if iat else "")
        warn = " ⚠️即将过期" if left_days <= 7 else ""
        return f"{issued}剩 {max(0, left_days)} 天{warn}"
    except Exception:
        return ""


def machine_id():
    """机器标识: WB_MACHINE_ID 覆盖 > 物理网卡 MAC(12位大写十六进制) > 主机名。"""
    mid = os.environ.get("WB_MACHINE_ID", "").strip().upper()
    if mid:
        return re.sub(r"[^A-Z0-9-]", "", mid) or "UNKNOWN"
    macs = _physical_macs()
    if macs:
        return macs[0]
    mac = f"{uuid.getnode():012X}"
    if int(mac[:2], 16) & 0x01:  # 组播位=1 说明是随机化的假 MAC
        import socket
        host = re.sub(r"[^A-Z0-9]", "", socket.gethostname().upper())[:10]
        return f"HOST-{host}" if host else "UNKNOWN"
    return mac


def _ql_req(base, path, token=None, method="GET", payload=None):
    # 面板若套 Cloudflare, 非浏览器 UA 会被 1010 拦截
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"code": e.code, "msg": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return -1, {"code": -1, "msg": str(e)}


def _ql_login(base, cid, csec):
    """青龙 OpenAPI 鉴权: GET /open/auth/token, 凭证走 URL 参数(2.13+ 版本)。"""
    import urllib.parse
    qs = urllib.parse.urlencode({"client_id": cid, "client_secret": csec})
    st, body = _ql_req(base, f"/open/auth/token?{qs}")
    if body.get("code") != 200 or not (body.get("data") or {}).get("token"):
        raise RuntimeError(f"青龙登录失败: {json.dumps(body, ensure_ascii=False)[:160]}")
    return body["data"]["token"]


def _b64d(v):
    """青龙 API 返回的环境变量值是 base64 编码的, 兼容明文。"""
    try:
        return base64.b64decode(str(v)).decode("utf-8")
    except Exception:
        return str(v or "")


def _parse_accounts(raw):
    """解析 WB_ACCOUNTS JSON -> [{machine, device, account, token, uid, updated_at}]。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"WB_ACCOUNTS 不是合法 JSON: {e}")
    if not isinstance(data, list):
        raise ValueError("WB_ACCOUNTS 应为 JSON 数组")
    return [a for a in data if isinstance(a, dict) and str(a.get("token", "")).strip()]


def panel_config():
    """统一配置: WORKBUDDY_CONFIG(JSON) 优先, 兼容旧的 WB_ACCOUNTS / QW_WEBHOOK / WB_TASKS。

    结构: {"accounts": [...], "qw_webhook": "...", "tasks": {"checkin": true, ...}}
    返回 {"accounts": list, "qw_webhook": str, "tasks": dict|None, "source": str}
    """
    out = {"accounts": [], "qw_webhook": "", "tasks": None, "source": ""}
    raw = os.environ.get(CONF_ENV_NAME, "").strip()
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                out["accounts"] = [a for a in (d.get("accounts") or [])
                                   if isinstance(a, dict) and str(a.get("token", "")).strip()]
                out["qw_webhook"] = str(d.get("qw_webhook") or "").strip()
                out["tasks"] = d.get("tasks") if isinstance(d.get("tasks"), dict) else None
                out["source"] = f"env:{CONF_ENV_NAME}"
                return out
            log(f"⚠ {CONF_ENV_NAME} 应为 JSON 对象, 已回退旧变量")
        except json.JSONDecodeError as e:
            log(f"⚠ {CONF_ENV_NAME} 不是合法 JSON, 已回退旧变量: {e}")

    # 兼容: 旧的多变量方案
    acc_raw = os.environ.get(QL_ENV_NAME, "").strip()
    if acc_raw:
        try:
            out["accounts"] = _parse_accounts(acc_raw)
            out["source"] = f"env:{QL_ENV_NAME}(旧)"
        except ValueError as e:
            log(f"⚠ {e}")
    t = os.environ.get("WB_TASKS", "").strip()
    if t:
        try:
            parsed = json.loads(t)
            if isinstance(parsed, dict):
                out["tasks"] = parsed
        except json.JSONDecodeError:
            log("⚠ WB_TASKS 不是合法 JSON, 已忽略")
    return out


def push_server():
    """本机登录态 -> 青龙面板 WB_ACCOUNTS(按 machine 幂等合并)。"""
    base, cid, csec = _ql_conf()
    if not (base and cid and csec):
        log("✗ 青龙面板未配置。请在 config.json 或环境变量里设置:")
        log("  ql_url=面板地址  ql_client_id / ql_client_secret=应用凭证(权限勾'环境变量')")
        sys.exit(1)

    creds = load_credentials()
    if not creds:
        log("✗ 无法取得本机登录态(见 run 命令的提示)")
        sys.exit(1)
    token, uid, source, account, note, device, _ip = creds
    entry = {
        "machine": machine_id(),
        "device": device,
        "account": account,
        "token": token,
        "uid": uid,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        tk = _ql_login(base, cid, csec)
    except RuntimeError as e:
        log(f"✗ {e}")
        sys.exit(1)

    st, body = _ql_req(base, "/open/envs", token=tk)
    if body.get("code") != 200:
        log(f"✗ 读取环境变量失败: {json.dumps(body, ensure_ascii=False)[:160]}")
        sys.exit(1)
    envs = body.get("data") or []
    by_name = {e.get("name"): e for e in envs}

    # 1. 取现有统一配置; 没有则从旧变量(WB_ACCOUNTS/QW_WEBHOOK/WB_TASKS)迁移
    conf_env = by_name.get(CONF_ENV_NAME)
    cfg, accounts = {}, []
    if conf_env:
        try:
            parsed = json.loads(_b64d(conf_env.get("value")))
            cfg = parsed if isinstance(parsed, dict) else {}
            accounts = [a for a in (cfg.get("accounts") or []) if isinstance(a, dict)]
        except json.JSONDecodeError as err:
            log(f"⚠ 面板 {CONF_ENV_NAME} 格式异常, 将按旧变量重建: {err}")
            cfg = {}

    legacy_names = [QL_ENV_NAME, "QW_WEBHOOK", "WB_TASKS"]
    migrated = []
    if not conf_env:
        acc_env = by_name.get(QL_ENV_NAME)
        if acc_env:
            try:
                accounts = _parse_accounts(_b64d(acc_env.get("value")))
                migrated.append(QL_ENV_NAME)
            except ValueError as err:
                log(f"⚠ 旧变量 {QL_ENV_NAME} 格式异常, 忽略: {err}")
        hook_env = by_name.get("QW_WEBHOOK")
        if hook_env:
            cfg["qw_webhook"] = _b64d(hook_env.get("value")).strip()
            migrated.append("QW_WEBHOOK")
        tasks_env = by_name.get("WB_TASKS")
        if tasks_env:
            try:
                parsed_tasks = json.loads(_b64d(tasks_env.get("value")))
                if isinstance(parsed_tasks, dict):
                    cfg["tasks"] = parsed_tasks
                    migrated.append("WB_TASKS")
            except json.JSONDecodeError:
                log("⚠ 旧变量 WB_TASKS 不是合法 JSON, 忽略")

    # 2. webhook / tasks 兜底: 本机 config.json 的值做种子(不覆盖面板已有)
    if not cfg.get("qw_webhook"):
        cfg["qw_webhook"] = str(_load_config().get("qw_webhook", "")).strip()
    if not cfg.get("tasks"):
        cfg["tasks"] = _load_config().get("tasks") or {}

    # 3. 按 machine 幂等合并本机条目
    merged = [a for a in accounts if a.get("machine") != entry["machine"]] + [entry]
    cfg["accounts"] = merged
    cfg.setdefault("tasks", {})
    val = json.dumps(cfg, ensure_ascii=False)
    n_task = len([k for k, v in (cfg.get("tasks") or {}).items() if v is True]) \
        if isinstance(cfg.get("tasks"), dict) else 0
    remark = (f"WorkBuddy统一配置(自动维护: {len(merged)}台机器"
              + (f", {n_task}项任务开关" if n_task else "")
              + (", 含企微webhook" if cfg.get("qw_webhook") else "") + ")")

    if conf_env:
        # 实测 2.18.3: 创建用数组, 更新用单对象(值均为明文字符串)
        st, body = _ql_req(base, "/open/envs", token=tk, method="PUT",
                           payload={"id": conf_env.get("id"), "name": CONF_ENV_NAME,
                                    "value": val, "remarks": remark})
    else:
        st, body = _ql_req(base, "/open/envs", token=tk, method="POST",
                           payload=[{"name": CONF_ENV_NAME, "value": val, "remarks": remark}])
    if body.get("code") != 200:
        log(f"✗ 写入环境变量失败: {json.dumps(body, ensure_ascii=False)[:160]}")
        sys.exit(1)

    # 4. 启用新变量(青龙 2.18 新建默认未启用)
    st, body = _ql_req(base, f"/open/envs?searchValue={CONF_ENV_NAME}", token=tk)
    new_env = next((e for e in (body.get("data") or [])
                    if e.get("name") == CONF_ENV_NAME), None)
    if new_env:
        _ql_req(base, "/open/envs/enable", token=tk, method="PUT", payload=[new_env.get("id")])

    # 5. 清理被合并的旧变量(避免面板里重复/互相干扰)
    stale_ids = [by_name[n].get("id") for n in legacy_names if n in by_name]
    if stale_ids:
        st, body = _ql_req(base, "/open/envs", token=tk, method="DELETE", payload=stale_ids)
        if body.get("code") == 200:
            log(f"  已迁移并清理旧变量: {', '.join(n for n in legacy_names if n in by_name)}")

    log(f"✓ 已推送到青龙面板: 机器 {entry['machine']} ({device} | {account})")
    age = token_age_desc(token)
    log(f"  本次 token: {age or '有效期未知'}")
    log(f"  面板 {CONF_ENV_NAME} 现有 {len(merged)} 台机器"
        + (f", 已包含 webhook" if cfg.get("qw_webhook") else "")
        + (f", tasks: {json.dumps(cfg.get('tasks'), ensure_ascii=False)}"
           if cfg.get("tasks") else ""))
    return entry


# ---------- 企微推送 ----------

def resolve_webhook():
    """webhook key 优先级: 环境变量 QW_WEBHOOK > 面板 WORKBUDDY_CONFIG > config.json"""
    key = os.environ.get("QW_WEBHOOK", "").strip()
    if key:
        return key, "env:QW_WEBHOOK"
    pc = panel_config()
    if pc.get("qw_webhook"):
        return pc["qw_webhook"], pc.get("source") or "面板配置"
    key = str(_load_config().get("qw_webhook", "")).strip()
    if key:
        return key, f"config:{CONFIG_FILE.name}"
    return "", ""


def push_wecom(lines, account, device, ip, note=""):
    key, source = resolve_webhook()
    if not key:
        log("未配置企微 webhook, 跳过推送")
        log(f"  可设置环境变量 QW_WEBHOOK, 或在 {CONFIG_FILE} 里填 \"qw_webhook\"")
        return False
    head = ["【WorkBuddy 自动签到】", f"👤 账号: {account}"]
    if note and note != account:
        head.append(f"🏷️ 备注: {note}")
    head.append(f"💻 设备: {device}")
    ver = get_client_version()
    if ver:
        head.append(f"🧩 客户端: WorkBuddy {ver}")
    if ip:
        head.append(f"🌐 IP: {ip}")
    text = "\n".join(head + lines)
    payload = {"msgtype": "text", "text": {"content": text}}
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        ok = body.get("errcode") == 0
        log(f"企微推送({source}): {body.get('errmsg', body)}")
        return ok
    except Exception as e:
        log(f"企微推送失败: {e}")
        return False


# ---------- 主流程 ----------

def process_account(token, uid, account, note, device, ip):
    """单账号完整流程: 状态 -> 幂等签到 -> 积分 -> 本周 -> 连登 -> 派猫(自动出发/领奖)。

    返回 (推送行列表, flags); flags 用于 decided 是否推送(静默模式)。
    """
    flags = {"checkin_new": False, "streak_claimed": False,
             "travel_claimed": False, "travel_departed": False, "error": False}
    lines = [f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    age = token_age_desc(token)
    lines.append(f"🔑 token: {age or '有效期未知(非标准JWT)'}")

    # 1. 查状态(不单独成行, 结果并入下面的签到行)
    ok, data = checkin_status(token, uid)
    if not ok:
        lines.append(f"⚠️ 签到状态查询失败: {json.dumps(data, ensure_ascii=False)[:80]}")
        log(lines[-1])

    # 2. 幂等签到: 一行说清结果(已签到/新签/失败), 不再与状态行重复
    if not task_enabled("checkin"):
        log("⏭ 跳过签到(tasks.checkin=false)")
    elif ok and data.get("today_checked_in"):
        lines.append("✅ 签到: 今日已签到")
        log(lines[-1])
    else:
        try:
            ok2, detail, _ = do_checkin(token, uid)
            lines.append(("✅ " if ok2 else "❌ ") + f"签到: {detail}")
            flags["checkin_new"] = bool(ok2)
            if not ok2:
                flags["error"] = True
            log(lines[-1])
        except Exception as e:
            lines.append(f"❌ 签到异常: {e}")
            flags["error"] = True
            log(lines[-1])

    # 3. 剩余积分(签到后查, 数值已含本次奖励)
    info, raw = get_balance(token, uid)
    lines.append(balance_line(info))
    detail_line = balance_detail(info)
    if detail_line:
        lines.append(f"   └ {detail_line}")
    log(lines[-2])
    if not info:
        log(f"   积分接口响应: {json.dumps(raw, ensure_ascii=False)[:120]}")
    else:
        used_line = consume_line(uid, used_credits(info))
        if used_line:
            lines.append(used_line)
            log(used_line)

    # 4. 汇总本周进度
    if ok and data.get("week_progress"):
        wp = data["week_progress"]
        lines.append(f"📅 本周: {sum(1 for x in wp if x)}/{len(wp)} 天 "
                     f"(每日 {data.get('daily_credit', '?')} 积分)")
        log(lines[-1])

    # 5. 连登奖励: 达标档位自动兑换(每档每月限兑1次)
    if not task_enabled("streak_reward"):
        log("⏭ 跳过连登奖励(tasks.streak_reward=false)")
    else:
        try:
            got, streak_msg = claim_streak_rewards(token, uid)
            flags["streak_claimed"] = bool(got)
            if streak_msg:
                lines.append(("🎁 " if got else "❌ ") + f"连登: {streak_msg}")
                log(lines[-1])
            else:
                log("连登: 无可领档位(不推送)")
        except Exception as e:
            lines.append(f"⚠️ 连登奖励异常: {e}")
            flags["error"] = True
            log(lines[-1])

    # 6. 派猫旅行: 途中报 ETA -> 到达领奖 -> 空闲且有名额则自动出发
    if not task_enabled("cat_travel"):
        log("⏭ 跳过派猫旅行(tasks.cat_travel=false)")
    else:
        try:
            buddy = get_buddy_info(token, uid)
            cat_name = buddy["name"] if buddy else "猫猫"
            line, tflags = travel_flow(token, uid, cat_name,
                                      str(task_option("cat_travel", "location", "")))
            lines.append(line)
            flags["travel_claimed"] = bool(tflags.get("claimed"))
            flags["travel_departed"] = bool(tflags.get("departed"))
            if tflags.get("error"):
                flags["error"] = True
            log(line)
        except Exception as e:
            lines.append(f"⚠️ 派猫旅行异常: {e}")
            flags["error"] = True
            log(lines[-1])

    return lines, flags


def should_push(flags, quiet=False):
    """是否推送: 正常模式总是推; 静默模式只在有实际收获(领到奖励)或出错时推。"""
    if not quiet:
        return True
    return bool(flags.get("travel_claimed") or flags.get("streak_claimed")
                or flags.get("error"))


def run_all(quiet=False):
    pc = panel_config()
    accounts = pc["accounts"]

    # 多账号模式(面板统一配置 WORKBUDDY_CONFIG 里的 accounts, 每台机器一组凭证)
    if accounts:
        ip = get_device_ip()
        log(f"多账号模式({pc.get('source') or '环境变量'}): 共 {len(accounts)} 台机器")
        for a in accounts:
            machine = str(a.get("machine", "?"))
            device = str(a.get("device") or f"机器{machine}")
            account = str(a.get("account") or device)
            note = str(a.get("note") or "")
            log(f"—— {device} ({machine}) | {account} ——")
            try:
                lines, flags = process_account(a["token"], str(a.get("uid") or ""),
                                               account, note, device, ip)
            except Exception as e:
                lines = [f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                         f"❌ 执行异常: {str(e)[:120]}"]
                flags = {"error": True}
                log(lines[-1])
            if should_push(flags, quiet):
                push_wecom(lines, account, device, ip, note)
            else:
                log("⏭ 静默模式: 无新增奖励, 跳过推送")
        return

    # 单账号模式(本机客户端自动解密)
    creds = load_credentials()
    if not creds:
        log("✗ 无法取得登录态。请确认 WorkBuddy 客户端正在运行且已登录,")
        log("  或设置环境变量 WORKBUDDY_ACCESS_TOKEN (明文 JWT) 后重试。")
        sys.exit(1)
    token, uid, source, account, note, device, ip = creds
    log(f"凭证来源: {source} | 账号: {account} | 设备: {device} ({ip}) | uid: {uid[:8]}...")

    lines, flags = process_account(token, uid, account, note, device, ip)
    if should_push(flags, quiet):
        push_wecom(lines, account, device, ip, note)
    else:
        log("⏭ 静默模式: 无新增奖励, 跳过推送")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"
    trim_old_logs()

    if arg == "run":
        quiet = len(sys.argv) > 2 and sys.argv[2] in ("quiet", "--quiet", "silent")
        run_all(quiet=quiet)
        return

    if arg == "push_server":
        push_server()
        return

    if arg in ("tasks", "task_list"):
        print_tasks()
        return

    creds = load_credentials()
    if not creds:
        log("✗ 无法取得登录态(见 run 命令的提示)")
        sys.exit(1)
    token, uid, source, account, note, device, ip = creds

    if arg == "status":
        print(f"账号: {account} | 设备: {device} ({ip})")
        ok, data = checkin_status(token, uid)
        if ok:
            print(f"今日已签到: {data.get('today_checked_in')} | 连签: {data.get('streak_days')} 天 "
                  f"| 每日积分: {data.get('daily_credit')}")
            print(f"本周进度: {data.get('week_progress')}")
        else:
            print(f"查询失败: {json.dumps(data, ensure_ascii=False)[:200]}")
            sys.exit(1)
    elif arg == "checkin":
        ok, detail, _ = do_checkin(token, uid)
        print(f"[{account} @ {device}] {detail}")
        sys.exit(0 if ok else 1)
    elif arg == "point":
        print(f"账号: {account} | 设备: {device} ({ip})")
        info, raw = get_balance(token, uid)
        if info:
            print(f"剩余积分: {fmt_credit(info['total'])} ({info['plan']})")
            for name, rem, tot in info["groups"]:
                print(f"  {name}: {fmt_credit(rem)} / {fmt_credit(tot)}")
        else:
            print(f"余额查询失败: {json.dumps(raw, ensure_ascii=False)[:200]}")
        ok, data = checkin_status(token, uid)
        if ok:
            print(f"今日积分: {data.get('today_credit', '?')} | 每日: {data.get('daily_credit', '?')} "
                  f"| 连签: {data.get('streak_days', '?')} 天")
        sys.exit(0 if info else 1)
    elif arg in ("streak", "streak_claim"):
        print(f"账号: {account} | 设备: {device} ({ip})")
        d = get_streak(token, uid)
        if not d:
            print("连登信息查询失败(接口可能变更)")
            sys.exit(1)
        s = d.get("streak") or {}
        red = d.get("redemption_status") or {}
        print(f"连登 {s.get('days')} 天 | 本月累计 {s.get('month_total_days')} 天"
              f" | 下一档 {s.get('next_tier')} 还差 {_streak_remaining(s, red)} 天")
        for t in red.get("tiers") or []:
            n = t.get("tier")
            print(f"  {n:<4} 状态 {red.get('tier_' + str(n) + '_status')} | "
                  f"积分 {t.get('credit')} 能量 {t.get('energy')} "
                  f"补登卡 {t.get('cards')} 抽奖 {t.get('chances')}")
        if arg == "streak_claim":
            got, msg = claim_streak_rewards(token, uid)
            print(msg)
            sys.exit(0 if got or "还差" in msg or "暂无" in msg else 1)
    elif arg in ("cat_status", "cat_claim", "cat_start", "cat_reward", "cat_depart"):
        # cat_start/cat_reward 为旧命令名, 保留兼容; cat_depart = 空闲且有额度就出发
        print(f"账号: {account} | 设备: {device} ({ip})")
        if arg in ("cat_status", "cat_start"):
            buddy = get_buddy_info(token, uid)
            if buddy:
                extra = f" | {buddy['personality']}" if buddy["personality"] else ""
                print(f"猫猫: {buddy['name'] or '?'} ({buddy['rarity'] or '?'}{extra})")
            else:
                print("猫猫档案查询失败(接口可能变更, 请检查客户端新版本)")
            trip = get_travel_status(token, uid)
            if trip:
                state = trip.get("state", "?")
                loc = ((trip.get("location") or {}).get("name")) or "?"
                if state == "traveling":
                    remain = trip.get("arrive_at") and trip.get("server_now") \
                        and max(0, int(trip["arrive_at"]) - int(trip["server_now"]))
                    print(f"旅行: 在「{loc}」途中, 剩余 {fmt_eta(remain)}"
                          f" | 奖励: {trip.get('reward_credit', '?')} 积分"
                          + (" | 今日次数已达上限" if trip.get("daily_limit_reached") else ""))
                else:
                    print(f"旅行状态: {state}"
                          + (" | 今日次数已达上限" if trip.get("daily_limit_reached") else " | 还有名额"))
            else:
                print("行程状态查询失败")
            # 空闲且有名额时列一下可选目的地, 方便配置 tasks.cat_travel.location
            if trip and trip.get("state") != "traveling" and not trip.get("daily_limit_reached"):
                locs = get_travel_locations(token, uid)
                if locs:
                    print("可选目的地: " + " / ".join(
                        f"{l.get('name')}({l.get('code')} id={l.get('id')})" for l in locs))
            if arg == "cat_status" and buddy and buddy.get("poll_seconds"):
                print(f"轮询间隔: {buddy['poll_seconds']} 秒")
        elif arg == "cat_depart":
            buddy = get_buddy_info(token, uid)
            cat_name = buddy["name"] if buddy else "猫猫"
            pref = str(task_option("cat_travel", "location", ""))
            line, flags = travel_flow(token, uid, cat_name, pref)
            print(line)
            sys.exit(0 if not flags.get("error") else 1)
        else:
            trip = get_travel_status(token, uid)
            if trip and trip.get("state") == "traveling":
                remain = trip.get("arrive_at") and trip.get("server_now") \
                    and max(0, int(trip["arrive_at"]) - int(trip["server_now"]))
                print(f"派猫旅行: 旅行中, 剩余 {fmt_eta(remain)}, 到达后再执行 cat_claim")
                sys.exit(0)
            claimed, msg = claim_travel(token, uid)
            print(f"派猫旅行: {msg}")
            sys.exit(0 if claimed or "尚未到达" in msg or "已领取" in msg else 1)
    else:
        print(f"未知命令: {arg}")
        print("用法: python workbuddy_checkin.py "
              "[run [quiet]|status|checkin|point|streak|streak_claim"
              "|cat_status|cat_depart|cat_claim|push_server|tasks]")
        sys.exit(2)


if __name__ == "__main__":
    main()
