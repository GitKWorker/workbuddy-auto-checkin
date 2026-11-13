# workbuddy-auto-checkin

WorkBuddy（Buddy 加油站）每日自动签到 + 积分查询 + 企业微信推送的技能包。

在 WorkBuddy 对话里说一句「签到」就能跑：查状态 → 幂等签到 → 查剩余积分 → 推送企微。
全程自动使用本机客户端的登录态，**不需要手工填 Cookie / Token**。

---

## 1. 能力

| 能力 | 说明 |
|---|---|
| 每日签到 | 未签到则签到，已签到按幂等处理（不报错、不重复领） |
| 状态查询 | 今日是否已签、连签天数、本周进度、每日/今日积分 |
| 剩余积分 | 账户积分总额 + 套餐名 + 各额度分组剩余/总额 |
| 连登奖励 | 连登 7/14/28 天档位达标后自动兑换能量/补登卡/积分/抽奖次数 |
| 派猫旅行 | 有空余名额自动出发（地点可选/随机），到达自动领奖，途中报 ETA |
| 企微推送 | 一键执行后推送明细，含账号名、设备名、设备 IP |
| 多账号/多机区分 | 每台机器推送自带主机名与 IP，一个群里也分得清 |

派猫旅行已接入新版接口（2026-11）：`run` 一次做完「到达领奖 / 空闲则主动出发 / 途中报 ETA」；`cat_status` 查档案与可选目的地，`cat_depart` 手动出发，`cat_claim` 手动领奖。旧 `cat-travel-*` 接口已永久 404，不要再用。

---

## 0. 实测环境（配套版本）

| 项目 | 实测版本 | 说明 |
|---|---|---|
| WorkBuddy 客户端 | **5.9.3** | 接口路径、登录态加密方案均以该版本为准；客户端升级后若报 404/解密失败，优先检查新版 `app.asar` 里是否改了路由 |
| 操作系统 | Windows 11 | macOS 有路径兼容代码但未验证 |
| Python | 3.13 | 仅用标准库，无第三方依赖，3.8+ 应均可运行 |

> 接口与客户端版本强相关：签到/积分走 `/v2/billing/meter/*`，派猫旅行走 `/activity/growth/buddy/travel/*`（从 5.9.3 客户端提取，2026-11 接口大改）。官方升级客户端后接口可能变动，届时对照 `resources/app.asar` 里的实际路由同步修改脚本常量（`API_BASE` / `BALANCE_PATH` / `TRAVEL_*_URL`）。

---

## 2. 快速使用

### 2.1 在 WorkBuddy 对话里用（推荐）

技能装好并重启客户端后，直接说：

- 「签到」「一键执行」→ 完整流程 + 企微推送
- 「查询积分」「我还剩多少积分」→ 只查余额
- 「检查签到状态」→ 只查状态，不签到、不推送

### 2.2 命令行直接用

先进入脚本目录（关键，避免相对路径找不到）：

```bash
cd "%USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\scripts"
python workbuddy_checkin.py run          # 一键：签到 + 积分 + 连登 + 派猫 + 推送
python workbuddy_checkin.py run quiet    # 静默跑，只在领到奖励/出错时推送
python workbuddy_checkin.py status       # 只看签到状态
python workbuddy_checkin.py checkin      # 只签到（幂等）
python workbuddy_checkin.py point        # 只看剩余积分
python workbuddy_checkin.py streak       # 只看连登档位（streak_claim 领取）
python workbuddy_checkin.py cat_status   # 猫猫档案 + 行程 + 可选目的地
python workbuddy_checkin.py cat_depart   # 有空名额就派猫出发
python workbuddy_checkin.py cat_claim    # 到达则领奖
```

只依赖 Python 标准库，**无需 pip 安装任何包**（Python 3.8+）。

---

## 3. 实现原理

### 3.1 凭证：为什么不用手工填 Cookie

本机登录态存在：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

里面 `auth.accessToken` **不是明文 JWT，而是 AES-256-GCM 加密信封**（`$wbEncrypted` 标记，客户端 5.6.2 起）。直接拿去请求会一直 401，这是最容易踩的坑。

解密密钥 `atRestSecretKey` 只存在于**运行中的客户端内存**里，磁盘上没有。所以脚本这样做：

