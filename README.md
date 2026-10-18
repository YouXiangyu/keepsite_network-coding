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
| 收藏增删改、版本冲突 | `backend/app.py`、`backend/store.py`；前端见[责任地图](demo/README.md#代码责任地图) | `backend/tests/test_api.py`、`demo/browser/web-app.spec.ts` |
| 生成任务调度、生成流水线、结果发布 | [后端责任地图](backend/README.md#生成业务责任地图) | `backend/tests/test_jobs.py` |
| 原图与模型存储、授权读取 | `backend/object_storage.py`、`backend/store.py`、`backend/app.py` | 后端对象存储与账号隔离测试 |
| 页面路由、探索、朋友分享、展示组件 | [前端责任地图](demo/README.md#代码责任地图) | `npm run check:runtime`、`npm run test:unit`、`npm run test:web`、`npm run build`（在 `demo` 执行） |
| 浮雕展示与模型加载 | `demo/src/ReliefViewer.tsx`、`demo/src/api.ts` | `demo/tests/relief-angle.test.mjs`、Web 浮雕测试 |
| 制品准备、发布激活与回滚 | `scripts/deploy-ubuntu.ps1`、`deploy/ubuntu/prepare-release.py`、`deploy/ubuntu/activate.sh` | `deploy/ubuntu/tests/test_deployment.py`；真实服务器另行验收 |

修改前先沿对应入口追踪调用和测试。公共规则只保留一个实现：前端网络请求集中于 `api.ts`，业务写操作共用账户级协调入口；后端网络与推理不占 SQLite 写事务，最终发布保留账户、任务、输入版本和原图校验。生产目前只支持一个应用进程与一个生成 worker，模块拆分不意味着可以直接增加 worker 数量。

## 后续解耦与恢复验证

首轮拆分生成流程、路由及探索/分享展示；账户状态协调、收藏编辑、后端 HTTP 路由与其业务 SQL 仍有集中职责。后续按以下顺序完成独立修改单元，每项先保持契约并验证，再提交：

1. 在生成执行与发布边界上解决 **F01**：保留已生成结果，有限预算重试最终事务，每次重新检查归属与版本；验证事务开始和提交失败、写入失败状态再次失败、持续故障。
2. 解决 **F02**：引入任务级取消，在阶段边界和推理子进程中响应删除/换图；保留服务停止后的重启恢复语义，实测取消时延。
3. 为浮雕加载建立 **F03** 错误隔离，验证失败的 lazy Promise 确实能重新加载。**F04** 的 API 正文与模型超时已有实现，补挂起正文、忙状态释放、草稿保留和退出/换号回归。
4. 集中提取前端账户状态协调（初始化、轮询、revision、单写锁），再提取收藏编辑器。后端再按收藏、探索/分享、备份拆路由和业务逻辑；共享存储和鉴权保持单一入口，不给每个功能复制一套状态或权限规则。
5. 更新 **F05/F06**：激活脚本已有锁，继续验证真实锁竞争，并处理制品准备的并发/中断与激活阶段可捕获信号恢复。强杀和断电需要另行设计恢复流程。
6. 持续补 **F07** 的完整业务和故障恢复证据，包括隔离目录备份恢复、真实生成与双账号媒体/任务隔离。模拟测试与历史验收不替代新改动的必要验证。

以上是待完成工作，不是本轮结构拆分已解决的缺陷。最新运行状态与线上验证仍以部署验收记录为准。

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

## 数据与第三方组件

Git 不包含原数据库、新账号数据库、用户照片、生成模型、模型权重、依赖环境或凭据。会话密钥和 Google OAuth 密钥只保存在受限配置中。第三方来源与许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。
