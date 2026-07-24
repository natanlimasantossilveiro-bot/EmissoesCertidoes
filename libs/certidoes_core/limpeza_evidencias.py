"""
Limpeza automática de evidências antigas (screenshots de erro/diagnóstico
capturados por capturar_evidencia() em evidencia.py).

Roda só dentro do serviço dedicado `cleanup-evidencias`
(services/cleanup-evidencias/) — nunca no Gateway nem nos workers. O
Gateway continua só leitura nos volumes dos workers, de propósito; só esse
serviço à parte tem acesso de escrita, montando os mesmos volumes em
`/evidencias-workers/<portal>` (raiz "/data" de cada worker).

Evidência não tem (e não precisa de) flag de confirmação de upload — é só
material de diagnóstico técnico, sem valor de entrega pro cliente. Ver
limpeza_certidoes.py pro fluxo separado das certidões emitidas, que respeita
upload_confirmado antes de apagar.
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path

from certidoes_core import storage
from certidoes_core.banco import PedidoCertidao, get_session
from certidoes_core.config import config

logger = logging.getLogger("limpeza_evidencias")

RAIZ_VOLUMES_LOCAIS = Path("/evidencias-workers")

# Os 3 portais que rodam nativos no Windows (fora do Docker) gravam com
# separador de caminho do próprio SO (barra invertida), então o caminho
# salvo em pedido.url_evidencia é diferente dos demais (que rodam em
# container Linux). Ver docker-compose.yml e workers_nativos/.
PORTAIS_NATIVOS_WINDOWS = {"receita_federal", "sefaz_pr_certidao_debitos", "atendenet_pinhais_cnd"}


def limpar_evidencias_antigas(horas: int = 72) -> dict:
    """Remove evidências com mais de `horas` desde a criação (local ou S3,
    conforme STORAGE_BACKEND) e zera url_evidencia do pedido correspondente.
    Nunca apaga a linha do pedido inteira — ela também guarda status,
    caminho_certidao, histórico do colaborador etc."""
    if config.STORAGE_BACKEND == "s3":
        resultado = _limpar_s3(horas)
    else:
        resultado = _limpar_local(horas)

    logger.info(
        "[limpeza_evidencias] corte=%sh removidos=%s registros_atualizados=%s erros=%s",
        horas, resultado["removidos"], resultado["registros_atualizados"], resultado["erros"],
    )
    return resultado


def _limite(horas: int) -> datetime:
    return datetime.utcnow() - timedelta(hours=horas)


def _limpar_local(horas: int) -> dict:
    corte = _limite(horas)
    removidos = registros_atualizados = erros = 0

    if not RAIZ_VOLUMES_LOCAIS.exists():
        logger.warning("[limpeza_evidencias] Raiz %s não existe — nenhum volume montado?", RAIZ_VOLUMES_LOCAIS)
        return {"removidos": 0, "registros_atualizados": 0, "erros": 0}

    for pasta_portal in sorted(RAIZ_VOLUMES_LOCAIS.iterdir()):
        if not pasta_portal.is_dir():
            continue
        portal = pasta_portal.name
        pasta_evidencias = pasta_portal / "evidencias"

        for arquivo in storage.listar_arquivos_locais(pasta_evidencias):
            criado_em = storage.extrair_timestamp_do_nome(arquivo.name)
            if criado_em is None:
                criado_em = datetime.utcfromtimestamp(arquivo.stat().st_mtime)
            if criado_em >= corte:
                continue

            try:
                storage.remover_arquivo_local(arquivo)
                removidos += 1
            except OSError as erro:
                erros += 1
                logger.warning("[limpeza_evidencias] Falha ao remover %s: %s", arquivo, erro)
                continue

            registros_atualizados += _zerar_url_evidencia(portal, arquivo.name)

    return {"removidos": removidos, "registros_atualizados": registros_atualizados, "erros": erros}


def _limpar_s3(horas: int) -> dict:
    corte = _limite(horas)
    removidos = registros_atualizados = erros = 0

    try:
        objetos = storage.listar_objetos_s3()
    except Exception as erro:
        logger.warning("[limpeza_evidencias] Falha ao listar bucket S3: %s", erro)
        return {"removidos": 0, "registros_atualizados": 0, "erros": 1}

    for objeto in objetos:
        chave = objeto["Key"]
        # gerar_nome_evidencia grava como "<portal>/<motivo>_..._<ts>.png"
        if "/" not in chave:
            continue
        portal, nome_arquivo = chave.split("/", 1)
        nome_arquivo = nome_arquivo.rsplit("/", 1)[-1]

        criado_em = storage.extrair_timestamp_do_nome(nome_arquivo)
        if criado_em is None:
            criado_em = objeto["LastModified"].replace(tzinfo=None)
        if criado_em >= corte:
            continue

        try:
            storage.remover_objeto_s3(chave)
            removidos += 1
        except Exception as erro:
            erros += 1
            logger.warning("[limpeza_evidencias] Falha ao remover s3://%s/%s: %s", config.S3_BUCKET, chave, erro)
            continue

        registros_atualizados += _zerar_url_evidencia(portal, nome_arquivo, s3=True)

    return {"removidos": removidos, "registros_atualizados": registros_atualizados, "erros": erros}


def _caminhos_esperados(portal: str, nome_arquivo: str, s3: bool) -> list[str]:
    """Reconstrói o(s) valor(es) possíveis gravados em pedido.url_evidencia
    na hora do salvar_bytes original — evita casar por LIKE difuso (o nome
    do arquivo tem muito "_", que em SQL LIKE é wildcard de 1 caractere).

    ⚠️ Os 3 portais nativos do Windows têm DOIS formatos reais em produção,
    confirmado direto na base (não é só suposição): registros antigos, de
    quando esses workers ainda rodavam em container Linux antes da migração
    pra processo nativo, gravaram estilo posix ("/data/evidencias/...");
    registros novos (já nativos) gravam com barra invertida SEM letra de
    unidade ("\\data\\evidencias\\..." — confirmado que `Path("/data/...")`
    no Windows não resolve pra "C:\\...", fica relativo à unidade atual).
    Por isso sempre tenta os dois formatos pra esses 3 portais."""
    if s3:
        return [f"s3://{config.S3_BUCKET}/{portal}/{nome_arquivo}"]
    caminhos = [f"/data/evidencias/{portal}/{nome_arquivo}"]
    if portal in PORTAIS_NATIVOS_WINDOWS:
        caminhos.append(f"\\data\\evidencias\\{portal}\\{nome_arquivo}")
    return caminhos


def _zerar_url_evidencia(portal: str, nome_arquivo: str, s3: bool = False) -> int:
    caminhos_esperados = _caminhos_esperados(portal, nome_arquivo, s3)
    with get_session() as sessao:
        pedidos = (
            sessao.query(PedidoCertidao)
            .filter(PedidoCertidao.url_evidencia.in_(caminhos_esperados))
            .all()
        )
        if not pedidos:
            logger.warning(
                "[limpeza_evidencias] Arquivo removido sem pedido correspondente no banco (tentativas: %s)",
                caminhos_esperados,
            )
            return 0
        for pedido in pedidos:
            pedido.url_evidencia = None
        sessao.commit()
        return len(pedidos)