1. 用 `ELECTRON_RUN_AS_NODE=1` 把 `WorkBuddy.exe` 当 Node 运行时启动（不弹窗口、不走 GUI）；
2. 在子进程里调客户端自己的原生绑定 `process._linkedBinding("electron_browser_workbuddy_storage").loggerGet()`，取到 `atRestSecretKey`；
3. 按客户端规则派生密钥：`key = SHA256(atRestSecretKey)`、`keyId = SHA256(key).hex[:16]`；
4. 构造 AAD（`WB-AAD\0` + framing tag 等字节串）后 AES-256-GCM 解密，得到明文 JWT；
5. 顺手解密 `account.nickname` / `account.phoneNumber`，用于推送里显示「兰心 (134****2267)」这样的账号名。

明文 token 只在子进程内存里存在，**不写任何临时文件**，进程退出即消失。

### 3.2 wb_auth_helper.js 是什么

上面第 1~5 步的解密逻辑就在 `scripts/wb_auth_helper.js` 里——它是**本机凭证解析的辅助脚本**，由 `workbuddy_checkin.py` 自动调用（无需手动运行），负责「借客户端运行时取密钥并解密登录态」这一步。

需要它存在的原因：解密密钥只存在于客户端内存，纯 Python 拿不到，必须借 `WorkBuddy.exe` 自带的 Node 运行时执行客户端原生绑定，所以需要一个 JS 桥接文件。

安全性说明：

- 文件里**只有解密逻辑，不含任何凭证**（你的 token / webhook 都在 gitignored 的 config.json 或环境变量里）
- 解密结果只输出到子进程 stdout，不落盘
- 它**只在装了 WorkBuddy 客户端的本机上会被调用**；青龙面板 / 纯 token 服务器模式下脚本走 `WORKBUDDY_CONFIG` 里的明文 token，这个文件完全不会被使用（订阅拉下来看到它属正常，放着不管即可）

请求头：

```
Authorization: Bearer <accessToken>
X-User-Id: <uid>
User-Agent: WorkBuddy-Checkin-Bot/1.0     # 积分接口缺 UA 会被网关 403
```

### 3.3 接口

签到（`POST https://www.codebuddy.cn/v2/billing/meter/…`）：

| 用途 | 路径 | 关键返回 |
|---|---|---|
| 查状态 | `checkin-activity-status` | `today_checked_in` `streak_days` `week_progress` `daily_credit` |
| 签到 | `daily-checkin` | `code=0` 成功；`code=10001` 今日已签 |

剩余积分（注意**不带 `/v2`**）：

```
POST https://www.codebuddy.cn/billing/meter/get-user-resource-summary
→ { TotalCredits, SubscriptionPackageName, IsPaidUser,
    CreditGroups: [{ Name, CycleRemainCapacity, CycleTotalCapacity }] }
```

前缀差异不是笔误：客户端里签到用 `billingPrefix`（`/v2`），积分用 `resourcePrefix`（空字符串），两条路由挂在不同的网关路径上。

### 3.4 派猫旅行（主动出发 + 领奖）

旧 `cat-travel-*` 接口已永久 404，2026-11 客户端改成了这一套：

| 用途 | 接口 | 说明 |
|---|---|---|
| 猫猫档案 | `GET /v2/activity/growth/buddy/info` | 昵称/稀有度/性格 + `poll_interval_seconds` |
| 行程状态 | `GET /activity/growth/buddy/travel/status` | `state`(idle/traveling)、`location.name`、`arrive_at`/`server_now`、`reward_credit`、`daily_limit_reached` |
| 目的地表 | `GET /activity/growth/buddy/travel/config` | 4 个地点：咖啡馆/商场店铺/健身房/古镇客栈，各 1~4 小时、5~10 积分 |
| 主动出发 | `POST /activity/growth/buddy/travel/depart` | body `{"location_id": 1}`，**每日 1 次** |
| 领取奖励 | `POST /activity/growth/buddy/travel/claim` | 未到达返回 403 `not arrived yet` |

`depart` 的报错很清楚：今日已用完 → 403 `daily limit reached`；地点不存在 → 400 `location not available`；空 body → 400 `invalid request`。

所以 `run` 里的派猫顺序是：**途中**报 ETA → 否则先试领奖 → 领不到且 `daily_limit_reached=false` 就自动出发。目的地默认随机，也可以固定（见 6.1）：

```json
{ "tasks": { "cat_travel": { "enabled": true, "location": "coffee" } } }
```

