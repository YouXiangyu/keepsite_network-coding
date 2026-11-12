# 网络诊断隔离组件验证

## 2026-11-12 主分支集成检查

本轮将 main `3d46e5c` 合入 PR #28，处理 `MainActivity` 与 `UiViewModel` 的冲突，保留网络监控、自动更新和编辑器通知的状态及资源释放。网络页或报告文件选择进行中统一阻止安装交接，离开后恢复更新；在原安装保护用例中补充阻止及恢复断言。

本地 Android 164 项 JVM 测试通过，无失败或跳过；Lint 0 错误、67 警告，应用与测试 APK 构建通过。Web 220 项单元测试、运行时契约、类型检查和生产构建通过；本机 Chrome 使用构建产物及原 API 夹具运行既有两项浏览器用例，覆盖月份/网络页导航刷新、编辑器错误可见性、忙态与单次提交，均通过。独立代码复审确认两侧功能和安装保护接线完整。

CI 按用户决定保持暂停。本轮是冲突解决与集成回归，不代替下方声明的正式跨端、设备传输及观测非干扰验收，也未重跑独立 Host 设备用例。

## 隔离组件范围

`android/app/src/androidTest/network-host` 是独立Gradle工程，复用真实诊断Screen、core和theme，以合成状态及受控controller验证页面、旋转、报告和系统文件交付。辅助包为 `io.github.youxiangyu.keepsite.networkverification`，测试包为其 `.test`；辅助清单移除INTERNET且不声明正式MainActivity。

这项验证不启动正式App、不登录、不发业务请求，不代表MainActivity/AppRepository整链、线上跨设备同步、真实Native RTC、TURN/S3或两台物理手机验收。此前Android CI只编译正式app及其仪器测试并运行JVM/Lint，没有构建这个独立工程，也没有执行这些Host仪器用例；自动CI现已暂停。

## 独立构建与准备

从仓库根目录执行，先为独立构建进程配置JDK、SDK，并将 `$verificationGradleCache` 设为工作区依赖缓存、`$verificationSigningStore` 设为已有开发测试签名文件路径：

```powershell
.\android\gradlew.bat -p android/app/src/androidTest/network-host -g $verificationGradleCache --offline --no-daemon "-PverificationSigningStore=$verificationSigningStore" assembleDebug assembleDebugAndroidTest
```

两个APK都使用同一个开发测试签名；不提交密钥，也不把开发签名当作正式分发签名。离线依赖缺失需先记录并处理，不能沿用旧APK冒充新源码。输出位于该独立工程的 `build/outputs/apk/debug/` 及 `build/outputs/apk/androidTest/debug/`。核实准确源码、两个新APK的hash、包名、签名及实际manifest后，按明确计划安装到自有隔离模拟器。正式根只有`:app`，常规`:app:assembleDebugAndroidTest`不能替代本构建。

测试最低要求Android API 31；低于31会在任何显示修改前拒绝执行。

运行前明确自有模拟器、唯一serial、专用ADB端口及进程归属，禁止用户真机、默认共享设备和并行测试。上次若有未恢复记录，先恢复或人工处理，不能换一个输出目录继续测试。准备并启动隔离环境、构建和安装均不由下述runner自动完成。确认两个辅助包已安装且没有正在运行；保留设备原显示设置和本轮资源归属记录。不要运行正式App的全部历史instrumentation。

## 运行原有三项测试

执行者先设置 `$adb`（ADB绝对路径）、`$port`（专用ADB端口）、`$serial`（自有模拟器的 `emulator-N`）和 `$runDirectory`（本轮尚不存在的输出目录），再从仓库根目录执行：

```powershell
python scripts/run-network-host.py --adb $adb --port $port --serial $serial --output $runDirectory
```

runner仅运行已安装的辅助测试包，原三方法为：

- `narrowOverviewDetailsConnectionsAndBackUseActualSharedViews`
- `largeFontTabletFiltersKeepInputAndExpandFullTraceAcrossClocksThenRotate`
- `actualSafSaveLateAccountResultAndSystemChooserHaveOwnedOutputsOnly`

测试会调整全局size/density并冻结或改变旋转；大字体来自Host进程局部覆盖。每例TestRule负责内层快照与恢复，外层runner在运行前持久化设备身份及size/density的物理值、override有无、旋转模式/偏好，结束或异常时独立恢复并读回核对。恢复原自定义值，原来没有override才reset；不能统一覆盖成默认分辨率或竖屏。

查看输出目录的 `instrumentation.txt` 和 `display-state.json`。三例测试成功与显示恢复成功分别判断；测试失败即使恢复成功仍是测试失败，恢复失败也必须报错。正常Ctrl+C或runner超时会尝试停止两个辅助包并恢复，不处理正式App。结束后仍需按本轮归属记录收尾模拟器/私有ADB；runner不自动卸载、关闭设备或清理用户数据。

## 中断后的显式恢复

强杀Python、ADB离线或设备断开可能阻止finally执行，不能保证立即恢复。若命令完成状态不明，即使设置暂时读回一致，记录仍保留为未恢复；先确认辅助测试和设备命令已结束，再显式恢复。保留 `display-state.json`，排除并行测试，重新核对原目标；将 `$failedRunDirectory` 指向原失败目录，并明确传入同一ADB、端口和serial：

```powershell
python scripts/run-network-host.py --adb $adb --port $port --serial $serial --recover $failedRunDirectory
```

恢复模式不重跑测试。身份包含AVD、构建、Android用户及同一次开机bootID；模拟器重启、换AVD/用户或身份不符时拒绝自动恢复，不把旧基线覆盖到新目标。已成功恢复的旧journal也拒绝复用。目标不可恢复或记录不完整时保留错误交人工核查，不修改journal绕过守卫，不把强杀、重启或设置未知记为恢复通过。

验收需分别保留原三例结果、正常/异常恢复及非默认原设置回读的证据。组件测试通过与正式设备/线上账号验收分开记录，未执行项目保持未验。

## 本轮验证记录

在 API35 隔离模拟器上，原三例一次通过；结束后无分辨率/密度覆盖，旋转恢复原锁定方向。另将基线设为900×1700、360dpi、锁定方向3，观察测试改变设置后主动停止辅助进程：运行器按预期返回失败并保留未恢复状态，确认进程退出后显式恢复成功，随后还原真实初始设置。

[结果与设置前后值](network-host-display-evidence/results.json)、[三例原始日志](network-host-display-evidence/natural-instrumentation.txt)、[受控中断原始日志](network-host-display-evidence/interrupted-instrumentation.txt)一并留存；日志仅按仓库规则统一换行，摘要区分原件与仓库文本。中断日志中的 `Process crashed` 来自主动停止，不是自然崩溃；恢复成功也没有将中断用例改记为通过。辅助工程13项JVM回归、Lint和两APK构建通过；外层运行器19项本地回归通过。上述记录不代表正式应用整链或全面系统界面验收。
