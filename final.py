import os
import time
import requests
import sys
import platform
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from flask_sqlalchemy import SQLAlchemy
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ==============================
# Platform detection (WIN-COMPAT)
# ==============================
IS_WINDOWS = platform.system().lower().startswith('win')
IS_LINUX = platform.system().lower().startswith('lin')

# ---- Hardware sensor imports (guarded) ----
# On Raspberry Pi: requires libgpiod2 and adafruit-circuitpython-dht
DHT_AVAILABLE = False
if not IS_WINDOWS:
    try:
        import adafruit_dht
        import board
        DHT_AVAILABLE = True
    except Exception:
        DHT_AVAILABLE = False
else:
    adafruit_dht = None
    board = None

# ==============================
# Configuration (can override with environment variables)
# ==============================
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', "rajputpraful791@gmail.com")
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL', "reetakrana65@gmail.com")
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', "tehj edww iiqu cwgy") # Gmail App Password recommended
OPENWEATHER_API_KEY = os.environ.get('OPENWEATHER_API_KEY', '32322b3a704cf8713c242c2f1c2bfae7')

# Hardware config
DHT_GPIO = os.environ.get('DHT_GPIO', 'GPIO17')  # DHT22 pin, e.g., GPIO17 on Pi
PREFER_DS18B20 = os.environ.get('PREFER_DS18B20', 'false').lower() == 'true'
SIMULATE_SENSOR = os.environ.get('SIMULATE_SENSOR', 'auto').lower()  # 'auto' | 'true' | 'false'
USE_WEATHER = os.environ.get('USE_WEATHER', 'true').lower() == 'true'  # toggle weather coupling

def _should_simulate():
    if SIMULATE_SENSOR == 'true':
        return True
    if SIMULATE_SENSOR == 'false':
        return False
    # auto
    return IS_WINDOWS or not DHT_AVAILABLE

# ==============================
# Single-file sensor helpers
# ==============================
def _read_ds18b20_temp():
    """Read first DS18B20 temperature from 1-Wire filesystem. Returns float °C or None."""
    if not IS_LINUX:
        return None
    try:
        base_dir = '/sys/bus/w1/devices/'
        devices = [d for d in os.listdir(base_dir) if d.startswith('28-')]
        if not devices:
            return None
        device_file = os.path.join(base_dir, devices[0], 'w1_slave')
        with open(device_file, 'r') as f:
            lines = f.read().strip().split('\n')
        if len(lines) < 2:
            return None
        if not lines[0].strip().endswith('YES'):
            return None
        t_pos = lines[1].find('t=')
        if t_pos == -1:
            return None
        t_str = lines[1][t_pos + 2:]
        return int(t_str) / 1000.0
    except Exception:
        return None

