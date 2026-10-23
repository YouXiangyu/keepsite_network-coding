# 第三方组件与历史依赖

当前应用的固定依赖分别见 `backend/requirements.txt`、`backend/requirements-dev.txt` 和 `demo/package-lock.json`。随应用分发依赖时须保留依赖包自带的许可证与适用声明。

M2 使用 [redis-py 8.1.0](https://pypi.org/project/redis/8.1.0/) 作为 Redis 客户端，[fakeredis 2.40.0](https://pypi.org/project/fakeredis/2.40.0/) 仅用于本地隔离测试。CI 的真实服务使用官方 Redis 8.2.10 镜像；模拟服务通过不等于真实服务验证通过。浏览器媒体缓存直接使用平台 IndexedDB 与 Web Crypto API，没有新增客户端存储依赖。

M4 新增固定 [websockets 17.3](https://pypi.org/project/websockets/17.3/)，由既有固定 [Uvicorn 0.44.0](https://pypi.org/project/uvicorn/0.44.0/) 的 `websockets-sansio` 引擎处理 WebSocket。2026-10-22 核验上述固定版本的官方包元数据及安装包许可证，两者均为 BSD-3-Clause；随应用分发时保留各自版权、许可条件与免责声明，不将作者或贡献者名称用于未经许可的背书。依赖直接固定在 `backend/requirements.txt`，生产服务单元明确选择引擎、消息上限与 ping/pong 参数；本机回环测试核验实际引擎行为，不将浏览器或第三方 WebSocket 服务作为新增依赖。

M1 已移除 AnyImage/MoGe2 浮雕和 Three.js 运行链；旧模型缓存、依赖副本及其许可证不因源码改版删除。旧来源、固定提交和准备脚本可从 Git 检查点 `d7945d8` 的本文件及对应脚本追溯；不将这些旧组件视为新版 OCR 方案。

M3 的 CPU OCR 固定 [RapidOCR 3.10.0（Apache-2.0）](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/LICENSE)和 [ONNX Runtime 1.31.0（MIT）](https://github.com/microsoft/onnxruntime/blob/v1.31.0/LICENSE)。完整直接运行依赖见 `backend/requirements-ocr.txt`。不启用 GPU、方向分类、大模型或付费识别接口。

检测和识别使用 PaddlePaddle 的 PP-OCRv6 tiny，经 RapidAI 转换的固定 ONNX 文件；模型名、v3.10.0 下载地址、SHA-256 及从识别模型导出的字典 SHA-256 集中在 `backend/ocr-models.json`。来源为 [RapidOCR 固定模型目录](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/python/rapidocr/default_models.yaml)，上游[检测模型](https://huggingface.co/PaddlePaddle/PP-OCRv6_tiny_det)和[识别模型](https://huggingface.co/PaddlePaddle/PP-OCRv6_tiny_rec)均声明 Apache-2.0。保留 PaddlePaddle/RapidAI 的来源与许可；不能把 RapidOCR 当前仅列部分型号的 MODEL_LICENSES 表误称为逐项包含 tiny。模型未提交 Git，重新分发模型时须附带上游许可与适用声明。

海报仅使用 [Wikimedia Commons](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)，按每个文件的许可、作者及来源审核，保留并展示来源页、许可链接、作者和获取时间。站点不是统一许可的图片库，搜索命中不能替代逐文件核验。应用没有购买商业海报许可，也不承诺现代电影覆盖；许可不足或匹配不明时保留原票。

免费 TURN 仅调查了 Open Relay/Metered。官方页面对免费档位及公共静态端点的额度边界未能闭合，因此没有启用、注册账号或分发其凭据；强制中继验收未完成。核验链接和原因见开发计划 M3 记录，不把公开端点视为无限免费授权。

## Android 原生客户端

Android 工程直接依赖与固定版本见 `android/app/build.gradle.kts`，只使用 Google Maven、Maven Central 及构建插件所需的 Gradle Plugin Portal。本轮没有把 Gradle 缓存、AAR、SDK、debug key 或下载制品提交 Git；构建和缓存路径见 [Android README](android/README.md)。以下许可名称于 2026-10-22 从本机实际解析的相应 POM 核对；直接声明不等同于完整传递依赖清单。

| 直接依赖 | 声明版本 | 用途与 POM 许可 |
| --- | --- | --- |
| `androidx.core:core-ktx` | 1.19.0 | Android 平台扩展；Apache-2.0 |
| `androidx.activity:activity-ktx` | 1.13.0 | Activity、系统图片选择等；Apache-2.0 |
| `androidx.appcompat:appcompat` | 1.8.0 | 原生界面兼容组件；Apache-2.0 |
| `com.google.android.material:material` | 1.14.0 | Material 原生控件；Apache-2.0 |
| `androidx.recyclerview:recyclerview` | 1.2.1 | 票夹列表；Apache-2.0 |
| `androidx.lifecycle:lifecycle-runtime-ktx` / `lifecycle-viewmodel-ktx` | 2.6.2 | 生命周期与 ViewModel；Apache-2.0 |
| `com.squareup.okhttp3:okhttp` | 5.3.2 | HTTP 与 WebSocket；Apache-2.0 |
| `io.github.webrtc-sdk:android` | 150.7871.01 | 原生 WebRTC/JNI；BSD-3-Clause，另有底层第三方声明 |
| `junit:junit` | 4.13.2 | 仅 JVM 测试；EPL-1.0 |
| `org.json:json` | 20240303 | 仅 JVM 测试替代 Android 平台 JSON 类；POM 声明 Public Domain |

AndroidX Test runner/JUnit 扩展/Espresso 只用于设备测试 APK；具体版本同样在 Gradle 文件中。Kotlin 标准库、Okio 等由依赖图引入，其实际解析版本和适用声明应随最终制品核对，不把直接依赖表冒称为完整的软件物料清单。

[OkHttp 5.3.2](https://github.com/square/okhttp) 的实际缓存 POM SHA-256 为 `f947372c4dd848f752aef6837d1517461c444c1d43af5aeb15e6f2bd737bb072`，其中声明 Apache-2.0。已固定采用该版本，不因维护者网页更新而自动升级。

[WebRTC SDK Android](https://github.com/webrtc-sdk/android) 是第三方预编译发行，不能称作 Google 官方 Android SDK。打包标签 `v150.7871.01` 对应提交 `45d64c8be316b89e139d20ea51602027dc486d27`；该固定提交的 [版本记录](https://github.com/webrtc-sdk/android/blob/45d64c8be316b89e139d20ea51602027dc486d27/CHANGES.md) 将 `150.7871.01` 对应到原生源码提交 `73cb8180f7258ee292878d6edd05177f41883962`。实际 AAR 为 49,147,033 字节，SHA-256 `0a1627b1a48c2bc17d9a40d62fc47bd45166f44a311e95917f147c402de379b0`；实际 POM SHA-256 为 `b740a6bec98f078892a70ee502404eb7b437feb0ef4a7fa74b4561ae25db553c`，声明 BSD-3-Clause。

打包仓库的固定 [根 LICENSE](https://github.com/webrtc-sdk/android/blob/45d64c8be316b89e139d20ea51602027dc486d27/LICENSE) 为 MIT，但底层固定 WebRTC 源码的 [LICENSE](https://github.com/webrtc-sdk/webrtc/blob/73cb8180f7258ee292878d6edd05177f41883962/LICENSE) 为 BSD-3-Clause；不能将整个 AAR 的许可证简化为 MIT。维护者另提供该发布提交的 [WebRTC 及捆绑第三方通知](https://github.com/webrtc-sdk/android/blob/45d64c8be316b89e139d20ea51602027dc486d27/Licenses/WEBRTC.md)。

本轮 AAR 的六个 ZIP 条目中没有 LICENSE/NOTICE，因此 Android 工程已在 `app/src/main/assets/licenses/` 保存原始许可与完整通知，随 APK 打包至 `assets/licenses/`，不只保留网页链接：

| 随 APK 附带文件 | 固定来源 | 原始字节数 | SHA-256 |
| --- | --- | --- | --- |
| `WEBRTC-LICENSE.txt` | 原生源码提交的 BSD-3-Clause 许可 | 1,511 | `ab00a482b6a3902e40211b43c5d0441962ea99b6cc7c25c0f243fa270b78d482` |
| `WEBRTC-PATENTS.txt` | 同一原生源码提交的专利授权 | 1,388 | `01462e2068d1a04c2274f3389773014c14ed9bc3446b28303543bd3e3c064145` |
| `WEBRTC-SDK-PACKAGING-LICENSE.txt` | 打包提交的 MIT 许可 | 1,068 | `e6b282fe6c0fb353928923470457f31b44cbab203effd60c0cde4a5bb96c8aec` |
| `WEBRTC-THIRD-PARTY-NOTICES.md` | 打包提交提供的完整捆绑通知 | 788,358 | `d1f9382c6878ac024155fd6d44a5977329108bb8b0a01cea40e4a2f1d7de252e` |

每个文件的固定下载 URL 和制品关联见 [sources.json](android/app/src/main/assets/licenses/sources.json)。原文完整保留，没有用摘要取代许可条件；这里核验的是发布来源与完整性，不宣称重新完成了全部传递原生库的许可审核。

二进制只读检查确认 `arm64-v8a` 与 `x86_64` 的 ELF LOAD 为 16 KiB 对齐，两个 32 位 ABI 为 4 KiB；详细方法和验证边界见 Android README。未修改或重打包原生库，也未宣称已经在 16 KiB 真机上运行通过。
