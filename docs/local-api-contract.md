# 本机 API 实施约定

本轮：FastAPI + SQLite + 本地媒体文件，仅监听 `127.0.0.1:8000`；前端仍在 `127.0.0.1:4173`。不连接远端服务器。此接口是单用户本机工作区，尚无正式账户认证，不可直接公开部署。

## 读写约定

- 原有 `AppData` 展示结构保持：collections / shares / community / profile / likedCommunityIds。新增收藏字段 `assetId?: string`、`version: number`、`generation: reference | queued | processing | blocked | ready | failed`、`generationJobId?: string`、`generationError?: string`、`modelUrl?: string`。服务端返回的 `customImage` 是原图 HTTP URL。
- `GET /api/state` 返回 `{initialized, serverId, revision, data: AppData | null}`。所有业务修改成功统一返回同样的完整状态；前端按 revision 丢弃晚到的旧响应。
- 业务修改请求携带 `X-MemoVerse-Client: local`。JSON 错误使用 `{detail: string}`，冲突 409、缺失 404、校验 422、过大 413、未初始化 409。前端必须等成功后更新状态、关闭表单及提示；失败保留草稿。
- `GET /api/health` 返回 `{status, mode: 'local', database: 'sqlite', storage: 'local-files', generation: {engine: 'AnyImage', available: boolean, reason: string}}`。

## 首次迁移

- `POST /api/bootstrap`：`{migrationId: string, data: 原AppData}`。只有空工作区能导入；同一 migrationId 重试返回已有结果，不覆盖后续修改。非空目标收到其他 migrationId 返回 409。
- 前端启动读取旧 IndexedDB 快照；在同一数据库另一个键中原子建立稳定 migrationId，保持原 `current` 不动。后端将旧图片 Data URL 解码为真实文件，保留 ID、时间、来源、分享与资料。提交成功后前端记录 serverId 的迁移完成标记。
- 没有旧数据且服务端未初始化时才允许用 makeSeedData 初始化。读取失败、目标冲突不得静默重置、合并或用 seed 覆盖。非空后端与无旧数据的新浏览器直接读取现有后端。

## 图片与收藏

| 请求 | 输入与行为 |
| --- | --- |
| `POST /api/media` | multipart 字段 file；返回 `{assetId,url,sha256,size,width,height,mimeType}`。真实解码校验 JPG/PNG/WebP，12 MiB 上限；内容哈希去重，原图落盘。 |
| `GET /api/media/{assetId}` | 原始图片文件 |
| `POST /api/collections` | `{id,title,category,tags: string[] | null,note,date,art,assetId}`；新个人收藏必须上传真实图片；id 是前端草稿中稳定 UUID。同 ID 同输入重试幂等，差异输入冲突。tags=null 使用历史名称匹配，否则保留传入标签。 |
| `PATCH /api/collections/{id}` | `{version,title,category,tags,note,date?,art,assetId?}`；version 必填作并发保护。assetId 省略保留原图。纯文字编辑复用生成任务与模型；换图/视觉模板才产生新版本任务。 |
| `DELETE /api/collections/{id}` | JSON `{version}`；删除条目并取消待执行任务，旧任务不能复活条目。朋友独立副本保留。 |
| `POST /api/collections/{id}/retry-generation` | 重试当前图片任务；未配置服务时清楚显示 blocked，不假报完成。 |
| `GET /api/jobs/{jobId}` | `{id,collectionId,status,error,modelUrl?,createdAt,updatedAt,inputRevision}` |

## 其他本地业务

- `PATCH /api/profile`：`{name,bio}`。
- `POST /api/shares/{id}/read`、`POST /api/shares/read-all`：已读。
- `POST /api/shares/{id}/collect`：保存独立副本，重复请求不重复添加。
- `POST /api/community/{id}/view`：示例内容本地浏览计数。
- `PUT /api/community/{id}/like`：`{liked:boolean}`，幂等设置。
- `POST /api/community/{id}/collect`：收藏示例内容，计数只增加一次。
- `GET /api/backup`：导出含元数据与完整媒体文件的 ZIP；前端通过 fetch 处理失败后再触发下载。

## 生成边界

真实图片持久化后自动创建数据库任务。任务持有不可变图片标识和输入版本；完成写回前再次核对收藏仍存在且输入版本相同。后台单任务执行，避免同时加载多个模型；重启恢复中断任务。服务未就绪时保留 blocked，服务恢复自动继续。AnyImage 的实际自动化入口由验证结果决定，不能用灰度亮度位移或 CSS 旋转冒充 AnyImage。

完整图片、模型与必要纹理保存于项目 D 盘数据目录；旧 IndexedDB 保留为迁移前备份。云同步、正式认证、Android 原生存储与近场网络不属于此次本机 API 接通的完成声明。
