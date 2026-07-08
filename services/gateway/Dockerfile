FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# lib compartilhada instalada primeiro (cache de build)
COPY libs/ /libs/
RUN pip install --no-cache-dir -e /libs

COPY services/gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/gateway/ .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
