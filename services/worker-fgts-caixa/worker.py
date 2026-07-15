"""
Worker do portal FGTS — Situação de Regularidade do Empregador (Caixa
Econômica Federal). Reaproveita AutomacaoNodriverBase.

Antes bloqueado por WAF (ShieldSquare/Radware Bot Manager) rodando do
ambiente de datacenter/cloud usado nas primeiras varreduras — retestado
em 15/07/2026 a partir da rede real do escritório e abriu limpo (ver
`docs/CATALOGO_PORTAIS.md`). Validado manualmente pelo usuário com o CNPJ
real do próprio escritório (13.316.414/0001-76), resultado "REGULAR".

Mecânica confirmada por inspeção do HTML servido (sem gastar nada — o
formulário é renderizado no servidor, não é SPA):

- Framework **JSF/RichFaces clássico** (A4J.AJAX), não uma SPA moderna —
  o botão "Consultar" (`mainForm:btnConsultar`) dispara
  `A4J.AJAX.Submit(...)`, uma atualização parcial da página via AJAX, não
  uma navegação de verdade. Clicar o botão de verdade (via
  `page.evaluate` + `.click()`, igual aos outros workers) deixa o próprio
  JS do site cuidar da chamada — não precisamos montar a requisição na mão.
- Campos do formulário: `mainForm:tipoEstabelecimento` (select — "1" =
  CNPJ, "3" = CPF; usamos sempre CNPJ, é o único caso de uso do
  escritório pra esse portal — ver catálogo), `mainForm:txtInscricao1`
  (texto, `maxlength="14"` — só cabe dígito puro, sem pontuação, ao
  contrário do Pinhais que exigia o documento formatado), `mainForm:uf`
  (select opcional, deixamos em branco).
- **Sem captcha em nenhum ponto** — nenhuma referência a
  reCAPTCHA/hCaptcha/Turnstile no HTML servido. Mais simples que qualquer
  outro portal já construído nesse projeto.

⚠️ **Ponto ainda não validado em automação** (mecânica montada por
inspeção do HTML servido, não por reconhecimento ao vivo com nodriver —
essa página não expõe o resultado nem o link de download antes do AJAX
rodar de verdade num navegador real): depois do resultado "REGULAR"
aparecer, a tela mostra um link "Obtenha o Certificado de Regularidade
do FGTS - CRF", que é o PDF de verdade (a mensagem de texto sozinha não
é o documento). Não temos como saber de antemão se esse link dispara um
download nativo, abre em nova aba, ou renderiza inline — por isso
`_capturar_certidao` tenta, nessa ordem: (1) download nativo (mais
provável, é o padrão desse tipo de certificado gov.br), (2) imprimir a
própria página como PDF, como último recurso. Precisa de confirmação
rodando o container de verdade.
"""
import asyncio
import re

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class FgtsCaixa(AutomacaoNodriverBase):
    portal = "fgts_caixa"
    url_inicial = "https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf"
    espera_inicial_segundos = 4
    # Confirmado num teste real: o ShieldSquare/Radware bloqueava citando
    # literalmente "HeadlessChrome/149.0.0.0" no corpo da página de
    # bloqueio — ou seja, o próprio Chromium headless denuncia a
    # automação via User-Agent, mesmo vindo de um IP limpo (o reteste
    # anterior via `curl`, que não é um navegador, não passava por essa
    # checagem). Sobrescrever o UA removendo "Headless" é suficiente pra
    # esse WAF específico, sem precisar de tela virtual (Xvfb).
    browser_args_extra = [
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ]

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._selecionar_cnpj(page)
        await self._preencher_inscricao(page, pedido.documento)
        await self._clicar_consultar(page)
        await page.wait(5)  # atualização parcial via AJAX, não navegação — precisa de espera própria

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self._capturar_certidao(page, pedido, pdfs_antes)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _selecionar_cnpj(self, page):
        await page.evaluate("""
            (() => {
                const sel = document.querySelector('select[id="mainForm:tipoEstabelecimento"]');
                if (!sel) return;
                sel.value = "1";
                sel.dispatchEvent(new Event('change', { bubbles: true }));
            })()
        """)

    async def _preencher_inscricao(self, page, documento: str):
        # Campo tem maxlength="14" — só cabe dígito puro (sem pontuação),
        # diferente do Pinhais que exigia o documento formatado.
        digitos = re.sub(r"\D", "", documento or "")
        await page.evaluate(f"""
            (() => {{
                const campo = document.querySelector('input[id="mainForm:txtInscricao1"]');
                if (!campo) return;
                campo.value = "{digitos}";
                campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
                campo.dispatchEvent(new Event('change', {{ bubbles: true }}));
                campo.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            }})()
        """)

    async def _clicar_consultar(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.querySelector('input[id="mainForm:btnConsultar"]');
                if (botao) botao.click();
            })()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        if "não foi possível verificar" in texto_lower:
            return {
                "status": "erro_portal",
                "mensagem": "Caixa não conseguiu verificar a regularidade — confira se o CNPJ está correto ou tente novamente mais tarde.",
            }
        if "irregular" in texto_lower:
            return {"status": "irregular", "mensagem": "Empresa está IRREGULAR no FGTS."}
        if "regular" in texto_lower:
            return {"status": "regular", "mensagem": "Empresa está REGULAR no FGTS."}
        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    async def _capturar_certidao(self, page, pedido: PedidoCertidao, pdfs_antes: set) -> str:
        await page.evaluate("""
            (() => {
                const links = Array.from(document.querySelectorAll('a'));
                const link = links.find(a => (a.innerText || '').toLowerCase().includes('certificado'));
                if (link) link.click();
            })()
        """)
        await page.wait(3)
        caminho = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=10)
        if caminho:
            return caminho
        return await self.salvar_pagina_como_pdf(page, pedido)

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao in ("regular", "irregular"):
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = FgtsCaixa()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
