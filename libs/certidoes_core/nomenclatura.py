"""
Convenção única de nome de arquivo — usada tanto pra certidão final quanto
pra evidência (screenshot), em qualquer portal. Garante que dá pra abrir a
pasta de certidões emitidas (ou de evidências) e já saber de quem é e de
qual portal, sem precisar abrir arquivo por arquivo.
"""
import re
import unicodedata


def normalizar_nome_arquivo(texto: str) -> str:
    """Remove acentos e qualquer caractere que não seja letra/número,
    deixando um bloco em maiúsculas separado por '_'."""
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^A-Za-z0-9]+", "_", texto).strip("_")
    return texto.upper() or "SEM_NOME"


# Convenção de pasta/nome que o escritório já usa manualmente (planilha
# de levantamento feita pela colaboradora que mais usa o sistema) — troca
# o slug técnico do portal (ex: "receita_federal") por um rótulo
# reconhecível (ex: "Tributo Federal") no nome do arquivo final, pra quem
# já está acostumado com esse padrão não estranhar na transição.
NOME_PASTA_POR_PORTAL = {
    "receita_federal": "Tributo Federal",
    "cpf_situacao_cadastral": "CPF",
    "tst_cndt": "Débitos Trabalhistas",
    "curitiba_certidao_cadastro_imovel": "Certidão de Cadastro",
    "curitiba_consulta_debitos_divida_ativa": "Consulta de Débitos",
    "sefaz_pr_certidao_debitos": "Tributo Estadual",
    "atendenet_pinhais_cnd": "Tributo Municipal Pinhais",
    "mpf_certidao_negativa": "Certidão MPF",
    "fgts_caixa": "Certidão FGTS",
    "curitiba_guia_amarela": "Guia Amarela",
}

# O TRF4 tem um rótulo diferente por tipo de certidão (mesmo portal) —
# a colaboradora distingue "Certidão Criminal JFPR" de "Certidão Cível
# JFPR" na pasta, então usamos o campo "tipo" do pedido pra escolher.
NOME_PASTA_TRF4_POR_TIPO = {
    "civel": "Certidão Cível JFPR",
    "criminal": "Certidão Criminal JFPR",
    "eleitoral": "Certidão Eleitoral JFPR",
}


def _rotulo_portal(portal: str, tipo: str | None) -> str:
    if portal == "trf4_certidao_civel_criminal" and tipo in NOME_PASTA_TRF4_POR_TIPO:
        return NOME_PASTA_TRF4_POR_TIPO[tipo]
    return NOME_PASTA_POR_PORTAL.get(portal, portal)


def gerar_nome_certidao(nome_pessoa: str, portal: str, documento: str, extensao: str = "pdf", tipo: str | None = None) -> str:
    rotulo = normalizar_nome_arquivo(_rotulo_portal(portal, tipo))
    # CNPJ formatado com barra (ex: "37.187.679/0001-80") quebra o caminho
    # do arquivo — a barra vira separador de pasta, e o worker tenta salvar
    # num diretório que não existe em vez de um arquivo só (confirmado num
    # erro real: "No such file or directory" no TRF4 com CNPJ). Pontos e
    # traço continuam intactos (são válidos em nome de arquivo e já são o
    # padrão usado neste projeto) — só a barra precisa de substituição.
    documento_seguro = (documento or "").replace("/", "-")
    return f"{normalizar_nome_arquivo(nome_pessoa)}_{rotulo}_{documento_seguro}.{extensao}"