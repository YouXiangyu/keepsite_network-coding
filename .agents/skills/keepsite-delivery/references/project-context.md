# keepsite 项目运行信息
## 2026-10-25 千问识别配置与独立联调（未发布应用代码）

用户明确以本人北京百炼工作空间的 `qwen3.7-flash` 替换本地 OCR（D-081），并授权将提供的 Key 配置到本机与 EC2。Key 仅保存于本机 `.cache/server/keepsite-qwen.env`（当前用户与 SYSTEM 访问，Git 忽略）及服务器 `/etc/keepsite.env`（root:root 0600），不记录值或哈希。配置项为 `KEEPSITE_QWEN_API_KEY`、`KEEPSITE_QWEN_BASE_URL`、`KEEPSITE_QWEN_MODEL`；本机文件按后端 README 显式加载到进程，不全局写入注册表。浏览器核对了官方模型页和工作空间调用示例，保留原“免费额度用完即停”设置。

本机手工兼容接口、源码识别适配器的 45 秒隔离进程、EC2 keepsite 服务身份均完成一次合成票面五字段调用验证；这些是单样本连通与结构验证，不是实际票据准确率或性能基准。安全明细在本机 `output/qwen-recognition-2026-10-25/`。服务器测试临时客户端已移除，没有写业务库、上传用户照片或更改 S3/Cognito/TMDB 配置。

本次仅为配置维护启动 `i-0e9ba9bc6f4b1c019`，状态检查 2/2 通过，核验时 IP 为 `13.114.44.30`。`keepsite-auto-stop.timer` enabled/active，仍为 OnBootSec=58min，本次到期为北京时间 2026-10-25 19:03:16，未延长或重置；实例 shutdown behavior 已核对为 stop。后续状态和 IP 必须重新查询。

实际 `/opt/keepsite/current` 仍指向 `810f900cd6f3d664cf96af3095b44a5bbea197ff`，没有切换应用发布链接或手动重启应用服务。千问新代码须完成独立审查、测试、提交及后续明确授权发布后才进入生产流水线；不能把凭据已配置写成线上 OCR 已替换。旧 CPU 模型、依赖副本、数据库及回滚发布均保留。

## 2026-10-25 V0 基线与测试 EC2 清理

用户确认当前版本为 V0。附注标签 `v0` 已推送并读回核对，指向 `main@2cb7b06b73cc4adbb6a42320872b022635489cec`；后续修复从该基线继续。

用户明确确认两台旧测试实例及各自根磁盘均无需保留数据，允许永久删除、不创建快照。东京账号 `125647512944` 下的 `i-0f636c3e4868d24d7`（t3.nano，名称含 test）和 `i-06f3d9b03ffbf42aa`（未命名 t3.micro）已终止；读回均为 `terminated`。两块随实例删除的 8 GiB gp3 卷 `vol-07f1c7efbe59aaf5d`、`vol-081d1d6d77d3f04db` 按 volume-id 过滤查询结果为空。执行前核对账号、停止状态、无实例角色、准确卷映射、DeleteOnTermination 和无 Auto Scaling 归属；没有扩展删除安全组、密钥或其他云资源。

keepsite 的 `i-0e9ba9bc6f4b1c019` 与 10 GiB 卷 `vol-0bb6aa01805d28c75` 保留，清理后实例读回为 `stopped`。本次未启动、部署或更改其 58 分钟自动关机策略，也未改 S3、Cognito 或业务数据。清理明细保存在本机 `output/aws-test-cleanup-2026-10-25/`，不纳入 Git。

当日 AWS Pricing API 核实东京 gp3 容量价为 `0.096 USD/GB-month`。删除 16 GiB 测试磁盘预计减少抵扣前容量费用 `1.536 USD/月`，keepsite 10 GiB 约 `0.96 USD/月`；实际按持有时间、抵扣和税费结算。该估算不是新增收费或全部云资源月费用承诺。

## 2026-10-25 阶段收尾与用户试用反馈

