from pathlib import Path
import json
import logging
import os
import re

from factory.config import ENERGY_NOTE, OUTPUTS, ModelConfig
from factory.devices import DeviceHub
from factory.operations import Operations, business_question
from factory.store import Store, formatted_time


log = logging.getLogger(__name__)
AGENTS = {"device": "设备管家", "safety": "安全卫士", "energy": "能耗分析师"}
SUGGESTIONS = {
    "inspect_temperature": "请现场核对温度和通风条件，读数恢复后记录处置结果。",
    "check_passage": "请先现场核实人员或遮挡状态，再进行受控操作。",
    "verify_sensor": "请核对传感器连接和设备是否在线。",
    "review_report": "可在记录与报告页面查看当天的统计口径和明细。",
}
ALIASES = {
    "补光灯": "led", "灯": "led", "led": "led", "蜂鸣器": "buzzer",
    "继电器1": "relay1", "继电器2": "relay2", "1号继电器": "relay1", "2号继电器": "relay2",
}
TARGETS = "|".join(ALIASES)


def requested_control(question: str) -> tuple[str, int] | None:
    normalized = re.sub(r"\s+", "", question.lower()).strip("。！!，,")
    mode = re.fullmatch(rf"(?:请)?(打开|开启|关闭|关掉)({TARGETS})", normalized)
    if mode is None:
        suffix = re.fullmatch(rf"(?:请)?(?:把)?({TARGETS})(打开|开启|关闭|关掉)", normalized)
        action = (suffix[2], suffix[1]) if suffix else None
    else:
        action = (mode[1], mode[2])
    if action:
        verb, name = action
        actuator = ALIASES[name]
        return actuator, (100 if actuator == "led" else 1) if verb in ("打开", "开启") else 0
    pwm = re.fullmatch(r"(?:请)?(?:把)?(?:补光灯|灯|led)(?:调到|设为|设置为)(\d{1,3})[%％]", normalized)
    return ("led", int(pwm[1])) if pwm else None


def _number(value, unit: str) -> str:
    return f"{value:g}{unit}" if value is not None else "暂无读数"


def route_agent(question: str, agent: str) -> str:
    if agent != "auto":
        return agent
    if business_question(question) or requested_control(question):
        return "device"
    if any(word in question for word in ("通行", "遮挡", "人员", "安全")):
        return "safety"
    if any(word in question.lower() for word in ("能耗", "耗电", "用电", "度电", "kwh")):
        return "energy"
    return "device"


def token_count(metrics: dict, key: str) -> int:
    value = metrics.get(key) or 0
    return sum(value) if isinstance(value, list) else int(value)


