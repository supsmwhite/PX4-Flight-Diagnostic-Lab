# 开发进展

## 当前版本与开发阶段

- V0.1.0，初始化最小闭环已完成；2026-11-04 UTC。
- 独立工程，只读诊断。当前验证层级：合成 MAVLink + 本机 UDP 回环；未验证真实 ESP32、定制 PX4 或实机飞行。

## 已经完成的功能

- Python src 布局、venv、pyproject 依赖和 CLI、Git 初始化与数据忽略规则。
- 实际阅读参考仓库六个指定文件，独立实现，没有改原仓库或运行其控制程序。
- 按来源隔离的 common MAVLink v1/v2 多帧/分片解析，CRC/坏数据/未知ID报告。
- 原始包先保存，UTC+单调时间、来源、Base64、完整解析字段与帧字节；schema 1 JSONL。
- 独占新 Session；采集/离线重放复用管线，重放不联网、不覆盖源数据。
- 接收频率/间隔/心跳、CRC候选错误、序号环绕/缺口/重复/逆序、原始吞吐；不输出伪丢包率。
- 保守时间规范化；time_usec unknown，不假装已对齐或识别重启。
- 四份核心文档、两份专项文档及参考研究笔记；合成样例生成器。

## 实际运行和测试结果

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
- 无真实 UDP/ULog输入，无法验证自定义固件字段、性能或横移根因。
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
- 未实现ULog/时钟分段对齐/实时图表/根因诊断/双机对照/任何控制功能。

## 下一步最重要的三个任务

1. 获授权后开展被动 ESP32 采集：两机分别标记固件/参数、测量速率与CPU/磁盘/内核drop，保存可追溯真实样本。
2. 引入pyulog只读导入和时间域/重启分段验证，核对定制PX4消息定义，建立可审计对齐而不猜测。
3. 做同条件双机光流/测距/EKF2估计与姿态目标对照图，并为每个假设列证据和局限；不自动认定根因。

## 最近相关 Git 提交信息

- 首次公开发布准备：仓库 `supsmwhite/PX4-Flight-Diagnostic-Lab`，分支 `main`，首次提交消息 `chore: initial commit`。最终远端状态须以 GitHub/Git 查询为准。
- 已检查26个待提交文件，无超过50MiB文件；常见敏感信息扫描未发现密钥，README本机绝对路径已替换为通用克隆步骤。
- 原始数据、.venv、本地连接配置、GITHUB_INIT.md、.env及密钥文件已被忽略。提交身份使用本仓库级GitHub noreply邮箱，未改全局身份。
