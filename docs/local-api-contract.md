# Web App API 约定

当前为账号隔离 API（schema 2），由网站 HTTPS 同源 `/api` 提供。未登录不能访问业务功能。身份由服务器会话决定，不能通过 URL、body 或前端过滤指定任意 owner。旧单用户 `/api/bootstrap` 已移除，旧 IndexedDB 不自动迁移。

## 认证入口

| 请求 | 输入 / 返回 |
| --- | --- |
| `GET /api/auth/session` | `{authenticated, configured, providers:{google,email}, user?:{id,email,name}, csrfToken?}` |
| `POST /api/auth/register` | `{email,password}` → `{verificationRequired:true,email}` |
| `POST /api/auth/verify-email` | `{email,code}` → `{verified:true}` |
| `POST /api/auth/resend-code` | `{email}` |
| `POST /api/auth/login` | `{email,password}` → session 结构及 HttpOnly Cookie |
| `POST /api/auth/forgot-password` | `{email}`；不泄露账号是否存在 |
| `POST /api/auth/reset-password` | `{email,code,password}` |
| `POST /api/auth/logout` | `{ok:true,logoutUrl?}`；本地会话立即失效，旧媒体链接不可继续访问 |
| `GET /api/auth/google` | 跳转 Google 联合登录流程，设置短期绑定 Cookie |
| `GET /api/auth/callback` | 成功后返回网站 `/`；失败清绑定 Cookie，固定返回 `/?auth_error=google_login_failed`，不回显提供方错误或查询参数 |

认证写请求要求精确 Origin 和 `X-MemoVerse-Client: web`。已登录的业务写操作还要求 `X-CSRF-Token`，注销现有会话同样验证 CSRF。密码不 trim，当前策略为 12–128 字符且包含大小写字母、数字与符号。未验证邮箱拒绝业务使用。

错误结构为 `{detail:string, code?:string}`；未认证 401、邮箱未验证或 CSRF 403、限流 429、上游暂不可用 503。验证码与令牌不能进入前端持久存储、访问日志或错误信息。

## 业务状态

`GET /api/state` 及成功修改返回 `{initialized,serverId,userId,revision,data,generationEngine}`。data 保持 collections / shares / community / profile / likedCommunityIds。revision 属于当前账户；客户端必须同时核对 userId 与会话代次，丢弃旧账户迟到结果。首次认证由服务器幂等复制已批准模板，不由浏览器上传全局旧工作区。

收藏、分享内容和社区内容可返回 `assetId`、`customImage`、`modelId`、`modelUrl`、`modelCamera`、`generation`；收藏另有 `version` 和任务状态字段。模型 ready 必须已有真实对象。媒体链接均为同源受保护 `/api/...` 路径，不能因为知道哈希或模型 ID 就跳过授权。

`GET /api/health` 只返回最小 `{status:'ok'}`，用于服务检查；详细生成状态随已认证业务状态返回。

## 业务接口

| 请求 | 行为 |
| --- | --- |
| `POST /api/media` | multipart file，校验并保存私有原图，返回 `{assetId,url,sha256,size,width,height,mimeType}`；最多 12 MiB |
| `GET /api/media/{assetId}` | 仅当前账户有授权的原始图片 |
| `POST /api/collections` | `{id,title,category,tags,note,date,art,assetId}`；草稿 ID 仅在当前账号内幂等；tags=null 时匹配本人历史名称 |
| `PATCH /api/collections/{id}` | `{version,title,category,tags,note,date?,art,assetId?}`；版本冲突 409，纯文字编辑复用模型 |
| `DELETE /api/collections/{id}` | `{version}`；取消对应未完成任务，不删除其他记录仍引用的文件 |
| `POST /api/collections/{id}/retry-generation` | 重试当前账户自己的失败/阻塞任务 |
| `GET /api/jobs/{jobId}` | 仅当前账户自己的任务 |
| `GET /api/models/{modelId}/model.glb` | 仅有模型授权的账户可读取真实 GLB |
| `PATCH /api/profile` | `{name,bio}`，只修改本人资料 |
| `POST /api/shares/read-all`、`POST /api/shares/{id}/read` | 当前账户的示例分享已读状态 |
| `POST /api/shares/{id}/collect` | 保存当前账户独立收藏副本，重复请求不重复添加 |
| `POST /api/community/{id}/view` | 当前账户测试内容浏览计数 |
| `PUT /api/community/{id}/like` | `{liked:boolean}`，幂等更新本人互动 |
| `POST /api/community/{id}/collect` | 保存独立收藏，复用已存在的不可变模型 |
| `GET /api/backup` | 本人数据、原图和模型的完整 ZIP，包含本人模板分享/社区引用；不包含全站数据库或身份令牌 |

图片与模型从私有对象存储经认证后端流式返回，退出后同一 URL 返回 401。对象传输不持 SQLite 写事务；上传、生成或对象存储失败不能报告 ready。全站迁移和备份是运维操作，不是普通用户接口。