def _resolve_board_pin(dht_gpio: str):
    """
    Robustly resolve various pin naming formats to a board.* pin object.
    Accepts: "GPIO17", "D17", "GP17", "17", "GPIO-17" (dashes ignored), etc.
    """
    if board is None:
        return None
    raw = str(dht_gpio).strip().upper().replace('-', '')
    # Strip common prefixes
    for prefix in ("GPIO", "GP", "D", "PIN", "BOARD"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    # Now raw should be mostly numeric
    try:
        num = int(raw)
    except Exception:
        # Fallback: try the original direct attribute
        return getattr(board, dht_gpio, None)
    # Try common attribute names
    for cand in (f"D{num}", f"GPIO{num}", f"GP{num}", f"P{num}", f"IO{num}"):
        pin_obj = getattr(board, cand, None)
        if pin_obj is not None:
            return pin_obj
    # Absolute fallback
    return getattr(board, dht_gpio, None)

class SensorReader:
    """
    Reads from DHT22 on a chosen GPIO; optionally overrides temperature using DS18B20.
    On Windows (or when simulation is enabled), returns simulated readings.
    """
    def _init_(self, dht_gpio='GPIO17', prefer_ds18b20=False):
        self.prefer_ds18b20 = prefer_ds18b20
        self.simulate = _should_simulate()
        self._dht = None
        if not self.simulate:
            try:
                pin_obj = _resolve_board_pin(dht_gpio)
                if pin_obj is None:
                    raise RuntimeError(f"Could not resolve board pin for {dht_gpio}. Check wiring and pin name (use BCM numbering).")
                time.sleep(1.0)
                self._dht = adafruit_dht.DHT22(pin_obj, use_pulseio=False)
            except Exception as e:
                print(f"WARNING: Real sensor init failed ({e}). Falling back to simulation.")
                self.simulate = True
    def _simulate_reading(self):
        """
        Generate stable, realistic values for a cold room:
        Temp ~ 2.0-6.0°C, Humidity ~ 88-96%.
        Slight variation per second for a bit of realism.
        """
        t = time.time()
        base_temp = 3.5 + 1.5 * (0.5 - ((int(t) % 10) / 10.0))  # small wiggle
        base_hum = 92.0 + 2.0 * (0.5 - ((int(t) % 7) / 7.0))
        return round(base_temp, 1), round(base_hum, 1)
    def read(self, retries=5, delay_s=2.0):
        """
        Returns (temperature_c, humidity_percent), rounded to 1 decimal.
        Retries on transient DHT errors. On Windows or simulation mode, returns simulated values.
        Adds 2s cadence and sanity checks to avoid garbage values.
        """
        if self.simulate:
            return self._simulate_reading()
        last_exc = None
        for _ in range(max(1, retries)):
            try:
                time.sleep(2.0)  # Adafruit guidance: pause between reads
                hum = self._dht.humidity
                temp = self._dht.temperature
                if self.prefer_ds18b20:
                    t_ds = _read_ds18b20_temp()
                    if t_ds is not None:
                        temp = t_ds
                # Sanity checks
                if hum is not None and temp is not None:
                    temp_f = float(temp)
                    hum_f = float(hum)
                    if -20.0 <= temp_f <= 50.0 and 0.0 <= hum_f <= 100.0:
                        return round(temp_f, 1), round(hum_f, 1)
            except Exception as e:
                last_exc = e
            time.sleep(delay_s)
        raise RuntimeError(f"Failed to read from DHT22/DS18B20 sensors after {retries} attempts. Last error: {last_exc}")

# ==============================
# Fruit-specific storage conditions
# ==============================
FRUIT_CONDITIONS = {
    "Apple": {"temp_min": 0, "temp_max": 4, "hum_min": 90, "hum_max": 95},
    "Kiwi": {"temp_min": 0, "temp_max": 4, "hum_min": 90, "hum_max": 95},
    "Litchi": {"temp_min": 1, "temp_max": 5, "hum_min": 90, "hum_max": 95},
    "Dragon Fruit": {"temp_min": 7, "temp_max": 10, "hum_min": 85, "hum_max": 90},
}

DEFAULT_FRUIT = "Apple"

# ==============================
# HTML Template (updated with fruit select)
# ==============================
INDEX_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cold Room AI Monitor</title>
<style>
:root {
  --bg-color: #0d1117;
  --primary-widget-color: #161b22;
  --text-color: #c9d1d9;
  --text-secondary-color: #8b949e;
  --border-color: #30363d;
  --accent-color: #58a6ff;
  --accent-hover-color: #1f6feb;
  --risk-high-color: #f85149;
  --risk-medium-color: #f0a32e;
  --risk-low-color: #3fb950;
  --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
html { scroll-behavior: smooth; }
body {
  font-family: var(--font-family);
  margin: 0;
  padding: 2em;
  background-color: var(--bg-color);
  color: var(--text-color);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
.container { display: flex; flex-wrap: wrap; gap: 2em; }
.main, .sidebar {
  padding: 2em;
  border: 1px solid var(--border-color);
  border-radius: 12px;
  background-color: var(--primary-widget-color);
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  transition: transform 0.3s ease, box-shadow 0.3s ease;
}
.main:hover, .sidebar:hover {
  transform: translateY(-5px);
  box-shadow: 0 12px 32px rgba(88, 166, 255, 0.2);
}
.main { flex-grow: 1; min-width: 400px; }
.sidebar { width: 320px; }
h1 { color: #fff; text-align: center; margin-bottom: 1em; font-size: 2.5em; font-weight: 600; }
h2, h3 { border-bottom: 1px solid var(--border-color); padding-bottom: 0.6em; color: var(--text-color); font-weight: 500; }
hr { border: none; border-top: 1px solid var(--border-color); margin: 2em 0; }
form { display: grid; gap: 1em; }
label { font-weight: 600; color: var(--text-secondary-color); font-size: 0.9em; }
input[type="number"], select {
  padding: 12px; border-radius: 8px; border: 1px solid var(--border-color);
  background-color: var(--bg-color); color: var(--text-color);
  width: 100%; box-sizing: border-box; font-size: 1em;
  transition: border-color 0.2s, box-shadow 0.2s;
}
input[type="number"]:focus, select:focus {
  outline: none; border-color: var(--accent-color);
  box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.3);
}
button {
  padding: 14px; background: linear-gradient(145deg, var(--accent-color), var(--accent-hover-color));
  color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 1.1em; font-weight: bold;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
button:hover { transform: translateY(-2px); box-shadow: 0 4px 15px rgba(88, 166, 255, 0.2); }
button:active { transform: translateY(1px); box-shadow: none; }
.live-data { font-size: 2em; margin: 1em 0; text-align: center; }
.live-data span { font-weight: bold; color: #fff; }
.ai-analysis p { font-size: 1.1em; }
.ai-analysis b { font-size: 1.2em; padding: 4px 8px; border-radius: 6px; font-weight: 600; }
#spoilage-risk.High { color: var(--risk-high-color); background-color: rgba(248, 81, 73, 0.1); }
#spoilage-risk.Medium { color: var(--risk-medium-color); background-color: rgba(240, 163, 46, 0.1); }
#spoilage-risk.Low { color: var(--risk-low-color); background-color: rgba(63, 185, 80, 0.1); }
#alert-list { list-style-type: none; padding-left: 0; max-height: 200px; overflow-y: auto; }
#alert-list li {
  background-color: var(--bg-color); margin-bottom: 8px; padding: 12px; border-radius: 6px;
  border-left: 4px solid var(--accent-color); font-size: 0.95em; transition: background-color 0.2s;
}
#alert-list li:hover { background-color: #222831; }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
</head>
<body>
  <h1>🍎 Cold Room AI Monitor</h1>
  <div class="container">
    <div class="main">
      <h2>Live Status</h2>
      <div class="live-data">
        🌡 Temp: <span id="current-temp">{{ reading.temperature if reading else 'N/A' }}</span> °C&nbsp;&nbsp;&nbsp;
        💧 Humidity: <span id="current-hum">{{ reading.humidity if reading else 'N/A' }}</span> %
      </div>
      <h3 class="ai-analysis">AI Analysis (Factoring in External Weather)</h3>
      <p class="ai-analysis">Spoilage Risk: <b id="spoilage-risk" class="{{ spoilage_risk }}">{{ spoilage_risk }}</b></p>
      <p class="ai-analysis">Anomaly Status: <b id="anomaly-status">{{ anomaly_status }}</b></p>
      <h2>Historical Data (Last 100 Readings)</h2>
      <canvas id="dataChart" width="400" height="200"></canvas>
    </div>
    <div class="sidebar">
      <h3>Weather Station</h3>
      <form id="location-form">
        <label for="location">Select Location:</label>
        <select id="location" name="location">
          {% for loc in locations %}
          <option value="{{ loc }}" {% if loc == selected_location %}selected{% endif %}>{{ loc.split(',')[0] }}</option>
          {% endfor %}
        </select>
        <button type="submit">Get Weather</button>
      </form>
      <hr>
      <h3>Select Fruit Type</h3>
      <form id="fruit-form">
        <select id="fruit" name="fruit">
          {% for f in fruits %}
          <option value="{{ f }}" {% if f == selected_fruit %}selected{% endif %}>{{ f }}</option>
          {% endfor %}
        </select>
        <button type="submit">Set Fruit</button>
      </form>
      <hr>
      {% if weather and not weather.error %}
        <h4>{{ weather.location }}</h4>
        <p>Temp: {{ weather.temperature }}°C | Humidity: {{ weather.humidity }}%</p>
        <p>Conditions: {{ weather.description|title }}</p>
      {% elif weather %}
        <p style="color:#f85149;">{{ weather.error }}</p>
      {% endif %}
      <hr>
      <h3>Manual Data Entry</h3>
      <form id="data-form">
        <label for="temperature">Internal Temp (°C):</label>
        <input type="number" id="temperature" name="temperature" step="0.1" required>
        <label for="humidity">Internal Humidity (%):</label>
        <input type="number" id="humidity" name="humidity" step="0.1" required>
        <button type="submit">Submit Data</button>
      </form>
      <hr>
      <h3>Recent Alerts</h3>
      <ul id="alert-list">
        {% for alert in alerts %}
          <li><small>{{ alert.timestamp.strftime('%Y-%m-%d %H:%M') }}</small>: {{ alert.message }}</li>
        {% else %}
          <li>No recent alerts.</li>
        {% endfor %}
      </ul>
      <hr>
      <button id="read-sensor-btn">Read Hardware Sensor Now</button>
    </div>
  </div>
  <script>
  document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('location-form').addEventListener('submit', function(e) {
      e.preventDefault();
      const selectedLocation = document.getElementById('location').value;
      const fruitSelect = document.getElementById('fruit');
      const selectedFruit = fruitSelect ? fruitSelect.value : '{{ selected_fruit }}';
      window.location.href = '/?location=' + encodeURIComponent(selectedLocation) + '&fruit=' + encodeURIComponent(selectedFruit);
    });
    document.getElementById('fruit-form').addEventListener('submit', function(e) {
      e.preventDefault();
      const fruit = document.getElementById('fruit').value;
      const locationSelect = document.getElementById('location');
      const selectedLocation = locationSelect ? locationSelect.value : '{{ selected_location }}';
      window.location.href = '/?location=' + encodeURIComponent(selectedLocation) + '&fruit=' + encodeURIComponent(fruit);
    });
    const ctx = document.getElementById('dataChart').getContext('2d');
    let dataChart;
    function createChart(chartData) {
      if (dataChart) dataChart.destroy();
      Chart.defaults.color = 'rgba(201, 209, 217, 0.8)';
      dataChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: chartData.labels,
          datasets: [
            { label: 'Temperature (°C)', data: chartData.temperatures,
              borderColor: 'rgb(248, 81, 73)', backgroundColor: 'rgba(248, 81, 73, 0.2)',
              tension: 0.2, yAxisID: 'y', borderWidth: 2, pointBackgroundColor: 'rgb(248, 81, 73)' },
            { label: 'Humidity (%)', data: chartData.humidities,
              borderColor: 'rgb(88, 166, 255)', backgroundColor: 'rgba(88, 166, 255, 0.2)',
              tension: 0.2, yAxisID: 'y1', borderWidth: 2, pointBackgroundColor: 'rgb(88, 166, 255)' }
          ]
        },
        options: {
          scales: {
            y: { type: 'linear', display: true, position: 'left', title: { display: true, text: 'Temperature (°C)' }, grid: { color: 'rgba(48, 54, 61, 0.8)' } },
            y1: { type: 'linear', display: true, position: 'right', title: { display: true, text: 'Humidity (%)' }, grid: { drawOnChartArea: false } },
            x: { type: 'time', time: { unit: 'minute' }, grid: { color: 'rgba(48, 54, 61, 0.8)' } }
          }
        }
      });
    }
    async function updateChart() {
      const response = await fetch('/api/historical-data');
      const chartData = await response.json();
      createChart(chartData);
    }
    document.getElementById('data-form').addEventListener('submit', async function (e) {
      e.preventDefault();
      const formData = new FormData(e.target);
      const response = await fetch('/data', { method: 'POST', body: formData });
      const result = await response.json();
      if (result.status === 'success') {
        alert('Data submitted successfully! The page will now refresh.');
        window.location.href = window.location.href;
      } else {
        alert('Error submitting data: ' + result.message);
      }
    });
    document.getElementById('read-sensor-btn').addEventListener('click', async function () {
      try {
        const res = await fetch('/read_sensor', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
          alert('Hardware read: ' + data.temperature + '°C, ' + data.humidity + '%');
          window.location.href = window.location.href;
        } else {
          alert('Read error: ' + data.message);
        }
      } catch (e) {
        alert('Request failed: ' + e);
      }
    });
    updateChart();
  });
  </script>
