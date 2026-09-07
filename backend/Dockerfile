FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY constraints.txt ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests
RUN pip install --no-cache-dir --constraint constraints.txt '.[dev]' && useradd --create-home findcar
USER findcar
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
