"""
Worker do portal Certidão Negativa de Feitos — Ministério Público do
Trabalho (MPT-PR). Reaproveita AutomacaoNodriverBase.

Antes bloqueado por WAF genérico (mesmo padrão do MPF originalmente,
depois reconfigurado pra um 403 Forbidden simples) rodando do ambiente
de datacenter/cloud — retestado em 15/07/2026 a partir da rede real do
escritório com o mesmo `--user-agent` corrigido usado no FGTS/MPF, e
abriu limpo (ver `docs/CATALOGO_PORTAIS.md`).

Mecânica confirmada por inspeção ao vivo (nodriver):

- Site clássico (Joomla, sem SPA) — formulário `extratoCertidaonegForm`,
  com **onkeypress** de máscara nos campos (`MascaraCNPJ`/`MascaraCPF`)
  — por isso preenchemos via `Element.send_keys()` (teclas de verdade
  via CDP), não `.value = X`, senão a máscara não é acionada.
- Radio `#codin_criterio_certidao` (CNPJ ou CPF) + campo de texto
  correspondente (`#cnpj` ou `#cpf`).
- Captcha: parece um reCAPTCHA v2 clássico (checkbox "Não sou um
  robô"), sitekey fixo no HTML (`6Lcr4rUUAAAAALkZBmt-0hVS4rsMbwiZLtJ51--R`)
  — **mas não é**. ⚠️ **Bug real encontrado no primeiro teste real**:
  resolvendo como v2 clássico (`resolver_recaptcha_v2`) e injetando o
  token na textarea, o formulário nunca submeteu de verdade — a
  evidência mostrou a MESMA página do formulário original, e o próprio
  widget exibia "Este site está excedendo a cota gratuita do reCAPTCHA
  **Enterprise**". Ou seja, é reCAPTCHA Enterprise em modo checkbox
  (não invisível), não v2 clássico — corrigido usando
  `resolver_recaptcha_enterprise(sitekey, url, invisible=False)`.
  Se a mensagem de cota excedida aparecer de novo mesmo com o tipo
  certo, é um problema de configuração do PRÓPRIO site (limite do
  Google Cloud do MPT), não do nosso código — nesse caso nem um
  visitante humano real conseguiria usar o captcha.
- Botão de envio: existem **dois elementos com o mesmo id**
  `codin_consultar` no HTML (bug do próprio site — um pra "Consultar",
  outro pra "Validar certidão"); selecionamos pelo atributo
  `title="Consultar"` pra pegar o certo, sem depender do id duplicado.
- O clique aciona `ValidarTamanhoCampo()`, que troca o `action` do
  formulário e chama `form.submit()` — é uma navegação de página de
  verdade (POST), não uma chamada AJAX, terminando numa página de
  "impressão" (`&print=1` na URL) — não é download nativo, então o
  resultado é capturado via `salvar_pagina_como_pdf` (a própria página
  renderizada, igual ao worker do CPF/Situação Cadastral).

`_interpretar_resultado` primeiro confere se a URL realmente navegou
pra fora da página inicial (`task=certidaoneg` na URL) antes de tentar
interpretar qualquer texto — sem isso, o texto "Certidão Negativa de
Feitos" (que é só o TÍTULO da página, aparece sempre, resultado ou não)
gerava falso positivo de sucesso mesmo quando o envio não tinha
acontecido de verdade (foi exatamente o que aconteceu no teste com o
tipo de captcha errado).
"""
import asyncio
import json
import re

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase

SITEKEY_RECAPTCHA = "6Lcr4rUUAAAAALkZBmt-0hVS4rsMbwiZLtJ51--R"
UA_CHROME_REAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class MptCertidaoNegativa(AutomacaoNodriverBase):
    portal = "mpt_certidao_negativa"
    url_inicial = "https://prt9.mpt.mp.br/servicos/certidao-positiva-negativa"
    espera_inicial_segundos = 4
    browser_args_extra = [f"--user-agent={UA_CHROME_REAL}"]

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        await self._selecionar_criterio(page, pedido.tipo)
        digitos = re.sub(r"\D", "", pedido.documento or "")
        seletor_campo = "#cpf" if (pedido.tipo or "pf").lower() == "pf" else "#cnpj"
        campo = await page.select(seletor_campo)
        await campo.send_keys(digitos)
        await page.wait(1)

        resolvedor = obter_resolvedor()
        token = await resolvedor.resolver_recaptcha_enterprise(SITEKEY_RECAPTCHA, self.url_inicial, invisible=False)
        await self._injetar_token_captcha(page, token)
        await page.wait(1)

        await self._clicar_consultar(page)
        await self._aguardar_navegacao(page)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _selecionar_criterio(self, page, tipo: str):
        valor = "CPF" if (tipo or "pf").lower() == "pf" else "CNPJ"
        await page.evaluate(f"""
            (() => {{
                const radio = document.querySelector('input[name="codin_criterio_certidao"][value="{valor}"]');
                if (radio) {{
                    radio.checked = true;
                    radio.onclick && radio.onclick();
                }}
            }})()
        """)

    async def _injetar_token_captcha(self, page, token: str):
        token_js = json.dumps(token)
        await page.evaluate(f"""
            (() => {{
                const campo = document.querySelector('#g-recaptcha-response');
                if (!campo) return;
                campo.value = {token_js};
                campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
                campo.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})()
        """)

    async def _clicar_consultar(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.querySelector('input[title="Consultar"]');
                if (botao) botao.onclick();
            })()
        """)

    async def _aguardar_navegacao(self, page, tentativas: int = 20):
        # ⚠️ Bug real encontrado testando pelo painel: o clique em
        # Consultar aciona um `form.submit()` de verdade (POST + reload
        # completo da página, não AJAX) — num teste real, 6s fixos não
        # foram suficientes pra essa navegação terminar (site do governo,
        # relativamente lento), e a checagem seguinte lia a URL ainda
        # antiga, concluindo (errado) que o formulário "não navegou".
        # Corrigido esperando de verdade a URL mudar pra incluir
        # "task=certidaoneg", em vez de um `wait` fixo curto.
        for _ in range(tentativas):
            await page.wait(1)
            if "task=certidaoneg" in page.url:
                return
        await page.wait(2)

    async def _interpretar_resultado(self, page) -> dict:
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        # "Certidão Negativa de Feitos" é o TÍTULO da página — aparece
        # sempre, resultado ou não. Sem confirmar que a URL realmente
        # navegou pra fora do formulário inicial, esse texto sozinho é
        # falso positivo (confirmado num teste real com o tipo de
        # captcha errado, onde o envio nunca aconteceu de verdade).
        if "task=certidaoneg" not in page.url:
            if "cota gratuita" in texto_lower and "recaptcha" in texto_lower:
                return {
                    "status": "erro_tecnico",
                    "mensagem": "reCAPTCHA Enterprise do próprio MPT excedeu a cota gratuita — problema do site, não do nosso código.",
                }
            return {"status": "erro_tecnico", "mensagem": "Formulário não navegou pra fora da página inicial após o envio."}

        if "nada consta" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão negativa de feitos gerada."}
        if "consta" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão de feitos gerada (com registro)."}
        if "informe o cnpj ou o cpf" in texto_lower or ("documento" in texto_lower and "inválid" in texto_lower):
            return {"status": "erro_portal", "mensagem": "Documento rejeitado pelo portal."}
        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao == "certidao_emitida":
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        if status_emissao == "erro_tecnico":
            return StatusPedido.ERRO_TECNICO
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = MptCertidaoNegativa()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
