"""
Worker do portal SEFAZ PR — Certidão de Débitos Tributários e de Dívida
Ativa Estadual. Reaproveita AutomacaoNodriverBase.

⚠️ **Portal exploratório, com risco conhecido de rejeição por pontuação de
comportamento** — mesma categoria de problema do worker de CNPJ+QSA
(Receita Federal), que segue bloqueado. Construído mesmo assim a pedido
do usuário, já que a landing page passou a carregar sem bloqueio de borda
(antes rejeitava a sessão antes mesmo de qualquer captcha ser resolvido).

Mecânica confirmada por inspeção ao vivo (via Chromium real, sem gastar
captcha):

- Formulário simples: SPA (Vue/Vuetify, ícones Material Design) com um
  único campo de texto (sem atributo `name` — selecionado via
  `input[type="text"]`, único da página) pra CPF ou CNPJ, e um botão
  "EMITIR CERTIDÃO".
- Captcha: **reCAPTCHA Enterprise invisível** (`size: "invisible"`,
  `badge: "bottomright"`), renderizado via `grecaptcha.enterprise.render()`
  em modo `explicit` — não tem atributo `data-sitekey` no HTML, a sitekey
  só aparece na URL do iframe âncora que o próprio script do Google cria
  (`https://www.google.com/recaptcha/enterprise/anchor?...&k=<sitekey>`).
- Igual ao CNPJ+QSA (hCaptcha): o app registra um `callback` na hora de
  chamar `.render({sitekey, callback})`, e só esse callback aciona o fluxo
  de submissão de verdade — por isso usa
  `usar_hook_recaptcha_enterprise_callback = True`
  (`HOOK_SCRIPT_RECAPTCHA_ENTERPRISE_CALLBACK` em
  `certidoes_core.automacao.nodriver_base`), que intercepta
  `grecaptcha.enterprise.render()` pra capturar esse callback antes de
  qualquer JS da página rodar.
- Resolve via `resolver_recaptcha_enterprise()` (novo método em
  `certidoes_core.captcha`, usa `enterprise=1` na API do 2captcha).

🔴 **Bloqueado num ponto ainda mais cedo do que o CNPJ+QSA**: testado com
captcha real (3 tentativas, todas via retry automático da fila) — o
próprio 2captcha devolveu `ERROR_CAPTCHA_UNSOLVABLE` nas 3, ou seja, não
chegamos nem a conseguir um token pra testar se o SEFAZ PR aceitaria (no
CNPJ+QSA pelo menos o 2captcha resolvia, e a rejeição vinha depois, do
backend do portal). Os parâmetros enviados batem com a documentação
oficial da lib (`googlekey`, `url`, `method=userrecaptcha`,
`version=v2`, `enterprise=1`, `invisible=1`) — não parece erro de
configuração nossa, e sim o serviço de resolução não conseguindo lidar
com esse reCAPTCHA Enterprise específico. Não vale a pena insistir sem
mudar de provedor de captcha (ex: testar outro serviço especializado em
Enterprise) — cada tentativa não resolvida ainda assim consome tempo/cota,
mesmo sem cobrar.
"""
import asyncio
import json
import re

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class SefazPrCertidaoDebitos(AutomacaoNodriverBase):
    portal = "sefaz_pr_certidao_debitos"
    url_inicial = "https://cdwfazenda.paas.pr.gov.br/cdwportal/certidao/automatica"
    espera_inicial_segundos = 5
    usar_hook_recaptcha_enterprise_callback = True

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._preencher_documento(page, pedido.documento)

        sitekey = await self._obter_sitekey(page)
        resolvedor = obter_resolvedor()
        token = await resolvedor.resolver_recaptcha_enterprise(sitekey, self.url_inicial, invisible=True)

        info_callback = await self._acionar_callback(page, token)
        print(f"[{self.portal}] Callback do reCAPTCHA Enterprise: {info_callback}")
        await page.wait(3)

        await self._clicar_emitir(page)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=8)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _preencher_documento(self, page, documento: str):
        documento_js = json.dumps(documento)
        await page.evaluate(f"""
            (() => {{
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                const campo = document.querySelector('input[type="text"]');
                if (campo) {{
                    setter.call(campo, {documento_js});
                    campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    campo.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }})()
        """)
        await page.wait(1)

    async def _obter_sitekey(self, page) -> str:
        # Não existe atributo data-sitekey no HTML (render=explicit) — a
        # sitekey só aparece na URL do iframe âncora que o script do
        # Google cria em tempo de execução.
        src = await page.evaluate("""
            (() => {
                const iframe = document.querySelector('iframe[src*="recaptcha"]');
                return iframe ? iframe.src : '';
            })()
        """)
        match = re.search(r"[?&]k=([^&]+)", src or "")
        return match.group(1) if match else ""

    async def _acionar_callback(self, page, token: str) -> dict:
        token_js = json.dumps(token)
        info_json = await page.evaluate(f"""
            (() => {{
                if (typeof window.__recaptchaEnterpriseCallback === 'function') {{
                    window.__recaptchaEnterpriseCallback({token_js});
                    return JSON.stringify({{encontrado: true}});
                }}
                return JSON.stringify({{encontrado: false}});
            }})()
        """)
        return json.loads(info_json)

    async def _clicar_emitir(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => b.innerText.includes('EMITIR'));
                if (botao) botao.click();
            })()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        texto_bruto = await page.evaluate("document.body.innerText")
        texto = texto_bruto.strip() if isinstance(texto_bruto, str) else ""
        texto_lower = re.sub(r"\s+", " ", texto.lower())

        if "certidão negativa" in texto_lower or "certidão positiva" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": texto[:1000]}

        if "recaptcha" in texto_lower and ("inválid" in texto_lower or "falhou" in texto_lower or "expirad" in texto_lower):
            return {"status": "erro_captcha", "mensagem": texto[:500]}

        if "cpf ou cnpj inválido" in texto_lower or "documento inválido" in texto_lower:
            return {"status": "erro_portal", "mensagem": texto[:500]}

        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao == "certidao_emitida":
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_captcha":
            return StatusPedido.ERRO_TECNICO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = SefazPrCertidaoDebitos()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
