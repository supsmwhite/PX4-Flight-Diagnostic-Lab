# PX4 Flight Diagnostic Lab

只读采集、保存、重放、离线报告与实时Web可视化 PX4 MAVLink 遥测的 Python 诊断实验室（V0.2+）。

## 背景与边界

目标环境为 Ubuntu、HKUST NXT-Dual、PX4 v1.15.2 定制固件、ESP32 Wi-Fi/UDP、光流+测距+IMU/EKF2 室内四旋翼。测试机2在 Position 起飞时横移的原因仍待验证；EKF2 估计位置不是真实位置，光流创新异常也不单独证明根因。本版本只建立可靠数据基础，不作自动根因判定。

已实现：配置化 UDP 被动接收、MAVLink v1/v2 common 解析、多帧/分片、按来源隔离、原始 JSONL、完整字段与双接收时间、保守消息时间注释、离线重放、消息频率/间隔/心跳/CRC/序号候选缺口/吞吐统计、独立 Session、pytest。**不发送心跳、消息请求、参数写入或飞行控制；无需 MAVSDK。**

## Ubuntu 安装

需要 Python 3.10 或更新版本；当前实测 Python 3.10.12。

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
git clone https://github.com/supsmwhite/PX4-Flight-Diagnostic-Lab.git
cd PX4-Flight-Diagnostic-Lab
python3 -m venv .venv
source .venv/bin/activate
# ROS 终端可能预设 PYTHONPATH，避免外部 ROS 包污染本虚拟环境
unset PYTHONPATH
python -m pip install -e '.[dev]'
python -m pip check
python -m px4_flight_diag --help
px4-flight-diag --version
```

PyMAVLink 固定 2.4.49，PyYAML 6.x，Plotly >=5.24,<7（独立HTML绘图），pytest 8.x；不要求真实设备。不要把本项目依赖安装进 ROS 或系统 Python。

## 先离线运行（推荐）

```bash
# 接续上述已激活的虚拟环境；仅生成合成文件，不联网
python scripts/generate_demo.py --output data/demo/raw/example.jsonl
python -m px4_flight_diag replay --input data/demo/raw/example.jsonl --drone demo-replay
python -m pytest -q
```

生成器使用独占创建；若文件已存在请换一个新文件名，不能覆盖原始日志。重放按保存的接收时间计算，尽快运行而非按墙钟等待，不创建 socket。输入是**本项目 schema 1 UDP JSONL**，不是 ULog、裸二进制、tlog 或参考仓库 JSONL；其他格式需显式转换器。

合成示例有5个 HEARTBEAT、105字节、首末跨度4秒、频率1 Hz、最大间隔1秒。数据是人为生成，不是无人机测量。

## V0.2 离线交互报告

```bash
# 使用现有Session，不连接飞控，不重新解析raw
python -m px4_flight_diag analyze --session data/drone2/<session>
# 所有图统一使用电脑接收时间；自定义断线阈值（秒）
python -m px4_flight_diag analyze --session data/drone2/<session> --time-axis receive --gap-seconds 0.5
xdg-open data/drone2/<session>/reports/diagnostic.html
```

输入为 `processed/messages.jsonl`，元信息和采集摘要可选。输出首次为 `reports/diagnostic.html`，再次运行依次为 `diagnostic-001.html` 等，独占创建，不覆盖任何原文件。Plotly JS内嵌，浏览器无需联网或服务器即可查看数值、缩放、拖动。

五组图：实际/目标Roll与Pitch（度）、NED水平估计位置（m）、消息直接提供的水平速度（m/s）、光流积分角/质量/积分时间、测距及量程（m）。按 `(peer,SYSID,COMPID)` 分节，光流按sensor_id、测距按ID/方向分曲线；无数据图明确显示 `INSUFFICIENT_DATA`。目标消息仅表示遥测目标，报告同时列出未解锁/心跳过期/模式未知计数，不代表执行了飞行控制。

默认 `auto` 按图检查显式 `time_boot_ms` 是否齐全且各消息类型内无倒退，满足则用相同来源的飞控启动相对时间；光流等未知 `time_usec` 使用电脑单调接收相对时间。不同时间基准的图不能直接毫秒级比较；需要一致横轴时用 `--time-axis receive`，仍含链路传输延迟。不会猜测时钟偏移或隐藏重启。

超过0.5秒（可配置）的间隔、无效值、忽略掩码和时间回退会断线；不插值、不填零。报告含11类消息计数/频率/最大间隔和会话级CRC警告，CRC不阻止绘图。接收频率不是源采样率，估计位置不是真值，图表不是根因诊断。读取损坏JSON/结构时报告文件行号，不悄悄跳过。

## V0.2+ 实时Web（先用历史回放验收）

```bash
env -u PYTHONPATH .venv/bin/python -m pip install -e '.[dev]'
# 不开飞控UDP端口、不发送数据、不改历史Session；原始接收间隔/速度倍率调度
# 需要Session中的raw/udp.jsonl，复用现有Parser处理CRC及分片
python -m px4_flight_diag live --replay-session data/drone2/<session> --speed 1 --start-delay 5
# 浏览器访问 http://127.0.0.1:8765

