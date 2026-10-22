# MemoVerse 电影票后端

M2 沿用独立 SQLite schema 3，追加持久变更记录、上传意图与存储绑定。默认数据目录是 `backend/data-v3`；正式环境使用 `/var/lib/memoverse-v3`。应用遇到旧 schema 1/2 会在写入前拒绝启动，不能把旧目录直接切给新版本。

## 当前能力与边界

- 保留 Cognito 邮箱/Google 登录、HttpOnly 服务端会话、加密令牌、精确 Origin 和 CSRF 校验；业务归属只取服务器认证身份。
- 新账号创建空票夹，无初始化模板、社交、分类、标签、徽章或浮雕任务。原 AnyImage worker 不再加载；不安装或下载旧模型。
- 票根包含片名、影院、影厅、座位和放映时间，允许为空。放映时间只接受可靠的本地 ISO 分钟格式 `YYYY-MM-DDTHH:mm`。字段来源、时间、版本、输入版本与删除墓碑单独保存。
- 按字段在线修改；同字段按服务端事务应用顺序生效，不同字段分别保留。每次写入携带稳定 `operationId`；相同操作重放返回当前状态，不同载荷复用操作标识返回 409。删除墓碑阻止旧请求复活记录。
- 原票和海报分别引用；纠错不提前撤下既有有效海报。当前不运行 OCR/海报抓取，处理状态明确为 `not_started`，`capabilities.processing=false`。OCR/Redis Streams、增量 HTTP/WebSocket 和设备 P2P 留待 M3—M5。
- 票根变更、操作幂等和 `ticket_changes` 在同一事务提交；每账号 `revision` 是持久变更游标，创建、修改、删除都保存完整票根快照，删除快照包含墓碑。上传核对不占用票根游标。`sync_baselines` 标记历史边界：M1 旧库、隔离迁移和备份恢复从其当前 revision 建立基线，不伪造之前的增量。

## 媒体与授权

上传支持解码核验的 JPG/PNG/WebP，最多 12 MiB、4000 万像素；SHA-256 寻址，不覆盖已有不同内容。S3 写入带校验和、AES256 和条件写，私有对象存储的网络 I/O 不持有 SQL 写事务。显式 `MEMOVERSE_STORAGE=local` 仅用于开发测试，不作为 S3 故障回退。

上传必须携带稳定 `uploadId`。服务端先解码照片，再将账号、上传标识、SHA-256、类型、尺寸、对象键及存储身份写入 `upload_intents`，随后才执行存储 I/O；相同标识换内容返回 409。对象成功后登记图片与待引用凭证。响应丢失、对象写入后数据库失败时，`GET /api/uploads/{uploadId}` 可核对并恢复；对象未到返回 `pending`，存储故障明确失败。客户端声称或旧 grant 都不能替代对象核验。

S3 核对使用 `HeadObject(ChecksumMode=ENABLED)` 返回的原生完整 `ChecksumSHA256`，同时核对长度、类型及元数据；校验和必须与服务端解码时的 SHA-256 相同。单次 `PutObject` 已上传相同校验和并使用 AES256；不接受缺失校验和或 multipart COMPOSITE 校验和，不通过整图重复下载来确认上传。依据：[AWS HeadObject 校验和说明](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/head_object.html)。本地开发读取有界文件并核验实际哈希。

上传响应只返回图片身份和 uploadId，不声称票根已保存。只有已提交本人有效票根引用时才可通过 `/api/media/{sha}` 读取；S3 引用显示 `saved`，本地开发显示 `not_configured`。缓存、设备和 OCR 未就绪不伪装成成功。

`asset_grants` 仅记录账号归属。`upload_receipts` 还必须对应当前存储身份下已核验的 `stored` 意图，才可创建票根或换原票；本人已有未删除票根引用也可复用。提交引用时消费凭证并将相关意图标为 `attached`。查询或重传已消费的旧 uploadId 不重新授权删除文件；显式使用新 uploadId 上传并核验后才可创建新票根。

后台每 30 秒最多核对 3 条 pending，仅覆盖创建后 24 小时内的意图，并按最后核对时间轮转；不阻塞 HTTP 启动。超过 24 小时停止自动核对，保留意图供客户端显式 `GET /api/uploads/{uploadId}` 或人工核对，避免遗失草稿永久消耗 S3 请求。S3 连接/读取超时为 3/5 秒、standard 总计最多 2 次尝试；停止时不再领取下一条，校验完成后再次检查停止和持久状态。未引用产物及 pending 意图保留以便核对，不自动删除任何云对象或历史版本。

数据库 `storage_binding` 绑定 S3 的区域、桶和区域端点，防止切换桶或从 local 切到 S3 后错误显示已保存。已有媒体却无绑定的 schema 3 数据须先做显式隔离验证/恢复，应用拒绝猜测。local 绑定为模式而非临时路径，允许验证后的完整备份搬移；上传意图与缓存仍包含完整 local 路径，旧意图不能给另一对象目录授权。

`GET /api/media/{sha}/download` 先验证本人有效引用，默认返回受保护同源流地址。仅显式 `MEMOVERSE_DIRECT_DOWNLOAD=1` 时返回 60 秒 SigV4 HTTPS GET 地址；使用明确区域 S3 端点，并限制签名 URL 主机。签名不落库或日志，不向浏览器发长期凭据；未核验桶 CORS 前保持默认服务器传输。授权失效可重新请求；已签发地址有自己的有效期，退出或删除不会保证瞬时撤销它。临时凭据先过期时，实际有效期也会提前结束。

