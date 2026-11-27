from argparse import ArgumentParser
from getpass import getpass
from urllib.parse import quote, urlsplit
import json

import httpx

from factory.config import DEMO_ACCOUNTS, internal_address


def main():
    parser = ArgumentParser(description="蜀云智造终端工作台")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="厂区内网的后台地址")
    parser.add_argument("--user", default="operator")
    parser.add_argument("--demo", action="store_true", help="使用本机演示账户")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="查询设备在线和读数")
    ledger = sub.add_parser("business", help="查询产量和库存台账")
    ledger.add_argument("--day")
    text = sub.add_parser("ask", help="向值班智能体问数或生成指令预览")
    text.add_argument("device")
    text.add_argument("text")
    text.add_argument("--agent", choices=("auto", "device", "safety", "energy"), default="auto")
    logs = sub.add_parser("records", help="查询历史留痕")
    logs.add_argument("kind", choices=("events", "alerts", "commands", "conversations", "passes", "audit"))
    logs.add_argument("--query", default="")
    approval = sub.add_parser("confirm", help="核对预览并确认下发")
    approval.add_argument("command_id")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (url.scheme not in ("http", "https") or not url.hostname or not internal_address(url.hostname)
            or url.username or url.password or url.query or url.fragment):
        parser.error("请填写本机或厂区内网的 HTTP(S) 地址。")
    if args.demo:
        if url.hostname not in ("localhost", "127.0.0.1", "::1") or args.user not in DEMO_ACCOUNTS:
            parser.error("演示账户使用本机服务地址及 admin、operator 或 viewer。")
        password = DEMO_ACCOUNTS[args.user]
    else:
        password = getpass("后台账户密码 ")
    try:
        with httpx.Client(base_url=args.url, timeout=20, trust_env=False) as client:
            client.post("/api/login", json={"username": args.user, "password": password}).raise_for_status()
            match args.action:
                case "status":
                    data = client.get("/api/state").raise_for_status().json()["devices"]
                case "business":
                    params = {"day": args.day} if args.day else {}
                    data = client.get("/api/business", params=params).raise_for_status().json()
                case "ask":
                    data = client.post("/api/assistant", json={
                        "device_id": args.device, "agent": args.agent, "text": args.text,
                    }).raise_for_status().json()
                case "records":
                    data = client.get("/api/records", params={"kind": args.kind, "q": args.query}).raise_for_status().json()
                case "confirm":
                    path = "/api/commands/" + quote(args.command_id, safe="")
                    command = client.get(path).raise_for_status().json()
                    print(json.dumps(command, ensure_ascii=False, indent=2))
                    if input("核对指令与设备。输入 CONFIRM 下发 ").strip() != "CONFIRM":
                        return
                    data = client.post(path + "/confirm").raise_for_status().json()
            print(json.dumps(data, ensure_ascii=False, indent=2))
    except httpx.HTTPStatusError as error:
        try:
            detail = error.response.json().get("detail")
        except ValueError:
            detail = None
        parser.exit(1, f"{detail or error.response.status_code}\n")
    except httpx.RequestError:
        parser.exit(1, "无法连接后台，检查项目是否正在运行及地址端口。\n")


if __name__ == "__main__":
    main()
