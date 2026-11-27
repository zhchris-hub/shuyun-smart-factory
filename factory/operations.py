from datetime import date, timedelta
from hashlib import sha256
from uuid import uuid4
import json
import re
import secrets
import time

from factory.devices import DeviceHub
from factory.store import Store, day_bounds, formatted_time


BUSINESS_LABELS = {"production": "产量", "inventory": "库存"}
DEFAULT_PROMPTS = {
    "device": ["现在多少度，超温没？", "今天装配线跑了多少件？", "打开补光灯", "现在有哪些处置建议？"],
    "safety": ["安全通道现在有遮挡吗？", "今天通行了多少次？", "上一次回答的来源"],
    "energy": ["今天用了多少度电？", "今天车间的用电统计口径是什么？"],
}
PASS_FIELDS = "id,device_id,purpose,issued_by,issued_at,expires_at,verified_by,verified_at,status,confirmed_at,detail"
GATE_SECONDS = 3.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS business_record (
    kind TEXT NOT NULL CHECK(kind IN ('production','inventory')),
    day TEXT NOT NULL,
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity>=0),
    source TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind,day,code)
);
CREATE TABLE IF NOT EXISTS handoff (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES device(id),
    actor TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversation(id),
    reason TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS alert_review (
    alert_id INTEGER PRIMARY KEY REFERENCES alert(id),
    actor TEXT NOT NULL,
    at REAL NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_pass (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES device(id),
    digest TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    verified_by TEXT,
    verified_at REAL,
    status TEXT NOT NULL DEFAULT 'issued',
    confirmed_at REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS access_pass_device ON access_pass(device_id, confirmed_at DESC);
"""


def business_question(text: str) -> str | None:
    if any(word in text for word in ("库存", "存量", "在库")):
        return "inventory"
    if any(word in text for word in ("产量", "产出", "多少件", "生产多少", "生产了多少")):
        return "production"
    return None


class Operations:
    def __init__(self, store: Store, hub: DeviceHub, demo: bool):
        self.store, self.hub, self.demo = store, hub, demo
        store.db.executescript(SCHEMA)
        if demo:
            self._seed()

    def _seed(self):
        today, _, _ = day_bounds()
        examples = (
            ("production", "装配线", "装配线", 128),
            ("production", "包装线", "包装线", 86),
            ("inventory", "MAT-001", "紧固件组件", 240),
            ("inventory", "FIN-021", "装配成品", 42),
        )
        with self.store.db:
            self.store.db.executemany(
                """INSERT OR IGNORE INTO business_record(kind,day,code,label,quantity,source,recorded_by,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(kind, today, code, label, count, "实训演示台账", "系统", time.time())
                 for kind, code, label, count in examples],
            )

    def business(self, day: str | None = None, kind: str | None = None) -> dict:
        label, _, _ = day_bounds(day)
        if kind and kind not in BUSINESS_LABELS:
            raise ValueError("业务类型须为 production 或 inventory。")
        where = " AND kind=?" if kind else ""
        args = (label, kind) if kind else (label,)
        rows = self.store.rows(
            f"SELECT * FROM business_record WHERE day=?{where} ORDER BY kind,code", args,
        )
        return {
            "day": label, "records": rows,
            "totals": {name: sum(row["quantity"] for row in rows if row["kind"] == name) for name in BUSINESS_LABELS},
            "counts": {name: sum(row["kind"] == name for row in rows) for name in BUSINESS_LABELS},
            "updated_at": max((row["updated_at"] for row in rows), default=None),
        }

    def save_business(self, kind: str, day: str, code: str, label: str, quantity: int, source: str, actor: str) -> dict:
        day, _, _ = day_bounds(day)
        code = code.upper() if kind == "inventory" else code
        with self.store.db:
            old = self.store.one(
                "SELECT quantity FROM business_record WHERE kind=? AND day=? AND code=?", (kind, day, code),
            )
            self.store.db.execute(
                """INSERT INTO business_record(kind,day,code,label,quantity,source,recorded_by,updated_at)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind,day,code) DO UPDATE SET
                     label=excluded.label, quantity=excluded.quantity, source=excluded.source,
                     recorded_by=excluded.recorded_by, updated_at=excluded.updated_at""",
                (kind, day, code, label, quantity, source, actor, time.time()),
            )
            change = f"{old['quantity']} 至 {quantity}" if old else str(quantity)
            self.store._audit(actor, "登记" + BUSINESS_LABELS[kind], f"{day} · {code} · {change} 件 · {source}")
        return self.store.one("SELECT * FROM business_record WHERE kind=? AND day=? AND code=?", (kind, day, code))

    def answer_business(self, question: str, kind: str) -> tuple[str, str, float | None, dict]:
        explicit = re.search(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", question)
        today, _, _ = day_bounds()
        selected_day = explicit[0] if explicit else (
            (date.fromisoformat(today) - timedelta(days=1)).isoformat() if "昨天" in question else today
        )
        try:
            data = self.business(selected_day, kind)
        except ValueError:
            return "请检查日期后重试，格式为 YYYY-MM-DD。", "业务台账 · 日期校验", None, {}
        all_records = data["records"]
        normalized = question.casefold()
        matches = [row for row in all_records if row["code"].casefold() in normalized or row["label"].casefold() in normalized]
        specific_line = re.search(r"([\u4e00-\u9fa5A-Za-z0-9_-]{1,12}线)(?:跑|产|做)", question)
        specific_sku = re.search(r"(?<![A-Z0-9])[A-Z]{2,8}-\d{2,}(?![A-Z0-9])", question.upper())
        records = matches or all_records
        if not matches and (specific_line or specific_sku):
            records = []
        basis = "按产线汇总该日登记的完成件数" if kind == "production" else "按物料汇总该日登记的库存快照"
        if not records:
            return f"{selected_day} 暂无匹配的{BUSINESS_LABELS[kind]}记录。", "业务台账 · " + basis, None, {
                "day": selected_day, "kind": kind, "records": 0, "quantity": None,
            }
        total = sum(row["quantity"] for row in records)
        updated = max(row["updated_at"] for row in records)
        names = "、".join(dict.fromkeys(row["label"] for row in records[:3]))
        if len(records) > 3:
            names += f" 等 {len(records)} 条"
        source = "、".join(dict.fromkeys(row["source"] for row in records[:3]))
        return (
            f"{selected_day} 的{names}{BUSINESS_LABELS[kind]}为 {total:,} 件。",
            f"业务台账 · {basis} · {source} · 更新 {formatted_time(updated)}（UTC+08:00）",
            updated,
            {"day": selected_day, "kind": kind, "records": len(records), "quantity": total},
        )

    def prompts(self) -> dict:
        row = self.store.one("SELECT value FROM setting WHERE name='agent_prompts'")
        return json.loads(row["value"]) if row else DEFAULT_PROMPTS

    def guidance(self) -> dict:
        row = self.store.one("SELECT value FROM setting WHERE name='agent_guidance'")
        return json.loads(row["value"]) if row else dict.fromkeys(DEFAULT_PROMPTS, "")

    def save_prompts(self, prompts: dict, actor: str, guidance: dict | None = None) -> dict:
        settings = [("agent_prompts", json.dumps(prompts, ensure_ascii=False))]
        if guidance is not None:
            settings.append(("agent_guidance", json.dumps(guidance, ensure_ascii=False)))
        with self.store.db:
            self.store.db.executemany(
                """INSERT INTO setting(name,value) VALUES (?,?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value""",
                settings,
            )
            self.store._audit(actor, "更新智能体话术", "设备管家、安全卫士和能耗分析师")
        return prompts

    def suggestions(self, device_id: str) -> list[dict]:
        current = self.hub.current(device_id)
        sample = current["latest"]
        if not current["fresh"]:
            return [{"id": "offline", "title": "检查终端连接", "detail": "设备没有新鲜采样，建议转人工核查连接。",
                     "kind": "handoff", "sample_id": sample["id"] if sample else None}]
        plans = []
        limit = self.store.rules["temperature_high"]
        if sample["temperature"] is not None and sample["temperature"] > limit:
            plans.append({
                "id": "overheat", "title": "现场核查温度", "kind": "handoff", "sample_id": sample["id"],
                "detail": f"温度 {sample['temperature']:g} °C 高于 {limit:g} °C，建议检查通风和传感器。",
            })
        light, threshold = sample["light"], self.store.rules["light_low"]
        if light is not None and "led" in current["capabilities"]:
            actual = current["outputs"].get("led", 0)
            value = min(100, max(30, round((threshold - light) * 100 / max(threshold, 1) / 10) * 10))
            if light < threshold and value > actual:
                plans.append({
                    "id": "increase_light", "title": "提高补光亮度", "kind": "command", "sample_id": sample["id"],
                    "detail": f"采样 {light} ADC 低于阈值 {threshold} ADC，建议补光 {value}%。",
                    "actuator": "led", "value": value,
                })
            elif light >= threshold + 150 and actual > 0:
                plans.append({
                    "id": "dim_light", "title": "关闭补光灯", "kind": "command", "sample_id": sample["id"],
                    "detail": f"现场照度 {light} ADC 已有余量，建议将补光调至 0%。",
                    "actuator": "led", "value": 0,
                })
        return plans

    def adopt(self, device_id: str, plan_id: str, actor: str) -> dict:
        plan = next((item for item in self.suggestions(device_id) if item["id"] == plan_id), None)
        if plan is None:
            raise ValueError("设备状态已变化，请刷新建议。")
        if plan["kind"] == "handoff":
            return {"handoff": self.handoff(device_id, actor, plan["detail"], None, plan["id"])}
        key = f"{device_id}/{plan_id}/{plan['sample_id']}/{actor}"
        return {"preview": self.hub.draft(
            device_id, plan["actuator"], plan["value"], actor, origin="decision", decision_key=key,
        )}

    def handoffs(self) -> list[dict]:
        return self.store.rows(
            """SELECT h.*, c.question AS question FROM handoff h
               LEFT JOIN conversation c ON c.id=h.conversation_id
               ORDER BY h.resolved_at IS NOT NULL,h.created_at DESC LIMIT 100""",
        )

    def handoff(self, device_id: str, actor: str, reason: str, conversation_id: int | None, origin: str = "manual") -> dict:
        if conversation_id is not None:
            row = self.store.one("SELECT actor,device_id FROM conversation WHERE id=?", (conversation_id,))
            if row is None or row["actor"] != actor or row["device_id"] != device_id:
                raise LookupError("找不到该终端的本人会话。")
        with self.store.db:
            if origin != "manual" or conversation_id is not None:
                previous = self.store.one(
                    """SELECT * FROM handoff WHERE device_id=? AND actor=? AND origin=? AND conversation_id IS ?
                       AND resolved_at IS NULL ORDER BY id DESC LIMIT 1""",
                    (device_id, actor, origin, conversation_id),
                )
                if previous:
                    return previous
            row = self.store.db.execute(
                "INSERT INTO handoff(device_id,actor,conversation_id,reason,origin,created_at) VALUES (?,?,?,?,?,?)",
                (device_id, actor, conversation_id, reason, origin, time.time()),
            )
            self.store._audit(actor, "发起转人工" if origin == "manual" else "采纳建议并转人工", f"{device_id} · 工单 {row.lastrowid}")
            return self.store.one("SELECT * FROM handoff WHERE id=?", (row.lastrowid,))

    def resolve_handoff(self, ticket_id: int, actor: str, resolution: str) -> dict:
        row = self.store.one("SELECT * FROM handoff WHERE id=?", (ticket_id,))
        if row is None:
            raise LookupError("找不到该人工工单。")
        if row["resolved_at"]:
            raise ValueError("该人工工单已完成。")
        with self.store.db:
            self.store.db.execute(
                "UPDATE handoff SET resolved_at=?,resolved_by=?,resolution=? WHERE id=?",
                (time.time(), actor, resolution, ticket_id),
            )
            self.store._audit(actor, "完成人工处置", f"工单 {ticket_id} · {resolution}")
        return self.store.one("SELECT * FROM handoff WHERE id=?", (ticket_id,))

    def review_alert(self, alert_id: int, actor: str, note: str) -> dict:
        row = self.store.one("SELECT closed_at FROM alert WHERE id=?", (alert_id,))
        if row is None:
            raise LookupError("找不到该告警工单。")
        if not row["closed_at"]:
            raise ValueError("告警工单关闭后可登记回访。")
        if self.store.one("SELECT alert_id FROM alert_review WHERE alert_id=?", (alert_id,)):
            raise ValueError("该告警已登记回访。")
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO alert_review(alert_id,actor,at,note) VALUES (?,?,?,?)", (alert_id, actor, time.time(), note),
            )
            self.store._audit(actor, "登记工单回访", f"告警 {alert_id} · {note}")
        return self.store.one("SELECT * FROM alert_review WHERE alert_id=?", (alert_id,))

    def access(self) -> dict:
        now = time.time()
        rows = self.store.rows(f"SELECT {PASS_FIELDS} FROM access_pass ORDER BY issued_at DESC LIMIT 60")
        for row in rows:
            if row["status"] in ("issued", "verified") and row["expires_at"] < now:
                row["status"] = "expired"
        active = self.store.rows(
            """SELECT device_id,MAX(confirmed_at) AS opened
               FROM access_pass WHERE status='released' AND confirmed_at>=? GROUP BY device_id""",
            (now - GATE_SECONDS,),
        )
        return {
            "devices": [{"id": item.id, "name": item.name} for item in self.hub.config.values() if item.domain == "safety"],
            "gates": {row["device_id"]: row["opened"] + GATE_SECONDS for row in active},
            "passes": rows, "demo": self.demo,
        }

    def issue_pass(self, device_id: str, actor: str, purpose: str, minutes: int) -> dict:
        device = self.hub.config.get(device_id)
        if not device or device.domain != "safety":
            raise ValueError("请选择已登记的安全通道。")
        now, pass_id, code = time.time(), uuid4().hex, secrets.token_hex(8).upper()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO access_pass(id,device_id,digest,purpose,issued_by,issued_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                (pass_id, device_id, sha256(code.encode()).hexdigest(), purpose, actor, now, now + minutes * 60),
            )
            self.store._audit(actor, "签发通行码", f"{device_id} · {pass_id[:8]}")
        return {**self._pass(pass_id), "code": code}

    def verify_pass(self, code: str, actor: str) -> dict:
        normalized = re.sub(r"[\s-]+", "", code).upper()
        if not re.fullmatch(r"[0-9A-F]{16}", normalized):
            raise ValueError("请填写完整的 16 位通行码。")
        row = self.store.one(
            f"SELECT {PASS_FIELDS} FROM access_pass WHERE digest=?", (sha256(normalized.encode()).hexdigest(),),
        )
        now = time.time()
        if not row or row["expires_at"] < now:
            raise ValueError("通行码无效或已过期。")
        if row["status"] != "issued":
            raise ValueError("通行码已经核验，请查看通行记录。")
        with self.store.db:
            self.store.db.execute(
                "UPDATE access_pass SET status='verified',verified_by=?,verified_at=? WHERE id=?",
                (actor, now, row["id"]),
            )
            self.store._audit(actor, "核验通行码", f"{row['device_id']} · {row['id'][:8]}")
        return self._pass(row["id"])

    def confirm_pass(self, pass_id: str, actor: str) -> dict:
        row = self._pass(pass_id)
        if row is None:
            raise LookupError("找不到该通行记录。")
        if row["verified_by"] != actor and actor != "admin":
            raise PermissionError("请由核验人或管理员确认通行。")
        if row["status"] in ("released", "recorded", "rejected"):
            return row
        now = time.time()
        if row["status"] != "verified" or row["expires_at"] < now:
            raise ValueError("请先核验有效通行码。")
        status, detail = "recorded", "已完成人工通行登记。"
        if self.demo:
            current = self.hub.current(row["device_id"])
            sample = current["latest"]
            if not current["fresh"] or sample["obstruction"] != 0 or sample["presence"] != 0:
                status, detail = "rejected", "联锁拒绝，通道离线、有人或遮挡未清除。"
            elif sample["temperature"] is None or sample["temperature"] > self.store.rules["temperature_high"]:
                status, detail = "rejected", "联锁拒绝，通道温度读数不可用或越限。"
            elif row["device_id"] in self.access()["gates"]:
                status, detail = "rejected", "上一条模拟放行还未结束。"
            else:
                status, detail = "released", "模拟闸机开放 3 秒，随后自动关闭。"
        with self.store.db:
            self.store.db.execute(
                "UPDATE access_pass SET status=?,confirmed_at=?,detail=? WHERE id=?", (status, now, detail, pass_id),
            )
            self.store._audit(actor, "确认通行", f"{row['device_id']} · {pass_id[:8]} · {detail}")
            if status in ("released", "recorded"):
                self.store.event(row["device_id"], "pass", f"通行核验 {pass_id[:8]} · {detail}")
        return self._pass(pass_id)

    def _pass(self, pass_id: str) -> dict | None:
        return self.store.one(f"SELECT {PASS_FIELDS} FROM access_pass WHERE id=?", (pass_id,))
