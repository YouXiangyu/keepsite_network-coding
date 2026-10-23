# MemoVerse 电影票后端

M4 沿用独立 SQLite schema 3，通过固定分页快照、持久增量和 WebSocket 水位提示同步本人票夹。默认数据目录是 `backend/data-v3`；正式环境使用 `/var/lib/memoverse-v3`。应用遇到旧 schema 1/2 会在写入前拒绝启动，不能把旧目录直接切给新版本。

## 当前能力与边界

- 保留 Cognito 邮箱/Google 登录、HttpOnly 服务端会话、加密令牌、精确 Origin 和 CSRF 校验；业务归属只取服务器认证身份。
- 新账号创建空票夹，无初始化模板、社交、分类、标签、徽章或浮雕任务。原 AnyImage worker 不再加载；不安装或下载旧模型。
- 票根包含片名、影院、影厅、座位和放映时间，允许为空。放映时间只接受可靠的本地 ISO 分钟格式 `YYYY-MM-DDTHH:mm`。字段来源、时间、版本、输入版本与删除墓碑单独保存。
- 按字段在线修改；同字段按服务端事务应用顺序生效，不同字段分别保留。每次写入携带稳定 `operationId`；相同操作重放返回已确认的同步屏障，不同载荷复用操作标识返回 409。删除墓碑阻止旧请求复活记录。
- 原票和海报分别引用；纠错不提前撤下既有有效海报。配置私有 S3、Redis 与固定 OCR 模型后，自动登记 OCR/海报任务；缺少条件时 `capabilities.processing=false`，并通过 `processingUnavailableReason` 说明原因，不伪造识别结果。设备 P2P 留待 M5。
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

Redis I/O 只发生在完成的 SQL 事务之间；写操作直接提交数据库，不等待 Redis。连接和读超时均为 0.5 秒、零自动重试，生命周期关闭连接池。缓存 miss 使用批量媒体、处理状态和出处查询，避免每张票单独查资产。完整能力配置参与缓存身份，未配置原因变化不会复用旧提示。在线设备状态仍未实现。

## OCR 与海报处理

`processing_jobs` 与创建/修改票根在同一事务登记。原票或用户片名/放映时间变化时推进 `inputVersion`、取消旧任务并登记新任务；处理和有限重试期间保留既有海报，最终 `no_match` 或 `failed` 时在同一事务撤销旧海报引用与来源、发布增量并恢复原票封面，不直接删除存储对象。更换原票时清空旧 OCR 来源字段，保留用户及迁移来源。OCR 只返回五字段候选，非空可靠结果才能写入，不能覆盖 user/migration 来源；全文不持久保存或写日志。

任务由 Redis Streams 消费者组投递，消息仅含 jobId。数据库每 10 秒允许补投未完成任务，队列删除、入队中断和重复消息不丢处理意图；消费者通过 90 秒 SQL 租约、递增 token、当前 job、账号、原图和 inputVersion 校验发布资格。计算/网络均在写事务外，每任务最多 3 次尝试、总窗口 10 分钟。即使 Redis 持续失联，过期任务也由独立 SQL 检查收敛为 failed。原票保存响应不等待 OCR。

每次 OCR 尝试使用独立 spawn 子进程，最多 45 秒；停止/超时终止并回收进程和管道。子进程在导入 OCR 之前限定 OMP/OpenBLAS/MKL 线程为 1，OCR 引擎也使用单 CPU 线程；每任务包含模型冷启动成本。模型目录由 `MEMOVERSE_OCR_MODEL_DIR` 明确指定，固定依赖、文件与字典哈希见 `ocr-models.json`，不在运行时下载模型。

85 秒尝试预算和停止信号在阶段边界、流读取返回后检查；它们不是整个 S3 网络操作的硬截止。botocore 的 5 秒读取超时针对 socket 等待，持续低速且不空闲的流可能让一次聚合读取超过该墙钟预算。当前不通过私有 StreamingBody 接口或强杀 Python 线程绕过限制；只有 OCR 子进程能在 45 秒硬终止。SQL 租约/输入版本仍拒绝超期结果发布，服务关停不延长 EC2 倒计时。真实 S3 慢流耗时和退出行为属于后续测量边界，不能由 fixture 测试宣称已经保证严格端到端超时。

