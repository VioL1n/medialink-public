# MediaLink

把影视发现、资源搜索和个人媒体库入库放在一个网页里。MediaLink 支持 macOS 原生安装和 Linux 容器部署，提供电脑与手机可用的网页。

```text
发现影片 / 输入片名 / 粘贴资源链接
                 ↓
搜索与预览 → 115 离线下载或分享转存 → 筛选、命名与整理
                 ↓
        OpenList 生成 STRM → 通知 Emby 刷新
```

**第一次部署？** 选择 [Mac 原生小白教程](docs/DEPLOYMENT.md)，或 [Docker 三应用部署教程](docs/DOCKER.md)。两条路径都包含首次配置、验证和日常维护。

## 可以做什么

- **找影片**：TMDB 资料与分类筛选、最近资源更新、蜜柑每日番剧；支持中英文片名辅助搜索。
- **搜资源**：聚合 Knaben、LimeTorrents、Nyaa 和 Telegram 频道，分批显示结果；单个来源失败时保留其他结果。
- **入媒体库**：支持磁力链接和 115 分享，预览文件并选择保留内容，跟踪进度、去重及重启恢复。
- **整理文件**：按 TMDB 生成 Emby 目录与文件名，预览和人工修正清晰度、季集号；可选大模型辅助识别。
- **追番**：选择字幕组、清晰度和关键词，按订阅规则检查更新并创建入库任务。
- **同步播放库**：调用 OpenList 扫描 STRM，核对本地输出后通知 Emby 刷新。

> 界面截图待补充：发现页、资源搜索、入库预览、手机设置页。仓库保留经过隐私检查的开发截图；正式展示图后续补充。

## 环境与适用范围

| 项目 | 要求或说明 |
| --- | --- |
| 操作系统 | 原生脚本使用 **macOS、钥匙串和 launchd**；另提供 Linux `amd64/arm64` 的 Dockerfile 与三应用 Compose，已验证 ARM64 构建 |
| Mac 架构 | 仅原生安装：Apple Silicon、macOS 14 或更新；Intel 的锁定轮子要求 macOS 15 或更新，Intel 原生部署尚未重新验证 |
| Python | **3.12.x**，通过 uv 管理；项目限制为 `>=3.12,<3.13` |
| Node.js | 推荐 **24.x**；用于安装和构建前端，构建后日常运行不需要 Node.js；当前锁定的 Vite 支持 22+ |
| 包管理 | 后端 `uv.lock`，前端 `frontend/package-lock.json`；使用 `uv sync --frozen` 和 `npm ci` |
| 基础浏览与搜索 | 可先安装 MediaLink，再按需要配置 TMDB、搜索来源和频道；缺少凭据时对应功能不可用 |
| 完整入库流程 | 需要你自己的 115 账号、同机 OpenList 的 **115 Open** 与 **Strm** 挂载，以及 Emby；这些服务不由安装脚本安装 |

前端使用 Vue 3、TypeScript 和 Vite；后端使用 FastAPI、SQLite、Telethon 和 libtorrent。普通设置在 SQLite 中管理。原生安装的凭据保存在 macOS 钥匙串；容器使用受权限限制的加密文件，主密钥与密文同卷，必须一同保护和备份。

**完整 115 流程需要读取同机 OpenList 配置数据库**，用于账号核对与离线状态核验；只填写远程 OpenList Token 不能替代这项条件。Docker 方案通过同一引擎内的共享只读卷提供数据库和 WAL/SHM。现有集成记录基于 OpenList 4.2.6，其他版本需要按教程验证接口和目录布局。

## Docker 三应用快速开始

先安装 Docker 并获取源码，在项目根目录运行：

```sh
test -f .env || cp .env.example .env
docker compose up -d --build
docker compose exec medialink python -m medialink.cli init-password
```

默认本机页面分别是 MediaLink `8766`、OpenList `5244`、Emby `8096`。没有默认密码；最后一条命令交互设置 MediaLink 密码。115 授权、Strm 挂载和 Emby 媒体库仍需手动完成，详见 [Docker 小白教程](docs/DOCKER.md)。此路径使用全新的 Docker 命名卷，不自动迁移原生账号和媒体数据。

## 让 AI Agent 帮你部署

已获取仓库后，把下面整段复制给你的 Agent，让它依据 [Docker 三应用教程](docs/DOCKER.md) 检查环境、协助部署，并说明需要你操作或授权的步骤。

