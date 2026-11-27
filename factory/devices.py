from dataclasses import dataclass
import asyncio
import hmac
import json
import logging
import time

from pydantic import ValidationError

from factory.config import OUTPUTS, DeviceConfig
from factory.protocol import (
    COMMAND_TIMEOUT, DRAFT_SECONDS, FRESH_SECONDS, MAX_FRAME, Hello, Receipt, Telemetry,
    allowed_value, encode, valid_output_states,
)
from factory.store import Store


log = logging.getLogger(__name__)


@dataclass
class Link:
    device_id: str
    writer: asyncio.StreamWriter
    outputs: set[str]
    last_seq: int = -1
    sampled_at: float | None = None
    states: dict[str, int] | None = None
    pending_id: str | None = None
    sent_at: float = 0
    # A timed-out or interrupted command needs an independent telemetry sample.
    require_sample: bool = False


class DeviceHub:
    def __init__(self, store: Store, devices: tuple[DeviceConfig, ...]):
        self.store = store
        self.config = {device.id: device for device in devices}
        self.links: dict[str, Link] = {}
        self.tasks: set[asyncio.Task] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.tasks.add(task)
        link = None
        try:
            frame = await self._read(reader, 5)
            hello = Hello.model_validate(frame)
            device = self.config.get(hello.device_id)
            if device is None or not hmac.compare_digest(hello.token.encode(), device.token.encode()):
                writer.write(encode({"type": "error", "code": "unauthorized"}))
                await writer.drain()
                return
            advertised = set(hello.outputs)
            if not advertised <= set(device.outputs) or device.id in self.links:
                writer.write(encode({"type": "error", "code": "unavailable"}))
                await writer.drain()
                return
            link = Link(device.id, writer, advertised)
            self.links[device.id] = link
            self.store.device_event(device.id, "online", "设备连接已鉴权。")
            writer.write(encode({"type": "welcome", "device_id": device.id, "rules": self.store.rules}))
            await writer.drain()
            while True:
                frame = await self._read(reader, 18)
                kind = frame.get("type")
                if kind == "telemetry":
                    self._sample(link, Telemetry.model_validate(frame))
                    writer.write(encode({
                        "type": "telemetry_ack", "seq": frame["seq"], "rules": self.store.rules,
                    }))
                    await writer.drain()
                elif kind == "command_ack":
                    self._receipt(link, Receipt.model_validate(frame))
                else:
                    raise ValueError("帧类型无效")
        except (ConnectionError, OSError, TimeoutError, ValidationError, ValueError):
            # The peer can send secrets or arbitrary text. Log only the authenticated ID.
            if link:
                log.debug("Device %s disconnected", link.device_id)
        finally:
            if link and self.links.get(link.device_id) is link:
                self.links.pop(link.device_id)
                if link.pending_id:
                    self.store.finish_command(link.pending_id, "unknown", "连接中断，未收到执行回执。")
                self.store.device_event(link.device_id, "offline", "设备连接已断开。")
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            finally:
                self.tasks.discard(task)

    @staticmethod
    async def _read(reader: asyncio.StreamReader, timeout: float) -> dict:
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
            raise ConnectionError("NDJSON stream ended or exceeded frame size")
        frame = json.loads(line.decode("utf-8"))
        if not isinstance(frame, dict):
            raise ValueError("NDJSON object required")
        return frame

    def _sample(self, link: Link, sample: Telemetry):
        if not valid_output_states(sample.outputs, link.outputs):
            raise ValueError("执行器状态和注册能力不一致")
        if sample.seq <= link.last_seq:
            return
        now = time.monotonic()
        continuous = link.sampled_at is not None and now - link.sampled_at <= FRESH_SECONDS
        data = sample.model_dump(exclude={"type", "seq"})
        self.store.ingest(link.device_id, data, continuous=continuous)
        link.sampled_at = now
        link.last_seq = sample.seq
        link.states = sample.outputs
        link.require_sample = False

    def _receipt(self, link: Link, receipt: Receipt):
        if link.pending_id != receipt.id:
            return
        command = self.store.command(receipt.id)
        link.pending_id = None
        if not valid_output_states(receipt.outputs, link.outputs):
            link.require_sample = True
            self.store.finish_command(receipt.id, "unknown", "回读状态不完整，请核实设备状态。")
            return
        link.states = receipt.outputs
        verified = receipt.ok and receipt.outputs.get(command["actuator"]) == command["value"]
        if verified:
            detail = f"{OUTPUTS[command['actuator']]} 回读值 {command['value']}，与确认指令一致。"
        else:
            detail = receipt.detail or "设备拒绝执行或返回值不一致。"
        self.store.finish_command(receipt.id, "succeeded" if verified else "failed", detail)

    def current(self, device_id: str) -> dict:
        link = self.links.get(device_id)
        sample = self.store.latest(device_id)
        fresh = link is not None and link.sampled_at is not None and time.monotonic() - link.sampled_at <= FRESH_SECONDS
        return {
            "connected": link is not None, "fresh": fresh,
            "latest": sample, "outputs": dict(link.states or {}) if link else {},
            "capabilities": sorted(link.outputs) if link else list(self.config[device_id].outputs),
        }

    def draft(self, device_id: str, actuator: str, value: int, actor: str, *,
              origin: str = "manual", decision_key: str | None = None) -> dict:
        device = self.config.get(device_id)
        if not device:
            raise LookupError("设备未登记。")
        link = self.links.get(device_id)
        supported = link.outputs if link else device.outputs
        if actuator not in supported or not allowed_value(actuator, value):
            raise ValueError("设备不支持该执行器，或目标值超出范围。")
        return self.store.draft(device_id, actuator, value, actor, origin=origin, decision_key=decision_key)

    def _interlock(self, command: dict, link: Link | None) -> str | None:
        if link is None or link.writer.is_closing():
            return "设备离线，指令没有下发。"
        if command["actuator"] not in link.outputs:
            return "设备未声明该执行器，指令没有下发。"
        if link.pending_id:
            return "设备仍在等待上一条指令回执，请重新预览后再试。"
        if command["value"] == 0:
            return None
        current = self.current(command["device_id"])
        if not current["fresh"] or link.require_sample:
            return "尚未收到新鲜的板端状态，已拦截启动指令。"
        if command["actuator"] not in ("relay1", "relay2"):
            return None
        sample = current["latest"]
        if sample["presence"] != 0 or sample["obstruction"] != 0:
            return "联锁拒绝：人员或遮挡未确认清除。"
        if sample["temperature"] is None or sample["temperature"] > self.store.rules["temperature_high"]:
            return "联锁拒绝：温度异常或读数不可用。"
        other = "relay2" if command["actuator"] == "relay1" else "relay1"
        if current["outputs"].get(other):
            return "联锁拒绝：两路继电器互斥，请先关闭另一路。"
        return None

    async def confirm(self, command: dict, actor: str) -> dict:
        command_id = command["id"]
        if command["status"] != "draft":
            return command
        if time.time() - command["created_at"] > DRAFT_SECONDS:
            self.store.finish_command(command_id, "expired", "预览已超时，请重新预览。", actor)
            return self.store.command(command_id)
        link = self.links.get(command["device_id"])
        reason = self._interlock(command, link)
        if reason:
            self.store.finish_command(command_id, "rejected", reason, actor)
            return self.store.command(command_id)
        # No await between the interlock check and reservation, so concurrent confirmations
        # on this event loop cannot send two commands to a device at the same time.
        self.store.mark_sent(command_id, actor)
        link.pending_id = command_id
        link.sent_at = time.monotonic()
        try:
            link.writer.write(encode({
                "type": "command", "id": command_id, "actuator": command["actuator"], "value": command["value"],
            }))
            await asyncio.wait_for(link.writer.drain(), 2)
        except (OSError, TimeoutError):
            if link.pending_id == command_id:
                link.pending_id = None
                link.require_sample = True
                self.store.finish_command(command_id, "unknown", "发送连接异常，执行结果待核实。")
        return self.store.command(command_id)

    async def expire_pending(self):
        next_draft_check = 0.0
        while True:
            await asyncio.sleep(0.4)
            now = time.monotonic()
            if now >= next_draft_check:
                self.store.expire_drafts(time.time() - DRAFT_SECONDS)
                next_draft_check = now + 2.0
            for link in self.links.values():
                if link.pending_id and now - link.sent_at > COMMAND_TIMEOUT:
                    command_id = link.pending_id
                    link.pending_id = None
                    link.require_sample = True
                    self.store.finish_command(command_id, "unknown", "等待板端回执超时，执行结果待核实。")

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
