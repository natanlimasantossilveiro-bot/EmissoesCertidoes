"""
Autenticação por usuário (substitui a chave única compartilhada
`GATEWAY_API_KEY`): login com e-mail/senha, token JWT enviado pelo front
no header `Authorization: Bearer <token>` em toda chamada — mesmo padrão
de header que já era usado, só troca o mecanismo por trás.
"""
import os
from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

from certidoes_core.banco import get_session, Usuario, PapelUsuario

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = "HS256"
JWT_HORAS_EXPIRACAO = 12

ADMIN_EMAIL_BOOTSTRAP = os.getenv("ADMIN_EMAIL", "")
ADMIN_SENHA_BOOTSTRAP = os.getenv("ADMIN_SENHA_INICIAL", "")


def _checar_jwt_configurado():
    if not JWT_SECRET_KEY:
        raise RuntimeError(
            "JWT_SECRET_KEY não configurada — gere uma com `openssl rand -hex 32` "
            "e coloque no .env antes de subir o Gateway."
        )


def gerar_hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def verificar_senha(senha: str, senha_hash: str) -> bool:
    return bcrypt.checkpw(senha.encode(), senha_hash.encode())


def criar_token(usuario: Usuario) -> str:
    _checar_jwt_configurado()
    payload = {
        "sub": usuario.id,
        "papel": usuario.papel.value,
        "exp": datetime.utcnow() + timedelta(hours=JWT_HORAS_EXPIRACAO),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def autenticar(email: str, senha: str) -> Usuario:
    """Confere e-mail/senha e atualiza `ultimo_acesso_em`. Levanta
    HTTPException(401) se as credenciais não baterem ou o usuário estiver
    desativado — mesma mensagem genérica pros dois casos, pra não revelar
    se o e-mail existe ou não."""
    with get_session() as session:
        usuario = session.query(Usuario).filter_by(email=email).first()
        if not usuario or not usuario.ativo or not verificar_senha(senha, usuario.senha_hash):
            raise HTTPException(401, "E-mail ou senha inválidos, ou usuário desativado.")

        usuario.ultimo_acesso_em = datetime.utcnow()
        session.commit()
        session.refresh(usuario)
        # Evita DetachedInstanceError ao acessar atributos depois que a
        # sessão fechar (mesmo cuidado já usado em outros pontos do
        # Gateway) — devolve um objeto solto da sessão com os valores já
        # carregados.
        session.expunge(usuario)
        return usuario


def obter_usuario_atual(authorization: str = Header(default="")) -> Usuario:
    _checar_jwt_configurado()
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token ausente (header Authorization: Bearer <token>).")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Sessão expirada, faça login novamente.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido.")

    with get_session() as session:
        usuario = session.get(Usuario, payload["sub"])
        if not usuario or not usuario.ativo:
            raise HTTPException(401, "Usuário não encontrado ou desativado.")
        session.expunge(usuario)
        return usuario


def exigir_admin(usuario: Usuario = Depends(obter_usuario_atual)) -> Usuario:
    if usuario.papel != PapelUsuario.ADMIN:
        raise HTTPException(403, "Ação restrita a administradores.")
    return usuario


def bootstrap_admin_inicial():
    """Roda no startup do Gateway: se ainda não existe nenhum usuário e as
    variáveis de ambiente do admin inicial estão configuradas, cria a
    primeira conta — sem isso, ninguém consegue logar pela primeira vez
    num banco novo."""
    if not ADMIN_EMAIL_BOOTSTRAP or not ADMIN_SENHA_BOOTSTRAP:
        return

    with get_session() as session:
        if session.query(Usuario).count() > 0:
            return

        admin = Usuario(
            nome="Administrador",
            email=ADMIN_EMAIL_BOOTSTRAP,
            senha_hash=gerar_hash_senha(ADMIN_SENHA_BOOTSTRAP),
            papel=PapelUsuario.ADMIN,
        )
        session.add(admin)
        session.commit()
        print(f"[auth] Admin inicial criado: {ADMIN_EMAIL_BOOTSTRAP}")
