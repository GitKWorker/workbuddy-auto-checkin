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
| 企微推送 | 一键执行后推送明细，含账号名、设备名、设备 IP |
| 多账号/多机区分 | 每台机器推送自带主机名与 IP，一个群里也分得清 |

派猫旅行已接入新版接口（2026-11）：旅行由服务端自动推进，`run` 一键执行时会顺带尝试领取到达奖励；`cat_status` 查猫猫档案，`cat_claim` 手动领取。旧 `cat-travel-*` 接口已永久 404，不要再用。

---

## 0. 实测环境（配套版本）

| 项目 | 实测版本 | 说明 |
|---|---|---|
| WorkBuddy 客户端 | **5.9.3** | 接口路径、登录态加密方案均以该版本为准；客户端升级后若报 404/解密失败，优先检查新版 `app.asar` 里是否改了路由 |
| 操作系统 | Windows 11 | macOS 有路径兼容代码但未验证 |
| Python | 3.13 | 仅用标准库，无第三方依赖，3.8+ 应均可运行 |

> 接口与客户端版本强相关：签到/积分走 `/v2/billing/meter/*`，派猫旅行走 `/activity/growth/buddy/travel/claim`（从 5.9.3 客户端 `app.asar` 中提取）。官方升级客户端后接口可能变动，届时对照 `resources/app.asar` 里的实际路由同步修改脚本常量（`API_BASE` / `BALANCE_PATH` / `TRAVEL_CLAIM_URL`）。

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
python workbuddy_checkin.py run       # 一键：签到 + 积分 + 推送
python workbuddy_checkin.py status    # 只看签到状态
python workbuddy_checkin.py checkin   # 只签到（幂等）
python workbuddy_checkin.py point     # 只看剩余积分
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
- 它**只在装了 WorkBuddy 客户端的本机上会被调用**；青龙面板 / 纯 token 服务器模式下脚本走 `WB_ACCOUNTS` 环境变量的明文 token，这个文件完全不会被使用（订阅拉下来看到它属正常，放着不管即可）

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

### 3.4 幂等

- 签到成功：`code=0`
- 今日已签到：`code=10001`，且**以 HTTP 400 返回**，业务 JSON 在 body 里 —— 脚本按「成功 + 跳过」处理，不当失败。

所以一天跑多次、多个定时任务重叠，都不会报错或重复领取。

### 3.5 推送内容

```
【WorkBuddy 自动签到】
👤 账号: 兰心 (134****2267)
🏷️ 备注: 家里电脑                       ← 可选，config.account_note，与账号名相同则省略
💻 设备: DESKTOP-89VE384
🌐 IP: 10.1.9.15 / 公网 118.114.237.61
⏰ 2026-11-12 13:16:42
🔑 token: 签发 2026-11-12 16:06:18, 剩 27 天  ← JWT iat/exp 解析, 签发时刻即新旧标识; ≤7天标 ⚠️即将过期
📋 状态: 今日已签到, 连签 4 天
✅ 签到: 今日已签到(跳过)
💰 剩余积分: 784.06 (体验版)
   └ 套餐基础积分 384.06/500 · 平台奖励积分 400/400
📅 本周: 4/7 天 (每日 100 积分)
```

- 内网 IP 用 UDP `connect` 探测默认出口地址获取（**不实际发包**）
- 公网 IP 走 `api.ipify.org`，3 秒超时，失败自动省略该行，不影响推送
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
| `qw_webhook` | `QW_WEBHOOK` | 空 | 企微群机器人 key，不填则跳过推送 |
| `account_note` | `WB_ACCOUNT_NOTE` | 空 | 备注，多账号区分用 |
| `device_name` | `WB_DEVICE_NAME` | 系统主机名 | 推送里的设备名 |
| `device_ip` | `WB_DEVICE_IP` | 自动探测 | 推送里的 IP |
| `public_ip` | — | `true` | 是否查公网 IP，`false` 可省一次外部请求 |
| `access_token` | `WORKBUDDY_ACCESS_TOKEN` | 空 | 仅客户端未运行时临时用，正常留空 |
| `user_id` | `WORKBUDDY_USER_ID` | 空 | 配合上面一条使用 |
| `account_name` | `WB_ACCOUNT_NAME` | 空 | 手动填 token 时显示用 |

webhook key 获取：企业微信群 → 右上角「…」→ 群机器人 → 添加机器人 → 复制 Webhook 地址，取 `key=` 后面那段（贴整条 URL 不行）。

> 环境变量用 `setx` 设置后，**已在运行的 WorkBuddy 进程读不到**，需要重启客户端；改 `config.json` 则立即生效。

---

## 7. 定时执行（可选）

Windows 任务计划每天 9:00 跑一次：

```powershell
schtasks /create /tn "WorkBuddy签到" /tr "python \"%USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\scripts\workbuddy_checkin.py\" run" /sc daily /st 09:00
```

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
# 每天 9:05 执行
5 9 * * * cd ~/wb-checkin && /usr/bin/python3 scripts/workbuddy_checkin.py run >> checkin.log 2>&1
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
3. 定时任务 → 新建：命令 `task workbuddy_checkin.py run`，cron `5 9 * * *`

环境变量 `WB_ACCOUNTS` **无需手动建**——本机首次推送时自动创建，格式：

```json
[
  {"machine": "EC63D7AFE1DC", "device": "台式机-书房", "account": "兰心",
   "token": "eyJ...", "uid": "6acf...", "updated_at": "2026-11-12 15:00"}
]
```

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
- 建议：本机每日签到任务顺带执行 `push_server`，token（约 55 天有效期）永远新鲜
- 面板侧 `run` 检测到 `WB_ACCOUNTS` 自动进入**多账号模式**：逐台签到/领奖，各自推送企微（设备名区分）
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
- 派猫旅行走新版 `/activity/growth/buddy/travel/claim`，未到达时仅提示不重试；若官方再调整路径需同步修改 `TRAVEL_CLAIM_URL`
- 积分接口属客户端内部路由，若官方调整路径需同步修改 `BALANCE_PATH`

---

## 11. 变更记录

| 版本 | 变更 |
|---|---|
| 1.6 | 推送增加设备 IP 与剩余积分（含额度分项）；修正积分接口真实路径（去 `/v2`）与 403 陷阱 |
| 1.5 | 推送增加账号名（昵称 + 掩码手机号）与设备名 |
| 1.4 | 打通登录态自动解密（`wb_auth_helper.js`），不再需要手工 Cookie |
| 1.3 | 新增 `config.json` 配置入口，免重启生效 |
| 1.0 | 初版：签到、状态、积分、企微推送 |
