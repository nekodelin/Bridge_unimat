# UNIMAT Monitoring Backend

Realtime backend-bridge между MQTT и frontend.

## Что реализовано

- MQTT client layer (`app/mqtt/client.py`)
- decoder / mapping layer (`app/services/decoder.py`)
- state store in memory (`app/services/state_store.py`)
- event journal service (последние 500, только при смене состояния канала)
- websocket broadcasting (`/ws/state`, также alias `/ws`)
- REST API (`/api/*`)
- mock mode без MQTT (`MOCK_MODE=true`)
- статические конфиги вместо чтения Excel на лету:
  - `app/config/signal_map.json`
  - `app/config/event_texts.json`
  - `app/config/module_map.json`

## Структура

```text
app/
  main.py
  api/
  mqtt/
  services/
  models/
  schemas/
  config/
  utils/
```

## Переменные окружения

См. `.env.example`:

```env
APP_ENV=local
API_HOST=0.0.0.0
API_PORT=8000
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_USER=
MQTT_PASSWORD=
MQTT_TOPIC_STATE=puma_board
MQTT_TOPIC_ACT=puma_board_act
MQTT_TLS=false
FRONTEND_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,https://your-project.web.app
MOCK_MODE=false
JOURNAL_LIMIT=500
WS_HEARTBEAT_SEC=15
```

## Локальный запуск

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
copy .env.example .env

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Docker

```bash
docker compose up --build
```

## REST API

- `GET /api/health`
- `GET /api/state`
- `GET /api/debug/bits`
- `GET /api/debug/last-payload`
- `GET /api/channels`
- `GET /api/journal?limit=100`
- `GET /api/config`
- `POST /api/act/tifon` body: `{"value": true|false}`

## WebSocket

- `ws://localhost:8000/ws/state` (alias `ws://localhost:8000/ws`)
- сообщения:
  - `type: "snapshot"` (initial)
  - `type: "state_update"` (при изменениях)
  - `type: "journal_append"` (новая запись журнала)
  - `type: "heartbeat"`

## Mock mode

Для демонстрации без брокера:

```env
MOCK_MODE=true
```

Backend будет циклически генерировать состояния `normal -> breakage -> short_circuit`.
