# Ubuntu 24.04 single-server deployment

This is the direct Linux deployment of the existing SQLite/FastAPI application.
No container runtime or cloud SDK is required. Preparation scripts default to a
printed plan; `--execute` is required for package installation or downloads.
They do not create an AWS instance, request a certificate, change DNS, generate a
default login password, create swap, start the app or publish the site.

## Layout and ownership

| Path | Purpose |
| --- | --- |
| `/opt/memoverse/releases/<git-commit>` | Release source and locally built `demo/dist/client` |
| `/opt/memoverse/current` | Active release symlink, switched by the reviewed update procedure |
| `/opt/memoverse/shared/cache` | Shared AnyImage source, CPU wheels, model, pip cache and temporary files |
| `<release>/.cache` | Symlink to the shared cache; never a copy of Windows wheels |
| `<release>/backend/.venv` | Per-release Ubuntu Python 3.12 virtual environment |
| `/var/lib/memoverse` | Persistent SQLite, media, model outputs and temporary backup exports |
| `/etc/memoverse.env` | Private service environment, root:memoverse 0640 |
| `/etc/nginx/memoverse.htpasswd` | Operator-created BasicAuth password hashes, root:www-data 0640 |
| `/var/log/memoverse` | Nginx application logs; separate from distro nginx logrotate wildcard |

The `memoverse` system user has no login shell. Releases should be root-owned and
readable/traversable by both `memoverse` and Nginx's `www-data`; executable/source
directories need normal 0755 traversal and static files 0644 readability. The
service can write only its persistent data/shared cache and private temporary
directory. The installer does not recursively change ownership of uploaded user
data or arbitrary existing release directories.

## Initial preparation

Build the existing frontend on the development computer after its protected-runtime
check, with the API base configured for the final HTTPS origin. Upload source and
`demo/dist/client` into a commit-named release directory. Do not upload `.venv`,
`node_modules`, secrets, Windows dependency caches or local user media implicitly.

```bash
bash /opt/memoverse/releases/<commit>/deploy/ubuntu/install.sh \
  --release /opt/memoverse/releases/<commit> --domain keepsite.space
# After reviewing that plan:
sudo bash /opt/memoverse/releases/<commit>/deploy/ubuntu/install.sh \
  --release /opt/memoverse/releases/<commit> --domain keepsite.space --initialize-host --execute
```

The installer checks Ubuntu 24.04 and the uploaded release before modifying it.
Only explicit `--initialize-host` performs system package/user/global-log setup.
Ordinary release preparation verifies the initialized host and installs only the
release venv/model dependencies; it does not run apt or recreate the environment.
Initial host setup installs Nginx, Python venv/pip, unzip/git/curl, CA certificates, apache2-utils,
certbot, libgomp1, logrotate and GNU time. A temporary `policy-rc.d` prevents package
installation from starting the default Nginx site; it is removed on exit. If an
administrator already supplied that policy, the installer stops without replacing
it. A force-killed installation can leave the temporary policy: inspect its content
before removing it; do not delete an unrelated administrator policy.

The original `/etc/memoverse.env` is preserved on repeated runs. Check its allowed
origins and data directory explicitly. Its default origins include the selected
HTTPS domain and local port 4173 for SSH-tunnel tests. Only the operator writes
BasicAuth credentials; never put them in source, shell command arguments or logs.
Create/update the htpasswd interactively in the private server session, and check
permissions before exposing TLS. Existing passwords must not be overwritten by a
new deployment.

## Model and native dependencies

`scripts/setup-anyimage.sh` can also be run independently as the service user:

```bash
sudo -u memoverse bash /opt/memoverse/releases/<commit>/scripts/setup-anyimage.sh \
  --python /opt/memoverse/releases/<commit>/backend/.venv/bin/python
# Add --execute only after reviewing its plan.
```

It pins AnyImage to `2e2ab3eb9b5242deadca097c929c40329dbb5012`, skips unrelated
Git LFS assets, and installs Linux CPU binary wheels into `.cache/anyimage-deps`:
ONNXRuntime 1.24.4, trimesh 4.11.2 and NumPy 2.3.5, including their required
dependencies. The base venv provides Pillow and API packages. No Blender, Torch,
CUDA, background-removal model or upscaler is installed.

Only `moge-2-vits-normal-onnx/model.onnx` is downloaded: 140,852,051 bytes,
SHA256 `24eacb5dc7a2c54c7bc98f7de085ffbed79ad006ea5b664c2c2cdc02ff3a52f0`.
Existing models are reused only after hashing them; downloads use the pinned
official catalog/downloader and verify the result. Cached dependencies are tied
to the Python ABI/platform; a mismatch stops instead of overwriting a live cache.
Native imports and the CPU provider are checked before reporting preparation
success. This does **not** establish that a 1 GB machine can run inference safely.

Keep AnyImage's licence in its cached source. Do not modify that source to tune
the application; runtime knobs belong to the application's own adapter.

## Activation, TLS and authentication

1. Root separately prepares the planned 2 GB swap and verifies available memory
   and disk space. Swap is a buffer, not proof of sufficient inference memory.
