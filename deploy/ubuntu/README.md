# Ubuntu / EC2 部署

既有环境为东京 `t3.small`（2 GB）EC2，Nginx 提供 HTTPS。新版发布候选使用独立 `/var/lib/keepsite-v3`、Cognito 和私有 S3，单 API worker；配置就绪时另有受限的远程识别子进程，不运行本地 OCR 或旧浮雕 worker。源码更新不代表已部署；部署、服务器启停均须用户明确要求。

旧 `/var/lib/memoverse`、`/var/lib/memoverse-v2`、`/var/lib/memoverse-v3` 及其媒体保留，不自动升级或覆盖。名称迁移必须先完成[离线迁移与回滚](../../docs/keepsite迁移.md)，工具为 `scripts/migrate-keepsite.py`，默认只预演。schema 1/2 仍须先用一致快照和原票副本在隔离目录验证 `scripts/migrate-movie-tickets.py`；本地验证副本不能直接宣称已完成 S3 恢复。

实时实例、域名、角色、用户池与配置位置见[项目运行信息](../../.agents/skills/keepsite-delivery/references/project-context.md)。操作前用 AWS MCP 查询明确实例 ID；启动后重新查询动态公网 IP，核对 DDNS 和 SSH 主机身份，不使用历史 IP。

## 发布前提

- 源码完整提交，工作区干净，阶段独立审查与相关测试通过。
- EC2 running 且状态检查通过，严格 SSH host key 验证可用。动态地址使用稳定 `-HostKeyAlias keepsite-i-0e9ba9bc6f4b1c019`。
- Cognito 已配置邮箱验证、Google 提供方、授权码与令牌轮换；环境配置包含[后端列出的必需项](../../backend/README.md)。密钥只放受限文件，不输出或提交 Git。
- 私有 S3 禁止公开访问，服务器通过实例角色获得临时凭据。角色策略见 `keepsite-s3-policy.json`：只允许目标桶 media/models 的 Get/Put，另允许该桶 ListBucket，使不存在对象的 HEAD 正确返回 404；没有对象删除、ACL、KMS 或其他桶权限。策略中的既有 `memoverse-media-125647512944-apne1` 是真实桶标识，不能在代码改名时伪造替换；`KEEPSITE_S3_BUCKET` 保留实际桶值。S3 桶不能原地改名，另建桶、复制对象和切换权限须单独执行并验收。既有 IAM、Cognito 和 EC2 标识同样保持真实值。
- 新账号从空票夹开始，不再需要初始化模板或 AnyImage 模型。旧模板和共享模型缓存仍保留用于历史回滚，不自动清理。
- 票据识别使用远程视觉 API。通过受限环境文件明确配置 `KEEPSITE_QWEN_API_KEY`、`KEEPSITE_QWEN_BASE_URL` 和 `KEEPSITE_QWEN_MODEL`（当前用户选择 `qwen3.7-flash`，没有地址或模型默认值），并配置可用 Redis、私有 S3 与海报来源；缺少条件时界面明确显示处理不可用。Key 不写入客户端、Git、命令行或日志；普通安装与 CI 不下载模型，不调用付费接口。确认配置不代表已验证模型可用、账户余额、真实票据准确率或费用。旧 CPU 依赖、准备脚本与运行回退已退出新版；已有共享模型缓存及旧 release 保留，不自动删除。
- `/var/lib/keepsite-v3` 属于 keepsite，0700；service 只对新数据目录开放写入。旧数据库、证书、DDNS 与认证回退资料保持完整。

## 准备与候选验证

先执行计划模式；同一次发布始终使用同一 Git commit 及其已校验制品。

```powershell
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias keepsite-i-0e9ba9bc6f4b1c019 -PrepareOnly
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias keepsite-i-0e9ba9bc6f4b1c019 -PrepareOnly -Execute
```

`-PrepareOnly` 构建同源前端、上传已提交源码和构建，校验 tar/文件哈希，准备独立 release 与 Python 依赖；不启动应用、不切换 current 或公开站点。相同提交只复用准确匹配的制品，目录或哈希冲突立即停止。`-InitializeHost` 仅用于已审查的新主机初始软件/用户配置，不在每次更新重复运行。

执行模式持有工作区 `.cache/server/deployment.lock` 的独占文件句柄，覆盖构建、制品发布、上传和远端执行；同一工作区第二次发布立即拒绝。锁文件保留，进程关闭句柄后释放锁，不按文件是否存在判断占用。上传包与辅助脚本使用每次唯一 ID，失败清理也只针对本次传输路径。制品先原子写入哈希，再晋升归档；只有哈希而没有归档的中断状态可重新构建，已有归档缺哈希或不匹配仍停止。

服务器准备和激活共用 `/opt/keepsite/activation-backups/.lock` 的真实 `flock`，准备锁覆盖解包、校验与依赖安装。先解包到同卷唯一临时目录，全部校验成功并写入制品身份后才原子改名为最终 release；中断解包不会留下半成品占用正式路径。venv 在最终路径安装，避免解释器绝对路径随目录移动失效；安装失败不写 prepared 标记，下次核验同一制品后重新安装。未知或冲突的正式目录始终保留并拒绝覆盖。