当前进入主要开发完成后的测试、debug 与修复阶段，不把这一结论等同于所有文档验收项完成。2026-10-24 后续已在连接的手机安装 `io.github.youxiangyu.keepsite`，确认前台为新包，票夹及《流浪地球2》海报正常显示；旧应用及数据保留。2026-10-25 用户反馈对两台手机简单使用后感觉没有明显问题，记录为用户简单试用反馈，不据此宣称并发修改、断网重连、跨网络和全部双真机矩阵已完成。

下节是 2026-10-24 发布当时的核验记录，其中运行状态、IP、剩余时间和待验收事项均有时间范围，不代表当前实时状态。此次 Git 收尾不启动、停止或部署服务器，不修改密钥或用户数据；正式签名与升级分发、完整边界场景及长期可用性安排仍按后续需要处理。
## 2026-10-24 最新主线发布与实际迁移

用户明确授权合并 PR #6 并发布最新 main。最新 head `f55dc6d` 的 Backend、Frontend、Android、deployment-recovery 均成功（一次 PowerShell 测试超时已定点重跑通过），Codex 对同一 head 完成审查且无发现。合并后的 `main@810f900cd6f3d664cf96af3095b44a5bbea197ff` 已正式运行；`current` 与实际 MainPID cwd 核验一致，重启次数 0。

本次实例 `i-0e9ba9bc6f4b1c019` 已开机，公网 IP `52.196.89.220`；这是本次核验值，后续操作仍须实时查询。`keepsite.service`、Nginx、Redis、`keepsite-ddns.service` 均 active。新 `keepsite-auto-stop.timer` 已启用并核对与旧 timer 完全相同的本次开机期限：北京时间 2026-10-24 23:53:26，未延长或重置。旧应用、DDNS 与旧 timer 已停用自启动。

实际运行目录已切为 `/opt/keepsite/current`、`/var/lib/keepsite-v3` 与 `/etc/keepsite.env`。原 `/var/lib/memoverse-v3`、原发布与配置保留。迁移前的 1 个账户、1 张票据及原图引用核对保留，S3 原对象通过校验，认证库完整迁移，账户 ID 未变化。备份位于 `/opt/memoverse/predeploy-backups/keepsite-810f900-20261024T1455`，激活回滚记录位于 `/opt/keepsite/activation-backups/activation-Xbu3i4ae`。新系统接受写入后不得直接回退到过时的旧数据库。

TMDB 只读令牌已从本机 `.cache/server/keepsite-tmdb.env` 安全配置到服务器 `/etc/keepsite.env`（root:root 0600），实际进程已确认启用 TMDB。以 keepsite 服务身份完成真实电影检索和 780×1170 海报下载/解码、OCR 模型检查、Redis ping 及原图对象校验；未把 AWS 本机 root 登录凭据上传到服务器，应用继续使用实例角色。Cognito/S3/Cloudflare 原凭据及资源 ID 保留，未创建或重命名云资源。

源站和服务器公网 HTTPS 200、证书校验通过、HTTP 跳转正常；匿名 session 显示邮箱/Google 均启用，票夹及账户绑定接口拒绝匿名访问。实际用户登录、双登录方式绑定和旧票据重新处理仍待用户验收。手机尚未安装新包，旧包使用旧协议，测试新版原生功能需要安装 keepsite 包；保留旧应用和未同步草稿。候选端口已关闭，本次候选数据副本及上传临时密钥已清理；正式数据、模型、release 和回滚备份保留。机器剩余磁盘约 1.4 GB。

本机详细发布证据 `.cache/server/deploy-keepsite-20261024.json`；以下旧名称与状态是历史定位，不得替代本节与实时核验。
## 2026-10-24 改名前的迁移计划（历史）

用户明确要求软件及技术标识统一为小写 `keepsite`，并确认执行显式完整迁移。当前源码目标：Android `io.github.youxiangyu.keepsite`、`KEEPSITE_*` 环境变量、`keepsite.sqlite3`、`keepsite.service`、`keepsite-auto-stop.timer`、`/opt/keepsite`、`/var/lib/keepsite-v3`、`/etc/keepsite.env`。数据复制与验证工具为 `scripts/migrate-keepsite.py`，迁移步骤见 `docs/keepsite迁移.md`。

