import json
import network
import select
import socket
import time

try:
    import config
except ImportError:
    raise RuntimeError("Copy config.example.py to config.py and fill in the kit settings.")

from hardware import Board


class DeviceClient:
    def __init__(self):
        self.board = Board(config)
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        self.poll = select.poll()
        self.sock = None
        self.buffer = b""
        self.online = False
        self.connected = False
        self.seq = 0
        self.last_rx = time.ticks_ms()
        self.retry_at = self.last_rx
        self.retry_ms = 1000
        self.wifi_retry_at = self.last_rx

    def send(self, frame):
        self.sock.sendall((json.dumps(frame) + "\n").encode())

    def _connect(self):
        address = socket.getaddrinfo(config.SERVER_HOST, config.SERVER_PORT)[0][-1]
        self.sock = socket.socket()
        self.sock.settimeout(0.8)
        self.sock.connect(address)
        self.poll.register(self.sock, select.POLLIN)
        self.last_rx = time.ticks_ms()
        self.send({
            "type": "hello", "device_id": config.DEVICE_ID, "token": config.DEVICE_TOKEN,
            "outputs": list(self.board.states),
        })

    def _disconnect(self):
        if self.sock is not None:
            try:
                self.poll.unregister(self.sock)
            except (KeyError, OSError):
                pass
            self.sock.close()
        self.sock = None
        self.buffer = b""
        self.online = self.connected = False
        self.seq = 0
        self.board.stop_remote_outputs()
        self.board.draw(False)
        self.retry_at = time.ticks_add(time.ticks_ms(), self.retry_ms)
        self.retry_ms = min(self.retry_ms * 2, 16000)

    def _accept(self, frame):
        kind = frame.get("type")
        if kind == "welcome" and frame.get("device_id") == config.DEVICE_ID:
            self.connected = True
            self.retry_ms = 1000
            print("Service connected")
        elif kind == "telemetry_ack" and self.connected:
            self.online = True
        elif kind == "command" and self.online:
            command_id = frame.get("id")
            if isinstance(command_id, str) and len(command_id) == 32:
                ok, detail = self.board.remote_command(frame.get("actuator"), frame.get("value"))
                self.send({
                    "type": "command_ack", "id": command_id, "ok": ok,
                    "outputs": self.board.states, "detail": detail,
                })
        elif kind == "error":
            raise OSError("Device registration rejected")
        if kind in ("welcome", "telemetry_ack") and self.connected:
            self.last_rx = time.ticks_ms()
            self.board.rules.update(frame.get("rules", {}))

    def _receive(self):
        chunk = self.sock.recv(1024)
        if not chunk:
            raise OSError("TCP disconnected")
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > 8192:
                raise ValueError("Frame too long")
            self._accept(json.loads(line.decode()))
        if len(self.buffer) >= 8192:
            raise ValueError("Frame too long")

    def run(self):
        if not config.WIFI_SSID or not config.DEVICE_TOKEN:
            print("Configure Wi-Fi and DEVICE_TOKEN in config.py. Sampling locally.")
        next_sample = time.ticks_ms()
        try:
            while True:
                now = time.ticks_ms()
                self.board.tick()
                try:
                    if self.sock:
                        if not self.wlan.isconnected() or time.ticks_diff(now, self.last_rx) > 8000:
                            raise OSError("Connection watchdog")
                        if self.poll.poll(0):
                            self._receive()
                    elif config.WIFI_SSID and config.DEVICE_TOKEN:
                        if self.wlan.isconnected():
                            if time.ticks_diff(now, self.retry_at) >= 0:
                                self._connect()
                        elif time.ticks_diff(now, self.wifi_retry_at) >= 0:
                            self.wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
                            self.wifi_retry_at = time.ticks_add(now, 10000)
                    if time.ticks_diff(now, next_sample) >= 0:
                        sample = self.board.sample(self.online)
                        next_sample = time.ticks_add(now, 2000)
                        if self.connected:
                            self.send(dict(sample, type="telemetry", seq=self.seq))
                            self.seq += 1
                        print("sample", sample["temperature"], sample["humidity"], sample["light"], self.online)
                except (OSError, ValueError) as exc:
                    print("Device link:", exc)
                    self._disconnect()
                    self.wifi_retry_at = time.ticks_add(now, 2000)
                time.sleep_ms(35)
        finally:
            self._disconnect()


DeviceClient().run()
