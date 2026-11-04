# 架构：被动采集与可复现重放

## 目标和非目标

V0.1 面向 PX4 已存在的 UDP MAVLink 遥测流，保存原始数据、解析 common 消息并报告通信观测。**不向飞控发送数据，不自动发现/查询设备，不执行控制，不读取 ULog，不输出故障根因结论。** 当前能力不是飞行安全系统，也没有实机验证声明。

```mermaid
flowchart TD
    UDP[UDP recvfrom 只接收] --> P[原始 packet]
    INPUT[已有 raw JSONL] --> CHECK[严格校验 无网络]
    CHECK --> P
    P --> RAW[新 Session/raw 不覆盖源]
    P --> PARSER[Parser 按 peer 隔离]
    PARSER --> REC[common 消息 / parse_error]
    REC --> TIME[时间域注释 不对齐时钟]
    TIME --> PROCESSED[Session/processed]
    P --> STATS[Stats 通信统计]
    TIME --> STATS
    STATS --> REPORT[Session/reports]
```

## 设计决策：MAVLink-first，ULog-on-demand

**2026-11-04 确认：ESP32 遥测优先，ULog 按需补充。** 核心目标是尽可能使用用户此前已验证的 ESP32 + Wi-Fi + MAVLink 链路完成故障定位，仅在实际证据不足时引入 ULog 或额外实验数据。用户已有链路验证不等于本工程已完成真实设备验证。

1. **默认工作流**：ESP32 遥测采集 → 原始记录与离线重放 → 采样质量检查 → 实时监控与第一轮故障诊断。实时监控和专项诊断为后续能力，V0.1 当前只实现采集、存储、重放、基础时间处理与通信统计。
2. **ULog 可选**：不要求每次实验后手动导入 ULog，不把 ULog 文件、pyulog 安装或 ULog 对齐作为常规 MAVLink 分析的前置条件。只有明确指出遥测证据缺口后，才评估是否需要该可选模块。
3. **V0.1 范围不变**：继续优先可靠原始数据记录、离线重放、时间戳处理和链路质量统计；不在本次决策更新中添加诊断引擎、ULog 导入或融合系统。
4. **未来诊断的证据约束**：每个诊断模块必须列出所需消息/字段及单位、实际收到且有效的字段、分析时间窗口，以及采样质量（实际接收频率、间隔/抖动、缺测、CRC/序号异常、时间域与对齐限制）。结论必须注明适用条件、可靠性和局限，不能只因消息类型出现过就认定证据充分，也不能给出未经验证的置信百分比。
5. **数据不足的明确状态**：缺少关键字段、有效样本或足够采样质量时，未来诊断输出 `INSUFFICIENT_DATA`，逐项说明“缺少什么”“为什么该判断需要它”“ULog 是否可能补足及需核对的话题/字段”。无法确认 ULog 是否含有所需信息时应写未知；若 ULog 也不能提供真值或外部观测，应说明所需额外实验/数据，而不是强行给出根因。
6. **双机对照优先**：正常机与异常机在相近实验条件下的 MAVLink 对照分析是重要后续功能。保留机号、固件/参数、环境、时间窗口与采样质量差异；估计位置仍不是实际位置，对照差异不自动证明因果。
7. **只保留扩展能力**：未来可加入 ULog 和外部视频时间对齐，必须保存各自原始时间域、对齐依据及误差。不提前实现复杂融合，不按电脑接收时间假定传感器或视频已经同步。

`INSUFFICIENT_DATA` 是后续诊断接口要求，**尚不是 V0.1 已实现的结构化输出状态**。当前执行状态仍为 `completed/interrupted/failed`，诊断说明仍为“无法判断故障根因”；执行成功不表示证据充分。以后实现诊断状态时，应与执行状态分离，并同步数据契约与测试。

## 模块与实际接口

