"""
Gateway: única porta de entrada. Responsabilidade única é validar,
gravar no banco com status PENDENTE, e publicar na fila do portal certo.
Nunca abre navegador nem espera o resultado — por isso responde rápido
mesmo com fila cheia.
"""
import os
import secrets

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response, Depends, Header
from fastapi.middleware.cors import CORSMiddleware

from certidoes_core.banco import get_session, criar_tabelas, PedidoCertidao, LotePlanilha, StatusPedido
from certidoes_core.fila import publicar_pedido

from app.planilha import ler_planilha_certidoes  # parser adaptado, ver services/gateway/app/planilha.py
from app.relatorio import gerar_relatorio_lote  # ver services/gateway/app/relatorio.py
from app.dlq import status_dlq_todos_portais  # ver services/gateway/app/dlq.py

# Autenticação simples por chave fixa (header X-API-Key) — o suficiente pra
# tirar o Gateway de "totalmente aberto" sem exigir infraestrutura de login
# de verdade. Se GATEWAY_API_KEY não estiver configurada (dev local), a
# checagem é pulada — nunca trava quem está só testando sem .env.
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")


def verificar_api_key(x_api_key: str = Header(default="")):
    if not GATEWAY_API_KEY:
        return
    if not secrets.compare_digest(x_api_key, GATEWAY_API_KEY):
        raise HTTPException(401, "API key inválida ou ausente (header X-API-Key).")


app = FastAPI(title="Certidões Gateway", dependencies=[Depends(verificar_api_key)])

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajustar pra domínio do front em produção
    allow_methods=["*"],
    allow_headers=["*"],
)

# Portais habilitados. Adicionar um novo portal aqui = criar o worker
# correspondente escutando essa fila. O Gateway não precisa saber MAIS
# NADA sobre como aquele portal funciona.
PORTAIS_DISPONIVEIS = {
    "receita_federal": "Certidão Conjunta PF/PJ - Receita Federal",
    "cpf_situacao_cadastral": "CPF - Situação Cadastral - Receita Federal",
    "cnpj_qsa": "CNPJ + QSA - Comprovante de Inscrição e Situação Cadastral - Receita Federal",
    "tst_cndt": "Certidão Negativa de Débitos Trabalhistas - TST",
    "trf4_certidao_civel_criminal": "Certidão Judicial Cível/Criminal/Eleitoral - JFPR/TRF4",
    "curitiba_certidao_cadastro_imovel": "Certidão de Cadastro de Imóvel - Prefeitura de Curitiba",
    "curitiba_consulta_debitos_divida_ativa": "Consulta de Débitos - Dívida Ativa - Prefeitura de Curitiba",
    # atendenet_pinhais entra aqui quando o worker existir de fato — listar
    # sem worker deixa o pedido "pendente" pra sempre, sem ninguém consumindo
    # a fila, o que parece bug numa demonstração
    # próximos portais entram aqui conforme forem sendo automatizados
}


@app.on_event("startup")
def startup():
    criar_tabelas()


@app.get("/portais")
def listar_portais():
    return PORTAIS_DISPONIVEIS


@app.get("/dlq/status")
def consultar_status_dlq():
    """Quantas mensagens estão presas na fila `<portal>.dlq` de cada
    portal — pedidos que falharam `MAX_TENTATIVAS` vezes seguidas e
    precisam de inspeção manual. Consulta a API de management do
    RabbitMQ (ver `app/dlq.py`)."""
    return status_dlq_todos_portais(list(PORTAIS_DISPONIVEIS.keys()))


@app.post("/pedidos")
def criar_pedido_unitario(
    portal: str = Form(...),
    nome: str = Form(...),
    tipo: str = Form(None),
    documento: str = Form(...),
    data_nascimento: str = Form(""),
    solicitado_por: str = Form(None),
):
    if portal not in PORTAIS_DISPONIVEIS:
        raise HTTPException(400, f"Portal '{portal}' não habilitado.")

    with get_session() as session:
        pedido = PedidoCertidao(
            portal=portal,
            nome=nome,
            tipo=tipo,
            documento=documento,
            data_nascimento=data_nascimento,
            solicitado_por=solicitado_por,
            status=StatusPedido.PENDENTE,
        )
        session.add(pedido)
        session.commit()
        session.refresh(pedido)

    publicar_pedido(portal, pedido.id)

    return {"pedido_id": pedido.id, "status": "pendente"}


