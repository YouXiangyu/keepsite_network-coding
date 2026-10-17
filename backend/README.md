# 本机后端

FastAPI 在 `127.0.0.1:8000` 提供真实 API；现有前端使用 `127.0.0.1:4173`。当前为单用户本机工作区，未实现正式账号认证、云同步或 Android 近场传输，不应把这个开发服务直接暴露到公网。

## 当前能力

- 收藏增删改查、资料修改、示例分享已读与独立收藏副本、示例社区互动均由 API 持久保存。
- 原图实际解码校验 JPG/PNG/WebP，单图不超过 12 MiB、4000 万像素；原始字节按 SHA-256 去重保存，不改写原图。
- SQLite 位于 `backend/data/memoverse.sqlite3`，完整原图在 `backend/data/media`，生成文件在 `backend/data/models`。
- 原浏览器 IndexedDB 的 `current` 数据保留为迁移前备份；首次迁移保留 ID、时间、来源、资料与图片，稳定迁移 ID 防重复导入。非空目标遇到不同旧快照会报告冲突，不覆盖。
- 图片保存后自动创建持久任务；新建、换图或视觉模板变化触发生成，纯文字修改复用模型。后台一次执行一个任务，中断后重启恢复，旧输入结果不会覆盖新图片或复活已删收藏。
- AnyImage 官方 MoGe2 ONNX 推理在本机 CPU 执行，本项目将学习到的深度转换为浮雕网格并导出嵌入纹理的 GLB。不是 Blender Bake Mesh 操作，未用亮度位移或 CSS 旋转替代学习深度。
- ZIP 备份包含元数据、当前原图、模型和相关产物；尚无备份导入界面。删除收藏暂不立即回收底层媒体，以保护朋友独立副本，后续可增加按引用检查的文件回收。

## 环境与启动

所有命令在项目根目录执行。使用 Python 3.12 或 3.13；当前本机已在 `backend/.venv` 配好，未修改全局 Python 包。

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install --cache-dir .cache/pip -r backend/requirements-dev.txt
./scripts/setup-anyimage.ps1
./scripts/start-local.ps1
```

AnyImage 的源码、额外依赖与单一几何模型存放在项目 `.cache`；代码固定提交，模型有 SHA-256 校验。模型约 141 MB，不下载去背景、超分全套资源，不使用付费 API。首次准备需要网络；准备完毕，推理本身无需远端服务。

也可在终端单独运行后端：

```powershell
backend/.venv/Scripts/python.exe -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000
```

可配置 `MEMOVERSE_DATA_DIR`、`MEMOVERSE_ALLOWED_ORIGINS`；前端通过 `VITE_API_BASE_URL` 指定 API 地址（修改后重启 Vite）。如需使用单独的模型 Python 环境，可指定 `ANYIMAGE_PYTHON`。不要更换原前端 origin 后再期待自动读到旧浏览器收藏。

## 验证

```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests --basetemp .cache/pytest-local -q
node --experimental-strip-types --test demo/tests/data.test.mjs demo/tests/api.test.mjs demo/tests/relief-angle.test.mjs
```

`--basetemp` 是测试专属目录，pytest 会清理其内容，不能指向真实数据目录。模型小样结构检查见 `scripts/check-anyimage-output.py`。接口约定见 `../docs/local-api-contract.md`，实际交互文档在 `/docs`。

此阶段不代表 OCR、邮件验证、跨端同步或真机网络功能已经完成。