2. Inspect the prepared release, dependencies and environment, then atomically set
   `/opt/memoverse/current` to that release. The installer never changes that link.
3. Start/enable `memoverse.service` deliberately. It binds only `127.0.0.1:8000`
   with **one** Uvicorn worker, so there is one persistent local job worker.
   Do not add reload, multiple Uvicorn workers or auto-sized Gunicorn workers.
4. Before DNS/TLS is ready, test through an SSH tunnel to loopback and the local
   frontend. Do not expose port 8000 or temporarily publish the application over
   plaintext HTTP. A tunnel is transport, not a substitute for final BasicAuth.
5. Once DNS is correct, the operator may configure a temporary port-80 server
   that serves **only** `/.well-known/acme-challenge/` from `/var/www/letsencrypt`
   and returns 404 elsewhere. Obtain the certificate with certbot's webroot mode.
   This avoids configuring the final 443 server before its certificate exists,
   and lets later renewals use the same challenge directory.
6. Create the private htpasswd, replace `__DOMAIN__` in `nginx.conf.template`,
   verify there are no placeholders, and install it as the site's config. Review
   other enabled Nginx sites (including the distro default), run `nginx -t`, then
   enable/reload Nginx. Do not remove an unrelated site's config automatically.
   Certificates and password hashes must exist before this step. Arrange a
   certificate renewal deploy hook that reloads Nginx after successful renewal.

The final template serves no application on port 80: ACME requests are files,
everything else redirects to HTTPS. All 443 application/static/API requests use
BasicAuth. `/api/...` is proxied with the original URI, no SPA fallback or access
to data directories; Nginx strips Authorization after authenticating it. The
frontend's local-client header remains in use but is not authentication itself.
Static content comes only from `current/demo/dist/client`; other routes use the
SPA's index. Photo uploads allow a 13 MiB HTTP body for a 12 MiB file plus multipart
overhead; the backend retains its 12 MiB content limit. API responses disable
proxy buffering and caching; long file downloads/backup responses are not loaded
into the Nginx response cache.

The domain rollout and credentials are operator tasks, not default script actions.
Only HTTP/HTTPS need public ingress; SSH should be scoped to the operator and
port 8000 remains closed publicly. Verify unauthenticated requests cannot fetch
the app, API, media or models, and authenticated HTTPS flows preserve images.

## Updating and rollback

The update entry point is `scripts/deploy-ubuntu.ps1 -Server <verified-ip>`; the
server is mandatory and has no historical IP fallback. It defaults to a plan.
Use `-InitializeHost` only for a new host; `-Execute` authorizes the selected plan.
SSH files and exact reusable release artifacts live in ignored `.cache/server/`.
Existing authentication, DNS and certificates are preserved. Instance startup and
provider checks remain separate operator actions. The release flow is:

1. Upload a new commit-named release, including prebuilt `dist/client`; verify its
   identity and frontend runtime checks. Never copy over the running release.
2. Link `.cache` to the existing shared cache, create a new Python 3.12 venv and
   install `backend/requirements.txt`; run the model setup's native import/hash
   checks. Do not rerun system apt installation for a normal application update.
   A model/dependency version change requires a separate reviewed cache migration.
3. Take a consistent SQLite backup using SQLite's backup API (a raw copy while WAL
   is active is insufficient) and preserve referenced media/model files. Keep
   `/var/lib/memoverse` outside the release tree and never replace it from Git.
4. Verify every source/static payload file against the SHA256-identified uploaded
   archive. A matching `.artifact-sha256` permits resuming dependency preparation;
   `.prepared-sha256` marks completed preparation. Unknown/conflicting directories
   stop without deletion. Reuse the exact local cached tar for same-commit retries;
   do not rebuild a different artifact under the same identity.
5. Stop the service so its generator child is reaped, atomically switch `current`,
   restart and verify loopback health plus an authenticated HTTPS smoke check.
   `KillMode=control-group` and a 30-second stop window cover owned subprocesses.
6. If acceptance fails, restore the previous link and restart. Schema-changing
   updates need an explicit compatible rollback/data-restore decision. Do not
   prune the prior release, backups or shared assets automatically.

## 1 GB acceptance and resource limits

The unit requests `MemoryHigh=700M`, `MemoryMax=850M` and `MemorySwapMax=2G` to
contain the API and its child process together, leaving room for the OS/Nginx.
These conservative limits are retained until the operator chooses the new host
budget; moving to a larger instance does not silently raise them. Configure a
reviewed `/etc/systemd/system/memoverse.service.d/resources.conf` with `[Service]`
and explicit `MemoryHigh`, `MemoryMax`, `MemorySwapMax` values, then daemon-reload
and deliberately restart. Release installation preserves such drop-ins.
These are initial safety limits, not measured sizing. Large image decode and
model activations can exceed them; a 141 MB model file is not its peak RSS.
The kernel may kill a worker under pressure and jobs must visibly fail/recover.
Verify cgroup/swap availability rather than assuming the settings took effect.

