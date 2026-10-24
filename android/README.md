# keepsite Android

独立 Kotlin 原生客户端，包名 `io.github.youxiangyu.keepsite`。工程位于 `demo/` 之外，使用 Android Views、ViewBinding 和现有 schema 3 业务、分页同步与 WebRTC v1 协议。产品范围以 [项目定义与决策主文档](../docs/项目定义与决策主文档.md) 和 [开发计划](../docs/开发计划.md) 为准。

## 改名与现有资料迁移

2026-10-24 起，应用名为 `keepsite`，包名和回调 scheme 均为 `io.github.youxiangyu.keepsite`，Keystore 别名为 `keepsite-session-v1`。这是独立应用身份；安装新包会与旧包并存，不覆盖或清除旧应用。旧应用的私有会话、草稿和缓存受 Android 沙箱隔离，新包不能直接读取；本次也不安装、卸载或清除设备数据。

迁移前在旧应用逐个账号核对草稿：保存并确认服务端已收录的电影票可在新包使用同一 Google 或邮箱登录方式恢复，图片按云端或服务端实际保存状态重新获取。新包必须重新登录，不复制旧 cookie、Keystore 或未完成的 Google 票据。后端需要与新请求头、cookie 和回调协议一同发布；仅安装新包不能证明生产登录已可用。

未同步草稿先在旧应用手动备份：记录所有填写字段，保留系统相册中的原票文件；若照片只剩旧应用私有副本，保持旧应用及其数据原样，先通过旧应用完成保存并确认，或另行提供并验证旧包的显式导出功能后再迁移。当前旧包没有通用草稿导出按钮，不把系统备份、截图或新包首次启动当作完整草稿导出。保存结果未知时先在旧包核对，避免重新录入造成重复。确认原票和字段均已导出后，在新包重新选择原票并录入字段；验证同账号云端记录和草稿恢复完成前不要卸载旧包。

下方历史验收记录及其 APK 文件名属于改名前版本，不代表本次新包已完成真机、Google 或云端验收。

## 工具链与本机构建

| 项目 | 当前配置 |
| --- | --- |
| Gradle Wrapper | 9.5.0，发行 ZIP 的 SHA-256 固定在 `gradle/wrapper/gradle-wrapper.properties` |
| Android Gradle Plugin | 9.3.3，使用内置 Kotlin，不另加 Kotlin Android 插件 |
| 内置 Kotlin 编译器 | 2.2.10；不要因网站发布了新版本而单独升级 |
| Java / Kotlin 字节码目标 | JVM 17；运行 Gradle 使用 JDK 17 或更新版本 |
| 本轮实际 JVM | Android Studio 附带的 JBR 25.0.2；不把编译目标 17 当作实际运行 JVM 版本 |
| Android SDK | `compileSdk=37`，本机平台目录 `platforms/android-37.0` |
| 应用平台范围 | `minSdk=26`、`targetSdk=36`，Build Tools 36.0.0 |

本机 SDK 为 `D:\300-tools\Android\Sdk`；`local.properties` 只记录本机 SDK 路径，不提交 Git。以下命令从仓库根目录执行，将 Gradle 缓存保存在 D 盘工作区。首次解析依赖需要联网；已有依赖不重复安装。

```powershell
$env:JAVA_HOME = 'D:\300-tools\AndroidStudio\jbr'
$env:ANDROID_HOME = 'D:\300-tools\Android\Sdk'
$env:GRADLE_USER_HOME = Join-Path (Get-Location) '.cache\android-gradle'
$env:ANDROID_USER_HOME = Join-Path (Get-Location) '.cache\m7-android-user'
$env:TEMP = Join-Path (Get-Location) '.cache\android-temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:ANDROID_USER_HOME, $env:TEMP | Out-Null
.\android\gradlew.bat -p android --no-daemon :app:assembleDebug :app:testDebugUnitTest :app:lintDebug
```

APK 位于 `app/build/outputs/apk/debug/app-debug.apk`。本机 debug key 位于仓库 `.cache/android-signing/debug.keystore`；`prepareDebugSigning` 仅在该文件缺失时创建开发身份，不是发布签名，不能提交密钥或用它替代正式分发身份。

## 服务地址与测试入口

默认服务地址为 `https://keepsite.space`。release 构建固定使用该 HTTPS 地址，生产清单禁止明文流量。debug 构建才允许通过 `keepsite.debugBaseUrl` 指向 HTTP loopback；不接受任意公网 HTTP 地址、用户信息、路径、查询或 fragment。

本机原生互通夹具使用 `http://127.0.0.1:4180`。构建测试 APK：

```powershell
.\android\gradlew.bat -p android --no-daemon '-Pkeepsite.debugBaseUrl=http://127.0.0.1:4180' :app:assembleDebug :app:assembleDebugAndroidTest :app:testDebugUnitTest :app:lintDebug
```

该命令只构建，不启动服务器或设备。互通测试另需受本轮随机 token 保护的合成服务、明确选择的测试设备和本轮端口转发；测试后只关闭自己创建的服务、浏览器、转发与模拟器。不要把本机测试地址用于正式 APK，也不要为运行本机测试启动 EC2。

