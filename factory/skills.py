from collections.abc import Callable
from typing import Any
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from openai import DefaultAsyncHttpxClient

from factory.devices import DeviceHub
from factory.operations import BUSINESS_LABELS, Operations
from factory.store import Store


CATALOG = {
    "read_factory_data": {"label": "设备实时读数", "device": True, "day": False},
    "read_business_summary": {"label": "产量和库存汇总", "device": False, "day": True},
    "read_energy_summary": {"label": "用电估算与事件汇总", "device": True, "day": True},
}


class FactorySkills:
    def __init__(self, store: Store, hub: DeviceHub, operations: Operations):
        self.store, self.hub, self.operations = store, hub, operations

    def read_factory_data(self, device_id: str) -> dict:
        if device_id not in self.hub.config:
            raise LookupError("没有登记该设备。")
        data = self.hub.current(device_id)
        return {"device_id": device_id, "online": data["fresh"], "sample": data["latest"], "rules": self.store.rules}

    def read_business_summary(self, day: str | None = None) -> dict:
        data = self.operations.business(day)
        return {
            "day": data["day"], "totals": data["totals"], "counts": data["counts"], "updated_at": data["updated_at"],
            "basis": {key: f"按台账登记日期汇总{label}" for key, label in BUSINESS_LABELS.items()},
        }

    def read_energy_summary(self, device_id: str | None = None, day: str | None = None) -> dict:
        if device_id and device_id not in self.hub.config:
            raise LookupError("没有登记该设备。")
        return self.store.dashboard_summary(device_id, day)

    def run(self, name: str, device_id: str | None, day: str | None) -> dict:
        match name:
            case "read_factory_data":
                if not device_id:
                    raise ValueError("请选择需要查询的终端。")
                return self.read_factory_data(device_id)
            case "read_business_summary":
                return self.read_business_summary(day)
            case "read_energy_summary":
                return self.read_energy_summary(device_id, day)
            case _:
                raise LookupError("找不到该只读工具。")


def build_mcp(current: Callable[[], FactorySkills]) -> MCPServer:
    server = MCPServer("shuyun-data", title="蜀云智造只读工具", version="1.0", log_level="WARNING")
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only, structured_output=True)
    async def read_factory_data(device_id: str) -> dict[str, Any]:
        """读取登记设备的最近采样、在线情况和阈值。"""
        return current().read_factory_data(device_id)

    @server.tool(annotations=read_only, structured_output=True)
    async def read_business_summary(day: str | None = None) -> dict[str, Any]:
        """查询指定日期的产量与库存台账汇总。日期采用 YYYY-MM-DD。"""
        return current().read_business_summary(day)

    @server.tool(annotations=read_only, structured_output=True)
    async def read_energy_summary(device_id: str | None = None, day: str | None = None) -> dict[str, Any]:
        """查询指定设备和日期的估算用电与事件计数。"""
        return current().read_energy_summary(device_id, day)

    return server


async def call_mcp(url: str, headers: dict[str, str], name: str, arguments: dict) -> dict:
    if name not in CATALOG:
        raise LookupError("找不到该只读工具。")
    async with asyncio.timeout(12), DefaultAsyncHttpxClient(headers=headers, timeout=8, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=8) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    if result.is_error:
        raise ValueError("MCP 工具执行失败，请核对参数和服务配置。")
    if result.structured_content is not None:
        return result.structured_content
    text = next((item.text for item in result.content if item.type == "text"), "")
    try:
        output = json.loads(text)
    except ValueError:
        output = {"text": text}
    return output if isinstance(output, dict) else {"value": output}