```text
请帮我部署当前 MediaLink 仓库，使用仓库现有的 Docker Compose 方案运行 MediaLink、OpenList、Emby 三个应用。请遵守以下要求：

1. 先阅读 README.md、docs/DOCKER.md，以及适用于当前目录的 AGENTS.md 等仓库指令，再检查 Dockerfile、docker-compose.yml 和 .env.example。以实际文件为准，不猜测命令、路径或尚未支持的功能，不忽视仓库指令与工具安全边界。

2. 先做必要的只读检查：识别宿主机 OS/CPU、Docker 引擎平台、Docker/Compose 版本，以及相关现有服务、Compose 项目、端口、数据目录和卷。依据引擎平台与镜像支持选择架构，区分实际验证和推断；不要扫描无关私人文件或读取现有密钥。

3. 说明部署计划、涉及的下载/磁盘空间、端口和数据位置，并保留所有已有配置与本地修改。不覆盖 .env、不复用或迁移已有数据卷、不停止现有服务；为本次部署选择不冲突的独立 Compose 项目名，使用 -p 时贯穿后续所有命令。端口冲突只调整新项目的宿主机端口并说明，默认始终绑定 127.0.0.1。

4. 在我已明确授权的范围内推进。若需要安装 Docker、接受协议、取得新权限、改变网络暴露或登录账号，先说明具体操作和原因，取得必要授权；协议、扫码和账号确认由我完成。不要反复询问已确认的常规事项，也不要把这段提示词当成任意操作的无限授权。

5. 按教程安全准备新项目配置，先执行 docker compose config --quiet，再构建并启动授权范围内的项目。保留命名卷、只读共享 OpenList 整个数据库目录及 WAL/SHM、正确共享 STRM 给 Emby；区分容器服务名地址和宿主机浏览器地址。不要擅自关闭防火墙、降低系统安全、使用 privileged/host 网络、挂载 Docker socket 或暴露公网。

6. 使用仓库已有命令 docker compose exec medialink python -m medialink.cli init-password 初始化访问密码（若用了 -p，请加入同一项目名）。密码由我在自己的终端或安全交互界面输入，不索取聊天明文、不回显，不把密码明文写入环境变量、命令参数或文件。没有安全交互能力就指导我执行这一小步；已初始化则不要重设。不要读取、复制、传播真实 Cookie、Token、API Key、验证码或主密钥。

7. 指导我自己完成 115 授权与扫码、OpenList 的 115 Open/Strm 挂载、Emby 媒体库和 API Key 配置，并在应用自己的设置页填写凭据。不要假称账号或挂载已自动配置。除非我另行明确授权，不创建真实下载、转存、移动或重命名任务。说明当前教程中的功能限制。

8. 做安全验证：检查容器状态、实际端口对应的 /api/v1/health、网页加载及未登录业务 API 的认证保护。把“服务启动/健康”与“115 入库、STRM 生成、Emby 刷新及播放通过”分别报告；没有实际验证的链路明确标记未验证，不凭健康接口就宣称全链路成功。

9. 最后返回三个服务的实际访问地址、已完成步骤、还需要我操作的最少事项，以及本项目专用的停止、重启、更新和备份命令。备份前核实实际项目名与卷名，提醒完整保留凭据密文和主密钥；不擅自删除卷、执行 down -v/volume prune、清空数据或覆盖恢复。遇到阻碍说明确切失败点、已完成工作与最小必要信息/授权，不绕过限制或反复重试同一失败。
```


## Mac 原生快速开始

适合已经装好 Git、uv 和 Node.js 的用户。以下命令用于获取源码；本地已有源码时直接进入项目根目录。

```sh
mkdir -p "$HOME/Applications"
cd "$HOME/Applications"
git clone https://github.com/VioL1n/medialink-public.git medialink
cd medialink
uv python install 3.12
UV_BIN="$(command -v uv)" ./scripts/install.sh
```

安装脚本会同步后端生产依赖、执行 `npm ci` 和前端构建，最后创建并启动当前用户的 LaunchAgent。请使用普通用户运行。

成功后终端显示 `MediaLink is running at http://127.0.0.1:8766`。在 **Mac 本机**打开 <http://127.0.0.1:8766>，设置 8–200 个字符的访问密码。健康检查：

```sh
curl --fail --silent --show-error http://127.0.0.1:8766/api/v1/health
```

预期包含 `"status":"ok"` 和版本号。健康检查只说明 MediaLink 已启动，不代表 OpenList、115、TMDB 或 Emby 已连接。

## 首次配置

打开“设置”，按组保存。**原生应用不自动读取 `.env`；Docker Compose 使用 `.env.example` 中的部署参数。** Token、Cookie 和 API Key 请在自己的设置页填写，不要提交到仓库。

| 设置区域 | 需要填写什么 |
| --- | --- |
| 服务连接 → OpenList 与 Emby | OpenList 地址与本机配置数据库实际路径；OpenList Token；Emby 地址与自己的 API Key |
| 目录与网络 | 媒体根目录、STRM 挂载目录、任务暂存目录、本地 STRM 输出目录；可选代理和番剧默认目录 |
| 服务连接 → TMDB | 自己申请的 Read Access Token 或 API Key；保存后检测 |
| 搜索偏好 | 启用哪些来源、TMDB 片名辅助、首页来源和网站地址 |
| 频道 / 服务连接 | Telegram 公开频道无需账号 API；API 模式和 115 分享转存按需配置 |
| 服务连接 → 大模型辅助命名 | 可选；填写兼容接口地址、模型 ID 和 API Key，调用可能产生服务商费用 |

