FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Headless Chromium for the job-discovery skill's Playwright render fallback
# (SPA career sites return an empty shell to plain requests). The Python
# package is already installed via requirements.txt; this installs the browser
# binary + system libs (libnss3 etc.) the fallback needs to actually launch.
RUN python -m playwright install --with-deps chromium

COPY backend ./backend
COPY skill ./skill
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY data ./data
COPY scripts ./scripts

EXPOSE 8000

ENTRYPOINT ["python", "-m", "backend.entrypoint"]
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
