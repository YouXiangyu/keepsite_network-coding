# 本轮图像处理依赖

| 组件 | 来源与固定信息 | 许可 |
| --- | --- | --- |
| AnyImage 代码 | https://github.com/OmooLab/AnyImage ，提交 `2e2ab3eb9b5242deadca097c929c40329dbb5012` | 上游 GPL-3.0-or-later，完整代码和 LICENSE 保留于 `.cache/anyimage` |
| MoGe2 ViT-S Normal ONNX | AnyImage 官方 model_catalog / 官方下载器指定模型，140852051 字节；准备脚本验证官方 SHA-256 | 上游模型目录标注 MIT；模型来源及校验信息由固定提交保留 |
| ONNX Runtime / trimesh | 项目内安装，版本及准备方式见 `scripts/setup-anyimage.ps1` | 依赖包附带各自许可 |
| Three.js | 前端包锁定于 `demo/package-lock.json` | MIT |

本项目调用 AnyImage 官方推理模块，并自行执行网格归一化、无效深度远平面填充、UV 与 GLB 导出；没有声称调用 Blender 的 Bake Mesh。正式打包或对外分发时应同时保留所分发依赖的许可与源码义务说明。