行程 1~4 小时随机、服务端自动推进，所以**只跑一次 9:05 时猫还在路上**，要过几个小时才到达。建议的调度方式见第 7 节（脚本不自己判断时间，跑不跑由外部定时决定）。

### 3.5 连登奖励（成长中心 growth-center）

网页版 `www.workbuddy.cn/profile/growth-center` 与脚本用同一套接口：

| 用途 | 接口 |
|---|---|
| 连登状态 | `GET /activity/growth/streak`（`streak.days` / `next_tier` / `redemption_status.tiers`） |
| 兑换档位 | `POST /activity/growth/redeem`，body `{"tier":"7d","client_token":"<uuid>"}` |

档位规则：7 天档（能量 2 / 补登卡 1 / 抽奖 1）、14 天档（+50 积分）、28 天档（+150 积分）；**每档每月限兑 1 次**，连登天数每月清零重算，兑换不扣减连登天数。
档位状态 `locked`（未达标）/ `available`（可领）/ `claimed`（本月已领），脚本只对 `available` 发起兑换。

### 3.6 幂等

- 签到成功：`code=0`
- 今日已签到：`code=10001`，且**以 HTTP 400 返回**，业务 JSON 在 body 里 —— 脚本按「成功 + 跳过」处理，不当失败。

所以一天跑多次、多个定时任务重叠，都不会报错或重复领取。

### 3.7 推送内容

```
【WorkBuddy 自动签到】
👤 账号: 兰心 (134****2267)
🏷️ 备注: 家里电脑                       ← 可选，config.account_note，与账号名相同则省略
💻 设备: DESKTOP-89VE384
🧩 客户端: WorkBuddy 5.9.3                ← 本机客户端版本；服务器/面板上读不到时省略
🌐 IP: 10.1.9.15 / 公网 118.114.237.61
⏰ 2026-11-12 13:16:42
🔑 token: 签发 2026-11-12 16:06:18, 剩 27 天  ← JWT iat/exp 解析, 签发时刻即新旧标识; ≤7天标 ⚠️即将过期
📋 状态: 今日已签到
✅ 签到: 签到成功 +100 积分            ← 仅在实际执行签到(或失败)时出现, 已签到时省略
💰 剩余积分: 784.06 (体验版)
   └ 套餐基础积分 384.06/500 · 平台奖励积分 400/400
📅 本周: 4/7 天 (每日 100 积分)
🎁 连登: 已领取 7天档(能量+2 补登卡+1 抽奖次数+1)   ← 仅真领到档位(或领取失败/异常)时出现, 平时不推
🐱 派猫旅行: 暗影喵 在「咖啡馆」旅行中, 预计 56分钟后(17:17)到达, 奖励 6 积分
```

派猫这一行有四种形态：

```
🐱 派猫旅行: 暗影喵 已出发「健身房」, 预计 2小时45分后(11:02)到达        ← 本轮自动出发
🎁 派猫旅行: 暗影喵 已到达「健身房」, 领取成功, 奖励 8 积分              ← 到达并领到奖
🐱 派猫旅行: 暗影喵 在「咖啡馆」旅行中, 预计 56分钟后(17:17)到达, 奖励 6 积分
🐱 派猫旅行: 暗影喵 今日已完成(奖励已领取)                              ← 今日 1 次已用完
```

`run quiet` 静默模式（见第 7 节）只有出现 🎁 领奖或报错时才推送，其余情况只在终端打日志，不刷群。

- 内网 IP 用 UDP `connect` 探测默认出口地址获取（**不实际发包**）
- **默认只推 IPv4**：内网用 IPv4 路由探测，公网按 `ipv4.icanhazip.com` → `api.ipify.org` → `ifconfig.me` 顺序取（第一个只有 A 记录，强制走 v4）
- **只有当本机没有 IPv4 时**才退回 IPv6（内网连 `2400:3200::1`、公网试 `api6.ipify.org` / `ipv6.icanhazip.com`），并标注 `[IPv6]`，例如 `[IPv6] 2408:8207:… / 公网[IPv6] 2408:8207:…`
- 公网查询 3 秒超时，失败自动省略该段，不影响推送；`public_ip: false` 则连公网都不查
- 积分在签到**之后**查询，数值已含本次奖励

---

## 4. 目录结构

