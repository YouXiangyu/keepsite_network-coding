# keepsite 项目运行信息
## 2026-10-24 名称迁移状态

用户明确要求软件及技术标识统一为小写 `keepsite`，并确认执行显式完整迁移。当前源码目标：Android `io.github.youxiangyu.keepsite`、`KEEPSITE_*` 环境变量、`keepsite.sqlite3`、`keepsite.service`、`keepsite-auto-stop.timer`、`/opt/keepsite`、`/var/lib/keepsite-v3`、`/etc/keepsite.env`。数据复制与验证工具为 `scripts/migrate-keepsite.py`，迁移步骤见 `docs/keepsite迁移.md`。

这些是代码和迁移目标，不是已切换的服务器事实。本轮未启动、部署或修改 AWS 资源，未安装新包或卸载旧包。原账户 ID 和 S3 对象归属保持不变。下面旧名称、资源 ID、桶名、路径及验收记录用于定位真实旧环境与回滚，保留原文；禁止把旧资源值直接字符串替换后当成真实云资源。后续切换必须先查询实例实时状态，保留本次开机原有 58 分钟自动关机期限。

## 改名前的历史运行记录

2026-10-24 视觉第二轮：Web `e6f414a`、Android `ce37eb1` 完成参考图的真错列、紧凑层次及搜索/排序，新增失败图片刷新和对比度回归。88 Web单测、56浏览器（含本机参考素材）、34 Android JVM、真机5项UI通过，Lint 0错误/13警告。Sol实现/独立审查，Astra定位并修正测试自动填充弹窗和旋转同步问题，未改生产权限或密码库。证据 `docs/evidence/visual-redesign-20261024.json`，最新版包及截图 `.cache/visual-redesign-20261024/`。用户授权参考裁图仅用于隔离夹具，不提交素材、不改真实账号。延续PR #5和旧提交，不合并/部署。为用户查看保留本机4180视觉夹具及当前手机ADB reverse（仅本机预览、模拟身份/存储）；看完后可按 `.cache/m7-native-control.json` 的本轮归属正常关闭，不能把预览当生产服务。以下为此前交付记录。

最近更新：2026-10-24，北京时间。AWS CLI 重新登录并确认项目 EC2 stopped、无公网 IP；S3 版本控制与阻止公开访问启用、无 CORS，Cognito Google/COGNITO 与既有回调配置存在。只对一枚随机合成 S3 对象进行创建、校验和精准版本清理，未改云配置、未启动或部署。下方 2026-10-18 运行信息为历史定位，不能替代操作前查询。

2026-10-24 移动端阶段：Web 视觉改版 `7af2946`、传输完成确认修复 `dded2c1`；Android 两列票夹/图标底栏、360dp 输入框滚动坐标修复和明确选择真机的互通工具已实现。Web 最终 88 单测、54 浏览器、7 双浏览器通过；Android 34 JVM、Lint 0错误/10警告、模拟器三档9次UI、模拟器/单台真机CRUD及JNI双向传图通过。真机完整UI仪器测试最终3/3通过：Sol高思考子代理定位系统START_ABORTED，并仅在测试launch期间临时采用后台启动权限，finally释放，API29判断保留；生产代码及手机长期权限未改变。原断言、旋转与后台恢复均通过，独立审查通过，无需Astra。默认HTTPS debug APK与loopback制品在 `.cache/mobile-delivery-20261024/`，不是正式签名或上线验收。完整范围见 `docs/evidence/mobile-delivery-20261024.json`。用户已明确允许代理在当前手机安装和交互，第二台真机及最终体验确认尚未完成；历史“由用户执行”不限制本次已授权单台自动验证。`docs/UI_example/` 和 `output/` 保留且不入提交。以下2026-10-23段落为历史检查点。

