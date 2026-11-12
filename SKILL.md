---
name: workbuddy-auto-checkin
display_name: WorkBuddy签到+积分查询+企微推送
description: Buddy加油站每日签到（自动从本机 WorkBuddy 客户端解密登录态），幂等跳过已完成任务，一键执行后推送企微明细（含账号名、设备名、设备 IP 与剩余积分）
author: 龙猫科技说复刻增强版（带积分）
version: 1.6
agent_created: true
permissions:
  - http.fetch
trigger_words:
  - 签到
  - 积分签到
  - 检查签到状态
  - 派猫旅行
  - 领取旅行奖励
  - 一键执行
  - 查询积分
---
# 技能执行规则

你是 WorkBuddy 签到助手（Buddy 加油站每日签到）。

## 接口（实测验证 2026-11-12）

签到（POST `https://www.codebuddy.cn/v2/billing/meter/`）：
- 状态查询：`checkin-activity-status` → `{today_checked_in, streak_days, daily_credit, today_credit, week_progress, checkin_dates, total_credits}`
- 签到领取：`daily-checkin`

剩余积分（POST `https://www.codebuddy.cn/billing/meter/get-user-resource-summary`，**注意不带 `/v2` 前缀**，这是客户端 `resourcePrefix` 为空导致的）：
- 返回 `{TotalCredits, SubscriptionPackageName, IsPaidUser, CreditGroups[{Name, CycleRemainCapacity, CycleTotalCapacity}], Packages[...]}`
- 实测：`TotalCredits=785.48`、`体验版`、套餐基础积分 385.48/500、平台奖励积分（签到）400/400
- 该路由**缺 User-Agent 会 403**，必须带 UA 头

派猫旅行（2026-11 更新，旧 `cat-travel-*` 已 404，不要再用）：
- `GET /v2/activity/growth/buddy/info`：猫猫档案（name/rarity/personality）+ `poll_interval_seconds`
- `GET /activity/growth/buddy/travel/status`：行程状态（`state=traveling`、`location.name` 目的地、`depart_at/arrive_at/server_now` Unix 秒可算 ETA、`reward_credit`、`daily_limit_reached`）
- `POST /activity/growth/buddy/travel/claim`：领取到达奖励；未到达返回 403 `"not arrived yet"`
- 旅行由服务端自动推进，**没有 start 接口**；到达后客户端收到 `growth.travel_arrival` 通知再领奖
仍下线（404，不要重试）：`/v2/billing/meter/info`、`/v2/.../get-user-resource-summary`（带 `/v2` 的旧写法）。

## 鉴权（已实测打通）

- Header：`Authorization: Bearer <accessToken>` + `X-User-Id: <uid>`
- accessToken 是明文 JWT（约 55 天有效期），但本机登录态文件里存的是 **AES-256-GCM 加密信封**（`$wbEncrypted`），不能直接用。
- **自动解密**：脚本 `scripts/workbuddy_checkin.py` 会用 `ELECTRON_RUN_AS_NODE=1` 借 WorkBuddy.exe 运行时执行 `scripts/wb_auth_helper.js`，通过客户端原生绑定 `electron_browser_workbuddy_storage` 取得 `atRestSecretKey` 并在子进程内解密信封，明文 token 只在内存中流转、不落盘。
- 前提：**WorkBuddy 客户端正在运行且已登录**，与脚本同一 Windows 用户。
- 兜底：环境变量 `WORKBUDDY_ACCESS_TOKEN` / `CODEBUDDY_ACCESS_TOKEN`（明文 JWT）优先于自动解密。
- 密钥派生规则（如需人工排查）：`key = SHA256(atRestSecretKey字符串)`，`keyId = SHA256(key).hex[:16]`；field 信封 AAD = `"WB-AAD\0" + 0x01 + u32(5)"WBEV1" + u32(6)"sym-v1" + u32(suite) + u32(16)keyId + [0x02] + [0x00] + [0x00]`。

## 错误码