新环境放 `/etc/keepsite.env.next`（root 0600 或 root:keepsite 0640），新站点放 `/etc/nginx/sites-available/keepsite.next`（root 0644）。候选文件必须是实际普通文件。环境中的 `KEEPSITE_DATA_DIR` 必须为 `/var/lib/keepsite-v3`，`KEEPSITE_STORAGE=s3`，身份配置必须齐全；不要从 shell 执行环境文件，也不要在命令行传密钥。旧模板和 `ANYIMAGE_PYTHON` 配置不再用于新版应用。

重大配置迁移先在服务器 `127.0.0.1` 的独立端口启动候选服务，使用相同新环境和实例角色检查真实对象读取、空账号初始化、匿名拒绝与登录流程。候选检查不改域名入口，结束后停止候选服务。

## 正式切换

```powershell
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias keepsite-i-0e9ba9bc6f4b1c019 -EnvironmentPath /etc/keepsite.env.next -NginxPath /etc/nginx/sites-available/keepsite.next
./scripts/deploy-ubuntu.ps1 -Server <本次IP> -HostKeyAlias keepsite-i-0e9ba9bc6f4b1c019 -EnvironmentPath /etc/keepsite.env.next -NginxPath /etc/nginx/sites-available/keepsite.next -Execute
```

激活脚本持互斥锁，保存实际旧环境、站点、unit 与 current 到 root-only 的 `/opt/keepsite/activation-backups/activation-*`。先预检候选权限、配置和 Nginx 语法；停止旧应用后切换环境、unit 和代码，再检查 health、匿名 state=401 和 session configured=true。

站点配置变化时会短暂停止 Nginx，避免旧日志格式记录新 OAuth 回调。只有新后端访问控制通过，才安装并启动公开登录页配置。环境/站点变更即使使用相同 commit 也会正确应用；完全相同且健康时保持幂等。

失败回滚先彻底停止 Nginx 的新公开 worker，恢复并检查旧站点，再恢复实际旧环境、unit 和代码，最后启动旧应用。若恢复失败则保留停站/停应用状态并明确报告 `ROLLBACK FAILED`；不能把尝试回滚当作成功。保留备份直至新版本及数据验证通过，不盲删冲突 release。

从首次停止旧应用开始，`INT`、`TERM`、`HUP` 与异常退出统一进入同一恢复出口。恢复期间忽略重复信号且始终持锁，避免重复回滚或另一发布穿插；恢复成功仍返回原失败/信号的非零状态，恢复失败返回 2。完全成功或同 commit、同配置且健康的早退不回滚，后者不重启应用。健康版本已切换但 enable-on-boot 失败时保留健康版本，报告非零并允许重试启用。

备份中的 `service-state` 记录修改前应用与 Nginx 是否 active。回滚恢复原配置和链接后，只启动原本运行的服务并验证健康；原本停止的服务保持停止并核验 inactive。失败或中断不应替用户启动维护期间已停止的服务。

`SIGKILL`、断电和主机自动关机耗尽退出期限不保证捕获恢复。恢复后仍必须查询实际 current、配置和服务状态；强制终止后先检查 root-only 激活备份，按其中 previous-release/environment/nginx/unit 恢复并验证访问控制，不能只重启旧服务。强杀可能保留唯一 `.extract-*` 临时目录或本地 `.building.tar`，它们不作为可发布目录；确认无活跃准备进程、所有权与路径后再清理，不自动删除未知目录。普通应用发布不延长 58 分钟关机计时器。

未来仅代码更新且现用配置已经完整时，可以省略两个候选路径参数。首次从 BasicAuth 单用户版本转到账号版本，必须一起切换环境和站点配置。

## 验收与运维

- 检查内部 health、HTTP→HTTPS、公开登录页、匿名 API/原图/备份拒绝；旧社交、模型、任务接口不存在。
- 实测邮箱/Google 身份、两账号隔离、新账号空票夹、原票上传与有效引用、字段纠错、幂等重试、删除后的媒体拒绝与备份；模拟提供方测试不能代替真实 Google 同意和邮件收件。启用 OCR 后还须验证异步进度、Redis 重启补投、纠错/删除竞态、海报来源及 S3 发布核对；本机合成样本不能替代主机资源和真实票据验收。
- 验证 `/opt/keepsite/current` 实际提交、S3 对象完整性与私有性、原数据库仍保留；检查磁盘、内存和 OOM。
- Nginx access 格式不含查询字符串，OAuth callback 的 access/error 日志单独关闭；Uvicorn 同样去除认证回调查询。应用日志不记录密码、验证码或令牌。
- 应用依赖实例角色，不保存长期 AWS 访问密钥。按测试规模评估 S3 访问日志、CloudTrail 数据事件和 CloudWatch 指标的可观测性与额外费用，不默认启用与本轮无关的收费服务。
- systemd 默认 MemoryHigh=1200M、MemoryMax=1500M、swap 上限 2 GB，针对当前 2 GB 测试机；更大图片/并发仍需实测。以应用行为判断容量，不把单样例当作压测结果。
- 每次开机 58 分钟后自动发起正常关机；等待验收不暂停倒计时。用户确认通过或明确要求后可提前停机，核验域名 A 记录仍指向保留的 EIP。延长或取消本次倒计时须用户明确授权。停机仍保留磁盘、S3 数据及固定 IP 计费。