本节记录改名当时的计划。2026-10-24 晚间的实际切换见上文；原账户 ID 和 S3 对象归属没有因为改名而变化。下面旧名称、资源 ID、桶名、路径及验收记录用于定位真实旧环境与回滚，保留原文；禁止把旧资源值直接字符串替换后当成真实云资源。

## 改名前的历史运行记录

2026-10-24 视觉第二轮：Web `e6f414a`、Android `ce37eb1` 完成参考图的真错列、紧凑层次及搜索/排序，新增失败图片刷新和对比度回归。88 Web单测、56浏览器（含本机参考素材）、34 Android JVM、真机5项UI通过，Lint 0错误/13警告。Sol实现/独立审查，Astra定位并修正测试自动填充弹窗和旋转同步问题，未改生产权限或密码库。证据 [历史 visual-redesign-20261024.json](https://github.com/YouXiangyu/network-coding/blob/d23f04d886d7cd656c542c3f4bd69826906c8c55/docs/evidence/visual-redesign-20261024.json)，最新版包及截图 `.cache/visual-redesign-20261024/`。用户授权参考裁图仅用于隔离夹具，不提交素材、不改真实账号。延续PR #5和旧提交，不合并/部署。为用户查看保留本机4180视觉夹具及当前手机ADB reverse（仅本机预览、模拟身份/存储）；看完后可按 `.cache/m7-native-control.json` 的本轮归属正常关闭，不能把预览当生产服务。以下为此前交付记录。

最近更新：2026-10-24，北京时间。AWS CLI 重新登录并确认项目 EC2 stopped、无公网 IP；S3 版本控制与阻止公开访问启用、无 CORS，Cognito Google/COGNITO 与既有回调配置存在。只对一枚随机合成 S3 对象进行创建、校验和精准版本清理，未改云配置、未启动或部署。下方 2026-10-18 运行信息为历史定位，不能替代操作前查询。

2026-10-24 移动端阶段：Web 视觉改版 `7af2946`、传输完成确认修复 `dded2c1`；Android 两列票夹/图标底栏、360dp 输入框滚动坐标修复和明确选择真机的互通工具已实现。Web 最终 88 单测、54 浏览器、7 双浏览器通过；Android 34 JVM、Lint 0错误/10警告、模拟器三档9次UI、模拟器/单台真机CRUD及JNI双向传图通过。真机完整UI仪器测试最终3/3通过：Sol高思考子代理定位系统START_ABORTED，并仅在测试launch期间临时采用后台启动权限，finally释放，API29判断保留；生产代码及手机长期权限未改变。原断言、旋转与后台恢复均通过，独立审查通过，无需Astra。默认HTTPS debug APK与loopback制品在 `.cache/mobile-delivery-20261024/`，不是正式签名或上线验收。完整范围见 [历史 mobile-delivery-20261024.json](https://github.com/YouXiangyu/network-coding/blob/d23f04d886d7cd656c542c3f4bd69826906c8c55/docs/evidence/mobile-delivery-20261024.json)。用户已明确允许代理在当前手机安装和交互，第二台真机及最终体验确认尚未完成；历史“由用户执行”不限制本次已授权单台自动验证。`docs/UI_example/` 和 `output/` 保留且不入提交。以下2026-10-23段落为历史检查点。

2026-10-23 Git 交付整理：Web PR #4 已按用户明确授权以保留历史的 merge commit `6126bf50e5b2bf96e3bef1d2e842328cb7d7e0a8` 合入 `main`，本地与 GitHub 主线已读回核对。合并前最新 head `2ebd2b09959280c5d721e9a1503a6d1d392800c0` 的 `Backend tests`、`Frontend tests`、`deployment-recovery` 均成功，Codex 对该提交的审查已完成且无未解决问题。旧海报残留、恢复保护标记与迁移并发覆盖三项修复均有回归测试；最新 Linux 后端 CI 为 477 项及 11 子测试通过、无跳过。本次主线交付范围为文档与 Web M1—M6，包含 300 条记录的 540 个主读取任务及 20 张合成票据的真实 OCR 流水线。已验证文档分支 `docs/spec-and-dev-plan@df53192` 与 Web head 均为主线祖先，随后删除这两个本地/远端分支并清理专用测试工作树。旧浮雕分支的 10 个独有提交按用户另行授权保存在已推送标签 `archive/subject-relief-prototype-20261023`（指向 `0594620`），PR #3 已关闭、分支已删除，未合入新版。

原生 Android 工作继续保存在独立 `codex/android-native-progress` 的 M7 WIP 检查点，本次同步已合入主线的审查修复，不作为已验收交付。此前两个 4180 测试 APK、34 项 JVM 测试与 Lint 通过；后台闲置连接 EOF 已修复并通过相同 6000ms 场景复测。完整 UI 矩阵、原生 CRUD、Android↔Web 双向传图、默认 HTTPS 验收包和两台真机验收尚未完成。全量本地后端 472 项及 11 子测试通过、5 项明确跳过；随后测试夹具定点 4 项通过。准确边界见开发计划、Android README 与 [历史 android-m7-checkpoint-20261023.json](https://github.com/YouXiangyu/network-coding/blob/d23f04d886d7cd656c542c3f4bd69826906c8c55/docs/evidence/android-m7-checkpoint-20261023.json)。`a2cb349` 的 Android、后端及部署恢复 CI 通过，前端任务因依赖准备耗时而触及 20 分钟上限，未报告为通过；同步后的验证以进度分支最新提交为准，Web PR 的检查不能代替 M7 验收。本次 Git 整理不恢复大规模功能联调；临时模拟器、本机夹具和自建 ADB 已关闭，个人新增的 `docs/UI_example/` 和既有 `output/pdf/` 保留、不夹带进代码提交。

产品方向仍为私有 S3 长期保存原票与海报、账号缓存、P2P 加速和服务器回退，服务端使用轻量 CPU OCR。以上本机结果使用合成数据、fakeredis 与本机/内存对象存储，不证明真实 S3、TURN、Cognito、跨网络或物理设备验收；免费 TURN 保持关闭。这些改动均未发布到 EC2。下列云资源位置仅供后续明确授权操作定位；旧模型、用户数据和回滚文件不得因文档或分支整理清理。EC2 非常驻时的信令与鉴权可用性仍按用户要求暂缓决定，本次不改变 58 分钟定时器、不启动或部署云资源。

## 位置与入口

下表是 2026-10-24 发布后的定位。实例是否仍在运行、当次公网 IP 必须现场查询，不能把表中的历史地址当成当前地址。云资源显示名没有随软件改名。

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
| 最近运行状态 | 2026-10-25 千问配置维护时 running，状态检查 2/2 通过；自动关机到期北京时间 19:03:16。仅代表本次核验，操作前重新查询 |
| 最近应用运行提交 | `810f900cd6f3d664cf96af3095b44a5bbea197ff`，`/opt/keepsite/current` 与当时 MainPID 的 cwd 一致。此前线上观测版本是 `2e74f23408afb26410c45b6c86515cd7964c3f13` |
| 公网 IPv4 | 2026-10-25 配置核验为 `13.114.44.30`；2026-10-24 的 `52.196.89.220` 及其他旧地址仅供历史定位。动态地址在停机后释放，每次启动重新查询 |
| Launch template / 新建默认版本 | `lt-016bca8507ac81480` / `2`，根盘默认 10 GB |
| 安全组 / SSH key pair | `sg-0f472ac3891c65681` / `memoverse-ec2-20261017` |
| SSH 用户 | `ubuntu` |
| DNS | Cloudflare。2026-10-24 起由 `keepsite-ddns.service` 更新 A 记录，正常关闭时撤回；旧 `memoverse-ddns.service` 已停用自启动。运行时 DNS-only、TTL 60。停机后的记录须读回确认 |
| 自动关机 | `keepsite-auto-stop.timer` 已启用，`OnBootSec=58min`、`AccuracySec=1s`；旧 timer 已停用自启动。到点发起正常 poweroff，实例 shutdown behavior 已核验为 `stop` |
| S3 | 东京私有桶 `memoverse-media-125647512944-apne1`，Block Public Access、BucketOwnerEnforced、AES256、版本控制已启用 |
| EC2 实例角色 / profile | `memoverse-ec2-app-20261018`，只附着本项目实例；Get/Put 限定桶内 `media/*`、`models/*`，另允许该桶 ListBucket，未授权删除、ACL、其他桶或 `ops/*` 读取 |
| Cognito | `ap-northeast-1_02QmwLnbG`（`memoverse-test-users`），Essentials，小范围测试 MFA OFF；邮箱验证码、短效令牌、刷新轮换与撤销已配置 |
| Cognito 网站客户端 / 登录域名 | `4eitpctiqil0pjgv0no8j1i8if` / `https://keepsite-125647512944.auth.ap-northeast-1.amazoncognito.com`；无客户端密钥，Google 与 COGNITO 已启用 |
| Google 登录项目 | `keepsite-auth-20261018`。控制台显示名仍是 `MemoVerse Login`，本次没有修改 Google 品牌。Web OAuth 只回调 Cognito 的 `/oauth2/idpresponse`，应用保持外部测试模式 |

管理端公网 IP 受 EC2 安全组 SSH 规则限制。连接失败先核对当前规则和管理端 IP，不直接放宽到所有来源。账户原有另外两台测试实例已按上方 2026-10-25 用户授权清理记录终止；该次授权不扩展到 keepsite 或其他资源。

## 本机与服务器路径

本机路径以项目根目录为基准；这些敏感文件不得加入 Git。

| 用途 | 路径 |
| --- | --- |
| 本机旧业务数据 | `backend/data`、`backend/data-v2` 保留不覆盖；不能直接作为 schema 3 运行目录 |
| 电影票代码默认目录 / 已发布数据 | 本机 `backend/data-v3`；2026-10-24 起服务器为 `/var/lib/keepsite-v3`，schema 3。旧 `/var/lib/memoverse-v3` 保留，不覆盖 |
| 最近已部署的账号目录 | `/var/lib/keepsite-v3`。旧 `/var/lib/memoverse-v2` 与 schema 1 的 `/var/lib/memoverse` 保留 |
| 初始化模板 | 本机 `.cache/initial-template-v1`。旧服务器模板 `/opt/memoverse/shared/initial-template-v1` 保留用于历史回滚；新版账号从空票夹开始，不使用该模板 |
| 本机网页 / API | `http://127.0.0.1:4173` / `http://127.0.0.1:8000` |
| SSH 私钥 / 已知主机 | `.cache/server/ssh/id_rsa` / `.cache/server/ssh/known_hosts` |
| SSH 主机身份别名 | 当前部署使用 `keepsite-i-0e9ba9bc6f4b1c019`。旧别名 `memoverse-i-0e9ba9bc6f4b1c019` 对应同一实例密钥，不得为改名关闭严格校验 |
| 站点测试账号与口令 | `.cache/server/access.json`，只记录位置，不读取或输出明文来做一般巡检 |
| Cloudflare DDNS 配置 | 本机 `.cache/server/cloudflare.json`；当前服务读取 `/etc/keepsite/cloudflare.json`。旧安装目标 `/etc/memoverse/cloudflare.json` 保留。Token 只允许本域名 DNS 编辑 |
| Google OAuth 凭据 | 本机 `.cache/server/google-oauth.json`，仅本机用户可读；已安全配置到 Cognito，临时 S3 传输副本已按版本删除并核验为空 |
| 应用环境 | 正式文件 `/etc/keepsite.env`；候选 `/etc/keepsite.env.next`。本机候选 `.cache/server/account.env.next`。含会话加密密钥，不输出或纳入 Git。旧 `/etc/memoverse.env` 保留 |
| DDNS 服务 / 程序 / 状态 | `keepsite-ddns.service` / `/opt/keepsite/admin/sync-cloudflare-dns.py` / `/var/lib/keepsite-ddns/state.json`。旧 `memoverse-ddns.service` 与 `/var/lib/memoverse-ddns/state.json` 保留 |
| 本机识别配置与历史模型 | 新源码使用 `.cache/server/keepsite-qwen.env`，依赖在 `backend/.venv`；已退役 CPU 模型 `.cache/ocr-models/ppocrv6-tiny`、旧 `.cache/models`、`.cache/anyimage`、`.cache/anyimage-deps` 保留，不作为新发布运行前置 |
| 部署入口 | `scripts/deploy-ubuntu.ps1`，`Server` 必填，默认计划模式，`-Execute` 执行；`-PrepareOnly` 只准备，`-EnvironmentPath /etc/keepsite.env.next` 与 `-NginxPath /etc/nginx/sites-available/keepsite.next` 指定候选配置；新机另加 `-InitializeHost` |
| 一次性数据迁移 | `scripts/package-server-data.py`，SQLite backup API + 文件 SHA-256 清单 |
| Linux 安装 / 识别配置 | `deploy/ubuntu/install.sh` 仅安装基本依赖；千问三项环境由受限文件单独配置，不自动写入凭据。新运行链不再准备 CPU/AnyImage 模型，旧生产版本事实见上文 |
| 发布目录 / 当前链接 | `/opt/keepsite/releases/<完整commit>` / `/opt/keepsite/current`。旧 `/opt/memoverse/current` 保留到切换前 |
| 服务器持久数据（最近已部署版本） | `/var/lib/keepsite-v3`。旧 `/var/lib/memoverse-v2` 与 `/var/lib/memoverse` 保留 |
| 服务器共享模型缓存 | `/opt/keepsite/shared/cache`。旧 `/opt/memoverse/shared/cache` 保留，新版不覆盖旧模型目录 |
| 应用服务 | `keepsite.service`，单 Uvicorn worker，`127.0.0.1:8000`。旧 `memoverse.service` 已停用自启动 |
| 自动关机 units | `/etc/systemd/system/keepsite-auto-stop.timer`、`keepsite-auto-stop.service`；源码在 `deploy/ubuntu/`，只启用 timer，普通应用发布不会修改或重置。旧 timer 已停用自启动 |
| Nginx 配置 | `/etc/nginx/sites-available/keepsite`。旧 `/etc/nginx/sites-available/memoverse` 与启用入口保留到回滚所需的范围 |
| 旧 Nginx BasicAuth 哈希 | `/etc/nginx/memoverse.htpasswd`，当前网站使用应用账号登录 |
| 本次激活回滚元数据 | `/opt/keepsite/activation-backups/activation-Xbu3i4ae`，root-only。发布前备份位于 `/opt/memoverse/predeploy-backups/keepsite-810f900-20261024T1455`。更早的 `activation-VrKJ5JL0`、`activation-m1rFC4fz`、`activation-WKPhdvkQ`、`activation-LJTXEZwD` 属于旧名称阶段，不能混用 |
| TLS 证书与私钥目录 | `/etc/letsencrypt/live/keepsite.space` |
| TLS 配置脚本 | `deploy/ubuntu/enable-https.sh` |
| 日志 | `journalctl -u keepsite`；`/var/log/keepsite/`；本机 `.cache/server/`。旧 `/var/log/memoverse/` 保留 |

## 2026-10-18 及更早的运行记录

以下段落保留改名和本次发布之前的原始记录。其中的服务名、路径和“当前已停机”都是当时事实。

HTTPS 由源站 Nginx 使用 Let's Encrypt 证书提供，`certbot.timer` 续期并重载 Nginx。历史账号迁移阶段先 prepare-only，再以 127.0.0.1:8001 候选验证 Cognito/S3 后正式切换；本轮发布运行观测版本 `2e74f23`，真实 HTTPS 与匿名业务 API 401 通过。当前已停机，网站暂不可访问。旧 BasicAuth 配置保存在历史回滚备份。后续激活仍须先验证匿名拒绝和会话配置；回滚应先停止公共 Nginx、恢复并验证旧访问控制，再启动旧应用。自动 DDNS 只修改指定 A 记录，不擅自改变 Cloudflare 代理模式。

旧运行版本的 SQLite、原图和模型仍完整保留在原目录；新账号版本已使用 SQLite schema 2 与私有 S3，经鉴权后端代理文件访问。模板版本 `efc6dc4631e0466ed4de23463deb73a3b8e3b6f3af2bda57d7bf3e314dc14b5e`，73 个服务器文件逐一 SHA 验证通过（不含 source.sqlite3），权限 root:memoverse、目录 0750 / 文件 0640；18 图 / 18 模型与全部初始内容 ready。真实双账号隔离、私有文件、CPU 生成、备份、退出及刷新轮换均已验证。当前生成流程是官方 AnyImage MoGe2 CPU 推理加项目网格/GLB 导出；不是 Blender Bake Mesh，也不是 Lambda。2 GB 实例配置了 2 GB swap，应用 drop-in 设置 MemoryHigh=1200M、MemoryMax=1500M。

`61e6bd2` 的相同 commit、环境和站点配置重复激活曾返回 `Already active`，当时 MainPID 保持 `2314`。随后 `80d7681` 的真实浏览器 Cognito 退出回站与 Back 匿名保护通过。历史 Google 控制台核验应用为 Testing、测试名单含当前用户账号（个人邮箱不入 Git），当时仅验证授权跳转；用户于 2026-10-18 19:40 明确确认“Google 登录和邮箱收件都已验证”。临时 Cognito QA 账号使用 MessageAction=SUPPRESS，不作为邮件投递证据。历史清理与本轮清理分别记录，详见 [历史 EC2部署验收.md](https://github.com/YouXiangyu/network-coding/blob/d23f04d886d7cd656c542c3f4bd69826906c8c55/docs/EC2部署验收.md)。

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
- [历史 EC2部署验收.md](https://github.com/YouXiangyu/network-coding/blob/d23f04d886d7cd656c542c3f4bd69826906c8c55/docs/EC2部署验收.md) 与 [历史 可靠性与解耦验收.md](https://github.com/YouXiangyu/network-coding/blob/d23f04d886d7cd656c542c3f4bd69826906c8c55/docs/可靠性与解耦验收.md)：保留旧实现的运行/测试证据与历史时点，不证明本次新功能已经实现或上线。
- 通用 Ubuntu 发布脚本会核验制品哈希与准备状态后复用同一 commit；未知目录或内容冲突停止，不自动删除。激活失败后依旧要核对回滚输出与真实运行状态，不能将尝试回滚等同成功。
- 当前没有统一 formatter。遵循现有风格，不放宽 Web 运行契约或扩散到无关代码。
- 2026-10-18 最新阶段已发布运行观测版本，19:40 用户明确确认 Google 登录和邮箱收件后完成精确 QA 清理，19:46 核验 stopped。58 分钟 timer 全程未改或延长；本轮未新增业务数据备份。历史阶段的 22 + 6 项、9 个模板合成测试目录以及 7 个认证/后端测试目录删除被自动审批拒绝，未绕过；该历史待清理记录不等于本轮 QA 未清理，清单集中在 [历史 EC2部署验收.md](https://github.com/YouXiangyu/network-coding/blob/d23f04d886d7cd656c542c3f4bd69826906c8c55/docs/EC2部署验收.md)。保留业务数据、模型、模板、正式凭据、当前准确发布制品、配置回滚元数据和验收证据。文档更新无需再次发布应用。
- DDNS 限定一个实例管理一个域名。正常服务停止会撤回自己确认过的 A 记录；强制关机、Token 失效或并发改 DNS 时不保证撤回，停机后应读回确认。2026-10-18 换盘后已验证新公网 IP 发布，以及最终真实 EC2 停机后 A 记录归零。
