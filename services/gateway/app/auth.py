"""
Autenticação por usuário (substitui a chave única compartilhada
`GATEWAY_API_KEY`): login com e-mail/senha, token JWT enviado pelo front
no header `Authorization: Bearer <token>` em toda chamada — mesmo padrão
de header que já era usado, só troca o mecanismo por trás.
"""
import os
import threading
import time
from collections import defaultdict, deque
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


SENHA_TAMANHO_MINIMO = 8
# bcrypt só considera os primeiros 72 bytes — acima disso o resto da senha
# seria ignorado em silêncio.
SENHA_TAMANHO_MAXIMO_BYTES = 72


def validar_forca_senha(senha: str) -> None:
    if len(senha or "") < SENHA_TAMANHO_MINIMO:
        raise HTTPException(400, f"A senha deve ter pelo menos {SENHA_TAMANHO_MINIMO} caracteres.")
    if len(senha.encode()) > SENHA_TAMANHO_MAXIMO_BYTES:
        raise HTTPException(400, "Senha longa demais (máximo 72 bytes).")


def gerar_hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def verificar_senha(senha: str, senha_hash: str) -> bool:
    return bcrypt.checkpw(senha.encode(), senha_hash.encode())


# Hash de uma senha qualquer, só pra gastar o mesmo tempo de bcrypt quando
# o e-mail não existe — sem isso, a resposta mais rápida entrega quais
# e-mails têm conta.
_HASH_FICTICIO = gerar_hash_senha("senha-ficticia-para-tempo-constante")

# Limite de tentativas de login erradas, em memória (o Gateway roda num
# processo uvicorn só). 5 erros em 15 min bloqueiam aquela conta até a
# janela passar. Por IP o limite é bem mais alto de propósito: o
# escritório inteiro sai pelo mesmo IP público, e um colaborador errando
# a própria senha não pode trancar todos os outros — o limite por IP só
# pega quem testa senhas contra muitas contas diferentes. Sem nada disso,
# dava pra testar senhas à vontade contra qualquer conta.
LOGIN_MAX_FALHAS_POR_EMAIL = 5
LOGIN_MAX_FALHAS_POR_IP = 30
LOGIN_JANELA_SEGUNDOS = 15 * 60
_falhas_login: dict[str, deque] = defaultdict(deque)
_trava_falhas_login = threading.Lock()


def _chaves_limite(email: str, ip: str) -> list[str]:
    chaves = [f"email:{(email or '').lower()}"]
    if ip:
        chaves.append(f"ip:{ip}")
    return chaves


def _bloqueado_por_excesso(chaves: list[str]) -> bool:
    agora = time.monotonic()
    with _trava_falhas_login:
        for chave in chaves:
            fila = _falhas_login[chave]
            while fila and agora - fila[0] > LOGIN_JANELA_SEGUNDOS:
                fila.popleft()
            limite = LOGIN_MAX_FALHAS_POR_EMAIL if chave.startswith("email:") else LOGIN_MAX_FALHAS_POR_IP
            if len(fila) >= limite:
                return True
    return False


def _registrar_falha(chaves: list[str]) -> None:
    agora = time.monotonic()
    with _trava_falhas_login:
        for chave in chaves:
            _falhas_login[chave].append(agora)


def _limpar_falhas(email: str) -> None:
    with _trava_falhas_login:
        _falhas_login.pop(f"email:{(email or '').lower()}", None)


def criar_token(usuario: Usuario) -> str:
    _checar_jwt_configurado()
    payload = {
        "sub": usuario.id,
        "papel": usuario.papel.value,
        "exp": datetime.utcnow() + timedelta(hours=JWT_HORAS_EXPIRACAO),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def autenticar(email: str, senha: str, ip: str = "") -> Usuario:
    """Confere e-mail/senha e atualiza `ultimo_acesso_em`. Levanta
    HTTPException(401) se as credenciais não baterem ou o usuário estiver
    desativado — mesma mensagem genérica pros dois casos, pra não revelar
    se o e-mail existe ou não. 429 depois de LOGIN_MAX_FALHAS erros."""
    chaves = _chaves_limite(email, ip)
    if _bloqueado_por_excesso(chaves):
        raise HTTPException(429, "Muitas tentativas de login erradas — aguarde 15 minutos e tente novamente.")

    with get_session() as session:
        usuario = session.query(Usuario).filter_by(email=email).first()
        senha_ok = verificar_senha(senha, usuario.senha_hash if usuario else _HASH_FICTICIO)
        if not usuario or not usuario.ativo or not senha_ok:
            _registrar_falha(chaves)
            raise HTTPException(401, "E-mail ou senha inválidos, ou usuário desativado.")

        _limpar_falhas(email)
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
