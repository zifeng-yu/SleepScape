# 安装、运行与维护

更新：2026-11-11。本机已有可用配置、数据和程序；已有配置不必重建。
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
| `SLEEPSCAPE_ENVIRONMENT_INGEST_ENABLED` | 默认false；实际HTTPS部署并登记设备后才改true，控制唯一环境POST入口 |
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
备份用SQLite Online Backup API包含已提交WAL，经完整性、外键、raw哈希/JSON及环境表一致性验证后保存为新UUID文件，不覆盖旧备份。
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
`clean`也删除可重建的`firmware/build/`、生成的`sdkconfig`及旧构建配置；刷写前需要重新`make firmware-build`。
清理入口不删除.venv、.firmware-tools、源码、tests、私有凭据、实际数据库、原始观测、生产核验报告或备份。
不要手工只拷贝正在写入的SQLite主文件做备份，不通过删除WAL或原始观测“清理”运行库。

## 硬件构建与检查

先阅读[采购清单](procurement.md)与[接线方案](plans/hardware.md)。以下入口已经实现，采购前软件核验结果见报告；
真实刷写、接线、Wi-Fi/传感器和新手完整操作仍需到货验收。当前没有代用户下单、部署或登记真实设备。

在开发电脑仓库运行；服务器不需要ESP-IDF。Mac还需Apple命令行开发工具提供Git/clang；若提示未安装，先运行`xcode-select --install`并完成系统安装界面。项目脚本不代替这一系统步骤：

```sh
make firmware-setup
make firmware-build
make firmware-check
```

setup固定ESP-IDF v5.5.6提交及子模块，使用项目Python3.12对应的基础解释器，把SDK依赖、CMake3.30.2、Ninja1.12.1装入`.firmware-tools/`，不修改系统Python或安装IDE。
build产生`firmware/build/sleepscape_environment.bin`并强制应用镜像≤1.8MiB。check运行C主机算法/队列测试以及强制的C→Python协议互通；缺SDK或互通依赖会失败，不能以跳过代替通过。
CI的固件任务独立执行setup/build/check；普通Python/浏览器测试与它分开。源码和固定版本是交付基础，构建产物可清理后重建。

