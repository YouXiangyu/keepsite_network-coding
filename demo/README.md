# keepsite 电影票 Web

更新日期：2026-10-22。产品规则见[主规格](../docs/项目定义与决策主文档.md)，执行顺序见[开发计划](../docs/开发计划.md)。

当前 Web 已接入 M1—M5：同一响应式 Web 提供空票夹、原票上传、电影字段编辑、详情、删除、服务端处理状态、海报出处、分页快照、增量游标、WebSocket 提示及本人在线页面间的 P2P 加速。保留 Google 与邮箱认证、服务端 HttpOnly 会话、CSRF、账号隔离、可回收媒体缓存和云端恢复。旧分类、标签、社交、浮雕页面和 API 调用已移除。

2026-10-24 改名为 lowercase `keepsite`：Web 使用 `keepsite-media-cache-v1` 图片缓存及 `X-Keepsite-Client` 请求头，与后端新 cookie 协议同步。旧 IndexedDB 仅为可回收媒体缓存，保留原样且不读取、不自动删除；新缓存通过同账号授权媒体读取重新填充。切换后需用原登录方式重新登录，同一服务端账号和已保存记录保持不变。尚未提交的页面草稿应在刷新或切换前完成保存并确认，改名不提供未提交草稿恢复保证。

## 运行边界

- 页面：`/tickets`、`/tickets/new`、`/tickets/:id`、`/tickets/:id/edit`、`/devices`。浏览器刷新、直接链接和前进后退可用；生产入口不加载模拟手机运行时。
- 登录确认后才请求账户快照；新账户从空票夹开始，不复制模板、不读取旧浏览器业务数据。
- 上传 JPG/PNG/WebP，最多 12 MiB。片名、影院、影厅、座位、放映时间均可留空；编辑只提交变化字段。
- 原票先上传，再确认票根引用。仅 `cloudStatus=saved` 显示已保存到云端；本地未配置 S3 时明确显示当前服务器保存、尚未云存。
- OCR/海报处理是否可用以服务端 `capabilities.processing` 和 `processingUnavailableReason` 为准；未就绪时展示配置原因，保留手动编辑。处理进度通过增量同步更新。`capabilities.deviceSync` 表示实现了设备协议，当前是否可用仍以登记响应为准。
- 每次新选择的文件生成稳定 `uploadId`，相同文件重试复用该标识。上传响应丢失先查询 `/api/uploads/{uploadId}`，核对文件哈希后复用已存对象；仅上传对象尚不能确认票根云存成功。保存请求结果未知时保留页面草稿、上传结果及稳定操作标识；相同内容重试复用标识，改变内容使用新标识。核对通过账号隔离的持久操作回执确认提交是否已生效，再读取最新票根；只清除该次已确认字段和照片，提交之后的新编辑仍保留。已创建记录可转为编辑同一 ID，避免重复创建；已被删除的记录不能重新提交。页面关闭或清除浏览器数据不能恢复未提交草稿。
- 退出/会话失效/换号取消旧请求，迟到响应不能覆盖新账号，临时 object URL 在替换、离开编辑及退出时释放。退出不删除云端记录。

## 处理状态与海报

- 原票保存完成即可返回，页面分别显示云端保存、`queued`/`ocr`/`poster` 等任务阶段及本机缓存状态。缓存可用不表示服务端任务完成，任务完成也不表示所有设备已缓存图片。
- 处理进行中（含已登记的重试）保留当前封面，直到服务端提交新结果；本次处理以无匹配或失败终结时回退原票封面并显示实际结果。失败、无匹配和已有结果均可按服务端能力重试。
- `POST /api/tickets/{id}/process` 使用稳定操作标识。结果未知时保留同一标识重试，或查询持久操作回执再读取状态；退出、换号和同账号会话续期仍遵守请求取消与旧响应隔离。
- 详情与编辑显示手动填写、识别结果、迁移保留等字段来源。识别期间打开的表单只自动更新未触碰字段；本地纠错继续保留，提交只包含用户触碰且值发生变化、或需要确认为手动来源的字段；即使值与并发 OCR 相同，也会保存手动来源。新建时显式清空的字段作为用户空值提交，未触碰的空字段仍可由 OCR 填写。用户字段与旧任务的冲突最终由后端按输入版本和字段来源处理。
- 已发布海报显示 Commons 来源、作者与许可。前端仅接受 HTTPS Commons `File:` 页面及对应 Creative Commons 许可白名单；公共领域许可可链接该来源页的 Licensing 段落。作者仅作为文本显示，不执行 HTML；浏览器不直接抓取来源站海报。
- 这些浏览器回归使用合成任务与图片，不能证明真实模型精度、内存预算、海报命中率或第三方来源许可审核；相应实测由后端 M3 验证记录提供。

