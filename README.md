# 蜀云智造智慧工厂实训

平台接收 ESP32 或模拟终端的环境数据，提供实时监控、智能问数、安全控制和 Word 报告。运营端维护产量与库存、处置建议、人工接办、工单回访和通行核验。演示模式包含车间、仓库和安全通道三台模拟设备，使用与真机相同的设备协议。

## 在本机运行

当前目录已有 Python 虚拟环境时，双击 `start-demo.cmd`，打开 [本机监控台](http://127.0.0.1:8000/)，点击 **以操作员进入模拟演示**。后端和模拟设备会一起启动。停止时在运行窗口按 `Ctrl+C`。

在没有现成虚拟环境的电脑上，先安装 Python 3.12 或更新版本。以下命令在项目目录内安装依赖并启动演示。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py --demo
```

网页使用随项目提供的 ECharts 5.6.0，无需 npm 构建。默认 HTTP 和 WebSocket 使用 `8000`，设备 TCP 使用 `9000`。端口被占用时可加 `--port 8080 --device-port 9001`。模拟模式仅监听回环地址，数据写入 `.data/demo`。

| 演示账户 | 密码 | 权限 |
| --- | --- | --- |
| `operator` | `shuyun-demo` | 看板、台账、问数、建议采纳、人工接办、通行核验、经确认的控制和报告 |
| `viewer` | `shuyun-demo-viewer` | 看板、只读问数和报告 |
| `admin` | `shuyun-demo-admin` | 操作员权限，以及阈值、审计、权限矩阵、模型和工具测试 |

## 软件验收

按项目 PPT 标注的第 38、42 页整理了下列演示流程。各项对应课程 P0 或 P1 的正式分级以验收表为准。

| 验收项 | 演示方式 |
| --- | --- |
| 采集和可视化 | 在**实时监控**切换三台模拟终端，观察读数、趋势、连接状态与采样时间 |
| 越限和工单 | 在**设备与控制**触发模拟温度越限，从实时监控查看告警。恢复正常后在**告警工单**填写处置说明并关闭 |
| 实时问数 | 问“现在多少度，超温没”，将答案的采样时间和读数与看板对照。可切换设备管家、安全卫士和能耗分析师 |
| 产量与库存 | 在**业务台账**选择日期并修改装配线产量，再问“今天装配线跑了多少件”。在**管理大屏**对照同日业务汇总。库存按对应日期的盘点快照登记 |
| 人工确认与回执 | 问“把补光灯调到 40%”，检查预览。按**人工确认并下发**后，在设备页确认补光灯回读为 40% |
| 建议采纳 | 模拟照度不足，在**运营工作台**采纳补光建议。系统按最新采样生成指令预览，再经人工确认、联锁和回执 |
| 转人工及回访 | 问数后点击**转人工**，在运营工作台处理关联会话。告警工单关闭后可再次登记回访结果 |
| 通行核验 | 在运营工作台签发一次性通行码，核验后人工确认。演示闸机将检查通道状态，放行三秒后自动关闭。可在**记录与报告**查询结果 |
| 安全联锁 | 在越限、遮挡或设备离线时确认启动继电器，检查结果和拒绝原因。模拟双继电器时两路互斥 |
| 断线恢复 | 触发**模拟断线重连**，观察离线、拒绝控制、恢复在线及事件记录 |
| 留痕和报告 | 在**记录与报告**检索通行、会话和控制指令，下载巡检及能耗 Word 报告 |
| 管理大屏 | 查看脱敏后的在线数量、当日告警与分时能耗估算。也可登录后打开 `/screen` |
| 模型与工具 | 管理员可试跑问数、按输入费率估算模型调用成本，测试只读 HTTP / MCP 工具，查看 17 个权限点并维护岗位提示与常用问法 |

以下命令可重复运行软件验收。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时 SQLite 数据库及本机 HTTP、WebSocket、TCP 和 MCP。Agentica 比较测试调用本机的模型接口模拟器。通行与控制均可通过模拟终端验收。

能耗按执行器回报和采样间隔估算。人员出现次数取 PIR 从无人到有人的跳变，核验通行次数取已确认的通行记录。演示模式使用标为“实训演示台账”的业务样例。真实模式按实际班报与盘点记录登记，台账保留来源、时间和修改留痕。

## 接入实际 ESP32

ESP32 的 Flash 先装 MicroPython 固件。板上已有兼容的 MicroPython 时，可以直接通过 Thonny 将项目的 `main.py`、`hardware.py` 和 `config.py` 保存到开发板根目录。更新这些 Python 文件时，Thonny 可直接覆盖板上的文件。后台、网页、Agentica 及 SQLite 由电脑运行。

1. 将 `.env.example` 复制为 `.env`，给三个账号设置至少 12 字符的不同密码。将 `devices.example.json` 复制为 `devices.json`，按实际设备填写 ID、场景和允许的执行器。每台设备使用单独的随机令牌，至少 20 字符。
2. 将 `firmware/config.example.py` 复制为 `firmware/config.py`，填写 Wi-Fi、服务器电脑的局域网 IPv4、端口、设备 ID 和相同的随机令牌。逐一对照套件的 GPIO、传感器型号及高低电平。设备声明的执行器须在 `devices.json` 中允许。
3. 参考下表检查板端接线。示例以 ESP32-WROOM-32 为目标。已有套件按原理图调整配置。未使用的模块引脚设为 `None`。模块与开发板共地，连接到 ESP32 的信号使用 3.3 V 逻辑。
4. 用 Thonny 烧录 ESP32 的 MicroPython 固件，将 `firmware/main.py`、`firmware/hardware.py` 和填写后的 `firmware/config.py` 保存到开发板根目录。以下命令会启动真机接入服务。

   ```powershell
   .\.venv\Scripts\python.exe run.py --real --device-host 0.0.0.0
   ```

   真机模式的数据库使用 `.data/factory`。网页默认仅在当前电脑可访问，板端须能经可信局域网访问电脑的 TCP 9000。此 TCP 链路按实训规定使用 NDJSON，设备令牌随握手发送。
5. 先检查 Thonny 串口与 OLED 上的读数，然后确认页面显示设备在线。遮住光敏模块，核对本地和网页上报。用同一设备预览并确认 LED 指令，观察实物和回执。断开板端网络验证 OLED 进入离线状态且仍显示本地采样，重新联通后检查设备自动恢复。

| 外设 | 示例 GPIO | 配置说明 |
| --- | --- | --- |
| DHT11 | 27 | 数字信号 |
| 光敏模拟输出 | 34 | 经典 ESP32 的 ADC1，接入电压须符合板端规格 |
| PIR | 32 | 默认高电平表示有人 |
| 红外检测 | 33 | 默认低电平表示遮挡 |
| SSD1306 OLED | SDA 21 / SCL 22 | 默认地址 `0x3C`，固件按 128×64 驱动 |
| 补光 LED | 25 | PWM 输出 |
| 蜂鸣器 | 26 | 按模块选择有效电平，手动打开最多 3 秒 |
| 告警红灯 | 默认禁用 | 配置单独 GPIO 后显示越限状态 |
| 继电器 1 和 2 | 默认禁用 | 核实有效电平及安全负载后再配置 |

按 PPT 要求，继电器仅接套件低压负载或空接验证。启动继电器时后台检查采样新鲜度、人员、遮挡、温度和另一路状态。板端也会重新检查，并在联锁条件变坏或网络失联时关闭继电器。告警红灯和短蜂鸣由板端根据阈值驱动，断网后沿用已知阈值。本次软件验收使用模拟终端，实际电平和动作需接板实测。

操作参考 [ESP32 MicroPython 入门](https://docs.micropython.org/en/latest/esp32/tutorial/intro.html) 和 [Thonny 的 MicroPython 使用说明](https://github.com/thonny/thonny/wiki/MicroPython)。

## 终端工作台

启动服务后，可直接从终端问数或查询记录。

```powershell
.\.venv\Scripts\python.exe -m factory.cli --demo status
.\.venv\Scripts\python.exe -m factory.cli --demo ask sim-shop-01 "今天装配线跑了多少件"
.\.venv\Scripts\python.exe -m factory.cli --demo records passes
```

真实模式运行时使用 `--user operator` 并在终端输入后台密码。控制指令先用问数生成预览，核对预览后可使用 `confirm <指令ID>` 手工确认。

## 只读 HTTP 与 MCP 工具

“模型与工具”页可使用管理员权限测试已登记的设备读数、台账汇总和能耗估算。内网程序可通过 `POST /api/skills/run` 或 Streamable HTTP 的 `/toolkit/mcp` 调用。若需要服务端调用，在 `.env` 填写 `FACTORY_SKILL_TOKEN`，通过 `Authorization: Bearer` 发送令牌。

在 `.env` 配置 `FACTORY_REMOTE_MCP_URL` 和服务令牌后，工具页会显示**内网 MCP**选项。支持 `read_factory_data`、`read_business_summary` 和 `read_energy_summary` 三种固定只读工具。真实通行模式保存人工核验记录，实物闸机和传感器需使用套件联调。

传输参考 [Model Context Protocol](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

## 启用 Agentica 模型

默认使用本地数据工具回答确定性问题。通过 `.env` 的 `FACTORY_MODEL_ENABLED=1` 可以启用 Agentica 1.6.3。对厂区应用，优先填写内网的 OpenAI 兼容模型接口。使用校方授权的百炼接口处理实训数据时，填写 `FACTORY_MODEL_API_KEY`、`FACTORY_MODEL_ID` 和 `FACTORY_MODEL_BASE_URL`，并显式启用 `FACTORY_ALLOW_EXTERNAL_MODEL=1`。

问数结果的数值来自与看板相同的 SQLite 和设备状态。模型只调用读数工具，并可返回受约束的处置建议。设备控制由平台生成预览，经登录用户确认后由后端校验和下发。模型请求失败时会标记降级并返回本地数据。

接口参考 [Agentica 自定义工具](https://shibing624.github.io/agentica/concepts/tools/) 和 [百炼 OpenAI 兼容接入](https://help.aliyun.com/en/model-studio/model-calling-in-sub-workspace)。

## 源码位置

| 位置 | 内容 |
| --- | --- |
| `factory/app.py` | FastAPI、登录权限、REST、WebSocket 和部署生命周期 |
| `factory/devices.py` / `factory/protocol.py` | 设备协议、连接、控制联锁与回执 |
| `factory/store.py` / `factory/reports.py` | SQLite、告警工单、留痕、能耗口径与 Word 导出 |
| `factory/assistant.py` | 读数、自然语言指令预览和 Agentica 接入 |
| `factory/operations.py` / `factory/permissions.py` | 业务台账、建议、人工工单、通行及角色权限 |
| `factory/skills.py` / `factory/cli.py` | 只读工具、MCP 连接和终端工作台 |
| `factory/simulator.py` | TCP 模拟设备及验收场景 |
| `web/` | 网页看板、报告入口和聚合大屏 |
| `firmware/` | MicroPython 固件和套件配置示例 |
| `tests/test_acceptance.py` | 回环端到端验收 |