</body>
</html>
"""

# ==============================
# Flask App + DB
# ==============================
app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, 'cold_storage.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Instantiate sensor early (WIN-COMPAT: will simulate on Windows by default)
sensor = None
try:
    sensor = SensorReader(dht_gpio=DHT_GPIO, prefer_ds18b20=PREFER_DS18B20)
except Exception as e:
    print(f"WARNING: Sensor init failed: {e}")
    sensor = None

# retry sensor init if needed
if sensor is None:
    try:
        sensor = SensorReader(dht_gpio=DHT_GPIO, prefer_ds18b20=PREFER_DS18B20)
    except Exception as e:
        print(f"WARNING: Sensor init failed (retry): {e}")
        sensor = None

# ==============================
# Database Models
# ==============================
class SensorReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    temperature = db.Column(db.Float, nullable=False)
    humidity = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(255), nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

# ==============================
# Email Alerts
# ==============================
def send_email_alert(alert_message):
    print("="*20 + f"\nEMAIL ALERT TRIGGERED: {alert_message}\n" + "="*20)
    if not all([SENDER_EMAIL, RECEIVER_EMAIL, EMAIL_PASSWORD]):
        print("DEBUG: Email credentials not set. Skipping email.")
        return
    message = MIMEMultipart("alternative")
    message["Subject"] = "Cold Room AI Monitor - ALERT"
    message["From"] = SENDER_EMAIL
    message["To"] = RECEIVER_EMAIL
    text = f"""
