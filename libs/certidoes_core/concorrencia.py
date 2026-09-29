"""
Limite GLOBAL de navegadores abertos ao mesmo tempo, somando todos os
workers/portais. Cada portal tem a própria fila e o próprio container, e
prefetch=1 só garante "um pedido por vez POR PORTAL" — um pedido marcando 9
portais abria 9 Chromes de uma vez. No VPS (2 núcleos) isso derrubava tudo:
confirmado em 29/09/2026, carga 7,4 com 17 Chromes abrindo juntos, gerando
"Failed to connect to browser", captcha que não carregava e timeouts.

Mecanismo: named locks do próprio MySQL (GET_LOCK), um por vaga
("certidoes_navegador_0", "..._1", ...). Já é o banco central que todo
worker usa, então não precisa de nenhum serviço novo. O lock pertence à
CONEXÃO: se o worker morrer (OOM, deploy, crash), o MySQL fecha a conexão
e a vaga volta sozinha — nunca fica uma vaga "presa" pra sempre.
"""
import asyncio
import random
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from certidoes_core.config import config

_PREFIXO_LOCK = "certidoes_navegador_"
_INTERVALO_ESPERA_SEGUNDOS = 2

_engine_vagas = None


def _obter_engine_vagas():
    # NullPool de propósito: conexão fechada é fechada DE VERDADE. Com o
    # pool padrão, a conexão voltaria pro pool ainda segurando o lock se o
    # RELEASE_LOCK falhasse — e a vaga ficaria ocupada sem ninguém usando.
    global _engine_vagas
    if _engine_vagas is None:
        _engine_vagas = create_engine(config.DATABASE_URL, poolclass=NullPool)
    return _engine_vagas


def _tentar_pegar_vaga(conexao, total_vagas: int) -> int | None:
    # Ordem embaralhada: sem isso todo worker tentaria a vaga 0 primeiro,
    # e as de número alto ficariam sempre sobrando pra quem chegasse depois.
    for indice in random.sample(range(total_vagas), total_vagas):
        obtido = conexao.execute(
            text("SELECT GET_LOCK(:nome, 0)"), {"nome": f"{_PREFIXO_LOCK}{indice}"}
        ).scalar()
        if obtido == 1:
            return indice
    return None


@asynccontextmanager
async def vaga_navegador(portal: str):
    """Uso: `async with vaga_navegador(self.portal): ...abre o navegador...`

    Espera (sem bloquear o event loop — o heartbeat do RabbitMQ continua
    funcionando) até existir uma das MAX_NAVEGADORES_SIMULTANEOS vagas
    livres. Com MAX_NAVEGADORES_SIMULTANEOS=0, ou fora do MySQL (ex:
    testes com SQLite), não limita nada."""
    total_vagas = config.MAX_NAVEGADORES_SIMULTANEOS
    engine = _obter_engine_vagas()
    if total_vagas <= 0 or engine.dialect.name != "mysql":
        yield None
        return

    conexao = await asyncio.to_thread(engine.connect)
    vaga = None
    avisou_espera = False
    try:
        while vaga is None:
            vaga = await asyncio.to_thread(_tentar_pegar_vaga, conexao, total_vagas)
            if vaga is None:
                if not avisou_espera:
                    print(f"[{portal}] Todas as {total_vagas} vagas de navegador ocupadas — aguardando a vez.")
                    avisou_espera = True
                await asyncio.sleep(_INTERVALO_ESPERA_SEGUNDOS + random.random())
        yield vaga
    finally:
        if vaga is not None:
            try:
                await asyncio.to_thread(
                    lambda: conexao.execute(
                        text("SELECT RELEASE_LOCK(:nome)"), {"nome": f"{_PREFIXO_LOCK}{vaga}"}
                    )
                )
            except Exception as erro:
                # Fechar a conexão abaixo libera o lock de qualquer jeito.
                print(f"[{portal}] Aviso ao liberar vaga de navegador {vaga}: {erro}")
        await asyncio.to_thread(conexao.close)