## 开机 58 分钟自动关机

`keepsite-auto-stop.timer` 使用 `OnBootSec=58min`，从系统启动计时，触发同名 service 执行正常 `systemctl poweroff`。本机已核验 EC2 `InstanceInitiatedShutdownBehavior=stop`；迁移到其他实例时必须重新核验，不能假定系统关机一定不会删除实例。配置不依赖 Codex 或 AWS 登录会话继续在线。

首次安装前查看 `cat /proc/uptime`。如果本次运行已超过 58 分钟，启动该 timer 会立即触发关机；应在刚启动的维护窗口安装。只启用 timer，不启用 service。以下命令针对已审查、位于当前工作目录的两个 unit：

```bash
sudo install -o root -g root -m 0644 keepsite-auto-stop.service keepsite-auto-stop.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/keepsite-auto-stop.service /etc/systemd/system/keepsite-auto-stop.timer
sudo systemctl daemon-reload
sudo systemctl enable --now keepsite-auto-stop.timer
systemctl is-enabled keepsite-auto-stop.timer
systemctl list-timers keepsite-auto-stop.timer --all
systemctl show keepsite-auto-stop.timer -p NextElapseUSecMonotonic -p TimersMonotonic -p AccuracyUSec
```

这是独立的主机配置，普通应用发布不会替换或重新计时。应用重启不会延长；系统重新开机重新计时。在第 58 分钟发起正常关机，预留约两分钟等待应用、DDNS 退出和 AWS 状态转换；该缓冲不保证严格在开机后 60 分钟内进入 AWS `stopped`。若用户明确授权延长，另行调整并记录本次期限，不要仅重启 timer 冒充延长。

配置语义参考 [systemd timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html) 与 [EC2 系统关机行为](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Using_ChangingInstanceInitiatedShutdownBehavior.html)。

## 自动 DNS

当前 EC2 使用固定 Elastic IP。`keepsite-ddns.service` 在开机后读取 IMDSv2，核对并更新 Cloudflare 精确 A 记录；正常关机保留该记录，减少重复删除/创建引起的 DNS 负缓存。配置和状态仅 root 可读。DDNS 是独立运维组件，普通应用发布不会自动替换 `/opt/keepsite/admin/sync-cloudflare-dns.py`。固定地址保留期间持续计费，停机仍不可访问网站。

从旧策略迁移时，先安装不含 `ExecStop` 的 unit 并执行 `daemon-reload`，确认 `systemctl show keepsite-ddns.service -p ExecStop` 为空；此前不要 stop/restart 旧服务，以免触发撤回。核对旧 `memoverse-ddns.service` 已 inactive 且停用自启动。绑定 EIP 后显式执行一次 `publish --execute`，再读回权威 A 记录。只允许单实例管理该域名，关机后核验记录仍指向保留的 EIP。脚本的 `withdraw` 仅用于另行明确授权的人工撤回，不再由关机自动调用；释放 EIP 前必须另行处理域名，避免指向已回收地址。58 分钟自动关机 timer 无需更改或重启。

## 本地控制流测试

```powershell
backend/.venv/Scripts/python.exe -B -m unittest discover -s deploy/ubuntu/tests -v
```

测试使用隔离路径及服务命令桩，不调用真实 systemd、SSH 或 AWS。Windows 的独立 PowerShell 进程测试覆盖工作区互斥、失败后释放锁、唯一上传与制品中断重试；普通控制流测试覆盖制品身份、路径安全、幂等、配置更新与失败回滚。

`test_deployment_processes.py` 的 Linux 测试使用真实 `flock`、进程和 POSIX 信号，覆盖准备互斥、解包中断、停止服务后/unit 安装后/current 切换后的 TERM、重复信号、恢复期间锁竞争及恢复失败。Windows 明确跳过这些测试，不能用 Git Bash 的命令桩结果替代 Linux 证据。GitHub Actions `Deployment recovery` 在功能分支 push 和 PR 上执行 Ubuntu 测试；在本地 Linux 也可运行：

```bash
python3 -B -m unittest discover -s deploy/ubuntu/tests -v
```

CI 仅证明隔离进程与控制流，不验证真实服务、身份提供方或生产数据。真实发布必须另行验收并维护[项目运行信息](../../.agents/skills/keepsite-delivery/references/project-context.md)。
