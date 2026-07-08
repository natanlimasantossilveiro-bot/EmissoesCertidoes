FROM python:3.12-slim

# nodriver precisa de um Chromium instalado no sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

ENV BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
# sem isso, os prints do worker (inclusive evidência de erro) ficam presos
# no buffer do stdout e não aparecem em `docker logs` até o processo morrer
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY libs/ /libs/
RUN pip install --no-cache-dir -e /libs

COPY services/worker-cpf/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/worker-cpf/ .

RUN mkdir -p /data/downloads /data/evidencias /data/certidoes_emitidas

CMD ["python", "worker.py"]