## 快照、增量与重连

- `/api/sync/snapshot` 每页请求 100 条；校验相同 `serverId/userId/snapshotId/cursor/total/expiresAt`、连续 offset 和不重复 ID，全部页面及快照后的增量完成后才整体发布和修剪缓存。单页上限 200 条/1 MiB，完整快照上限 10,000 条/16 MiB；到期由服务器判断，不使用设备时钟拒绝有效快照。
- `/api/sync/changes` 严格逐条推进连续游标，应用 upsert 与 delete tombstone；不跳过写回执前的其他变化。生产不再调用 `/api/state` 或周期性轮询完整票夹。
- 写入回执和持久操作核对均携带 `serverId` 与 `committedCursor`。只有同域本地视图追上回执水位才确认；域变化先重建快照，旧域高游标不能确认新域操作。核对期间再次换域时保留未确认草稿。
- `/api/sync/events` 只接收 `sync.ready`/`sync.hint` 水位提示；重复、倒序、同水位健康心跳不触发 HTTP。断开后按 1—30 秒退避拉增量并重连，8 秒未 ready 或 45 秒没有有效心跳会关闭连接；页面获焦、重新联网和显式重试会校准。WebSocket 关闭本身不会退出登录，HTTP 鉴权失效才结束会话。
- `410` 游标/快照失效或服务身份变化触发有界重建。完整替换完成前保留当前页面与编辑草稿；远端删除保留草稿和已选照片，禁用向旧记录保存。会话 epoch、同步 generation 和 socket identity 阻止旧请求或连接覆盖新账号、新实例状态。

## 本人页面传输

- 元数据同步与设备信令复用唯一 `/api/sync/events`。页面获得账号快照后登记；服务端分配的 `peerId` 只表示当前在线页面，刷新、重连会重新登记，同一浏览器的两个标签页是两个连接。设备页可停用或重新启用本页 P2P，不影响元数据同步。隐藏页面暂停设备传输，回到前台再登记，不承诺锁屏或后台持续传输。
- Redis 不可用或登记状态丢失时，设备加速停用并使用现有云端路径；没有本机信令替代分支。每 15 秒核对在线状态与缓存清单；心跳确认丢失后撤回旧登记。重登记接受服务端当前清单版本，并从完整替换恢复，不能用空本地基线 patch 遗留旧 hash。
- 清单最多 1024 项，每批 32 项、串行等待 ACK；完整批次才成为可发现的清单。只登记当前有效引用对应的实际 IDB 缓存；成功缓存事务、LRU 淘汰、清空、引用删除及 focus 检查发现系统回收都会更新清单。暂存图片和 Blob URL 不登记。源端接受传输前重新读取并核验大小、MIME 与完整 SHA-256；哈希不是授权凭据。
- 传输授权绑定账号、服务空间、双方在线连接/会话和具体媒体；默认 10 秒短租期，按返回的剩余有效时间提前续期，过短或失效时停止。退出、断线、换号、换服务空间、删除、清缓存或关闭开关都会关闭相关 PeerConnection，迟到数据不能写缓存。
- 当前 `iceServers=[]`、`turnEnabled=false`，只验证主机候选直连；未配置公共 STUN、未启用未经免费额度核验的 TURN。跨公网 NAT 连接失败转云端。强制中继与真实目标网络尚未验收，不能把强制 relay 无路由后的云端恢复说成中继成功。
- 全页发送与接收合计最多 2 个 P2P 传输，超过直接走云端；总媒体读取仍最多 4 项。每文件单独使用 reliable/ordered DataChannel，完整帧含 8 字节 offset/length 头，最大 `min(16 KiB, negotiated maxMessageSize)`。发送前确保 `bufferedAmount + 完整帧 <= 256 KiB`，低水位 64 KiB，先监听再复查；控制、等待和取消都有边界。
- P2P 整体 12 秒预算包含发现、源端校验、连接与一次恢复；发现约 1 秒、连接最多 4 秒，2 秒无有效新字节或 ACK 则结束当前连接。仅明确传输中断允许一次按连续前缀续传，共用原 12 秒截止；RPC 拒绝、权限/用户取消、无源或完整性失败直接转云端。转云端前关闭旧 PC，不让两个路径同时完整下载。WebCrypto 校验不能中途终止，前后均检查会话与取消，单图最多 12 MiB。
- SDP 先进入有序发送链，再释放本地产生的 ICE；总信令帧 64 KiB、SDP 48 KiB、普通控制帧 4 KiB、ICE payload 2 KiB，双端合计最多 128 条 ICE 与 512 KiB signal payload。失败会关闭该次传输，不无限创建连接。
- 设备页展示实际在线页面、已确认缓存清单及最近 50 次传输。P2P payload、WS/DataChannel 控制、丢弃字节、S3 直读、同源服务器读取分别统计；应用层统计不含网络协议开销。直连/中继来自实际 selected candidate pair；缺少统计记 unknown，只有不同已知路径才记 mixed。机器可读记录包含发现、授权、连接、首字节、完整校验、源端读取与续传偏移，不保存 SDP、IP、凭据或签名 URL。

