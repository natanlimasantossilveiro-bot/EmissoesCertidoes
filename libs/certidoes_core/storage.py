"""
Abstrai onde as evidências/certidões ficam guardadas. Começa em disco local
compartilhado (mais simples) e migra pra S3/MinIO sem mexer no worker,
só trocando STORAGE_BACKEND no .env.
"""
from pathlib import Path
from datetime import datetime
from certidoes_core.config import config
from certidoes_core.nomenclatura import normalizar_nome_arquivo


def _caminho_local(nome_arquivo: str) -> Path:
    caminho = config.STORAGE_LOCAL_PATH / nome_arquivo
    caminho.parent.mkdir(parents=True, exist_ok=True)
    return caminho


def salvar_bytes(nome_arquivo: str, conteudo: bytes) -> str:
    """Retorna a URL/caminho público pra ser gravado no banco (url_evidencia
    ou caminho_certidao)."""
    if config.STORAGE_BACKEND == "s3":
        return _salvar_s3(nome_arquivo, conteudo)
    caminho = _caminho_local(nome_arquivo)
    caminho.write_bytes(conteudo)
    return str(caminho)


def _salvar_s3(nome_arquivo: str, conteudo: bytes) -> str:
    import boto3

    cliente = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT_URL or None,
        aws_access_key_id=config.S3_ACCESS_KEY,
        aws_secret_access_key=config.S3_SECRET_KEY,
    )
    cliente.put_object(Bucket=config.S3_BUCKET, Key=nome_arquivo, Body=conteudo)
    return f"s3://{config.S3_BUCKET}/{nome_arquivo}"


def gerar_nome_evidencia(nome_pessoa: str, documento: str, portal: str, motivo: str, extensao: str = "png") -> str:
    data_hora = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{portal}/{motivo}_{normalizar_nome_arquivo(nome_pessoa)}_{documento}_{data_hora}.{extensao}"
