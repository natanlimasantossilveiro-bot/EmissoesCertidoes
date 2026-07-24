"""
Limpeza automática de certidões emitidas antigas (o PDF final salvo em
PASTA_CERTIDOES_EMITIDAS por aguardar_e_mover_pdf/salvar_pagina_como_pdf em
automacao/nodriver_base.py).

Fluxo deliberadamente separado de limpeza_evidencias.py, mesmo rodando no
mesmo scheduler: uma certidão só pode ser apagada se `upload_confirmado`
estiver True no(s) pedido(s) correspondente(s) — ao contrário da evidência,
que não tem (e não precisa de) confirmação nenhuma.

⚠️ Hoje NENHUM fluxo do sistema envia a certidão pra nuvem do escritório
nem seta upload_confirmado como True em lugar nenhum — esse campo existe só
como trava de segurança, pronto pro dia que esse envio for implementado.
Até lá, esta função nunca apaga nenhuma certidão de verdade: só identifica
as com mais de `horas` e loga um alerta pedindo revisão manual. Isso é
esperado, não um bug.
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path

from certidoes_core import storage
from certidoes_core.banco import PedidoCertidao, get_session
from certidoes_core.config import config

logger = logging.getLogger("limpeza_certidoes")

RAIZ_VOLUMES_LOCAIS = Path("/evidencias-workers")

# Mesma ressalva de limpeza_evidencias.py: esses 3 portais rodam nativos no
# Windows (fora do Docker) e gravam caminho_certidao com separador de
# caminho do próprio SO (barra invertida), diferente dos demais (Linux).
PORTAIS_NATIVOS_WINDOWS = {"receita_federal", "sefaz_pr_certidao_debitos", "atendenet_pinhais_cnd"}


def limpar_certidoes_antigas(horas: int = 72) -> dict:
    """Certidões emitidas nunca passaram pela abstração local/S3 do
    storage.py (ver aviso em nodriver_base.py: caminho hardcoded em disco
    local) — então, ao contrário da limpeza de evidências, aqui só existe
    o caminho local por enquanto."""
    corte = datetime.utcnow() - timedelta(hours=horas)
    removidos = registros_atualizados = alertas = erros = 0

    if not RAIZ_VOLUMES_LOCAIS.exists():
        logger.warning("[limpeza_certidoes] Raiz %s não existe — nenhum volume montado?", RAIZ_VOLUMES_LOCAIS)
        return {"removidos": 0, "registros_atualizados": 0, "alertas": 0, "erros": 0}

    for pasta_portal in sorted(RAIZ_VOLUMES_LOCAIS.iterdir()):
        if not pasta_portal.is_dir():
            continue
        portal = pasta_portal.name
        pasta_certidoes = pasta_portal / "certidoes_emitidas"

        # Nota: os 3 portais nativos do Windows (PORTAIS_NATIVOS_WINDOWS)
        # compartilham a mesma pasta física C:\data\certidoes_emitidas (ela
        # não tem subpasta por portal, ao contrário de evidências) — então
        # esse laço passa 3x pelos mesmos arquivos sob aliases diferentes.
        # Sem problema: o segundo/terceiro passe só encontra os arquivos já
        # removidos no primeiro (unlink é idempotente) ou repete o mesmo
        # alerta pros que ainda não podem ser removidos — nunca duplica
        # remoção nem apaga registro errado.
        for arquivo in storage.listar_arquivos_locais(pasta_certidoes):
            # gerar_nome_certidao não embute timestamp no nome (duas
            # requisições pra mesma pessoa/portal/documento geram o mesmo
            # nome de arquivo, uma sobrescrevendo a outra) — diferente da
            # evidência, aqui só dá pra confiar na data de modificação.
            criado_em = datetime.utcfromtimestamp(arquivo.stat().st_mtime)
            if criado_em >= corte:
                continue

            pedidos = _pedidos_correspondentes(portal, arquivo.name)
            if not pedidos:
                logger.warning(
                    "[limpeza_certidoes] Certidão antiga (>%sh) sem pedido correspondente no banco: %s/%s",
                    horas, portal, arquivo.name,
                )
                alertas += 1
                continue

            if not all(p.upload_confirmado for p in pedidos):
                ids_pendentes = [p.id for p in pedidos if not p.upload_confirmado]
                logger.warning(
                    "[limpeza_certidoes] ALERTA: certidão antiga (>%sh) sem upload confirmado pra nuvem do "
                    "escritório — revisão manual necessária. Arquivo: %s/%s. Pedido(s) pendente(s): %s",
                    horas, portal, arquivo.name, ids_pendentes,
                )
                alertas += 1
                continue

            try:
                storage.remover_arquivo_local(arquivo)
                removidos += 1
            except OSError as erro:
                erros += 1
                logger.warning("[limpeza_certidoes] Falha ao remover %s: %s", arquivo, erro)
                continue

            registros_atualizados += _zerar_caminho_certidao(pedidos)

    logger.info(
        "[limpeza_certidoes] corte=%sh removidos=%s registros_atualizados=%s alertas=%s erros=%s",
        horas, removidos, registros_atualizados, alertas, erros,
    )
    return {
        "removidos": removidos,
        "registros_atualizados": registros_atualizados,
        "alertas": alertas,
        "erros": erros,
    }


def _pedidos_correspondentes(portal: str, nome_arquivo: str) -> list[PedidoCertidao]:
    """Casa pelo caminho local completo, exatamente como
    aguardar_e_mover_pdf/salvar_pagina_como_pdf gravam em
    pedido.caminho_certidao (PASTA_CERTIDOES_EMITIDAS + nome do arquivo).

    ⚠️ Confirmado direto na base (não suposição): os 3 portais nativos do
    Windows gravam sem letra de unidade ("\\data\\certidoes_emitidas\\...",
    porque `Path("/data/...")` no Windows não vira "C:\\..." sozinho — fica
    relativo à unidade atual). Tenta os dois formatos (posix e barra
    invertida) pra não deixar passar registro antigo gravado antes da
    migração desses workers pra processo nativo."""
    caminhos_esperados = [f"/data/certidoes_emitidas/{nome_arquivo}"]
    if portal in PORTAIS_NATIVOS_WINDOWS:
        caminhos_esperados.append(f"\\data\\certidoes_emitidas\\{nome_arquivo}")
    with get_session() as sessao:
        return (
            sessao.query(PedidoCertidao)
            .filter(PedidoCertidao.caminho_certidao.in_(caminhos_esperados))
            .all()
        )


def _zerar_caminho_certidao(pedidos: list[PedidoCertidao]) -> int:
    atualizados = 0
    with get_session() as sessao:
        for pedido_antigo in pedidos:
            pedido = sessao.get(PedidoCertidao, pedido_antigo.id)
            if pedido and pedido.caminho_certidao:
                pedido.caminho_certidao = None
                atualizados += 1
        sessao.commit()
    return atualizados
