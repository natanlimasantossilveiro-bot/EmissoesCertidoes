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

    @abstractmethod
    async def executar(self, pedido: PedidoCertidao) -> ResultadoEmissao:
        """Implementado pela camada de plataforma (nodriver, Playwright,
        etc.), não diretamente pelo portal concreto."""

    async def processar_pedido(self, pedido_id: str, tentativa: int) -> bool:
        """Callback plugado em certidoes_core.fila.consumir_fila. Retorna
        True (ack) pra qualquer resultado definitivo do portal — mesmo que
        seja erro de negócio — e False (retry/DLQ) só quando algo técnico
        impediu de sequer obter um resultado."""
        with get_session() as session:
            pedido = session.get(PedidoCertidao, pedido_id)
            if not pedido:
                print(f"[{self.portal}] Pedido {pedido_id} não encontrado no banco.")
                return True

            pedido.status = StatusPedido.PROCESSANDO
            pedido.tentativas = tentativa
            session.commit()

            try:
                resultado = await self.executar(pedido)
            except Exception as erro:
                print(f"[{self.portal}] Erro técnico ao processar {pedido_id}: {erro}")
                pedido.status = StatusPedido.ERRO_TECNICO
                pedido.mensagem = str(erro)
                session.commit()
                return False

            print(f"[{self.portal}] Pedido {pedido_id} processado — status: {resultado.status.value}")
            pedido.status = resultado.status
            pedido.mensagem = resultado.mensagem
            pedido.caminho_certidao = resultado.caminho_certidao
            pedido.url_evidencia = resultado.url_evidencia
            session.commit()

        return resultado.status != StatusPedido.ERRO_TECNICO

    def nome_arquivo_certidao(self, pedido: PedidoCertidao) -> str:
        return gerar_nome_certidao(pedido.nome, self.portal, pedido.documento, tipo=pedido.tipo)