## 账号、图片与前后台

- HTTP 与 WebSocket 使用同一后端会话。原生写请求明确标记 `X-Keepsite-Client: android`，仍发送固定 Origin 与会话 CSRF；该标记不是身份凭据，也不豁免用户归属检查。
- Google 登录交给系统浏览器，返回 `io.github.youxiangyu.keepsite:/oauth/callback`。App 校验固定 URI、单值 state 与有效期，用自己保存的 S256 verifier 兑换一次性票据；不在回调 URI 中接收 Cognito token，也不从浏览器复制会话 cookie。
- 图片选择使用系统 Photo Picker，读取用户明确选择的 URI 并复制为私有草稿。不申请整个照片库、外部存储、相机或麦克风权限。未确认上传的草稿与可回收媒体缓存分开保存。
- 前台是设备传输基线。进入后台、退出或换号会撤销旧网络与 peer 生命周期；旧代请求不能发布新状态。返回前台重新确认会话并补拉游标，不承诺锁屏、后台或进程关闭后持续 P2P。
- 原图与海报长期保存由服务端云存状态确认；本机按账号缓存，预算 128 MiB。文件按 SHA-256、长度、类型与图片尺寸校验，成功提交后才标为缓存完成。空间或写入失败可临时展示，不伪称已缓存；清本机缓存不删除云端记录。
- 写操作保留 `operationId`，上传保留 `uploadId`；超时先核对结果，再补拉到 `{serverId, committedCursor}` 屏障。不增加离线 CRUD。

## 原生设备互通

复用现行 `backend/peer_contracts.py` 与 Web 客户端协议。设备登记仅发送 `name`，不向严格 v1 消息擅自添加 `platform` 或缓存计数；服务端未提供的设备属性保持未知。

每次传输一个可靠有序 DataChannel，名称为 `keepsite-{transferId}`。二进制整帧最多 16 KiB，头部是大端序 `uint32 offset + uint32 payloadLength`，没有额外 `seq` 或 `meta` 消息。控制顺序为 `get → 分块/ack → end → verified`。背压使用 256/64 KiB 高低水位；发送端以 `send()` 真正接受的字节推进偏移，接收端校验完成后等待确认排空再关闭。

每个页面/原生实例发送与接收合计最多两条，媒体加载最多四条。一次获取的总 peer 预算为 12 秒，连接最多 4 秒、停滞最多 2 秒，仅允许一次连续前缀续传；重试不重置总预算。源文件再次校验，发送过程中持续检查引用和可共享性，清缓存或删除后不继续从旧内存副本发送。当前只接受服务器返回的空 ICE 列表和 `turnEnabled=false`，不自行启用 STUN/TURN。

## 固定依赖与二进制核验

运行与测试直接依赖见 `app/build.gradle.kts`，仓库来源限定 Google Maven、Maven Central；Gradle 插件另使用 Gradle Plugin Portal。主要网络依赖实际解析为：

| 坐标 | 用途 | 来源 / 许可 |
| --- | --- | --- |
| `com.squareup.okhttp3:okhttp:5.3.2` | HTTP、WebSocket、受限媒体下载 | Maven Central；本轮缓存 POM 声明 Apache-2.0 |
| `io.github.webrtc-sdk:android:150.7871.01` | 原生 libwebrtc / JNI | Maven Central 的第三方预编译发行；POM 声明 BSD-3-Clause，不是 Google 官方 Android 发行 |