## 媒体缓存与恢复

- IndexedDB 只保存 `(accountId, sha256)` 寻址的图片 Blob、类型、大小和访问时间，不保存业务快照、身份令牌或短期签名 URL，不支持离线新增/编辑。
- 读取按有效本机缓存、有界本人页面 P2P、授权下载顺序执行。读取时验证 MIME、字节数和 SHA-256；损坏条目移除，下载校验失败不会展示为可用图片。
- 下载授权由 `/api/media/{assetId}/download` 返回。同源受保护流是默认路径；后端启用 S3 直签时，只向 HTTPS S3 地址发送不带凭据的请求。过期/403 最多重新授权一次，S3 CORS/网络失败回退同源流；请求与正文都有超时，最多接收记录声明的大小。
- 每账号 128 MiB LRU 上限，只逐出当前不活跃的图片。图片持续跟踪近视口 200px，离屏取消读取、释放 Blob URL；滚回时按需重读缓存。整个读取/下载/校验链最多 4 项并发，折叠原票不预下载。
- 写缓存不可用或空间不足时，仍展示已校验的临时 Blob 并明确提示未缓存。缓存写入不是上传前提。
- 设备状态页实际读取当前账号缓存清单；页面重获焦点也核对系统/其他标签页回收情况。清理只删除当前账号缓存，保留已在内存展示的图片并标记未缓存；之后再次需要时才恢复下载，不因清理立即回填。
- 最新服务器快照决定有效媒体引用；删除后只回收本账号不再被任何有效票根引用的缓存。退出不删除持久缓存，换号不复用；下载、校验后写入及临时 URL 均受会话 epoch 和取消信号约束。
- 无引用缓存清理遇到暂时的 IndexedDB 故障时保留待清理标记并显示提示；返回前台或后续状态校准时重试。只有当前引用对应的清理事务成功才清除标记，读取缓存统计成功不能掩盖清理失败。显式清空失败优先提示，不增加后台重试循环。
- 测试中的 S3 授权与 CORS 故障是模拟验证，不能替代真实桶策略、跨域配置或真实设备验收；本阶段不自动修改云端配置。

## 责任地图

| 文件 | 职责 |
| --- | --- |
| `src/App.tsx`、`src/AuthGate.tsx` | 认证、会话续查与账户业务树边界 |
| `src/api.ts` | 唯一网络入口、超时、CSRF、会话 epoch 与响应校验 |
| `src/data.ts` | Ticket、媒体、字段来源与 schema 验证 |
| `src/Prototype.tsx` | 路由组装、票夹、详情、设备状态与删除 |
| `src/app/accountSync.ts` | 单一拉取循环、原子分页、连续增量、写入屏障、重连及代际隔离 |
| `src/app/useAccountWorkspace.ts` | React 订阅、独立写操作锁、回执核对与页面生命周期 |
| `src/media/peerProtocol.ts`、`peerMedia.ts` | 设备/信令类型、登记、清单 ACK、授权租期、恢复预算、路径观测与取消 |
| `src/media/dataChannel.ts` | 有序分块、完整帧背压、连续前缀、最终确认与实际连接统计 |
| `src/features/tickets/DeviceStatus.tsx` | 本页 P2P 开关、实际在线页面、缓存和传输观测 |
| `src/features/tickets/TicketEditor.tsx` | 上传草稿、按字段修改、操作标识与结果核对 |
| `src/features/tickets/ProcessingStatus.tsx`、`processing.ts` | 服务端处理阶段、配置原因、幂等重试与结果核对 |
| `src/features/tickets/PosterAttribution.tsx` | 已校验海报出处、作者与许可链接 |
| `src/features/tickets/TicketImage.tsx` | 近视口原票/封面显示、释放和读取失败反馈 |
| `src/MediaProvider.tsx`、`src/mediaCache.ts` | 账号媒体生命周期、校验、IndexedDB、LRU、读取队列和清理状态 |
| `src/app/routes.ts`、`src/navigation.ts` | 唯一新路由与浏览器历史 |

`src/mobile/` 与 mobile runtime lock 是独立归档，不在生产依赖图；其依赖仅为归档测试保留。Android 工程在 Web 完成后的 M7 于 `demo/` 之外建立。

## 开发与验证

Vite 的同源 `/api` 代理指向 `127.0.0.1:8000`，启用 WebSocket 转发；后端需配置认证，参见[后端说明](../backend/README.md)。不在前端存储身份提供方 token。

