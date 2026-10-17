# Ubuntu / EC2 部署

项目运行于东京的既有 `t3.small`（2 GB）EC2，应用单进程、单生成 worker，Nginx 提供 HTTPS。账号版使用新的 `/var/lib/memoverse-v2`、Cognito 和私有 S3。旧 `/var/lib/memoverse` 保留，绝不能把旧 schema 1 目录交给新版本自动升级。

实时实例、域名、角色、用户池与配置位置见[项目运行信息](../../.agents/skills/memoverse-delivery/references/project-context.md)。操作前用 AWS MCP 查询明确实例 ID；启动后重新查询动态公网 IP，核对 DDNS 和 SSH 主机身份，不使用历史 IP。

## 发布前提

- 源码完整提交，工作区干净，阶段独立审查与相关测试通过。
- EC2 running 且状态检查通过，严格 SSH host key 验证可用。动态地址使用稳定 `-HostKeyAlias memoverse-i-0e9ba9bc6f4b1c019`。
- Cognito 已配置邮箱验证、Google 提供方、授权码与令牌轮换；环境配置包含[后端列出的必需项](../../backend/README.md)。密钥只放受限文件，不输出或提交 Git。
- 私有 S3 禁止公开访问，服务器通过实例角色获得临时凭据。角色策略见 `memoverse-s3-policy.json`：只允许目标桶 media/models 的 Get/Put，另允许该桶 ListBucket，使不存在对象的 HEAD 正确返回 404；没有对象删除、ACL、KMS 或其他桶权限。授权候选由 IAM Policy Autopilot 生成后按实际 SSE-S3 和前缀范围收紧，并经过 IAM 模拟验证。
- `/opt/memoverse/shared/initial-template-v1` 是已核验、不可变的账号初始化模板。目录 root:memoverse 0750、文件 0640。分发只含 manifest 及引用媒体/模型，不能夹带审计快照 `source.sqlite3`。
- `/var/lib/memoverse-v2` 属于 memoverse，0700；service 的写路径只包括新数据目录和共享模型缓存。旧数据库、证书、DDNS 与认证回退资料保持完整。

## 准备与候选验证

先执行计划模式；同一次发布始终使用同一 Git commit 及其已校验制品。

```powershell
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias memoverse-i-0e9ba9bc6f4b1c019 -PrepareOnly
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias memoverse-i-0e9ba9bc6f4b1c019 -PrepareOnly -Execute
```

`-PrepareOnly` 构建同源前端、上传已提交源码和构建，校验 tar/文件哈希，准备独立 release 与 Python 依赖；不启动应用、不切换 current 或公开站点。相同提交只复用准确匹配的制品，目录或哈希冲突立即停止。`-InitializeHost` 仅用于已审查的新主机初始软件/用户配置，不在每次更新重复运行。

新环境放 `/etc/memoverse.env.next`（root 0600 或 root:memoverse 0640），新站点放 `/etc/nginx/sites-available/memoverse.next`（root 0644）。候选文件必须是实际普通文件。环境中的 `MEMOVERSE_DATA_DIR` 必须为新目录，`MEMOVERSE_STORAGE=s3`，身份与模板配置必须齐全；不要从 shell 执行环境文件，也不要在命令行传密钥。

环境中的 `ANYIMAGE_PYTHON` 可省略，让生成任务使用当前进程解释器；如指定，必须对应新 release 的 venv。不能让候选版本调用旧 current 的解释器。

重大配置迁移先在服务器 `127.0.0.1` 的独立端口启动候选服务，使用相同新环境和实例角色检查真实对象读取、账号初始化、匿名拒绝与登录流程。候选检查不改域名入口，结束后停止候选服务；同一数据目录不要同时运行多个生成 worker。

## 正式切换

```powershell
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias memoverse-i-0e9ba9bc6f4b1c019 -EnvironmentPath /etc/memoverse.env.next -NginxPath /etc/nginx/sites-available/memoverse.next
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias memoverse-i-0e9ba9bc6f4b1c019 -EnvironmentPath /etc/memoverse.env.next -NginxPath /etc/nginx/sites-available/memoverse.next -Execute
```

激活脚本持互斥锁，保存实际旧环境、站点、unit 与 current 到 root-only 的 `/opt/memoverse/activation-backups/activation-*`。先预检候选权限、配置和 Nginx 语法；停止旧应用后切换环境、unit 和代码，再检查 health、匿名 state=401 和 session configured=true。

站点配置变化时会短暂停止 Nginx，避免旧日志格式记录新 OAuth 回调。只有新后端访问控制通过，才安装并启动公开登录页配置。环境/站点变更即使使用相同 commit 也会正确应用；完全相同且健康时保持幂等。

失败回滚先彻底停止 Nginx 的新公开 worker，恢复并检查旧站点，再恢复实际旧环境、unit 和代码，最后启动旧应用。若恢复失败则保留停站/停应用状态并明确报告 `ROLLBACK FAILED`；不能把尝试回滚当作成功。保留备份直至新版本及数据验证通过，不盲删冲突 release。

未来仅代码更新且现用配置已经完整时，可以省略两个候选路径参数。首次从 BasicAuth 单用户版本转到账号版本，必须一起切换环境和站点配置。

## 验收与运维

- 检查内部 health、HTTP→HTTPS、公开登录页、匿名 API/原图/模型/备份拒绝。
- 实测邮箱/Google 身份、两账号隔离、模板首次初始化、上传生成、编辑复用、删除与导出；模拟提供方测试不能代替真实 Google 同意和邮件收件。
- 验证 `/opt/memoverse/current` 实际提交、S3 对象完整性与私有性、原数据库仍保留；检查磁盘、内存和 OOM。
- Nginx access 格式不含查询字符串，OAuth callback 的 access/error 日志单独关闭；Uvicorn 同样去除认证回调查询。应用日志不记录密码、验证码或令牌。
- 应用依赖实例角色，不保存长期 AWS 访问密钥。按测试规模评估 S3 访问日志、CloudTrail 数据事件和 CloudWatch 指标的可观测性与额外费用，不默认启用与本轮无关的收费服务。
- systemd 默认 MemoryHigh=1200M、MemoryMax=1500M、swap 上限 2 GB，针对当前 2 GB 测试机；更大图片/并发仍需实测。以应用行为判断容量，不把单样例当作压测结果。
- 用户验收前保留服务器可访问；用户确认通过或明确要求后正常停止 EC2，核验 DDNS 撤回。停机仍保留磁盘及 S3 数据计费。

## 自动 DNS

`memoverse-ddns.service` 在 EC2 开机后读取 IMDSv2，更新 Cloudflare 精确 A 记录，正常关机撤回仍属于本实例的记录。配置和状态仅 root 可读。DDNS 是独立运维组件，普通应用发布不会自动替换 `/opt/memoverse/admin/sync-cloudflare-dns.py`。

若明确修改 DDNS，先验证脚本与 unit，再同步两者、daemon-reload，并有计划地重启服务；重启会短暂撤回再发布 DNS。只允许单实例管理该域名。强制关机、网络故障或 Token 失效时不保证撤回，关机后必须读回核验。

## 本地控制流测试

```powershell
backend/.venv/Scripts/python.exe -B -m unittest discover -s deploy/ubuntu/tests -v
```

测试使用隔离路径及命令桩，覆盖制品身份、路径安全、幂等、配置更新与失败回滚；不调用真实 systemd、SSH 或 AWS。真实发布必须另行验收并维护[EC2 部署记录](../../docs/EC2部署验收.md)。
