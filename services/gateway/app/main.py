"""
Gateway: única porta de entrada. Responsabilidade única é validar,
gravar no banco com status PENDENTE, e publicar na fila do portal certo.
Nunca abre navegador nem espera o resultado — por isso responde rápido
mesmo com fila cheia.
"""
from pathlib import Path
from datetime import datetime, timezone
from pydantic import BaseModel, EmailStr
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from certidoes_core.banco import (
    get_session, criar_tabelas, PedidoCertidao, LotePlanilha, StatusPedido, Usuario, PapelUsuario,
)
from certidoes_core.fila import publicar_pedido

from app.planilha import ler_planilha_certidoes  # parser adaptado, ver services/gateway/app/planilha.py
from app.relatorio import gerar_relatorio_lote  # ver services/gateway/app/relatorio.py
from app.dlq import status_dlq_todos_portais  # ver services/gateway/app/dlq.py
from app.auth import (
    autenticar, criar_token, obter_usuario_atual, exigir_admin,
    gerar_hash_senha, verificar_senha, bootstrap_admin_inicial,
)

app = FastAPI(title="Certidões Gateway")

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
    "sefaz_pr_certidao_debitos": "Certidão de Débitos Tributários e Dívida Ativa - SEFAZ PR",
    "atendenet_pinhais_cnd": "Certidão Negativa de Débitos (CND) - Prefeitura de Pinhais",
    "fgts_caixa": "Situação de Regularidade do Empregador (FGTS) - Caixa Econômica Federal",
    "mpf_certidao_negativa": "Certidão Negativa - Ministério Público Federal",
    "curitiba_cnd_cpf": "Certidão de Tributos Municipais - Pessoa Física - Prefeitura de Curitiba",
    "curitiba_certidao_tributos_imovel": "Certidão de Tributos Municipais - Imóvel - Prefeitura de Curitiba",
    "trt9_certidao_trabalhista": "Certidão Trabalhista - PJe TRT9",
    "mpt_certidao_negativa": "Certidão Negativa de Feitos - Ministério Público do Trabalho",
    # próximos portais entram aqui conforme forem sendo automatizados
    # (lembre de adicionar também em GRUPO_DOCUMENTO_POR_PORTAL, abaixo)
}

# Pedido/planilha com múltiplos portais marcados de uma vez só faz sentido
# se todos pedirem o MESMO tipo de documento — não dá pra ter um campo só
# de "documento" servindo CPF/CNPJ pra uns e Indicação Fiscal pra outros
# ao mesmo tempo. Isso é o que garante que a seleção múltipla (pedido
# avulso ou planilha) nunca mistura as duas naturezas de consulta.
GRUPO_CPF_CNPJ = "cpf_cnpj"
GRUPO_IMOVEL = "imovel"
GRUPO_DOCUMENTO_POR_PORTAL = {
    "receita_federal": GRUPO_CPF_CNPJ,
    "cpf_situacao_cadastral": GRUPO_CPF_CNPJ,
    "cnpj_qsa": GRUPO_CPF_CNPJ,
    "tst_cndt": GRUPO_CPF_CNPJ,
    "trf4_certidao_civel_criminal": GRUPO_CPF_CNPJ,
    "sefaz_pr_certidao_debitos": GRUPO_CPF_CNPJ,
    "atendenet_pinhais_cnd": GRUPO_CPF_CNPJ,
    "fgts_caixa": GRUPO_CPF_CNPJ,
    "mpf_certidao_negativa": GRUPO_CPF_CNPJ,
    "curitiba_cnd_cpf": GRUPO_CPF_CNPJ,
    "trt9_certidao_trabalhista": GRUPO_CPF_CNPJ,
    "mpt_certidao_negativa": GRUPO_CPF_CNPJ,
    "curitiba_certidao_cadastro_imovel": GRUPO_IMOVEL,
    "curitiba_consulta_debitos_divida_ativa": GRUPO_IMOVEL,
    "curitiba_certidao_tributos_imovel": GRUPO_IMOVEL,
}


