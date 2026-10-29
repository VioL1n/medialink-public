# MediaLink 中文小白部署教程

本教程使用项目已有的 macOS 安装脚本，从一台 Mac 开始，先让网页运行，再连接自己的媒体库。Docker 用户请改用 [三应用 Docker 教程](DOCKER.md)；本页原生脚本不安装 OpenList、Emby。先读 [README](../README.md) 了解功能和适用范围。

## 1. 开始前准备

你需要一台保持开机、已经登录桌面的 Mac，以及可下载安装包的网络。首次安装需要下载 Python 依赖和前端依赖，耗时取决于网络。

在“应用程序 → 实用工具”打开“终端”。后面的代码框是终端命令，逐块粘贴并按回车；看到上一条命令结束、重新出现输入提示后，再执行下一块。网页地址则填进浏览器地址栏。

先查看系统与架构：

```sh
sw_vers -productVersion
uname -m
```

- `arm64` 是 Apple Silicon：当前 `uv.lock` 中的 libtorrent 轮子最低支持 macOS 14。
- `x86_64` 是 Intel：当前锁文件中的对应轮子最低支持 macOS 15，尚未在本次文档整理中验证 Intel 新机安装。
- 如果不符合上述条件，先停在这里。不要把脚本当成 Windows、Linux、NAS 或 Docker 的部署脚本。

建议把项目放在 `~/Applications/medialink`。`~` 指当前用户的个人目录。安装后不要随意移动项目，否则启动项仍会指向旧位置；移动后需在新位置重新运行 `./scripts/start.sh`。

## 2. 安装基础软件

### 2.1 Git

在终端运行：

```sh
git --version
```

如果已经显示 `git version ...`，继续下一节。如果系统提示安装开发者工具，按系统提示完成；也可以手动发起安装：

```sh
xcode-select --install
```

安装结束后重新运行 `git --version`。若提示工具已安装，无需重复安装。

### 2.2 Node.js 与 npm

