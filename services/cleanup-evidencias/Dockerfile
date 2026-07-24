FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY libs/ /libs/
RUN pip install --no-cache-dir -e /libs

COPY services/cleanup-evidencias/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/cleanup-evidencias/ .

CMD ["python", "main.py"]