# 仅在现场人员授权、无桨且上锁后，进行真实ESP32被动采集
python -m px4_flight_diag live --config config/connection.yaml --web-port 8765
```

Web固定只绑定127.0.0.1，UDP仍按原连接配置监听，两端口必须不同。未提供任意路径HTTP接口、飞控控制或公网功能；HTML/Plotly脚本都由本机提供，无CDN。**回放生产者不创建socket；实时回放的浏览器服务需要本机HTTP/WebSocket socket**，原replay命令仍完全无网络。

采集时长到达后停止记录、保存摘要并显示COMPLETED；服务继续保留末段曲线直到Ctrl+C。浏览器关闭不停止采集；Ctrl+C关闭线程/文件/WebSocket。回放速度仅改变调度，不改变历史时间戳、频率统计和目标心跳上下文。新采集Session可照常使用analyze。

页面约5Hz增量更新五组图，默认最近60秒，坐标为电脑接收相对秒（包含链路延迟），不猜time_usec或做时钟对齐。来源可选择，传感器独立曲线；过期数据标为历史，断线自动重连。状态WAITING_FOR_DATA/RECEIVING/STALE/DISCONNECTED/COMPLETED/ERROR仅界面观测，不是飞控failsafe或解锁真值。整体无有效消息2秒标STALE，无任意包5秒标DISCONNECTED；HEARTBEAT及各类消息独立计算2秒新鲜度。

显示层最多20000点/64来源/512消息流，每客户端快照最多2000点，最多4个WS客户端，发送超时1秒；慢客户端跳过旧更新并显示client_missed，容量/来源限额记display_dropped。正常60秒窗口到期不算显示丢弃。前端最多128曲线、每曲线12000点；这不是原始存档丢包数。采集保存先于显示通知，网页与磁盘记录解耦；显示观察器异常计入摘要，不因此中断存档。

新增依赖FastAPI（只读路由/WS）、Uvicorn（本机ASGI服务）、websockets（WS协议）；httpx仅用于测试。没有MAVSDK或控制SDK。当前只完成纯软件与真实历史日志回放验收，**未完成本轮真实在线ESP32地面验收**。

## UDP 采集

```bash
cp -n config/connection.example.yaml config/connection.yaml
# 用编辑器修改 config/connection.yaml：绑定本机地址、端口、时长、机号、输出目录
python -m px4_flight_diag capture --config config/connection.yaml
```

配置文件必须包含 `bind_host / bind_port / duration_seconds / drone / output_root` 五项，不接受拼错或额外字段。路径相对**执行命令时的工作目录**。`bind_host` 是本机监听地址（示例 `0.0.0.0`），不是 ESP32 地址；示例端口14551可改。ESP32 应主动向本机 IP/端口转发；此工具不向 ESP32 发包，也不主动请求数据速率。允许端口的防火墙规则应限制为可信局域网。`drone` 是人工实验分类标签，不能代替 SYSID/COMPID 身份验证。

时长到达或 Ctrl-C 后保存摘要；Ctrl-C 标记 interrupted。无数据时报告零计数与 null 频率，不伪造测量。错误输入返回退出码2，已有 Session 保存 failed 摘要；磁盘故障可能阻止最终摘要，请检查错误与原始文件。

## 输出与解读

每次采集/重放新建 `data/<drone>/<UTC>-<随机后缀>/`：

- `metadata.json`：版本、模式、配置或源文件关联。
- `raw/udp.jsonl`：先于解析保存的原始 UDP 字节、包索引、来源、UTC 与单调时间。
- `processed/messages.jsonl`：解析成功/错误事件、原始帧、完整字段、时间来源。
- `reports/summary.json`：汇总，同时向 stdout 输出一行 JSON。
- `plots/`：预留目录；V0.2图表内嵌于新增的 `reports/diagnostic*.html`，不改写采集摘要。

重放写新 Session，并复制经过验证的原始记录，绝不改源文件。每条原始记录 flush，正常关闭 fsync；并非断电零丢失承诺。原始日志默认不进入 Git。

频率按每个 `(peer, SYSID, COMPID, msg_type)` 的 `(N-1)/(最后-最先接收时间)` 计算；少于两条或零跨度为 null。吞吐按所有原始字节/首末包跨度，不是整场含静默时间的平均。分片帧归属于完成包，同包多帧间隔可能为0。CRC错误是解析器拒绝的候选帧；序号缺口仅为模256启发式候选缺失，UDP丢包率无法从当前观测可靠确定，三者绝非同一指标。

`time_boot_ms` 注释为 boot 秒，`time_usec` 保守标为 unknown；不按数值大小猜测启动/Unix时间，不用电脑接收时间冒充采样时间。未实施重启分段、时钟漂移修正或 ULog 对齐。

## 工程目录与职责

```text
PX4-Flight-Diagnostic-Lab/
├── README.md                       安装、运行、边界
├── AGENTS.md                       开发交接与安全规范
├── ARCHITECTURE.md                  架构与设计理由
├── PROGRESS.md                      实际进展及验证
├── pyproject.toml                   构建、依赖、CLI
├── .gitignore
├── config/connection.example.yaml  监听配置示例
├── docs/
│   ├── DATA_CONTRACT.md             字段、单位、时间与统计契约
│   ├── EXPERIMENTS.md               未填充的实验模板
│   └── REFERENCE_REVIEW.md          六个参考文件研究笔记
├── scripts/generate_demo.py         无网络的合成样例生成器
├── src/px4_flight_diag/
│   ├── __init__.py                  版本
│   ├── __main__.py                  python -m 入口
│   ├── cli.py                      参数、配置校验、共用处理管线
│   ├── collectors/udp.py            只接收 UDP
│   ├── communication/parser.py      分来源增量 MAVLink 解析
│   ├── timebase/normalize.py        保守时间字段注释
│   ├── storage/session.py          JSONL 校验、独占 Session
│   └── analysis/
│       ├── stats.py                 流式通信统计（V0.1保留）
│       ├── telemetry.py             Session读取、来源隔离、字段/单位/缺测处理
│       └── report.py                五组图表、通信提示、独立HTML
├── tests/
│   ├── test_parser_stats.py         协议/时间/统计单元测试
│   ├── test_pipeline.py             存储/CLI/本机回环集成测试
│   ├── test_telemetry.py            字段、四元数、来源、时间轴校验
│   └── test_report.py               图表、HTML、analyze CLI与无网络验证
└── data/                           忽略的实验 Session（drone1/drone2等）
```

## 常见问题

- `No module named px4_flight_diag`：激活 `.venv`，在项目根目录安装 `-e '.[dev]'`。
- pip 报 ROS `generate-parameter-library-py` 缺少依赖：`unset PYTHONPATH` 后重新检查，不要为此修改 ROS 环境。
- `Address already in use`：地面站或其他进程占用端口；关闭冲突程序或修改两端端口，不开启共享监听来掩盖冲突。
- `Cannot assign requested address`：监听地址不是本机地址，检查网卡；不要填 ESP32 IP。
- 无数据：检查 ESP32 目标IP/端口、Wi-Fi 路由、防火墙、PX4是否主动输出消息。工具不会请求消息。
- CRC/坏数据：可能是链路损坏、噪声、错误协议或定制 dialect；保留原始日志再分析，不能直接认定 UDP 丢包。
- `invalid raw record`：按报错行查看格式/截断情况；保留原始文件，转换或修复必须另存。
- pending_bytes 非0：可能采集结束时分片未完成或损坏长度导致等待，不能直接当丢包数量。

## 验证与未来工作

实际测试记录见 [PROGRESS.md](<PROGRESS.md>)。已验证合成数据、本机 UDP 回环及用户本地机2约20秒真实静止ESP32日志的离线可视化。没有真实 ULog 或实机飞行验证；本地Session默认不进入Git，其他环境需要自行提供数据路径。

V0.2+已实现本机Web实时曲线；未实现 ULog 导入/严格时钟对齐、双机对照、EKF2/动力专项诊断、自定义 dialect、签名认证。高负载同步写盘可能导致内核队列丢包，当前不测 OS drop 计数；来源/流数量没有全局配额，应仅用于受控网络。无法判定测试机2横移根因。

参考 [原项目](https://github.com/supsmwhite/px4-optical-flow-gcs) 的通信与记录设计后独立实现，未修改或复制整套地面站；详细区别见 [参考研究](<docs/REFERENCE_REVIEW.md>)。后续建议：实机被动链路验证、ULog与遥测时钟分段对照、双机同条件可追溯可视化。
