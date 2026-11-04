# 开发进展

## 当前版本与开发阶段

- V0.2+，实时Web闭环已实现（包版本仍0.2.0）；2026-11-04 UTC。保留V0.1/V0.2命令及schema 1。
- 独立工程，只读诊断。当前验证层级：合成 MAVLink、本机 UDP 回环及用户提供的机2真实地面静止遥测检查；尚未完成实机飞行或定制固件全部字段语义验证。
- **2026-11-04 设计决策：MAVLink-first，ULog-on-demand。** 默认优先使用用户此前已验证的 ESP32 + Wi-Fi + MAVLink 链路开展采集、监控、存储及第一轮诊断；本工程的实时监控现由V0.2+实现，专项诊断仍为后续功能。ULog 不要求每次实验导入，只在遥测证据不足时按需补充，不能成为默认使用前置条件。
- V0.1 范围不扩大：可靠原始记录、重放、时间戳与链路统计优先。正常机/异常机对照是重要后续功能；仅保留 ULog、外部视频时间对齐扩展能力，不提前实现复杂融合。
- 诊断必须报告所需字段、实际有效字段、采样质量、结论可靠性与局限。V0.2已在图表缺失/不足时展示 `INSUFFICIENT_DATA`，说明优先补MAVLink、ULog是否补足需核对话题；未实现自动根因诊断，不与 `completed/interrupted/failed` 执行状态混淆；详见 [架构决策](<ARCHITECTURE.md>)。

## 已经完成的功能

- Python src 布局、venv、pyproject 依赖和 CLI、Git 初始化与数据忽略规则。
- 实际阅读参考仓库六个指定文件，独立实现，没有改原仓库或运行其控制程序。
- 按来源隔离的 common MAVLink v1/v2 多帧/分片解析，CRC/坏数据/未知ID报告。
- 原始包先保存，UTC+单调时间、来源、Base64、完整解析字段与帧字节；schema 1 JSONL。
- 独占新 Session；采集/离线重放复用管线，重放不联网、不覆盖源数据。
- 接收频率/间隔/心跳、CRC候选错误、序号环绕/缺口/重复/逆序、原始吞吐；不输出伪丢包率。
- 保守时间规范化；time_usec unknown，不假装已对齐或识别重启。
- 四份核心文档、两份专项文档及参考研究笔记；合成样例生成器。

## V0.2+ 实时Web实现与软件验收（2026-11-04 UTC）

- 新增live/core.py、server.py、static/index.html、static/app.js和核心/服务测试；复用Parser/Session/Stats/receive_packets，不建第二套协议解析器。telemetry公开field_value，offline/live共享单位/缺测规则。
- process增加可选观察器：先落盘再通知；观察器错误计数不终止记录。UDP增加可选stop回调，兼容旧调用。live有独立非daemon生产线程与WebSocket客户端，固定本机8765（可换端口，不可暴露主机地址）。
- live --config保存新Session，时长结束保留Web末窗；live --replay-session读取原raw定时重放，无UDP/设备发送/写文件。原replay仍完全无网络，live回放仅本机Web socket。
- 后台20000点/64来源/512流、60秒双时间窗、每客户端2000点，前端128trace/12000点；显示丢弃与client_missed独立可见，不解释为真实UDP丢包率。整体/包链路/HB/消息流分别新鲜度，终态保留数据。
- 新依赖实际安装FastAPI0.162.0、Uvicorn0.54.0、websockets15.0.1，测试httpx0.28.1。最终完整回归150 passed,1 warning in3.09s；pip check通过、compileall/node --check/git diff --check退出0。warning来自Starlette测试客户端提示httpx未来弃用，不影响当前验证；未屏蔽。
- 测试覆盖合成分发/容量/慢游标/并发快照/单位/来源隔离/心跳上下文/状态转换/定时重放无socket/源不变/WS断开不取消生产/Origin及Host限制/观察器失败归档/本机UDP合成live Session可analyze。未进行长期磁盘/CPU压测或真实飞控控制。
- 真实历史验收：`env -u PYTHONPATH .venv/bin/python -m px4_flight_diag live --replay-session data/drone2/20261104T030153.373745Z-785100e2 --web-port 8765 --speed 0.5 --start-delay 15`。Chrome CDP实际读取window.__liveDebug：24秒内updates6→117、图样本2252→5832，五组均存在并更新；不是stub截图代替验证。
- 完成WS实测COMPLETED、messages5249、CRC108、candidate_missing1717、display_dropped0。关闭服务后Chrome实测DISCONNECTED、connected=false、6283个含空值曲线样本保留；服务日志正常application shutdown。验收Web与Chrome已关闭，无残留监听。
- 最终真实Session raw/processed/metadata/summary四文件SHA256均与V0.2验收记录一致；容量为2点的100条合成姿态归档测试确认显示丢弃98、client_missed98，但100条原始/解析记录全保留且analyze成功。
- **本轮未连接真实ESP32设备；历史真实日志回放不等于真实在线地面/飞行验收。** 用户下一步在无桨、上锁条件自行授权现场验收。本轮未提交/推送Git。

