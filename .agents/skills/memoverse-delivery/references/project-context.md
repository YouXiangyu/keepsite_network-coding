# 项目运行信息

核验基准：2026-10-17，北京时间。以下是定位信息，不能替代操作前的实时查询。

## 位置与入口

| 项目 | 已知值 |
| --- | --- |
| 项目 | MemoVerse / network-coding |
| 本机根目录 | `D:\100-code\110-Project\network-coding` |
| GitHub 私人仓库 | `https://github.com/YouXiangyu/network-coding` |
| 默认分支 | `main` |
| 本机技能入口 | `C:\Users\游翔宇\.codex\skills\memoverse-delivery`，链接到项目 `.agents/skills/memoverse-delivery` |
| 网站 | `https://keepsite.space` |
| AWS 服务 / 区域 | 普通 Lightsail / 东京 `ap-northeast-1` |
| 实例 / 可用区 | `memoverse-keepsite` / `ap-northeast-1a` |
| 套餐 | `micro_3_0`：Linux、2 vCPU、1 GB RAM、40 GB 磁盘、月套餐上限 7 USD |
| 静态 IP 名称 / IPv4 | `memoverse-keepsite-ip` / `52.196.70.26` |
| SSH 用户 | `ubuntu` |
| DNS | Cloudflare；最近核验为直接解析源站的 DNS-only 行为 |

管理端公网 IP 受 Lightsail SSH 防火墙限制。连接失败先核对当前规则和管理端 IP，不直接放宽到所有来源。

## 本机与服务器路径

本机路径以项目根目录为基准；这些敏感文件不得加入 Git。

| 用途 | 路径 |
| --- | --- |
| 本机正在使用的数据 | `backend/data` |
| 本机网页 / API | `http://127.0.0.1:4173` / `http://127.0.0.1:8000` |
| SSH 私钥 / 已知主机 | `.cache/lightsail/ssh/id_rsa` / `.cache/lightsail/ssh/known_hosts` |
| 站点测试账号与口令 | `.cache/lightsail/access.json`，只记录位置，不读取或输出明文来做一般巡检 |
| 本机模型与推理依赖 | `.cache/models`、`.cache/anyimage`、`.cache/anyimage-deps` |
| 部署入口 | `scripts/deploy-lightsail.ps1`，默认计划模式，`-Execute` 执行 |
| 一次性数据迁移 | `scripts/package-server-data.py`，SQLite backup API + 文件 SHA-256 清单 |
| Linux 安装 / 模型准备 | `deploy/lightsail/install.sh` / `scripts/setup-anyimage.sh` |
| 发布目录 / 当前链接 | `/opt/memoverse/releases/<完整commit>` / `/opt/memoverse/current` |
| 服务器持久数据 | `/var/lib/memoverse` |
| 服务器共享模型缓存 | `/opt/memoverse/shared/cache` |
| 应用环境 | `/etc/memoverse.env` |
| 应用服务 | `memoverse.service`，单 Uvicorn worker，`127.0.0.1:8000` |
| Nginx 配置 | `/etc/nginx/sites-available/memoverse` |
| Nginx BasicAuth 哈希 | `/etc/nginx/memoverse.htpasswd` |
| TLS 证书与私钥目录 | `/etc/letsencrypt/live/keepsite.space` |
| TLS 配置脚本 | `deploy/lightsail/enable-https.sh`；已部署运维副本 `/opt/memoverse/admin/enable-https.sh` |
| 日志 | `journalctl -u memoverse`；`/var/log/memoverse/`；本机 `.cache/lightsail/` |

HTTPS 由源站 Nginx 使用 Let's Encrypt 证书提供，`certbot.timer` 续期并重载 Nginx。HTTP 跳转 HTTPS；整个站点与 API 由 BasicAuth 保护。未经用户指示不要调整认证或 DNS 代理模式。

当前数据库是 SQLite，原图和模型存磁盘。当前生成流程是官方 AnyImage MoGe2 CPU 推理加项目网格/GLB 导出；不是 Blender Bake Mesh，也不是 Lambda。1 GB 实例配置了 2 GB swap；已有功能样例不代表所有尺寸或并发场景都已验证。

## 启停与计费决策

- 待确认的仅是验收后自动停机：用户提出测试后关机以省钱；已说明普通 Lightsail 停机仍计费，用户正在了解费用。未确认是否保留该停机流程前不自动停机，也未授权删除或迁移实例。已授权阶段发布所需的启动不受此未决事项影响。
- 发布前始终查询实例状态；实例已停机时，按阶段发布授权启动并等待就绪。验收后的停机按用户明确选择执行。不要用历史的 `running` / `stopped` 状态驱动操作。
- 官方规则：普通 Lightsail 运行和停止都按保留时间计费，月套餐封顶；删除实例才停止实例套餐计费。普通按需 EC2 停止后不再计计算费，但 EBS、保留公网 IP 等可能继续收费。
- 2026-10-17 AWS Pricing API 核验东京本套餐单价为 `0.0094 USD/小时`；这是公开基础费，实际账单另计抵扣、税费和其他资源。未来报价重新查询，不能固定按此数值承诺账单。
- Lightsail 静态 IP 绑定实例时不额外收费；未绑定超过一小时后产生单独费用。删除实例不是清除所有相关费用。
- 账户既有 5 USD 月预算提醒不等于金额硬上限，不自动调整预算。

来源：[Lightsail 计费](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html)、[EC2 状态与计费](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-lifecycle.html)。

## 现有约束与文档

- 根 `AGENTS.md` 与 `demo/AGENTS.md`：一般工作规则与受保护前端运行时。
- `docs/项目定义与决策主文档.md`：产品决策。
- `deploy/lightsail/README.md`：安装、发布与恢复流程。
- `docs/Lightsail部署验收.md`：迁移、实际验证、计时范围与清理剩余项。
- 当前更新脚本要求目标 commit 发布目录不存在，不支持直接重跑相同 commit。失败时先检查服务器残留状态再决定恢复步骤。
- `activate.sh` 的健康失败分支会尝试回滚，但 `systemctl restart` 本身失败时可能在进入回滚分支前退出。每次失败都实际核对链接和服务，不能保证已自动恢复。
- 当前没有统一 formatter。遵循现有风格，格式整理不能触碰受保护运行时或扩散到无关代码。