Measure the real inference worker with `/usr/bin/time -v`, then observe the full
service via `systemctl show memoverse -p MemoryCurrent -p MemoryPeak -p Result`,
the service cgroup's `memory.events`, `free -h`, and kernel OOM logs. Test small
and large JPEG/PNG, alpha images, repeated jobs, upload during generation, backup,
restart recovery and the generated GLB's embedded texture/non-flat geometry.
Check responsiveness as well as successful completion. Do not claim this machine
passes based on desktop timings; persistent swap thrashing or OOM means the
selected workload/settings need adjustment before enabling normal generation.

The service's stdout/stderr go to journald with rate limits. Installer writes
host-wide journald bounds (100 MB persistent, 30 MB runtime, seven days), which
need an operator-reviewed journald reload/restart to apply. Nginx's own logs have
daily/10 MB rotation, three old files and compression, without duplicating the
distribution's `/var/log/nginx/*.log` rules. Inspect actual disk usage during the
trial. Retaining data/backups across updates also requires monitoring free disk.

## 当前部署入口

仓库更新入口为 `scripts/deploy-ubuntu.ps1`：默认输出计划，确认参数后加 `-Execute`。脚本要求工作区已提交，使用当前 Git 提交生成源码包和同源 API 的前端构建，上传后按制品 SHA256 验证/准备发布目录并执行有超时的健康检查；启动或健康失败会尝试恢复此前代码与 service unit，再验证恢复健康。回滚失败会明确报错，不能视为已经恢复。相同 commit 已启用且健康时不重启。

数据迁移使用 `scripts/package-server-data.py --output <绝对路径.tar.gz>`，接收端在启动服务前执行 `--verify-extracted <解包目录>`。SQLite 业务行完整保留，原图与所有 ready 任务的产物逐文件校验；临时传输包不作为长期备份。

### Script-only validation

`python -B -m unittest discover -s deploy/ubuntu/tests -v` checks archive identity,
unsafe extraction, same-commit reuse/resume and command-stub activation failures.
The activation tests substitute only filesystem root/root guard and OS commands;
no real systemd, SSH or cloud calls occur. Also parse PowerShell and run `bash -n`.
A successful internal health response is not final acceptance: verify the active
commit, HTTP redirect, unauthenticated denial and authenticated HTTPS behavior.
The code deployment does not make a data backup automatically; create/verify the
consistent backup before a schema-changing deployment.

## EC2 动态公网 IP 与 Cloudflare DDNS

本项目使用自动分配的公网 IPv4。实例停止后该地址释放，下次启动通过 `memoverse-ddns.service` 更新 `keepsite.space` 的 A 记录；不要将最近观测到的 IP 当作固定地址。通过 AWS MCP 按项目运行信息中的 instance ID 启动/停止，避免误操作账户中的其他实例。

一次性配置位于本机 `.cache/server/cloudflare.json`，字段为 `zone_id`、`record_name`、`api_token`；Token 使用 Cloudflare **Edit zone DNS** 模板，资源限定 `keepsite.space`。该文件受本机权限保护并被 Git 忽略。将其安全传输到服务器 `/etc/memoverse/cloudflare.json`，目录 root:root 700、文件 root:root 600；不要将 Token 写入 shell 参数、user data、Git 或日志。

首次配置及修改 DDNS 代码时，单独同步以下运维组件；普通应用发布不会静默替换它们：

```bash
sudo install -d -m 0755 /opt/memoverse/admin
sudo install -m 0755 /opt/memoverse/current/scripts/sync-cloudflare-dns.py /opt/memoverse/admin/sync-cloudflare-dns.py
sudo install -m 0644 /opt/memoverse/current/deploy/ubuntu/memoverse-ddns.service /etc/systemd/system/memoverse-ddns.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/memoverse-ddns.service
sudo python3 /opt/memoverse/admin/sync-cloudflare-dns.py publish
sudo systemctl enable --now memoverse-ddns
```

`publish` 默认仅预览；systemd 执行带 `--execute` 的操作。更新一个已经 active 的 DDNS 服务，需要有计划地重启该服务并检查 DNS；这会短暂撤回再发布记录。程序读取 IMDSv2，只修改指定 zone/name 的单一 A 记录，读回验证后将归属记录保存到 `/var/lib/memoverse-ddns/state.json`。正常关闭时仅撤回仍匹配本实例、IP、record ID 和 revision 的记录。

只允许一个实例管理该域名。强制停机、Token 失效或并发 DNS 修改可能导致撤回跳过/失败；实例停机后应检查结果。用户验收前保持 EC2 运行，用户确认测试通过或明确要求停机后正常停止实例。2026-10-18 已验证真实 EC2 停机后的 A 记录撤回；最近一次状态及验证范围见[EC2 部署验收](../../docs/EC2部署验收.md)。

动态 IP 部署使用 `-HostKeyAlias memoverse-i-0e9ba9bc6f4b1c019`，known_hosts 中该别名已关联经 AWS 控制台输出核对的主机公钥。每次启动重新查询 IP，但保留 `StrictHostKeyChecking=yes`，不因地址变化跳过身份校验。
