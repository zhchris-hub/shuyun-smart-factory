from machine import ADC, I2C, PWM, Pin
import dht
import framebuf
import time


DEFAULT_RULES = {"temperature_high": 32, "humidity_low": 30, "humidity_high": 85, "light_low": 600}


class Display(framebuf.FrameBuffer):
    def __init__(self, i2c, address):
        self.i2c, self.address = i2c, address
        self.buffer = bytearray(1 + 128 * 64 // 8)
        self.buffer[0] = 0x40
        super().__init__(memoryview(self.buffer)[1:], 128, 64, framebuf.MONO_VLSB)
        # SSD1306 128x64, horizontal addressing, internal charge pump.
        for cmd in (
            0xAE, 0x20, 0x00, 0x40, 0xA1, 0xA8, 0x3F, 0xC8, 0xD3, 0x00,
            0xDA, 0x12, 0xD5, 0x80, 0xD9, 0xF1, 0xDB, 0x30, 0x81,
            0xCF, 0xA4, 0xA6, 0x8D, 0x14, 0xAF,
        ):
            self.command(cmd)
        self.fill(0)
        self.show()

    def command(self, value):
        self.i2c.writeto(self.address, bytes((0x80, value)))

    def show(self):
        for cmd in (0x21, 0, 127, 0x22, 0, 7):
            self.command(cmd)
        self.i2c.writeto(self.address, self.buffer)


def _number(value, suffix):
    return "--" if value is None else str(value) + suffix


class Board:
    def __init__(self, config):
        pins = (
            config.DHT_PIN, config.LIGHT_PIN, config.PIR_PIN, config.IR_PIN, config.I2C_SDA, config.I2C_SCL,
            config.LED_PIN, config.BUZZER_PIN, config.ALARM_LED_PIN, config.RELAY1_PIN, config.RELAY2_PIN,
        )
        active_pins = [pin for pin in pins if pin is not None]
        if len(set(active_pins)) != len(active_pins):
            raise ValueError("Duplicate GPIO in config.py")
        if config.LIGHT_PIN is not None and config.LIGHT_PIN not in (32, 33, 34, 35, 36, 39):
            raise ValueError("Use an ADC1 GPIO for light sensing with Wi-Fi on this ESP32")
        if (config.I2C_SDA is None) != (config.I2C_SCL is None):
            raise ValueError("Configure both OLED I2C pins or disable both")
        self.config = config
        self.switches = {}
        self.states = {}
        for key, gpio, level in (
            ("relay1", config.RELAY1_PIN, config.RELAY_ACTIVE), ("relay2", config.RELAY2_PIN, config.RELAY_ACTIVE),
            ("buzzer", config.BUZZER_PIN, config.BUZZER_ACTIVE),
        ):
            if gpio is not None:
                self.switches[key] = (Pin(gpio, Pin.OUT, value=1 - level), level)
                self.states[key] = 0
        self.led = None
        if config.LED_PIN is not None:
            self.led = PWM(Pin(config.LED_PIN, Pin.OUT, value=0), freq=1000, duty_u16=0)
            self.states["led"] = 0
        self.alarm_led = Pin(config.ALARM_LED_PIN, Pin.OUT, value=1 - config.ALARM_LED_ACTIVE) if config.ALARM_LED_PIN is not None else None
        self.dht = dht.DHT11(Pin(config.DHT_PIN)) if config.DHT_PIN is not None else None
        self.light = ADC(Pin(config.LIGHT_PIN)) if config.LIGHT_PIN is not None else None
        if self.light:
            self.light.atten(ADC.ATTN_11DB)
        self.pir = Pin(config.PIR_PIN, Pin.IN) if config.PIR_PIN is not None else None
        self.ir = Pin(config.IR_PIN, Pin.IN) if config.IR_PIN is not None else None
        self.display = None
        if config.I2C_SDA is not None:
            try:
                bus = I2C(0, sda=Pin(config.I2C_SDA), scl=Pin(config.I2C_SCL), freq=400000)
                self.display = Display(bus, config.OLED_ADDRESS)
            except OSError:
                print("OLED unavailable. Check I2C pins, power and address.")
        self.values = dict(temperature=None, humidity=None, light=None, presence=None, obstruction=None)
        self.rules = dict(DEFAULT_RULES)
        self.buzzer_until = None
        self.alarm = False

    def set_output(self, key, value):
        if key == "led":
            self.led.duty_u16(value * 65535 // 100)
        else:
            pin, level = self.switches[key]
            pin.value(level if value else 1 - level)
        self.states[key] = value

    def stop_remote_outputs(self):
        for key in self.states:
            self.set_output(key, 0)
        self.buzzer_until = None

    def remote_command(self, key, value):
        if key not in self.states or type(value) is not int or not (0 <= value <= (100 if key == "led" else 1)):
            return False, "Unsupported output or value"
        if key in ("relay1", "relay2") and value:
            other = "relay2" if key == "relay1" else "relay1"
            if self.states.get(other) or not self.relay_safe():
                return False, "ESP32 interlock prevented activation"
        self.set_output(key, value)
        if key == "buzzer":
            self.buzzer_until = time.ticks_add(time.ticks_ms(), 3000) if value else None
        return True, ""

    def relay_safe(self):
        temp = self.values["temperature"]
        return (
            temp is not None and temp <= self.rules["temperature_high"] and
            self.pir is not None and self.pir.value() != self.config.PIR_ACTIVE and
            self.ir is not None and self.ir.value() != self.config.IR_ACTIVE
        )

    def tick(self):
        if self.buzzer_until is not None and time.ticks_diff(time.ticks_ms(), self.buzzer_until) >= 0:
            self.set_output("buzzer", 0)
            self.buzzer_until = None
        if (self.states.get("relay1") or self.states.get("relay2")) and not self.relay_safe():
            for key in ("relay1", "relay2"):
                if self.states.get(key):
                    self.set_output(key, 0)

    def sample(self, online):
        if self.dht is not None:
            try:
                self.dht.measure()
                self.values["temperature"], self.values["humidity"] = self.dht.temperature(), self.dht.humidity()
            except OSError:
                self.values["temperature"] = self.values["humidity"] = None
        if self.light is not None:
            self.values["light"] = self.light.read_u16() >> 4
        if self.pir is not None:
            self.values["presence"] = self.pir.value() == self.config.PIR_ACTIVE
        if self.ir is not None:
            self.values["obstruction"] = self.ir.value() == self.config.IR_ACTIVE
        t, h, light = self.values["temperature"], self.values["humidity"], self.values["light"]
        alert = (
            (t is not None and t > self.rules["temperature_high"]) or
            (h is not None and not self.rules["humidity_low"] <= h <= self.rules["humidity_high"]) or
            (light is not None and light < self.rules["light_low"])
        )
        if self.alarm_led is not None:
            level = self.config.ALARM_LED_ACTIVE
            self.alarm_led.value(level if alert else 1 - level)
        if alert and not self.alarm and "buzzer" in self.states and not self.states["buzzer"]:
            self.set_output("buzzer", 1)
            self.buzzer_until = time.ticks_add(time.ticks_ms(), 750)
        self.alarm = alert
        self.tick()
        self.draw(online)
        return dict(self.values, outputs=dict(self.states))

    def draw(self, online):
        if self.display is None:
            return
        self.display.fill(0)
        self.display.text("ESP32  " + ("ONLINE" if online else "OFFLINE"), 0, 0)
        self.display.text("TEMP " + _number(self.values["temperature"], " C"), 0, 16)
        self.display.text("HUM  " + _number(self.values["humidity"], " %"), 0, 32)
        light = _number(self.values["light"], "")
        self.display.text("ADC " + light + ("  AL" if self.alarm else ""), 0, 48)
        try:
            self.display.show()
        except OSError:
            pass
