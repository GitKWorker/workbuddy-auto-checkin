# -*- coding: utf-8 -*-
# workbuddy-auto-checkin: WorkBuddy(Buddy加油站)每日签到 + 企微推送
#
# 命令: run(默认) / status / checkin / point / cat_status / cat_claim / push_server
#   run         一键: 查状态 -> 未签则签到 -> 查积分 -> 尝试领取旅行奖励 -> 汇总 -> 推送企微
#               若存在 WB_ACCOUNTS 环境变量(JSON数组)则多账号模式: 逐台执行并分别推送
#   status      仅查询签到状态(连签天数/本周进度)
#   checkin     未签到则签到, 已签到幂等跳过
#   point       查询积分余额(接口已下线时会明确提示)
#   cat_status  查询猫猫(昵称/稀有度)与旅行行程(目的地/ETA/奖励)
#   cat_claim   尝试领取派猫旅行奖励(未到达时明确提示, 不算失败)
#   push_server 把本机最新登录态推送到青龙面板(单一 WB_ACCOUNTS JSON 环境变量, 按 MAC 幂等合并)
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
#   WB_DEVICE_IP       设备 IP(默认自动取内网 IP + 公网出口 IP)
#   WORKBUDDY_USER_ID  手动指定 uid(一般自动从 auth 文件读取)
#
# 推送内容包含: 账号名(客户端昵称 + 掩码手机号) / 设备名 / 设备 IP /
#               签到状态与本周进度 / 剩余积分(含额度分项)

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime
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


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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


def get_local_ip():
    """本机内网 IP: UDP connect 只取路由选中的出口地址, 不实际发包。"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            import socket
            ip = socket.gethostbyname(socket.gethostname())
            return "" if ip.startswith("127.") else ip
        except Exception:
            return ""


def get_public_ip(timeout=3):
    """公网出口 IP, 失败返回空串(不影响推送)。"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ip = resp.read().decode("utf-8", errors="replace").strip()
            if ip and len(ip) <= 45 and " " not in ip:
                return ip
        except Exception:
            continue
    return ""


def get_device_ip():
    """设备 IP: WB_DEVICE_IP / config.device_ip 覆盖, 否则 '内网 / 公网'。"""
    fixed = (os.environ.get("WB_DEVICE_IP", "").strip()
             or str(_load_config().get("device_ip", "")).strip())
    if fixed:
        return fixed
    local = get_local_ip()
    show_public = _load_config().get("public_ip", True)
    public = get_public_ip() if show_public else ""
    if local and public and public != local:
        return f"{local} / 公网 {public}"
    return local or public or "未知IP"


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


