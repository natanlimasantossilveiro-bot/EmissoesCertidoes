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


def gerar_nome_certidao(nome_pessoa: str, portal: str, documento: str, extensao: str = "pdf") -> str:
    return f"{normalizar_nome_arquivo(nome_pessoa)}_{portal}_{documento}.{extensao}"