WebRTC 打包标签 `v150.7871.01` 指向提交 `45d64c8be316b89e139d20ea51602027dc486d27`，该提交的 [变更记录](https://github.com/webrtc-sdk/android/blob/45d64c8be316b89e139d20ea51602027dc486d27/CHANGES.md) 将版本映射到原生源码提交 `73cb8180f7258ee292878d6edd05177f41883962`。2026-10-22 对实际 Gradle 缓存 AAR 的只读核验结果：

- AAR 大小：49,147,033 字节。
- AAR SHA-256：`0a1627b1a48c2bc17d9a40d62fc47bd45166f44a311e95917f147c402de379b0`。
- AAR SHA-1：`b9ae2a17fa38f16ce071f029cf8684fb9166a1c7`，与 Gradle 缓存内容目录一致；SHA-256 才是这里保留的完整性记录。
- POM SHA-256：`b740a6bec98f078892a70ee502404eb7b437feb0ef4a7fa74b4561ae25db553c`。
- `classes.jar` SHA-256：`2789e444c63f5b9b5f9f1ba227d12fbc03dfe617d6b83c0192b426730b3d8989`；JNI 适配签名已用实际解析的 classes 核对。

| AAR 内 ABI | ELF 位数 | 三个 PT_LOAD 的 `p_align` | 16 KiB 加载段检查 |
| --- | --- | --- | --- |
| `arm64-v8a` | 64 | 16,384 | 通过，文件偏移与虚拟地址同余 |
| `x86_64` | 64 | 16,384 | 通过，文件偏移与虚拟地址同余 |
| `armeabi-v7a` | 32 | 4,096 | 仍为 4 KiB，未声称 16 KiB 兼容 |
| `x86` | 32 | 4,096 | 仍为 4 KiB，未声称 16 KiB 兼容 |

两个 64 位库的 GNU_RELRO 独占对应可写 LOAD，向上扩到 16 KiB 页边界不覆盖其他可写 LOAD。只读检查时的 debug APK 中四个 `.so` 都未压缩，ZIP 数据起点均按 16 KiB 对齐，内容哈希与 AAR 相同。这些是二进制结构检查，不能代替 [Android 官方要求的实际 16 KiB 环境测试](https://developer.android.com/guide/practices/page-sizes)。

以上是固定版本的本轮校验记录，不是已启用 Gradle dependency verification 的声明；升级依赖后需重新核验。完整许可来源与打包仓库 MIT / 底层 WebRTC BSD 的区别见 [第三方组件](../THIRD_PARTY.md#android-原生客户端)。

AAR 本身未包含 LICENSE/NOTICE，因此工程在 `app/src/main/assets/licenses/` 附带固定提交的 WebRTC BSD 许可、PATENTS 授权、打包仓库 MIT 许可和完整维护者第三方通知；这些文本随 APK 放入 `assets/licenses/`。来源、版本、原始字节数与 SHA-256 见 [sources.json](app/src/main/assets/licenses/sources.json)。通知使用对应发布提交的原文，不混用 `main` 最新内容；这不等同于重新审计每个传递原生组件。

## 验证状态

2026-10-24 视觉第二轮：票夹采用真实错列、搜索/排序、紧凑片名日期和轻量底栏；显式刷新可重试失败图片且保留搜索，灰字对比度与触达区域已校验。34 JVM、Lint 0错误/13警告、最终同一真机5项UI通过，使用用户授权参考裁图的隔离账号验收。当前包位于 `.cache/visual-redesign-20261024/`；`memoverse-https-debug.apk` 为默认 HTTPS 开发签名包。详见 [视觉证据](../docs/evidence/visual-redesign-20261024.json)。UI测试需要向新媒体回归传入本轮 `runToken`；测试只隔离自身Activity的自动填充会话，不改变系统设置或生产行为。下方为此前检查点。

2026-10-24 更新：本轮应用代码通过 34 JVM、Lint（0 错误、10 警告）、360dp/大字号/800dp 平板共 9 次模拟器 UI，以及模拟器和一台用户授权 Android 16 真机的 CRUD、账号隔离、真实 JNI/DataChannel 双向传图。Web 已修复完成确认提前关闭竞态。真机完整 UI 仪器测试最终 3/3 通过；测试启动期间临时采用后台 Activity 启动权限，并在 finally 释放，API 29 以下保留原路径。输入、旋转、重建及后台恢复断言未放宽，独立审查通过。详见 [本轮证据](../docs/evidence/mobile-delivery-20261024.json) 和开发计划；下述 2026-10-23 内容为历史检查点。

默认 HTTPS 验收包位于 `.cache/mobile-delivery-20261024/memoverse-https-debug.apk`，开发签名，连接 `https://keepsite.space`；服务器尚未部署新版。已测 loopback 包和测试包在同目录。互通校验器可用 `--physical --serial <明确授权设备>`，以及 `--apk` / `--test-apk` 指定工作区内保存的 APK；仍核对安装制品 SHA、随机夹具 token 和前后源码哈希。先确认目标设备没有未知的同包用户数据，不使用卸载或 `pm clear` 掩盖冲突。

2026-10-23 保存为 `codex/android-native-progress` 的 **M7 WIP 检查点**，不属于已验收版本。最近一轮 `assembleDebug`、`assembleDebugAndroidTest`、**34 项 JVM 测试**通过；`lintDebug` 为 0 错误、10 警告。已有 APK 是仅供本机夹具使用的 4180 构建，尚未生成供用户验收的默认 HTTPS 包。

平板返回前台曾因复用已被服务器关闭的闲置连接而失败。修正后，JSON 和媒体 GET 在既有超时内允许 OkHttp 恢复连接，写请求保持不自动重试；WS 回调保留原始代次和连接归属。原失败场景在实际后台停留 6001ms 后自动恢复，严格断言通过且未手动刷新；534ms 的记录包含连续 500ms 的健康状态等待，不作为网络性能测量。最新 APK 的完整 UI 矩阵、真实 Android↔Web 双向传图和原生 CRUD 仪器联调仍未完成。检查点证据与下一步见 [开发计划](../docs/开发计划.md) 和 [有限验证记录](../docs/evidence/android-m7-checkpoint-20261023.json)。

两台 Android 真机验收由用户执行。真实 Google/邮箱、私有 S3/CORS、实际票据、跨网络、免费 TURN、16 KiB 设备，以及正式发布签名与分发均不能由本机合成夹具或模拟器替代。本阶段没有启动、发布或调整 EC2，也没有改变 58 分钟自动关机约定。