2026-10-23 Git 交付整理：Web PR #4 已按用户明确授权以保留历史的 merge commit `6126bf50e5b2bf96e3bef1d2e842328cb7d7e0a8` 合入 `main`，本地与 GitHub 主线已读回核对。合并前最新 head `2ebd2b09959280c5d721e9a1503a6d1d392800c0` 的 `Backend tests`、`Frontend tests`、`deployment-recovery` 均成功，Codex 对该提交的审查已完成且无未解决问题。旧海报残留、恢复保护标记与迁移并发覆盖三项修复均有回归测试；最新 Linux 后端 CI 为 477 项及 11 子测试通过、无跳过。本次主线交付范围为文档与 Web M1—M6，包含 300 条记录的 540 个主读取任务及 20 张合成票据的真实 OCR 流水线。已验证文档分支 `docs/spec-and-dev-plan@df53192` 与 Web head 均为主线祖先，随后删除这两个本地/远端分支并清理专用测试工作树。旧浮雕分支的 10 个独有提交按用户另行授权保存在已推送标签 `archive/subject-relief-prototype-20261023`（指向 `0594620`），PR #3 已关闭、分支已删除，未合入新版。

原生 Android 工作继续保存在独立 `codex/android-native-progress` 的 M7 WIP 检查点，本次同步已合入主线的审查修复，不作为已验收交付。此前两个 4180 测试 APK、34 项 JVM 测试与 Lint 通过；后台闲置连接 EOF 已修复并通过相同 6000ms 场景复测。完整 UI 矩阵、原生 CRUD、Android↔Web 双向传图、默认 HTTPS 验收包和两台真机验收尚未完成。全量本地后端 472 项及 11 子测试通过、5 项明确跳过；随后测试夹具定点 4 项通过。准确边界见开发计划、Android README 与 `docs/evidence/android-m7-checkpoint-20261023.json`。`a2cb349` 的 Android、后端及部署恢复 CI 通过，前端任务因依赖准备耗时而触及 20 分钟上限，未报告为通过；同步后的验证以进度分支最新提交为准，Web PR 的检查不能代替 M7 验收。本次 Git 整理不恢复大规模功能联调；临时模拟器、本机夹具和自建 ADB 已关闭，个人新增的 `docs/UI_example/` 和既有 `output/pdf/` 保留、不夹带进代码提交。

产品方向仍为私有 S3 长期保存原票与海报、账号缓存、P2P 加速和服务器回退，服务端使用轻量 CPU OCR。以上本机结果使用合成数据、fakeredis 与本机/内存对象存储，不证明真实 S3、TURN、Cognito、跨网络或物理设备验收；免费 TURN 保持关闭。这些改动均未发布到 EC2。下列云资源位置仅供后续明确授权操作定位；旧模型、用户数据和回滚文件不得因文档或分支整理清理。EC2 非常驻时的信令与鉴权可用性仍按用户要求暂缓决定，本次不改变 58 分钟定时器、不启动或部署云资源。

## 位置与入口