| 层 | 接口与职责 | 不承担的职责 |
| --- | --- | --- |
| 采集 | [collectors/udp.py](src/px4_flight_diag/collectors/udp.py) `receive_packets(bind_host, bind_port, duration, ready=None)`；IPv4 UDP 排他监听、接收时间戳与 Base64 原始包 | 不发送、转发、请求遥测，不验证飞控身份 |
| 解析 | [communication/parser.py](src/px4_flight_diag/communication/parser.py) `Parser.feed(packet)` 返回记录列表；`pending_bytes()` 返回残留字节；按 `(IP,port)` 隔离跨包状态 | 不开 socket；不验证签名，不支持自定义 dialect |
| 时间 | [timebase/normalize.py](src/px4_flight_diag/timebase/normalize.py) `normalize_time(msg_type,fields)` 保留源字段并注释单位/时间域 | 不估计时钟偏移，不把 boot 时间强行转为 UTC |
| 存储 | [storage/session.py](src/px4_flight_diag/storage/session.py) `Session`、`read_packets`、严格 JSON 序列化 | 不覆盖原始输入、不静默修复坏行 |
| 分析 | [analysis/stats.py](src/px4_flight_diag/analysis/stats.py) `Stats.add_packet/add_record/summary`；在线计数、接收间隔和序号观测 | 不判飞行故障根因，不用缺口计算确证丢包率 |
| 编排 | [cli.py](src/px4_flight_diag/cli.py) `capture` / `replay` 共用 `process` | 不启动 SDK、Web 控制台或设备控制任务 |

解析器正在独立演进，具体错误类型与重同步策略应以对应版本实现及测试为准；本架构不把参考仓库算法当成本工程已完成的实现。未知 ID 记录为 `unknown_message` 错误，不计有效消息，因为无法验证其 CRC_EXTRA。有效帧的 CRC 只校验帧完整性，来源/IP/SYSID 也不证明身份。当前来源状态数没有全局配额，损坏长度的不完整候选可能保留 pending 等待后续字节，没有超时清理；不能宣称对恶意输入具备完整资源防护。

## 数据生命周期

每次 capture/replay 都新建 `<output_root>/<drone>/<UTC时间>-<随机后缀>/`，包含：

```text
metadata.json
raw/udp.jsonl
processed/messages.jsonl
plots/
reports/summary.json
```

`plots/` 当前仅预留；V0.2图表由analyze另外生成到reports/diagnostic*.html。输出采用排他创建；原始包在解析前写入并 flush。干净关闭时 raw/processed 文件执行 fsync，但不保证掉电时最后一条记录完整。summary 记录 `completed/interrupted/failed`；失败会尽量保留已接收证据和错误，文件系统失败等仍可能使 summary 不完整。

重放读取原文件，检查 schema、packet index、接收单调时间、来源结构、UTC 时区、Base64 和长度，再把同一包记录写到新会话 raw 中；保留原接收时间，而非重放执行时间。JSON 排版可能重新序列化，因此这是包记录/载荷的复制，不承诺文件字节级副本。metadata 记录原始输入绝对路径和工具版本。

### 为什么先用 JSONL 而不是数据库

V0.1 优先需要逐包追加、保留坏数据、可检查的交换格式和离线重放。JSONL 允许流式写入/读取，一行定位错误，无需数据库服务或迁移系统，原始 Base64 可完整保留 datagram。代价是空间开销、没有索引/事务查询、同步 flush 吞吐限制以及崩溃时末行可能截断；它并不自动提供不可篡改保证。

若后续需要跨会话索引或长时查询，可从原始日志构建可重建数据库索引，而不是让数据库成为唯一原始证据。索引和清洗产物不得反向修改 raw；容量与性能要用实际负载测量。

## V0.2 离线可视化（已实现）

`analyze --session ...` → [telemetry.py](<src/px4_flight_diag/analysis/telemetry.py>) → [report.py](<src/px4_flight_diag/analysis/report.py>) → 独立HTML。读取schema 1 processed记录，不需要raw，不创建socket，不改采集/重放管线。Plotly是唯一新增绘图依赖，JS内嵌报告，不依赖CDN或服务。

