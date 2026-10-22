# MemoVerse 电影票 Web

更新日期：2026-10-22。产品规则见[主规格](../docs/项目定义与决策主文档.md)，执行顺序见[开发计划](../docs/开发计划.md)。

当前实现 M1：同一响应式 Web 提供空票夹、原票上传、电影字段编辑、详情、删除以及真实保存状态。保留 Google 与邮箱认证、服务端 HttpOnly 会话、CSRF、账号隔离。旧分类、标签、社交、浮雕页面和 API 调用已移除。

## 运行边界

- 页面：`/tickets`、`/tickets/new`、`/tickets/:id`、`/tickets/:id/edit`、`/devices`。浏览器刷新、直接链接和前进后退可用；生产入口不加载模拟手机运行时。
- 登录确认后才请求账户快照；新账户从空票夹开始，不复制模板、不读取旧浏览器业务数据。
- 上传 JPG/PNG/WebP，最多 12 MiB。片名、影院、影厅、座位、放映时间均可留空；编辑只提交变化字段。
- 原票先上传，再确认票根引用。仅 `cloudStatus=saved` 显示已保存到云端；本地未配置 S3 时明确显示当前服务器保存、尚未云存。
- 当前没有 OCR、海报抓取、浏览器持久媒体缓存、设备发现、WebSocket 或 P2P。界面明确显示未启用。详情每 10 秒读取快照；其他页面可手动刷新，游标同步留待 M4。
- 保存请求结果未知时保留页面草稿、上传结果及稳定操作标识；相同内容重试复用标识，改变内容使用新标识。核对通过账号隔离的持久操作回执确认提交是否已生效，再读取最新票根；只清除该次已确认字段和照片，提交之后的新编辑仍保留。已创建记录可转为编辑同一 ID，避免重复创建；已被删除的记录不能重新提交。页面关闭或清除浏览器数据不能恢复未提交草稿。
- 退出/会话失效/换号取消旧请求，迟到响应不能覆盖新账号，临时 object URL 在替换、离开编辑及退出时释放。退出不删除云端记录。

## 责任地图

| 文件 | 职责 |
| --- | --- |
| `src/App.tsx`、`src/AuthGate.tsx` | 认证、会话续查与账户业务树边界 |
| `src/api.ts` | 唯一网络入口、超时、CSRF、会话 epoch 与响应校验 |
| `src/data.ts` | Ticket、媒体、字段来源与 schema 验证 |
| `src/Prototype.tsx` | 路由组装、票夹、详情、设备状态与删除 |
| `src/app/useAccountWorkspace.ts` | 全量快照、防倒退、写操作锁和详情轮询 |
| `src/features/tickets/TicketEditor.tsx` | 上传草稿、按字段修改、操作标识与结果核对 |
| `src/features/tickets/TicketImage.tsx` | 原票/封面显示和读取失败反馈 |
| `src/app/routes.ts`、`src/navigation.ts` | 唯一新路由与浏览器历史 |

`src/mobile/` 与 mobile runtime lock 是独立归档，不在生产依赖图；其依赖仅为归档测试保留。Android 工程在 Web 完成后的 M7 于 `demo/` 之外建立。

## 开发与验证

Vite 的同源 `/api` 代理指向 `127.0.0.1:8000`；后端需配置认证，参见[后端说明](../backend/README.md)。不在前端存储身份提供方 token。

```powershell
npm ci
npm run dev -- --host 127.0.0.1 --port 4173 --strictPort
npm run check:runtime
npm run test:unit
npm run build
npm run test:web
npm run test:reliability
```

浏览器测试使用隔离合成数据，覆盖认证门禁、空票夹、上传/纠错/删除、账号切换、原生表单、会话失效、旧轮询与临时图片释放，宽度为 360/390、768、1280/1440。生产可靠性测试使用仅监听本机的独立 HTTP 服务，真实发送成功头后挂起保存响应正文，检验超时草稿、幂等重试与换号取消。模拟认证不证明实际 Google 登录或邮件投递；真机验收由用户执行。

`test:sites` 只校验静态 Worker 输出。测试、构建不代表部署；发布、服务器启停及合并另需用户明确要求。