海报只使用经过严格匹配与许可校验的 Wikimedia Commons 来源；无可信匹配以 no_match 结束，不自动扩展其他站点。海报 bytes 再次解码核验后，在上传前将 descriptor、出处和临时文件名写入 `processing_artifacts`。S3 保存并核验后才发布引用；最终 SQL 发布失败时，下一次租约先核对同一产物，不重跑 OCR 或重新抓取。当前海报出处保存在 `poster_sources`，返回 provider、sourcePage、sourceFile、licenseName、licenseUrl、author、fetchedAt。

处理中间海报只放在数据目录的 `tmp/processing`，原票始终来自已验证的私有对象。S3 核验后立即清除该临时副本；未完成副本、取消/失败及崩溃遗留孤儿采用 24 小时保留期，每轮最多扫描 200 项，活动任务仍引用的文件不清理。逐段拒绝 tmp/processing 父链上的符号链接和 junction，记录清理原因与时间；不删除 S3 对象或有效图片。服务退出先停止领取任务，在调用边界结束当前工作，再由 worker 的退出路径关闭海报客户端；等待返回后仍存活的线程不被并发关闭 provider。

处理状态为 `not_started`、`queued`、`ocr`、`poster`、`ready`、`no_match`、`failed`，error 为 null 或安全文案。每次处理状态变化均推进票根 version/updatedAt、账号 revision 并写持久变更。任务失败、删除、纠错、失效租约均不能复活旧结果。

## HTTP 契约

| 接口 | 请求/行为 |
| --- | --- |
| `GET /api/sync/snapshot` | 固定水位的分页快照，参数 `limit`、`pageToken`；不包含墓碑 |
| `GET /api/sync/changes` | 参数 `serverId`、`after`、`limit`；连续的 upsert/delete 增量 |
| `GET /api/operations/{operationId}` | `{serverId,operationId,applied,committedCursor}`；未知/其他账号为 `false/null` |
| `POST /api/media` | multipart `uploadId` 和 `file`；返回 assetId、sha256、URL、类型、尺寸及 uploadId |
| `GET /api/uploads/{uploadId}` | `{uploadId,status:pending或stored或attached,asset}`；pending 时 asset 为 null，未知/其他账号返回 404 |
| `GET /api/media/{sha}` | 仅本人有效票根引用可读 |
| `GET /api/media/{sha}/download` | `{url,expiresAt,transport,sha256,size,mimeType}`；期限为毫秒时间戳，transport 为 server 或 s3 |
| `POST /api/tickets` | `{id, operationId, originalAssetId, fields:{...}}` |
| `PATCH /api/tickets/{id}` | `{operationId, fields?:{...}, originalAssetId?}`，只修改显式字段 |
| `DELETE /api/tickets/{id}` | `{operationId}`；持久墓碑 |
| `POST /api/tickets/{id}/process` | `{operationId}`；显式重试，稳定操作标识幂等，返回同步回执 |
| `GET /api/backup` | 当前账号 schema 3 ZIP，不包含认证数据库或其他账号 |

票根 CRUD/process 写响应统一为 `{serverId,operationId,committedCursor}`，不再返回整份票夹。`committedCursor` 是该操作被确认已应用时的账号水位，重放可返回更大的水位，不伪造历史操作的原始提交游标。serverId 与水位在同一 SQL 事务读取；恢复空间改变时，不能用旧空间较大的数字满足新空间回执。客户端先核对空间，再从自己已应用的旧游标连续补到屏障，不能直接跳到 committedCursor。

写响应丢失时先查询稳定操作标识是否已应用，再决定是否重试。当前图片可能已被另一端替换，不能用当前图片哈希推断先前写入是否成功；操作查询不返回请求内容或其他账号信息。

## 分页、增量与实时提示

快照响应为 `{serverId,userId,snapshotId,cursor,total,offset,tickets,nextPageToken,expiresAt,capabilities}`。一次捕获固定水位 C 和完整内容，内部使用同一短读事务、每批 200 行逐票累计字节，随后释放事务；分页读取不可变视图，不重新读 live 表。所有页的 snapshotId/C/total 一致，客户端校验连续 offset，收齐后从 C 补拉后续变化。没有跨 HTTP 请求的长 SQLite 事务。

