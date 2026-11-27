from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from io import BytesIO
import asyncio
import json
import os
import socket
import time
import unittest

from fastapi import FastAPI, Request
import httpx
import uvicorn
from websockets.asyncio.client import connect

from factory.app import COOKIE, WORD_MEDIA_TYPE, create_app
from factory.config import DEMO_ACCOUNTS, DEMO_DEVICES, DeviceConfig, ModelConfig, Settings, SkillConfig
from factory.protocol import encode


SHOP = "sim-shop-01"
GATE = "sim-safety-01"


@asynccontextmanager
async def serve(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    address = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="warning", access_log=False, timeout_graceful_shutdown=2,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(9):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Test server exited during startup")
                await asyncio.sleep(0.03)
        yield address
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            listener.close()


async def wait_for(probe, seconds=8):
    async with asyncio.timeout(seconds):
        while True:
            result = await probe()
            if result:
                return result
            await asyncio.sleep(0.11)


async def login(client, role="operator"):
    response = await client.post("/api/login", json={"username": role, "password": DEMO_ACCOUNTS[role]})
    response.raise_for_status()
    return client


async def state(client):
    return (await client.get("/api/state")).raise_for_status().json()


async def selected(client, device_id=SHOP):
    return next(entry for entry in (await state(client))["devices"] if entry["id"] == device_id)


async def fresh(client, device_id=SHOP):
    reading = await selected(client, device_id)
    return reading if reading["fresh"] else None


async def draft(client, actuator="led", value=64, device_id=SHOP):
    response = await client.post("/api/commands", json={"device_id": device_id, "actuator": actuator, "value": value})
    return response.raise_for_status().json()


async def confirm(client, command):
    return (await client.post(f"/api/commands/{command['id']}/confirm")).raise_for_status().json()


async def finished(client, command):
    response = (await client.get(f"/api/commands/{command['id']}")).raise_for_status().json()
    return response if response["status"] != "sent" else None


async def scenario(client, name, device_id=SHOP):
    return (await client.post(f"/api/demo/{device_id}/scenario", json={"scenario": name})).raise_for_status().json()


class FactoryAcceptance(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def demo(self, model=None, skills=None):
        with TemporaryDirectory(prefix="shuyun-acceptance-") as tmp:
            settings = Settings(True, Path(tmp), "127.0.0.1", 0, DEMO_ACCOUNTS, DEMO_DEVICES, model, skills or SkillConfig())
            async with serve(create_app(settings)) as base:
                async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
                    yield client, base

    async def test_live_telemetry_and_grounded_answer(self):
        async with self.demo() as (client, base):
            self.assertEqual((await client.get("/api/state")).status_code, 401)
            self.assertIn("蜀云智造", (await client.get("/")).raise_for_status().text)
            self.assertEqual((await client.get("/assets/vendor/echarts.min.js")).status_code, 200)
            await login(client)
            devices = (await state(client))["devices"]
            self.assertEqual({d["domain"] for d in devices}, {"workshop", "warehouse", "safety"})
            live = await wait_for(lambda: fresh(client))
            headers = {"Cookie": f"{COOKIE}={client.cookies[COOKIE]}"}
            async with connect(base.replace("http:", "ws:") + "/ws", additional_headers=headers, origin=base, proxy=None) as ws:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 4))
                self.assertEqual(frame["type"], "state")
                sample = next(d for d in frame["data"]["devices"] if d["id"] == SHOP)["latest"]
                self.assertGreaterEqual(sample["at"], live["latest"]["at"])
            response = (await client.post("/api/assistant", json={
                "device_id": SHOP, "text": "车间现在多少度、超温没？",
            })).raise_for_status().json()
            points = (await client.get(f"/api/devices/{SHOP}/trend")).raise_for_status().json()
            answer_sample = next(entry for entry in points if entry["at"] == response["updated_at"])
            self.assertIn(f"{answer_sample['temperature']:g}°C", response["answer"])
            self.assertIn("32°C", response["answer"])
            self.assertIn("设备采样", response["source"])
            self.assertEqual(response["engine"], "local")
            follow_up = (await client.post("/api/assistant", json={
                "device_id": SHOP, "text": "上一条的来源？",
            })).raise_for_status().json()
            self.assertEqual(follow_up["source"], response["source"])
            self.assertTrue((await client.get("/api/records?kind=conversations&q=超温")).raise_for_status().json())

    async def test_preview_confirmation_and_single_dispatch(self):
        async with self.demo() as (client, base):
            await login(client)
            await wait_for(lambda: fresh(client))
            response = (await client.post("/api/assistant", json={
                "device_id": SHOP, "text": "把补光灯调到64%",
            })).raise_for_status().json()
            command = response["preview"]
            self.assertEqual(command["status"], "draft")
            self.assertEqual((await selected(client))["outputs"]["led"], 0)
            self.assertIsNone((await client.post("/api/assistant", json={
                "device_id": SHOP, "text": "不要打开补光灯",
            })).raise_for_status().json()["preview"])
            first, second = await asyncio.gather(confirm(client, command), confirm(client, command))
            self.assertIn(first["status"], {"sent", "succeeded"})
            self.assertIn(second["status"], {"sent", "succeeded"})
            receipt = await wait_for(lambda: finished(client, command))
            self.assertEqual(receipt["status"], "succeeded")
            self.assertIn("回读值 64", receipt["detail"])
            self.assertEqual((await selected(client))["outputs"]["led"], 64)
            async with httpx.AsyncClient(base_url=base, trust_env=False) as admin:
                await login(admin, "admin")
                audits = (await admin.get("/api/records", params={"kind": "audit", "q": command["id"][:8]})).raise_for_status().json()
                self.assertEqual(sum(row["action"] == "确认并下发" for row in audits), 1)

    async def test_overheat_interlock_recovery_and_work_order(self):
        async with self.demo() as (client, _):
            await login(client)
            await wait_for(lambda: fresh(client))
            await scenario(client, "overheat")

            async def alarm():
                return next((item for item in (await state(client))["alerts"] if item["device_id"] == SHOP
                             and item["metric"] == "temperature" and not item["resolved_at"]), None)

            entry = await wait_for(alarm)
            self.assertGreater(entry["value"], entry["threshold"])
            self.assertEqual((await client.post(f"/api/alerts/{entry['id']}/review", json={"note": "过早回访"})).status_code, 409)
            command = await draft(client, "relay1", 1)
            denied = await confirm(client, command)
            self.assertEqual(denied["status"], "rejected")
            self.assertIn("温度", denied["detail"])
            self.assertEqual((await selected(client))["outputs"]["relay1"], 0)
            self.assertEqual((await client.post(f"/api/alerts/{entry['id']}/close", json={"note": "继续检查"})).status_code, 409)
            await scenario(client, "normal")

            async def cleared():
                return next((item for item in (await state(client))["alerts"] if item["id"] == entry["id"] and item["resolved_at"]), None)

            await wait_for(cleared)
            closed = (await client.post(f"/api/alerts/{entry['id']}/close", json={"note": "现场复核后关闭"})).raise_for_status().json()
            self.assertEqual(closed["closed_by"], "operator")
            review = (await client.post(f"/api/alerts/{entry['id']}/review", json={"note": "复查温度稳定"})).raise_for_status().json()
            self.assertEqual(review["actor"], "operator")
            search = (await client.get("/api/records?kind=alerts&q=现场复核")).raise_for_status().json()
            self.assertEqual([item["id"] for item in search], [entry["id"]])
            self.assertEqual(search[0]["review_note"], "复查温度稳定")

    async def test_roles_and_threshold_update(self):
        async with self.demo() as (client, base):
            await login(client, "viewer")
            self.assertEqual((await client.post("/api/commands", json={"device_id": SHOP, "actuator": "led", "value": 100})).status_code, 403)
            self.assertEqual((await client.get("/api/records?kind=audit")).status_code, 403)
            self.assertIsNone((await client.post("/api/assistant", json={"device_id": SHOP, "text": "打开补光灯"})).raise_for_status().json()["preview"])
            async with httpx.AsyncClient(base_url=base, trust_env=False) as admin:
                await login(admin, "admin")
                thresholds = {"temperature_high": 22.0, "humidity_low": 30.0, "humidity_high": 85.0, "light_low": 600}
                self.assertEqual((await admin.put("/api/rules", json=thresholds, headers={"Origin": "http://unrelated.invalid"})).status_code, 403)
                self.assertEqual((await client.put("/api/rules", json=thresholds)).status_code, 403)
                self.assertEqual((await admin.put("/api/rules", json=thresholds)).raise_for_status().json(), thresholds)

                async def threshold_applied():
                    return any(item["threshold"] == 22 for item in (await state(admin))["alerts"])

                await wait_for(threshold_applied)

    async def test_disconnect_reject_and_automatic_reconnect(self):
        async with self.demo() as (client, _):
            await login(client)
            await wait_for(lambda: fresh(client))
            request = await draft(client)
            await scenario(client, "disconnect")

            async def is_offline():
                return not (await selected(client))["connected"]

            await wait_for(is_offline)
            result = await confirm(client, request)
            self.assertEqual(result["status"], "rejected")
            self.assertIn("离线", result["detail"])
            await wait_for(lambda: fresh(client), seconds=10)
            events = (await client.get(f"/api/records?kind=events&q={SHOP}")).raise_for_status().json()
            self.assertIn("offline", {row["kind"] for row in events})
            self.assertGreaterEqual(sum(row["kind"] == "online" for row in events), 2)

    async def test_word_exports_and_estimated_energy(self):
        async with self.demo() as (client, _):
            await login(client)
            await wait_for(lambda: fresh(client))
            await scenario(client, "presence")

            async def has_entry():
                data = (await client.get("/api/summary")).raise_for_status().json()
                return data if data["events"].get("entry") else None

            summary = await wait_for(has_entry)
            self.assertTrue(summary["estimated"])
            self.assertGreater(summary["kwh"], 0)
            await scenario(client, "overheat")

            async def has_heat_alert():
                return next((entry for entry in (await state(client))["alerts"]
                             if entry["device_id"] == SHOP and entry["metric"] == "temperature"), None)

            await wait_for(has_heat_alert)
            for kind, text in (("inspection", "巡检报告"), ("energy", "能耗估算报告")):
                response = (await client.get(f"/api/reports/{kind}")).raise_for_status()
                self.assertEqual(response.headers["content-type"], WORD_MEDIA_TYPE)
                self.assertIn("attachment", response.headers["content-disposition"])
                with ZipFile(BytesIO(response.content)) as archive:
                    xml = archive.read("word/document.xml").decode()
                    self.assertIn(text, xml)
                    self.assertIn("模拟设备", xml)
                    self.assertIn(summary["day"], xml)
                    if kind == "inspection":
                        self.assertIn("越限中", xml)
                output = os.getenv("FACTORY_QA_ARTIFACTS")
                if output:
                    directory = Path(output)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / (kind + ".docx")).write_bytes(response.content)

    async def test_device_identity_and_receipt_binding(self):
        registered = DeviceConfig("esp32-fixture", "板端协议测试", "workshop", "fixture-token-unused-on-real-hardware", ("led",))
        with TemporaryDirectory(prefix="shuyun-device-test-") as tmp:
            settings = Settings(False, Path(tmp), "127.0.0.1", 0, DEMO_ACCOUNTS, (registered,))
            app = create_app(settings)
            async with serve(app) as base, httpx.AsyncClient(base_url=base, trust_env=False) as client:
                await login(client)
                tcp = app.state.tcp_port
                hello = {"type": "hello", "device_id": registered.id, "token": registered.token, "outputs": ["led"]}
                wrong_reader, wrong_writer = await asyncio.open_connection("127.0.0.1", tcp)
                wrong_writer.write(encode({**hello, "token": "wrong"}))
                wrong = json.loads(await asyncio.wait_for(wrong_reader.readline(), 3))
                self.assertEqual(wrong["code"], "unauthorized")
                wrong_writer.close()
                await wrong_writer.wait_closed()

                reader, writer = await asyncio.open_connection("127.0.0.1", tcp)
                try:
                    writer.write(encode(hello))
                    self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 3))["type"], "welcome")
                    writer.write(encode({"type": "telemetry", "seq": 0, "temperature": 25, "humidity": 53,
                                         "light": 1000, "presence": False, "obstruction": False, "outputs": {"led": 0}}))
                    self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 3))["type"], "telemetry_ack")
                    await wait_for(lambda: fresh(client, registered.id))
                    command = await draft(client, "led", 25, registered.id)
                    self.assertEqual((await confirm(client, command))["status"], "sent")
                    frame = json.loads(await asyncio.wait_for(reader.readline(), 3))
                    self.assertEqual((frame["type"], frame["id"], frame["value"]), ("command", command["id"], 25))
                    writer.write(encode({"type": "command_ack", "id": "0" * 32, "ok": True, "outputs": {"led": 25}}))
                    self.assertIsNone(await finished(client, command))
                    writer.write(encode({"type": "command_ack", "id": command["id"], "ok": True, "outputs": {"led": 21}}))
                    self.assertEqual((await wait_for(lambda: finished(client, command)))["status"], "failed")
                finally:
                    writer.close()
                    await writer.wait_closed()

    async def test_agentica_function_call_with_local_model_fixture(self):
        endpoint = FastAPI()
        tool_results = []
        requests = []

        @endpoint.post("/v1/chat/completions")
        async def model(request: Request):
            payload = await request.json()
            requests.append(payload["messages"])
            tool_output = next((item for item in payload["messages"] if item["role"] == "tool"), None)
            if tool_output:
                tool_results.append(json.loads(tool_output["content"]))
                message = {"role": "assistant", "content": "review_report"}
            else:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_sample", "type": "function",
                    "function": {"name": "read_factory_data", "arguments": "{}"},
                }]}
            return {
                "id": "fixture", "created": int(time.time()), "object": "chat.completion", "model": payload["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "stop" if tool_output else "tool_calls"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            }

        async with serve(endpoint) as base:
            async with self.demo(ModelConfig("local-fixture", base + "/v1", "local-key")) as (client, _):
                await login(client)
                await wait_for(lambda: fresh(client))
                result = (await client.post("/api/assistant", json={
                    "device_id": SHOP, "text": "有没有超温",
                })).raise_for_status().json()
                self.assertEqual(result["engine"], "agentica")
                self.assertTrue(tool_results and tool_results[0]["fresh"])
                self.assertIn(f"{tool_results[0]['readings']['temperature']:g}°C", result["answer"])
                self.assertIn("记录与报告页面", result["answer"])
                self.assertIsNone(result["preview"])
                await login(client, "admin")
                settings = (await client.get("/api/admin/tools")).raise_for_status().json()
                guidance = {**settings["guidance"], "device": "优先提示复核采样记录。"}
                prompts = {**settings["prompts"], "guidance": guidance}
                (await client.put("/api/admin/prompts", json=prompts)).raise_for_status()
                comparison = (await client.post("/api/admin/model-test", json={
                    "device_id": SHOP, "text": "有没有超温", "compare": True, "input_rate": 5, "output_rate": 10,
                })).raise_for_status().json()["results"]
                self.assertEqual([entry["engine"] for entry in comparison], ["local", "agentica"])
                self.assertGreater(comparison[1]["usage"]["input"], 0)
                self.assertGreater(comparison[1]["estimated_cost"], 0)
                self.assertIn(guidance["device"], json.dumps(requests[-1], ensure_ascii=False))

    async def test_production_inventory_and_checkpoint_answers(self):
        async with self.demo() as (client, _):
            await login(client)
            ledger = (await client.get("/api/business")).raise_for_status().json()
            self.assertEqual(ledger["totals"], {"production": 214, "inventory": 282})
            record = {"kind": "production", "day": ledger["day"], "code": "装配线", "label": "装配线",
                      "quantity": 153, "source": "现场人工班报 A"}
            saved = (await client.put("/api/business", json=record)).raise_for_status().json()
            answer = (await client.post("/api/assistant", json={
                "device_id": SHOP, "agent": "auto", "text": "今天装配线跑了多少件？",
            })).raise_for_status().json()
            self.assertIn("153 件", answer["answer"])
            self.assertIn(record["source"], answer["source"])
            self.assertEqual(answer["updated_at"], saved["updated_at"])
            self.assertEqual(answer["agent"], "device")
            report = (await client.get("/api/summary")).raise_for_status().json()
            self.assertEqual(report["business"]["production"], 239)

            inventory = {"kind": "inventory", "day": ledger["day"], "code": "fin-021", "label": "装配成品",
                         "quantity": 0, "source": "仓库当日盘点"}
            (await client.put("/api/business", json=inventory)).raise_for_status()
            reply = (await client.post("/api/assistant", json={
                "device_id": SHOP, "text": "FIN-021 库存多少？",
            })).raise_for_status().json()
            self.assertIn("0 件", reply["answer"])
            unknown = (await client.post("/api/assistant", json={
                "device_id": SHOP, "text": "NEW-123 库存多少？",
            })).raise_for_status().json()
            self.assertIn("暂无匹配", unknown["answer"])
            await login(client, "viewer")
            self.assertEqual((await client.put("/api/business", json=record)).status_code, 403)

    async def test_decision_adoption_and_human_handoff(self):
        async with self.demo() as (client, _):
            await login(client)
            await wait_for(lambda: fresh(client))
            await scenario(client, "dim")

            async def low_light():
                plans = (await client.get(f"/api/decisions/{SHOP}")).raise_for_status().json()
                return next((plan for plan in plans if plan["id"] == "increase_light"), None)

            plan = await wait_for(low_light)
            advice = (await client.post("/api/assistant", json={"device_id": SHOP, "text": "现在有哪些处置建议？"})).raise_for_status().json()
            self.assertGreater(advice["recommendations"], 0)
            self.assertIn("补光", advice["answer"])
            action = (await client.post(f"/api/decisions/{SHOP}/{plan['id']}/adopt")).raise_for_status().json()["preview"]
            self.assertEqual(action["origin"], "decision")
            self.assertEqual(action["status"], "draft")
            self.assertEqual((await selected(client))["outputs"]["led"], 0)
            await confirm(client, action)
            result = await wait_for(lambda: finished(client, action))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual((await selected(client))["outputs"]["led"], plan["value"])

            answer = (await client.post("/api/assistant", json={
                "device_id": SHOP, "text": "现场光照多少？",
            })).raise_for_status().json()
            body = {"device_id": SHOP, "reason": "需要现场检查照度", "conversation_id": answer["conversation_id"]}
            ticket = (await client.post("/api/handoffs", json=body)).raise_for_status().json()
            repeated = (await client.post("/api/handoffs", json=body)).raise_for_status().json()
            self.assertEqual(repeated["id"], ticket["id"])
            entries = (await client.get("/api/handoffs")).raise_for_status().json()
            self.assertEqual(entries[0]["question"], "现场光照多少？")
            resolved = (await client.post(f"/api/handoffs/{ticket['id']}/resolve",
                                          json={"note": "照明已核查"})).raise_for_status().json()
            self.assertEqual(resolved["resolution"], "照明已核查")

    async def test_pass_validation_confirmation_and_interlock(self):
        async with self.demo() as (client, _):
            await login(client)
            await wait_for(lambda: fresh(client, GATE))

            async def issue():
                ticket = (await client.post("/api/access/issue", json={
                    "device_id": GATE, "purpose": "实训巡检", "minutes": 3,
                })).raise_for_status().json()
                self.assertNotIn(ticket["code"], (await client.get("/api/access")).text)
                verified = (await client.post("/api/access/verify", json={"code": ticket["code"]})).raise_for_status().json()
                self.assertEqual(verified["status"], "verified")
                self.assertEqual((await client.post("/api/access/verify", json={"code": ticket["code"]})).status_code, 409)
                return verified["id"]

            await scenario(client, "obstruction", GATE)

            async def blocked():
                return (await selected(client, GATE))["latest"]["obstruction"] == 1

            await wait_for(blocked)
            denied = await issue()
            result = (await client.post(f"/api/access/{denied}/confirm")).raise_for_status().json()
            self.assertEqual(result["status"], "rejected")
            await scenario(client, "normal", GATE)

            async def clear():
                return (await selected(client, GATE))["latest"]["obstruction"] == 0

            await wait_for(clear)
            confirmed = await issue()
            self.assertEqual((await client.get("/api/access")).raise_for_status().json()["gates"], {})
            result = (await client.post(f"/api/access/{confirmed}/confirm")).raise_for_status().json()
            self.assertEqual(result["status"], "released")
            repeated = (await client.post(f"/api/access/{confirmed}/confirm")).raise_for_status().json()
            self.assertEqual(result["confirmed_at"], repeated["confirmed_at"])
            self.assertIn(GATE, (await client.get("/api/access")).raise_for_status().json()["gates"])

            async def gate_closed():
                return not (await client.get("/api/access")).raise_for_status().json()["gates"]

            await wait_for(gate_closed)
            rows = (await client.get("/api/records", params={"kind": "events", "q": confirmed[:8]})).raise_for_status().json()
            self.assertEqual([row["kind"] for row in rows], ["pass"])

    async def test_permission_matrix_http_and_mcp_tools(self):
        key = "fixture-business-reader-token"
        async with self.demo(skills=SkillConfig(token=key)) as (client, base):
            tool = {"name": "read_business_summary"}
            self.assertEqual((await client.post("/api/skills/run", json=tool)).status_code, 401)
            self.assertEqual((await client.post("/toolkit/mcp", json={})).status_code, 401)
            with_key = await client.post("/api/skills/run", json=tool, headers={"Authorization": "Bearer " + key})
            self.assertEqual(with_key.raise_for_status().json()["totals"]["production"], 214)
            await login(client, "operator")
            self.assertEqual((await client.get("/api/admin/tools")).status_code, 403)
            await login(client, "admin")
            admin = (await client.get("/api/admin/tools")).raise_for_status().json()
            self.assertEqual(len(admin["permissions"]), 17)
            new_prompts = {**admin["prompts"], "device": ["今日产量是多少？"]}
            (await client.put("/api/admin/prompts", json=new_prompts)).raise_for_status()
            self.assertEqual((await client.get("/api/session")).raise_for_status().json()["prompts"], new_prompts)
            mcp_result = (await client.post("/api/skills/mcp/local", json=tool)).raise_for_status().json()
            self.assertEqual(mcp_result["totals"]["production"], 214)
            async with self.demo(skills=SkillConfig(remote_url=base + "/toolkit/mcp", remote_token=key)) as (external, _):
                await login(external, "admin")
                remote = (await external.post("/api/skills/mcp/remote", json=tool)).raise_for_status().json()
                self.assertEqual(remote["totals"], mcp_result["totals"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