```
workbuddy-auto-checkin/
├── SKILL.md                  技能说明（WorkBuddy 读取的入口，含触发词与接口约定）
├── README.md                 本文件
├── config.example.json       配置模板（可随意分发）
├── config.json               实际配置（含 webhook 密钥，勿提交到公开仓库）
└── scripts/
    ├── workbuddy_checkin.py  主脚本：凭证解析 + 接口调用 + 推送
    └── wb_auth_helper.js     解密辅助：借客户端运行时取密钥并解密登录态
                              (仅本机使用, 不含凭证; 服务器/青龙模式用不到, 详见 3.2 节)
```

---

## 5. 安装 / 迁移到新机器

### 5.1 本机（已装好）

技能已在 `%USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\`，重启 WorkBuddy 后即可在对话里触发。

### 5.2 新机器

1. 把整个 `workbuddy-auto-checkin` 文件夹复制到：

   ```
   %USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\
   ```

2. 把 `config.example.json` 复制为 `config.json`，填上自己的企微 webhook key（`config.json` 不要跨机器传递密钥，各自填各自的）
3. 重启 WorkBuddy，在对话里说「查询积分」验证是否跑通

前提：该机器装了 WorkBuddy 客户端并已登录，且脚本与客户端是**同一个 Windows 用户**（否则读不到登录态）。

---

## 6. 配置项

`config.json`（环境变量优先级更高，便于临时覆盖）：

| 字段 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `qw_webhook` | `QW_WEBHOOK` / 面板 `WORKBUDDY_CONFIG.qw_webhook` | 空 | 企微群机器人 key，不填则跳过推送 |
| `account_note` | `WB_ACCOUNT_NOTE` | 空 | 备注，多账号区分用 |
| `device_name` | `WB_DEVICE_NAME` | 系统主机名 | 推送里的设备名 |
| `device_ip` | `WB_DEVICE_IP` | 自动探测 | 推送里的 IP（默认 IPv4，无 v4 才用 IPv6） |
| — | `WB_CLIENT_VERSION` | 自动读取 | 推送里的客户端版本；手动覆盖用，一般不用设 |
| `public_ip` | — | `true` | 是否查公网 IP，`false` 可省一次外部请求 |
| `access_token` | `WORKBUDDY_ACCESS_TOKEN` | 空 | 仅客户端未运行时临时用，正常留空 |
| `user_id` | `WORKBUDDY_USER_ID` | 空 | 配合上面一条使用 |
| `account_name` | `WB_ACCOUNT_NAME` | 空 | 手动填 token 时显示用 |
| `tasks` | 面板 `WORKBUDDY_CONFIG.tasks` | 全部启用 | 任务开关（见下节），对**所有账号/机器**统一生效 |

webhook key 获取：企业微信群 → 右上角「…」→ 群机器人 → 添加机器人 → 复制 Webhook 地址，取 `key=` 后面那段（贴整条 URL 不行）。

> 环境变量用 `setx` 设置后，**已在运行的 WorkBuddy 进程读不到**，需要重启客户端；改 `config.json` 则立即生效。

### 6.1 任务开关（哪些活动要自动执行）

`config.json` 里的 `tasks` 是**全局一份**，对所有账号和机器生效；青龙面板上没有 config.json，改面板唯一变量 `WORKBUDDY_CONFIG` 的 `tasks` 字段即可（优先级最高）：

```jsonc
// 本机 config.json
"tasks": {
  "checkin": true,
  "cat_travel": false
}
```

```jsonc
// 青龙面板 WORKBUDDY_CONFIG（其余字段保持不变）
{
  "accounts": [...],
  "qw_webhook": "...",
  "tasks": {"cat_travel": false}
}
```

> 老版本用过的独立环境变量 `WB_TASKS` 仍兼容（优先级高于 config.json），新部署统一放 `WORKBUDDY_CONFIG` 一个变量里。

| 任务名 | 默认 | 说明 |
|---|---|---|
| `checkin` | ✅ 启用 | 查状态 + 幂等签到，拿每日积分 |
| `streak_reward` | ✅ 启用 | 连登档位（7/14/28 天）达标后自动兑换能量/补登卡/积分/抽奖次数 |
| `cat_travel` | ✅ 启用 | 途中报 ETA，到达自动领奖，空闲且有当日名额时自动出发 |
| _未登记的新任务_ | ✅ 启用 | 配置里没写的任务按启用处理，方便灰度上线 |

规则与细节：

- **只写要改的**：未出现的任务走默认值（启用），不必列全
- **也接受带参数的写法**（`enabled` 之外还能带该任务的参数）：

  | 任务 | 参数 | 说明 |
  |---|---|---|
  | `cat_travel` | `location` | 目的地，支持 `id`/`code`/中文名；缺省或 `random` 每次随机 |
  | `streak_reward` | `tiers` | 只自动领这些档位，如 `["14d","28d"]`；不写则 7/14/28 三档都领 |

  ```jsonc
  "tasks": {
    "cat_travel":    {"enabled": true, "location": "coffee"},
    "streak_reward": {"enabled": true, "tiers": ["14d", "28d"]}
  }
  ```
- **禁用后推送里不出现该行**，只在日志里留一句「⏭ 跳过 …」，保持消息干净
- **单任务异常不影响其他任务**：某个活动接口挂了只在推送里标 `⚠️ 异常`，签到照常
- 查看当前生效状态：`python workbuddy_checkin.py tasks`

```
$ python workbuddy_checkin.py tasks
任务开关(来源: config:config.json)
  checkin      每日签到     ✅ 启用   查状态并幂等签到, 拿每日积分
  cat_travel   派猫旅行     ✅ 启用   途中报 ETA, 到达自动领奖, 空闲且有当日名额时自动出发