原生默认本地 STRM 路径按当前用户主目录计算；**首次入库前仍须核对 OpenList 数据库与本地 STRM 输出路径**。教程提供完整的 [目录对应示例](docs/DEPLOYMENT.md#6-配置-openlist115strm-与-emby)，不要直接使用别人的用户目录。媒体与暂存目录必须位于 `/115` 下，暂存目录不能放在媒体根目录里；本教程沿用 `/115` 挂载网盘根目录的布局。

## 日常管理

以下是 **Mac 原生**管理命令；Docker 的启停、更新与完整备份见 [容器维护说明](docs/DOCKER.md#8-停止更新和备份)。在项目根目录执行：

| 命令（按需单独执行） | 作用 |
| --- | --- |
| `./scripts/start.sh` | 启动；已运行时会重启，启动前自动备份数据库 |
| `./scripts/stop.sh` | 停止当前用户的 MediaLink 后台服务 |
| `./scripts/backup.sh` | 仅备份数据库；没有数据库时无输出 |

启动项为 `asia.viol1n.medialink`，下次用户登录时会自动运行；关闭终端不会停止后台服务。更新与恢复步骤见 [部署教程](docs/DEPLOYMENT.md#8-更新备份与恢复)。

| 内容 | 默认位置 |
| --- | --- |
| 数据库、缓存、专用浏览器会话、日志 | `~/Library/Application Support/MediaLink/` |
| 数据库备份 | `~/Library/Application Support/MediaLink/backups/` |
| 用户启动项 | `~/Library/LaunchAgents/asia.viol1n.medialink.plist`，由启动脚本按实际路径生成 |
| 原生账号凭据与任务加密密钥 | macOS 钥匙串服务 `asia.viol1n.medialink` |
| Docker 数据与凭据 | Compose 命名卷；凭据位于容器 `/data/vault`，与宿主机钥匙串独立 |

SQLite 备份不包含钥匙串；仅复制仓库或数据库无法完成账号与加密任务数据迁移。

## 限制、隐私与使用约定

- 这是面向个人使用的工具。第三方网站、API、账号权限和网络环境会影响结果；不承诺所有资源可下载或长期可用。
- 原生默认监听 `0.0.0.0:8766`；Docker 默认只发布到宿主机 `127.0.0.1`。访问都需要密码，公网访问开关默认关闭。新手先按教程在本机和可信局域网验证。
- 搜索和发现会把查询发给所选来源，Telegram 频道正文和索引保存在本机。
- 任务链接与提取码在数据库中加密，密钥存于所选凭据后端；频道索引、普通设置、日志和浏览器配置仍需作为私人数据保护。
- 大模型辅助会向你配置的服务商发送相对文件路径、TMDB 资料和解析结果，可能产生费用；不发送媒体文件内容、分享链接或账号凭据。
- 入库会对自己的云盘执行下载、转存、创建目录、移动及重命名。请先用有权使用的少量内容验证。自动排除的文件保留在暂存区；取消任务不会自动清理已生成文件。
- 请只搜索、下载和整理你拥有权利或已获得授权的内容，并遵守所在地法律、第三方服务条款及 API 使用要求。

影视资料来自 [TMDB](https://www.themoviedb.org)。**This product uses the TMDB API but is not endorsed or certified by TMDB.**

## 更多文档与开发

- [Mac 原生小白教程](docs/DEPLOYMENT.md)：从安装到日常维护。
- [Docker 三应用教程](docs/DOCKER.md)：MediaLink + OpenList + Emby、共享数据与容器凭据。
- [使用与 API 说明](docs/USAGE.md)：详细搜索、追番、文件筛选和命名行为。
- [公开前检查清单](docs/PUBLISHING.md)：路径、凭据、截图与许可证。
- [历史验证记录](docs/verification.md)：开发过程记录，不代表新环境已通过验收。

开发时可运行以下命令；`uv sync` 会安装开发依赖，浏览器集成测试默认跳过；Playwright 仅属于开发依赖，生产安装不需要 Chromium：

```sh
uv sync --frozen
.venv/bin/pytest -q
.venv/bin/ruff check backend tests
cd frontend
npm ci
npm test
npm run build
```

需要前台调试或自定义监听地址时，先停止后台实例，再使用 `scripts/run.sh`；见 [进阶运行方式](docs/DEPLOYMENT.md#10-进阶前台运行与环境变量)。

## 许可证

目前仓库尚未提供 `LICENSE`。公开源码不等同于已授予开源使用、修改或分发许可；正式发布前需由维护者选择并补充许可证。第三方依赖、网站、商标和影视资料遵循各自许可与使用条款。