| 项目 | 已知值 |
| --- | --- |
| 项目 | keepsite / network-coding |
| 本机根目录 | `D:\100-code\110-Project\network-coding` |
| GitHub 私人仓库 | `https://github.com/YouXiangyu/network-coding` |
| 默认分支 | `main` |
| 改版前 Git 核验（2026-10-21） | GitHub `main` 为 `eb486c9`；当前工作分支 `docs/spec-and-dev-plan` 的改版前检查点 `d7945d8` 已推送并读回核对。检查点无源码改动；之后的文档提交以 Git 日志为准 |
| 本机技能入口 | `C:\Users\游翔宇\.codex\skills\keepsite-delivery`，链接到项目 `.agents/skills/keepsite-delivery` |
| 网站 | `https://keepsite.space` |
| AWS 服务 / 区域 | 普通按需 EC2 / 东京 `ap-northeast-1` |
| 实例 ID / 名称 / 可用区 | `i-0e9ba9bc6f4b1c019` / `memoverse-keepsite` / `ap-northeast-1a` |
| 规格 | `t3.small`：x86_64、2 vCPU、2 GB RAM；CPU credits 为 standard，避免 surplus credit 费用 |
| 系统与磁盘 | Canonical Ubuntu 24.04；10 GB 加密 gp3 根卷 `vol-0bb6aa01805d28c75`，旧 20 GB 卷已删除 |
| 最近运行状态 | 2026-10-18 19:46（北京时间）AWS 确认 `stopped`；19:40 用户确认 Google 登录和邮箱收件后，完成 QA 清理，19:45 正常提前停机；操作前仍须实时查询 |
| 最近应用运行提交 | `2e74f23408afb26410c45b6c86515cd7964c3f13`，current 与当时 MainPID 1451 的 cwd 均核验一致。后续 PR #2 已于 2026-10-19 02:57 合并，`main` 为 `eb486c9`；此为 Git 状态，不代表重新发布或实例实时状态。主机自动关机配置仍为 `622486b`（58 分钟） |
| 公网 IPv4 | 当前无公网 IP；本次 19:27 开机使用 `54.150.236.186`，停机后已释放；`43.207.114.190`、`18.181.150.249`、`13.230.191.15` 均为历史地址，每次启动必须重新查询 |
| Launch template / 新建默认版本 | `lt-016bca8507ac81480` / `2`，根盘默认 10 GB |
| 安全组 / SSH key pair | `sg-0f472ac3891c65681` / `memoverse-ec2-20261017` |
| SSH 用户 | `ubuntu` |
| DNS | Cloudflare，`memoverse-ddns.service` 每次启动更新 A 记录，正常关闭时撤回；本次停机后公共 DNS DoH 无 A 回答，未用 Cloudflare API 查询记录数量；运行时 DNS-only、TTL 60 |
| 自动关机 | `memoverse-auto-stop.timer` 已 enabled，`OnBootSec=58min`、`AccuracySec=1s`；每次系统开机 58 分钟后发起正常 poweroff，实例 shutdown behavior 已核验为 `stop` |
| S3 | 东京私有桶 `memoverse-media-125647512944-apne1`，Block Public Access、BucketOwnerEnforced、AES256、版本控制已启用 |
| EC2 实例角色 / profile | `memoverse-ec2-app-20261018`，只附着本项目实例；Get/Put 限定桶内 `media/*`、`models/*`，另允许该桶 ListBucket，未授权删除、ACL、其他桶或 `ops/*` 读取 |
| Cognito | `ap-northeast-1_02QmwLnbG`（`memoverse-test-users`），Essentials，小范围测试 MFA OFF；邮箱验证码、短效令牌、刷新轮换与撤销已配置 |
| Cognito 网站客户端 / 登录域名 | `4eitpctiqil0pjgv0no8j1i8if` / `https://keepsite-125647512944.auth.ap-northeast-1.amazoncognito.com`；无客户端密钥，Google 与 COGNITO 已启用 |
| Google 登录项目 | `keepsite-auth-20261018`（MemoVerse Login）；Web OAuth 只回调 Cognito 的 `/oauth2/idpresponse`，应用保持外部测试模式 |

管理端公网 IP 受 EC2 安全组 SSH 规则限制。连接失败先核对当前规则和管理端 IP，不直接放宽到所有来源。账户原有另外两台停止的测试实例不属于本项目，不得顺手启动、改规格或清理。

## 本机与服务器路径

本机路径以项目根目录为基准；这些敏感文件不得加入 Git。