Hi,
Automated alert from the Cold Room AI Monitoring system.
An anomaly has been detected:
---
{alert_message}
---
Please check the system.
"""
    part = MIMEText(text, "plain")
    message.attach(part)
    server = None
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(SENDER_EMAIL, EMAIL_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, message.as_string())
        print(f"Email alert sent successfully to {RECEIVER_EMAIL}!")
    except Exception as e:
        print(f"ERROR sending email: {e}")
    finally:
        if server:
            server.quit()

# ==============================
# Weather
# ==============================
_last_weather = {'ts': 0, 'data': None}
def get_weather_forecast(location="Bhoranj,IN"):
    """ Fetches weather data for a given location with basic caching. """
    API_KEY = OPENWEATHER_API_KEY
    if not API_KEY or API_KEY.startswith('REPLACE'):
        return {"error": "OpenWeather API key not set"}
    now = time.time()
    # 5-minute cache
    if _last_weather['data'] and (now - _last_weather['ts'] < 300) and _last_weather['data'].get('location') == location.split(',')[0]:
        return _last_weather['data']
    BASE_URL = "http://api.openweathermap.org/data/2.5/weather"
    url = f"{BASE_URL}?q={location}&appid={API_KEY}&units=metric"
    try:
        response = requests.get(url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            result = {
                "location": data.get('name', 'Unknown'),
                "temperature": data['main']['temp'],
                "humidity": data['main']['humidity'],
                "description": data['weather'][0]['description'],
                "error": None
            }
            _last_weather['ts'] = now
            _last_weather['data'] = result
            return result
        else:
            try:
                msg = response.json().get('message', 'Unknown error')
            except Exception:
                msg = f"HTTP {response.status_code}"
            return {"error": f"Weather API error: {msg}"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Network error fetching weather: {e}"}

# ==============================
# AI Logic (fruit aware)
# ==============================
def predict_spoilage(temperature, humidity, external_temp=None, fruit=DEFAULT_FRUIT):
    cfg = FRUIT_CONDITIONS.get(fruit, FRUIT_CONDITIONS[DEFAULT_FRUIT])
    risk_score = 0

    if temperature > cfg["temp_max"]: 
        risk_score += (temperature - cfg["temp_max"]) * 10
    if temperature < cfg["temp_min"]: 
        risk_score += abs(temperature - cfg["temp_min"]) * 15
    if humidity > cfg["hum_max"]: 
        risk_score += (humidity - cfg["hum_max"]) * 5
    if humidity < cfg["hum_min"]: 
        risk_score += (cfg["hum_min"] - humidity) * 5
    
    if external_temp and external_temp > 25:
        risk_score += (external_temp - 25) * 1.5

    if risk_score > 70: return "High"
    if risk_score > 30: return "Medium"
    return "Low"

def detect_anomaly(temperature, humidity, external_temp=None, fruit=DEFAULT_FRUIT):
    cfg = FRUIT_CONDITIONS.get(fruit, FRUIT_CONDITIONS[DEFAULT_FRUIT])

    temp_min, temp_max = cfg["temp_min"], cfg["temp_max"]
    hum_min, hum_max = cfg["hum_min"], cfg["hum_max"]

    # Adjust max temp slightly based on external temp
    if external_temp and external_temp > 20:
        temp_max += ((external_temp - 20) / 5) * 0.1

    if not (temp_min <= temperature <= temp_max):
        return True, f"Temperature {temperature}°C out of range ({temp_min}-{temp_max}°C)"
    if not (hum_min <= humidity <= hum_max):
        return True, f"Humidity {humidity}% out of range ({hum_min}-{hum_max}%)"

    return False, f"Conditions Normal ({temp_min}-{temp_max}°C, {hum_min}-{hum_max}%)"

# ==============================
# Routes
# ==============================
@app.route('/')
def index():
    locations = [
        "Bilaspur,IN", "Chamba,IN", "Hamirpur,IN", "Kangra,IN",
        "Kinnaur,IN", "Kullu,IN", "Lahaul and Spiti,IN", "Mandi,IN",
        "Shimla,IN", "Sirmaur,IN", "Solan,IN", "Una,IN", 
    ]
    selected_location = request.args.get('location', 'Kinnaur,IN')
    if selected_location not in locations:
        selected_location = "Bhoranj,IN"

    selected_fruit = request.args.get('fruit', DEFAULT_FRUIT)
    if selected_fruit not in FRUIT_CONDITIONS:
        selected_fruit = DEFAULT_FRUIT

    weather_data = get_weather_forecast(selected_location)
    latest_reading = SensorReading.query.order_by(SensorReading.timestamp.desc()).first()
    alerts = AlertLog.query.order_by(AlertLog.timestamp.desc()).limit(10).all()

    spoilage_risk, anomaly_status = "N/A", "N/A"
    if latest_reading:
        external_temp = None
        if USE_WEATHER and weather_data and not weather_data.get('error'):
            external_temp = weather_data.get('temperature')
        spoilage_risk = predict_spoilage(latest_reading.temperature, latest_reading.humidity, external_temp, selected_fruit)
        _, anomaly_status = detect_anomaly(latest_reading.temperature, latest_reading.humidity, external_temp, selected_fruit)

    return render_template_string(
        INDEX_HTML_TEMPLATE, reading=latest_reading, alerts=alerts, weather=weather_data,
        spoilage_risk=spoilage_risk, anomaly_status=anomaly_status,
        locations=locations, selected_location=selected_location,
        fruits=list(FRUIT_CONDITIONS.keys()), selected_fruit=selected_fruit
    )

@app.route('/data', methods=['POST'])
def add_data():
    """Manual input endpoint."""
    try:
        temp = float(request.form['temperature'])
        humidity = float(request.form['humidity'])
        fruit = request.args.get('fruit', DEFAULT_FRUIT)
        if fruit not in FRUIT_CONDITIONS:
            fruit = DEFAULT_FRUIT
            
        weather_data = get_weather_forecast()
        external_temp = None
        if USE_WEATHER and weather_data and not weather_data.get('error'):
            external_temp = weather_data.get('temperature')
        reading = SensorReading(temperature=temp, humidity=humidity)
        db.session.add(reading)
        is_anomaly, anomaly_reason = detect_anomaly(temp, humidity, external_temp, fruit)
        if is_anomaly:
            send_email_alert(anomaly_reason)
            db.session.add(AlertLog(message=anomaly_reason))
        db.session.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/read_sensor', methods=['POST'])
def read_sensor():
    """Takes a hardware reading and stores it, triggering alerts if needed."""
    if sensor is None:
        return jsonify({'status': 'error', 'message': 'Sensor not initialized'}), 500
    try:
        temp, hum = sensor.read()
        fruit = request.args.get('fruit', DEFAULT_FRUIT)
        if fruit not in FRUIT_CONDITIONS:
            fruit = DEFAULT_FRUIT

        weather_data = get_weather_forecast()
        external_temp = None
        if USE_WEATHER and weather_data and not weather_data.get('error'):
            external_temp = weather_data.get('temperature')
        reading = SensorReading(temperature=temp, humidity=hum)
        db.session.add(reading)
        is_anomaly, anomaly_reason = detect_anomaly(temp, hum, external_temp, fruit)
        if is_anomaly:
            send_email_alert(anomaly_reason)
            db.session.add(AlertLog(message=anomaly_reason))
        db.session.commit()
        return jsonify({'status': 'success', 'temperature': temp, 'humidity': hum})
    except Exception as e:
        db.session.rollback()
        try:
            db.session.add(AlertLog(message=f"Sensor read failed: {e}"))
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/historical-data')
def historical_data():
    readings = SensorReading.query.order_by(SensorReading.timestamp.desc()).limit(100).all()
    readings.reverse()
    return jsonify({
        'labels': [r.timestamp.strftime('%Y-%m-%dT%H:%M:%S') for r in readings],
        'temperatures': [r.temperature for r in readings],
        'humidities': [r.humidity for r in readings]
    })

# ==============================
# Main
# ==============================
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    print(f"Running on Windows: {IS_WINDOWS}, Linux: {IS_LINUX}, Simulate sensor: {_should_simulate()}, USE_WEATHER={USE_WEATHER}")
    app.run(host='0.0.0.0', port=5000, debug=True)