"""
Serviço dedicado só pra apagar evidências e certidões antigas
periodicamente. Roda separado do Gateway de propósito: o Gateway monta os
volumes de cada worker como somente-leitura (:ro), decisão deliberada de
isolamento que não deve ser revertida — só este serviço tem acesso de
escrita nesses volumes (ver docker-compose.yml).

Sem scheduler nenhum configurado no projeto (sem Celery Beat, sem
APScheduler, sem cron) — um loop simples com `time.sleep` já resolve, dado
que é só uma checagem por hora, sem concorrência nem agendamento fino
necessário.

Os dois fluxos de limpeza (evidências e certidões) são módulos e critérios
completamente separados — ver limpeza_evidencias.py e limpeza_certidoes.py
em certidoes_core. Este arquivo só os chama em sequência, no mesmo laço.
"""
import logging
import time

from certidoes_core.limpeza_evidencias import limpar_evidencias_antigas
from certidoes_core.limpeza_certidoes import limpar_certidoes_antigas
from certidoes_core.config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cleanup-evidencias")

INTERVALO_SEGUNDOS = 3600
RETENCAO_EVIDENCIAS_HORAS = config.RETENCAO_EVIDENCIAS_HORAS
RETENCAO_CERTIDOES_HORAS = config.RETENCAO_CERTIDOES_HORAS


def executar_ciclo() -> None:
    try:
        limpar_evidencias_antigas(horas=RETENCAO_EVIDENCIAS_HORAS)
    except Exception:
        logger.exception("[cleanup-evidencias] Falha inesperada na limpeza de evidências")

    try:
        limpar_certidoes_antigas(horas=RETENCAO_CERTIDOES_HORAS)
    except Exception:
        logger.exception("[cleanup-evidencias] Falha inesperada na limpeza de certidões")


if __name__ == "__main__":
    logger.info(
        "[cleanup-evidencias] Iniciando — retenção evidências=%sh, certidões=%sh, intervalo=%ss",
        RETENCAO_EVIDENCIAS_HORAS, RETENCAO_CERTIDOES_HORAS, INTERVALO_SEGUNDOS,
    )
    while True:
        executar_ciclo()
        time.sleep(INTERVALO_SEGUNDOS)