```

### 6.2 新增一个活动（开发者）

1. 写一个任务函数，签名 `f(token, uid) -> list[str]`，返回要进推送的文本行（内部自行处理幂等与失败提示）
2. 在 `TASK_REGISTRY` 里加一行：`("新任务名", "显示名", True, "一句话说明")`
3. 在 `process_account` 里用 `if task_enabled("新任务名"):` 包住调用，异常用 `try/except` 兜住

这样配置、`tasks` 命令、文档三处自动对齐，无需改其他地方。

---

## 7. 定时执行（可选）

> **脚本本身不判断时间、不做调度**：什么时候跑一次，完全由你外部的任务计划 / 青龙 cron / WorkBuddy 定时任务决定。下面是**建议方案**，不是代码里的行为。

**为什么建议一天跑多档**：派猫出发后 1~4 小时才到达（服务端随机），只跑一次 9:05 时猫还在路上，`claim` 返回 `not arrived yet`，当天奖励就白等了。所以推荐「早上一次完整推送 + 白天每 2 小时静默巡检」：

```bash
python workbuddy_checkin.py run          # 9:05 完整跑 + 推送（签到/连登/出发）
python workbuddy_checkin.py run quiet    # 11:05 / 13:05 … 静默跑，只在领到奖励或出错时推送
```

如果你只用 WorkBuddy 的定时任务（本机这一台就是），那就只配 9:05 一条，白天想领奖时手动说一句「帮我跑一次签到」——效果一样，只是不自动化。

Windows 任务计划里就是两条触发器：

```powershell
# 触发1: 每天 9:05 完整执行
schtasks /create /tn "WorkBuddy签到" /tr "python \"%USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\scripts\workbuddy_checkin.py\" run" /sc daily /st 09:05

