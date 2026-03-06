homeassistant: true
frontend: true

mqtt:
  server: mqtt://mosquitto:1883
  user: ${MQTT_USER}
  password: ${MQTT_PASSWORD}

serial:
  port: ${SMLIGHT_SERIAL_URL}
  baudrate: 115200
  adapter: ember
  disable_led: false

advanced:
  transmit_power: 20
  network_key: ${Z2M_NETWORK_KEY}