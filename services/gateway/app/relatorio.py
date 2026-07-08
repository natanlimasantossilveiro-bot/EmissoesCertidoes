"""
Gera o relatório consolidado de um lote (planilha) em .xlsx — pensado pra
quem subiu 50 pedidos de uma vez e quer um resumo pra baixar, em vez de
conferir pedido por pedido na tabela do front.
"""
import io
from openpyxl import Workbook
from openpyxl.styles import Font

from certidoes_core.banco import PedidoCertidao

STATUS_LABELS = {
    "pendente": "Pendente",
    "processando": "Processando",
    "sucesso_confirmado": "Sucesso",
    "sucesso_provavel": "Sucesso (provável)",
    "erro_portal": "Erro do portal",
    "erro_tecnico": "Erro técnico",
    "falha_indefinida": "Falha indefinida",
}

CABECALHO = [
    "Linha", "Nome", "Documento", "Tipo", "Status", "Mensagem",
    "Certidão (arquivo)", "Evidência (arquivo)", "Solicitado por",
    "Criado em", "Atualizado em",
]


def gerar_relatorio_lote(pedidos: list[PedidoCertidao]) -> bytes:
    workbook = Workbook()
    planilha = workbook.active
    planilha.title = "Relatório"

    planilha.append(CABECALHO)
    for celula in planilha[1]:
        celula.font = Font(bold=True)

    def nome_arquivo(caminho):
        if not caminho:
            return ""
        return caminho.replace("\\", "/").rsplit("/", 1)[-1]

    for pedido in sorted(pedidos, key=lambda p: int(p.linha_planilha or 0)):
        status_valor = pedido.status.value if hasattr(pedido.status, "value") else pedido.status
        planilha.append([
            pedido.linha_planilha or "",
            pedido.nome,
            pedido.documento,
            (pedido.tipo or "").upper(),
            STATUS_LABELS.get(status_valor, status_valor),
            pedido.mensagem or "",
            nome_arquivo(pedido.caminho_certidao),
            nome_arquivo(pedido.url_evidencia),
            pedido.solicitado_por or "",
            pedido.criado_em.strftime("%d/%m/%Y %H:%M") if pedido.criado_em else "",
            pedido.atualizado_em.strftime("%d/%m/%Y %H:%M") if pedido.atualizado_em else "",
        ])

    larguras = [8, 28, 18, 8, 20, 45, 32, 32, 18, 16, 16]
    for indice, largura in enumerate(larguras, start=1):
        planilha.column_dimensions[planilha.cell(row=1, column=indice).column_letter].width = largura

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()
