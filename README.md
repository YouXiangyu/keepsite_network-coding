# MemoVerse · 个人电影票整理与多端同步

当前需求以[产品规格](docs/项目定义与决策主文档.md)为准，按[开发计划](docs/开发计划.md)的 M1—M7 顺序实施。先完成电脑、手机、平板共用的响应式 Web，再开发独立 Kotlin Android 客户端。

## 当前实现

M1 将应用收窄为个人电影票：新用户空票夹，原票上传，片名、影院、影厅、座位和放映时间编辑，详情及删除。Google/邮箱认证、服务端 HttpOnly 会话、CSRF 和账号隔离继续保留。

新运行数据使用独立 SQLite schema 3；原票存储沿用私有 S3，显式本地开发存储会标为“未配置云存储”。只有已提交的有效票根引用才能读取图片，上传成功本身不等于票根保存成功。按字段修改使用稳定操作标识，删除保留墓碑，旧写入不能复活已删除记录。

OCR/海报流水线、Redis、增量与 WebSocket、浏览器持久缓存、P2P/免费 TURN 和 Android 按后续里程碑实现。M1 的处理状态是“尚未处理”，本机图片加载也不等于持久缓存完成。测试与验收的具体完成情况见开发计划，不以文档描述代替验证。

浮雕、分类标签、分享、社区及旧模板调用链已退出新版。历史实现与资料可从 Git 检查点 `d7945d8` 追溯；实际数据库、照片、模型缓存、S3 对象和回滚文件保持原样。

## 代码入口

| 范围 | 实现 | 验证 |
| --- | --- | --- |
| 身份、会话、CSRF | `backend/auth.py`、`demo/src/AuthGate.tsx`、`demo/src/api.ts` | 身份单测、真实 BFF 组合测试、Web 换号和迟到响应测试 |
| 票根、字段来源、操作幂等与墓碑 | `backend/contracts.py`、`store.py`、`services/tickets.py`、`state.py` | API、双账号、删除和重试测试 |
| 图片校验、私有存储、有效引用授权 | `backend/media.py`、`object_storage.py`、`api.py` | 文件类型/大小/哈希、存储故障和媒体授权测试 |
| 响应式 Web | `demo/src/Prototype.tsx`、`app/`、`features/tickets/` | 运行时契约、单测、浏览器测试和生产构建 |
| 账号备份、隔离恢复 | `backend/backups.py`、`scripts/restore-account-backup.py` | 资源限制、ZIP 安全、归属、墓碑和完整性验证 |
| 旧数据迁移 | `scripts/migrate-movie-tickets.py` | `scripts/tests/test_ticket_migration.py` |
| 制品发布和回滚 | `scripts/deploy-ubuntu.ps1`、`deploy/ubuntu/` | 隔离发布恢复测试 |

前端网络请求集中在会话作用域内的 `api.ts`，业务写入通过账户级协调入口；后端对象存储 I/O 不占数据库写事务。模块配置和运行命令见[后端说明](backend/README.md)与[前端说明](demo/README.md)。

## 旧数据迁移与保护

新程序不在启动时升级旧库，也不把旧全局数据归给首次登录者。已有 `backend/data`、`backend/data-v2` 和服务器旧目录保留；M1 使用 `backend/data-v3`，正式服务器候选目录为 `/var/lib/memoverse-v3`。

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

在 `demo/` 运行 `npm run check:runtime`、`npm run test:unit`、`npm run build`、`npm run test:web` 和生产构建故障测试。测试使用隔离数据和测试身份，不替代真实 Google/邮箱、S3、Redis、OCR、TURN 或真机验收。

GitHub PR 的 `Backend tests`、`Frontend tests` 与 `deployment-recovery` 检查必须对应最新提交；独立代码审查也必须覆盖最新提交。开发交付到提交与 PR，部署、EC2 启停和合并另等用户明确要求，不自动执行。

## 部署信息

既有环境为 AWS 东京 EC2、Cognito、私有 S3 和 [keepsite.space](https://keepsite.space)。定位信息见[项目运行信息](.agents/skills/memoverse-delivery/references/project-context.md)；当前状态必须现场查询。新版代码或 PR 不代表已部署。发布与回滚说明见 [Ubuntu 部署](deploy/ubuntu/README.md)。

每次 EC2 开机 58 分钟后自动发起正常关机，本次开发不改变该规则。停机仍保留磁盘与 S3 数据费用；数据持久存在不代表服务停止时还能登录或获取下载授权。

[可靠性验收](docs/可靠性与解耦验收.md)和[EC2 验收](docs/EC2部署验收.md)是各自历史版本的证据，不能替代新版验收。Git 不包含数据库、用户照片、凭据、模型或构建缓存；第三方组件说明见 [THIRD_PARTY.md](THIRD_PARTY.md)。
