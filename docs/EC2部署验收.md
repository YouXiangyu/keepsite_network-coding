# EC2 部署验收

验收日期：2026-10-18，北京时间。最近应用运行提交：`2e74f23408afb26410c45b6c86515cd7964c3f13`；主机自动关机配置仍为 `622486b`（58 分钟），本轮未修改或延长。该次验收时 PR #2 尚待合并、`main` 为 `e0c5085`；2026-10-19 02:57 已合并为 `eb486c9`。后续 Git 核验不代表重新发布或云资源复查，运行提交与默认分支分别记录。

当前状态：用户 19:40 确认 Google 登录和邮箱收件均已验证，完成本轮 QA 精确清理后，19:45 正常 StopInstances 提前停机。19:46 AWS 核验 EC2 `i-0e9ba9bc6f4b1c019` 为 `stopped`、无公网 IP；公共 DNS DoH 查询没有 A 回答，本次未用 Cloudflare API 查询记录数量。网站暂不可访问，业务数据、S3 和账号配置保留。下文 active 等状态均对应各自验收时点。

## 19:27—19:46：运行观测版本线上验收（最新）

19:27 启动上述实例，本次动态公网 IP 为 `54.150.236.186`，停机后已释放。全程核查 `memoverse-auto-stop.timer` 为 58 分钟、enabled/active，未更改、延长或取消。发布后 `/opt/memoverse/current` 和 MainPID `1451` 的 cwd 均指向 `2e74f23408afb26410c45b6c86515cd7964c3f13`。配置、unit 与链接的回滚元数据保存在 `/opt/memoverse/activation-backups/activation-VrKJ5JL0`；这不是新增业务数据备份，本轮没有执行业务数据备份。

临时 A/B 账号的真实 HTTPS 验收通过：匿名业务访问 401、Secure/HttpOnly 会话 cookie、原图 SHA、GLB 及跨账号图片/模型/任务 404。临时账号使用 `MessageAction=SUPPRESS`，不作为邮件送达证据。用户于 19:40 明确确认“Google 登录和邮箱收件都已验证”，这是本人交互验收，与脚本测试证据分别记录。初次 QA 输入将 Cognito raw sub 当作应用内部 ID，改为 issuer + NUL + sub 的 SHA-256 后通过；属于测试输入纠正，没有因此修改应用。

本次单任务 GLB 为 3,663,024 bytes，轮询首次观察到 ready 为 109.005 秒；结构化日志的 `pipelineDurationSeconds=107.20122` 包括下载、推理、校验及上传，不是纯推理耗时。队列 CLI 实际观察到 processing=1 → ready=1。MemoryPeak=830,328,832 bytes、Swap=0、OOM=0、NRestarts=0；健康请求最大往返 288.203 ms 包含公网网络，不等于纯 API 处理耗时。该单样例不构成并发容量或最大吞吐保证。

脚本删除本轮收藏并退出两个 QA 账号；随后停止应用，防止新登录和 worker 与清理竞态。SQL dry-run 与精确 apply 清除两个 QA 业务账号及其授权、一个新增 asset/model/job，其他账号数据保持为 1 user、17 collections、18 assets、18 models、0 jobs；QA auth 活动 session 为 0，integrity/FK 检查通过。仅对四个本轮 S3 key 精确删除版本，读回 versions/deleteMarkers 均为空；删除两个临时 Cognito 身份后，针对各目标的 ListUsers 查询为空，未删除用户本人账号。共享模板及既有用户数据保留。

本机本轮 `credentials.json` 已删除，验收浏览器页已关闭。证据集中在 `.cache/operations-acceptance/`：`deploy-2e74f23.log`、`online-2e74f23-02.json`、`queue-during-generation.json`、`metrics-after-generation.json`、`cleanup-business-plan.json`、`cleanup-business-result.json`、`cloud-cleanup.json`、`before-stop.json`、`live-after-deploy.jpg`。本次仅补记文档无需再次发布应用；合并仍按用户授权和最新 CI/审查流程处理，不宣称私有仓库套餐已强制实施合并门禁。

## 历史：用户测试后停机与自动关机配置

### 58 分钟配置（当前）

为给正常关机预留约两分钟，用户要求将 timer 改为 `OnBootSec=58min`。短暂维护开机后的 IP 为 `43.207.114.190`；修改前先确认此前安装的 timer 已随这次系统启动自动进入 enabled/active。只更新 timer 的说明和时长，service 的正常 poweroff 路径保持不变。

