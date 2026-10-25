# keepsite · 个人电影票整理与多端同步

V0 基线：用响应式 Web 与 Kotlin Android 整理个人电影票，在同一账号的设备间同步。后续进入问题修复与增量迭代；V0 是产品阶段名称，本次不改变包版本或创建发布标签。

支持原票上传、字段编辑、服务端 OCR、TMDB/Commons 海报、邮箱密码与 Google 登录及显式账户绑定。原票与海报存入私有 S3；设备缓存和受限 P2P 用于读取加速。处理失败时保留原票，清缓存不删除云端记录。

当前需求见[产品规格](docs/项目定义与决策主文档.md)，后续工作见[开发计划](docs/开发计划.md)，已验证范围及限制见[V0 验证摘要](docs/V0验证摘要.md)。

## 本地开发入口

分别按[后端配置与启动](backend/README.md)、[Web 开发](demo/README.md)和[Android 构建](android/README.md)准备环境。完整云端功能需要自行配置 Cognito、私有 S3、Redis、OCR 模型及海报来源；密钥仅放本机环境文件或服务器配置，不放进源码或 APK。

仓库尚未选择项目开源许可证；公开前需要确定 LICENSE。第三方许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 代码入口

| 范围 | 实现 | 验证 |
| --- | --- | --- |
| 身份、会话、CSRF | `backend/auth.py`、`demo/src/AuthGate.tsx`、`demo/src/api.ts` | 身份单测、真实 BFF 组合测试、Web 换号和迟到响应测试 |
| 票根、字段来源、操作幂等与墓碑 | `backend/contracts.py`、`store.py`、`services/tickets.py`、`state.py` | API、双账号、删除和重试测试 |
| 图片校验、私有存储、有效引用授权 | `backend/media.py`、`object_storage.py`、`api.py` | 文件类型/大小/哈希、存储故障和媒体授权测试 |
| 可恢复上传、持久变更、Redis 缓存 | `backend/media.py`、`store.py`、`shared_state.py`、`state.py` | 上传未知结果、事务原子性、缓存损坏/丢失与真实 Redis CI |
| OCR/海报流水线 | `backend/processing.py`、`processing_repository.py`、`ocr.py`、`posters.py` | Streams 恢复、任务租约、纠错/删除竞态、海报发布故障、固定模型与合成测量 |
| 分页、持久增量与通知 | `backend/sync_http.py`、`sync_notifications.py`、`sync_socket.py`、`demo/src/app/accountSync.ts` | 快照一致性、游标边界、写入确认、断线补拉、会话撤销及真实 WebSocket/Redis |
| 响应式 Web | `demo/src/Prototype.tsx`、`app/`、`features/tickets/` | 运行时契约、单测、浏览器测试和生产构建 |
| 本机媒体缓存 | `demo/src/mediaCache.ts`、`MediaProvider.tsx` | 账号隔离、全量清理后恢复、空间不足、完整性与按需加载 |
| 本人设备传输 | `backend/peer_service.py`、`peer_registry.py`、`demo/src/media/` | 授权信令、实际 DataChannel、校验与续传、服务器回退和资源回收 |
| Kotlin Android | `android/app/src/main/`、`backend/native_auth.py` | JVM 协议/缓存测试、原生仪器测试及 Web 双向联调；真机验收由用户执行 |
| 综合测量 | `scripts/benchmark-browser.mjs`、`scripts/benchmark-processing.py` | 300 条元数据的浏览器矩阵、完整任务采样与真实隔离 OCR 流水线；环境和限制见报告 |
| 账号备份、隔离恢复 | `backend/backups.py`、`scripts/restore-account-backup.py` | 资源限制、ZIP 安全、归属、墓碑和完整性验证 |
| 旧数据迁移 | `scripts/migrate-movie-tickets.py` | `scripts/tests/test_ticket_migration.py` |
| 制品发布和回滚 | `scripts/deploy-ubuntu.ps1`、`deploy/ubuntu/` | 隔离发布恢复测试 |

前端网络请求集中在会话作用域内的 `api.ts`，业务写入通过账户级协调入口；后端对象存储 I/O 不占数据库写事务。模块配置和运行命令见[后端说明](backend/README.md)与[前端说明](demo/README.md)。

## 旧数据迁移与保护

2026-10-24 软件正式名称统一为 `keepsite`，包括客户端包名、环境变量、数据库文件名与部署服务名。已有安装与服务器不能直接原地替换；重命名迁移步骤见 [keepsite 迁移说明](docs/keepsite迁移.md)。原用户账户 ID、原票与云端对象不因改名改变。

新程序不在启动时升级旧库，也不把旧全局数据归给首次登录者。已有 `backend/data`、`backend/data-v2` 和服务器旧目录保留；M1 使用 `backend/data-v3`，正式服务器目录为 `/var/lib/keepsite-v3`。

迁移工具默认只读清单：

```powershell
python scripts/migrate-movie-tickets.py --source-snapshot D:/isolated/source-schema2.sqlite3
```

只有明确指定 `--execute --source-media <本地原图副本目录> --destination <全新隔离目录>` 才创建新库。输入须为一致的离线 schema 2 快照；存在 WAL/回滚日志时拒绝。工具核对归属、原图类型、尺寸与 SHA-256，保留完整旧数据库和迁移报告，不联网、不修改源库、不删除源文件。

只导入旧 `category=movie` 记录及可核实原票；标题标为迁移来源，旧收藏日期不推断为放映时间，其余电影字段留空待纠错。非电影、社交与模型元数据保留在旧数据库副本中。实际云数据迁移、旧对象和 S3 历史版本回收仍须明确方案及授权；隔离本地副本不代表已经恢复到 S3。

## 验证与交付

在根目录运行后端与迁移/基准测试：

```powershell
python -B -m pytest backend/tests scripts/tests -q
python -B -m pytest deploy/ubuntu/tests -q
```

在 `demo/` 运行 `npm run check:runtime`、`npm run test:unit`、`npm run build`、`npm run test:web` 和生产构建故障测试。测试使用隔离数据和测试身份；真实 Redis、固定 OCR 模型由专门测试验证，真实 Google/邮箱、S3、实际票据、TURN 和真机另行验收。

GitHub PR 与 `codex/**` 分支推送运行 `Backend tests`、`Frontend tests`、`Android tests` 和 `deployment-recovery`；后端 CI 使用隔离 Redis 服务执行真实客户端验证。Android CI 编译应用与仪器测试、运行 JVM 测试及 Lint，不将编译成功称为设备测试通过。合并前检查必须对应最新提交，独立代码审查也必须覆盖最新提交。开发交付到提交与 PR，部署、EC2 启停和合并另等用户明确要求，不自动执行。

## 部署信息

既有环境为 AWS 东京 EC2、Cognito、私有 S3 和 [keepsite.space](https://keepsite.space)。定位信息见[项目运行信息](.agents/skills/keepsite-delivery/references/project-context.md)；当前状态必须现场查询。新版代码或 PR 不代表已部署。发布与回滚说明见 [Ubuntu 部署](deploy/ubuntu/README.md)。

每次 EC2 开机 58 分钟后自动发起正常关机，本次开发不改变该规则。停机仍保留磁盘与 S3 数据费用；数据持久存在不代表服务停止时还能登录或获取下载授权。

历史验收已收拢到 [V0 验证摘要](docs/V0验证摘要.md)，原始报告保留在 Git 历史中，不能替代后续版本验收。Git 不包含数据库、用户照片、凭据、模型或构建缓存；第三方组件说明见 [THIRD_PARTY.md](THIRD_PARTY.md)。
