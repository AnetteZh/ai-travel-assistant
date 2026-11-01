# «О!дин запрос» — AI-агент путешествий (трек «О!дин запрос»)

Пользователь одним сообщением описывает поездку обычным языком, а агент подбирает направление, транспорт, отель и трансфер, проверяет, что всё состыковано по датам, времени и бюджету, объясняет выбор и доводит до корзины и имитации покупки.

- Видео-скринкаст: **TODO — вставить ссылку** (проверить, что открывается в режиме инкогнито)
- Развёрнутый сервис: не используется

## Быстрый старт

```bash
git clone <URL репозитория>
cd <repo>
cp .env.example .env   # вписать ключ модели — см. ниже; без ключа работает демо-режим
docker compose up --build
# открыть http://localhost:8080
```

Те же команды работают в PowerShell на Windows. Первая сборка занимает 2–5 минут в зависимости от скорости сети.

**Ключ языковой модели.** В `.env` укажите `GIGACHAT_CREDENTIALS` — ключ авторизации GigaChat (developers.sber.ru). Вместо GigaChat можно подключить любое OpenAI-совместимое API: `LLM_PROVIDER=openai_compatible`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_COMPATIBLE_BASE_URL`. Если ключа нет, приложение всё равно запускается. Во вкладке «Демо» сценарий полностью проходит на подготовленных ответах, а «Настоящий агент» сообщит, что модель не настроена.

Остановить: `docker compose down`. Если порт 8080 занят, задайте в `.env` другой, например `APP_PORT=8081`, и откройте `http://localhost:8081`.

## Как устроено

```
Браузер ──▶ app (Next.js, :8080) ──/api/agent/*──▶ core (FastAPI + LangGraph, :8000 внутри сети)
                                                      ├─ языковая модель (GigaChat / OpenAI-совместимое API)
                                                      └─ travel_data: демо-мир поездок, поиск, цены, погода
```

- **app** — интерфейс: главная, направления, подбор поездки, корзина, чат с агентом. Проксирует запросы к Core, поток ответов идёт по SSE.
- **core** — агент на LangGraph: разбор запроса → допущения → поиск → сборка плана → проверка состыковок ⇄ исправление → объяснение. Цены, расписания и проверки считаются программно, языковая модель только разбирает запрос.
- **travel_data** — синтетический мир: 20 направлений, 200 отелей, поезда, рейсы, трансферы. Он нужен для демонстрации, предложения не настоящие. Погода берётся из MET Norway или Open-Meteo, а без сети — из оценки по климату.

Подробно о Core и Data: [`docs/CORE_AND_DATA.md`](docs/CORE_AND_DATA.md) и [`DATA_FILE_GUIDE.md`](DATA_FILE_GUIDE.md).

## Зависимости и переменные окружения

Рекомендованные ресурсы:
- 2 CPU cores
- 4 GB RAM (сборка интерфейса; в работе оба сервиса занимают меньше 1 GB)
- около 2 GB на диске под образы

Внешних баз данных и кэша нет. Сессии поездок хранятся в памяти Core и сбрасываются при перезапуске. Нужен доступ в интернет к API языковой модели. Погода без сети не ломается.

Версии зафиксированы: базовые образы `python:3.12.11-slim-bookworm` и `node:22.22.0-bookworm-slim`, Python-зависимости в `requirements-docker.txt`, npm-зависимости в `frontend/package-lock.json`. Docker Compose v2.24 или новее.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `APP_PORT` | `8080` | порт интерфейса на компьютере |
| `LLM_PROVIDER` | `gigachat` | `gigachat` или `openai_compatible` |
| `GIGACHAT_CREDENTIALS` | — | ключ авторизации GigaChat |
| `GIGACHAT_MODEL` | `GigaChat` | модель GigaChat, например `GigaChat-2-Max` |
| `GIGACHAT_SCOPE` | `GIGACHAT_API_PERS` | тип доступа GigaChat |
| `OPENAI_API_KEY` | — | ключ OpenAI-совместимого API |
| `OPENAI_MODEL` | `gpt-4o-mini` | модель OpenAI-совместимого API |
| `OPENAI_COMPATIBLE_BASE_URL` | пусто | адрес OpenAI-совместимого API, пусто — api.openai.com |
| `OPENAI_TIMEOUT_SECONDS` | `60` | тайм-аут запроса к модели, секунды |
| `AGENT_MAX_SEARCH_CALLS` | `3` | сколько раз агент может вызвать один инструмент поиска |
| `AGENT_MAX_REPAIRS` | `1` | число попыток исправить план |
| `TRAVEL_DATA_WORLD_PATH` | `data/world.json` | файл демо-мира |
| `MET_NO_ENABLED`, `OPEN_METEO_ENABLED` | `true` | источники погоды |
| `MET_NO_USER_AGENT` | см. `.env.example` | User-Agent для MET Norway |
| `CORE_API_URL` | `http://core:8000` | задаётся в `docker-compose.yml`: адрес Core для интерфейса |