新配置经独立复核、服务器 `systemd-analyze verify` 检查和源码/安装文件 SHA-256 对照通过。daemon-reload 并重启 timer 后，`TimersMonotonic` 和 `NextElapseUSecMonotonic` 均为 58min，AccuracyUSec=1s，仍 enabled/active；uptime 44.55 秒，计划触发时间为当日 13:15:04 北京时间。这次没有等待 58 分钟；确认没有生成或传输任务后正常 StopInstances，12:19 读回 stopped、IP 已释放，DNS A 为零。临时上传文件已删除，记录见 `.cache/server/auto-stop-58-install.log`、`auto-stop-58-acceptance.json`。

58 分钟是发起正常关机的时点；约两分钟缓冲不构成严格在 60 分钟内到达 stopped 的保证。下列一小时设置保留为历史证据，不再是当前配置。

### 原一小时配置（历史）

首次检查没有待处理生成任务或传输进程，以正常 `StopInstances` 停机，确认 stopped、动态 IP `13.230.191.15` 释放、DNS A 为零。随后按用户新增要求短暂维护开机，读取新 IP `18.181.150.249`，严格校验既有 SSH host key，安装已审查的 `memoverse-auto-stop.timer` 和同名 service，之后恢复关机。

定时器启用在 `timers.target`，`OnBootSec=1h`、`AccuracySec=1s`，触发 `/usr/bin/systemctl poweroff`。AWS `InstanceInitiatedShutdownBehavior` 两次读回均为 `stop`。服务器 `systemd-analyze verify` 通过，两个已安装文件的 SHA-256 与提交源码一致；`is-enabled` 返回 enabled、timer active、`TimersMonotonic` 与 `NextElapseUSecMonotonic` 均显示开机后 1h。当时 uptime 76.49 秒，下一次计划为 2026-10-18 13:09:21 北京时间，约剩 58 分钟。

没有等待完整一小时：手动启动同一个关机 service 验证执行链。最终 AWS 状态为 stopped，原因 `Client.InstanceInitiatedShutdown`，维护 IP 已释放；Cloudflare A 再次为零。10 GiB 根卷 `vol-0bb6aa01805d28c75` 仍为加密、in-use。一次性 unit 上传文件已删除，证据保留在 `.cache/server/auto-stop-install.log` 和 `auto-stop-acceptance.json`。

此设置不依赖 Codex 在线或 AWS 临时登录有效。每次系统开机重新计时，普通应用重启、发布和等待验收不暂停或重置倒计时；一小时到点发起正常关机，实例进入 stopped 还需服务退出时间。需要延长时必须事先获得用户明确授权。用户本次只表示简单测试，不能据此补记 Google 本人授权或真实邮件投递已专项验收。

## 历史：Web / 账号 / 私有 S3 发布验收

该历史阶段基础提交：`95cae17` 初始化模板、`96e23d2` 账号后端、`7535cfd` 响应式 Web、`61e6bd2` 安全部署。先以 `-PrepareOnly` 准备准确制品，再在 `127.0.0.1:8001` 候选服务验证真实 Cognito 与 S3；停止候选后正式激活。随后发布退出修复 `80d7681`，当时 `/opt/memoverse/current` 读回指向 `80d7681556823f81372b9c6cc5d32450ddc7961f`；该次切换备份为 `/opt/memoverse/activation-backups/activation-LJTXEZwD`。

此前 `61e6bd2` 阶段，服务器以同 commit、相同环境和站点配置重复激活，返回 `Already active`，当时 MainPID 保持 `2314`，未重启应用；新增备份 `/opt/memoverse/activation-backups/activation-WKPhdvkQ` 记录重复激活时状态。真正切换前的原始回滚备份仍为 `activation-m1rFC4fz`，不可混用。

