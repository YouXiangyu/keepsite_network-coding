# keepsite 名称与数据迁移

更新日期：2026-10-25。

2026-10-24 用户确认软件名称及技术标识统一为小写 `keepsite`，同时要求保留已有数据。下文是当时的迁移步骤，旧路径是迁移来源。服务器切换和手机安装新包已在之后完成，结果见[项目运行信息](../.agents/skills/keepsite-delivery/references/project-context.md)。云资源标识没有改名。历史验收文件中的旧名称是当时事实，不改写成未发生的迁移结果。

## 变更范围

| 位置 | 新标识 | 迁移影响 |
| --- | --- | --- |
| 网站、应用、接口及文档 | `keepsite` | 用户可见名称统一 |
| Android 安装与回调 | `io.github.youxiangyu.keepsite` | 新安装身份，旧 App 保留并存 |
| 后端配置 | `KEEPSITE_*` | 必须转换旧环境变量，新程序拒绝旧前缀 |
| 业务库 | `keepsite.sqlite3` | 显式复制验证后启用；发现旧库拒绝创建空库 |
| 会话及客户端请求 | keepsite Cookie、`X-Keepsite-Client` | 旧 Cookie 不复用，需重新登录；客户端与后端一起切换 |
| Web 缓存、Redis、设备通道 | keepsite 命名空间 | 从持久 SQL/S3 重建缓存及处理通知，不复制缓存为业务事实 |
| Ubuntu 服务及路径 | `keepsite.service`、`/opt/keepsite`、`/var/lib/keepsite-v3` | 停止旧服务后迁移，保留旧目录与回滚配置 |

账户 ID 仍由原 Cognito issuer 和 subject 计算，双登录方式绑定关系保存在原 `auth.sqlite3`，不按新品牌重算。原票、海报、S3 对象键、记录 ID、账号归属和服务数据不能通过文本替换修改。

## 离线数据与配置迁移

使用新源码提供的 `scripts/migrate-keepsite.py`。默认只检查并显示计划，只有 `--execute` 才生成全新目标。工具不启停服务、不改 DNS、不删除来源、不改变云资源。

```sh
python -B scripts/migrate-keepsite.py \
  --source-data /var/lib/memoverse-v3 \
  --source-env /etc/memoverse.env \
  --destination /var/lib/keepsite-v3 \
  --path-map /opt/memoverse/shared=/opt/keepsite/shared
```

执行迁移前，另行明确安排维护窗口：停止接收新写入，停止旧应用及候选应用，确认没有在途上传、处理或设备传输；保持原自动关机期限。先单独准备 `keepsite` 系统用户及同名组，不创建数据目录，也不要先运行会创建目标目录的安装器。用户不存在时可由管理员执行 `useradd --system --user-group --no-create-home --home-dir /var/lib/keepsite-v3 --shell /usr/sbin/nologin keepsite`；已有用户先核对其 UID/GID 与服务配置。

以 root 在上述预演命令后加 `--execute`。工具要求离线服务状态、部署锁与 SQLite 写锁，并验证数据库完整性、文件内容与目标不覆盖条件；新服务 unit 可以尚未安装。运行数据复制后归 `keepsite` 服务账户，权限保持私有，含秘密的 `keepsite-migration` 元数据只允许 root 读取。失败时保留源数据，不把未校验结果作为有效目标。

迁移后的环境文件随数据一起保存在目标的 `keepsite-migration/keepsite.env`，包含真实秘密，须保持受限权限，不打印或提交 Git。配置转换只修改变量名及明确声明的本机路径；S3 桶、Cognito ID、凭据、Cloudflare 配置和已有云资源值保持原值。显式路径映射不负责复制外部 OCR 模型或共享缓存，管理员须先复制并核验这些资源，再启用新配置。

