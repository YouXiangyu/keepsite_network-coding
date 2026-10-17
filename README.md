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