def _validar_portais_mesmo_grupo(portais: list[str]) -> None:
    if not portais:
        raise HTTPException(400, "Selecione ao menos um portal.")
    invalidos = [p for p in portais if p not in PORTAIS_DISPONIVEIS]
    if invalidos:
        raise HTTPException(400, f"Portal(is) não habilitado(s): {', '.join(invalidos)}.")
    grupos = {GRUPO_DOCUMENTO_POR_PORTAL.get(p, p) for p in portais}
    if len(grupos) > 1:
        raise HTTPException(
            400,
            "Não é possível selecionar portais de CPF/CNPJ junto com portais de "
            "Indicação Fiscal (imóvel) no mesmo pedido — envie em pedidos separados.",
        )

# Cada worker grava certidão/evidência no PRÓPRIO volume Docker (isolado
# dos outros containers) — sem isso, o caminho salvo em
# `caminho_certidao`/`url_evidencia` (ex: "/data/certidoes_emitidas/x.pdf")
# não existe do ponto de vista do Gateway. O docker-compose.yml monta o
# volume de cada worker aqui também, só leitura, num subcaminho próprio
# por portal — isso é só o mapa de qual subcaminho corresponde a qual
# portal, pra resolver o arquivo certo na hora do download.
CAMINHO_DADOS_POR_PORTAL = {
    "receita_federal": "/dados-workers/receita_federal",
    "cpf_situacao_cadastral": "/dados-workers/cpf_situacao_cadastral",
    "cnpj_qsa": "/dados-workers/cnpj_qsa",
    "tst_cndt": "/dados-workers/tst_cndt",
    "trf4_certidao_civel_criminal": "/dados-workers/trf4_certidao_civel_criminal",
    "curitiba_certidao_cadastro_imovel": "/dados-workers/curitiba_certidao_cadastro_imovel",
    "curitiba_consulta_debitos_divida_ativa": "/dados-workers/curitiba_consulta_debitos_divida_ativa",
    "sefaz_pr_certidao_debitos": "/dados-workers/sefaz_pr_certidao_debitos",
    "atendenet_pinhais_cnd": "/dados-workers/atendenet_pinhais_cnd",
    "fgts_caixa": "/dados-workers/fgts_caixa",
    "mpf_certidao_negativa": "/dados-workers/mpf_certidao_negativa",
    "curitiba_cnd_cpf": "/dados-workers/curitiba_cnd_cpf",
    "curitiba_certidao_tributos_imovel": "/dados-workers/curitiba_certidao_tributos_imovel",
    "trt9_certidao_trabalhista": "/dados-workers/trt9_certidao_trabalhista",
    "mpt_certidao_negativa": "/dados-workers/mpt_certidao_negativa",
}


def _resolver_arquivo_worker(portal: str, caminho_gravado: str | None) -> Path | None:
    """Traduz o caminho gravado no banco (visão de dentro do worker, ex:
    "/data/certidoes_emitidas/x.pdf") pro caminho equivalente dentro do
    Gateway (visão pelo mount read-only, ex:
    "/dados-workers/receita_federal/certidoes_emitidas/x.pdf")."""
    base = CAMINHO_DADOS_POR_PORTAL.get(portal)
    if not base or not caminho_gravado:
        return None
    partes = caminho_gravado.replace("\\", "/").split("/data/", 1)
    if len(partes) != 2:
        return None
    caminho = Path(base) / partes[1]
    return caminho if caminho.is_file() else None


@app.on_event("startup")
def startup():
    criar_tabelas()
    bootstrap_admin_inicial()


# ---------- autenticação ----------

class LoginRequest(BaseModel):
    email: EmailStr
    senha: str


