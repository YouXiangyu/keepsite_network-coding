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
bash /opt/memoverse/releases/<commit>/deploy/lightsail/install.sh \
  --release /opt/memoverse/releases/<commit> --domain keepsite.space
# After reviewing that plan:
sudo bash /opt/memoverse/releases/<commit>/deploy/lightsail/install.sh \
  --release /opt/memoverse/releases/<commit> --domain keepsite.space --execute
```

The installer checks Ubuntu 24.04 and the uploaded release before modifying it.
It installs Nginx, Python venv/pip, unzip/git/curl, CA certificates, apache2-utils,
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

The root-controlled update script should implement this sequence:

1. Upload a new commit-named release, including prebuilt `dist/client`; verify its
   identity and frontend runtime checks. Never copy over the running release.
2. Link `.cache` to the existing shared cache, create a new Python 3.12 venv and
   install `backend/requirements.txt`; run the model setup's native import/hash
   checks. Do not rerun system apt installation for a normal application update.
   A model/dependency version change requires a separate reviewed cache migration.
3. Take a consistent SQLite backup using SQLite's backup API (a raw copy while WAL
   is active is insufficient) and preserve referenced media/model files. Keep
   `/var/lib/memoverse` outside the release tree and never replace it from Git.
4. Stop the service so its generator child is reaped, atomically switch `current`,
   restart and verify loopback health plus an authenticated HTTPS smoke check.
   `KillMode=control-group` and a 30-second stop window cover owned subprocesses.
5. If acceptance fails, restore the previous link and restart. Schema-changing
   updates need an explicit compatible rollback/data-restore decision. Do not
   prune the prior release, backups or shared assets automatically.

## 1 GB acceptance and resource limits

The unit requests `MemoryHigh=700M`, `MemoryMax=850M` and `MemorySwapMax=2G` to
contain the API and its child process together, leaving room for the OS/Nginx.
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
