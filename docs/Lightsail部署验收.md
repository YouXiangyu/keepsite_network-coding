# Lightsail 部署验收

日期：2026-10-17。当前状态：服务器内部功能与代码更新已验收；公网 HTTPS 等待域名 NS 切换生效。

## 资源与运行方式

| 项目 | 值 |
| --- | --- |
| 实例 | `memoverse-keepsite` |
| 区域 / 可用区 | 东京 `ap-northeast-1` / `ap-northeast-1a` |
| 套餐 | `micro_3_0`，7 USD/月，2 vCPU、1 GB RAM、40 GB 磁盘、2 TB 套餐流量 |
| 系统 | Ubuntu 24.04 LTS，Python 3.12，x86_64 |
| 静态 IPv4 | `52.196.70.26`，已绑定实例 |
| 域名 | `keepsite.space` |
| 数据 | `/var/lib/memoverse`，SQLite + 原图 + GLB/depth/geometry |
| 代码 | `/opt/memoverse/releases/<Git提交>`；`current` 指向启用版本 |
| 模型缓存 | `/opt/memoverse/shared/cache`，代码更新时复用 |
| 服务 | systemd 单个 Uvicorn worker，监听 `127.0.0.1:8000` |
| 内存配置 | 2 GB swap；服务 MemoryHigh 700M、MemoryMax 850M、MemorySwapMax 2G |

前端在开发电脑构建。Nginx 负责静态文件与 API 反向代理，最终 HTTPS 入口使用 BasicAuth 测试口令。密钥及明文口令仅保存在开发电脑被 Git 忽略的受限文件中；实例只保存网站口令哈希。SSH 防火墙只允许创建时验证的管理端 IPv4；管理网络变化后需要更新该规则。

创建的 Lightsail 资源为一台实例、一个已绑定静态 IP 和一个部署公钥。没有创建 Lambda、DynamoDB、S3、CloudFront、ECR 或 SAM/CloudFormation 试用资源。账户已有 5 USD 月预算提醒未修改；它低于本套餐完整月基础费用。套餐税费和超额流量以 AWS 账单为准，预算提醒不是硬性停机限额。

## 数据迁移

通过 SQLite backup API 生成一致性传输副本；原本机数据未修改。迁移包使用 SSH 上传。接收端校验 SQLite 完整性、引用关系和每个文件的 SHA-256。

- 17 件收藏，4 条分享，6 条社区示例。
- 3 个原图资源，3 个 ready 生成任务。
- 共 13 个数据文件：SQLite、3 张原图、9 个模型产物。
- geometry 副本去掉开发机绝对路径字段；图片、GLB 和深度数据保留原字节。
- 数据与密钥没有上传 GitHub。

## 验收结果

本地 43 项后端测试、18 项前端测试、28 个受保护运行时文件检查、生产构建通过；迁移脚本 3 项合成数据自测通过。

服务器实际执行了上传原图 SHA-256 校验、重复创建、CPU 生成 GLB、生成中查询、编辑复用模型和删除。一次参考图片的生成及轮询流程约 49.373 秒，GLB 2,606,284 字节，期间健康请求的最大观测响应时间 1.189 秒。

该轮整个应用服务 cgroup 的峰值物理内存为 612,495,360 字节（约 584 MiB），峰值 swap 为 283,504,640 字节（约 270 MiB）。`memory.events` 的 `oom`、`oom_kill`、`max` 均为 0，服务重启计数为 0。这是一次功能样例，不是吞吐量或最大尺寸图片的性能保证；1 GB 机器实际使用了 swap。

浏览器通过临时 SSH 隧道打开服务器静态页面，显示 17 件收藏并成功加载原有 GLB。视角按钮操作后 DOM 记录总偏转角 11.18°，模型正常显示。

随后从提交 `1d0be98` 更新到 `1b97173`，共享模型缓存成功复用。更新后 17 件收藏、4 条分享、3 个原图、3 个原生成任务仍在；12 个原图与模型产物文件哈希一致。验收临时收藏及其专属任务产物已清理。

## 尚待完成

域名注册局的查询仍显示 `launch1.spaceship.net`、`launch2.spaceship.net`；用户已提交 Cloudflare 名称服务器切换，等待生效。A 记录应为 `@ → 52.196.70.26`，首次签证时使用“仅 DNS”。

DNS 指向本实例后执行 `deploy/lightsail/enable-https.sh keepsite.space 52.196.70.26 --execute`：脚本先校验解析，再申请 Webroot 证书、启用 TLS/BasicAuth 和续期 reload hook。随后还需验证公网浏览器、未登录访问拒绝和证书链。

Lambda 新增源码、部署文件、专用 Python 包、本地与远端实验分支已删除。自动命令审批拒绝清理本机 `backups/local-baseline-20261017-213502`、`.cache/iam`、`.cache/uv`、`.cache/pytest-cloud-generator` 和 `backend/.pytest-repo-integration-20261017-a`，返回原因仅为 `blocked by policy`；这些目录仍在，未绕过限制。它们不属于服务器运行依赖，也不在 GitHub 中。