删除提交墓碑后撤销后续业务媒体授权；另一条有效票根仍引用相同文件时继续可读。不删除 S3 对象、本地旧文件、数据库或模型缓存，云对象和历史版本回收须另行授权。

## Redis 热点快照

`MEMOVERSE_REDIS_URL` 可选，支持 redis/rediss/unix URL。未配置、断连、超时或内容损坏均回数据库；SQL 是持久事实。缓存键隔离账号、serverId、revision 和完整存储身份，默认 TTL 60 秒、单快照上限 8 MiB。缓存前后校验票根和归属，恢复空间或存储变化不会串用旧缓存。

Redis I/O 只发生在完成的 SQL 事务之间；写操作直接提交数据库，不等待 Redis。连接和读超时均为 0.5 秒、零自动重试，生命周期关闭连接池。缓存 miss 的快照使用一次批量媒体查询，避免每张票单独查资产。该快照缓存不代表 Redis Streams 或在线设备状态已经完成。

## HTTP 契约

| 接口 | 请求/行为 |
| --- | --- |
| `GET /api/state` | 当前账号完整票夹快照；不包含墓碑 |
| `GET /api/operations/{operationId}` | `{operationId, applied}`；仅核对当前账号持久操作，未知和其他账号的操作均返回 `applied:false` |
| `POST /api/media` | multipart `uploadId` 和 `file`；返回 assetId、sha256、URL、类型、尺寸及 uploadId |
| `GET /api/uploads/{uploadId}` | `{uploadId,status:pending或stored或attached,asset}`；pending 时 asset 为 null，未知/其他账号返回 404 |
| `GET /api/media/{sha}` | 仅本人有效票根引用可读 |
| `GET /api/media/{sha}/download` | `{url,expiresAt,transport,sha256,size,mimeType}`；期限为毫秒时间戳，transport 为 server 或 s3 |
| `POST /api/tickets` | `{id, operationId, originalAssetId, fields:{...}}` |
| `PATCH /api/tickets/{id}` | `{operationId, fields?:{...}, originalAssetId?}`，只修改显式字段 |
| `DELETE /api/tickets/{id}` | `{operationId}`；持久墓碑 |
| `GET /api/backup` | 当前账号 schema 3 ZIP，不包含认证数据库或其他账号 |

快照及票根写响应统一为 `{initialized, serverId, userId, revision, data:{tickets}, capabilities}`。`revision` 仍是全量快照防旧值标记，不代表已实现增量同步。

写响应丢失时先查询稳定操作标识是否已应用，再决定是否重试。当前图片可能已被另一端替换，不能用当前图片哈希推断先前写入是否成功；操作查询不返回请求内容或其他账号信息。

## 迁移与备份

旧数据通过仓库根目录的 `scripts/migrate-movie-tickets.py` 检查清单并显式迁移到新目录。输入是独立 schema 2 数据库快照和本地原媒体副本；有活动 WAL/journal 的源拒绝。先使用工具的计划模式，`--execute` 才执行隔离迁移。迁移保留源快照与报告，不覆盖旧库、不连接 S3、不删除旧文件。非电影资料保留在旧快照，日期不猜成放映时间；字段来源标为 `migration`。

schema 3 账户备份包含本人票根、墓碑、操作幂等记录与媒体元数据，仅打包仍被有效票根引用的媒体字节。删除专用文件不通过备份重新开放下载。恢复保留墓碑并拒绝重新引用只有墓碑的媒体；新 `serverId` 标识恢复空间，当前 revision 为新同步基线，不重放原空间历史。未引用上传及 pending 意图不是已确认票根，不进入账户备份恢复保障。本地恢复媒体显示 `not_configured`，不会冒称已在 S3 保存。

```powershell
backend/.venv/Scripts/python.exe -B scripts/restore-account-backup.py --archive D:/backups/account.zip --destination D:/backups/restored-account --user-id <原账号ID>
```

目标必须是父目录已存在、没有链接的新绝对目录。恢复验证 ZIP 成员、归属、记录契约、引用、图片类型/尺寸/SHA-256 和 SQLite 完整性；限制 50,000 个成员、2 GiB 展开大小和 16 MiB manifest。禁止覆盖、合并或重新分配原账号。发布中断留下 `RESTORE_INCOMPLETE`，应用会拒绝启动该目录。schema 2 历史备份不由新在线恢复接口解释，先按隔离迁移清单处理；保留历史原始备份。

## 配置与验证

必需环境配置包括明确的 `MEMOVERSE_STORAGE`，S3 模式的 `MEMOVERSE_S3_BUCKET`/`AWS_REGION`，以及既有 Cognito、公共 Origin、会话加密密钥。`MEMOVERSE_TEMPLATE_DIR` 已退出。认证协议配置见 `backend/auth.py`；不得把凭据写入 Git。运行定位与发布边界见[项目运行信息](../.agents/skills/memoverse-delivery/references/project-context.md)。

Python 3.12/3.13，依赖使用 `backend/.venv`。测试均在隔离目录，认证/S3 为显式 fixture；真实身份服务及真机验收另行执行。

```powershell
backend/.venv/Scripts/python.exe -B -m pytest backend/tests scripts/tests/test_ticket_migration.py scripts/tests/test_capacity_benchmark.py --basetemp .cache/pytest-movie-tickets-unique -q
```

测试目录必须使用新的任务专属路径。核心覆盖空账号、真实 BFF/CSRF、双账号隔离、字段更新、幂等、删除、上传故障恢复、变更日志原子性、备份基线、Redis 丢失/故障降级及纯本地 SDK 签名。fakeredis 不代替真实 Redis，真实 Redis 测试由 CI 提供服务，本机缺连接时显式跳过；没有真实 S3、OCR 或设备同步验收结论。
