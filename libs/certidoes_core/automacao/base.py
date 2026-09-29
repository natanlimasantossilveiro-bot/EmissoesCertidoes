"""
Contrato comum a qualquer worker de automação de portal. Centraliza o que
não pode depender de cada dev lembrar de fazer: transição de status no
banco, contagem de tentativa, nomeação padronizada do arquivo final, e
qual resultado é retry-ável.

Esta classe não sabe nada sobre navegador — isso é responsabilidade da
camada de plataforma (ex: AutomacaoNodriverBase, em nodriver_base.py), que
herda daqui e implementa `executar()`, inclusive a regra de "sempre
capturar evidência quando não for sucesso confirmado". Cada portal
concreto herda da camada de plataforma certa e implementa só a automação
específica dele.
"""
import asyncio
from abc import ABC, abstractmethod
from contextlib import nullcontext
from dataclasses import dataclass

from certidoes_core.banco import get_session, PedidoCertidao, StatusPedido
from certidoes_core.concorrencia import vaga_navegador
from certidoes_core.config import config
from certidoes_core.nomenclatura import gerar_nome_certidao


class PortalInacessivel(Exception):
    """O portal nem respondeu a uma conexão simples a partir do servidor —
    não adianta abrir navegador (ver `verificar_portal_acessivel`)."""


@dataclass
class ResultadoEmissao:
    status: StatusPedido
    mensagem: str = ""
    caminho_certidao: str = ""   # já no destino final, preenchido só em sucesso
    url_evidencia: str = ""      # preenchido pela camada de plataforma quando aplicável


