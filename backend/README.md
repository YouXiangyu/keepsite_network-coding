# MemoVerse 电影票后端

M1 使用独立 SQLite schema 3、账号隔离和私有媒体引用。默认数据目录是 `backend/data-v3`；正式环境使用 `/var/lib/memoverse-v3`。应用遇到旧 schema 1/2 会在写入前拒绝启动，不能把旧目录直接切给新版本。

## 当前能力与边界

- 保留 Cognito 邮箱/Google 登录、HttpOnly 服务端会话、加密令牌、精确 Origin 和 CSRF 校验；业务归属只取服务器认证身份。
- 新账号创建空票夹，无初始化模板、社交、分类、标签、徽章或浮雕任务。原 AnyImage worker 不再加载；不安装或下载旧模型。
- 票根包含片名、影院、影厅、座位和放映时间，允许为空。放映时间只接受可靠的本地 ISO 分钟格式 `YYYY-MM-DDTHH:mm`。字段来源、时间、版本、输入版本与删除墓碑单独保存。
- 按字段在线修改；同字段按服务端事务应用顺序生效，不同字段分别保留。每次写入携带稳定 `operationId`；相同操作重放返回当前状态，不同载荷复用操作标识返回 409。删除墓碑阻止旧请求复活记录。
- 原票和海报分别引用；纠错不提前撤下既有有效海报。M1 不运行 OCR/海报抓取，处理状态明确为 `not_started`，`capabilities.processing=false`。Redis、上传意图恢复、浏览器持久缓存、增量/WebSocket 和设备 P2P 留待 M2—M5。

## 媒体与授权

上传支持解码核验的 JPG/PNG/WebP，最多 12 MiB、4000 万像素；SHA-256 寻址，不覆盖已有不同内容。S3 写入带校验和、AES256 和条件写，私有对象存储的网络 I/O 不持有 SQL 写事务。显式 `MEMOVERSE_STORAGE=local` 仅用于开发测试，不作为 S3 故障回退。

上传返回图片身份，不声称票根已保存。只有已提交本人有效票根引用时才可通过 `/api/media/{sha}` 读取；S3 引用显示 `saved`，本地开发显示 `not_configured`。缓存、设备和 OCR 未就绪不伪装成成功。

`asset_grants` 仅记录账号归属。`upload_receipts` 记录上传已验证且待业务引用；创建票根或换原票要求有效上传凭证或本人已有未删除票根引用，提交时消费凭证。删除后仅有 grant 不能新建记录重新挂回旧文件；显式重新上传并核验后可建立新票根。该凭证不是 M2 的完整可恢复上传意图。

删除提交墓碑后撤销后续业务媒体授权；另一条有效票根仍引用相同文件时继续可读。M1 不删除 S3 对象、本地旧文件、数据库或模型缓存，云对象和历史版本回收由后续策略处理。

## HTTP 契约

| 接口 | 请求/行为 |
| --- | --- |
| `GET /api/state` | 当前账号完整票夹快照；不包含墓碑 |
| `GET /api/operations/{operationId}` | `{operationId, applied}`；仅核对当前账号持久操作，未知和其他账号的操作均返回 `applied:false` |
| `POST /api/media` | multipart `file`；返回 assetId、sha256、URL、类型、尺寸 |
| `GET /api/media/{sha}` | 仅本人有效票根引用可读 |
| `POST /api/tickets` | `{id, operationId, originalAssetId, fields:{...}}` |
| `PATCH /api/tickets/{id}` | `{operationId, fields?:{...}, originalAssetId?}`，只修改显式字段 |
| `DELETE /api/tickets/{id}` | `{operationId}`；持久墓碑 |
| `GET /api/backup` | 当前账号 schema 3 ZIP，不包含认证数据库或其他账号 |

快照及票根写响应统一为 `{initialized, serverId, userId, revision, data:{tickets}, capabilities}`。`revision` 仍是全量快照防旧值标记，不代表已实现增量同步。

写响应丢失时先查询稳定操作标识是否已应用，再决定是否重试。当前图片可能已被另一端替换，不能用当前图片哈希推断先前写入是否成功；操作查询不返回请求内容或其他账号信息。

## 迁移与备份

旧数据通过仓库根目录的 `scripts/migrate-movie-tickets.py` 检查清单并显式迁移到新目录。输入是独立 schema 2 数据库快照和本地原媒体副本；有活动 WAL/journal 的源拒绝。先使用工具的计划模式，`--execute` 才执行隔离迁移。迁移保留源快照与报告，不覆盖旧库、不连接 S3、不删除旧文件。非电影资料保留在旧快照，日期不猜成放映时间；字段来源标为 `migration`。

schema 3 账户备份包含本人票根、墓碑、操作幂等记录与媒体元数据，仅打包仍被有效票根引用的媒体字节。删除专用文件不通过备份重新开放下载。恢复保留墓碑并拒绝重新引用只有墓碑的媒体；新 `serverId` 标识恢复空间。本地恢复媒体显示 `not_configured`，不会冒称已在 S3 保存。

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

测试目录必须使用新的任务专属路径。核心覆盖空账号、真实 BFF/CSRF、双账号隔离、字段更新、幂等重放、删除与引用授权、上传故障、备份校验和隔离恢复。没有真实 OCR、Redis 或设备同步验收结论。
