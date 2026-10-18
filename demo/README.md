# MemoVerse Web App

本目录提供真正的响应式网页应用，桌面使用侧栏和宽内容区，小屏浏览器使用适合触控的布局。Android APK 后续独立开发；旧手机模拟器不进入生产依赖图。五张参考图继续提供视觉语言与业务参考，历史截图不代表当前 Web 验收。

未登录只显示 Google 登录、邮箱注册登录、邮箱验证码和密码找回。登录成功后才挂载业务页面；每个账号由服务器首次复制已批准的初始化模板。普通登录不读取旧 IndexedDB、不调用 bootstrap，也不重置收藏。旧浏览器快照保持原状。

## 业务与会话

收藏详情、搜索、分类、来源筛选、排序、单图导入、增删改、资料编辑、分享及社区示例互动继续保留。原图与模型通过同源受保护 API 获取；前端不保存身份提供方令牌。服务器 HttpOnly Cookie 负责会话，业务写入携带 CSRF 令牌；注销、401 或换号时卸载业务并取消请求、轮询、模型加载和临时预览。迟到响应不能跨账号覆盖界面。

上传支持 JPG、PNG、WebP，最多 12 MiB。提交成功才更新界面，失败保留草稿；重复创建仍使用稳定草稿 ID。纯文字编辑复用模型，原图改变才触发新生成。AnyImage 当前提供真实深度与原图纹理的浮雕；查看器从正面总偏转不超过 40°，包括斜向。CSS 晃动不代表模型已生成。

当前测试模板包含 17 件收藏、4 条分享、6 条社区内容。曝光、喜欢和推荐人物仍是测试示例；新用户拥有独立记录和互动，不会自动发布个人新上传。

## 代码责任地图

| 模块 | 修改入口与职责 |
| --- | --- |
| `src/App.tsx`、`src/AuthGate.tsx` | 登录门禁、会话续查、按账户挂载/卸载业务树 |
| `src/api.ts` | 唯一网络入口；超时、CSRF、会话 epoch、请求取消与响应归属校验 |
| `src/Prototype.tsx` | 应用组装、账户快照/轮询、统一写锁、收藏草稿和操作回调；后续仍需按账户协调与收藏编辑拆分 |
| `src/app/routes.ts` | 路由类型、解析与导航标签；直接链接的协议入口 |
| `src/navigation.ts`、`src/app/Link.tsx` | 浏览器历史与应用内链接，保留原生修饰键行为 |
| `src/features/collections/PhotoCard.tsx`、`presentation.ts` | 收藏卡片、分类和日期显示；不发请求、不维护账户状态 |
| `src/features/explore/ExplorePage.tsx`、`communityItem.ts` | 探索页展示与社区条目转换；互动通过明确回调交给应用协调 |
| `src/features/shares/SharesPage.tsx` | 分享页展示；未读筛选与操作仍由应用持有，路由切换保留原有行为 |
| `src/shared/ui.tsx` | 跨页面纯展示组件；不依赖业务页面、会话或请求层 |
| `src/ReliefViewer.tsx` | Three.js 模型展示、40° 限制与资源释放；下载仍经过 `api.ts` |
| `src/data.ts` | 服务端快照的共享数据类型与校验契约 |

页面组件只接受所需数据、忙状态和动作回调，不接收整个账户 store 或各自调用网络。依赖方向为应用组装 → 业务页面 → 共享展示/数据类型；共享组件不反向依赖业务页面。不要为每个页面复制 transport、revision 防倒退逻辑或写锁。

当前拆分保留原有行为：探索分类与未读筛选的状态所有权不迁移，统一写锁仍在 `Prototype`。F03 的懒加载错误隔离、F04 的挂起正文超时回归是后续独立任务，不能因页面提取或构建通过而视为完成。

## 开发与验证

正式环境运行于 AWS EC2，身份使用 Cognito，文件使用私有 S3。详情见 [后端说明](../backend/README.md)、[API 约定](../docs/local-api-contract.md) 与 [EC2 部署验收](../docs/EC2部署验收.md)。本机开发也使用同源 `/api`，Vite 将其代理至 `127.0.0.1:8000`；无需跨域 Cookie 或旧浏览器迁移配置。后端必须先配置有效身份服务与初始化模板。

```powershell
npm ci
npm run dev -- --host 127.0.0.1 --port 4173 --strictPort
npm run check:runtime
npm run build
node --test tests/data.test.mjs tests/api.test.mjs tests/relief-angle.test.mjs
npx playwright test --config playwright.web.config.ts
```

Web 运行约束检查验证生产依赖图不含设备模拟器、不自动迁移旧 IndexedDB、网络访问经过会话层且允许浏览器缩放。浏览器测试覆盖响应式布局、原生表单、深链与返回、401/换号隔离、CRUD 和浮雕行为；测试夹具不代表真实 Google 授权或邮件投递成功。`npm run test:sites` 仍只检查静态 Worker 输出，不发布站点。
