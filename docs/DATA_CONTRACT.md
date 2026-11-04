# 数据契约（schema_version = 1）

本文描述当前被动 UDP 采集/重放的数据结构与解释限制。实现入口为 [session.py](../src/px4_flight_diag/storage/session.py)、[parser.py](../src/px4_flight_diag/communication/parser.py)、[normalize.py](../src/px4_flight_diag/timebase/normalize.py) 和 [stats.py](../src/px4_flight_diag/analysis/stats.py)。**已读取用户提供的机2真实地面静止遥测，未进行实机飞行验证；估计值、设定值和真值必须分开。**

## 1. 会话与原始记录

每次运行新建独立会话，包含 metadata、raw、processed、plots、reports；plots 当前为预留目录。metadata 含 schema、工具 version、mode，以及 capture config 或 replay input 来源。原始输入不可改写，重放复制包记录到新会话，不覆盖源。

raw JSONL 每行一个对象：

```json
{"schema_version":1,"kind":"udp_packet","packet_index":0,"recv_utc":"2026-11-04T02:00:00+00:00","recv_monotonic_ns":1234567890,"peer":["127.0.0.1",14551],"size_bytes":3,"data_b64":"AQID"}
```

这是仅演示结构的三个字节，不是有效 MAVLink 帧。

| 字段 | 类型/含义 |
| --- | --- |
| schema_version | 当前为整数 1 |
| kind | `udp_packet` |
| packet_index | 会话内包序号；capture 从 0 开始；重放要求整数且严格增加，不要求连续 |
| recv_utc | 带时区 ISO 8601 接收墙钟；capture 使用 UTC |
| recv_monotonic_ns | 非负整数，主机 monotonic 纳秒；重放要求非递减；不能跨启动/主机直接比较 |
| peer | `[来源IP字符串, 来源UDP端口整数]`；不证明来源真实性 |
| size_bytes | 原始 UDP payload 字节数，必须与解码后的 Base64 长度一致 |
| data_b64 | 原始 datagram 的严格 Base64 编码，保留坏帧、噪声和分片 |

`read_packets` 对每行进行校验，损坏/截断/非法结构报源路径及行号；不静默跳过。空行也不是合法包。raw 不等于抓包文件：它不包含以太网/IP/UDP 头，也无法记录在内核或网络中已经丢失的包。

## 2. 解析记录

processed JSONL 每行一个 `message` 或 `parse_error`，由存储层补充 `schema_version: 1`。

共同字段：`packet_index`、`recv_utc`、`recv_monotonic_ns`、`peer`、`kind`、`sysid`、`compid`、`msg_type`、`seq`、`fields`、`frame_hex`、`time`。

- `message`：sysid/compid/seq 为协议头数值，msg_type 为消息名；fields 保留解码字段，不隐式转换为 SI 单位或其它坐标系；frame_hex 保存帧字节十六进制。
- `parse_error`：身份及类型/seq 为 null，fields 为空对象，另有 `reason`、`error_type`。当前类别包括 `invalid_base64`、`bad_data`、`crc`、`unknown_message`；未知消息没有可验证的 CRC_EXTRA，不计入有效 message。错误候选/丢弃片段是观测，不保证每条错误对应一个物理损坏帧，不能把错误数当丢包数。
- 跨 datagram 的帧归属完成解析的包；每个 peer 有独立状态，不能把两个来源半帧拼接。会话结束未完成的尾部以 summary `pending_bytes` 表示，不伪造完整消息。
- 默认 dialect 为 pymavlink MAVLink v2 common，同时解析 v1 帧。未知消息、自定义 dialect 不宣称可用；签名尾部可被识别/解码，**签名未认证**。
- JSON 不允许裸 NaN/Infinity。存储层将非有限浮点写成 `"nan"`、`"inf"`、`"-inf"` 字符串；它们不是有效数值，分析必须排除。数组保留数组；解析字段字节通常转为列表，通用序列化器还有 `{bytes_hex: ...}` 保底表示。缺失与协议 sentinel 不应擅自变为 0。

## 3. 时间字段

```json
{"sources":[{"field":"time_boot_ms","value":1234,"unit":"ms","domain":"boot","seconds":1.234,"utc":null}],"clock_alignment":null}
```

每个 source 的字段固定为 `field/value/unit/domain/seconds/utc`；原值 value 保留。无消息时间时 sources 为 `[]`。

| 来源字段 | 域 | 处理 |
| --- | --- | --- |
| time_boot_ms | boot | 毫秒转秒；UTC 为 null |
| time_boot_us | boot | 微秒转秒；UTC 为 null |
| SYSTEM_TIME.time_unix_usec | unix | 微秒转秒；正值且可转换时给 UTC，否则 null |
| time_usec | unknown | 只做微秒转秒；不根据数量级推定 epoch/boot；UTC 为 null |

