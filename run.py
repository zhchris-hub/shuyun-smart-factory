from argparse import ArgumentParser
from pathlib import Path

import uvicorn

from factory.app import create_app
from factory.config import ROOT, load_settings


def main():
    parser = ArgumentParser(description="启动蜀云智造平台")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="使用本地模拟设备（默认）")
    mode.add_argument("--real", action="store_true", help="接入已登记的 ESP32")
    parser.add_argument("--host", default="127.0.0.1", help="网页服务监听地址")
    parser.add_argument("--port", type=int, default=8000, help="网页服务端口")
    parser.add_argument("--device-host", default="127.0.0.1", help="TCP 设备接入监听地址")
    parser.add_argument("--device-port", type=int, default=9000, help="TCP 设备接入端口")
    parser.add_argument("--data-dir", type=Path, help="SQLite 和 Agentica 数据目录")
    parser.add_argument("--registry", type=Path, default=ROOT / "devices.json", help="真机设备登记文件")
    options = parser.parse_args()
    demo = not options.real
    if demo and (options.host not in ("127.0.0.1", "localhost") or options.device_host != "127.0.0.1"):
        parser.error("演示模式只监听本机地址。")
    if not (1 <= options.port <= 65535 and 1 <= options.device_port <= 65535):
        parser.error("端口须在 1–65535 之间。")
    try:
        settings = load_settings(demo, options.data_dir, options.device_host, options.device_port, options.registry)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(create_app(settings), host=options.host, port=options.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