打开 [Node.js 官方下载页](https://nodejs.org/en/download)，选择 **24.x** 和适合 Mac 的安装包，按安装向导完成。它自带 npm。项目当前锁定的 Vite 也支持 Node.js 22+；本教程推荐与已检查环境一致的 24.x。

关闭并重新打开终端，检查：

```sh
node --version
npm --version
```

成功时分别显示 `v24...` 和 npm 版本号。Node.js 只用于构建前端，网页上线后由 Python 后端提供。

### 2.3 uv 与 Python 3.12

按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装。下面命令会下载并执行官方安装脚本；也可从该说明页选择其他安装方式。

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

采用官方默认安装位置时，让当前终端找到 uv：

```sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv python install 3.12
```

成功时显示 uv 版本和 Python 3.12 的安装结果。如果 Python 已存在，会提示已安装。无需替换 macOS 自带的 Python，也无需手动激活虚拟环境。

## 3. 获取 MediaLink 源码

```sh
mkdir -p "$HOME/Applications"
cd "$HOME/Applications"
git clone https://github.com/VioL1n/medialink-public.git medialink
cd medialink
ls
```

成功后能看到 `README.md`、`backend`、`frontend`、`scripts`、`pyproject.toml` 和 `uv.lock`。仓库发布前可能无法克隆，也可由维护者提供源码压缩包。

如果使用 GitHub 的“Code → Download ZIP”，解压后用终端进入实际解压目录，再执行下一节。不要进入 `frontend` 或 `backend` 子目录运行安装脚本。ZIP 下载的目录不支持后面的 `git pull` 更新方式。

若提示 `destination path 'medialink' already exists`，说明该位置已有目录。先确认是不是原有安装；是的话直接 `cd "$HOME/Applications/medialink"`，不要删除它重新开始。

## 4. 安装并启动 MediaLink

确认当前终端位于包含 `scripts` 的项目根目录，运行：

```sh
UV_BIN="$(command -v uv)" ./scripts/install.sh
```

这一步会依次：

1. 用 `uv.lock` 安装 Python 3.12 的生产依赖，建立项目内 `.venv`。
2. 用 `frontend/package-lock.json` 执行 `npm ci`，再构建到 `frontend/dist`。
3. 在当前用户目录生成 LaunchAgent 并启动，轮询本机健康接口。

预期最后一行是：

```text
MediaLink is running at http://127.0.0.1:8766
```

请使用普通用户运行，不要加 `sudo`。安装中断时先处理提示的问题，再重跑同一命令；脚本使用锁文件，不需要手动修改版本或删除锁文件。

启动脚本会写入 `~/Library/LaunchAgents/asia.viol1n.medialink.plist`，以当前用户身份运行，并在下次登录桌面时自动启动。关闭终端不会结束后台服务；Mac 休眠、关机或退出该用户后，不能继续提供正常后台处理。

`deploy/` 中的 plist 是现有环境样例，含固定路径。**不要手动复制它**，让 `scripts/start.sh` 按你的项目位置生成。

## 5. 打开页面与首次验证

在运行项目的 Mac 上打开 <http://127.0.0.1:8766>。第一次会要求创建访问密码，长度为 **8–200 个字符**。这是 MediaLink 自己的密码，不是 Apple ID、115 或 Emby 密码。后续设备使用同一密码登录。

再在终端执行：

```sh
curl --fail --silent --show-error http://127.0.0.1:8766/api/v1/health
```

预期响应：

```json
{"status":"ok","version":"0.1.0"}
```

版本号以后可能变化，关键是 `status` 为 `ok`。这个接口不需要登录，只证明 MediaLink 服务能响应。

还可以检查认证保护：

```sh
curl -i http://127.0.0.1:8766/api/v1/settings
```

终端请求没有浏览器的登录 Cookie，应返回 **HTTP 401** 和“请先登录”，即使浏览器已经登录也是正常现象。项目关闭了自动 API 文档页面，不需要寻找 `/docs`。

刚安装时 TMDB、115 和其他服务尚未连接，首页可能空白或提示未配置。先进入设置，不要用“有海报”作为安装成功的唯一标准。只想试用搜索时，可以先在“搜索偏好”关闭暂不使用的来源，之后再逐个启用。

## 6. 配置 OpenList、115、STRM 与 Emby

这一节用于完整入库。只体验网页和独立搜索时可以稍后完成。安装脚本不会安装、授权或修改这些外部服务。

### 6.1 先准备 OpenList 和 Emby

1. 按 [OpenList 官方安装教程](https://doc.oplist.org/guide/installation/manual) 安装适合 Mac 架构的版本并运行。用浏览器确认 OpenList 首页可访问，默认通常是 `http://127.0.0.1:5244`。记下你实际使用的地址和数据目录。
2. 按 [115 Open 驱动说明](https://doc.oplist.org/guide/drivers/115_open) 在 OpenList 后台添加 **115 Open** 存储，由你完成自己账号的授权。挂载路径使用 `/115`，根文件夹选择网盘根目录（根 ID 为 `0`）。本教程保持此布局；不要用另一个账号或仅挂载子目录。
3. 在自己的 115 网盘根目录创建 `infuse` 和 `MediaLinkStaging` 两个文件夹。它们在 OpenList 中分别显示为 `/115/infuse` 和 `/115/MediaLinkStaging`。媒体文件最终进入前者，下载与排除文件暂存于后者。
4. 按 [Emby 官方安装说明](https://emby.media/support/articles/Installation.html) 安装 **Emby Server**，完成网页初始化。默认地址通常是 `http://127.0.0.1:8096`；之后在管理后台创建供 MediaLink 使用的 API Key。

OpenList 和 MediaLink 必须能够访问同一份本机 OpenList SQLite 数据库。只使用远程 NAS 的 OpenList HTTP API，无法满足当前实现的 115 账号核对。Emby 可以在其他设备，但该设备还必须能读取 STRM 文件并访问其中的播放地址。

### 6.2 准备 STRM 挂载与本地输出

STRM 是保存媒体播放地址的小文件，影视正片仍在网盘中。下面是与项目既有默认目录匹配的一组示例，不能只创建空的本地文件夹就认为 STRM 已配置好。

在终端创建本地输出根目录：

```sh
mkdir -p "$HOME/MediaLink/strm"
printf '%s\n' "$HOME/MediaLink/strm"
```

记下输出的完整路径，例如 `/Users/你的用户名/MediaLink/strm`。网页路径框中填写完整路径，**不要填写字面量 `~` 或 `$HOME`**。

按 [OpenList Strm 驱动说明](https://doc.oplist.org/guide/drivers/strm) 添加 Strm 存储：挂载点示例为 `/115Strm`，来源路径为 `/115/infuse`；启用保存 STRM 到本地，把本地保存根目录设为刚才的完整路径。文件过滤至少包含你的影视扩展名。

生成的 STRM 必须保留源目录结构。本项目按下面的关系查找输出：

```text
OpenList 媒体：/115/infuse/电影/演示影片/影片.mkv
本地 STRM：  /Users/你的用户名/MediaLink/strm/infuse/电影/演示影片/影片.strm
```

这里的影片名称只是目录示例，不提供媒体资源。`strm_local` 填 **`strm` 这一层**，不要再加一次 `infuse`。如果你的 OpenList 实际输出布局不同，需要先调整到与这条规则一致：`本地输出根目录 + 媒体目标相对于 /115 的路径`。

部分 OpenList 版本会把单来源 Strm 的虚拟路径展开：配置 `/115Strm/infuse`，实际浏览根为 `/115Strm`。应用会先探测，再读取本机 OpenList 挂载配置兼容该情况；本地文件仍需保留 `infuse` 这一层。

在 Emby 中添加媒体库，指向生成 STRM 的本地目录（可按电影、剧集再细分）。STRM 中的 OpenList 地址必须能被实际播放设备访问：在电视或另一台电脑上，`127.0.0.1` 代表那台设备自己，应使用 Mac 的局域网地址。该映射需要在 OpenList 侧配置，MediaLink 不会自动修复播放地址。

### 6.3 在 MediaLink 逐项填写

打开“设置 → 服务连接 → OpenList 与 Emby”：

| 字段 | 怎么填 |
| --- | --- |
| OpenList 地址 | 同机默认 `http://127.0.0.1:5244`；若改过端口，填写实际地址 |
| OpenList 配置数据库 | OpenList 实际数据目录中的 `data.db` **完整路径**；去自己的 OpenList 启动配置中确认，不要猜测或复制数据库到 Git 仓库 |
| OpenList Token | 从自己的 OpenList 管理后台取得并粘贴；留空会保留已有值，或尝试只读获取本机数据库令牌 |
| Emby 地址 | 同机默认 `http://127.0.0.1:8096`，其他设备填其可访问地址 |
| Emby API Key | 在自己的 Emby 管理后台创建后粘贴；新安装建议明确填写，默认的钥匙串复用项是作者环境的兼容配置 |

点击“保存连接配置”。配置数据库不只是为了读取 Token：115 账号验证、原生离线任务核对和部分 STRM 路径兼容也会使用它。保持 OpenList 数据库原有位置，并确保当前 macOS 用户能够读取。

切到“目录与网络”：

| 字段 | 本教程示例 | 含义 |
| --- | --- | --- |
| 媒体根目录 | `/115/infuse` | OpenList 中最终保存影片的位置 |
| STRM 挂载目录 | `/115Strm/infuse` | OpenList Strm 虚拟路径，单来源展开见上一节 |
| 任务暂存目录 | `/115/MediaLinkStaging` | 在网盘里暂存任务，不能位于媒体根目录内部 |
| 本地 STRM 输出目录 | `/Users/你的用户名/MediaLink/strm` | Mac 上的实际目录，替换为上一步得到的完整路径 |
| 番剧默认入库目录 | 可留空，或 `/115/infuse/动漫` | 必须位于媒体根目录内，并先确认目录存在 |
| 代理地址 | 无需代理则留空 | 可选 `socks5://127.0.0.1:7890` 或 HTTP 代理；示例端口必须替换成自己的；不支持带账号密码的代理地址 |

点击“保存目录与网络”。各组分别保存，点击连接检测不会替你保存其他组的草稿。存在活动任务时，目录映射会被锁定；先处理任务再调整。

默认本地 STRM 路径按当前用户主目录计算；仍需以实际 OpenList 配置核对并修改。`mount_root` 默认 `/115`，`last_target` 默认 `/115/infuse`；新手保留这组根目录，在入库位置选择器里选择媒体根下的分类目录即可。

### 6.4 115 分享转存登录与连接检查

磁力下载使用 OpenList 已有的 115 Open 授权。**115 分享转存需要额外登录同一个账号**：在“服务连接 → 115 网盘”点击“扫码登录 115”，由你在手机 115 App 扫码并确认。程序会只读核对是否与 OpenList 的账号一致。

保存设置后使用页面上的连接检测。OpenList 应显示媒体与 STRM 目录可访问；115 分享功能应显示账号一致。Emby 的连接检测只检查服务信息可访问，不能证明 API Key 已有刷新权限。

最终用一份你有权使用的小型媒体做完整入库验证，核对任务完成、本地 `.strm` 存在、Emby 收到刷新且能够实际播放。提交会操作你的网盘，确认所选账号、目录和文件后再执行。“已通知 Emby”表示刷新请求被接受，不代表刮削或播放已经完成。

## 7. 按需开启其他功能

| 功能 | 配置步骤与成功标志 |
| --- | --- |
| TMDB 影片资料 | 在 [TMDB API 设置](https://www.themoviedb.org/settings/api) 获取自己的 Read Access Token（推荐）或 API Key；在“服务连接 → TMDB 影视资料”选类型、填写、保存并检测。成功显示已配置且检测通过；网络异常先检查代理。未配置时仍可原词搜资源 |
| Telegram 公开频道 | 在频道页添加有网页预览的 `@频道名` 或 `t.me/s/频道名`，选择公开网页模式。无需个人 API 和登录；成功后能看到同步状态和索引数量。每 5 分钟检查更新，仅覆盖可见历史，不保证频道全部历史 |
| Telegram API 模式 | 按需在 [my.telegram.org](https://my.telegram.org) 获取自己的 API ID/Hash，保存后按页面提示登录；只同步账号能访问的频道。没有此需求可一直不配置 |
| 大模型辅助命名 | 可选，默认关闭。填写自己服务商的兼容 Base URL、模型 ID 和 API Key，保存并检测；Base URL 不包含 `/chat/completions`。例如 `https://api.example.com/v1`、`your-model-id`、`YOUR_API_KEY` 都是假值，必须替换。检测和识别可能收费，并会发送相对文件路径与影片资料 |

不要把 Cookie、验证码、API Hash、Token 或真实账号密码发到 GitHub Issue。未登录的来源可暂时在“搜索偏好”关闭；连接失败不需要重新安装整个项目。

### 7.1 手机访问

先在 Mac 完成首次密码设置。在“系统设置 → 网络”查看 Mac 当前局域网 IP，手机连接同一个可信 Wi-Fi，然后打开例如 `http://192.168.1.20:8766`；这是示例 IP，替换成你的 Mac 地址，用 MediaLink 密码登录。

手机上的 `127.0.0.1` 指手机自己，不能用来连接 Mac。默认服务监听所有网卡，但路由器客户端隔离、Mac 防火墙规则、VPN 和休眠都可能影响连接；先保证本机访问正常，再检查网络，不需要为局域网访问开启“允许公网访问”。

公网访问是进阶功能：需要自行配置 HTTPS 反向代理或其他受保护的入口，再在“安全与访问”配置准确的允许域名/IP（不含协议、端口和路径）。代理要保留带端口的 Host 并支持长连接/SSE。应用开关不会自动配置路由器、证书或防火墙；本教程不提供公网部署的完整验收方案。

## 8. 更新、备份与恢复

### 8.1 启动、停止与重启

在项目根目录按需运行其中一个命令：

```sh
./scripts/start.sh
```

未运行时启动，已运行时重启；启动前自动备份数据库。健康检查通过后显示本机网页地址。

```sh
./scripts/stop.sh
```

停止后台服务，数据保留。如果服务本来就没有加载，`launchctl` 可能提示找不到服务；不要据此删除数据或重新安装。

```sh
./scripts/backup.sh
```

单独执行数据库备份。已有数据库时输出备份路径，文件保存在 `~/Library/Application Support/MediaLink/backups/`，使用 SQLite 备份接口，支持服务运行期间备份。尚未生成数据库时没有输出。

### 8.2 更新源码

先停止新增任务，等待正在进行的任务到达可停止的状态。进入原来的 Git 克隆目录，先检查本地修改：

```sh
git status --short
```

若有输出，先保留并处理自己的修改；不要直接执行更新。确认状态干净后，再依次运行：

```sh
./scripts/backup.sh
./scripts/stop.sh
git pull --ff-only
UV_BIN="$(command -v uv)" ./scripts/install.sh
```

每条命令成功后再执行下一条；`git pull --ff-only` 失败时先停下来处理，不要运行强制覆盖命令。安装脚本重新同步锁定依赖、构建前端并启动；最后再跑健康检查并登录确认。

不要在运行中的服务上单独执行 `npm run build` 作为完整更新：服务直接提供 `frontend/dist`，前后端版本可能不一致。ZIP 安装应先备份，在新解压目录安装，确认成功前保留旧目录；不要把别人的 `.venv`、`node_modules` 或数据目录覆盖进来。

### 8.3 备份包含什么

| 内容 | 是否在 `backup.sh` 中 | 额外处理 |
| --- | --- | --- |
| SQLite 设置、频道索引和任务记录 | 是 | 备份中仍有私人索引与设置，请妥善保管 |
| 钥匙串凭据和任务加密密钥 | 否 | 迁移时保留/迁移原 macOS 钥匙串，不能只复制 `.db` |
| TMDB/蜜柑图片缓存与日志 | 否 | 位于应用数据目录，可与完整备份一起保存 |
| 115 云端媒体、OpenList 数据库、STRM 与 Emby 配置 | 否 | 分别按照对应服务的方法备份 |

完整应用数据目录是 `~/Library/Application Support/MediaLink/`，不要把它上传到公开仓库。备份脚本没有自动清理旧备份，请定期检查空间。

### 8.4 确需恢复数据库时

日常更新不需要做以下操作。只在你明确要回到某个备份时：

1. 运行 `./scripts/stop.sh` 并确认后台实例已停止，也没有前台实例。
2. 在 Finder 用“前往 → 前往文件夹”打开 `~/Library/Application Support/MediaLink/`，先把整个目录复制到仓库外的另一处安全位置，保留原钥匙串。
3. 把当前 `medialink.db` 及若存在的 `medialink.db-wal`、`medialink.db-shm` 一起移到另一个保留目录，不要将旧 WAL 与恢复后的数据库混用。
4. 从 `backups/` 复制所选日期的备份回应用数据目录，将副本命名为 `medialink.db`。保留原备份，检查文件只供当前用户读写（0600）。
5. 运行 `./scripts/start.sh`，检查健康、登录与任务。备份之后的本地记录不会出现在恢复后的数据库里；恢复不会把 115 云端文件一起回滚，不确定的任务先核对远端。

换 Mac 或钥匙串丢失时，重新登录外部服务并不一定能解密旧任务链接。先保留原数据寻求协助，不要盲目清空数据库。

## 9. 常见问题

| 现象 | 优先检查和处理 |
| --- | --- |
| `uv: command not found` 或找不到 uv 文件 | 按 2.3 节安装并检查 PATH；安装命令使用 `UV_BIN="$(command -v uv)"`，兼容 uv 不在脚本默认路径的情况 |
| `node` / `npm` 找不到，或 `npm ci` 报环境不兼容 | 重开终端检查版本，安装 Node.js 24.x；先解决第一条报错，不删除锁文件改版本 |
| libtorrent 没有合适的 wheel | 检查 Python 必须为 3.12、Mac 架构与第 1 节最低系统版本；不要承诺在旧系统上自动源码编译成功 |
| GitHub、Python 包或 npm 包下载超时 | 检查本机网络与下载工具的代理配置后重试；网页内的应用代理不控制安装器，不关闭证书校验 |
| `Permission denied`，无法执行脚本 | 在项目根目录执行 `zsh scripts/install.sh`，需要 UV_BIN 时用 `UV_BIN="$(command -v uv)" zsh scripts/install.sh`；确认项目位于当前用户可读写目录 |
| `Service did not become ready` 或页面打不开 | 查看下方错误日志；检查 8766 是否被其他程序占用：`lsof -nP -iTCP:8766 -sTCP:LISTEN`。不要直接结束不认识的进程 |
| `LaunchAgent could not start` | 确认是已登录桌面的普通用户，项目路径存在；在该用户终端重试启动并查日志，不用 sudo 启动用户服务 |
| 首次密码设置提示“请从 Mac 本机打开页面” | 在服务器 Mac 浏览器打开 `http://127.0.0.1:8766`，不要通过手机或公网代理首次初始化 |
| API 返回 401 或密码修改后退出 | 重新登录；`curl` 默认不带浏览器 Cookie，401 是预期保护。连续失败过多时等一分钟再试 |
| “访问主机不在允许列表” / “请求来源不匹配” | 本机用 IP 地址；域名访问需在安全设置明确允许；检查代理保留的 Host 和浏览器实际端口一致 |
| 无法读取 OpenList 令牌/无法验证 115 账号 | 确认实际 `openlist_db` 路径可读、115 Open 授权有效，分享登录与 OpenList 同账号且挂载根目录；显式 Token 不能替代数据库核验 |
| OpenList 连通但一直等待 STRM | 检查 Strm 已启用本地保存、虚拟扫描目录和实际本地输出布局，避免 `infuse` 漏一层或多一层；参照 6.2 节 |
| Emby 可检测但刷新失败 | 检查 API Key；服务信息可访问不等于密钥有效。已入库文件与 STRM 会保留，修复后重试当前阶段，无需重新下载 |
| TMDB 没有海报/某个来源超时 | 确认凭据已保存、代理与来源地址可访问；先关闭失败来源验证其他功能，第三方可用性不是健康检查的一部分 |
| 提示有活动任务不能改目录 | 先处理未完成任务；不要直接编辑数据库绕过检查 |
| 任务提交或移动超时、状态不确定 | 先在 115/OpenList 核查远端任务和文件，保留现有记录。不要重复提交同一资源或手动覆盖同名文件 |
| 手机无法打开网页 | 先确认 Mac 本机健康检查正常，再确认同 Wi-Fi、真实局域网 IP、未休眠及网络隔离情况 |

查看后台状态与最近日志：

```sh
launchctl print "gui/$(id -u)/asia.viol1n.medialink"
tail -n 80 "$HOME/Library/Application Support/MediaLink/service-error.log"
```

找不到启动项通常代表服务未加载；日志里没有新内容也可能是端口、文件路径或启动项问题。提交 Issue 时只贴必要报错，先删除账号、路径中的个人信息、链接、提取码和 Token。

## 10. 进阶：前台运行与环境变量

日常使用不需要这一节。需要排错时先停止 LaunchAgent，在同一个项目根目录运行前台实例：

```sh
./scripts/stop.sh
MEDIALINK_HOST=127.0.0.1 MEDIALINK_PORT=8766 ./scripts/run.sh
```

终端需保持打开，按 `Control+C` 停止；前台服务同样会启动后台任务，使用现有数据时可能恢复未完成入库，不能把它当成无操作的预览模式。结束后再用 `./scripts/start.sh` 恢复后台运行。不要同时启动两个实例处理同一数据库。

| 变量 | 实际作用 |
| --- | --- |
| `UV_BIN` | `install.sh` 使用的 uv 可执行文件路径 |
| `MEDIALINK_HOST` / `MEDIALINK_PORT` | `run.sh` 的监听地址与端口，默认 `0.0.0.0` / `8766` |
| `MEDIALINK_DATA` | 后端和备份脚本可读取的自定义数据目录；默认是用户应用支持目录 |
| `MEDIALINK_ALLOWED_HOSTS` | 旧版公网设置的一次性迁移入口；新安装请用网页安全设置 |

`start.sh` 的启动项、日志目录和健康探测按默认数据目录与 8766 设计，它不会自动把当前终端的自定义 HOST/PORT/DATA 复制进新 LaunchAgent。新手使用默认方式；需要定制后台启动时须自行调整并验证启动配置。环境变量通过 shell 导出或启动配置提供，创建 `.env` 文件不会自动生效。
