"""
Gateway: única porta de entrada. Responsabilidade única é validar,
gravar no banco com status PENDENTE, e publicar na fila do portal certo.
Nunca abre navegador nem espera o resultado — por isso responde rápido
mesmo com fila cheia.
"""
from pydantic import BaseModel, EmailStr
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response, Depends
from fastapi.middleware.cors import CORSMiddleware

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
    # atendenet_pinhais entra aqui quando o worker existir de fato — listar
    # sem worker deixa o pedido "pendente" pra sempre, sem ninguém consumindo
    # a fila, o que parece bug numa demonstração
    # próximos portais entram aqui conforme forem sendo automatizados
}


@app.on_event("startup")
def startup():
    criar_tabelas()
    bootstrap_admin_inicial()


# ---------- autenticação ----------

class LoginRequest(BaseModel):
    email: EmailStr
    senha: str


def _usuario_para_json(usuario: Usuario) -> dict:
    return {
        "id": usuario.id,
        "nome": usuario.nome,
        "email": usuario.email,
        "papel": usuario.papel,
        "ativo": usuario.ativo,
        "ultimo_acesso_em": usuario.ultimo_acesso_em,
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
                        "criado_em": p.criado_em,
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
    portal: str = Form(...),
    nome: str = Form(...),
    tipo: str = Form(None),
    documento: str = Form(...),
    data_nascimento: str = Form(""),
    usuario: Usuario = Depends(obter_usuario_atual),
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
            usuario_id=usuario.id,
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
    usuario: Usuario = Depends(obter_usuario_atual),
):
    if portal not in PORTAIS_DISPONIVEIS:
        raise HTTPException(400, f"Portal '{portal}' não habilitado.")

    conteudo = await planilha.read()
    registros, erros = ler_planilha_certidoes(conteudo)

    with get_session() as session:
        lote = LotePlanilha(
            nome_arquivo_original=planilha.filename,
            total_linhas=str(len(registros)),
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
                usuario_id=usuario.id,
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
