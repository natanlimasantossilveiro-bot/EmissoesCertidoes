FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

ENV BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY libs/ /libs/
RUN pip install --no-cache-dir -e /libs

COPY services/worker-curitiba-cnd-cpf/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/worker-curitiba-cnd-cpf/ .

RUN mkdir -p /data/downloads /data/evidencias /data/certidoes_emitidas

CMD ["python", "worker.py"]