| 用途 | 路径 |
| --- | --- |
| 本机旧业务数据 | `backend/data`、`backend/data-v2` 保留不覆盖；不能直接作为 schema 3 运行目录 |
| 电影票代码默认目录 / 发布候选 | 本机 `backend/data-v3`、服务器候选 `/var/lib/memoverse-v3`，schema 3；本轮尚未部署或迁移真实数据，不能据此推断服务器已有该目录 |
| 最近已部署的账号目录 | `/var/lib/memoverse-v2`，对应下文历史发布；旧 schema 1 目录继续保留 |
| 初始化模板 | 本机 `.cache/initial-template-v1`，服务器 `/opt/memoverse/shared/initial-template-v1`；18 张图、18 个模型，17 收藏、4 分享、6 社区内容 |
| 本机网页 / API | `http://127.0.0.1:4173` / `http://127.0.0.1:8000` |
| SSH 私钥 / 已知主机 | `.cache/server/ssh/id_rsa` / `.cache/server/ssh/known_hosts` |
| SSH 主机身份别名 | `memoverse-i-0e9ba9bc6f4b1c019`；部署传 `-HostKeyAlias`，保持动态 IP 变化时的密钥校验 |
| 站点测试账号与口令 | `.cache/server/access.json`，只记录位置，不读取或输出明文来做一般巡检 |
| Cloudflare DDNS 配置 | 本机 `.cache/server/cloudflare.json`；安装目标 `/etc/memoverse/cloudflare.json`，Token 只允许本域名 DNS 编辑 |
| Google OAuth 凭据 | 本机 `.cache/server/google-oauth.json`，仅本机用户可读；已安全配置到 Cognito，临时 S3 传输副本已按版本删除并核验为空 |
| 新鉴权环境候选 | 本机 `.cache/server/account.env.next`、服务器 `/etc/memoverse.env.next`；含会话加密密钥，不输出或纳入 Git |
| DDNS 服务 / 程序 / 状态 | `memoverse-ddns.service` / `/opt/memoverse/admin/sync-cloudflare-dns.py` / `/var/lib/memoverse-ddns/state.json` |
| 本机模型与推理依赖 | 当前 CPU OCR 固定模型为 `.cache/ocr-models/ppocrv6-tiny`，依赖在 `backend/.venv`；旧 `.cache/models`、`.cache/anyimage`、`.cache/anyimage-deps` 保留 |
| 部署入口 | `scripts/deploy-ubuntu.ps1`，`Server` 必填，默认计划模式，`-Execute` 执行；`-PrepareOnly` 只准备，`-EnvironmentPath /etc/memoverse.env.next` 与 `-NginxPath /etc/nginx/sites-available/memoverse.next` 指定候选配置；新机另加 `-InitializeHost` |
| 一次性数据迁移 | `scripts/package-server-data.py`，SQLite backup API + 文件 SHA-256 清单 |
| Linux 安装 / 模型准备 | `deploy/ubuntu/install.sh` / `scripts/prepare-ocr.py`（默认只显示计划）；不再运行旧 AnyImage 准备链 |
| 发布目录 / 当前链接 | `/opt/memoverse/releases/<完整commit>` / `/opt/memoverse/current` |
| 服务器持久数据（最近已部署版本） | `/var/lib/memoverse-v2`；旧 `/var/lib/memoverse` 保留。源码的 v3 发布候选尚未启用 |
| 服务器共享模型缓存 | `/opt/memoverse/shared/cache` |
| 应用环境 | `/etc/memoverse.env` |
| 应用服务 | `memoverse.service`，单 Uvicorn worker，`127.0.0.1:8000` |
| 自动关机 units | `/etc/systemd/system/memoverse-auto-stop.timer`、`memoverse-auto-stop.service`；源码在 `deploy/ubuntu/`，只启用 timer，普通应用发布不会修改或重置 |
| Nginx 配置 | `/etc/nginx/sites-available/memoverse` |
| 旧 Nginx BasicAuth 哈希 | `/etc/nginx/memoverse.htpasswd`，当前网站使用应用账号登录 |
| 本轮激活回滚元数据 | `/opt/memoverse/activation-backups/activation-VrKJ5JL0`，root-only，保存本次切换前配置、unit 与 release 定位，不是业务数据备份。历史 `activation-m1rFC4fz` 为旧 BasicAuth 阶段、`activation-WKPhdvkQ` 为重复激活、`activation-LJTXEZwD` 为退出修复发布前账号版本，不能混用 |
| TLS 证书与私钥目录 | `/etc/letsencrypt/live/keepsite.space` |
| TLS 配置脚本 | `deploy/ubuntu/enable-https.sh` |
| 日志 | `journalctl -u memoverse`；`/var/log/memoverse/`；本机 `.cache/server/` |

HTTPS 由源站 Nginx 使用 Let's Encrypt 证书提供，`certbot.timer` 续期并重载 Nginx。历史账号迁移阶段先 prepare-only，再以 127.0.0.1:8001 候选验证 Cognito/S3 后正式切换；本轮发布运行观测版本 `2e74f23`，真实 HTTPS 与匿名业务 API 401 通过。当前已停机，网站暂不可访问。旧 BasicAuth 配置保存在历史回滚备份。后续激活仍须先验证匿名拒绝和会话配置；回滚应先停止公共 Nginx、恢复并验证旧访问控制，再启动旧应用。自动 DDNS 只修改指定 A 记录，不擅自改变 Cloudflare 代理模式。