- `https://keepsite.space` 登录首页 HTTP 200，匿名业务 API 401；公网 TLS 下登录和访问控制再次通过。新 Nginx 在后端健康、匿名拒绝及会话配置检查通过后启用，OAuth 查询参数不进入访问日志。原 BasicAuth 站点、环境、service unit 和旧 release 定位保存在 root-only 的 `/opt/memoverse/activation-backups/activation-m1rFC4fz`。
- 旧 `/var/lib/memoverse` 未覆盖；新账号目录为 `/var/lib/memoverse-v2`。本机与旧服务器 collections、shares、community、assets 内容指纹均为 `24f1d86f40fc1df3ab9e9d71a276e438a91c6fdf31ab2b307172de82525fa4c2`。
- 模板版本 `efc6dc4631e0466ed4de23463deb73a3b8e3b6f3af2bda57d7bf3e314dc14b5e`：18 张图、18 个模型（15 个补齐、3 个旧模型），17 收藏、4 分享、6 社区示例，全部 ready。服务器 73 个文件 SHA-256 核验通过，目录 `/opt/memoverse/shared/initial-template-v1` 为 root:memoverse、目录 0750 / 文件 0640；不分发审计用 `source.sqlite3`。
- S3 私有桶 `memoverse-media-125647512944-apne1` 已读回验证 Block Public Access 全 true、SSE-S3 AES256、BucketOwnerEnforced、Versioning Enabled、IsPublic=false。实例角色仅关联本项目 EC2，允许该桶 `media/*`、`models/*` 的 Get/Put 和单桶 ListBucket；删除、ACL、`ops/*` 和其他桶访问未授权。
- Cognito Essentials 已启用 Google + COGNITO、刷新令牌轮换与撤销。Google 控制台 UI 已核验应用为 Testing，测试名单含 1 个当前用户账号，邮箱及密钥不入 Git；凭据位置见项目运行信息，临时 S3 凭据转运副本已删除。

本地验证：后端 125 项、模板 10 项、部署 23 项、Web 单元 24 项、原 Chromium 17 项及同用户 CSRF 更新窗口回归 2 项、打包 4 项通过；最新生产构建、Web 运行契约（14 个可达模块）和交付技能 quick_validate 通过。旧版本“28 个受保护运行时文件”不是本次 Web 契约。独立审查已关闭旧 SQLite 只读 WAL、S3 传输异常、HTTPError 资源关闭、模板路径穿越及同账号 CSRF 更新首屏挂起等问题；部署失败回滚、配置切换与 S3 权限复核无剩余 P1/P2。

真实服务器候选验证使用临时 Cognito A/B 和未验证账号，设置 `MessageAction=SUPPRESS`，因此不代表邮件投递验收。实际登录、邮箱验证状态拦截、双账号独立初始化、CSRF、私有 S3 图片/GLB SHA、上传、真实 AnyImage 生成、仅改文字复用模型、备份不串号、删除和退出后原图片/模型 401 全部通过。生成等待时间 48.878 秒（非纯推理分段计时），GLB 2,586,268 字节，健康查询最大观测 0.0045 秒；候选 MemoryPeak=820,273,152 字节、Swap=0、OOM/max=0、NRestarts=0，未作负载压测。强制 QA 会话 refresh_at 后，真实 Cognito 刷新令牌变更及版本递增通过，退出后该令牌被上游拒绝。

真实 HTTPS 浏览器登录后显示 17 件收藏，新生成“雨夜列车”GLB 加载和方向按钮操作通过；本机截图证据为 `.cache/qa-auth/web-login-live.png`、`web-collection-live.png`、`web-relief-live.png`，私有图片不提交。Cognito authorize 实际 302 到已登记 Google client 的 `accounts.google.com` 通过；**当时用户本人 Google consent 和真实邮件收件尚待验收**，该历史阶段不能宣称认证全链路已完成。本人于同日 19:40 的补充确认见顶部最新验收。

浏览器补充验收发现的两个退出时序问题已在 `80d7681` 修复并发布：前端先取消旧请求、卸载私人界面，再跳转严格校验的 Cognito `/logout`；后端只撤销请求携带的旧 sid，迟到响应不清除另一标签页后来登录或 OAuth 流程的 cookie。无效旧 sid 不再有任何访问权限，由新登录覆盖或自然过期。该流程退出 Cognito 托管会话，不退出 Google 自身会话。

退出修复经独立复核；认证 45 项、Web 单元 27 项、退出专项 Chromium 3 项、运行契约和生产构建通过。真实 HTTPS 浏览器退出后回到本站匿名首页，浏览器 Back 仍只显示登录页；Cognito `/logout` 实际返回 302 到 `https://keepsite.space/`。最终首页 200、HTTP→HTTPS 301、匿名业务 API 401，Google/email provider 均已配置。证据：`.cache/server/final-account-acceptance.json`、`logout-deploy.log`、`.cache/qa-auth/web-logout-live.png`；实际本人 Google consent 与邮件投递仍保留上述待验收边界。