默认每页 100、最多 200 条和 1 MiB；单视图最多 10,000 票/16 MiB，TTL 最多 10 分钟。每账号最多 4 个未完成视图，当前单进程实例最多 32 个、保留序列化内容预算 128 MiB；构造中的视图也预留预算。数字是内容字节配额，不是进程 RSS 上限。超限明确失败，不截断记录。最后一页返回时立即释放视图与 token，单页/空票夹反复刷新不会积累配额；最后一页响应丢失后须重新建快照，中间页在有效期内可重放。

增量响应为 `{serverId,userId,fromCursor,nextCursor,headCursor,hasMore,changes,capabilities}`。upsert 为 `{cursor,type:'upsert',ticket}`，删除为 `{cursor,type:'delete',id,version,deletedAt}`。数据库仍只保留同一份完整 `ticket_changes` 日志，删除在 wire 上精简；不会维护第二套增量事实。只允许 `B <= after <= H`，逐游标验证连续性，分批推进 nextCursor，不以 headCursor 代替尚未收到的变更。

日志保留窗口为 7 天或每账号最新 10,000 条。清理只删除游标连续前缀，并在同一事务推进 `sync_baselines` 下界 B；票根墓碑、操作幂等记录及云对象不随日志清理删除。不会为分页视图无限保留旧日志；若读取期间 B 已超过 C，客户端重新建快照。失效/非法/跨账号 pageToken 统一返回 410 `snapshot_expired`；游标过期、空间改变、游标超前或日志缺口分别为 410 `cursor_expired`、`server_changed`、`cursor_ahead`、`cursor_gap`，格式 `{detail:{code,message}}`。其他错误保留 detail 字符串。

`WS /api/sync/events` 的 `sync.ready`/`sync.hint` 只发水位元数据，不推图片或完整票夹。Store 在 SQL commit 成功并关闭连接后调用快速排队钩子；Redis Pub/Sub 是可丢失提示，断连/重连与周期 SQL 水位校准负责修复漏通知，持久事实始终由增量 HTTP 获取。旧 `/api/state` 已移除，历史全量轮询对照仅在明确的旧 commit 上运行。

WebSocket 不经过 HTTP middleware，因此单独复用 `AuthService.websocket_session`：精确 Origin、HttpOnly 会话 Cookie、原有 JWT/刷新/绝对到期规则；URL 不接受令牌或账号参数。每次提示前重新核验会话，退出/换号在本地删除会话提交后、上游 revoke 前立即通知连接关闭；跨进程还依赖定期复查。401/403 关闭 1008，临时认证不可用关闭 1013，不将服务暂时失败当成退出。启动失败也按生命周期释放已注册通知、线程、缓存视图与客户端。

## 本人页面 P2P 信令

同一 WebSocket 在客户端显式发送 `device.register` 后启用 `device.*`、`cache.*`、`peer.*` v1 消息；未登记的旧客户端仍只收到原样的 `sync.ready`/`sync.hint`。每个在线页面获得随机 `peerId`，同浏览器多标签页分别登记，重连重新生成，不保存设备 SQL 表、稳定设备 ID 或硬件指纹。`capabilities.deviceSync=true` 表示软件支持；实时是否可用以 `device.registered.enabled/reason` 为准。

客户端消息固定含 `{v:1,type,requestId}`，requestId 为 1—64 位字母、数字、下划线或连字符。服务器补充 `serverId/userId`；客户端身份字段、额外字段、重复 JSON key、非整数数字和二进制帧拒绝。`device.register{name}`、`device.heartbeat`、`device.list`、`device.unregister` 管理本页面短租约；心跳 15 秒，在线有效期 45 秒。注销 P2P 不关闭同步 WebSocket。

缓存清单只表示可尝试的来源，不构成文件权限。`cache.replace{revision,batchIndex,batchCount,assetIds}` 每批最多 32 个 SHA-256、最多 32 批/1,024 项，10 秒内完整收到且 SQL 授权有效才原子替换。`cache.patch{revision,add,remove}` 一次最多增减 32 项。新登记版本为 0，下一次修改使用当前版本加一；全量替换各批共用目标版本并顺序等待 ACK。ACK 为 `{revision,count,complete,nextBatchIndex}`；未完成时不会改变已发布版本，完成时 nextBatchIndex 为 null。完全相同的上一批可重试，最近一次完整操作回执保留 10 秒；乱序、旧版本或内容冲突明确拒绝。