- code=0：成功
- code=10001：今日已签到 —— **注意：会以 HTTP 400 返回，body 里才是业务 JSON，必须按幂等成功处理，不可视为失败**
- 登录态过期时返回 401，提示用户打开 WorkBuddy 客户端重新登录后重试

## 脚本命令映射

脚本路径（Windows）：`%USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\scripts\workbuddy_checkin.py`

执行时**先进入脚本目录**再运行，否则相对路径会找不到文件：

```bash
cd "%USERPROFILE%\.workbuddy\skills\workbuddy-auto-checkin\scripts" && python workbuddy_checkin.py run
```

| 用户指令 | 脚本命令 | 行为 |
|---|---|---|
| 一键执行 | `python workbuddy_checkin.py run` | 查状态 → 幂等签到 → 剩余积分 → 本周汇总 → 推送企微 |
| 检查签到状态 | `python workbuddy_checkin.py status` | 仅查询签到状态/连签/本周进度 |
| 签到 | `python workbuddy_checkin.py checkin` | 未签则签，已签幂等跳过 |
| 查询积分 | `python workbuddy_checkin.py point` | 剩余积分总额 + 额度分项 + 今日/连签 |
| 查询猫猫档案 | `python workbuddy_checkin.py cat_status` | 猫猫昵称/稀有度/性格 + 轮询间隔 |
| 领取旅行奖励 | `python workbuddy_checkin.py cat_claim` | 到达则领奖，未到达明确提示不算失败 |

完整实现说明、配置项、常见问题见同目录 `README.md`。

## 企微推送

- 仅 `run` 一键执行后推送，其他指令不推送，避免刷屏
- **配置位置（二选一，环境变量优先）**：
  1. 技能目录 `config.json`（推荐，改完直接生效，无需重启 WorkBuddy）
     ```json
     { "qw_webhook": "<企业微信群机器人 key>", "account_note": "", "device_name": "", "device_ip": "", "public_ip": true }
     ```
  2. 环境变量 `QW_WEBHOOK`（注意：用 `setx` 设置后需重启 WorkBuddy 才生效）
- webhook key 获取：企业微信群 → 右上角「…」→ 群机器人 → 添加机器人 → 复制 Webhook 地址，取 `key=` 后面那段
- **推送内容**（首部固定带账号与设备，多机多账号可区分）：
  ```
  【WorkBuddy 自动签到】
  👤 账号: 兰心 (134****2267)      ← 自动解密客户端昵称 + 掩码手机号
  🏷️ 备注: 家里电脑              ← 可选，config.account_note，与账号名相同则省略
  💻 设备: DESKTOP-89VE384        ← 系统主机名，可用 config.device_name 覆盖
  🌐 IP: 10.1.9.15 / 公网 118.114.237.61   ← 内网 IP 必显示，公网可关（config.public_ip=false）
  ⏰ 2026-11-12 13:16:01
  📋 状态: 今日已签到, 连签 4 天
  ✅ 签到: 今日已签到(跳过)
  💰 剩余积分: 785.48 (体验版)
     └ 套餐基础积分 385.48/500 · 平台奖励积分 400/400
  📅 本周: 4/7 天 (每日 100 积分)
  ```
- 账号名取值顺序：客户端昵称 > 掩码手机号 > `uid` 前 8 位（手动填 token 时可用 `WB_ACCOUNT_NAME` / `config.account_name` 指定）
- 设备名取值顺序：`WB_DEVICE_NAME` > `config.device_name` > 系统主机名（`COMPUTERNAME`）
- 设备 IP 取值顺序：`WB_DEVICE_IP` > `config.device_ip` > 内网 IP（UDP 探测默认出口，不发包）+ 公网出口 IP（api.ipify.org，失败自动省略）
- 剩余积分在**签到后**查询，数值已含本次奖励；额度分项只列总额大于 0 的分组
- 参考模板见 `config.example.json`；`config.json` 含密钥，不要提交或分享

## 安全规则

- 全程不打印、不落盘明文 token；`wb_auth_helper.js` 的密钥只存在于子进程内存
- 不把用户粘贴的 token/cookie 写入任何文件
