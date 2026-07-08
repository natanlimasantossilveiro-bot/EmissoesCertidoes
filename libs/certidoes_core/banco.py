"""
Tabela central de pedidos de certidão. Todo worker escreve aqui o status,
e o Gateway/Front consulta daqui — ninguém precisa perguntar diretamente
a um worker "como está indo".
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, Enum, Text, Boolean, Integer, ForeignKey, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from certidoes_core.config import config

Base = declarative_base()


class StatusPedido(str, enum.Enum):
    PENDENTE = "pendente"
    PROCESSANDO = "processando"
    SUCESSO_CONFIRMADO = "sucesso_confirmado"
    SUCESSO_PROVAVEL = "sucesso_provavel"
    ERRO_PORTAL = "erro_portal"          # portal recusou/erro de negócio (ex: Receita)
    ERRO_TECNICO = "erro_tecnico"        # captcha, timeout, portal fora do ar
    FALHA_INDEFINIDA = "falha_indefinida"


class PapelUsuario(str, enum.Enum):
    ADMIN = "admin"
    COLABORADOR = "colaborador"


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    nome = Column(String(128), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    senha_hash = Column(String(255), nullable=False)
    papel = Column(Enum(PapelUsuario), default=PapelUsuario.COLABORADOR, nullable=False)

    # Desativar em vez de apagar — preserva o histórico de pedidos do
    # colaborador mesmo depois que ele sai do escritório.
    ativo = Column(Boolean, default=True, nullable=False)

    criado_em = Column(DateTime, default=datetime.utcnow)
    # None = nunca fez login — é exatamente o "quem não acessou" que o
    # painel de admin precisa mostrar, sem precisar de uma tabela de
    # histórico de login separada.
    ultimo_acesso_em = Column(DateTime, nullable=True)


class PedidoCertidao(Base):
    __tablename__ = "pedidos_certidao"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Identifica qual worker/portal deve processar. Ex: "receita_federal",
    # "atendenet_pinhais", "tjsp". É o nome da fila também.
    portal = Column(String(64), nullable=False, index=True)

    # Nome completo (PF) ou razão social (PJ) — usado pra nomear o arquivo
    # final da certidão e da evidência, pra dar pra identificar de quem é
    # sem precisar abrir cada PDF/print um por um.
    nome = Column(String(255), nullable=False)

    tipo = Column(String(16), nullable=True)              # pf/pj, ou tipo de certidão
    documento = Column(String(32), nullable=False, index=True)
    data_nascimento = Column(String(16), nullable=True)

    # Rastreabilidade: se veio de planilha, guarda o lote e a linha original
    lote_id = Column(String(36), nullable=True, index=True)
    linha_planilha = Column(String(8), nullable=True)

    status = Column(Enum(StatusPedido), default=StatusPedido.PENDENTE, nullable=False, index=True)
    mensagem = Column(Text, nullable=True)

    tentativas = Column(Integer, default=0)

    caminho_certidao = Column(String(512), nullable=True)   # PDF final, se houver
    url_evidencia = Column(String(512), nullable=True)      # screenshot de erro/sucesso

    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Quem criou o pedido — vem do login, não é mais digitado à mão no
    # front. Também é a base do painel de "atividade por colaborador".
    usuario_id = Column(String(36), ForeignKey("usuarios.id"), nullable=True, index=True)
    usuario = relationship("Usuario")


class LotePlanilha(Base):
    """Agrupa N pedidos gerados a partir de uma mesma planilha, pra permitir
    consultar 'como está o lote X' e gerar o relatório consolidado no final."""
    __tablename__ = "lotes_planilha"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    nome_arquivo_original = Column(String(255), nullable=True)
    total_linhas = Column(String(8), default="0")
    usuario_id = Column(String(36), ForeignKey("usuarios.id"), nullable=True, index=True)
    usuario = relationship("Usuario")
    criado_em = Column(DateTime, default=datetime.utcnow)


_engine = None
_SessionLocal = None


def _obter_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_session():
    """Uso: with get_session() as session: ..."""
    _obter_engine()
    return _SessionLocal()


def criar_tabelas():
    engine = _obter_engine()
    Base.metadata.create_all(engine)