@app.post("/pedidos/planilha")
async def criar_pedidos_planilha(
    portal: str = Form(...),
    planilha: UploadFile = File(...),
    solicitado_por: str = Form(None),
):
    if portal not in PORTAIS_DISPONIVEIS:
        raise HTTPException(400, f"Portal '{portal}' não habilitado.")

    conteudo = await planilha.read()
    registros, erros = ler_planilha_certidoes(conteudo)

    with get_session() as session:
        lote = LotePlanilha(
            nome_arquivo_original=planilha.filename,
            total_linhas=str(len(registros)),
            solicitado_por=solicitado_por,
        )
        session.add(lote)
        session.commit()
        session.refresh(lote)
        # Guarda o valor puro antes do loop: cada commit() dentro dele expira
        # os atributos de TODOS os objetos da sessão (não só o pedido recém
        # commitado), inclusive `lote` — acessar `lote.id` depois que a
        # sessão fechar (fora do `with`) dispararia DetachedInstanceError.
        lote_id_valor = lote.id

        ids_publicados = []
        for registro in registros:
            pedido = PedidoCertidao(
                portal=portal,
                nome=registro["nome"],
                tipo=registro.get("tipo"),
                documento=registro["documento"],
                data_nascimento=registro.get("data_nascimento", ""),
                lote_id=lote_id_valor,
                linha_planilha=str(registro["linha"]),
                solicitado_por=solicitado_por,
                status=StatusPedido.PENDENTE,
            )
            session.add(pedido)
            session.commit()
            session.refresh(pedido)
            ids_publicados.append(pedido.id)

    # Publica todos na fila só depois de garantir que gravou tudo no banco
    for pedido_id in ids_publicados:
        publicar_pedido(portal, pedido_id)

    return {
        "lote_id": lote_id_valor,
        "total_validos": len(registros),
        "total_erros_validacao": len(erros),
        "erros": erros,
    }


@app.get("/pedidos/{pedido_id}")
def consultar_pedido(pedido_id: str):
    with get_session() as session:
        pedido = session.get(PedidoCertidao, pedido_id)
        if not pedido:
            raise HTTPException(404, "Pedido não encontrado.")
        return {
            "id": pedido.id,
            "portal": pedido.portal,
            "nome": pedido.nome,
            "documento": pedido.documento,
            "status": pedido.status,
            "mensagem": pedido.mensagem,
            "caminho_certidao": pedido.caminho_certidao,
            "url_evidencia": pedido.url_evidencia,
        }


@app.get("/lotes/{lote_id}")
def consultar_lote(lote_id: str):
    with get_session() as session:
        pedidos = session.query(PedidoCertidao).filter_by(lote_id=lote_id).all()
        if not pedidos:
            raise HTTPException(404, "Lote não encontrado ou vazio.")

        resumo = {"pendente": 0, "processando": 0, "sucesso": 0, "erro": 0}
        for p in pedidos:
            if p.status == StatusPedido.PENDENTE:
                resumo["pendente"] += 1
            elif p.status == StatusPedido.PROCESSANDO:
                resumo["processando"] += 1
            elif p.status in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
                resumo["sucesso"] += 1
            else:
                resumo["erro"] += 1

        return {
            "lote_id": lote_id,
            "total": len(pedidos),
            "resumo": resumo,
            "pedidos": [
                {
                    "id": p.id,
                    "nome": p.nome,
                    "documento": p.documento,
                    "status": p.status,
                    "mensagem": p.mensagem,
                    "caminho_certidao": p.caminho_certidao,
                }
                for p in pedidos
            ],
        }


@app.get("/lotes/{lote_id}/relatorio")
def baixar_relatorio_lote(lote_id: str):
    """Relatório consolidado do lote em .xlsx — pra quem subiu uma planilha
    com várias linhas e quer um resumo pra baixar, em vez de conferir
    pedido por pedido na tabela do front. Pode ser gerado a qualquer
    momento, mesmo com pedidos ainda pendente/processando."""
    with get_session() as session:
        pedidos = session.query(PedidoCertidao).filter_by(lote_id=lote_id).all()
        if not pedidos:
            raise HTTPException(404, "Lote não encontrado ou vazio.")

        conteudo = gerar_relatorio_lote(pedidos)

    return Response(
        content=conteudo,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="relatorio_lote_{lote_id[:8]}.xlsx"'},
    )
