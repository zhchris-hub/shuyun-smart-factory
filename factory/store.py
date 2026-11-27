from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import time
from uuid import uuid4

from factory.config import DEFAULT_RULES, ENERGY_NOTE, OUTPUTS, DeviceConfig


LOCAL_TZ = timezone(timedelta(hours=8), "CST")
METRIC_LABELS = {"temperature": "温度", "humidity": "湿度", "light": "光照"}
UNITS = {"temperature": "°C", "humidity": "%", "light": "ADC"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES device(id),
    at REAL NOT NULL,
    temperature REAL,
    humidity REAL,
    light INTEGER,
    presence INTEGER,
    obstruction INTEGER,
    outputs TEXT NOT NULL,
    energy REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS telemetry_device_at ON telemetry(device_id, at DESC, id DESC);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    device_id TEXT NOT NULL REFERENCES device(id),
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES device(id),
    metric TEXT NOT NULL,
    direction TEXT NOT NULL,
    opened_at REAL NOT NULL,
    value REAL NOT NULL,
    threshold REAL NOT NULL,
    resolved_at REAL,
    closed_at REAL,
    closed_by TEXT,
    note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS alert_active ON alert(device_id, metric, direction)
    WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS command (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES device(id),
    actuator TEXT NOT NULL,
    value INTEGER NOT NULL,
    created_at REAL NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    confirmed_by TEXT,
    sent_at REAL,
    finished_at REAL,
    detail TEXT,
    origin TEXT NOT NULL DEFAULT 'manual',
    decision_key TEXT
);
CREATE INDEX IF NOT EXISTS command_created ON command(created_at DESC);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    actor TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES device(id),
    agent TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setting (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def day_bounds(day: str | None = None) -> tuple[str, float, float]:
    date = datetime.now(LOCAL_TZ).date() if day is None else datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime(date.year, date.month, date.day, tzinfo=LOCAL_TZ)
    return date.isoformat(), start.timestamp(), (start + timedelta(days=1)).timestamp()


def formatted_time(at: float | None) -> str:
    return datetime.fromtimestamp(at, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S") if at is not None else "—"


def estimated_watts(outputs: dict[str, int]) -> float:
    return 6 * outputs.get("led", 0) / 100 + 40 * (outputs.get("relay1", 0) + outputs.get("relay2", 0)) + 0.25 * outputs.get("buzzer", 0)


class Store:
    def __init__(self, path: Path, devices: tuple[DeviceConfig, ...], demo: bool):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.rows("PRAGMA table_info(command)")}
        with self.db:
            if "origin" not in columns:
                self.db.execute("ALTER TABLE command ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
            if "decision_key" not in columns:
                self.db.execute("ALTER TABLE command ADD COLUMN decision_key TEXT")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS command_decision ON command(decision_key)")
        self.rules = dict(DEFAULT_RULES)
        saved = self.one("SELECT value FROM setting WHERE name='thresholds'")
        if saved:
            self.rules.update(json.loads(saved["value"]))
        kind = "simulator" if demo else "esp32"
        with self.db:
            # Keep recorded history if an installation later changes its device registry.
            self.db.executemany(
                """INSERT INTO device(id,name,domain,kind) VALUES (?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, domain=excluded.domain, kind=excluded.kind""",
                [(device.id, device.name, device.domain, kind) for device in devices],
            )
            self.db.execute(
                "UPDATE command SET status='expired',finished_at=?,detail=? WHERE status='draft'",
                (time.time(), "服务重新启动，请重新预览。"),
            )
        for row in self.rows("SELECT id FROM command WHERE status='sent'"):
            self.finish_command(row["id"], "unknown", "服务重新启动，执行结果待核实。")

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, args)]

    def close(self):
        self.db.close()

    def event(self, device_id: str, kind: str, message: str, at: float | None = None):
        self.db.execute(
            "INSERT INTO event(at,device_id,kind,message) VALUES (?,?,?,?)",
            (time.time() if at is None else at, device_id, kind, message),
        )

    def _audit(self, actor: str, action: str, detail: str):
        self.db.execute(
            "INSERT INTO audit(at,actor,action,detail) VALUES (?,?,?,?)",
            (time.time(), actor, action, detail),
        )

    def audit(self, actor: str, action: str, detail: str):
        with self.db:
            self._audit(actor, action, detail)

    def device_event(self, device_id: str, kind: str, message: str):
        with self.db:
            self.event(device_id, kind, message)

    def latest(self, device_id: str) -> dict | None:
        row = self.one(
            "SELECT * FROM telemetry WHERE device_id=? ORDER BY at DESC, id DESC LIMIT 1", (device_id,),
        )
        if row:
            row["outputs"] = json.loads(row["outputs"])
        return row

    def breaches(self, values: dict) -> dict[tuple[str, str], tuple[float, float]]:
        boundaries = (
            ("temperature", "high", self.rules["temperature_high"]),
            ("humidity", "low", self.rules["humidity_low"]),
            ("humidity", "high", self.rules["humidity_high"]),
            ("light", "low", self.rules["light_low"]),
        )
        found = {}
        for metric, direction, limit in boundaries:
            value = values.get(metric)
            if value is None:
                continue
            if (direction == "high" and value > limit) or (direction == "low" and value < limit):
                found[metric, direction] = (value, limit)
        return found

    def _check_alerts(self, device_id: str, values: dict, at: float):
        breaches = self.breaches(values)
        active = {
            (row["metric"], row["direction"]): row
            for row in self.rows("SELECT * FROM alert WHERE device_id=? AND resolved_at IS NULL", (device_id,))
        }
        for (metric, direction), row in active.items():
            if (metric, direction) not in breaches and values.get(metric) is not None:
                self.db.execute("UPDATE alert SET resolved_at=? WHERE id=?", (at, row["id"]))
                self.event(device_id, "recovered", f"{METRIC_LABELS[metric]}恢复阈值范围，等待处置记录。", at)
        for (metric, direction), (value, limit) in breaches.items():
            if (metric, direction) in active:
                continue
            self.db.execute(
                "INSERT INTO alert(device_id,metric,direction,opened_at,value,threshold) VALUES (?,?,?,?,?,?)",
                (device_id, metric, direction, at, value, limit),
            )
            symbol = "高于" if direction == "high" else "低于"
            self.event(device_id, "alarm", f"{METRIC_LABELS[metric]} {value:g} {UNITS[metric]} {symbol}阈值 {limit:g}。", at)

    def ingest(self, device_id: str, values: dict, at: float | None = None, continuous: bool = True):
        at = time.time() if at is None else at
        previous = self.latest(device_id) if continuous else None
        seconds = min(max(at - previous["at"], 0), 5) if previous else 0
        energy = estimated_watts(previous["outputs"]) * seconds / 3_600_000 if previous else 0
        with self.db:
            self.db.execute(
                """INSERT INTO telemetry(device_id,at,temperature,humidity,light,presence,obstruction,outputs,energy)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (device_id, at, values["temperature"], values["humidity"], values["light"], values["presence"],
                 values["obstruction"], json.dumps(values["outputs"]), energy),
            )
            if previous and values["presence"] is not None and previous["presence"] is not None:
                if values["presence"] and not previous["presence"]:
                    self.event(device_id, "entry", "人体感应触发，记为一次出现事件。", at)
                elif previous["presence"] and not values["presence"]:
                    self.event(device_id, "exit", "人体感应恢复无人。", at)
            if values["obstruction"] is not None and (not previous or previous["obstruction"] != values["obstruction"]):
                if values["obstruction"]:
                    self.event(device_id, "obstruction", "红外检测到遮挡。", at)
                elif previous and previous["obstruction"]:
                    self.event(device_id, "clear", "红外遮挡解除。", at)
            self._check_alerts(device_id, values, at)
        return at

    def trend(self, device_id: str, seconds: int) -> list[dict]:
        result = self.rows(
            """SELECT at,temperature,humidity,light FROM telemetry WHERE device_id=? AND at>=?
               ORDER BY at DESC,id DESC LIMIT 1200""", (device_id, time.time() - seconds),
        )
        return list(reversed(result))

    def alerts(self, limit: int = 50, only_open: bool = True) -> list[dict]:
        where = "WHERE a.closed_at IS NULL" if only_open else ""
        return self.rows(
            f"""SELECT a.*, d.name AS device_name FROM alert a JOIN device d ON d.id=a.device_id
                {where} ORDER BY a.opened_at DESC LIMIT ?""", (limit,),
        )

    def close_alert(self, alert_id: int, actor: str, note: str) -> dict:
        alert = self.one("SELECT * FROM alert WHERE id=?", (alert_id,))
        if not alert:
            raise LookupError("找不到该告警。")
        if alert["closed_at"] or not alert["resolved_at"]:
            raise ValueError("告警恢复到正常范围后才能关闭工单。")
        at = time.time()
        with self.db:
            self.db.execute(
                "UPDATE alert SET closed_at=?,closed_by=?,note=? WHERE id=?", (at, actor, note, alert_id),
            )
            self._audit(actor, "关闭工单", f"告警 #{alert_id} · {note}")
        return self.one("SELECT * FROM alert WHERE id=?", (alert_id,))

    def set_rules(self, rules: dict, actor: str):
        with self.db:
            self.db.execute(
                """INSERT INTO setting(name,value) VALUES ('thresholds',?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (json.dumps(rules),),
            )
            self._audit(actor, "更新阈值", json.dumps(rules, ensure_ascii=False))
        self.rules = rules

    def draft(self, device_id: str, actuator: str, value: int, actor: str, *,
              origin: str = "manual", decision_key: str | None = None) -> dict:
        command_id = uuid4().hex
        with self.db:
            if decision_key:
                previous = self.one("SELECT * FROM command WHERE decision_key=?", (decision_key,))
                if previous:
                    return previous
            self.db.execute(
                """INSERT INTO command(id,device_id,actuator,value,created_at,actor,status,origin,decision_key)
                   VALUES (?,?,?,?,?,?,'draft',?,?)""",
                (command_id, device_id, actuator, value, time.time(), actor, origin, decision_key),
            )
            action = "采纳建议并预览" if origin == "decision" else "预览指令"
            self._audit(actor, action, f"{device_id} · {OUTPUTS[actuator]}={value} · {command_id[:8]}")
        return self.command(command_id)

    def command(self, command_id: str) -> dict | None:
        return self.one("SELECT * FROM command WHERE id=?", (command_id,))

    def expire_drafts(self, cutoff: float):
        with self.db:
            self.db.execute(
                """UPDATE command SET status='expired',finished_at=?,detail=?
                   WHERE status='draft' AND created_at<?""",
                (time.time(), "预览已超时，请重新预览。", cutoff),
            )

    def mark_sent(self, command_id: str, actor: str):
        with self.db:
            self.db.execute(
                "UPDATE command SET status='sent',confirmed_by=?,sent_at=? WHERE id=?",
                (actor, time.time(), command_id),
            )
            self._audit(actor, "确认并下发", f"指令 {command_id[:8]}")

    def finish_command(self, command_id: str, status: str, detail: str, actor: str = "系统"):
        with self.db:
            self.db.execute(
                "UPDATE command SET status=?,finished_at=?,detail=? WHERE id=?",
                (status, time.time(), detail, command_id),
            )
            self._audit(actor, f"指令 {status}", f"{command_id[:8]} · {detail}")

    def conversation(self, actor: str, device_id: str, agent: str, question: str, answer: str, source: str) -> int:
        with self.db:
            row = self.db.execute(
                "INSERT INTO conversation(at,actor,device_id,agent,question,answer,source) VALUES (?,?,?,?,?,?,?)",
                (time.time(), actor, device_id, agent, question, answer, source),
            )
            return row.lastrowid

    def dashboard_summary(self, device_id: str | None = None, day: str | None = None) -> dict:
        label, start, end = day_bounds(day)
        filter_device = " AND device_id=?" if device_id else ""
        args = (start, end, device_id) if device_id else (start, end)
        telemetry = self.one(
            f"""SELECT COUNT(*) AS samples,COALESCE(SUM(energy),0) AS kwh,AVG(temperature) AS avg_temperature,
                MIN(temperature) AS min_temperature,MAX(temperature) AS max_temperature,
                AVG(humidity) AS avg_humidity,AVG(light) AS avg_light,MAX(at) AS updated_at
                FROM telemetry WHERE at>=? AND at<?{filter_device}""", args,
        )
        event_counts = self.rows(f"SELECT kind,COUNT(*) AS count FROM event WHERE at>=? AND at<?{filter_device} GROUP BY kind", args)
        alert_count = self.one(f"SELECT count(*) AS count FROM alert WHERE opened_at>=? AND opened_at<?{filter_device}", args)
        hourly = self.rows(
            f"""SELECT CAST((at-?)/3600 AS INTEGER) AS hour,SUM(energy) AS kwh FROM telemetry
                WHERE at>=? AND at<?{filter_device} GROUP BY hour ORDER BY hour""", (start, *args),
        )
        return {
            **telemetry, "day": label, "events": {row["kind"]: row["count"] for row in event_counts},
            "alerts": alert_count["count"], "hourly": hourly, "estimated": True, "energy_note": ENERGY_NOTE,
        }
