# MemoVerse · 收藏与浮雕预览

用一张照片保存一件收藏，整理分类、标签和笔记，并通过 CPU 深度推理生成可交互的浮雕模型。

## 当前功能

- 收藏、分类、探索、好友与个人五个页面，保留手机预览框架。
- 原图上传、编辑、删除、搜索与持久保存。
- FastAPI + SQLite 保存元数据，磁盘保存原图和 GLB 模型。
- 固定版本 AnyImage MoGe2 ONNX CPU 推理，加上本项目的浮雕网格与 GLB 导出。
- Three.js 模型预览，总偏转角限制为 40°。

探索和好友内容仍包含演示数据；正式多人账号、OCR、云端多人同步和 Android 近场分享尚未实现。当前生成流程不是 Blender 的 Bake Mesh 操作。

## 技术栈与目录

| 目录 | 内容 |
| --- | --- |
| `demo/` | React 19、TypeScript、Vite、Three.js 前端 |
| `backend/` | FastAPI、SQLite、媒体存储与串行生成任务 |
| `scripts/` | 本机启动、模型准备、服务器更新与数据迁移 |
| `deploy/lightsail/` | Ubuntu 安装、systemd、Nginx 和部署说明 |
| `docs/` | 产品决策、设计参考、接口与验证记录 |

## 本机运行

已有依赖和模型的 Windows 开发环境：

```powershell
./scripts/start-local.ps1
```

前端默认 `http://127.0.0.1:4173`，API 默认 `http://127.0.0.1:8000`。首次安装及模型准备见 [后端说明](backend/README.md) 和 [前端说明](demo/README.md)。

## 服务器部署

采用东京区域 Lightsail 1 GB 套餐，Ubuntu 24.04，单进程 FastAPI + Nginx。代码按 Git 提交放在独立发布目录；SQLite、图片与模型保存在 `/var/lib/memoverse`，更新代码不覆盖数据。前端在本机构建，服务器不运行开发服务器。

- [安装、访问控制、HTTPS 与运维说明](deploy/lightsail/README.md)
- [部署与更新脚本](scripts/deploy-lightsail.ps1)（默认仅显示计划，执行需要 `-Execute`）
- [一次性数据迁移与完整性校验](scripts/package-server-data.py)

1 GB 实例上的实际生成与内存验收以部署记录为准，不以本机性能代替。域名 `keepsite.space` 的 HTTPS 正在随 DNS 生效完成配置。

## 验证

本次服务器适配已通过 43 项后端检查、18 项前端检查、28 个受保护运行时文件完整性检查，以及前端生产构建。迁移脚本另有 3 项合成数据自测。

## 数据与第三方组件

仓库不包含 `backend/data`、个人照片、生成模型、模型权重、Python/Node 环境、访问口令或 SSH 私钥。服务器数据通过独立的 SSH 传输包迁移。第三方来源与许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。
