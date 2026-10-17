# 项目运行信息

核验基准：2026-10-18，北京时间。以下是定位信息，不能替代操作前的实时查询。

## 位置与入口

| 项目 | 已知值 |
| --- | --- |
| 项目 | MemoVerse / network-coding |
| 本机根目录 | `D:\100-code\110-Project\network-coding` |
| GitHub 私人仓库 | `https://github.com/YouXiangyu/network-coding` |
| 默认分支 | `main` |
| 本机技能入口 | `C:\Users\游翔宇\.codex\skills\memoverse-delivery`，链接到项目 `.agents/skills/memoverse-delivery` |
| 网站 | `https://keepsite.space` |
| AWS 服务 / 区域 | 普通按需 EC2 / 东京 `ap-northeast-1` |
| 实例 ID / 名称 / 可用区 | `i-0e9ba9bc6f4b1c019` / `memoverse-keepsite` / `ap-northeast-1a` |
| 规格 | `t3.small`：x86_64、2 vCPU、2 GB RAM；CPU credits 为 standard，避免 surplus credit 费用 |
| 系统与磁盘 | Canonical Ubuntu 24.04；10 GB 加密 gp3 根卷 `vol-0bb6aa01805d28c75`，旧 20 GB 卷已删除 |
| 最近运行状态 | 2026-10-18 阶段收尾再次核验为 `stopped`，业务数据保留；操作前仍须实时查询 |
| 公网 IPv4 | 自动分配；停机后无公网 IP，上次 `18.183.236.78` 已释放，启动前必须查询新值 |
| Launch template / 新建默认版本 | `lt-016bca8507ac81480` / `2`，根盘默认 10 GB |
| 安全组 / SSH key pair | `sg-0f472ac3891c65681` / `memoverse-ec2-20261017` |
| SSH 用户 | `ubuntu` |
| DNS | Cloudflare，`memoverse-ddns.service` 每次启动更新 A 记录，正常关闭时撤回；当前停机 A 记录为空，发布时 DNS-only、TTL 60 |

管理端公网 IP 受 EC2 安全组 SSH 规则限制。连接失败先核对当前规则和管理端 IP，不直接放宽到所有来源。账户原有另外两台停止的测试实例不属于本项目，不得顺手启动、改规格或清理。

## 本机与服务器路径

本机路径以项目根目录为基准；这些敏感文件不得加入 Git。

| 用途 | 路径 |
| --- | --- |
| 本机正在使用的数据 | `backend/data` |
| 本机网页 / API | `http://127.0.0.1:4173` / `http://127.0.0.1:8000` |
| SSH 私钥 / 已知主机 | `.cache/server/ssh/id_rsa` / `.cache/server/ssh/known_hosts` |
| SSH 主机身份别名 | `memoverse-i-0e9ba9bc6f4b1c019`；部署传 `-HostKeyAlias`，保持动态 IP 变化时的密钥校验 |
| 站点测试账号与口令 | `.cache/server/access.json`，只记录位置，不读取或输出明文来做一般巡检 |
| Cloudflare DDNS 配置 | 本机 `.cache/server/cloudflare.json`；安装目标 `/etc/memoverse/cloudflare.json`，Token 只允许本域名 DNS 编辑 |
| DDNS 服务 / 程序 / 状态 | `memoverse-ddns.service` / `/opt/memoverse/admin/sync-cloudflare-dns.py` / `/var/lib/memoverse-ddns/state.json` |
| 本机模型与推理依赖 | `.cache/models`、`.cache/anyimage`、`.cache/anyimage-deps` |
| 部署入口 | `scripts/deploy-ubuntu.ps1`，`Server` 必填，默认计划模式，`-Execute` 执行；新机另加 `-InitializeHost` |
| 一次性数据迁移 | `scripts/package-server-data.py`，SQLite backup API + 文件 SHA-256 清单 |
| Linux 安装 / 模型准备 | `deploy/ubuntu/install.sh` / `scripts/setup-anyimage.sh` |
| 发布目录 / 当前链接 | `/opt/memoverse/releases/<完整commit>` / `/opt/memoverse/current` |
| 服务器持久数据 | `/var/lib/memoverse` |
| 服务器共享模型缓存 | `/opt/memoverse/shared/cache` |
| 应用环境 | `/etc/memoverse.env` |
| 应用服务 | `memoverse.service`，单 Uvicorn worker，`127.0.0.1:8000` |
| Nginx 配置 | `/etc/nginx/sites-available/memoverse` |
| Nginx BasicAuth 哈希 | `/etc/nginx/memoverse.htpasswd` |
| TLS 证书与私钥目录 | `/etc/letsencrypt/live/keepsite.space` |
| TLS 配置脚本 | `deploy/ubuntu/enable-https.sh` |
| 日志 | `journalctl -u memoverse`；`/var/log/memoverse/`；本机 `.cache/server/` |