- 按 `(peer,SYSID,COMPID)` 分节，每节五组图；测距按ID/方向、光流按sensor_id隔离。未知标识明确显示None，不混入已知传感器。
- 姿态rad转度，目标四元数[w,x,y,z]校验有限值/范数/忽略位后转欧拉；距离cm转m；速度直接读vx/vy，光流保留积分角量和积分窗口。
- `auto` 只对图中显式time_boot_ms完整且每种消息无倒退的流使用boot相对时间，原点为该来源最早有效boot字段；其余用来源首个消息的接收单调时间。未知time_usec不推测。可用 `--time-axis receive` 使全图采用一致接收轴；并非毫秒级因果时钟。
- 默认超过0.5秒间隔断线（可配置），无效字段None、connectgaps=False，无插值。重新生成独占创建 diagnostic.html、diagnostic-001.html等，原报告不覆盖。
- 每条目标结合此前最多2秒内心跳统计上下文：未解锁/过期/模式未确认不能解释为有效飞行控制。有效目标仍仅标“遥测目标”，不声称执行。
- 图表状态 `AVAILABLE_WITH_LIMITATIONS` 只表示必需曲线各至少两条有效样本；否则 `INSUFFICIENT_DATA` 并说明缺口。该状态不改变V0.1执行状态、summary或schema，也不是自动根因诊断。
- 会话CRC/错误按processed重新统计，消息数量/接收频率/最大间隔按来源分开；无法归属SYSID的CRC仅会话级展示，错误不阻止绘图。

当前整体读入内存，无抽稀或大规模资源配额；图表缺消息正常降级，结构损坏明确报路径行号而非静默跳过。未实现严格重启分段、时钟漂移修正、跨机对照或飞行模式真值验证。

### 未来整合位置（尚未实现）

- **pyulog（按需可选）**：仅在 MAVLink 证据缺口明确且 ULog 有望补足时，再新增离线 ULog importer，将 uORB 原值、实例、固件版本和源时间保留到独立记录模型，进入显式时间域/重启分段与对照分析。不是每次实验必经步骤；不把 ULog 填成伪 UDP 包，也不假设 MAVLink 与 uORB 严格等价。
- **外部视频时间对齐（按需可选）**：未来保存视频原始时间轴、可核查同步事件、偏移/漂移及误差估计，再与遥测对照；目前不接入视频、不实现自动同步或多源融合。
- **FastAPI**：作为只读会话/摘要查询服务包在存储与分析输出之上，不在采集器内嵌控制路由；优先绑定 localhost，授权/路径访问/敏感日志另做审查。不得通过 HTTP API 绕回飞控发送。
上述ULog、视频与FastAPI组件未加入当前运行管线；引入前分别定义数据兼容、资源边界与独立验证。Plotly离线HTML已由V0.2实现，不属于待实现项。

## 配置和操作边界

capture YAML 恰好包含 `bind_host`、`bind_port`、`duration_seconds`、`drone`、`output_root`。端口为整数 1–65535，时长须正且有限，drone 由存储层限制为字母/数字/下划线/连字符。未提供源 IP 白名单、身份认证或主动流速协商；监听地址应由操作人员明确选择。`drone` 是会话标签，不是已验证 SYSID。

端口被其他地面站占用时应失败，不设置共享接收来抢包。没有收到消息可能只是上游未向该地址发送，不应自动增加心跳或请求来“修复”。已有地面站和飞控的数据流配置必须由用户在本工具之外负责。

## 时间和统计语义

- 接收 `recv_monotonic_ns` 用于同一采集会话的间隔；`recv_utc` 供人类关联。两者不等于传感器采样时刻。
- `time_boot_ms/time_boot_us` 属于 boot 域；SYSTEM_TIME 的 `time_unix_usec` 属于 unix；通用 `time_usec` 为 unknown，不按数值大小猜域。`clock_alignment` 固定为 null。
- 按来源、SYSID、COMPID、消息类型统计频率/间隔；序号按来源、SYSID、COMPID（跨类型）观察，因为 MAVLink seq 不是每消息类型独立计数。
- 多帧在一个 UDP 包中完成时可具有相同接收时间；分片帧归于完成解析的包。这是接收观测，不等于消息生产周期。
- 序号重复/乱序/回绕/重启、过滤与传输丢失可能混淆。summary 的结论必须保持“无法判断故障根因”。

更多字段见 [数据契约](docs/DATA_CONTRACT.md)，验证设计见 [实验规范](docs/EXPERIMENTS.md)。未来自定义 dialect、ULog、严格时钟对齐、实时可视化和诊断规则必须另立范围及测试，不能通过文档描述冒充已实现功能。
