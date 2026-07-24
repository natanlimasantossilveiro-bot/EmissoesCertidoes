"""
Abstrai onde as evidências/certidões ficam guardadas. Começa em disco local
compartilhado (mais simples) e migra pra S3/MinIO sem mexer no worker,
só trocando STORAGE_BACKEND no .env.
"""
import re
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


def listar_arquivos_locais(base_path: Path) -> list[Path]:
    """Lista todo arquivo dentro de base_path, recursivo. Recebe o caminho
    como parâmetro (em vez de usar config.STORAGE_LOCAL_PATH direto) porque
    quem limpa evidências/certidões antigas roda num serviço à parte, com
    cada volume de worker montado num ponto diferente — não tem um
    STORAGE_LOCAL_PATH único pra esse processo."""
    if not base_path.exists():
        return []
    return [caminho for caminho in base_path.rglob("*") if caminho.is_file()]


def listar_objetos_s3(prefixo: str = "") -> list[dict]:
    """Lista objetos do bucket S3 configurado (bucket único, compartilhado
    entre todos os portais — ao contrário do backend local, que é um volume
    por worker)."""
    import boto3

    cliente = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT_URL or None,
        aws_access_key_id=config.S3_ACCESS_KEY,
        aws_secret_access_key=config.S3_SECRET_KEY,
    )
    objetos = []
    paginador = cliente.get_paginator("list_objects_v2")
    for pagina in paginador.paginate(Bucket=config.S3_BUCKET, Prefix=prefixo):
        objetos.extend(pagina.get("Contents", []))
    return objetos


def remover_arquivo_local(caminho: Path) -> None:
    caminho.unlink(missing_ok=True)


def remover_objeto_s3(chave: str) -> None:
    import boto3

    cliente = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT_URL or None,
        aws_access_key_id=config.S3_ACCESS_KEY,
        aws_secret_access_key=config.S3_SECRET_KEY,
    )
    cliente.delete_object(Bucket=config.S3_BUCKET, Key=chave)


_PADRAO_TIMESTAMP_NOME = re.compile(r"(\d{8}_\d{6})\.\w+$")


def extrair_timestamp_do_nome(nome_arquivo: str) -> datetime | None:
    """Extrai o timestamp embutido no nome do arquivo (ver
    gerar_nome_evidencia/gerar_nome_certidao, formato AAAAMMDD_HHMMSS) —
    mais confiável que a data de modificação do arquivo/objeto pra decidir
    idade, porque essa muda se o arquivo for copiado/movido entre volumes,
    enquanto o nome não."""
    encontrado = _PADRAO_TIMESTAMP_NOME.search(nome_arquivo)
    if not encontrado:
        return None
    try:
        return datetime.strptime(encontrado.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def gerar_nome_evidencia(nome_pessoa: str, documento: str, portal: str, motivo: str, extensao: str = "png") -> str:
    data_hora = datetime.now().strftime("%Y%m%d_%H%M%S")
    # CNPJ formatado com barra (ex: "37.187.679/0001-80") quebra o caminho
    # do arquivo — a barra vira separador de pasta, criando uma subpasta
    # extra e inesperada em vez de fazer parte do nome do arquivo (mesmo
    # bug, mesma correção, já aplicada em gerar_nome_certidao).
    documento_seguro = (documento or "").replace("/", "-")
    return f"{portal}/{motivo}_{normalizar_nome_arquivo(nome_pessoa)}_{documento_seguro}_{data_hora}.{extensao}"
