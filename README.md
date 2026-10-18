# MemoVerse · Web 收藏与浮雕应用

用一张照片保存一件收藏，整理分类、标签和笔记，并通过 CPU 深度推理生成可交互的浮雕模型。本轮先完成响应式 Web App；Android APK 后续独立开发，网页不再套手机模拟器。

## 当前实现

- 桌面侧栏与响应式收藏布局，手机浏览器原生表单、滚动和导航。
- Google 一键登录、邮箱密码注册、邮箱验证码和密码找回；未登录不能使用业务功能。
- 服务端会话、CSRF 与账号数据隔离；退出和换号时取消旧请求，不串用用户状态。
- FastAPI + SQLite schema 2 保存业务数据，私有 AWS S3 保存原图、GLB 与相关产物。
- 新账户首次获得独立的初始收藏；当前模板包含 17 收藏、4 分享、6 社区内容，共 18 张图片和 18 个真实模型。后续登录不重置修改。
- 原图上传、编辑删除、搜索、资料与本人完整 ZIP 导出。
- 固定 AnyImage MoGe2 ONNX CPU 推理，加项目浮雕网格与 GLB 导出；Three.js 查看器总偏转上限 40°。

探索和分享中的初始人物、推荐及互动数仍是测试内容。模型是带真实深度与纹理的浮雕，不是完整背面重建或 Blender Bake Mesh。OCR、Android 离线存储与原生近场分享另行开发。真实 Google 授权和邮件收件是否完成，以部署验收记录为准，不能由模拟接口测试代替。

## 技术栈与目录

| 目录 | 内容 |
| --- | --- |
| `demo/` | React、TypeScript、Vite、Three.js Web 前端 |
| `backend/` | FastAPI、Cognito 会话、账号 SQLite、对象存储与串行生成任务 |
| `scripts/` | 模型/模板准备、验证、构建发布与运维迁移 |
| `deploy/ubuntu/` | EC2 安装、systemd、Nginx、角色权限和安全切换 |
| `docs/` | 产品决定、设计参考、API 与验收证据 |

## 职责边界与修改入口

当前采用单体应用内的模块拆分：模块各自承担明确职责，仍共享统一鉴权、请求通道与事务规则。格式整理只改变代码外观；职责拆分应保留业务行为；恢复能力的改进则需要单独的故障验证，不能以文件变小代替验收。

