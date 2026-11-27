from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import ipaddress
import json
import os
import re

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
DOMAINS = {"workshop": "车间", "warehouse": "仓库", "safety": "安全"}
OUTPUTS = {"led": "补光 LED", "buzzer": "蜂鸣器", "relay1": "继电器 1", "relay2": "继电器 2"}
ROLES = {"admin": "管理员", "operator": "操作员", "viewer": "观察员"}
DEMO_ACCOUNTS = {
    "admin": "shuyun-demo-admin",
    "operator": "shuyun-demo",
    "viewer": "shuyun-demo-viewer",
}
DEFAULT_RULES = {"temperature_high": 32.0, "humidity_low": 30.0, "humidity_high": 85.0, "light_low": 600}
ENERGY_NOTE = "估算口径：补光灯满功率 6 W、每路继电器负载 40 W、蜂鸣器 0.25 W，按设备回报的状态和采样间隔积分。"


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    name: str
    domain: str
    token: str = field(repr=False)
    outputs: tuple[str, ...] = ()


DEMO_DEVICES = tuple(
    DeviceConfig(device_id, name, domain, "local-training-link", tuple(OUTPUTS))
    for device_id, name, domain in (
        ("sim-shop-01", "装配车间终端", "workshop"),
        ("sim-stock-01", "仓库环境终端", "warehouse"),
        ("sim-safety-01", "安全通道终端", "safety"),
    )
)


@dataclass(frozen=True)
class ModelConfig:
    id: str
    base_url: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class SkillConfig:
    token: str = field(default="", repr=False)
    remote_url: str = ""
    remote_token: str = field(default="", repr=False)


@dataclass(frozen=True)
class Settings:
    demo: bool
    data_dir: Path
    tcp_host: str
    tcp_port: int
    accounts: dict[str, str] = field(repr=False)
    devices: tuple[DeviceConfig, ...] = ()
    model: ModelConfig | None = None
    skills: SkillConfig = field(default_factory=SkillConfig)


def read_devices(path: Path) -> tuple[DeviceConfig, ...]:
    if not path.is_file():
        raise ValueError("请将 devices.example.json 复制为 devices.json，登记设备和连接令牌。")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("devices.json 至少需要登记一台设备。")
    devices: dict[str, DeviceConfig] = {}
    tokens: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("devices.json 中的设备必须是 JSON 对象。")
        device_id = item.get("id", "")
        token = item.get("token", "")
        domain = item.get("domain")
        name = item.get("name", "")
        outputs = item.get("outputs", [])
        if not isinstance(device_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", device_id):
            raise ValueError("设备 ID 只能用小写字母、数字、短横线和下划线，最多 48 字符。")
        if device_id in devices or device_id.startswith("sim-"):
            raise ValueError("实物设备 ID 必须唯一，并使用与模拟设备不同的前缀。")
        if not isinstance(token, str) or not 20 <= len(token) <= 256 or token.startswith("replace-") or token in tokens:
            raise ValueError("为每台真机设置 20–256 字符的独立随机令牌。")
        if not isinstance(domain, str) or domain not in DOMAINS or not isinstance(name, str) or not name.strip():
            raise ValueError("每台设备都需要名称及 workshop、warehouse、safety 之一的场景。")
        if not isinstance(outputs, list) or any(not isinstance(entry, str) or entry not in OUTPUTS for entry in outputs):
            raise ValueError("设备执行器只能选 led、buzzer、relay1、relay2。")
        devices[device_id] = DeviceConfig(device_id, name.strip(), domain, token, tuple(dict.fromkeys(outputs)))
        tokens.add(token)
    return tuple(devices.values())


def internal_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or any(
            address in ipaddress.ip_network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        )
    except ValueError:
        return host == "localhost"


def read_model() -> ModelConfig | None:
    if os.environ.get("FACTORY_MODEL_ENABLED") != "1":
        return None
    base_url = os.environ.get("FACTORY_MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    url = urlsplit(base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("模型地址须为 OpenAI 兼容 API 的 HTTP(S) 基础地址。")
    inside = internal_address(url.hostname)
    if not inside and (os.environ.get("FACTORY_ALLOW_EXTERNAL_MODEL") != "1" or url.scheme != "https"):
        raise ValueError("外部模型需要 HTTPS，并须明确启用 FACTORY_ALLOW_EXTERNAL_MODEL。")
    key = os.environ.get("FACTORY_MODEL_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not key and not inside:
        raise ValueError("配置外部模型前需要填写 FACTORY_MODEL_API_KEY。")
    return ModelConfig(os.environ.get("FACTORY_MODEL_ID", "qwen-plus"), base_url, key or "local")


def read_skills() -> SkillConfig:
    token = os.environ.get("FACTORY_SKILL_TOKEN", "")
    if token and (len(token) < 20 or not re.fullmatch(r"[a-zA-Z0-9._~-]+", token)):
        raise ValueError("FACTORY_SKILL_TOKEN 须为至少 20 位的随机 URL 安全字符。")
    remote = os.environ.get("FACTORY_REMOTE_MCP_URL", "").strip()
    if remote:
        parsed = urlsplit(remote)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or not internal_address(parsed.hostname)
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("FACTORY_REMOTE_MCP_URL 须填写受信的厂区内网 IP 或 localhost 的 HTTP(S) 地址。")
    return SkillConfig(token, remote, os.environ.get("FACTORY_REMOTE_MCP_TOKEN", ""))


def load_settings(
    demo: bool, data_dir: Path | None, tcp_host: str, tcp_port: int, registry: Path,
) -> Settings:
    load_dotenv(ROOT / ".env")
    if demo:
        accounts, devices = dict(DEMO_ACCOUNTS), DEMO_DEVICES
    else:
        accounts = {role: os.environ.get(f"FACTORY_{role.upper()}_PASSWORD", "") for role in ROLES}
        if any(not 12 <= len(password) <= 300 for password in accounts.values()) or len(set(accounts.values())) < len(accounts):
            raise ValueError("请在 .env 中给管理员、操作员和观察员分别配置 12–300 字符的不同密码。")
        devices = read_devices(registry)
    return Settings(
        demo, data_dir or ROOT / ".data" / ("demo" if demo else "factory"),
        tcp_host, tcp_port, accounts, devices, read_model(), read_skills(),
    )
