# Core AI: FastAPI + LangGraph + модуль travel_data.
# Внутри сети Compose слушает 8000; снаружи доступен только через интерфейс на 8080.

ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm

# --- Сборка: зависимости ставятся в отдельный venv, который потом копируется целиком.
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Сначала только список версий — слой с зависимостями кэшируется, пока он не меняется.
COPY requirements-docker.txt ./
RUN pip install -r requirements-docker.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . && pip check

# --- Рантайм: только интерпретатор, venv и данные, без pip-кэша и исходников сборки.
FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRAVEL_DATA_WORLD_PATH=/app/data/world.json
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY data ./data
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
# Один процесс: сессии поездок хранятся в памяти Core.
CMD ["uvicorn", "agent_core.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
