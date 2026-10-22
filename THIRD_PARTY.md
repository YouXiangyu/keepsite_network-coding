# 第三方组件与历史依赖

当前应用的固定依赖分别见 `backend/requirements.txt`、`backend/requirements-dev.txt` 和 `demo/package-lock.json`。随应用分发依赖时须保留依赖包自带的许可证与适用声明。

M2 使用 [redis-py 8.1.0](https://pypi.org/project/redis/8.1.0/) 作为 Redis 客户端，[fakeredis 2.40.0](https://pypi.org/project/fakeredis/2.40.0/) 仅用于本地隔离测试。CI 的真实服务使用官方 Redis 8.2.10 镜像；模拟服务通过不等于真实服务验证通过。浏览器媒体缓存直接使用平台 IndexedDB 与 Web Crypto API，没有新增客户端存储依赖。

M1 已移除 AnyImage/MoGe2 浮雕和 Three.js 运行链；旧模型缓存、依赖副本及其许可证不因源码改版删除。旧来源、固定提交和准备脚本可从 Git 检查点 `d7945d8` 的本文件及对应脚本追溯；不将这些旧组件视为新版 OCR 方案。

M3 的 CPU OCR 固定 [RapidOCR 3.10.0（Apache-2.0）](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/LICENSE)和 [ONNX Runtime 1.31.0（MIT）](https://github.com/microsoft/onnxruntime/blob/v1.31.0/LICENSE)。完整直接运行依赖见 `backend/requirements-ocr.txt`。不启用 GPU、方向分类、大模型或付费识别接口。

检测和识别使用 PaddlePaddle 的 PP-OCRv6 tiny，经 RapidAI 转换的固定 ONNX 文件；模型名、v3.10.0 下载地址、SHA-256 及从识别模型导出的字典 SHA-256 集中在 `backend/ocr-models.json`。来源为 [RapidOCR 固定模型目录](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/python/rapidocr/default_models.yaml)，上游[检测模型](https://huggingface.co/PaddlePaddle/PP-OCRv6_tiny_det)和[识别模型](https://huggingface.co/PaddlePaddle/PP-OCRv6_tiny_rec)均声明 Apache-2.0。保留 PaddlePaddle/RapidAI 的来源与许可；不能把 RapidOCR 当前仅列部分型号的 MODEL_LICENSES 表误称为逐项包含 tiny。模型未提交 Git，重新分发模型时须附带上游许可与适用声明。

海报仅使用 [Wikimedia Commons](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)，按每个文件的许可、作者及来源审核，保留并展示来源页、许可链接、作者和获取时间。站点不是统一许可的图片库，搜索命中不能替代逐文件核验。应用没有购买商业海报许可，也不承诺现代电影覆盖；许可不足或匹配不明时保留原票。

免费 TURN 仅调查了 Open Relay/Metered。官方页面对免费档位及公共静态端点的额度边界未能闭合，因此没有启用、注册账号或分发其凭据；强制中继验收未完成。核验链接和原因见开发计划 M3 记录，不把公开端点视为无限免费授权。