| 想修改什么 | 从哪里开始 | 验证入口 |
| --- | --- | --- |
| 登录、会话、账号隔离 | `backend/auth.py`、`demo/src/AuthGate.tsx`、`demo/src/api.ts` | `backend/tests/test_auth_integration.py`、`demo/tests/api.test.mjs`、Web 账户切换测试 |
| 收藏增删改、版本冲突 | `backend/services/collections.py`、`backend/contracts.py`；前端见[责任地图](demo/README.md#代码责任地图) | `backend/tests/test_api.py`、`demo/browser/web-app.spec.ts` |
| 生成任务调度、生成流水线、结果发布 | [后端责任地图](backend/README.md#生成业务责任地图) | `backend/tests/test_jobs.py` |
| 原图与模型存储、授权读取 | `backend/object_storage.py`、`backend/store.py`、`backend/api.py` | 后端对象存储与账号隔离测试 |
| 页面路由、探索、朋友分享、展示组件 | [前端责任地图](demo/README.md#代码责任地图) | `npm run check:runtime`、`npm run test:unit`、`npm run test:web`、`npm run build`（在 `demo` 执行） |
| 浮雕展示、加载失败与正文超时 | `demo/src/features/relief/`、`demo/src/ReliefViewer.tsx`、`demo/src/api.ts` | `demo/reliability/failures.spec.ts`、角度与 API 测试 |
| 账户初始化、状态与资料 | `backend/accounts.py`、`backend/state.py`、`backend/services/profiles.py` | 后端账户测试；前端 `useAccountWorkspace.ts` 相关 Web 回归 |
| 分享、社区互动 | `backend/services/social.py`、`demo/src/features/shares/`、`demo/src/features/explore/` | API 与 Web 分享/探索回归 |
| 账户备份与隔离恢复 | `backend/backups.py`、`scripts/restore-account-backup.py` | `backend/tests/test_backup_restore.py`、真实生成闭环测试 |
| 制品准备、发布激活与回滚 | `scripts/deploy-ubuntu.ps1`、`deploy/ubuntu/prepare-release.py`、`deploy/ubuntu/activate.sh` | `deploy/ubuntu/tests/test_deployment.py`；真实服务器另行验收 |

修改前先沿对应入口追踪调用和测试。公共规则只保留一个实现：前端网络请求集中于 `api.ts`，业务写操作共用账户级协调入口；后端网络与推理不占 SQLite 写事务，最终发布保留账户、任务、输入版本和原图校验。生产目前只支持一个应用进程与一个生成 worker，模块拆分不意味着可以直接增加 worker 数量。

## 解耦与恢复验证

账户协调、收藏编辑、后台生成、业务服务、HTTP 适配、备份恢复与部署事务已经分开。`app.py` 负责装配，`api.py` 负责请求/响应，`services/` 负责业务事务，`store.py` 负责数据库与授权存取；前端账户 hook 统一管理状态与操作锁，业务组件接收所需数据和动作。

F01—F07 的实现、验收映射、复现命令及边界集中在[可靠性与解耦验收](docs/可靠性与解耦验收.md)。本分支提供有界结果发布重试、任务级取消、浮雕加载隔离、完整正文超时、发布互斥和信号恢复，并验证真实 CPU 生成、隔离备份恢复及两个账户的资源隔离。

代码审查、测试通过与 PR 合并不等于已部署。生产运行状态仍以部署验收记录和现场查询为准；CI 不启动 EC2，也不变更自动关机规则。

## 服务器与运行

正式应用运行于 AWS 东京既有 EC2 `t3.small`（2 GB），Nginx 提供 [keepsite.space](https://keepsite.space) 的 HTTPS。Cognito 管理身份，S3 管理私有文件；Google Cloud 项目只用于 Google 登录配置，不承载应用或对象存储。前端在开发机打包后部署静态文件，正式环境不依赖开发机开机。

代码按 Git 提交部署到独立目录。新账号数据位于 `/var/lib/memoverse-v2`，旧 `/var/lib/memoverse` 保留；初始化模板经独立校验，不能自动把旧全局工作区归给首个注册人。环境和站点配置变化会与代码一起受控切换，失败时恢复实际旧配置与访问控制。

- [运行信息与实例定位](.agents/skills/memoverse-delivery/references/project-context.md)
- [Ubuntu 发布与回滚](deploy/ubuntu/README.md)
- [EC2 部署验收](docs/EC2部署验收.md)
- [后端配置](backend/README.md) · [前端说明](demo/README.md) · [API 约定](docs/local-api-contract.md)

每次开机通过 Cloudflare DDNS 更新动态 IP，正常停机撤回记录。部署前按需启动，用户验收后按要求停止；停止 EC2 仍保留磁盘和 S3 数据费用。当前状态必须现场查询，不从历史 IP 或日志推断。

## 开发与验证

本机开发需先按后端说明配置身份服务、模板和显式存储模式，然后使用 `scripts/start-local.ps1`；本机 origin 必须匹配配置，不能直接复用生产环境文件。旧 IndexedDB 保留，不再自动迁移。

阶段检查涵盖后端身份/账户/对象存储与组合测试，Web 请求和运行约束、响应式浏览器交互、生产构建、初始化模板完整性以及配置切换/回滚控制流。真实服务器再验收访问控制、数据隔离、S3 文件、上传生成和 HTTPS 行为。

### PR 持续集成

每次 PR 更新及推送到 `main`，GitHub Actions 自动执行以下检查；更新同一 PR 时，旧的应用检查会被取消，避免重复占用运行资源。

| 检查 | 内容 |
| --- | --- |
| `Backend tests` | Python 3.13 安装固定测试依赖，运行完整 `backend/tests`，覆盖接口、身份与账户隔离、对象存储、任务取消/恢复和备份恢复 |
| `Frontend tests` | Node 24 + `npm ci`，执行单元测试、Web 契约检查、TypeScript/生产构建、Chromium Web 交互及生产构建 HTTP 故障测试 |
| `deployment-recovery` | Ubuntu 上运行发布恢复测试，包括真实文件锁、进程信号和版本竞争；系统服务使用隔离夹具 |

应用检查失败时保留 JUnit 报告和已有的浏览器失败 trace，保留期 7 天。后端和浏览器测试使用测试身份及隔离数据，不需要生产密钥，不访问真实 Cognito/S3。普通 CI 不下载模型权重，缺少本地 ONNX 模型的会话检查和显式开启的真实 CPU 推理测试会报告跳过；真实推理、Google/邮件与生产云服务验收仍单独执行。

## 数据与第三方组件

Git 不包含原数据库、新账号数据库、用户照片、生成模型、模型权重、依赖环境或凭据。会话密钥和 Google OAuth 密钥只保存在受限配置中。第三方来源与许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。