## V0.2 实现与实际验收（2026-11-04 UTC）

- 新增 [telemetry.py](<src/px4_flight_diag/analysis/telemetry.py>)：严格Session读取、来源与传感器隔离、有限值/掩码/四元数校验、单位换算、时间轴与缺测断线。
- 新增 [report.py](<src/px4_flight_diag/analysis/report.py>)：每来源五组Plotly图、消息质量表、目标状态上下文、内嵌JS独立HTML、独占版本文件；新增analyze CLI，不读raw、不连接飞控。
- 版本0.2.0，新增Plotly >=5.24,<7（实际安装6.9.0）；更新README/架构/数据契约。新增telemetry/report测试并更新版本测试。
- 最终执行：`env -u PYTHONPATH .venv/bin/python -m pytest -q` → **113 passed in 1.98s**；含原26项回归、字段/单位/四元数/掩码/质量/来源/传感器/坏输入/时间/断线、HTML缺图降级、无socket、重复生成不覆盖、CLI集成。
- `env -u PYTHONPATH .venv/bin/python -m pip check` → No broken requirements found；compileall与git diff --check退出0。
- 真实机2离线验收命令：`env -u PYTHONPATH .venv/bin/python -m px4_flight_diag analyze --session data/drone2/20261104T030153.373745Z-785100e2`；初次输出diagnostic.html，再次输出 [最终报告](<data/drone2/20261104T030153.373745Z-785100e2/reports/diagnostic-001.html>)，均成功，已有报告保留。
- 初次生成前后raw/processed/metadata/summary SHA256全部相同；原始raw仍为 `5cba976f33dbaa70ce7aaee1db920cf8faa8f43c49ac32f92805636049654d62`。无真实设备连接/指令、无代码Git提交或推送。
- Chrome headless对本地file URL实际执行Plotly，DOM中5个已初始化图、15个main-svg、零外部script；不依赖CDN。尚未由人员手动逐项操作缩放拖动；合成与浏览器验证不等于飞行验证。
- 开发中一次全套失败源于旧版本测试仍断言0.1.0，已更新0.2.0后全套通过；四元数欧拉奇异点测试调整为不对不唯一roll做固定期望。

## 实际运行和测试结果（V0.1历史）

2026-11-04机2静态数据检查：用户说明平放地面未移动；会话20261104T030153.373745Z-785100e2，19.966秒、887包、227072字节、5249条有效消息、22类，目标11类全部收到。对原始JSONL进行内存重放，统计与已有摘要全部一致，尾部pending=26字节；原始记录未改动、未发送任何飞控指令。水平估计首末变化约5.48mm，测距恒4cm；不足以判断起飞故障（人工分析状态 `INSUFFICIENT_DATA`，未新增程序状态）。报告含字段有效性、链路质量、单位疑点和下一步实验建议，见 [机2静态检查报告](<data/drone2/20261104T030153.373745Z-785100e2/reports/static_review.md>)（本地数据目录，默认不提交Git）。本次无代码变更，未重跑pytest。

本次设计决策更新仅修改架构和进展文档，未修改代码、依赖或数据格式；未重跑自动测试。以下为此前实际运行结果，不代表新增诊断能力已验证。