Compose читает `.env.example` как значения по умолчанию, а `.env` их переопределяет. Реальные ключи хранятся только в `.env`, он в `.gitignore`.

## Сидирование

Ничего делать не нужно: демо-мир лежит в репозитории (`data/world.json`) и загружается при старте Core. Пересоздать его можно так (нужен Python 3.11+), затем снова выполнить `docker compose up --build`:

```bash
python scripts/generate_world.py
python scripts/validate_world.py
```

## Маршруты/доступ

Интерфейс на `http://localhost:8080`:

- `/` — главная с чатом агента; переключатель «Демо / Настоящий агент» в шапке чата
- `/destinations` — идеи направлений
- `/selection` — подбор поездки: как агент понял запрос, отели, дорога, план по шагам, проверка состыковок
- `/checkout` — корзина и имитация покупки
- `/health` — `200`, `{"status": "ok", "core": "ok"}` (`core: "unavailable"`, если Core не отвечает)
- `/api/agent/chat` (POST, SSE), `/api/agent/trips/{id}` (PATCH), `/api/agent/trips/{id}/book` (POST) — прокси к Core

Core наружу не публикуется. Внутри сети Compose он отвечает на `/health`, `/chat`, `/trips/{id}`, `/trips/{id}/book`.

Регистрации и тестовых пользователей нет.

Примеры запросов для чата:
- «Хочу на неделю в Сочи, бюджет 80 тысяч на двоих, вылет из Москвы»
- «Из Казани в Питер с мужем и двумя детьми 5 и 8 лет»
- «Куда-нибудь к морю недорого» → «Давай первый вариант» → «А если на два дня позже?»

## Логи и диагностика

```bash
docker compose ps            # оба сервиса должны быть healthy
docker compose logs core     # ошибки агента и модели
docker compose logs app      # ошибки интерфейса
```

На Apple Silicon образы собираются нативно: базовые образы мультиплатформенные. При проблемах со сборкой добавьте `platform: linux/amd64` к сервисам в `docker-compose.yml`.

## Разработка без Docker

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -c requirements-docker.txt -e ".[api,langgraph,test]"
python -m uvicorn agent_core.api:app --app-dir src --port 8765

cd frontend && npm ci && npm run dev -- -p 3001      # CORE_API_URL=http://127.0.0.1:8765 в frontend/.env.local
```

Проверки: `python -m pytest -q`. Прогон агента по набору из 34 запросов с настоящей моделью — `python scripts/eval_agent.py`, отчёт пишется в `evals/reports/`. Есть `Makefile`: `make up`, `make down`, `make logs`, `make seed`, `make test`. CI в `.github/workflows/ci.yml` запускает тесты, линтер, сборку и проверку `/health` в Docker.

## Структура репозитория

```
docker-compose.yml       сервисы app и core
Dockerfile               образ Core
requirements-docker.txt  точные версии Python-зависимостей
.env.example             переменные окружения
src/agent_core/          Core AI: граф LangGraph, API, промпты
src/travel_data/         Data: модели, поиск, цены, погода
data/world.json          демо-мир
frontend/                интерфейс Next.js (свой Dockerfile)
tests/, evals/, scripts/ тесты, набор запросов для проверки агента, генерация данных
docs/                    подробная документация Core и Data
```
