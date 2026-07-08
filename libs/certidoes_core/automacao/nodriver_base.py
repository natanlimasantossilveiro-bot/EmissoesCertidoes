"""
Camada de plataforma pra portais automatizados via nodriver (Chromium).
Cuida do ciclo de vida do navegador e garante, pra QUALQUER portal que
herdar daqui, a regra: sempre que o resultado não for sucesso confirmado,
captura evidência (print) antes de fechar o navegador — mesmo que seja
erro de regra de negócio do próprio portal (ex: CNPJ que não é matriz),
não só erro técnico. Isso não depende de cada worker novo lembrar de
chamar capturar_evidencia() — a base já garante.
"""
import asyncio
import base64
import shutil
from abc import abstractmethod
from pathlib import Path

import nodriver as nd

from certidoes_core.config import config
from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.evidencia import capturar_evidencia
from certidoes_core.automacao.base import AutomacaoPortal, ResultadoEmissao

PASTA_CERTIDOES_EMITIDAS = Path("/data/certidoes_emitidas")

# Alguns apps SPA (Angular/React) só sabem que o hCaptcha foi resolvido
# através do callback que ELES MESMOS registraram na hora de chamar
# hcaptcha.render({sitekey, callback}) — só preencher a textarea de
# resposta não é suficiente (confirmado contra o site real do CNPJ+QSA).
# Esse script roda ANTES de qualquer JS da página (via
# Page.addScriptToEvaluateOnNewDocument) e intercepta a atribuição de
# window.hcaptcha pra capturar essa função assim que o app chamar
# .render(), guardando em window.__hcaptchaCallback. Depois de resolver
# via 2captcha, o worker chama window.__hcaptchaCallback(token) — isso
# aciona o mesmo caminho que o widget real acionaria.
HOOK_SCRIPT_HCAPTCHA_CALLBACK = """
(function() {
    let _real;
    try {
        Object.defineProperty(window, "hcaptcha", {
            configurable: true,
            get() { return _real; },
            set(value) {
                _real = value;
                try {
                    const originalRender = value.render.bind(value);
                    value.render = function(container, params) {
                        window.__hcaptchaCallback = params.callback;
                        return originalRender(container, params);
                    };
                } catch (e) {}
            }
        });
    } catch (e) {}
})();
"""


class AutomacaoNodriverBase(AutomacaoPortal):
    url_inicial: str
    browser_args_extra: list = []
    espera_inicial_segundos: int = 3  # portais mais pesados (ex: SPA Angular) podem sobrescrever
    usar_hook_hcaptcha_callback: bool = False  # ver HOOK_SCRIPT_HCAPTCHA_CALLBACK acima

    @abstractmethod
    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        """Implementado por cada portal concreto: navega, preenche
        formulário, interpreta o resultado. Não precisa se preocupar com
        evidência nem com abrir/fechar navegador — a base cuida disso."""

    async def executar(self, pedido: PedidoCertidao) -> ResultadoEmissao:
        browser = await nd.start(
            headless=config.BROWSER_HEADLESS,
            browser_args=[
                f"--download-directory={config.BROWSER_DOWNLOAD_DIR}",
                # container roda como root; Chromium recusa iniciar sem isso
                "--no-sandbox",
                "--disable-dev-shm-usage",
                *self.browser_args_extra,
            ],
        )
        try:
            page = await browser.get("about:blank")
            if self.usar_hook_hcaptcha_callback:
                # Page.enable é obrigatório aqui — sem isso,
                # addScriptToEvaluateOnNewDocument "sucede" (retorna um id)
                # mas não tem efeito nenhum na prática (confirmado testando).
                await page.send(nd.cdp.page.enable())
                await page.send(nd.cdp.page.add_script_to_evaluate_on_new_document(
                    source=HOOK_SCRIPT_HCAPTCHA_CALLBACK
                ))

            await page.get(self.url_inicial)
            await page.wait(self.espera_inicial_segundos)

            resultado = await self.preencher_e_emitir(page, pedido)

            if resultado.status != StatusPedido.SUCESSO_CONFIRMADO and not resultado.url_evidencia:
                resultado.url_evidencia = await capturar_evidencia(
                    page, pedido.nome, pedido.documento, self.portal, motivo=resultado.status.value
                )

            return resultado
        finally:
            try:
                browser.stop()
            except Exception as erro:
                print(f"[{self.portal}] Aviso ao fechar navegador: {erro}")

    # ---------- helpers de download, comuns a qualquer portal via nodriver ----------

    def _listar_pdfs_downloads(self) -> set:
        return set(config.BROWSER_DOWNLOAD_DIR.glob("*.pdf"))

    async def aguardar_e_mover_pdf(self, pedido: PedidoCertidao, pdfs_antes: set, tentativas: int = 30) -> str:
        """Espera o navegador terminar de baixar um PDF novo e move pro
        destino final já com o nome padronizado (nome + portal + documento).
        Retorna "" se nenhum PDF novo aparecer dentro do prazo."""
        PASTA_CERTIDOES_EMITIDAS.mkdir(parents=True, exist_ok=True)
        destino = PASTA_CERTIDOES_EMITIDAS / self.nome_arquivo_certidao(pedido)

        for _ in range(tentativas):
            novos = self._listar_pdfs_downloads() - pdfs_antes
            if novos:
                arquivo_recente = max(novos, key=lambda p: p.stat().st_ctime)
                shutil.move(str(arquivo_recente), str(destino))
                return str(destino)
            await asyncio.sleep(1)
        return ""

    async def salvar_pagina_como_pdf(self, page, pedido: PedidoCertidao) -> str:
        """Alguns portais (ex: CPF) não disparam um download de PDF de
        verdade — só renderizam o comprovante como página HTML. Nesses
        casos, geramos o PDF a partir da própria página renderizada (via
        CDP Page.printToPDF) em vez de depender de um arquivo baixado."""
        PASTA_CERTIDOES_EMITIDAS.mkdir(parents=True, exist_ok=True)
        destino = PASTA_CERTIDOES_EMITIDAS / self.nome_arquivo_certidao(pedido)
        try:
            dados_b64, _ = await page.send(nd.cdp.page.print_to_pdf(print_background=True))
            destino.write_bytes(base64.b64decode(dados_b64))
            return str(destino)
        except Exception as erro:
            print(f"[{self.portal}] Falha ao gerar PDF da página: {erro}")
            return ""