SYSTEM_TIME 可同时有 unix 与 boot 两个 sources，仍不自动建立同步关系。0、回绕、设备重启和异步采样需要后续专项解释。`clock_alignment=null` 表示未对齐，不是零偏移。接收 UTC、接收 monotonic 与消息源时间分别保存，不能互换。

## 4. 十一类重点消息：关键单位与坐标

依据 MAVLink common 的字段语义整理；实际流中是否发送、更新频率和具体固件内容均待现场确认。官方字段参考：[MAVLink common](https://mavlink.io/en/messages/common.html)。下表只是关键字段，原始 fields 可以包含更多标准或扩展字段，未做字段白名单裁剪。

| 消息 | 关键字段与单位 | 坐标/解释注意 |
| --- | --- | --- |
| HEARTBEAT | type、autopilot、base_mode、custom_mode、system_status 为枚举/位域 | 不含位置或源时间；PX4 custom_mode 需要 PX4 专用解释，不能等同其他栈；心跳不代表全部传感器健康 |
| ATTITUDE | time_boot_ms；roll/pitch/yaw 为 rad；rollspeed/pitchspeed/yawspeed 为 rad/s | 航空右手系姿态，通常描述机体系相对 NED；角度不是度；欧拉角有奇异性 |
| ATTITUDE_TARGET | time_boot_ms；type_mask；q `[w,x,y,z]` 无量纲；body_roll/pitch/yaw_rate 为 rad/s；thrust 归一化 | 目标而非测量；按 type_mask 判断有效字段；姿态四元数与机体角速率不能混为欧拉角 |
| LOCAL_POSITION_NED | time_boot_ms；x/y/z 为 m，vx/vy/vz 为 m/s | 局部 North/East/Down，z 向下为正；原点为估计参考，不是固定地理真值，也不是直接离地高度 |
| POSITION_TARGET_LOCAL_NED | time_boot_ms；coordinate_frame、type_mask；x/y/z m；vx/vy/vz m/s；afx/afy/afz m/s²（force 位指定时为 N）；yaw rad、yaw_rate rad/s | 位置目标遥测，不是 SET_POSITION_TARGET_LOCAL_NED 控制发送；必须同时解释 frame 与 mask，区分 LOCAL/BODY/OFFSET 语义，不把被忽略字段画成有效目标 |
| OPTICAL_FLOW_RAD | time_usec；integration_time_us μs；integrated_x/y rad；integrated_xgyro/ygyro/zgyro rad；distance m；quality 0–255；temperature cdegC | 积分角量不是瞬时角速度，求速率需除以真实积分时长；传感器轴向/安装方向须额外确认；distance 无效值依协议处理，不能直接当高度真值 |
| DISTANCE_SENSOR | time_boot_ms；min_distance/max_distance/current_distance cm；covariance cm²；orientation 枚举；id/type | 按安装朝向/传感器类型解释，距离沿测量方向而非固定世界 z；扩展 FOV rad、quaternion `[w,x,y,z]`、signal_quality 有各自有效性语义 |
| ESTIMATOR_STATUS | time_usec；flags；vel_ratio、pos_horiz_ratio、pos_vert_ratio、mag_ratio、hagl_ratio、tas_ratio 无量纲；pos_horiz_accuracy/pos_vert_accuracy m | ratios 是检验比值而非原始 innovation；flag 与误差估计不能作为物理真值或根因证明 |
| HIGHRES_IMU | time_usec；xacc/yacc/zacc m/s²；xgyro/ygyro/zgyro rad/s；xmag/ymag/zmag gauss；abs_pressure/diff_pressure hPa；pressure_alt m；temperature °C；fields_updated 位域 | 加速度/陀螺/磁场按机体坐标语义；并非所有字段每帧都更新；pressure_alt 不是测距高度或 local NED z |
| RC_CHANNELS | time_boot_ms；chancount；chan1_raw…chan18_raw μs；rssi（0–254，255未知） | 通道值需结合遥控映射/校准/反向/死区；UINT16_MAX 为无效通道值；不能单凭某通道推断飞手意图或接管成功 |
| SYS_STATUS | onboard_control_sensors_present/enabled/health 位域；load 为 0.1%；voltage_battery mV；current_battery cA；battery_remaining %；drop_rate_comm 为 0.01% | 电流 -1、余量 -1 等 sentinel 表示未知；机载 drop_rate_comm 不是本地 UDP 测得丢包率；位域健康仅是机载报告 |

注意：DISTANCE_SENSOR 的 cm 与 OPTICAL_FLOW_RAD 的 m 不能混算；ATTITUDE 的 rad 与界面显示度不能混算；LOCAL_POSITION_NED 的 down 正与“高度向上正”必须显式转换且保留来源。

## V0.2 可视化派生契约（不变更schema 1）

输入仍是原始processed/messages.jsonl，保留原fields/time/recv_utc/recv_monotonic_ns。加载校验逐行JSON/schema/来源/结构，全文件接收单调时间与packet_index非递减（同包相等允许）。损坏记录报路径行号，不静默跳过；metadata和summary缺失警告但可绘图。传感器id/orientation/sensor_id存在时须uint8，缺失作为未知独立流。

- `finite`只接受有限数值，不接受bool、数字字符串或nan/inf字符串。缺失/无效绘图值为None，不回写processed。
- 实际roll/pitch rad→degree。目标q须4个有限数、范数在[0.9,1.1]内归一化；零/异常范数无效。type_mask缺失或bit128置位时目标姿态缺失，不填零。欧拉奇异点不用于精确跟踪判断。
- LOCAL_POSITION_NED x/y和vx/vy直接使用，NED North/East，m和m/s，明确EKF2估计不是真值。
- OPTICAL_FLOW_RAD积分量保持rad，不换成速度；要求quality在1..255且integration_time_us>0。quality图独立于积分窗口，0正常显示为零质量；缺失/越界质量显示缺失。integration_time_us保留µs。
- DISTANCE_SENSOR min/max/current由cm→m，量程须0≤min≤max<65535，current必须在有效量程内且非65535。量程外current显示缺失，范围参考仍可绘图；不解释v1未传扩展字段的默认值为实测值。
- series返回x/y、valid_count、invalid_count、gap_count、connectgaps=False；>gap_seconds（默认0.5秒）和时间倒退插入None断线。每条无效字段本身也是断点；不补采样、不插值。
- auto轴每图验证显式time_boot_ms完整且每类不倒退，使用同来源最早boot字段为原点；否则用同来源首条消息recv_monotonic_ns为原点。未知time_usec不自动提升为boot。receive选项全图使用接收轴；所有接收轴包含传输延迟。不承诺严格同步或重启识别。
- HEARTBEAT上下文只向后匹配2秒，过期/缺失为未知。未解锁目标可显示但标签为“遥测目标，不等于已执行”，上下文统计不构成模式或控制有效性证明。
- HTML每来源五组图，状态与执行状态分离。AVAILABLE_WITH_LIMITATIONS要求各必要曲线至少2条有效样本；否则INSUFFICIENT_DATA。不是医学式置信度、故障分类或飞行许可。
- `reports/diagnostic.html`首次独占创建；后续为diagnostic-001.html等。JS内嵌，无远程脚本，不修改summary/raw/processed。HTML包含元数据和来源信息，分享前需检查隐私；日志和报告默认忽略Git。

## V0.2+ WebSocket协议 version=1（独立于存档schema）

只读/ws每0.2秒推一个JSON快照，不接受飞控指令。HTTP端没有任意Session文件路径接口。字段：

- `version:1,session,mode`：稳定Session路径字符串及live/replay；切换session前端清空去重与曲线。
- `state`：WAITING_FOR_DATA（无包）、RECEIVING（近期有效消息）、STALE（有效消息超过stale_seconds默认2秒或仅坏包）、DISCONNECTED（任意包超过disconnect_seconds默认5秒）、COMPLETED、ERROR（终态优先）。阈值可通过LiveBuffer构造参数配置，CLI默认固定；须有限正数且disconnect>=stale。
- `link_state`独立依据任意包年龄，不受完成覆盖。`last_data_age_s`为任意包年龄，`last_message_age_s`为有效消息年龄，均运行monotonic，未知null。
- `messages,crc_errors,parse_errors,sequence_candidate_missing`累计计数，不是实测UDP丢包率。`sources`按[IP,port,SYSID,COMPID]隔离，含heartbeat_age_s/heartbeat_state及streams（msg_type/count/frequency_hz/max_gap_s/age_s/state）；频率仍原接收时间累计(N-1)/跨度，非滑动频率。
- `points:[{id,t,identity,msg_type,sensor,values,target_context}]`仅五组图所需类型。id会话递增，t相对首包接收单调秒，sensor光流为ID、测距为[ID,方向]。values复用field_value（姿态度、测距m、光流rad/µs/quality），非有限/掩码等None编码null；不更改原fields。目标上下文为此前同来源2秒证据时间内的HB，枚举UNKNOWN_OR_STALE/UNKNOWN/DISARMED/ARMED_PX4_MODE_UNCONFIRMED_EXECUTION/MODE_UNCONFIRMED，均不证明目标已执行。
- `cursor`当前全局点编号，`client_missed`本次游标到当前之间不可用/批次跳过点数（包含正常窗口过期）；`display_dropped`容量/来源限额显示拒绝计数（不含正常窗口到期）；`conversion_errors,stats_limited_records,warnings`明确降级。`finished,error`生产者终态。
- 硬边界：20000点、64来源、512流、单快照2000点，最近60秒证据/执行窗口；完成冻结末窗。前端最多128trace/每trace12000点，WebSocket最多4客户端，慢发送1秒超时，客户端可重连取得最近批次，不保证恢复完整历史。完整历史应读取存档。
- live采集metadata保持mode=capture并增加live:true；summary仅live模式新增live_observer_errors，原schema与旧命令不变。观察器错误不取消存档，结束后界面ERROR。时长结束COMPLETED保留服务；Ctrl+C保存interrupted摘要后关闭。发生写盘错误立即ERROR，不宣称完整归档。
- live回放需要原raw/udp.jsonl，复用原Parser，无UDP监听/设备发送/文件修改；仅Web服务本机网络。speed改变到达调度而非原时间/频率/目标上下文，age采用播放运行时间，慢放STALE不等于原链路故障。

## 5. 未来 uORB 对照：候选映射，不是严格等价

目前**没有 ULog 读取器、uORB 映射转换器或严格时间对齐**。以下只是未来跨层研究索引；话题名称/结构依 PX4 版本变化，需固定固件提交、消息定义、实例号和发布链路后验证。不能由某条 MAVLink 消息反推全部 uORB 字段。

| MAVLink | 可能相关的 PX4 uORB 话题 | 非等价原因 |
| --- | --- | --- |
| HEARTBEAT | vehicle_status、vehicle_control_mode | 枚举/模式压缩，不包含完整内部状态 |
| ATTITUDE | vehicle_attitude、vehicle_angular_velocity | 四元数转欧拉、独立采样时刻和不同发布频率 |
| ATTITUDE_TARGET | vehicle_attitude_setpoint、vehicle_rates_setpoint | 目标组合、掩码与控制层级不同 |
| LOCAL_POSITION_NED | vehicle_local_position | 缺少完整 reset counter、有效性、参考点与协方差信息 |
| POSITION_TARGET_LOCAL_NED | trajectory_setpoint、vehicle_local_position_setpoint | 具体发送流选择、控制模式和坐标转换依版本 |
| OPTICAL_FLOW_RAD | sensor_optical_flow、vehicle_optical_flow | 传感器原始量、补偿后量、积分窗口和质量定义需核对 |
| DISTANCE_SENSOR | distance_sensor | 实例/方向、有效性和时间戳需匹配 |
| ESTIMATOR_STATUS | estimator_status、estimator_status_flags、estimator_innovations、estimator_innovation_test_ratios | MAVLink 聚合子集，不提供完整 innovation/选择实例 |
| HIGHRES_IMU | sensor_combined、vehicle_imu、sensor_mag、sensor_baro、differential_pressure | 多传感器合成、校准、滤波和更新掩码不等价 |
| RC_CHANNELS | input_rc、rc_channels | 原始 PWM 与归一化通道、接收链路状态不同 |
| SYS_STATUS | vehicle_status、vehicle_status_flags、battery_status、cpuload | 多来源汇总，健康位/电池聚合依固件实现 |

## 6. 统计契约及限制

summary 包括 packets/raw_bytes/records/messages、parse_errors/crc_errors/error_counts、message_counts、packet_span_s、raw_bytes_per_second、streams、sequence，以及编排层 status/error/pending_bytes/session/diagnosis。

- 每个 stream 按 `(peer,sysid,compid,msg_type)` 区分，含 count、first/last monotonic、span、frequency_hz、interval count/mean/min/max、negative_intervals 和心跳最大间隔。频率以 `(count-1)/(最后-首次接收时间)` 计算，无法计算时为 null，不是配置发送频率。
- sequence 按 `(peer,sysid,compid)` 汇总所有消息类型。mod 256 前向距离 1–127 被视为向前，0 视为重复，128–255 记乱序；跨 255→0 记录回绕。缺口是 `candidate_missing`，不提供确证丢包率。
- 仅一个包/消息或相同时间戳时可能无法算频率。心跳最大相邻间隔不覆盖最后心跳后无限延续的静默，应结合采集范围解释。
- 接收时间含网络缓冲、UDP 聚合和主机调度影响；消息间隔抖动不能直接归因于传感器、估计器或飞控负载。
- 当前结论固定表达“无法判断故障根因；V0.1 仅统计通信观测”。加入诊断规则前必须定义证据需求、反例、置信边界和版本。
