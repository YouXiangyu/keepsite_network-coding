# EC2 部署验收

验收日期：2026-10-18，北京时间。应用运行代码提交：`01677b2801828bf913c9f9592ac9de995decadd9`。后续纯文档提交不改变运行版本。

当前状态（2026-10-18 00:46 核验）：用户已要求停止实例，并将 20 GB 根盘缩减为 10 GB。新盘恢复与验证完成，旧盘已删除，实例 `stopped`、无公网 IP，Cloudflare A 记录为空。下文早期 20 GB 验收数据作为过程证据保留。

## 部署结果

| 项目 | 结果 |
| --- | --- |
| 实例 | `i-0e9ba9bc6f4b1c019`，`memoverse-keepsite` |
| 区域 / 规格 | 东京 `ap-northeast-1a`，按需 `t3.small`，2 vCPU / 2 GB |
| 系统 / 磁盘 | Ubuntu 24.04 x86_64，20 GB 加密 gp3 |
| 公网 IP | 当前 `54.168.57.143`，自动分配，停机再启动可能变化 |
| 网站 | `https://keepsite.space`，原 BasicAuth 账号与口令保留 |
| 运行布局 | `/opt/memoverse/current` 指向发布目录；数据 `/var/lib/memoverse`；模型共享缓存 `/opt/memoverse/shared/cache` |
| 服务 | `memoverse`、`nginx`、`memoverse-ddns`、`certbot.timer` 均 active / enabled |
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

## 发布与功能验证

- 通用 Ubuntu 发布流程通过 14 项故障/幂等测试，子代理独立重跑并复核。修复了首次缺失 current 链接的判断、启动失败回滚以及同提交重试时补做自启动的问题。
- DDNS 通过 28 项离线测试及独立安全/健壮性复核；配置只接受 `api_token`，没有兼容字段回退。
- 生产构建通过，28 个受保护前端运行时文件完整性通过。
- 在真实 EC2 上首次部署成功。再次部署同 commit 复用相同已校验制品，应用 PID 保持 `4045`，未重复重启。
- 原图上传 SHA-256、重复创建、实际 CPU GLB 生成、生成中 API 查询、编辑复用模型、删除均通过。验收计时从创建后开始，到下载、编辑删除及核对结束为 42.407 秒，不能视作纯推理时间。
- 该样例 GLB 为 2,606,284 字节；健康查询最大观测响应 0.006 秒；服务 cgroup MemoryPeak=785,231,872 字节，MemorySwapPeak=0，OOM/max 事件均为 0，服务重启计数为 0。没有执行负载压测。
- 公网未认证 API 返回 401；认证后首页返回 200、读取 17 件收藏。源站保留有效的 Let's Encrypt 证书与 HTTP→HTTPS 跳转。

## 自动 DNS 的实际范围

EC2 启动后，`memoverse-ddns.service` 通过 IMDSv2 读取实例 ID 和当前公网 IPv4，用仅限本域名 DNS 编辑的 Cloudflare Token 更新精确的一条 A 记录。成功读回核验后，状态原子保存到 `/var/lib/memoverse-ddns/state.json`。配置和状态均为 root 所有、权限 600，Token 不写入 Git、命令参数或日志。

已在真实服务器上验证：初次 `published_update`，重启 DDNS 服务时 `withdrawn`，随后 `published_create`；Cloudflare API 和公共 DNS 均返回新 IP `54.168.57.143`，DNS-only、TTL 60。Linux 文件锁、owner/mode 检查和 systemd 启停实际运行通过。

此流程只允许一个实例管理该域名。Cloudflare 的 DELETE 没有按当前内容的原子条件删除，检查与删除之间仍有很小的竞争窗口。强制关机或网络/Token 故障可能跳过撤回，因此关闭实例后仍需检查 DNS。当前没有为了验收执行 EC2 Stop/Start；服务器继续运行供用户测试，待用户确认通过后才正常 StopInstances。

## 费用与清理

2026-10-17 公开价格核验：实例 `0.0272 USD/运行小时`，动态公网 IPv4 `0.005 USD/使用小时`，合计 `0.0322 USD/运行小时`；20 GB gp3 约 `1.92 USD/月`，停机仍保留这项磁盘费用。税费、抵扣和额外流量以实际账单为准。

服务器临时上传包和一次性恢复脚本已删除。本机保留当前 commit 的准确构建包与 SHA-256，用于同提交重试；保留密钥、访问配置及验收证据。迁移暂存目录 `.cache/migration-lightsail-ec2` 删除被自动审批拒绝（`blocked by policy`），已交用户手动清理。旧备份和 Lambda 缓存的历史清理限制见 Lightsail 退役记录，不能将未完成删除报告为已完成。
