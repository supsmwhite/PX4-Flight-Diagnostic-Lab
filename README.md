# PX4 Flight Diagnostic Lab

只读采集、保存和重放 PX4 MAVLink 遥测的 Python 诊断实验室（V0.1）。

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

PyMAVLink 固定 2.4.49，PyYAML 6.x，pytest 8.x；不要求真实设备。不要把本项目依赖安装进 ROS 或系统 Python。

## 先离线运行（推荐）

```bash
# 接续上述已激活的虚拟环境；仅生成合成文件，不联网
python scripts/generate_demo.py --output data/demo/raw/example.jsonl
python -m px4_flight_diag replay --input data/demo/raw/example.jsonl --drone demo-replay
python -m pytest -q
```

生成器使用独占创建；若文件已存在请换一个新文件名，不能覆盖原始日志。重放按保存的接收时间计算，尽快运行而非按墙钟等待，不创建 socket。输入是**本项目 schema 1 UDP JSONL**，不是 ULog、裸二进制、tlog 或参考仓库 JSONL；其他格式需显式转换器。

合成示例有5个 HEARTBEAT、105字节、首末跨度4秒、频率1 Hz、最大间隔1秒。数据是人为生成，不是无人机测量。

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
- `plots/`：预留输出目录，本版本不生成图表。

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
│   └── analysis/stats.py            流式通信统计
├── tests/
│   ├── test_parser_stats.py         协议/时间/统计单元测试
│   └── test_pipeline.py             存储/CLI/本机回环集成测试
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

实际测试记录见 [PROGRESS.md](<PROGRESS.md>)。只有合成数据和本机 UDP 回环得到验证，尚无真实 ESP32 日志、真实 ULog 或实机飞行验证。

未实现 ULog 导入/时钟对齐、Web/实时曲线、自动图表、双机对照、EKF2/动力专项诊断、自定义 dialect、签名认证。高负载同步写盘可能导致内核队列丢包，当前不测 OS drop 计数；来源/流数量没有全局配额，应仅用于受控网络。无法判定测试机2横移根因。

参考 [原项目](https://github.com/supsmwhite/px4-optical-flow-gcs) 的通信与记录设计后独立实现，未修改或复制整套地面站；详细区别见 [参考研究](<docs/REFERENCE_REVIEW.md>)。后续建议：实机被动链路验证、ULog与遥测时钟分段对照、双机同条件可追溯可视化。