def _load_config():
    """技能目录下的 config.json(与 SKILL.md 同级), 可选。
    支持字段: qw_webhook / access_token / account_note / user_id
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# ---------- 青龙面板推送(方案一: 单一 WB_ACCOUNTS JSON 环境变量) ----------

QL_ENV_NAME = "WB_ACCOUNTS"


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

    st, body = _ql_req(base, f"/open/envs?searchValue={QL_ENV_NAME}", token=tk)
    if body.get("code") != 200:
        log(f"✗ 读取环境变量失败: {json.dumps(body, ensure_ascii=False)[:160]}")
        sys.exit(1)
    env_obj, accounts = None, []
    for e in body.get("data") or []:
        if e.get("name") == QL_ENV_NAME:
            env_obj = e
            try:
                accounts = _parse_accounts(_b64d(e.get("value")))
            except ValueError as err:
                log(f"⚠ 面板已有 {QL_ENV_NAME} 但格式异常, 将覆盖重建: {err}")
                accounts = []
            break

    merged = [a for a in accounts if a.get("machine") != entry["machine"]] + [entry]
    val = json.dumps(merged, ensure_ascii=False)
    remark = f"WorkBuddy签到多账号(自动维护, 共{len(merged)}台)"
    if env_obj:
        # 实测 2.18.3: 创建用数组, 更新用单对象(值均为明文字符串)
        st, body = _ql_req(base, "/open/envs", token=tk, method="PUT",
                           payload={"id": env_obj.get("id"), "name": QL_ENV_NAME,
                                    "value": val, "remarks": remark})
    else:
        st, body = _ql_req(base, "/open/envs", token=tk, method="POST",
                           payload=[{"name": QL_ENV_NAME, "value": val, "remarks": remark}])
    if body.get("code") != 200:
        log(f"✗ 写入环境变量失败: {json.dumps(body, ensure_ascii=False)[:160]}")
        sys.exit(1)

    if env_obj:
        _ql_req(base, "/open/envs/enable", token=tk, method="PUT", payload=[env_obj.get("id")])

    log(f"✓ 已推送到青龙面板: 机器 {entry['machine']} ({device} | {account})")
    age = token_age_desc(token)
    log(f"  本次 token: {age or '有效期未知'} (签发日期即新旧标识, 每日顺带推送可保鲜)")
    log(f"  面板 {QL_ENV_NAME} 现有 {len(merged)} 台机器的凭证")
    return entry


# ---------- 企微推送 ----------

def resolve_webhook():
    """webhook key 优先级: 环境变量 QW_WEBHOOK > config.json 的 qw_webhook"""
    key = os.environ.get("QW_WEBHOOK", "").strip()
    if key:
        return key, "env:QW_WEBHOOK"
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
    """单账号完整流程: 状态 -> 幂等签到 -> 积分 -> 本周 -> 旅行。返回推送行列表。"""
    lines = [f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    age = token_age_desc(token)
    lines.append(f"🔑 token: {age or '有效期未知(非标准JWT)'}")

    # 1. 查状态
    ok, data = checkin_status(token, uid)
    if ok:
        already = data.get("today_checked_in", False)
        streak = data.get("streak_days")
        lines.append(f"📋 状态: {'今日已签到' if already else '今日未签到'}"
                     + (f", 连签 {streak} 天" if streak is not None else ""))
        log(lines[-1])
    else:
        lines.append(f"📋 状态查询失败: {json.dumps(data, ensure_ascii=False)[:80]}")
        log(lines[-1])

    # 2. 幂等签到(已签到时推送里不单列, 状态行已说明; 实际签到或失败才单列)
    if ok and data.get("today_checked_in"):
        log("签到: 今日已签到(跳过)")
    else:
        ok2, detail, _ = do_checkin(token, uid)
        lines.append(("✅ " if ok2 else "❌ ") + f"签到: {detail}")
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

    # 4. 汇总本周进度
    if ok and data.get("week_progress"):
        wp = data["week_progress"]
        lines.append(f"📅 本周: {sum(1 for x in wp if x)}/{len(wp)} 天 "
                     f"(每日 {data.get('daily_credit', '?')} 积分)")
        log(lines[-1])

    # 5. 派猫旅行: 行程中报 ETA, 已到达则领奖, 均不影响推送
    buddy = get_buddy_info(token, uid)
    cat_name = buddy["name"] if buddy else "猫猫"
    trip = get_travel_status(token, uid)
    if trip and trip.get("state") == "traveling":
        remain = trip.get("arrive_at") and trip.get("server_now") \
            and max(0, int(trip["arrive_at"]) - int(trip["server_now"]))
        loc = ((trip.get("location") or {}).get("name")) or "未知目的地"
        reward = trip.get("reward_credit")
        eta = fmt_eta(remain)
        arrive_at = trip.get("arrive_at")
        arrive_str = datetime.fromtimestamp(int(arrive_at)).strftime("%H:%M") if arrive_at else "?"
        lines.append(f"🐱 派猫旅行: {cat_name} 在「{loc}」旅行中, "
                     f"预计 {eta} 后({arrive_str})到达"
                     + (f", 奖励 {fmt_credit(reward)} 积分" if reward else ""))
        log(lines[-1])
    else:
        claimed, travel_msg = claim_travel(token, uid)
        lines.append(f"{'🎁' if claimed else '🐱'} 派猫旅行: {cat_name} {travel_msg}")
        log(lines[-1])

    return lines


def run_all():
    raw = os.environ.get(QL_ENV_NAME, "").strip()

    # 多账号模式(青龙面板: WB_ACCOUNTS JSON 数组, 每台机器一组凭证)
    if raw:
        try:
            accounts = _parse_accounts(raw)
        except ValueError as e:
            log(f"✗ {e}")
            sys.exit(1)
        if not accounts:
            log("✗ WB_ACCOUNTS 里没有可用的凭证(token 字段为空)")
            sys.exit(1)
        ip = get_device_ip()
        log(f"多账号模式: 共 {len(accounts)} 台机器")
        for a in accounts:
            machine = str(a.get("machine", "?"))
            device = str(a.get("device") or f"机器{machine}")
            account = str(a.get("account") or device)
            note = str(a.get("note") or "")
            log(f"—— {device} ({machine}) | {account} ——")
            try:
                lines = process_account(a["token"], str(a.get("uid") or ""),
                                        account, note, device, ip)
            except Exception as e:
                lines = [f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                         f"❌ 执行异常: {str(e)[:120]}"]
                log(lines[-1])
            push_wecom(lines, account, device, ip, note)
        return

    # 单账号模式(本机客户端自动解密)
    creds = load_credentials()
    if not creds:
        log("✗ 无法取得登录态。请确认 WorkBuddy 客户端正在运行且已登录,")
        log("  或设置环境变量 WORKBUDDY_ACCESS_TOKEN (明文 JWT) 后重试。")
        sys.exit(1)
    token, uid, source, account, note, device, ip = creds
    log(f"凭证来源: {source} | 账号: {account} | 设备: {device} ({ip}) | uid: {uid[:8]}...")

    lines = process_account(token, uid, account, note, device, ip)
    push_wecom(lines, account, device, ip, note)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"

    if arg == "run":
        run_all()
        return

    if arg == "push_server":
        push_server()
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
    elif arg in ("cat_status", "cat_claim", "cat_start", "cat_reward"):
        # cat_start/cat_reward 为旧命令名, 保留兼容: start 无接口(旅行自动进行),
        # reward 等同 claim
        print(f"账号: {account} | 设备: {device} ({ip})")
        if arg == "cat_status" or arg == "cat_start":
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
                    print(f"旅行状态: {state} @「{loc}」")
            else:
                print("行程状态查询失败")
            if arg == "cat_status" and buddy and buddy.get("poll_seconds"):
                print(f"轮询间隔: {buddy['poll_seconds']} 秒")
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
        print("用法: python workbuddy_checkin.py [run|status|checkin|point|cat_status|cat_claim|push_server]")
        sys.exit(2)


if __name__ == "__main__":
    main()