`peer.find{assetId}` 查询同账号来源，`peer.open{assetId,targetPeerId}` 创建服务端 transferId；来源收到 `peer.request`，核对本机文件后 `peer.accept`，双端收到 `peer.authorized` 后才能协商。授权绑定双方页面、双方会话和单个 asset；media 固定为 `{assetId,sha256,size,mimeType}`，无 S3 URL 或票根正文。请求方创建 offer，`peer.signal` 的 kind 仅 offer/answer/ice；offer/answer payload 为 `{type,sdp}`，ICE 为标准 candidate 字段对象或 null。`peer.renew` 续期，`peer.cancel` 终止。每次操作、递送和后台复查都重新核验双方登录会话及 SQL `referenced_asset`；内部只使用会话摘要，复用同一刷新锁与绝对到期规则。

传输等待接受最多 5 秒，双端分别每 5 秒续期，授权有效期取双方租约、会话和在线期限的最小值，最多 10 秒；单次硬上限 90 秒。每 peer 的来源与请求任务合计最多 2 条，每账号 8 条，每进程 32 条。单连接每秒 20 条、突发 40 条；find/open 另限每秒 4 次、突发 8 次。控制帧及 sync 消息最多 4 KiB，信令总帧 64 KiB、SDP 文本 48 KiB、单 ICE 2 KiB、双端累计 128 个 ICE 和 512 KiB 信令。单 writer 保持同步提示优先；P2P 输出最多 32 条/256 KiB，慢消费者关闭连接。

Redis 保存可丢失的登记、清单和传输租约；专用 PubSub 队列不与可合并的同步提示混用。Redis 缺失、断连、状态丢失或信令队列溢出时取消本代传输并回退 S3，恢复后重新登记和上报，不维护进程本地 P2P 兼容状态。关闭和启动失败会清理自有线程、队列、订阅和租约，不关闭借用的共享 Redis 客户端。

删除最后有效引用后拒绝新的发现、接受、信令与续期，并主动通知取消；同 hash 仍有其他有效票根引用时保留权限。已发送字节不能收回；前端须在删除、退出、换号、失联和租约到期时关闭 DataChannel。S3 兜底继续使用 `GET /api/media/{assetId}/download`，获得地址不代表对象读取成功，完成读取和哈希校验才算恢复。

当前 `iceServers=[]`、`turnEnabled=false`，只支持 host candidate 可达范围的直连尝试；跨公网失败使用 S3。不配置 STUN 服务或第三方 TURN，不自建中继；免费 TURN 条件和强制中继验收仍未完成。

## 迁移与备份

旧数据通过仓库根目录的 `scripts/migrate-movie-tickets.py` 检查清单并显式迁移到新目录。输入是独立 schema 2 数据库快照和本地原媒体副本；有活动 WAL/journal 的源拒绝。先使用工具的计划模式，`--execute` 才执行隔离迁移。迁移保留源快照与报告，不覆盖旧库、不连接 S3、不删除旧文件。非电影资料保留在旧快照，日期不猜成放映时间；字段来源标为 `migration`。

schema 3 账户备份包含本人票根、墓碑、操作幂等记录与媒体元数据，仅打包仍被有效票根引用的媒体字节。删除专用文件不通过备份重新开放下载。恢复保留墓碑并拒绝重新引用只有墓碑的媒体；新 `serverId` 标识恢复空间，当前 revision 为新同步基线，不重放原空间历史。未引用上传及 pending 意图不是已确认票根，不进入账户备份恢复保障。本地恢复媒体显示 `not_configured`，不会冒称已在 S3 保存。

备份保存当前海报出处和处理状态摘要，不复制 Redis 消息、租约、未引用处理产物或临时文件。恢复为 LocalObjectStore 时，处理中和 ready 摘要明确改为 failed、提示显式重试；已恢复图片和 OCR 字段继续可读，但不冒称新空间已完成云端处理。

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

测试目录必须使用新的任务专属路径。核心覆盖认证隔离、字段/幂等/墓碑、上传核对、持久变更、备份、Redis 降级和 SDK 离线签名，以及 Streams 丢失/重复、租约 fencing、有限重试、OCR 子进程超时、海报发布恢复和临时清理边界。fakeredis 不代替真实 Redis，真实 Redis/Streams 测试由 CI 提供服务，本机缺连接时显式跳过。真实 OCR 合成样本测量与 Commons 抓取证据见开发计划；它们不代替真实票据准确率、t3.small、S3 或设备同步验收。
