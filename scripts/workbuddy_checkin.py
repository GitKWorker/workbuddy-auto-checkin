# -*- coding: utf-8 -*-
# workbuddy-auto-checkin: WorkBuddy(Buddy加油站)每日签到 + 企微推送
#
# 命令: run(默认) / status / checkin / point / cat_status / cat_start / cat_reward
#   run        一键: 查状态 -> 未签则签到 -> 汇总 -> 推送企微
#   status     仅查询签到状态(连签天数/本周进度)
#   checkin    未签到则签到, 已签到幂等跳过
#   point      查询积分余额(接口已下线时会明确提示)
#   cat_*      派猫旅行相关(当前网关已下线, 会明确提示)
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

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_BASE = "https://www.codebuddy.cn/v2/billing/meter"
API_ORIGIN = "https://www.codebuddy.cn"
TIMEOUT = 30

# 积分汇总接口: 与签到不同, 它在 /v2 之外 (客户端 resourcePrefix 为空)
BALANCE_PATH = "/billing/meter/get-user-resource-summary"

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

def run_all():
    creds = load_credentials()
    if not creds:
        log("✗ 无法取得登录态。请确认 WorkBuddy 客户端正在运行且已登录,")
        log("  或设置环境变量 WORKBUDDY_ACCESS_TOKEN (明文 JWT) 后重试。")
        sys.exit(1)
    token, uid, source, account, note, device, ip = creds
    log(f"凭证来源: {source} | 账号: {account} | 设备: {device} ({ip}) | uid: {uid[:8]}...")

    lines = [f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]

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

    # 2. 幂等签到
    if ok and data.get("today_checked_in"):
        lines.append("✅ 签到: 今日已签到(跳过)")
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

    push_wecom(lines, account, device, ip, note)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"

    if arg == "run":
        run_all()
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
    elif arg in ("cat_status", "cat_start", "cat_reward"):
        print("派猫旅行接口(/v2/billing/meter/cat-travel-*)当前网关已下线(404), 暂不可用。")
        print("如客户端更新后恢复, 请检查 SKILL.md 中的接口清单。")
    else:
        print(f"未知命令: {arg}")
        print("用法: python workbuddy_checkin.py [run|status|checkin|point|cat_status|cat_start|cat_reward]")
        sys.exit(2)


if __name__ == "__main__":
    main()
