# MemoVerse 账号后端

FastAPI 在服务器 `127.0.0.1:8000` 提供 API，由 Nginx HTTPS 同源代理。当前实现为账号隔离的 SQLite schema 2、Cognito 身份服务和显式配置的对象存储。正式环境使用 AWS S3；本地文件存储只用于显式指定的开发和测试环境，不作为 S3 故障时的静默回退。

## 数据与权限

- 业务数据库与加密会话库位于新的 `MEMOVERSE_DATA_DIR`。默认本机目录为 `backend/data-v2`，正式目录为 `/var/lib/memoverse-v2`。旧 schema 1 会被拒绝，不自动升级或覆盖 `backend/data` / `/var/lib/memoverse`。
- 每个账号的收藏、分享、社区示例互动、资料、revision 和生成任务独立。后端按认证身份查询和写入；同一个模板 ID 在不同账户间不能串数据。
- 初始化模板由运维提供、版本与文件哈希校验通过后使用。第一次完成身份验证后原子初始化，重复登录不重置。模板文件可以复用，账号授权关系与可编辑记录独立。
- 图片与模型保持不可变对象：`media/<sha>.<ext>`、`models/<modelId>/*`。访问先验证会话与账号授权，再从私有对象存储传输。匿名、退出会话、仅猜到 ID 都不能读取。
- 上传解码校验 JPG/PNG/WebP，最多 12 MiB、4000 万像素。对象上传成功后才提交引用；网络 I/O 不持有 SQLite 写锁。实例角色不具备对象删除权限，删除收藏不会破坏其他账号或模板的共享文件。
- 单 worker 处理各账户的持久任务；完成时再次核对 owner、job 与输入版本。中断任务可恢复，失败不报告 ready，不复活已删除记录。
- 每账号 ZIP 备份只包含本人有权访问的数据与文件，包括分享/社区模板；不导出身份令牌、会话数据库或其他用户数据。全站运维备份与用户导出是不同范围。

## 鉴权

Cognito 提供邮箱密码、验证码确认、重置密码以及 Google 联合登录。Google 使用授权码、PKCE、state 和 nonce；JWT 校验签名、issuer、audience/client_id、用途、有效期、用户标识与邮箱验证状态，不按邮箱自动合并不同身份。

浏览器只保存 HttpOnly 会话 Cookie；身份提供方令牌由服务器加密保存。会话有绝对有效期，短效令牌自动轮换。业务写请求要求精确 Origin、`X-MemoVerse-Client: web` 和会话的 `X-CSRF-Token`。注销先使本地会话失效，再撤销上游令牌。Nginx 和 Uvicorn 不记录 OAuth 回调查询参数。

未配置完整身份服务时，匿名状态返回 `configured: false`，登录拒绝，业务仍要求认证。生产没有测试账户后门、模拟提供方开关或固定请求头绕过认证。

## 必需配置

| 配置 | 用途 |
| --- | --- |
| `MEMOVERSE_DATA_DIR` | 新 schema 2 账号数据库、会话和临时工作目录 |
| `MEMOVERSE_TEMPLATE_DIR` | 已核验初始化模板目录 |
| `MEMOVERSE_STORAGE` | 明确为 `s3` 或开发测试的 `local` |
| `MEMOVERSE_S3_BUCKET`、`AWS_REGION` | 私有存储桶和区域；服务器凭据来自实例角色 |
| `MEMOVERSE_COGNITO_REGION`、`MEMOVERSE_COGNITO_USER_POOL_ID`、`MEMOVERSE_COGNITO_CLIENT_ID` | Cognito 用户池和客户端 |
| `MEMOVERSE_COGNITO_DOMAIN` | 完整 HTTPS Cognito 登录域名 |
| `MEMOVERSE_PUBLIC_URL` | 精确网站 origin；生产必须 HTTPS |
| `MEMOVERSE_SESSION_KEY` | Fernet 会话加密密钥，仅保存在受限配置文件 |
| `MEMOVERSE_GOOGLE_ENABLED` | Google 提供方实际接通后为 `true` |

部署入口与配置切换见 [Ubuntu 部署说明](../deploy/ubuntu/README.md)，实时资源定位见[项目运行信息](../.agents/skills/memoverse-delivery/references/project-context.md)。不得将真实配置或凭据写入 Git。

## 开发验证

Python 3.12/3.13，依赖放 `backend/.venv`。配置有效身份服务和模板后，开发可显式选择 `MEMOVERSE_STORAGE=local`，通过 Vite `/api` 代理同源访问；生产验收在 EC2 进行。

```powershell
backend/.venv/Scripts/python.exe -m pip install --cache-dir .cache/pip -r backend/requirements-dev.txt
backend/.venv/Scripts/python.exe -m pytest backend/tests --basetemp .cache/pytest-account-check -q
backend/.venv/Scripts/python.exe -B scripts/prepare-initial-template.py --verify
```

`--basetemp` 只能指向新的测试目录，不得使用真实数据目录或此前被拒绝清理的目录。模拟 provider 的测试覆盖认证协议与账户边界；真实 Google 同意及邮箱投递另行验收。AnyImage 仍是固定 MoGe2 CPU 推理加项目浮雕网格导出，模型缓存不在对象存储中充当用户原图，也不需要为每个新账户重复生成相同模板。