HTTPS 由源站 Nginx 使用 Let's Encrypt 证书提供，`certbot.timer` 续期并重载 Nginx。迁移保留现有认证与证书；整个站点与 API 由 BasicAuth 保护。自动 DDNS 只修改指定 A 记录，不擅自改变 Cloudflare 代理模式。

当前数据库是 SQLite，原图和模型存磁盘。当前生成流程是官方 AnyImage MoGe2 CPU 推理加项目网格/GLB 导出；不是 Blender Bake Mesh，也不是 Lambda。2 GB 实例配置了 2 GB swap，应用 drop-in 设置 MemoryHigh=1200M、MemoryMax=1500M。新机功能样例通过，详细证据见 EC2 验收记录，不能把单样例等同所有负载保证。

## 启停与计费决策

- 用户已明确删除 Lightsail 并改用 EC2，选择新建 `t3.small`。按此前要求执行部署前开机、用户确认测试通过后正常停机；未得到用户验收时保留环境供测试。
- 发布前始终查询上述明确 instance ID 的状态和当次公网 IP；启动后等待状态检查及 DDNS 就绪。停机后确认 `stopped` 与 DNS 关机清理结果。不要用历史状态或 IP 驱动操作。
- 普通按需 EC2 停止后不再计计算费，自动公网 IPv4 随停止释放；EBS 磁盘继续保留和计费。不得把停机说成完全零费用。
- 2026-10-17 AWS Pricing API 核验：`t3.small` 计算费 `0.0272 USD/小时`，动态公网 IPv4 `0.005 USD/使用小时`，当前 10 GB gp3 磁盘约 `0.96 USD/月`。实例与 IP 运行时合计 `0.0322 USD/小时`；税费、抵扣和额外流量另计。gp3 容量按秒折算、最低 60 秒，不按实际文件占用计费；未来报价重新查询。
- 旧 Lightsail `memoverse-keepsite`、`memoverse-keepsite-ip` 和部署公钥已删除；旧 IP `52.196.70.26` 已释放。新 EC2 已验收迁移完整性；本机 `.cache/migration-lightsail-ec2` 暂存目录删除被自动审批拦截，已交用户手动清理。
- 账户既有 5 USD 月预算提醒不等于金额硬上限，不自动调整预算。

来源：[EC2 状态与计费](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-lifecycle.html)、[公网 IPv4 计费](https://aws.amazon.com/vpc/pricing/)。

## 现有约束与文档

- 根 `AGENTS.md` 与 `demo/AGENTS.md`：一般工作规则与受保护前端运行时。
- `docs/项目定义与决策主文档.md`：产品决策。
- `deploy/ubuntu/README.md`：安装、发布与恢复流程。
- `docs/Lightsail部署验收.md`：旧环境历史证据与退役说明，不能用于定位当前实例。
- `docs/EC2部署验收.md`：当前环境验收与 DDNS 验证范围。
- 通用 Ubuntu 发布脚本会核验制品哈希与准备状态后复用同一 commit；未知目录或内容冲突停止，不自动删除。激活失败后依旧要核对回滚输出与真实运行状态，不能将尝试回滚等同成功。
- 当前没有统一 formatter。遵循现有风格，格式整理不能触碰受保护运行时或扩散到无关代码。
- 2026-10-18 阶段收尾只整理测试格式与文档，未改变已部署的应用代码，也未重新开机。本机预览已停止；缓存删除被自动审批拒绝，未完成清单集中在 `docs/EC2部署验收.md` 的“阶段收尾与清理状态”。保留业务数据、模型、凭据、当前发布制品和验收证据。
- DDNS 限定一个实例管理一个域名。正常服务停止会撤回自己确认过的 A 记录；强制关机、Token 失效或并发改 DNS 时不保证撤回，停机后应读回确认。2026-10-18 换盘后已验证新公网 IP 发布，以及最终真实 EC2 停机后 A 记录归零。