环境：Ubuntu，Python 3.10.12；PyMAVLink 2.4.49、PyYAML 6.0.3、pytest 8.4.2。

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q
# 26 passed in 1.22s
env -u PYTHONPATH .venv/bin/python -m pip check
# No broken requirements found.
env -u PYTHONPATH .venv/bin/python -m compileall -q src tests scripts
# exit 0
git diff --check
# exit 0（初始文件尚未提交，此检查不替代测试）
```

测试覆盖 v1/v2、多帧、分片、CRC恢复、未知ID、来源隔离、时间、频率、序号、schema校验、截断/倒退、独占Session、源文件不变、无网络重放、本机回环实际收取3包并重放、capture CLI无数据正常退出、入口版本。

实际 CLI 演示：

```bash
env -u PYTHONPATH .venv/bin/python scripts/generate_demo.py --output data/demo/raw/synthetic.jsonl
env -u PYTHONPATH .venv/bin/python -m px4_flight_diag replay --input data/demo/raw/synthetic.jsonl --drone demo-replay
env -u PYTHONPATH .venv/bin/px4-flight-diag --help
```

全部退出0；合成重放得到5包、105字节、5条HEARTBEAT、1Hz、最大间隔1秒、0 CRC错误、0 pending字节。报告位于 [合成重放摘要](<data/demo-replay/20261104T020749.674971Z-2f3b8638/reports/summary.json>)。不是实机数据。

## 当前发现的问题

- 当前 shell 的 PYTHONPATH 带 ROS 包，导致普通 pip check 报外部 ROS依赖缺失；清除该变量后本项目检查通过。README已说明，不修改ROS。
- 已有机2真实地面静止UDP输入：11类目标消息齐全，但CRC候选错误108次、心跳最大间隔5.017秒，位置/速度目标被mask忽略且为NaN；仍缺起飞异常阶段、正常机对照及光流融合细节，无法判断横移根因。无真实ULog输入，按需而非必需。
- 全部最终自动测试通过，没有未修复测试失败。

## 已尝试但无效的方案

- 最初在 src 目录尚未创建时执行 editable 安装失败（egg_base不存在）；建立包目录后重新安装成功。
- 直接在继承ROS的PYTHONPATH下检查隔离环境会混入外部包；改用 env -u PYTHONPATH 解决。

## 未解决的技术风险

- 仅 common dialect，未知ID不验证CRC且不计作有效消息；签名未认证。
- 分片只按peer流重组；UDP乱序/丢包、peer变更、损坏长度可能导致待解析残片；无分片超时重置。
- 同步flush写盘可能降低吞吐，只有正常关闭fsync；未测内核丢包、断电可靠性或长期负载，peer/stream无全局配额。
- 频率是累计首末接收窗口而非采样频率；吞吐不含首包前末包后静默。
- seq跳变无法区分重启、过滤、乱序、网络丢失，不是丢包率。
- V0.2离线图表及V0.2+实时图表已实现；未实现ULog/严格时钟分段对齐/根因诊断/双机对照/任何控制功能。
- 可视化按内存加载，未做超长日志抽稀/规模资源限制；默认0.5秒断线阈值是显示策略而非传感器健康标准。auto不同图可能不同时间域，应使用receive显式统一轴，仍不能毫秒级因果判断。

## 下一步最重要的三个任务

1. 获授权后开展被动 ESP32 采集：两机分别标记固件/参数、测量速率与CPU/磁盘/内核drop，保存可追溯真实样本。
2. 针对真实CRC/心跳缺口核对链路与定制PX4字段单位；补充显式重启分段、有效控制模式语义与更细数据缺失原因；不把ULog导入设为前置任务。
3. 基于已实现单Session图表，开展同条件正常机/异常机对照及较长日志性能验证，列明证据和局限；仅在具体缺口需要时引入可选ULog/视频对齐，不自动认定根因。

## 最近相关 Git 提交信息

- 2026-11-04 已创建并首次推送公开仓库：https://github.com/supsmwhite/PX4-Flight-Diagnostic-Lab ，分支 `main`，首次提交 `773471a`（`chore: initial commit`）；origin 已绑定。
- 发布前重新运行全套测试：26 passed in 1.22s；暂存文件 `git diff --cached --check` 通过。
- 已检查26个待提交文件，无超过50MiB文件；常见敏感信息扫描未发现密钥，README本机绝对路径已替换为通用克隆步骤。
- 原始数据、.venv、本地连接配置、GITHUB_INIT.md、.env及密钥文件已被忽略。提交身份使用本仓库级GitHub noreply邮箱，未改全局身份。