def _marcar_utc(dt: datetime | None) -> datetime | None:
    """Os horários são gravados com `datetime.utcnow()` (sem timezone).
    Sem marcar explicitamente como UTC aqui, o front recebe um ISO sem
    'Z'/offset e o navegador interpreta como se já fosse hora local
    (`new Date(iso)`), mostrando 3h a mais que o horário real de
    Brasília. Isso só ajusta o dado na saída da API — não mexe no que
    está gravado no banco."""
    return dt.replace(tzinfo=timezone.utc) if dt else None


def _usuario_para_json(usuario: Usuario) -> dict:
    return {
        "id": usuario.id,
        "nome": usuario.nome,
        "email": usuario.email,
        "papel": usuario.papel,
        "ativo": usuario.ativo,
        "ultimo_acesso_em": _marcar_utc(usuario.ultimo_acesso_em),
    }


@app.post("/auth/login")
def login(dados: LoginRequest):
    usuario = autenticar(dados.email, dados.senha)
    return {"access_token": criar_token(usuario), "usuario": _usuario_para_json(usuario)}


@app.get("/auth/me")
def quem_sou_eu(usuario: Usuario = Depends(obter_usuario_atual)):
    return _usuario_para_json(usuario)


class TrocarSenhaRequest(BaseModel):
    senha_atual: str
    nova_senha: str


@app.patch("/auth/me/senha")
def trocar_minha_senha(dados: TrocarSenhaRequest, usuario: Usuario = Depends(obter_usuario_atual)):
    """Todo usuário troca a própria senha sem depender de um admin —
    importante principalmente pro admin inicial, cuja senha de bootstrap
    veio de uma variável de ambiente e deve ser trocada assim que possível."""
    with get_session() as session:
        usuario_db = session.get(Usuario, usuario.id)
        if not verificar_senha(dados.senha_atual, usuario_db.senha_hash):
            raise HTTPException(401, "Senha atual incorreta.")
        usuario_db.senha_hash = gerar_hash_senha(dados.nova_senha)
        session.commit()
    return {"ok": True}


# ---------- administração de usuários ----------

class CriarUsuarioRequest(BaseModel):
    nome: str
    email: EmailStr
    senha: str
    papel: PapelUsuario = PapelUsuario.COLABORADOR


class AtualizarUsuarioRequest(BaseModel):
    ativo: bool | None = None
    papel: PapelUsuario | None = None
    nova_senha: str | None = None


@app.post("/admin/usuarios")
def criar_usuario(dados: CriarUsuarioRequest, _admin: Usuario = Depends(exigir_admin)):
    with get_session() as session:
        if session.query(Usuario).filter_by(email=dados.email).first():
            raise HTTPException(400, "Já existe um usuário com esse e-mail.")

        usuario = Usuario(
            nome=dados.nome,
            email=dados.email,
            senha_hash=gerar_hash_senha(dados.senha),
            papel=dados.papel,
        )
        session.add(usuario)
        session.commit()
        session.refresh(usuario)
        return _usuario_para_json(usuario)


@app.get("/admin/usuarios")
def listar_usuarios(_admin: Usuario = Depends(exigir_admin)):
    with get_session() as session:
        usuarios = session.query(Usuario).order_by(Usuario.criado_em).all()
        return [_usuario_para_json(u) for u in usuarios]


@app.patch("/admin/usuarios/{usuario_id}")
def atualizar_usuario(usuario_id: str, dados: AtualizarUsuarioRequest, _admin: Usuario = Depends(exigir_admin)):
    with get_session() as session:
        usuario = session.get(Usuario, usuario_id)
        if not usuario:
            raise HTTPException(404, "Usuário não encontrado.")

        if dados.ativo is not None:
            usuario.ativo = dados.ativo
        if dados.papel is not None:
            usuario.papel = dados.papel
        if dados.nova_senha:
            usuario.senha_hash = gerar_hash_senha(dados.nova_senha)

        session.commit()
        session.refresh(usuario)
        return _usuario_para_json(usuario)


