"""
Generaliza o tirar_print_tela() que já existia no projeto da Certidão
Conjunta. Todo worker (independente do portal) chama isso do mesmo jeito
em caso de erro — ou de sucesso, se quiser manter print de comprovação.
"""
import tempfile
from pathlib import Path

from certidoes_core.storage import salvar_bytes, gerar_nome_evidencia


async def capturar_evidencia(page, nome_pessoa: str, documento: str, portal: str, motivo: str = "falha") -> str:
    """`page` é o objeto de página do nodriver. Retorna a URL/caminho salvo,
    ou string vazia se a captura falhar (nunca deve derrubar o worker).

    full_page=True porque vários portais (ex: Receita Federal) têm um
    cabeçalho grande que ocupa a viewport inteira — sem isso, o print só
    mostra o topo da página, sem a mensagem de resultado."""
    try:
        with tempfile.TemporaryDirectory() as pasta_tmp:
            caminho_tmp = Path(pasta_tmp) / "evidencia.png"
            await page.save_screenshot(filename=str(caminho_tmp), format="png", full_page=True)
            screenshot_bytes = caminho_tmp.read_bytes()
    except Exception as erro:
        print(f"[evidencia] Falha ao capturar screenshot: {erro}")
        return ""

    nome_arquivo = gerar_nome_evidencia(nome_pessoa, documento, portal, motivo)
    try:
        return salvar_bytes(nome_arquivo, screenshot_bytes)
    except Exception as erro:
        print(f"[evidencia] Falha ao salvar evidência: {erro}")
        return ""
