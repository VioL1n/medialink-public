# Docker 部署：MediaLink + OpenList + Emby

本方案用仓库根目录的 [docker-compose.yml](../docker-compose.yml) 启动 **三个应用**。OpenList 自带 Strm 驱动，不需要另加 STRM 应用。Compose 负责构建、网络与持久化；115 授权、挂载、媒体库和 API Key 仍需首次手动配置。

容器和原有 Mac 安装使用不同的数据位置，本方案不会自动读取宿主机钥匙串、OpenList 数据库或个人媒体目录。不要把现有真实数据目录直接覆盖到新卷。

## 1. 安装条件与支持范围

先按 [Docker 官方安装说明](https://docs.docker.com/get-started/get-docker/) 安装并启动 Docker，自行完成安装界面的协议或权限操作。在终端检查：

```sh
docker version
docker compose version
```

需要能运行 Linux 容器的 Docker 引擎与支持 Compose Specification 的 Compose V2 或更新版本。锁文件有 Linux `amd64`、`arm64` 的 Python 3.12/libtorrent 轮子；本方案已在 **Linux ARM64 引擎**构建验证，AMD64 尚未实机验证。Mac 可通过 Docker 的 Linux 虚拟机使用此方案，不依赖宿主机 Python、uv 或 Node.js。

容器版本支持发现、Knaben/LimeTorrents/Nyaa/Telegram 搜索、115 入库、命名及 STRM/Emby 流程。首页由已同步 Telegram 频道、YTS、EZTV 与蜜柑计划聚合，需要用户配置自己的 TMDB 凭据。生产镜像不安装桌面、VNC 或 Chromium。

默认不映射 GPU、DLNA/Wake-on-LAN 端口，不使用 host 网络、privileged 或 Docker socket。Emby 硬件转码与外部播放器直链需要另外配置，本教程不把它们算作默认可用能力。

## 2. 获取源码并检查配置

已有源码的用户先进入项目根目录；新用户可以克隆仓库：

```sh
git clone https://github.com/VioL1n/medialink-public.git medialink
cd medialink
test -f .env || cp .env.example .env
docker compose config --quiet
```

最后一条命令没有输出且退出成功，表示配置能解析。`.env` 是 **Docker Compose** 读取的端口、时区和镜像参数文件，MediaLink 原生 Python 启动仍不会自动读取它。不要在此文件填写 Cookie、API Key 或密码。

默认仅绑定本机 `127.0.0.1`：

| 应用 | 宿主机浏览器地址 | 容器间地址 |
| --- | --- | --- |
| MediaLink | `http://127.0.0.1:8766` | 其他应用无需回调；自身健康检查使用 `127.0.0.1:8766` |
| OpenList | `http://127.0.0.1:5244` | `http://openlist:5244` |
| Emby | `http://127.0.0.1:8096` | `http://emby:8096` |

如果原生服务已占用端口，先在 `.env` 修改对应宿主机端口，例如把 `MEDIALINK_PORT` 改成 `18766`。容器间地址保持不变。**浏览器不能使用 `openlist`、`emby` 这类 Compose 服务名；容器中的 `127.0.0.1` 也不代表另一个容器。**

## 3. 一条命令构建并启动三个应用

```sh
docker compose up -d --build
docker compose ps
```

首次会从官方镜像仓库和锁文件包源下载依赖，构建前端与 Python 镜像。默认 OpenList 基于官方 `openlistteam/openlist:v4.2.6`，仅在镜像层修正新数据目录的权限；Emby 使用官方 `emby/embyserver:latest` 稳定通道。可在 `.env` 把镜像替换为维护者审核过的标签或 digest，`latest` 会随上游变化。

`ps` 应显示三个服务运行，MediaLink 稍后进入 `healthy`。`depends_on` 只保证启动顺序，不代表另两个应用已经配置完成。

健康检查：

```sh
curl --fail --silent --show-error http://127.0.0.1:8766/api/v1/health
```

如果改过端口，这里也改成相同宿主机端口。预期包含 `"status":"ok"`。仅这一项通过，还不能证明 115 入库或 Emby 播放可用。

## 4. 初始化 MediaLink 密码

在项目根目录执行交互命令：

```sh
docker compose exec medialink python -m medialink.cli init-password
```

按提示输入两次 **8–200 字符**的个人密码，输入不会显示出来。成功后打开 MediaLink 网页并登录。若网页提前打开，命令成功后刷新页面即可。

该命令不启动入库工作线程、不请求外部服务，并拒绝覆盖已经设置的密码。网页初始化入口在此部署中关闭，避免把 Docker 网桥来源当成本机放宽认证；后续修改密码使用登录后的“安全与访问”页面。没有默认密码，也不要求把密码放进环境变量、Compose 或 shell 参数。

## 5. 配置 OpenList、115 与 Strm

1. 打开宿主机的 OpenList 页面。保留默认 SQLite 数据库与 `x_` 表前缀；此共享读取方案不支持改成 MySQL/PostgreSQL。首次管理员密码按 [OpenList 官方 Docker 文档](https://doc.oplist.org/guide/installation/docker#first-run) 从本机启动日志取得，登录后修改；不要把启动日志原样贴到公开 Issue。本方案不内置管理员弱密码。
2. 按 [115 Open 驱动说明](https://doc.oplist.org/guide/drivers/115_open) 用你自己的账号完成授权。存储挂载为 `/115`，根文件夹 ID 使用网盘根目录 `0`。不要只挂载网盘某个子目录。
3. 在自己的网盘根目录创建 `infuse` 和 `MediaLinkStaging`，对应 OpenList 的 `/115/infuse` 与 `/115/MediaLinkStaging`。
4. 在 OpenList 添加 **Strm** 驱动，挂载点示例 `/115Strm`，来源路径 `/115/infuse`；开启本地保存，**OpenList 里的本地保存根目录填写 `/opt/openlist/strm`**，保留来源目录结构。其他选项参照 [Strm 官方说明](https://doc.oplist.org/guide/drivers/strm)。
5. 若 STRM 由 Emby 服务器读取并代理播放，Strm 的站点 URL 可先使用 `http://openlist:5244`，它在本 Compose 网络内可达。不要填 `http://127.0.0.1:5244`，因为 Emby 容器中的这个地址指向 Emby 自己。

只有播放器能实际访问 STRM 内的 URL 才能播放。如果你的播放方式让电视/手机直接打开该 URL，它无法解析 `openlist` 服务名；需自行配置受保护的局域网/反向代理入口、更新 OpenList 站点 URL 并重新生成 STRM。默认只绑定宿主机回环地址，不能把此模式误认为已支持局域网直链播放。

## 6. 配置 Emby 与 MediaLink

### 6.1 Emby

打开宿主机的 Emby 页面，按照 [Emby 初始化说明](https://emby.media/support/articles/Installation.html) 完成你自己的设置与协议操作。添加媒体库时选择 **Emby 容器里的 `/mnt/strm/infuse`**，可在其下按电影、剧集划分。刚安装且没有资源时，该子目录可能要等 OpenList 首次生成 STRM 才出现。

Emby 对 STRM 卷是只读的，元数据与缓存保存在 `/config`；不要要求它把 NFO、下载字幕等写入这个只读媒体目录。创建供 MediaLink 使用的独立 Emby API Key。

### 6.2 MediaLink

登录 MediaLink，“服务连接 → OpenList 与 Emby”中确认：

| 字段 | Compose 配置值 |
| --- | --- |
| OpenList 地址 | `http://openlist:5244` |
| OpenList 配置数据库 | `/openlist-data/data.db` |
| OpenList Token | 可填写自己的 Token；否则尝试只读获取共享数据库中的令牌 |
| Emby 地址 | `http://emby:8096` |
| Emby API Key | 你刚创建的密钥，没有自动复用宿主机钥匙串 |

“目录与网络”中确认：

| 字段 | Compose 配置值 |
| --- | --- |
| 媒体根目录 | `/115/infuse` |
| STRM 挂载目录 | `/115Strm/infuse` |
| 任务暂存目录 | `/115/MediaLinkStaging` |
| **本地 STRM 输出目录** | **`/strm`**，不是 OpenList 容器里的路径 |

分别保存各组设置后再检测连接。环境变量仅提供未保存字段的默认值，不会覆盖已有数据库设置；修改 `.env` 或 Compose 后，已经在页面保存的设置仍以数据库为准。

115 磁力下载使用 OpenList 已有授权，115 分享转存仍需在 MediaLink 的 115 区域由你扫码登录同一个账号。TMDB、Telegram、大模型等配置方式与 [原生教程](DEPLOYMENT.md#7-按需开启其他功能) 相同。代理地址中的 `127.0.0.1` 也是容器自己；使用代理时填写容器能够访问的真实地址，不把宿主机本地代理端口直接照抄进去。

首次用一份自己有权使用的小媒体完成验证：任务状态、115 中的目标文件、实际 `.strm` 文件、Emby 刷新、最终播放分别核对。Emby 服务信息检测通过不代表 API Key 有效；“已通知 Emby”也不代表已经刮削或播放成功。

## 7. 数据共享、权限与凭据

Compose 使用命名卷，默认卷名以项目名 `medialink_` 开头，位于 Docker 的 Linux 文件系统中：

| 命名卷 | MediaLink | OpenList | Emby |
| --- | --- | --- | --- |
| `medialink-data` | `/data`，读写 | 无 | 无 |
| `openlist-data` | `/openlist-data`，**只读** | `/opt/openlist/data`，读写 | 无 |
| `strm-data` | `/strm`，**只读核对** | `/opt/openlist/strm`，生成文件 | `/mnt/strm`，**只读** |
| `emby-data` | 无 | 无 | `/config`，读写 |

MediaLink 和 OpenList 使用 UID/GID `1001:1001`，Emby 按官方镜像的 `UID/GID/GIDLIST` 参数以相同业务身份运行。OpenList 的新版镜像使用 Compose `user`，不是已经弃用的 `PUID/PGID`；见 [官方权限说明](https://doc.oplist.org/guide/installation/docker)。本项目的 OpenList 构建阶段只修正镜像内空目录，使新卷拥有正确权限，不会递归修改已有数据。

共享的是 **整个 OpenList 数据目录**，不是单独 `data.db`。SQLite 的 `data.db-wal`、`data.db-shm` 必须随同可见且可读，所有进程处在同一 Docker 引擎的本地文件系统。MediaLink 以 `mode=ro` 打开，不写库，也不使用会忽略实时 WAL 的 `immutable=1`。不要把活跃 SQLite 放在 NFS/SMB 上，也不要只复制一个正在写入的 `.db` 来代替实时共享；参照 [SQLite WAL 说明](https://sqlite.org/wal.html#read_only_databases)。

容器凭据不是 macOS 钥匙串：

- 使用 `/data/vault/credentials.enc` 保存 Fernet 加密的凭据，主密钥是 `/data/vault/master.key`。目录权限 `0700`，文件和锁 `0600`；写入经过文件锁、临时文件和原子替换。
- **主密钥与密文在同一个持久化卷中**。这不能保护已经获得整个卷、备份或 Docker 管理权限的人；仍依赖宿主机与卷访问控制，不能称作硬件密钥保护。
- 仅剩密文而主密钥丢失、仅剩主密钥而密文丢失，或文件损坏时安全停止读取，不静默创建一套新凭据覆盖；恢复时必须一起保留数据库、密文和主密钥。
- 不自动迁移真实钥匙串，不内置 115、TMDB、Telegram 或 Emby 凭据。Mac 原生默认继续使用钥匙串。

## 8. 停止、更新和备份

按需单独运行：

| 命令 | 作用 |
| --- | --- |
| `docker compose stop` | 停止三个应用，保留容器和卷 |
| `docker compose start` | 启动已创建的容器 |
| `docker compose restart medialink` | 重启 MediaLink，可能恢复未完成任务 |
| `docker compose down` | 移除本项目容器和网络，保留命名卷 |
| `docker compose logs --tail=80 medialink` | 只在本机查看必要日志，分享前脱敏 |

**不要在有数据的安装上使用 `down -v`、`volume prune` 或删除持久化卷。** 容器部署不会自动运行 Mac 的 `scripts/backup.sh`，需要另行备份。

完整冷备份：先停止新增任务并等待当前写入到达安全状态，在项目根目录创建仓库忽略的备份目录，再停止三个应用：

```sh
mkdir -p backups
docker compose stop
docker run --rm --network none --user 0:0 \
  -v medialink_medialink-data:/snapshot/medialink:ro \
  -v medialink_openlist-data:/snapshot/openlist:ro \
  -v medialink_strm-data:/snapshot/strm:ro \
  -v medialink_emby-data:/snapshot/emby:ro \
  -v "$PWD/backups:/backup" \
  medialink:local sh -c 'umask 077; tar -czf /backup/medialink-stack-$(date +%Y%m%d-%H%M%S).tar.gz -C /snapshot .'
docker compose start
```

这会产生含账号与主密钥的私人备份，保存在本机 `backups/`，不要上传 GitHub。示例卷名对应默认项目名；如果改过 `name` 或使用 `-p`，先核对真实卷名再备份。云盘中的媒体不在此备份内。

恢复时先保留现有卷，优先恢复到另一组空卷，再核对目录拥有者为 `1001:1001`、凭据目录/文件权限及应用状态。不要把归档直接覆盖到运行中的数据库；恢复数据库不会回滚云盘的移动或下载操作。

更新前完成备份并检查 `git status --short`，保留本地修改。状态干净后逐条执行，任一步失败先停下来处理：

```sh
git pull --ff-only
docker compose build --pull
docker compose pull emby
docker compose up -d
docker compose ps
```

随后重新检查健康、登录和相关任务。更新 OpenList 版本应先更改 `.env` 的明确标签并核对兼容性，尤其是数据库和 Strm 布局；避免让不受控的上游变更直接进入生产数据卷。

## 9. 常见问题

| 现象 | 处理 |
| --- | --- |
| Docker API permission denied / cannot connect | 先确认引擎运行及当前用户有访问权限，按 Docker 官方安装方式处理；不要挂载 socket 到应用中 |
| 宿主机端口占用 | 在 `.env` 改宿主机端口，重建容器；容器间地址仍用服务名和内部端口 |
| 网页提示先初始化密码 | 执行第 4 节交互命令后刷新，不开放远程 `/auth/setup` |
| OpenList 数据库不可读 | 检查服务已初始化、整个卷共享、UID/GID、实际文件名与 sidecar 权限；不要改成 `immutable=1` 规避问题 |
| 容器互联失败 | MediaLink 里使用 `http://openlist:5244` / `http://emby:8096`，不是宿主机回环地址 |
| 等待 STRM 不结束 | 检查 Strm 来源、本地保存开关与真实相对路径；同一文件在 MediaLink 下应为 `/strm/infuse/.../*.strm` |
| Emby 看到影片但播放失败 | 检查 STRM URL 是 Emby 或实际播放器可访问的地址；区分容器 DNS、宿主机回环和局域网地址 |
| 切换凭据存储后旧任务无法解密 | 容器不自动读取 Mac 钥匙串；保留原安装，不把旧数据库孤立复制过去，使用完整迁移方案 |

局域网访问是可选后续步骤：只把需要的 `*_BIND_IP` 改为宿主机可信局域网 IP，重建对应容器，再确认访问控制；不要把默认值直接改成公网监听。本教程未包含公网/HTTPS、硬件转码或电视直链的完整部署验收。

## 10. 本次验证与仍需人工验收

初始容器适配阶段已实际构建 Linux ARM64 的 MediaLink 和 OpenList 派生镜像，前后端依赖按锁文件安装成功。隔离容器无网络、仅使用临时假数据，验证了非 root 权限、新命名卷拥有者、前端静态页面、文件凭据、CLI 初始化、登录保护，以及跨容器只读 SQLite WAL/SHM 读取。测试容器和临时测试卷已清理。

后续源码精简已通过完整后端/浏览器回归、前端构建、Compose 与锁文件检查；该轮没有重新构建镜像。真实 OpenList/Emby 服务、115 授权、媒体移动、Strm 实际生成及播放没有在现有业务数据上运行验收；AMD64、硬件转码和公网入口也未验证。不要将镜像构建或健康 API 成功写成全业务已跑通。
