# Android 更新验证记录

2026-11-11，关联 #14 / PR #27。本轮同步 main `b97deb9`，仅合入 Web 与 CODEOWNERS 变化；Android 内容与 `a97a9f0` 完全一致，Web 内容与 main 完全一致。现有 Web 运行时契约检查通过（31 个可达模块）。本轮未重建或安装 APK、运行设备测试或启动正式 App；新提交的完整 CI 在提交后单独核对。

## 历史开发包身份

以下为 2026-11-09 留存的开发签名产物，本轮重新核对其摘要、实际签名、清单和 DEX。这些不是拟交付发行包。

| 产物 | 包名 | 字节数 | APK SHA-256 |
|---|---|---:|---|
| app-debug.apk | io.github.youxiangyu.keepsite | 56,347,061 | `d15a6f24fcf467a9c0a820e668eea88ace3a8139f1949627187f4d46406f5e8a` |
| app-debug-androidTest.apk | io.github.youxiangyu.keepsite.test | 683,966 | `aacb6d82a71354028cc61e88b2843269cef84808e8b1adc22ba702b971bd4995` |

主包版本为 `versionCode 3` / `0.1.0`。主 APK 的 DEX 含 `https://keepsite.space`，与该次构建的 `BuildConfig.API_BASE_URL` 一致。两包 v2 签名验证通过，均为单签名者；开发公有证书 SHA-256 为 `256859f8bdb20ff0fe68d567435e099b7b6cfe1ffc2fc7bfaac4cbfaa5817dab`。

当时构建输入为 `a171dbe` 加三文件冻结补丁，补丁 SHA-256 为 `5e5fee0d26e074182364632c9d5fc1f62db61c9219169828582ca01f5581e4ff`。该补丁与 `a171dbe..a97a9f0` 的完整差异一致，三份封存源码也与后续提交对应。它说明历史产物的源码来源，不代表从最新集成提交重新构建或验证了可重复构建。完整身份见 [验证摘要](android-update-verification.json)。

历史构建的 JVM 12 套、78 项测试通过，0 失败、错误或跳过；Lint 0 错误、16 警告。应用与仪器测试 APK 均构建成功，该轮未执行设备仪器测试。`a97a9f0` 的 [Application CI](https://github.com/YouXiangyu/keepsite_network-coding/actions/runs/42564558104) 和 [Deployment recovery](https://github.com/YouXiangyu/keepsite_network-coding/actions/runs/42564558103) 均通过，属于该历史提交的检查结果。

## 正式验收仍待完成

- 拟交付 APK 对应准确集成源码的服务地址、包名、版本、签名和摘要。
- 正式 App 在草稿、保存、账号输入期间阻止安装，以及取消系统安装后的恢复和再次安装。
- 同签名、更高版本的发行包覆盖升级，核对登录、票据和媒体数据保留。
- 两台真实 Android 设备上的上述流程与可回看证据。

目前缺少对应发行包、候选渠道、专用设备和账号条件。历史开发包、隔离组件和构建结果不能替代这些验收，当前不视为 #14 全部完成。[管理员复审要求](https://github.com/YouXiangyu/keepsite_network-coding/pull/27#pullrequestreview-5843351398)。
