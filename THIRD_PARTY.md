# 第三方组件与历史依赖

当前应用的固定依赖分别见 `backend/requirements.txt`、`backend/requirements-dev.txt` 和 `demo/package-lock.json`。随应用分发依赖时须保留依赖包自带的许可证与适用声明。

M2 使用 [redis-py 8.1.0](https://pypi.org/project/redis/8.1.0/) 作为 Redis 客户端，[fakeredis 2.40.0](https://pypi.org/project/fakeredis/2.40.0/) 仅用于本地隔离测试。CI 的真实服务使用官方 Redis 8.2.10 镜像；模拟服务通过不等于真实服务验证通过。浏览器媒体缓存直接使用平台 IndexedDB 与 Web Crypto API，没有新增客户端存储依赖。

M1 已移除 AnyImage/MoGe2 浮雕和 Three.js 运行链；旧模型缓存、依赖副本及其许可证不因源码改版删除。旧来源、固定提交和准备脚本可从 Git 检查点 `d7945d8` 的本文件及对应脚本追溯；不将这些旧组件视为新版 OCR 方案。

轻量 OCR 模型、海报来源和免费 TURN 服务将在开发计划 M3 中分别核实版本、哈希、许可、使用条件及费用限制，并更新这里。未完成核验前不宣称已有合法授权的海报内容库或已可用的免费中继。
