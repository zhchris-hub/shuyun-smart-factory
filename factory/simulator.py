import asyncio
import json
import math
import time

from factory.config import DeviceConfig
from factory.protocol import SAMPLE_INTERVAL, encode


SCENARIOS = {
    "normal": "恢复正常",
    "overheat": "模拟温度越限",
    "dim": "模拟照度不足",
    "presence": "模拟人员进入",
    "obstruction": "模拟红外遮挡",
    "disconnect": "模拟断线重连",
}


class SimulatedDevice:
    def __init__(self, config: DeviceConfig, index: int, host: str, port: int):
        self.config = config
        self.index = index
        self.host, self.port = host, port
        self.outputs = dict.fromkeys(config.outputs, 0)
        self.scenario = "normal"
        self.resume_at = 0.0
        self.buzzer_until = 0.0
        self.alarming = False
        self.rules = {}

    def set_scenario(self, scenario: str):
        self.scenario = scenario
        self.resume_at = time.monotonic() + 5 if scenario == "disconnect" else 0

    def sample(self, at: float) -> dict:
        phase = 1.4 * self.index
        temperature = round(25.8 + self.index * 0.7 + math.sin(at / 27 + phase) * 0.8, 1)
        if self.scenario == "overheat":
            temperature = min(125, max(36.8, self.rules.get("temperature_high", 32) + 2))
        humidity = round(53 + self.index * 3 + math.sin(at / 41 + phase) * 3, 1)
        light = 260 if self.scenario == "dim" else round(1560 + math.sin(at / 19 + phase) * 140)
        if self.outputs.get("buzzer") and time.monotonic() >= self.buzzer_until:
            self.outputs["buzzer"] = 0
        alarming = (
            temperature > self.rules.get("temperature_high", 32) or
            not self.rules.get("humidity_low", 30) <= humidity <= self.rules.get("humidity_high", 85) or
            light < self.rules.get("light_low", 600)
        )
        if alarming and not self.alarming and "buzzer" in self.outputs and not self.outputs["buzzer"]:
            self.outputs["buzzer"] = 1
            self.buzzer_until = time.monotonic() + 0.75
        self.alarming = alarming
        if temperature > self.rules.get("temperature_high", 32) or self.scenario in ("presence", "obstruction"):
            for actuator in ("relay1", "relay2"):
                if actuator in self.outputs:
                    self.outputs[actuator] = 0
        return {
            "temperature": temperature, "humidity": humidity, "light": light,
            "presence": self.scenario == "presence", "obstruction": self.scenario == "obstruction",
            "outputs": dict(self.outputs),
        }

    async def run(self):
        while True:
            if self.resume_at > time.monotonic():
                await asyncio.sleep(min(0.25, self.resume_at - time.monotonic()))
                continue
            if self.scenario == "disconnect":
                self.scenario = "normal"
            try:
                await self._run_link()
            except (OSError, ValueError, TimeoutError):
                pass
            await asyncio.sleep(0.5)

    async def _run_link(self):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.outputs = dict.fromkeys(self.config.outputs, 0)
        try:
            writer.write(encode({
                "type": "hello", "device_id": self.config.id, "token": self.config.token,
                "outputs": list(self.outputs),
            }))
            await writer.drain()
            frame = await self._read(reader, 3)
            if frame.get("type") != "welcome":
                return
            self.rules = frame["rules"]
            seq = 0
            next_sample = time.monotonic()
            while self.scenario != "disconnect":
                now = time.monotonic()
                if now >= next_sample:
                    writer.write(encode({"type": "telemetry", "seq": seq, **self.sample(time.time())}))
                    await writer.drain()
                    next_sample = now + SAMPLE_INTERVAL
                    seq += 1
                try:
                    frame = await self._read(reader, max(0.01, min(0.15, next_sample - time.monotonic())))
                except TimeoutError:
                    continue
                if frame.get("type") == "command":
                    await self._apply(frame, writer)
                elif frame.get("type") == "telemetry_ack":
                    self.rules = frame["rules"]
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    @staticmethod
    async def _read(reader: asyncio.StreamReader, timeout: float) -> dict:
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise ConnectionError("Disconnected")
        return json.loads(line)

    async def _apply(self, command: dict, writer: asyncio.StreamWriter):
        actuator, value = command["actuator"], command["value"]
        blocked = actuator in ("relay1", "relay2") and value and (
            self.scenario in ("overheat", "presence", "obstruction") or
            self.outputs.get("relay2" if actuator == "relay1" else "relay1") == 1
        )
        if not blocked:
            self.outputs[actuator] = value
            if actuator == "buzzer" and value:
                self.buzzer_until = time.monotonic() + 3
        writer.write(encode({
            "type": "command_ack", "id": command["id"], "ok": not blocked, "outputs": self.outputs,
            "detail": "模拟板端联锁拒绝。" if blocked else "",
        }))
        await writer.drain()
