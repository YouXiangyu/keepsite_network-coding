# 首轮 demo 检查记录

日期：2026-10-15。范围：用于逐页调整的本地交互 demo，不是完整产品验收。

| 检查 | 结果 |
| --- | --- |
| `npm run build` | 通过，TypeScript 检查、Vite 构建与静态输出准备完成，退出码 0 |
| 构建前的 `check:runtime` | 通过，28 个受保护运行时文件保持完整 |
| `node --experimental-strip-types --test tests/data.test.mjs tests/sites-worker.test.mjs` | 8 项通过、0 项失败：标签缺省与命中、独立副本、初始化隔离、静态资源与路由回退、构建输出 |
| 浏览器交互 | 已操作五页导航、搜索与筛选、收藏详情、单图导入、编辑、保存、刷新恢复及删除测试记录 |
| 视觉 | 五页均保存最终截图及参考对照；独立核对未发现阻碍首轮 demo 交付的 P0/P1/P2 问题 |
| 设备预览 | 检查 iPhone 与 Pixel 10 两种模板；Pixel 首页与底部导航正常，临时浏览器视口已恢复默认 |
| 浏览器日志 | 最后检查未捕获 warn/error |

视觉材料：`home-final.jpg`、`category-final.jpg`、`explore-final.jpg`、`friends-final.jpg`、`profile-final.jpg`。对应 `*-comparison.jpg` 为左参考、右实现；`pixel-home.jpg` 为 Android 尺寸首页。实际样例总数为 14，未照抄参考图中的装饰性数量。模板状态栏与设备外框会影响首屏露出量。

剩余字重、间距、图片与文字密度随逐页反馈继续调整。构建仍提示主 JavaScript 分块约 534 kB（gzip 约 166 kB），未作为本轮 demo 的阻塞项。

本轮没有验证真实 OCR、AnyImage 模型生成、邮箱认证、云同步、对象存储或 Android 真机近场传输；这些能力尚未接入。社区及朋友内容为示例，卡片晃动不是实际浮雕。完整运行时自动化浏览器测试和真机性能测试不在上述通过项内。