# 触发2: 白天每 2 小时静默巡检（11:05 起，共 6 档）
schtasks /create /tn "WorkBuddy巡检" /tr "python \"%USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\scripts\workbuddy_checkin.py\" run quiet" /sc daily /st 11:05 /ri 120 /du 12:00
```

`run quiet` 的判定很简单：**本轮真领到连登档位 / 旅行奖励，或出现错误**才推送，其余情况只在终端打日志。所以白天那些档位不会刷群，到达领奖那一次会单独推一条 `🎁 派猫旅行: … 领取成功, 奖励 8 积分`。

注意：定时任务运行在用户会话下才能读到客户端登录态（勾选「只在用户登录时运行」）。

### 7.1 服务器部署（脱离本机客户端）

本机客户端离线也能跑：把明文 token 部署到任意一台有 Python 3.8+ 的服务器（Linux VPS / NAS 均可），脚本无第三方依赖。

**第一步：在本机提取 token 与 uid**

客户端在线时执行（token 只打印到屏幕，不落盘）：

```powershell
python scripts\wb_auth_helper.js  # 用法见文件头注释; 或:
python -c "import sys; sys.path.insert(0,'scripts'); import workbuddy_checkin as w; \
c=w._decrypt_local_auth(); print('token:', c[0][:40]+'...'); print('uid:', c[1])"
```

记下输出的完整 token（JWT）和 `user_id`。

**第二步：在服务器上配置**

```bash
# 传输 token 用 scp / 密码管理器, 千万不要走 git 或聊天记录明文
mkdir -p ~/wb-checkin && cd ~/wb-checkin
# 放入 scripts/workbuddy_checkin.py（可整个仓库 clone, config.json 不在仓库里）
cat > config.json << 'EOF'
{
  "qw_webhook": "你的企微webhook key",
  "access_token": "粘贴JWT",
  "user_id": "粘贴uid",
  "account_name": "兰心",       // 仅显示用
  "device_name": "云端服务器"   // 推送里区分来源
}
EOF
chmod 600 config.json            # 关键: 限权, 只有当前用户可读
python3 scripts/workbuddy_checkin.py run   # 先手动跑一次验证
```

**第三步：crontab 定时**

```bash
crontab -e
# 每天 9:05 完整跑(推送), 白天 11-21 点每 2 小时静默巡检(只在领到奖励/出错时推送)
5 9 * * * cd ~/wb-checkin && /usr/bin/python3 scripts/workbuddy_checkin.py run >> checkin.log 2>&1
5 11-21/2 * * * cd ~/wb-checkin && /usr/bin/python3 scripts/workbuddy_checkin.py run quiet >> checkin.log 2>&1
```

**注意事项**

- ⚠️ **token 等同账号密码**：不要提交进任何 git 仓库（`.gitignore` 已挡 `config.json`，但别复制到别处再提交）；服务器被入侵 = 账号被盗，建议只在内网/可信机器上跑
- **token 约 55 天过期**，且客户端重新登录后会更换。过期后脚本会报 401，回本机重跑第一步提取新 token 更新服务器配置即可
- 服务器上无法自动续期（解密依赖本机客户端），这是唯一的维护成本
- 签到/积分/旅行接口实测对来源 IP 无限制，公网 VPS 可正常调用

### 7.2 青龙面板部署（多客户端，方案一）

推荐形态：**多台 WorkBuddy 客户端的凭证收敛到青龙面板的 1 个环境变量**，每台机器各跑一次推送，按 MAC 幂等合并，互不覆盖。

**面板侧（一次性，约 5 分钟）**

1. 系统设置 → 应用设置 → 新建应用，权限**只勾「环境变量」**，得到 Client ID / Client Secret
2. 订阅管理 → 新建订阅：仓库 `GitKWorker/workbuddy-auto-checkin`（脚本更新自动同步）
3. 定时任务 → 新建**两条**：
   - 完整执行（推送）：命令 `task workbuddy_checkin.py run`，cron `5 9 * * *`
   - 静默巡检（只在领到奖励/出错时推送）：命令 `task workbuddy_checkin.py run quiet`，cron `5 11-21/2 * * *`

环境变量 `WORKBUDDY_CONFIG` **无需手动建**——本机首次推送时自动创建，面板上只有这**一个**变量，包含账号、webhook、任务开关三部分：

```json
{
  "accounts": [
    {"machine": "EC63D7AFE1DC", "device": "台式机-书房", "account": "兰心",
     "token": "eyJ...", "uid": "6acf...", "updated_at": "2026-11-12 15:00"}
  ],
  "qw_webhook": "企微群机器人 key",
  "tasks": {"checkin": true, "cat_travel": true}
}
```

> 旧版的两个变量 `WB_ACCOUNTS`（账号）和 `QW_WEBHOOK`（企微 key）仍能读；首次执行 `push_server` 会自动合并进 `WORKBUDDY_CONFIG` 并删除旧变量，不用手工迁移。

**本机侧（每台客户端各一次）**

`config.json` 填面板连接信息（已 gitignore，不进仓库）：

```json
{
  "ql_url": "http://面板地址:5700",
  "ql_client_id": "应用ClientID",
  "ql_client_secret": "应用ClientSecret"
}
```

然后推送登录态：

```powershell
python scripts\workbuddy_checkin.py push_server
```

- 按 `machine`（物理网卡 MAC，可用 `WB_MACHINE_ID` 覆盖）幂等合并：已有则更新、没有则追加
- 推送时保留面板上已有的 `qw_webhook` / `tasks`，只更新自己的那条账号记录（读改写，不会覆盖别人的字段）
- 建议：本机每日签到任务顺带执行 `push_server`，token（约 55 天有效期）永远新鲜
- 面板侧 `run` 检测到 `WORKBUDDY_CONFIG.accounts` 自动进入**多账号模式**：逐台签到/领奖，各自推送企微（设备名区分）
- ⚠️ 面板即敏感资产：强密码、勿裸暴露公网；Client Secret 只存本机与面板两处

---

## 8. 常见问题

**Q：报 401 / 解密失败？**
A：客户端没运行、没登录，或脚本与客户端不是同一个 Windows 用户。打开 WorkBuddy 登录后重试；也确认客户端版本路径是 `C:\Program Files\WorkBuddy\WorkBuddy.exe`（装在别处改 `_find_workbuddy_exe()` 的候选路径）。

**Q：积分接口返回 403？**
A：请求缺 `User-Agent`。脚本已内置 UA，自己写脚本调试时别漏。

**Q：积分接口 404？**
A：路径写成了 `/v2/billing/meter/get-user-resource-summary`。去掉 `/v2`。

**Q：推送没收到？**
A：看控制台是否打印 `企微推送(config:config.json): ok`。失败常见于 key 填了整条 URL、机器人被移除、群被解散。未配置时会打印「未配置企微 webhook, 跳过推送」但签到照常完成。

**Q：token 多久过期？**
A：JWT 约 55 天。过期后打开客户端重新登录，`workbuddy-desktop.info` 会刷新，脚本下次运行自动取到新 token，无需改配置。

**Q：会不会重复签到 / 报错？**
A：不会。`code=10001` 按幂等成功处理。

---

## 9. 安全说明

- 明文 token 只在子进程内存流转，不落盘、不打日志
- `config.json` 含 webhook 密钥，**不要提交到公开仓库或公共同步盘**
- `wb_auth_helper.js` 是只读操作：取密钥、解密、打印字段，不修改客户端任何文件
- 该脚本复刻本机客户端自身的请求行为，仅供个人账号自动化使用；请遵守 WorkBuddy 服务条款

---

## 10. 限制

- 仅在 Windows + WorkBuddy 桌面客户端下实测通过（macOS 路径有兼容代码但未验证）
- 依赖客户端在线登录，不能脱离客户端在服务器上跑（密钥取不到）
- 派猫旅行走新版 `/activity/growth/buddy/travel/{status,config,depart,claim}`，未到达时仅提示不重试；若官方再调整路径需同步修改 `TRAVEL_*_URL`
- 派猫每日只能出发 1 次，行程 1~4 小时；只跑一次定时任务拿不到奖励，需要白天多次巡检（见第 7 节）
- 积分接口属客户端内部路由，若官方调整路径需同步修改 `BALANCE_PATH`

---

## 11. 变更记录

| 版本 | 变更 |
|---|---|
| 1.12 | 推送瘦身：状态行去掉连签天数、连登行去掉“距 X 档还差 Y 天”；无可领档位时连登行整行不推（服务端连登口径混乱，只推确定信息） |
| 1.11 | 修复多账号连登提示：服务端 `next_tier_remaining` 在连登中断后不重置（days=1 仍报差 3 天），改为按档位表本地计算 `档位天数 - 连登天数` |
| 1.10 | 推送增加 `🧩 客户端: WorkBuddy <版本>`（读 `resources/install-manifest.json`，退回 exe 文件版本） |
| 1.9 | 设备 IP 优先 IPv4，本机无 IPv4 时才退回 IPv6 并标注 `[IPv6]`；公网接口先试仅 A 记录的 v4 端点 |
| 1.8 | 派猫旅行支持**主动出发**（`travel/config` 地点表 + `travel/depart`，每日 1 次，地点可指定或随机）；新增 `cat_depart` 命令与 `run quiet` 静默巡检（只在领到奖励/出错时推送） |
| 1.7 | 新增连登奖励（`streak` / `streak_claim`，7/14/28 天档位自动兑换）；多客户端凭证与任务开关收敛到面板单一变量 `WORKBUDDY_CONFIG` |
| 1.6 | 推送增加设备 IP 与剩余积分（含额度分项）；修正积分接口真实路径（去 `/v2`）与 403 陷阱 |
| 1.5 | 推送增加账号名（昵称 + 掩码手机号）与设备名 |
| 1.4 | 打通登录态自动解密（`wb_auth_helper.js`），不再需要手工 Cookie |
| 1.3 | 新增 `config.json` 配置入口，免重启生效 |
| 1.0 | 初版：签到、状态、积分、企微推送 |
