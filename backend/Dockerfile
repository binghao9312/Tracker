FROM python:3.13-slim
WORKDIR /app
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/app ./backend/app
COPY config ./config
RUN pip install --no-cache-dir ./backend
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