class AutomacaoPortal(ABC):
    """Uma instância concreta por portal (ex: CertidaoConjunta). `portal`
    deve bater com a chave usada no Gateway (PORTAIS_DISPONIVEIS) e com o
    nome da fila em certidoes_core.fila."""

    portal: str

    # Opt-in por portal concreto (ex: worker-sefaz-pr). Quando setado, a
    # última tentativa que terminar em ERRO_TECNICO vira AGUARDANDO_MANUAL
    # em vez de ir pra DLQ — usado só em portais sem solução de automação
    # garantida (bloqueio por fingerprint avançado, não IP/SO). Default
    # None preserva o comportamento atual (DLQ) pra todo o resto.
    url_fallback_manual: str | None = None

    # Opt-in por portal concreto (ex: worker-tst-cndt). Quando setado,
    # `executar()` é cancelado se passar desse tempo sem terminar — vira
    # ERRO_TECNICO igual a qualquer outra falha técnica (mesmo ciclo de
    # retentativa/DLQ), em vez de travar a fila pra sempre (prefetch=1
    # significa que uma automação travada sem exceção nenhuma — nodriver
    # esperando um elemento que nunca aparece, por exemplo — nunca dá
    # ack/nack, e nenhum pedido novo desse portal é processado até
    # alguém perceber e reiniciar o container manualmente; confirmado em
    # produção em 14/09/2026 no TST CNDT). Default None preserva o
    # comportamento atual (sem limite) pra todo o resto.
    timeout_execucao_segundos: int | None = None

    @abstractmethod
    async def executar(self, pedido: PedidoCertidao) -> ResultadoEmissao:
        """Implementado pela camada de plataforma (nodriver, Playwright,
        etc.), não diretamente pelo portal concreto."""

    def _texto_fallback_manual(self, pedido: PedidoCertidao, mensagem_automacao: str) -> str:
        return (
            f"Automação esgotou {config.MAX_TENTATIVAS} tentativas sem solução garantida — "
            f"acesse manualmente em {self.url_fallback_manual} (documento: {pedido.documento}). "
            f"Depois de emitir no site, anexe o PDF pelo painel. "
            f"Último resultado da automação: {mensagem_automacao}"
        )

    async def verificar_portal_acessivel(self) -> str | None:
        """Checagem rápida, ANTES de pegar vaga e abrir navegador. Devolve a
        explicação se o portal está inacessível a partir do servidor, ou
        None se deu pra conectar. Default: não checa nada — implementado na
        camada de plataforma (ver AutomacaoNodriverBase)."""
        return None

    async def processar_pedido(self, pedido_id: str, tentativa: int) -> bool:
        """Callback plugado em certidoes_core.fila.consumir_fila. Retorna
        True (ack) pra qualquer resultado definitivo do portal — mesmo que
        seja erro de negócio — e False (retry/DLQ) só quando algo técnico
        impediu de sequer obter um resultado (e o portal não tem fallback
        manual configurado)."""
        # Portal fora do alcance (ex: TST bloqueando o IP do VPS em
        # 29/09/2026) falhava só depois de 3 × 300s de timeout, com a fila
        # do portal parada e o pedido "processando" por 15 minutos. Agora
        # falha em segundos, com a causa real, sem ocupar vaga de navegador.
        motivo_inacessivel = await self.verificar_portal_acessivel()

        # Espera a vaga ANTES de marcar PROCESSANDO: enquanto aguarda a vez
        # (ver concorrencia.py), o pedido continua "pendente" no painel.
        async with (nullcontext() if motivo_inacessivel else vaga_navegador(self.portal)):
            return await self._processar_pedido_com_vaga(pedido_id, tentativa, motivo_inacessivel)

    async def _processar_pedido_com_vaga(self, pedido_id: str, tentativa: int, motivo_inacessivel: str | None) -> bool:
        with get_session() as session:
            pedido = session.get(PedidoCertidao, pedido_id)
            if not pedido:
                print(f"[{self.portal}] Pedido {pedido_id} não encontrado no banco.")
                return True

            pedido.status = StatusPedido.PROCESSANDO
            pedido.tentativas = tentativa
            session.commit()

            esgotou_tentativas = tentativa >= config.MAX_TENTATIVAS

            try:
                if motivo_inacessivel:
                    raise PortalInacessivel(motivo_inacessivel)
                if self.timeout_execucao_segundos:
                    resultado = await asyncio.wait_for(
                        self.executar(pedido), timeout=self.timeout_execucao_segundos
                    )
                else:
                    resultado = await self.executar(pedido)
                status_bruto = resultado.status
                mensagem_bruta = resultado.mensagem
                caminho_certidao = resultado.caminho_certidao
                url_evidencia = resultado.url_evidencia
            except asyncio.TimeoutError:
                print(f"[{self.portal}] Pedido {pedido_id} excedeu {self.timeout_execucao_segundos}s "
                      f"sem terminar — provável travamento, cancelando.")
                status_bruto = StatusPedido.ERRO_TECNICO
                mensagem_bruta = (
                    f"Automação excedeu o limite de {self.timeout_execucao_segundos}s sem terminar — "
                    f"provável travamento técnico, não erro do documento."
                )
                caminho_certidao = ""
                url_evidencia = ""
            except Exception as erro:
                print(f"[{self.portal}] Erro técnico ao processar {pedido_id}: {erro}")
                status_bruto = StatusPedido.ERRO_TECNICO
                mensagem_bruta = str(erro)
                caminho_certidao = ""
                url_evidencia = ""
            else:
                print(f"[{self.portal}] Pedido {pedido_id} processado — status: {status_bruto.value}")

            # Enquanto ainda houver retentativa automática programada (ver
            # fila.py), o pedido continua "processando" pro usuário — gravar
            # ERRO_TECNICO como se fosse definitivo, só pra a tentativa
            # seguinte sobrescrever com SUCESSO_CONFIRMADO minutos depois, é
            # mais confuso que informativo (gera o "processando → erro →
            # sucesso" que aparece pro colaborador no painel). Só grava um
            # status diferente de PROCESSANDO quando o resultado é
            # definitivo (sucesso/erro de negócio) ou as tentativas
            # realmente se esgotaram.
            if status_bruto == StatusPedido.ERRO_TECNICO and not esgotou_tentativas:
                return False

            status_final = status_bruto
            mensagem_final = mensagem_bruta
            if status_final == StatusPedido.ERRO_TECNICO and self.url_fallback_manual:
                status_final = StatusPedido.AGUARDANDO_MANUAL
                mensagem_final = self._texto_fallback_manual(pedido, mensagem_final)

            pedido.status = status_final
            pedido.mensagem = mensagem_final
            pedido.caminho_certidao = caminho_certidao
            pedido.url_evidencia = url_evidencia
            session.commit()

        return status_final != StatusPedido.ERRO_TECNICO

    def nome_arquivo_certidao(self, pedido: PedidoCertidao) -> str:
        return gerar_nome_certidao(pedido.nome, self.portal, pedido.documento, tipo=pedido.tipo)