@app.delete("/admin/usuarios/{usuario_id}")
def excluir_usuario(usuario_id: str, admin: Usuario = Depends(exigir_admin)):
    if usuario_id == admin.id:
        raise HTTPException(400, "Você não pode excluir a própria conta.")

    with get_session() as session:
        usuario = session.get(Usuario, usuario_id)
        if not usuario:
            raise HTTPException(404, "Usuário não encontrado.")

        total_pedidos = (
            session.query(PedidoCertidao).filter_by(usuario_id=usuario_id).count()
            + session.query(LotePlanilha).filter_by(usuario_id=usuario_id).count()
        )
        if total_pedidos > 0:
            raise HTTPException(
                400,
                "Esse usuário já tem pedidos no histórico — exclua não é permitido "
                "pra não perder o registro de quem pediu o quê. Use \"Desativar\" em vez disso.",
            )

        session.delete(usuario)
        session.commit()
    return {"ok": True}


@app.get("/admin/atividade")
def consultar_atividade(_admin: Usuario = Depends(exigir_admin)):
    """Pedidos agrupados por usuário — junto com `GET /admin/usuarios`
    (que já mostra `ultimo_acesso_em`), dá a visão completa de quem
    acessou e o que cada colaborador pediu."""
    with get_session() as session:
        usuarios = session.query(Usuario).order_by(Usuario.criado_em).all()
        resultado = []
        for usuario in usuarios:
            pedidos = (
                session.query(PedidoCertidao)
                .filter_by(usuario_id=usuario.id)
                .order_by(PedidoCertidao.criado_em.desc())
                .limit(50)
                .all()
            )
            resultado.append({
                "usuario": _usuario_para_json(usuario),
                "total_pedidos": len(pedidos),
                "pedidos_recentes": [
                    {
                        "id": p.id,
                        "portal": p.portal,
                        "nome": p.nome,
                        "status": p.status,
                        "criado_em": _marcar_utc(p.criado_em),
                    }
                    for p in pedidos
                ],
            })
        return resultado


# ---------- portais e DLQ ----------

@app.get("/portais")
def listar_portais(_usuario: Usuario = Depends(obter_usuario_atual)):
    return PORTAIS_DISPONIVEIS


@app.get("/dlq/status")
def consultar_status_dlq(_usuario: Usuario = Depends(obter_usuario_atual)):
    """Quantas mensagens estão presas na fila `<portal>.dlq` de cada
    portal — pedidos que falharam `MAX_TENTATIVAS` vezes seguidas e
    precisam de inspeção manual. Consulta a API de management do
    RabbitMQ (ver `app/dlq.py`)."""
    return status_dlq_todos_portais(list(PORTAIS_DISPONIVEIS.keys()))


# ---------- pedidos ----------