本轮线上 QA 清理已完成：3 个临时 Cognito 账号已删除并查回为零；短暂停止应用后，精确清理 A/B 业务账号、归属记录及指定测试 asset/model，前后检查 SQLite 完整性、外键、模板引用和其他账号引用，复查为 already-cleaned，随后服务恢复 active。退出后已无匹配 QA 会话。S3 精确删除测试图、模型三文件及 QA 凭据共 5 个版本，相关前缀无版本或删除标记；桶内剩 18 media + 54 models，均为模板。服务器一次性暂存目录及本机 3 个临时凭据/签名 URL 文件已删除。本机 4173/4174/4175/8000/8001 无监听，候选服务已停止；该阶段曾保留网站登录页供用户验收，本次用户测试后已停机，网站暂不可访问。

历史 22 + 6 项拒绝清理记录继续保留；本轮下列 9 个合成测试目录删除被自动审批拒绝（`blocked by policy`），未绕过，仍为待清理项。保留当前准确发布制品、回滚备份、业务数据、模板、凭据及验收证据。

```text
.cache/initial-template-tests/tmp_tz77sg4
.cache/initial-template-tests/tmp4eou7n76
.cache/initial-template-tests/tmp6rezcqq7
.cache/initial-template-tests/tmp847tjs92
.cache/initial-template-tests/tmpdme40_tl
.cache/initial-template-tests/tmpgu4oisr4
.cache/initial-template-tests/tmphkzv0ps5
.cache/initial-template-tests/tmpls0nshho
.cache/initial-template-tests/tmpwziybpwf
```

最后只读审计还确认以下 7 个本轮合成测试目录未被 Git 跟踪、无重解析点，合计 18,737,557 字节（约 17.87 MiB）。逐项解析并限定在工作区 `.cache` 后，原生 PowerShell 递归删除整条命令仍被自动审批拒绝，仅返回 `blocked by policy`；没有执行删除，也未换方式重试，均继续保留：

```text
.cache/auth-review-callback-redirect
.cache/auth-review-http-close
.cache/auth-tests-20261018-a
.cache/auth-tests-20261018-b
.cache/auth-tests-20261018-c
.cache/pytest-account-stage-20261018
.cache/review-backend-20261018
```

## 初次部署验收（20 GB 阶段历史记录）

| 项目 | 结果 |
| --- | --- |
| 实例 | `i-0e9ba9bc6f4b1c019`，`memoverse-keepsite` |
| 区域 / 规格 | 东京 `ap-northeast-1a`，按需 `t3.small`，2 vCPU / 2 GB |
| 系统 / 磁盘 | Ubuntu 24.04 x86_64，20 GB 加密 gp3 |
| 公网 IP | 验收当时为 `54.168.57.143`，自动分配，现已释放 |
| 网站 | `https://keepsite.space`，原 BasicAuth 账号与口令保留 |
| 运行布局 | `/opt/memoverse/current` 指向发布目录；数据 `/var/lib/memoverse`；模型共享缓存 `/opt/memoverse/shared/cache` |
| 服务 | 验收当时 `memoverse`、`nginx`、`memoverse-ddns`、`certbot.timer` 均 active / enabled |
| SSH | 当前管理网络 CIDR 限制；部署使用稳定 `HostKeyAlias` 并保持严格主机密钥校验 |

## 根盘缩减至 10 GB 与最终停机

EBS 不支持原地缩小。使用原始 8 GB Canonical AMI 快照创建 10 GB 加密 gp3 卷 `vol-0bb6aa01805d28c75`，将应用数据、完整证书、认证、环境、资源限制和 DDNS 配置导出并校验后，正常停止同一实例并换根卷。旧 20 GB 卷在验证期间保持 detached，用作回退，未创建额外辅助实例或付费快照。

新根盘首次启动后，通过 AWS 最新控制台输出核对新的 SSH host key，再更新指定别名的已知主机记录，保持严格校验。恢复原运行提交 `01677b2`、相同模型与依赖、2 GB swap、Nginx、证书续期及 DDNS 服务；恢复数据时应用尚未启动，没有覆盖运行中的 SQLite。