旧运行版本的 SQLite、原图和模型仍完整保留在原目录；新账号版本已使用 SQLite schema 2 与私有 S3，经鉴权后端代理文件访问。模板版本 `efc6dc4631e0466ed4de23463deb73a3b8e3b6f3af2bda57d7bf3e314dc14b5e`，73 个服务器文件逐一 SHA 验证通过（不含 source.sqlite3），权限 root:memoverse、目录 0750 / 文件 0640；18 图 / 18 模型与全部初始内容 ready。真实双账号隔离、私有文件、CPU 生成、备份、退出及刷新轮换均已验证。当前生成流程是官方 AnyImage MoGe2 CPU 推理加项目网格/GLB 导出；不是 Blender Bake Mesh，也不是 Lambda。2 GB 实例配置了 2 GB swap，应用 drop-in 设置 MemoryHigh=1200M、MemoryMax=1500M。

`61e6bd2` 的相同 commit、环境和站点配置重复激活曾返回 `Already active`，当时 MainPID 保持 `2314`。随后 `80d7681` 的真实浏览器 Cognito 退出回站与 Back 匿名保护通过。历史 Google 控制台核验应用为 Testing、测试名单含当前用户账号（个人邮箱不入 Git），当时仅验证授权跳转；用户于 2026-10-18 19:40 明确确认“Google 登录和邮箱收件都已验证”。临时 Cognito QA 账号使用 MessageAction=SUPPRESS，不作为邮件投递证据。历史清理与本轮清理分别记录，详见 `docs/EC2部署验收.md`。

本轮队列实测 processing=1 → ready=1；GLB 3,663,024 bytes，轮询 ready 109.005 秒，JSON 流水线 107.20122 秒包含下载、推理、校验、上传。MemoryPeak=830,328,832 bytes、Swap=0、OOM=0、NRestarts=0；健康公网往返最大 288.203 ms，不能解释为纯 API 耗时或容量上限。清理在应用停止后执行，两个 QA 业务账号及新增 asset/model/job 已删除，其他账号保持 1 user、17 collections、18 assets、18 models、0 jobs，QA 活动 session 为 0，integrity/FK 通过。四个精准 S3 key 的 versions/deleteMarkers 均清空，两个临时 Cognito 身份分别查询为空。共享模板及用户本人账号保留。本机本轮 credentials.json 已删除、验收浏览器页已关闭；没有新增业务数据备份，证据位于 `.cache/operations-acceptance/`。

## 启停与计费决策

- 2026-10-20 起，部署、启动或停止实例只在用户明确要求时执行；开发代理完成测试、提交与 PR 后停止，不自行发布或开机。
- 用户已明确删除 Lightsail 并改用 EC2，选择新建 `t3.small`。2026-10-18 进一步将自动关机从一小时改为每次开机 58 分钟，留出约两分钟正常关机缓冲，但不保证严格在 60 分钟内进入 stopped。用户验收或明确要求时可提前停机；等待验收、继续部署或生成均不能自行延长，延长或取消须取得用户对本次变更的明确授权。
- 定时从系统启动计时，应用重启或发布不重置；系统重新开机重新计时。到点发起关机，服务退出及 AWS 状态转换还需短暂时间。每次开机核验 timer enabled 和剩余时间，维护命令见 `deploy/ubuntu/README.md`；不要在 uptime 超过 58 分钟后无意重新启用 timer，届时会立即触发。
- 发布前始终查询上述明确 instance ID 的状态和当次公网 IP；启动后等待状态检查及 DDNS 就绪。停机后确认 `stopped` 与 DNS 关机清理结果。不要用历史状态或 IP 驱动操作。
- 普通按需 EC2 停止后不再计计算费，自动公网 IPv4 随停止释放；EBS 磁盘继续保留和计费。不得把停机说成完全零费用。
- 2026-10-17 AWS Pricing API 核验：`t3.small` 计算费 `0.0272 USD/小时`，动态公网 IPv4 `0.005 USD/使用小时`，当前 10 GB gp3 磁盘约 `0.96 USD/月`。实例与 IP 运行时合计 `0.0322 USD/小时`；税费、抵扣和额外流量另计。gp3 容量按秒折算、最低 60 秒，不按实际文件占用计费；未来报价重新查询。
- 旧 Lightsail `memoverse-keepsite`、`memoverse-keepsite-ip` 和部署公钥已删除；旧 IP `52.196.70.26` 已释放。新 EC2 已验收迁移完整性；本机 `.cache/migration-lightsail-ec2` 暂存目录删除被自动审批拦截，已交用户手动清理。
- 账户既有 5 USD 月预算提醒不等于金额硬上限，不自动调整预算。

