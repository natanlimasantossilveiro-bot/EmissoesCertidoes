"""
Configuração central. Cada worker/serviço lê as mesmas variáveis de ambiente,
então basta um .env por ambiente (dev/staging/prod) em vez de espalhar config.
"""
import os
from pathlib import Path


class Config:
    # RabbitMQ
    RABBITMQ_URL: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

    # Banco de dados central (status dos pedidos)
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "mysql+pymysql://root:root@localhost:3306/certidoes"
    )

    # Storage de evidências (screenshots, PDFs)
    # "local" grava em disco compartilhado; "s3" usa bucket S3/MinIO
    STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "local")
    STORAGE_LOCAL_PATH: Path = Path(os.getenv("STORAGE_LOCAL_PATH", "/data/evidencias"))

    S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")  # ex: MinIO local
    S3_BUCKET: str = os.getenv("S3_BUCKET", "certidoes-evidencias")
    S3_ACCESS_KEY: str = os.getenv("S3_ACCESS_KEY", "")
    S3_SECRET_KEY: str = os.getenv("S3_SECRET_KEY", "")

    # 2captcha
    TWOCAPTCHA_API_KEY: str = os.getenv("TWOCAPTCHA_API_KEY", "")

    # Navegador (nodriver)
    BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
    BROWSER_DOWNLOAD_DIR: Path = Path(os.getenv("BROWSER_DOWNLOAD_DIR", "/data/downloads"))

    # Retry / resiliência
    MAX_TENTATIVAS: int = int(os.getenv("MAX_TENTATIVAS", "3"))

    # Limpeza automática (serviço cleanup-evidencias) — prazo em horas antes
    # de considerar um arquivo "antigo". Certidão só é apagada de fato se
    # upload_confirmado estiver True no pedido (ver limpeza_certidoes.py).
    RETENCAO_EVIDENCIAS_HORAS: int = int(os.getenv("RETENCAO_EVIDENCIAS_HORAS", "72"))
    RETENCAO_CERTIDOES_HORAS: int = int(os.getenv("RETENCAO_CERTIDOES_HORAS", "72"))


config = Config()
