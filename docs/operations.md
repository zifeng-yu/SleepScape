# 安装、运行与维护

更新：2026-11-10。本机已有可用配置、数据和程序；已有配置不必重建。
本文只维护操作步骤，实施状态与测试结果见[核验报告](verification.md)。

## 本机安装与启动

所有命令从仓库根目录运行，相对配置路径均以启动目录为准。
新机器需要Git、[uv](https://docs.astral.sh/uv/getting-started/installation/)和Python3.12：

```sh
uv python install 3.12
uv sync --locked
cp -n .env.example .env
uv run --locked sleepscape doctor
```

依赖隔离在`.venv`；`doctor`只检查环境及配置文件是否存在，不读取Key或验证认证。
静态看板随Python程序分发，生产运行不需要Node或前端构建服务。

```sh
uv run --locked sleepscape serve
```

打开`http://127.0.0.1:8000`；macOS也可双击根目录`启动看板.command`。启动窗口保持开启，按`Ctrl+C`停止。
页面刷新只查询本地数据库。空库不会自动联网、创建演示数据或把进程健康当作数据已采集。
默认无密码，仅允许本机直接访问；局域网或公网访问按部署步骤配置独立密码和HTTPS。

## 配置与凭据

模板为`.env.example`，实际配置为不提交的`.env`。环境变量可覆盖本地配置。

| 配置 | 默认/用途 |
| --- | --- |
| `GARMIN_PERSONAL_API_BASE_URL` | 中国区`https://personal.connectapi.garmin.cn`；不自动换区 |
| `SLEEPSCAPE_TIMEZONE` | `Asia/Shanghai`；来源时刻和本地日期的转换 |
| `SLEEPSCAPE_GARMIN_KEY_FILE` | `.secrets/garmin-personal-api-key` |
| `SLEEPSCAPE_GARMIN_TOKEN_CACHE` | `.secrets/garmin-personal-api-bearer-token.json` |
| `SLEEPSCAPE_DATA_DIR` | `./data` |
| `SLEEPSCAPE_HOST` / `SLEEPSCAPE_PORT` | `127.0.0.1` / `8000` |
| `SLEEPSCAPE_WEB_AUTH_FILE` | 默认不设置；部署使用`.secrets/dashboard-auth.json` |
| `SLEEPSCAPE_WEB_ALLOWED_HOSTS` | 默认本机域名/IP；远程部署填写实际域名JSON数组 |
| `SLEEPSCAPE_DEBUG_API_DOCS` | 默认false；开启只提供受保护的`/openapi.json`，`/docs`始终关闭 |

项目Settings读取配置，再交给官方SDK；`SLEEPSCAPE_GARMIN_*`是本项目的路径配置。
需要首次创建或更新Key时，在本人浏览器打开[中国区Personal API Key页面](https://connect.garmin.cn/app/settings/personalApiToken)，
登录后按页面条款选择有效期，将仅显示一次的Key保存到上述私有文件。
不要把Key写入聊天、命令参数、日志、模板或版本库；程序自动换取并缓存token，无需手动复制token或提供账号密码。
保持私有目录700、Key/token和实际配置文件600。有效Key可换取新token；Key过期/撤销时更新本地文件。
手表须先与Garmin Connect同步，本程序不能代替手机同步。

## 日常同步与历史补采

日常使用：

```sh
uv run --locked sleepscape garmin-sync --plan-only
uv run --locked sleepscape garmin-sync
```

第一条只读运行元数据预览计划，不读Key、不联网；第二条才联网。
今日睡眠等数据有则展示并标暂定，全天统计只取到昨日。首次背景38日，日常7日，每隔7日宽回查38日；
详细默认7日，可加`--history-days 38`显式扩大背景。旧历史保留，看板和核验均回读所需30日基线。

指定历史窗口：

```sh
uv run --locked sleepscape garmin-plan --end YYYY-MM-DD --detail-days 7 --history-days 38
uv run --locked sleepscape garmin-collect --end YYYY-MM-DD --detail-days 7 --history-days 38
```

将日期替换为所需截止日，必须早于配置时区今天；不写`--end`则截止昨日。
详细窗口1—31日，背景至少等于详细天数、最多366日；首次验证7晚完整负荷基线建议背景38日。
请求分块、分页和暂定策略见[Garmin方案](plans/garmin.md)，不要通过连续重启绕过采集锁或限流等待。

超过30个已结束日未成功同步时，`garmin-sync`返回`backfill_required`及准确分块命令并停止联网。
按输出逐块采集成功后再sync；未执行或失败的块不算补齐。重复采集同一窗口可取得晚到/修订，当前记录按来源分区更新，审计保留。

## 离线核验与故障处理

```sh
uv run --locked sleepscape garmin-verify
uv run --locked sleepscape garmin-audit
```

`verify`检查最近采集窗口与所需存量负荷历史，`audit`只读源库并用私有临时库重放两遍，结束清理临时副本。
这些是正式数据验收命令，不连接账号，不用真实库注入合成修订；其输出不是手表准确度证明。
报告覆盖当前`data/reports/garmin-verification.json`和`garmin-acceptance.json`，含个人数据的报告保持私有。
`audit`使用24小时新鲜度阈值；过期不等于数据库损坏，应先判断是否需要同步。

| 现象 | 处理 |
| --- | --- |
| 页面没有今天的记录 | 先同步手表到Connect，再执行garmin-sync；不要用旧夜晚替代今天 |
| token到期 | 适配器用有效Key换新，无需手工复制token |
| 认证持续拒绝 | 按错误码检查区域、Key有效性和文件权限，不反复登录或换区 |
| 限流/网络/服务故障 | 遵守最早重试时间，等待后再运行；失败保留旧值且标陈旧 |
| HTTP成功但字段为空 | 检查资源状态和缺失原因；空值、有效空列表、零及失败各自处理 |
| 核验非零退出 | 查看安全错误码及本地私有报告，不能只凭HTTP200或旧报告判断成功 |

## 看板密码

在私有`.env`设置`SLEEPSCAPE_WEB_AUTH_FILE=.secrets/dashboard-auth.json`后运行：

```sh
uv run --locked sleepscape web-password
```

交互输入用户名和至少12字符密码并确认，程序仅保存加盐PBKDF2哈希。重启Web后浏览器使用HTTP Basic认证。
看板密码与Garmin Key独立。远程健康数据访问必须HTTPS；设置密码不意味着可以公开明文8000端口。
浏览器Basic退出体验有限，若更改密码则旧凭据会失效；不要将密码写入shell参数。

## 本地备份与恢复检查

```sh
uv run --locked sleepscape db-backup
uv run --locked sleepscape db-restore-check "backups/实际备份文件名.sqlite3"
```

第二条使用第一条输出的`backup_path`，相对路径以data目录为基准。
备份用SQLite Online Backup API包含已提交WAL，经完整性、外键、raw哈希/JSON验证后保存为新UUID文件，不覆盖旧备份。
恢复检查只在私有临时目录恢复、核对数量和完整性，结束清理，不覆盖运行库；当前未提供自动切换生产库命令。
成功后更新私有`backup-status.json`；状态页表示最近本地验证成功，不保证文件永久存在、已离机或已加密。
目前没有加密、离机复制、定时备份或自动清理；自行管理保留数量，并在清理前核验可恢复副本。

## Linux部署（模板未安装）

先准备Python3.12、uv、Caddy、systemd和独立`sleepscape`用户。程序目录为`/opt/sleepscape`，
生产安装用`uv sync --locked --no-dev`。以下操作只在计划部署的目标机执行：

1. 创建私有配置及目录，所有者为服务用户；保持data/.secrets为700、凭据和配置600，不覆盖已有配置。
2. 配置中国区Key路径、看板密码文件及实际域名白名单，例如`SLEEPSCAPE_WEB_ALLOWED_HOSTS=["sleep.example.com"]`；示例域名须替换。
3. 以服务用户设置看板密码并检查`garmin-sync --plan-only`；应用保持本机监听、调试文档关闭。
4. 审阅并安装[Web服务](../deploy/sleepscape.service)、[采集服务](../deploy/garmin-sync.service)、[定时器](../deploy/garmin-sync.timer)：

```sh
sudo install -m 644 deploy/sleepscape.service /etc/systemd/system/sleepscape.service
sudo install -m 644 deploy/garmin-sync.service /etc/systemd/system/garmin-sync.service
sudo install -m 644 deploy/garmin-sync.timer /etc/systemd/system/garmin-sync.timer
sudo systemd-analyze verify /etc/systemd/system/sleepscape.service /etc/systemd/system/garmin-sync.service /etc/systemd/system/garmin-sync.timer
```

替换[Caddy模板](../deploy/Caddyfile.example)域名后合并到服务器配置，不覆盖已有站点。
Caddy负责HTTPS并保留Authorization给应用，不再配置第二套代理Basic；不要把仓库、data或.secrets作为静态目录。

```sh
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now sleepscape.service
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

先确认HTTPS可用、匿名无法读取健康数据、正确密码可读且无公开raw/data路径。
Web模板仅信任127.0.0.1代理头，不能改为任意可信来源；8000保持本机，外部只访问Caddy。
完成首次同步与备份恢复检查后启用定时器：

```sh
sudo systemctl enable --now garmin-sync.timer
sudo systemctl list-timers garmin-sync.timer
```

模板按上海时间08:30、12:30运行，Persistent补一次错过触发，历史缺口仍由sync判断。
Web单worker，服务内存上限分别512MiB/768MiB是保护配置，不能代替2C2G实测。
按[架构中的资源验收标准](architecture.md#资源预算与部署验收)观察持续运行、并发读取和每日增长；当前无自动磁盘水位保护。

## 回归、构建与清理

```sh
make check
make browser-install
make browser-check
uv build --wheel
make clean
# 如需释放浏览器及Node开发依赖占用
make clean-dev
```

保留tests中的必要合成回归；浏览器验收先确认demo=true，不使用真实数据截图。
`browser-install`重建Node/Chromium开发依赖，`browser-check`启动隔离合成服务；这些依赖不进入生产运行。
`clean`删除测试报告/截图、构建产物和代码缓存，`clean-dev`另删除node_modules与.browser-cache；下次浏览器验收先重新安装。
清理入口不删除.venv、源码、tests、私有凭据、实际数据库、原始观测、生产核验报告或备份。
不要手工只拷贝正在写入的SQLite主文件做备份，不通过删除WAL或原始观测“清理”运行库。

## 硬件阶段入口（尚未开发操作工具）

采购前先阅读[采购清单](procurement.md)及[接线/固件方案](plans/hardware.md)。主方案无需焊接，
后续交付预编译固件与macOS USB刷写/配置入口；目前仓库没有可执行的硬件刷写命令或设备上传路由。
不能将厂商语音固件当作SleepScape固件，也不把Garmin密钥写入硬件。
实际烧录、Wi-Fi配置、设备令牌生成/撤销、故障处理在工具实现后统一更新到本节，不维护平行旧教程。
