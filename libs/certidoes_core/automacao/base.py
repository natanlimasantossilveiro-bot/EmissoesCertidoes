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
from abc import ABC, abstractmethod
from dataclasses import dataclass

from certidoes_core.banco import get_session, PedidoCertidao, StatusPedido
from certidoes_core.config import config
from certidoes_core.nomenclatura import gerar_nome_certidao


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

    async def processar_pedido(self, pedido_id: str, tentativa: int) -> bool:
        """Callback plugado em certidoes_core.fila.consumir_fila. Retorna
        True (ack) pra qualquer resultado definitivo do portal — mesmo que
        seja erro de negócio — e False (retry/DLQ) só quando algo técnico
        impediu de sequer obter um resultado (e o portal não tem fallback
        manual configurado)."""
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
                resultado = await self.executar(pedido)
            except Exception as erro:
                print(f"[{self.portal}] Erro técnico ao processar {pedido_id}: {erro}")
                status_final = StatusPedido.ERRO_TECNICO
                mensagem_final = str(erro)
                if esgotou_tentativas and self.url_fallback_manual:
                    status_final = StatusPedido.AGUARDANDO_MANUAL
                    mensagem_final = self._texto_fallback_manual(pedido, mensagem_final)
                pedido.status = status_final
                pedido.mensagem = mensagem_final
                session.commit()
                return status_final != StatusPedido.ERRO_TECNICO

            print(f"[{self.portal}] Pedido {pedido_id} processado — status: {resultado.status.value}")
            status_final = resultado.status
            mensagem_final = resultado.mensagem
            if status_final == StatusPedido.ERRO_TECNICO and esgotou_tentativas and self.url_fallback_manual:
                status_final = StatusPedido.AGUARDANDO_MANUAL
                mensagem_final = self._texto_fallback_manual(pedido, mensagem_final)

            pedido.status = status_final
            pedido.mensagem = mensagem_final
            pedido.caminho_certidao = resultado.caminho_certidao
            pedido.url_evidencia = resultado.url_evidencia
            session.commit()

        return status_final != StatusPedido.ERRO_TECNICO

    def nome_arquivo_certidao(self, pedido: PedidoCertidao) -> str:
        return gerar_nome_certidao(pedido.nome, self.portal, pedido.documento, tipo=pedido.tipo)