验证结果：根设备 NVMe serial 对应新卷，EBS 10 GB、根分区约 9 GB、根文件系统约 8.7 GB，最终约 3.5 GB 可用；17 件收藏、4 条分享、3 张原图、3 个原模型任务保持，12 个原图/模型产物 SHA-256 一致。上传、实际生成、编辑删除、认证 HTTPS 与 DDNS 发布通过，OOM 事件为 0。新盘 `DeleteOnTermination=true`，加密保持启用，启动模板默认版本更新为 2（10 GB）。

验证通过后删除旧卷 `vol-0757cc21d5eb07417`，再正常 StopInstances。最终 AWS 查询只有一块本项目 10 GB 卷，实例 stopped 且无公网 IP；Cloudflare 查询 A 记录数为 0。换盘临时启动时的公网 IP `18.183.236.78` 已释放。

当前 10 GB gp3 基础费约 $0.96/月，按分配容量和保留时间逐秒折算，最低 60 秒，停机期间仍计费。本轮服务器上传包及本机换盘归档/一次性脚本已清理，保留验收日志。参考：[AWS 缩容限制](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-modify-volume.html)、[EBS 计费](https://aws.amazon.com/ebs/pricing/)。

使用公开动态 IPv4，不分配 Elastic IP、不创建负载均衡器或 NAT Gateway。CPU credits 采用 standard，避免额外 surplus credit 计费；耗尽 credits 时性能可能下降。账户原有两台停止的测试 EC2 未修改。

## 迁移与数据

旧 Lightsail 停止写入后导出一致性 SQLite 包及认证/完整证书目录。下载包 SHA-256 与旧服务器一致，在本机与 EC2 分别验证 SQLite 完整性、引用关系和文件哈希，然后删除旧 Lightsail 实例、静态 IP 与部署公钥。

恢复并验收：17 件收藏、4 条分享、6 条社区示例、3 张原图、3 个原 ready 模型任务。12 个原图及模型产物文件哈希与迁移清单一致。本机原始数据没有被覆盖。验收临时收藏、任务与产物已清理。

## 初次发布与功能验证记录

- 通用 Ubuntu 发布流程通过 14 项故障/幂等测试，子代理独立重跑并复核。修复了首次缺失 current 链接的判断、启动失败回滚以及同提交重试时补做自启动的问题。
- DDNS 通过 28 项离线测试及独立安全/健壮性复核；配置只接受 `api_token`，没有兼容字段回退。
- 生产构建通过，28 个受保护前端运行时文件完整性通过。
- 在真实 EC2 上首次部署成功。再次部署同 commit 复用相同已校验制品，应用 PID 保持 `4045`，未重复重启。
- 原图上传 SHA-256、重复创建、实际 CPU GLB 生成、生成中 API 查询、编辑复用模型、删除均通过。验收计时从创建后开始，到下载、编辑删除及核对结束为 42.407 秒，不能视作纯推理时间。
- 该样例 GLB 为 2,606,284 字节；健康查询最大观测响应 0.006 秒；服务 cgroup MemoryPeak=785,231,872 字节，MemorySwapPeak=0，OOM/max 事件均为 0，服务重启计数为 0。没有执行负载压测。
- 公网未认证 API 返回 401；认证后首页返回 200、读取 17 件收藏。源站保留有效的 Let's Encrypt 证书与 HTTP→HTTPS 跳转。

## 自动 DNS 的实际范围

EC2 启动后，`memoverse-ddns.service` 通过 IMDSv2 读取实例 ID 和当前公网 IPv4，用仅限本域名 DNS 编辑的 Cloudflare Token 更新精确的一条 A 记录。成功读回核验后，状态原子保存到 `/var/lib/memoverse-ddns/state.json`。配置和状态均为 root 所有、权限 600，Token 不写入 Git、命令参数或日志。

初次部署时在真实服务器上验证：`published_update`，重启 DDNS 服务时 `withdrawn`，随后 `published_create`；当时 Cloudflare API 和公共 DNS 均返回 IP `54.168.57.143`，DNS-only、TTL 60。Linux 文件锁、owner/mode 检查和 systemd 启停实际运行通过。

此流程只允许一个实例管理该域名。Cloudflare 的 DELETE 没有按当前内容的原子条件删除，检查与删除之间仍有很小的竞争窗口。强制关机或网络/Token 故障可能跳过撤回，因此关闭实例后仍需检查 DNS。初次验收仅重启 DDNS 服务，随后保持服务器运行供用户测试；之后按用户要求进行换盘和正常停机，真实 EC2 启停及 A 记录归零的结果见上文“根盘缩减至 10 GB 与最终停机”。

## 费用与清理

2026-10-17 公开价格核验：实例 `0.0272 USD/运行小时`，动态公网 IPv4 `0.005 USD/使用小时`，合计 `0.0322 USD/运行小时`。初次部署的 20 GB gp3 历史费用口径为约 `1.92 USD/月`；当前已缩减为 10 GB，约 `0.96 USD/月`，停机仍保留磁盘费用，详见上文。税费、抵扣和额外流量以实际账单为准。

服务器临时上传包和一次性恢复脚本已删除。本机保留当前 commit 的准确构建包与 SHA-256，用于同提交重试；保留密钥、访问配置及验收证据。迁移暂存目录 `.cache/migration-lightsail-ec2` 删除被自动审批拒绝（`blocked by policy`），已交用户手动清理。旧备份和 Lambda 缓存的历史清理限制见 Lightsail 退役记录，不能将未完成删除报告为已完成。

## 此前阶段收尾与清理状态（历史记录）

2026-10-18 用户结束本轮密集开发并要求停机、格式整理与清理。通过 AWS 实时查询确认指定 EC2 已为 `stopped`、无公网 IP，10 GB 加密根卷仍附着；Cloudflare API 确认本域名 A 记录数为 0。实例本已停止，本轮无需重复 StopInstances，也未启动或发布新应用版本，已部署源码仍为 `01677b2`。

子代理独立复核本阶段 CPU 推理资源调整、Ubuntu 发布、DDNS 及相关调用链，未发现新增阻塞问题；另行完成格式检查和本机清理审计。仓库没有统一 formatter，本轮仅补齐 DDNS 测试方法间空行、整理部署文档标题和历史状态。28 项 DDNS 离线测试、28 个受保护运行时文件完整性检查、Git diff 空白检查均通过；修改前后测试 AST 一致，运行逻辑未改。

本机 4173、8000、4174、4187、8080、8081、8443 均无监听，历史预览 PID 均不存在，无需再停进程。没有新建浏览器页面；用户现有 GitHub 页面保留。业务数据、模型与已安装依赖、私钥及认证配置、当前发布包及 SHA、备份和验收证据均保留。

清理预检确认以下 22 个目录位于本仓库内、没有 Git 跟踪文件或重解析点，合计约 139.35 MiB。执行递归删除前，自动审批拒绝整条命令，仅返回 `blocked by policy`，删除没有执行；随后逐项确认目录仍在。未改用其他方式绕过，以下均为**待清理项**，路径相对本仓库：

```text
.cache/anyimage-smoke
.cache/factory-smoke-84c7c4c8-6515-4edc-af91-44e627dd266a
.cache/lightsail-tests-f335dec9-20af-4ae7-8dc2-cad7c1bd3ce3
.cache/onegb-worker-smoke-21222bce727c43938dacf0790383dfe1
.cache/onegb-worker-smoke-51d134f919564f90a15ebc7c81595b05
.cache/pytest-2d4391d6-71a1-4e2d-a874-25be03d536cd
.cache/pytest-317110bc-71ad-4051-96c5-52834ca41fa5
.cache/pytest-a4b3fe68-8fee-4e56-b58e-80f9481e3d99
.cache/pytest-fc1a4ba7-40b5-4794-b0b5-0c449c24e832
.cache/pytest-api-e44aba01-fa47-45b5-b81d-a4f0330c9452
.cache/pytest-anyimage-preparation-6fb5dc83ab9b4beba72770d3a07550c7
.cache/pytest-anyimage-preparation-bfc9791c82f64bb7b93c0429e4e30228
backend/cloud
backend/__pycache__
.pytest_cache
.cache/deployment-tests
.cache/ddns-unit-tests
.cache/tmp
demo/dist
.cache/run
.cache/npm
.cache/pip
```

此前已被拒绝的以下 6 项仍在，本轮未重试删除：

```text
backups/local-baseline-20261017-213502
.cache/iam
.cache/uv
.cache/pytest-cloud-generator
backend/.pytest-repo-integration-20261017-a
.cache/migration-lightsail-ec2
```