@app.post("/pedidos")
def criar_pedido_unitario(
    portais: list[str] = Form(...),
    nome: str = Form(...),
    tipo: str = Form(None),
    documento: str = Form(...),
    data_nascimento: str = Form(""),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    _validar_portais_mesmo_grupo(portais)

    ids_criados = []
    with get_session() as session:
        for portal in portais:
            pedido = PedidoCertidao(
                portal=portal,
                nome=nome,
                tipo=tipo,
                documento=documento,
                data_nascimento=data_nascimento,
                usuario_id=usuario.id,
                status=StatusPedido.PENDENTE,
            )
            session.add(pedido)
            session.commit()
            session.refresh(pedido)
            ids_criados.append((portal, pedido.id))

    # Publica todos na fila só depois de garantir que gravou tudo no banco
    for portal, pedido_id in ids_criados:
        publicar_pedido(portal, pedido_id)

    return {"pedido_ids": [pid for _, pid in ids_criados], "total": len(ids_criados)}


@app.post("/pedidos/planilha")
async def criar_pedidos_planilha(
    portais: list[str] = Form(...),
    planilha: UploadFile = File(...),
    usuario: Usuario = Depends(obter_usuario_atual),
):
    _validar_portais_mesmo_grupo(portais)

    conteudo = await planilha.read()
    registros, erros = ler_planilha_certidoes(conteudo)

    with get_session() as session:
        lote = LotePlanilha(
            nome_arquivo_original=planilha.filename,
            total_linhas=str(len(registros) * len(portais)),
            usuario_id=usuario.id,
        )
        session.add(lote)
        session.commit()
        session.refresh(lote)
        # Guarda o valor puro antes do loop: cada commit() dentro dele expira
        # os atributos de TODOS os objetos da sessão (não só o pedido recém
        # commitado), inclusive `lote` — acessar `lote.id` depois que a
        # sessão fechar (fora do `with`) dispararia DetachedInstanceError.
        lote_id_valor = lote.id

        # Cada linha da planilha vira um pedido POR portal marcado — uma
        # planilha com 10 linhas e 3 portais gera 30 pedidos, todos no
        # mesmo lote (pra aparecer junto no relatório/acompanhamento).
        ids_publicados = []
        for registro in registros:
            for portal in portais:
                pedido = PedidoCertidao(
                    portal=portal,
                    nome=registro["nome"],
                    tipo=registro.get("tipo"),
                    documento=registro["documento"],
                    data_nascimento=registro.get("data_nascimento", ""),
                    lote_id=lote_id_valor,
                    linha_planilha=str(registro["linha"]),
                    usuario_id=usuario.id,
                    status=StatusPedido.PENDENTE,
                )
                session.add(pedido)
                session.commit()
                session.refresh(pedido)
                ids_publicados.append((portal, pedido.id))

    # Publica todos na fila só depois de garantir que gravou tudo no banco
    for portal, pedido_id in ids_publicados:
        publicar_pedido(portal, pedido_id)

    return {
        "lote_id": lote_id_valor,
        "total_validos": len(registros),
        "total_erros_validacao": len(erros),
        "total_pedidos_criados": len(ids_publicados),
        "erros": erros,
    }


@app.get("/pedidos/{pedido_id}")
def consultar_pedido(pedido_id: str, _usuario: Usuario = Depends(obter_usuario_atual)):
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


def _baixar_arquivo_pedido(pedido_id: str, campo: str, rotulo: str) -> FileResponse:
    with get_session() as session:
        pedido = session.get(PedidoCertidao, pedido_id)
        if not pedido:
            raise HTTPException(404, "Pedido não encontrado.")
        caminho_gravado = getattr(pedido, campo)
        arquivo = _resolver_arquivo_worker(pedido.portal, caminho_gravado)
        if not arquivo:
            raise HTTPException(404, f"Arquivo de {rotulo} não encontrado no servidor.")
        return FileResponse(arquivo, filename=arquivo.name)


@app.get("/pedidos/{pedido_id}/certidao")
def baixar_certidao(pedido_id: str, _usuario: Usuario = Depends(obter_usuario_atual)):
    return _baixar_arquivo_pedido(pedido_id, "caminho_certidao", "certidão")


@app.get("/pedidos/{pedido_id}/evidencia")
def baixar_evidencia(pedido_id: str, _usuario: Usuario = Depends(obter_usuario_atual)):
    return _baixar_arquivo_pedido(pedido_id, "url_evidencia", "evidência")


@app.get("/lotes/{lote_id}")
def consultar_lote(lote_id: str, _usuario: Usuario = Depends(obter_usuario_atual)):
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
def baixar_relatorio_lote(lote_id: str, _usuario: Usuario = Depends(obter_usuario_atual)):
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


# ── Frontend estático ─────────────────────────────────────────────────────────
# Serve index.html e admin.html diretamente pelo gateway, para que o front e a
# API fiquem sempre na mesma origem — elimina a necessidade de configurar uma
# URL separada do gateway e resolve o erro "not valid JSON" causado pelo
# localStorage guardando uma URL de ngrok antiga.
_FRONT_DIR = Path(__file__).resolve().parent.parent / "front"


@app.get("/")
def _serve_index():
    return FileResponse(_FRONT_DIR / "index.html")


@app.get("/admin")
def _serve_admin():
    return FileResponse(_FRONT_DIR / "admin.html")