到货后先按[硬件方案的逐步接线](plans/hardware.md#接线步骤)核对C008-C、SEN0528、SEN0228及DFR0759：两块传感器接分线模块，分线模块接主控底部的3V3/GND/G21/G25，全程断电并按文字标签核对。完成后用USB-A→USB-C数据线连接主控；只有USB-C接口的Mac需先插支持数据传输的USB-C公→USB-A母转接头。不要把仅充电线当数据线。

### 首次安装与后续升级

先确认目标串口，例如macOS的`/dev/cu.usbserial-…`，把下面占位值换成实际端口。工具只操作明确选定的串口，不会自动选择其他USB设备。

**刚购买的板卡第一次安装SleepScape**使用：

```sh
make firmware-first-flash PORT=/dev/cu.实际串口
```

工具先校验串口、SDK和构建镜像清单，再显示整片Flash清除提示。只有你在交互终端准确输入`FIRST-INSTALL`后，才清除板上的厂商预装固件、配置及全部数据，并写入启动程序、分区表和应用。默认115200波特率。此步骤为首次接入本项目准备干净的NVS和SPIFFS区域，避免出厂程序的存储布局影响初始化。尚未对实际新板执行，实物首装验收仍待完成。

首次安装完成后运行只读状态检查，不需要打开串口终端手写协议：

```sh
make firmware-status PORT=/dev/cu.实际串口
```

预期为`status=ready`、`firmware_version=0.2.0`和`configured=false`；若版本不符，先核对构建与刷写结果。该命令只发送状态查询，仅显示状态、固件版本及是否保存有效配置，不显示Wi-Fi或令牌。随后按后文完成USB配置、收到已写入NVS的确认；配置后重启应能初始化空白SPIFFS并进入采集，不能出现配置存储或队列挂载失败。这是到货首项软件验收，不以电脑构建成功代替。


**已经使用SleepScape后的普通升级**改用：

```sh
make firmware-flash PORT=/dev/cu.实际串口
```

普通flash同样校验三个允许地址，只写启动程序、分区表和应用，保留NVS配置与离线队列，不整片擦除。不要把`first-flash`当作升级、换Wi-Fi或故障重试步骤；已有数据的板卡若需清除，必须另行确认数据保留/补传和清除目的。换Wi-Fi使用后文configure即可。
串口找不到时先核对数据线和对应USB驱动；设备未进入下载模式时按原厂板卡说明操作，不通过清除Flash试错。

## 环境设备登记与HTTPS接收

以下服务端命令必须在**实际负责接收上传的服务器及其数据库配置**下运行；在本机登记不会自动登记到云服务器。
先完成上文HTTPS与域名白名单部署，保留8000仅本机监听，再在服务端私有`.env`中设置：

```dotenv
SLEEPSCAPE_ENVIRONMENT_INGEST_ENABLED=true
```

重启Web服务使设置生效。未启用时环境入口关闭；公网入口必须使用HTTPS，不能用伪造代理头或普通HTTP绕过。
设备走独立Bearer认证，不使用浏览器Basic密码或Garmin Key。Caddy必须把Authorization透传给应用，不加第二套覆盖设备认证的Basic。

首次准备设备档案，保留已有私有目录与文件，只复制尚不存在的文件：

```sh
mkdir -p .secrets
chmod 700 .secrets
cp -n firmware/profile.example.json .secrets/environment-profile.json
chmod 600 .secrets/environment-profile.json
```

编辑私有档案中的`placement_id`，用实际固定摆位的标识替换占位文字，例如`bedside-left-2026-11`。
其他硬件、固件版本、`i2c_bus`、`light_measurement`、声音处理方式和阈值字段必须与刷入程序匹配。SEN0228使用项目固定弱光档，不能只替换传感器名称却沿用其他光照档位；以当前`firmware/profile.example.json`和[数据契约](data-contract.md#当前光照测量配置)为准。没有校准就保留`uncalibrated`，不能为了通过校验虚填测量能力。
若此前登记过其他传感器档案，不覆盖旧档案；使用新的profile_id登记本组合，再让设备配置指向它。旧记录与原档案继续保留作为观测证据。
下面ID为示例，可保持一致使用，不能把自己的秘密填入命令参数：

```sh
uv run --locked sleepscape environment-register \
  --device-id bedroom-01 --profile-id bedroom-veml-01 \
  --profile-file .secrets/environment-profile.json \
  --token-file .secrets/environment-device.json
uv run --locked sleepscape environment-status
```

若目标机已有数据库，首次登记前先执行上文`db-backup`并确认恢复检查成功。登记命令校验现有基础表，再以事务建立环境表及设备记录；它不会自动生成备份，Garmin事实不被替换。令牌随机生成，只把哈希存库；
`--token-file`必须是不存在的新文件，输出为JSON，包含device_id、profile_id和token，权限600、父目录700。终端只报告成功状态，不显示令牌。
status只读本地环境接收状态，不联网、不凭空创建数据库；“已登记”不代表设备已经上传。

## USB配置与首次上传

USB操作在连接硬件的开发电脑上执行。若令牌在云端生成，通过已有加密SSH通道安全复制到本机私有目录，并核对文件600/目录700；
不要把内容粘贴进聊天、日志、终端命令或公共配置。也不要拿Garmin Key尝试设备令牌字段。

```sh
uv run --locked python tools/firmware.py configure \
  --port /dev/cu.实际串口 \
  --token-file .secrets/environment-device.json
```

工具从注册JSON自动读取device_id/profile_id/token，只交互询问完整上传地址、2.4GHz Wi-Fi名称和隐藏密码。
上传地址为`https://你的实际域名/api/v1/ingest/environment-batches`，不带查询参数、片段或用户名密码；Wi-Fi密码为空或8–63字节。
它通过115200 UART发送单行配置，仅收到“已写入NVS”的ACK才报告保存成功；设备随后重启，串口输出不会回显秘密。
没有可隐藏输入的终端时会拒绝回显降级；可改用权限600、目录700的私有完整配置JSON：

```sh
uv run --locked python tools/firmware.py configure \
  --port /dev/cu.实际串口 --config-file .secrets/environment-usb.json \
  --token-file .secrets/environment-device.json
```

这里USB配置JSON填写server_url、ssid、password，可省略由令牌文件提供的device_id/profile_id；**不能再包含token**。
显式填写的设备/配置ID必须与令牌JSON一致，否则拒绝。若不使用`--token-file`，完整配置JSON需包含全部六项。
不用文件也可运行`make firmware-configure PORT=/dev/cu.实际串口`，手动填写ID、地址、Wi-Fi，令牌和密码均隐藏输入。

配置成功后再运行：

```sh
make firmware-status PORT=/dev/cu.实际串口
```

此时版本仍应为`0.2.0`，且`configured=true`。该布尔值只说明固件能读到有效的已保存配置，配置ACK只证明NVS写入；均不证明Wi-Fi连接、TLS或上传成功。还需在服务器`environment-status`确认新增真实记录，并检查时钟锚点与各通道覆盖/故障；
当前环境曲线尚未接入看板，不能以Garmin页面能打开代替硬件端到端验收。
确认后改接墙上电源、固定摆位。换Wi-Fi可再次USB配置，不必重刷程序。

## 换摆位、令牌轮换与撤销

改变摆位或测量设置时先准备新档案：

```sh
cp -n firmware/profile.example.json .secrets/environment-profile-next.json
chmod 600 .secrets/environment-profile-next.json
```

编辑新档案记录实际摆位/设置，再登记新的profile_id；已登记档案不可原地覆盖：

```sh
uv run --locked sleepscape environment-profile \
  --device-id bedroom-01 --profile-id bedroom-veml-02 \
  --profile-file .secrets/environment-profile-next.json
```

新档案必须填写实际变更。设备USB配置也要切换到同一新ID；旧记录保留原配置。注册JSON内若仍写旧profile_id，
工具会拒绝与新配置混用；可以更新该私有JSON中的profile_id为已经登记的新ID，或用下面轮换流程取得不带旧profile_id的新令牌文件。

```sh
uv run --locked sleepscape environment-rotate-token \
  --device-id bedroom-01 --token-file .secrets/environment-device-rotated.json
```

轮换立即使旧令牌失效，已有历史与待补传记录保留；新JSON包含device_id/token，USB配置时需填写已登记的profile_id，
或从私有USB配置JSON读取。再执行configure并指向新令牌文件即可，不在命令行传令牌实值。
完成切换后删除已经失效的本地旧令牌文件，避免保留多份无用敏感配置；不要删除数据或设备档案记录。

```sh
uv run --locked sleepscape environment-revoke --device-id bedroom-01
uv run --locked sleepscape environment-status
```

撤销立即禁止该设备上传，历史仍保留。计划重新启用时执行令牌轮换，再USB配置新令牌；不能通过清库或更改设备ID绕过拒绝。

## 硬件运行与故障边界

| 现象 | 当前行为与处理 |
| --- | --- |
| 冷启动时没有网络/SNTP | 继续采样和记录boot_id/单调时间；UTC未知，取得SNTP后才允许TLS上传；不能用接收时刻补齐 |
| Wi-Fi中断 | 1–300秒退避重连；持续供电时目标缓存48小时，容量验证不等同实机验收 |
| 401/403 | 保留记录、提示认证拒绝并延长重试；核对登记/撤销状态，必要时轮换再USB配置 |
| 429或普通网络错误 | 遵守有界Retry-After并退避；没有完整且匹配的逐记录ACK就不删除待发记录 |
| 首次空白存储 | 仅确认整个区域全空白才初始化文件系统；普通刷写保留已有数据 |
| 文件系统损坏 | 不自动格式化、不整片擦除；启动时停止采集初始化并报告存储异常，保留现场后诊断 |
| 拒收记录隔离区达到64KiB | 停止继续隔离/上传推进并保留数据，不因此停止其他采样任务；需检查存储状态后处理 |
| 单通道缺失/异常 | 记录缺失、有效覆盖及质量，不用零值伪装成功；其余通道按真实状态继续。I²C两传感器共线，整体总线故障可能同时影响温湿度与光照 |
| 光照低光、暖机或饱和 | 按测量配置保留低光提示、暖机缺口或`out_of_range`；饱和不能当550 lx的真实观测，不自动换量程或修改校准 |
| 看板没有环境曲线 | 当前仍待环境查询与UTC映射开发，不属于刷写成功后的自动展示承诺 |

普通NVS不提供抗物理读取保证；任何流程都不保存或上传PCM录音。断电期间不能采样，未完成/未提交窗口可能丢失。
实物阶段还须完成读数合理性、USB驱动、8小时并发采样、48小时离线补传和随机掉电测试，核验报告将区分主机通过与实物通过。
