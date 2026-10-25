# 自动发布 main

GitHub Actions 负责测试和同源前端构建，EC2 不安装 Node/npm。仅本仓库 main 的 push 可发布；发布任务依赖 Backend、Frontend、Android，并核对准确 SHA、main push 对应的工作流路径、GitHub Actions App/check suite 和 Deployment recovery 成功。main 已推进或检查未成功时不更新候选。PR 不取得生产写入身份，不自动合并 PR，也不为发布自动开机。

私有 S3 桶使用独立 `deploy/keepsite/` 前缀，保持 HTTPS、AES256、阻止公开访问和禁用 ACL。GitHub 通过 OIDC 获取短期角色，EC2 实例角色只读该前缀，不写静态 AWS 密钥。实际仓库采用 immutable subject：`repo:YouXiangyu@148185441/network-coding@1423372245:ref:refs/heads/main`。制品写入权限等同生产代码发布权限，不能授予媒体接口。

每个 commit 的 manifest 只创建一次，重复构建复用第一次已核对的 tar，避免同 SHA 不同制品冲突。先保存 tar 与 commit manifest，再核对 main/检查并更新 latest。读取方严格校验 schema、commit、SHA-256、大小和固定前缀，拒绝任意 URL、命令或外部路径。

服务器使用 root-owned 独立管理 venv 和脚本，开机两分钟后检查，随后每次任务结束五分钟再检查；无新版本时不下载、不安装依赖、不重启。使用独立工作锁及已有 prepare/activate 部署锁，复用原子的准备、认证健康检查和失败回滚。服务启用失败可有限重试，未知激活中断会记录 blocked 并停止自动更新，须运维核对真实 current/服务/回滚记录后恢复。

只在距本次开机 58 分钟期限至少十分钟、磁盘足够时准备及激活，不调整或重启自动关机 timer。每个候选最多两次应用部署尝试，网络、锁竞争及资源窗口跳过不计作坏版本。首版不自动删除任何发布目录、模型、共享缓存或业务数据；空间不足保留当前版本并报告 insufficient_disk。下载临时文件正常退出或收到可处理的终止信号时回收；断电/SIGKILL 后仍需检查，不能声称能保证掉电回滚。

## 安装与查看

先核对 IAM templates 与 OIDC subject，再按明确的运维授权部署管理文件：

```bash
bash deploy/ubuntu/install-autodeploy.sh
sudo bash deploy/ubuntu/install-autodeploy.sh --execute
systemctl list-timers keepsite-autodeploy.timer keepsite-auto-stop.timer
sudo journalctl -u keepsite-autodeploy.service -n 30 --no-pager
sudo cat /var/lib/keepsite-autodeploy/state.json
```

安装不会切换当前业务代码或覆盖 `/etc/keepsite.env`；实际切换仅发生在存在已核验候选时。管理代码不会从应用可写的 venv/current 导入。新增管理组件的更新需单独运维，不由普通应用制品覆盖。

权限基线使用官方工具从 SDK 源码生成，再绑定准确前缀、去除本实现未使用的 KMS、ACL、标签、保留策略等可选权限：

```powershell
uvx iam-policy-autopilot@latest generate-policies D:/100-code/110-Project/network-coding/deploy/ubuntu/pull-release.py --region ap-northeast-1 --account 125647512944 --service-hints s3 --pretty
uvx iam-policy-autopilot@latest generate-policies D:/100-code/110-Project/network-coding/scripts/publish-release.py --region ap-northeast-1 --account 125647512944 --service-hints s3 --pretty
```

只读方使用 GetObject 与受前缀限制的 ListBucket，发布方另有 PutObject；没有删除、ACL 或整桶写权限。ListBucket 用于判断尚无候选，避免把 S3 缺对象的 403 错当成正常首次启动。

建议按部署前缀接入访问日志、CloudTrail 数据事件及 CloudWatch 指标/失败告警，并核算相应费用；本次不额外启用计费日志服务。日常故障先看 journal、安全状态文件、磁盘和实际 current，保留业务数据、凭据与回滚备份。