随后将已验证配置以 root-only 权限安装到 `/etc/keepsite.env.next`，按部署工具的计划模式检查新 release、服务、站点和数据目录。候选服务必须验证匿名拒绝、原账户及媒体可读、绑定关系、OCR/TMDB 配置和数据数量，再执行正式切换。保留旧数据、环境、unit、证书、发布目录和校验报告用于回滚；不得同时运行两个指向分叉数据库的写服务。

仅在新系统尚未接受业务写入时，才能直接恢复旧来源目录。若切换后已有新上传、修改或绑定操作，先停止新写入并备份新的业务库、认证库和媒体，再显式核对、迁回新增内容；不能直接启动过时的旧副本，把新数据丢在停用目录中。

正式激活前保存并撤出旧的 `/etc/nginx/sites-enabled/memoverse` 站点入口，核对其真实目标并保留原配置和联接信息用于回滚；不能同时启用相同域名的新旧站点，否则可能继续返回旧前端。新配置通过 `nginx -t` 及候选检查后才恢复公共访问。复制旧 DDNS 配置到 `/etc/keepsite/cloudflare.json` 时保持 root-only 权限和原 Token/区域/域名值，不打印内容；DDNS 服务的停用/启用需按维护步骤验证 DNS，不能同时留下两个自动更新者。

SSH 已知主机校验仍针对同一 EC2 的原密钥。若新调用使用 `keepsite-<instance-id>` 主机别名，应从已核验的旧 known_hosts 记录复制同一密钥到新别名，或在过渡操作中显式使用已信任的旧别名；不得为名称改变关闭严格主机校验或接受未经核对的新密钥。

源数据快照保留旧库与认证库；原备份文件原样保留。业务级 ZIP 的通用票据结构不因显示名改变，服务级备份格式改名须按新工具生成；旧服务器备份应使用对应旧版本工具检查恢复，不将其格式标签直接改写。

## Android 与 Web

新 Android 包无法直接读取旧包的私有目录或 Keystore，这不是改一个路径就能迁移的内容。保留旧应用，不卸载、不清数据；先在旧应用确认未提交原票已保存到云端，再安装新应用并重新登录同一账户。对尚未云存且原相册也不存在的私有草稿，必须先从旧应用完成保存或另行实现受用户控制的导出；本轮不声称已自动导出这些数据。确认原票和票据在新应用完整可读后，旧应用是否卸载由用户决定。

Web 新缓存名称仅影响可回收副本；旧 IndexedDB 不删除，新端通过原账号从云端恢复。未提交浏览器表单和正在传输的内容应在切换前确认完成。新旧客户端的请求头、Cookie、原生回调及 DataChannel 标识必须配套部署，不能把混用协议失败解释为账号数据消失。

## 云资源与自动关机

现有 S3 桶名、IAM role/profile、SSH key pair、Cognito 池显示名、EC2 标签和 Google OAuth 品牌不由离线工具修改。保留真实资源值能让新软件继续访问原对象；它不等于这些外部资源已经改名。若进一步改云资源名称，须单独安排资源复制、权限与对象校验、配额/费用及回滚，不能用不存在的新桶名替换原配置。Cognito issuer 不变是现有账户 ID 稳定的前提，不能为改显示名重新创建用户池。

`keepsite-auto-stop.timer` 仍为 `OnBootSec=58min`。实际维护时旧计时器应一直保持有效，直到已确认新计时器按同一次系统启动时间工作；不得用重启实例、延后时间或关闭旧定时器来延长维护窗口。超过本次期限就先结束维护，不能因改名自行续时。

## 本轮本机配置

TMDB 的新私密配置是 `.cache/server/keepsite-tmdb.env`；旧 `.cache/server/tmdb.env` 作为迁移源保留。新配置只使用 `KEEPSITE_*`，令牌通过后端请求头发送，未写入源码或 APK。2026-10-24 发布时，该令牌已配置到服务器 `/etc/keepsite.env`，实际进程启用了 TMDB。