```powershell
npm ci
npm run dev -- --host 127.0.0.1 --port 4173 --strictPort
npm run check:runtime
npm run test:unit
npm run build
npm run test:web
npm run test:reliability
npm run test:p2p
```

浏览器测试使用隔离合成数据，覆盖认证门禁、空票夹、上传/纠错/删除、账号切换、原生表单、会话失效、迟到增量与临时图片释放，宽度为 360/390、768、1280/1440。可靠性测试使用仅监听本机的独立 HTTP/WebSocket 服务，真实发送成功头后挂起保存响应正文，检验超时草稿、上传意图核对、幂等重试与换号取消。原生 IndexedDB 测试覆盖双账号、清空恢复、quota、损坏、外部回收、删除共享引用以及百条合成票根的按需读取和并发上限。处理回归覆盖各阶段、拉取故障恢复、纠错、海报发布切换、许可白名单和处理请求超时/续期/换号。M4 回归还验证跨页原子发布、重复 ID、游标失效重建、订阅空窗补齐、写入屏障、服务域变化、重连/心跳看门狗、远端删除草稿保留，以及 201 条快照后单条更新的响应字节减少。模拟认证不证明实际 Google 登录或邮件投递；真机验收由用户执行。

`test:sites` 只校验静态 Worker 输出。测试、构建不代表部署；发布、服务器启停及合并另需用户明确要求。

`test:p2p` 需先 build，使用两个独立 Chromium 进程、真实 FastAPI/WS/SQL、fakeredis 与本机对象存储，端口 4177。合成图片独立核验完整 SHA，记录目标媒体 HTTP 次数、DataChannel 实际帧、候选类型和故障恢复。测试中的服务器媒体字节不算 S3；它不证明真实 Redis 多节点、S3 桶策略、TURN 或物理手机互通。夹具通过专用退出入口正常关闭并等待自己的临时目录移除，避免 Windows 强制结束进程跳过清理。

## 独立浏览器测量入口

`npm run build:benchmark` 将 `benchmark/` 构建到仓库 `.cache/m6-bench-client`，不替换生产入口或 `dist/client`。夹具只允许 `http://127.0.0.1:4178`；通过公开的 AccountSync、AccountMediaCache、PeerMedia 接口执行真实读取与缓存，不修改私有状态、生产配额或连接统计。

`window.__keepsiteBenchmark` 提供 boot、status、setPeerWanted、run、verify、dispose。每阶段最多 300 个不同的自有媒体，任务并发只允许 1/2/4，总截止 120 秒。每个任务从 acquire 到真实缓存终态记录耗时和来源；失败与未调度任务保留，缺失时间不补零。verify 在计时之外逐项读取实际 IDB 并核验大小、MIME 和 SHA-256。生产诊断视图本身只保留最近 50 条，测量入口持续收集并明确报告覆盖情况，不用最后 50 条代替全量任务样本。

运行器是仓库 `scripts/benchmark-browser.mjs`，未加 `--execute` 时只显示计划，输出目录必须是新的绝对路径。正式矩阵使用两个独立 Chromium 进程、300 条元数据及 300 个独立合成 PNG，从中固定选取 60 张 working set；HTTP-only 冷读取、P2P 源预热后的目标冷读取、目标重载后的暖读取分别测试任务并发 1/2/4。每个配置的源端 HTTP 预热成本单列；暖缓存仅指该 working set，不代表整库预热。具体源码、样本、结果和限制见[开发计划](../docs/开发计划.md)。

该测量使用本机对象存储和 fakeredis，保留生产限流及 P2P 两槽/媒体四槽。浏览器端任务耗时包含校验与缓存完成；服务夹具的 ASGI 响应持续时间包含流式输出与背压，不等于纯业务 CPU 或浏览器收完正文的时间。这里的 HTTP、WebSocket 和 DataChannel 应用层字节不代表 TCP/TLS/SCTP 线缆流量，也不用于声称真实 S3 性能、云费用或公网 P2P 成功率。

在仓库根目录运行：

```powershell
npm --prefix demo run build:benchmark
node --test scripts/tests/browser-benchmark.test.mjs
node scripts/benchmark-browser.mjs --output D:/100-code/110-Project/network-coding/.cache/new-browser-measurement
# 核对计划后显式执行；目录必须尚不存在。
node scripts/benchmark-browser.mjs --output D:/100-code/110-Project/network-coding/.cache/new-browser-measurement --execute
```

默认矩阵共 540 个主任务；小规模 CI 传 `--count 3 --sample 3`，用于验证链路，不能代替完整负载证据。正式运行保留完整 JSON 报告及源码/构建哈希，并关闭自有服务与浏览器。