来源：[EC2 状态与计费](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-lifecycle.html)、[公网 IPv4 计费](https://aws.amazon.com/vpc/pricing/)。

## 现有约束与文档

- 2026-10-23 GitHub API 核验：可靠性 PR #1、观测能力 PR #2 与电影票 Web PR #4 已合并。旧浮雕 PR #3 已关闭但未合并，代码以归档标签保留；其历史检查结果不用于验收新版。

- 2026-10-18 合并流程核验：`main` 已包含 PR #1 的解耦、可靠性修复及前后端 CI。当时 PR 和 main push 自动运行 `Backend tests`、`Frontend tests` 与 `deployment-recovery`；2026-10-22 的 M2 同时将分支触发扩至 `codex/**`，`f1e2039` 的三项检查均成功。Codex 仓库自动代码审查已设置为“所有 PR”“每次推送时”；这不自动批准或合并 PR。
- 同日读取 GitHub 设置：当前私有仓库没有 classic branch protection 或 ruleset；两类设置页面均提示当前套餐不会强制执行私有仓库保护规则。未创建无效规则，未改变仓库可见性或账户套餐。因此当前是代理遵循的合并检查流程，不能宣称平台已强制拦截；后续套餐或配置变化须重新核验。
- 根 `AGENTS.md` 与 `demo/AGENTS.md`：一般工作规则、响应式 Web 运行契约和账号隔离要求。
- `docs/项目定义与决策主文档.md`：当前电影票产品规格；`docs/开发计划.md`：实施工作包与验收顺序。旧产品文档、原始研究、五张设计图及旧 API 说明已按用户授权清理，通过改版前 Git 检查点 `d7945d8` 追溯。
- `deploy/ubuntu/README.md`：安装、发布与恢复流程。
- Lightsail 退役证据保存在上述 Git 检查点；该环境已退役，不能用于定位当前实例。
- `docs/EC2部署验收.md` 与 `docs/可靠性与解耦验收.md`：保留旧实现的运行/测试证据与历史时点，不证明本次新功能已经实现或上线。
- 通用 Ubuntu 发布脚本会核验制品哈希与准备状态后复用同一 commit；未知目录或内容冲突停止，不自动删除。激活失败后依旧要核对回滚输出与真实运行状态，不能将尝试回滚等同成功。
- 当前没有统一 formatter。遵循现有风格，不放宽 Web 运行契约或扩散到无关代码。
- 2026-10-18 最新阶段已发布运行观测版本，19:40 用户明确确认 Google 登录和邮箱收件后完成精确 QA 清理，19:46 核验 stopped。58 分钟 timer 全程未改或延长；本轮未新增业务数据备份。历史阶段的 22 + 6 项、9 个模板合成测试目录以及 7 个认证/后端测试目录删除被自动审批拒绝，未绕过；该历史待清理记录不等于本轮 QA 未清理，清单集中在 `docs/EC2部署验收.md`。保留业务数据、模型、模板、正式凭据、当前准确发布制品、配置回滚元数据和验收证据。文档更新无需再次发布应用。
- DDNS 限定一个实例管理一个域名。正常服务停止会撤回自己确认过的 A 记录；强制关机、Token 失效或并发改 DNS 时不保证撤回，停机后应读回确认。2026-10-18 换盘后已验证新公网 IP 发布，以及最终真实 EC2 停机后 A 记录归零。