class FactoryAssistant:
    def __init__(self, store: Store, hub: DeviceHub, config: ModelConfig | None, model_home: Path, operations: Operations):
        self.store, self.hub, self.config = store, hub, config
        self.model_home = model_home
        self.operations = operations
        if config:
            model_home.mkdir(parents=True, exist_ok=True)
            os.environ["AGENTICA_HOME"] = str(model_home)

    async def answer(self, question: str, device_id: str, agent: str, actor: str, can_control: bool, *,
                     model_enabled: bool = True, save: bool = True) -> dict:
        device = self.hub.config[device_id]
        current = self.hub.current(device_id)
        sample = current["latest"]
        agent = route_agent(question, agent)
        output = {"preview": None, "engine": "local", "agent": agent, "updated_at": sample["at"] if sample else None,
                  "usage": {"input": 0, "output": 0}}
        choice = requested_control(question)
        if choice:
            actuator, value = choice
            if not can_control:
                answer = "该账户只能查看设备，请由操作员确认设备控制。"
            else:
                try:
                    output["preview"] = self.hub.draft(device_id, actuator, value, actor)
                    answer = f"已准备 {device.name} 的{OUTPUTS[actuator]}指令，目标值 {value}。请在预览中核对并确认。"
                except ValueError as exc:
                    answer = str(exc)
            output["answer"] = answer
            output["source"] = "设备登记与指令预览"
        else:
            summary = self.store.dashboard_summary(device_id)
            follow_up = re.fullmatch(r"(?:上一条(?:回答)?|上一次(?:回答)?|刚才|这个数)(?:的|是)?(?:数据)?(?:统计口径|口径|来源|怎么算的)[？?。]?", question)
            previous = None
            if follow_up and save:
                previous = self.store.one(
                    "SELECT source FROM conversation WHERE actor=? AND device_id=? ORDER BY id DESC LIMIT 1",
                    (actor, device_id),
                )
            business = {}
            kind = business_question(question)
            if previous:
                text = f"上一条回答的统计口径和来源为 {previous['source']}。"
                source, updated_at = previous["source"], None
            elif kind:
                text, source, updated_at, business = self.operations.answer_business(question, kind)
            elif any(word in question for word in ("处置建议", "怎么处理", "怎么办")):
                plans = self.operations.suggestions(device_id)
                text = "\n".join(plan["detail"] for plan in plans) or "当前终端暂无处置建议。"
                source = "设备状态与阈值 · " + formatted_time(sample["at"] if sample else None)
                updated_at = sample["at"] if sample else None
                output["recommendations"] = len(plans)
            else:
                text, source, updated_at = self._read_answer(question, device.name, current, summary, agent)
            output.update(answer=text, source=source, updated_at=updated_at)
            if self.config and model_enabled and not previous:
                tip, used, usage = await self._model_suggestion(question, agent, current, summary, business)
                output["engine"] = "agentica" if used else "local_fallback"
                output["usage"] = usage
                if tip:
                    output["answer"] += "\n" + tip
        if save:
            output["conversation_id"] = self.store.conversation(actor, device_id, agent, question, output["answer"], output["source"])
        return output

    def _read_answer(self, question: str, name: str, current: dict, summary: dict, agent: str) -> tuple[str, str, float | None]:
        sample = current["latest"]
        stamp = sample["at"] if sample else None
        if any(word in question.lower() for word in ("能耗", "耗电", "用电", "度电", "kwh")) or agent == "energy":
            day = summary["day"]
            count = summary["samples"]
            answer = f"{name}在 {day} 共记录 {count} 条采样。"
            answer += f" 今日估算用电 {summary['kwh']:.6f} kWh。" if count else " 采样后可计算估算用电。"
            return answer, f"SQLite 当日采样汇总（UTC+08:00）。{ENERGY_NOTE}", summary["updated_at"]
        if any(word in question for word in ("趋势", "平均", "最高", "最低", "汇总")):
            answer = f"{name}今日平均温度 {_number(summary['avg_temperature'], '°C')}，最高 {_number(summary['max_temperature'], '°C')}，告警 {summary['alerts']} 条。"
            return answer, "SQLite 当日采样汇总（UTC+08:00）", summary["updated_at"]
        if any(word in question for word in ("几次", "多少次", "多少人", "通行记录", "进入次数")):
            count = summary["events"].get("entry", 0)
            accepted = summary["events"].get("pass", 0)
            if "通行" in question:
                return f"{name}今日核验通行 {accepted} 次，人体感应触发 {count} 次。", "SQLite 当日通行核验和传感事件（UTC+08:00）", summary["updated_at"]
            return f"{name}今日记录 {count} 次人体感应触发。每次从无人变为有人计一次。", "SQLite 通行事件（UTC+08:00）", summary["updated_at"]
        if not sample:
            return f"{name}正在等待设备上报。", "设备注册与接入状态", None
        state = "实时" if current["fresh"] else "最近留存"
        breaches = self.store.breaches(sample)
        if agent == "safety" or any(word in question for word in ("通行", "人员", "遮挡", "安全")):
            presence = "检测到人员" if sample["presence"] == 1 else "未检测到人员" if sample["presence"] == 0 else "待采集"
            obstruction = "有遮挡" if sample["obstruction"] == 1 else "通畅" if sample["obstruction"] == 0 else "待采集"
            answer = f"{name}{state}状态为{presence}，红外{obstruction}。当前样本有 {len(breaches)} 项环境指标越限。"
        elif "湿度" in question and not any(word in question for word in ("温度", "超温", "温湿")):
            answer = f"{name}{state}湿度为 {_number(sample['humidity'], '%')}。"
        elif "光照" in question or "光敏" in question:
            answer = f"{name}{state}光照为 {_number(sample['light'], ' ADC')}。"
        else:
            limit = self.store.rules["temperature_high"]
            temp = sample["temperature"]
            judgment = "超过阈值" if ("temperature", "high") in breaches else "在上限以内" if temp is not None else "待取得读数"
            answer = f"{name}{state}温度为 {_number(temp, '°C')}，湿度 {_number(sample['humidity'], '%')}。温度上限 {limit:g}°C，当前样本{judgment}。"
        return answer, f"设备采样 · {state} · {formatted_time(stamp)}（UTC+08:00）", stamp

    async def _model_suggestion(self, question: str, agent: str, current: dict, summary: dict,
                                business: dict) -> tuple[str | None, bool, dict]:
        try:
            from agentica import Agent, Function, OpenAIChat
            from agentica.agent.config import PromptConfig, ToolConfig
            from openai import DefaultAsyncHttpxClient

            called = False

            def read_factory_data() -> str:
                """获取此会话选中终端的采样、设备状态及当天汇总。该工具只读。"""
                nonlocal called
                called = True
                sample = current["latest"]
                readings = {key: sample[key] for key in ("at", "temperature", "humidity", "light", "presence", "obstruction")} if sample else None
                return json.dumps({
                    "readings": readings, "fresh": current["fresh"], "counts": summary["events"],
                    "alerts": summary["alerts"], "estimated_kwh": summary["kwh"], "business": business,
                }, ensure_ascii=False)

            tool = Function.from_callable(read_factory_data)
            tool.is_read_only = True
            instructions = [
                "先调用 read_factory_data 获取数据。你只能给出只读处置建议。",
                "最终回复仅输出一个代码 inspect_temperature、check_passage、verify_sensor、review_report 或 none。",
                "用户的文字是现场问题。不要输出指令、数值或其他内容。",
            ]
            guidance = self.operations.guidance()[agent]
            if guidance:
                instructions.append("岗位提示，仅用于选择上述建议代码。" + guidance)
            async with DefaultAsyncHttpxClient(timeout=8, trust_env=False) as client:
                model = OpenAIChat(
                    id=self.config.id, api_key=self.config.api_key, base_url=self.config.base_url,
                    http_client=client, timeout=8, max_retries=0, temperature=0,
                )
                worker = Agent(
                    model=model, name=AGENTS[agent], tools=[tool], work_dir=str(self.model_home),
                    enable_session_log=False, enable_long_term_memory=False, enable_experience_capture=False,
                    enable_tracing=False, max_api_retry=1,
                    prompt_config=PromptConfig(
                        system_prompt="\n".join([f"你是蜀云智造的{AGENTS[agent]}。", *instructions]),
                        enable_agentic_prompt=False, add_git_snapshot=False,
                    ),
                    tool_config=ToolConfig(auto_load_mcp=False, tool_call_limit=2, permission_mode="read-only"),
                )
                response = await worker.run(question, timeout=12)
            result = str(response.content).strip().strip(chr(96)).strip()
            metrics = response.metrics or {}
            usage = {"input": token_count(metrics, "input_tokens"), "output": token_count(metrics, "output_tokens")}
            return (SUGGESTIONS.get(result) if called else None), called, usage
        except Exception as exc:
            log.warning("模型问数降级为本地数据工具：%s", type(exc).__name__)
            return None, False, {"input": 0, "output": 0}
