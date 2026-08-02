FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY data ./data
COPY scripts ./scripts

EXPOSE 8000

ENTRYPOINT ["python", "-m", "backend.entrypoint"]
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
