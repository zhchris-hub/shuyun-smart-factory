# Copy to config.py. Match all GPIO numbers and active levels to your kit.
WIFI_SSID = ""
WIFI_PASSWORD = ""
SERVER_HOST = "192.168.1.100"
SERVER_PORT = 9000
DEVICE_ID = "esp32-shop-01"
DEVICE_TOKEN = ""

# Example placement for an ESP32-WROOM-32. None disables that peripheral.
DHT_PIN = 27
LIGHT_PIN = 34
PIR_PIN = 32
IR_PIN = 33
PIR_ACTIVE = 1
IR_ACTIVE = 0
I2C_SDA = 21
I2C_SCL = 22
OLED_ADDRESS = 0x3C

LED_PIN = 25
BUZZER_PIN = 26
BUZZER_ACTIVE = 1
ALARM_LED_PIN = None
ALARM_LED_ACTIVE = 1

# Leave relays disabled until the kit wiring and low-voltage load are verified.
# Configuring a relay here also requires adding its capability in devices.json.
RELAY1_PIN = None
RELAY2_PIN = None
RELAY_ACTIVE = 0
