"""
Worker do portal CNPJ + QSA (Comprovante de Inscrição e Situação Cadastral —
Receita Federal). Reaproveita AutomacaoNodriverBase, igual aos outros dois
workers da Receita.

Mecânica confirmada por inspeção ao vivo (nodriver, sem gastar captcha):

- É uma SPA Angular (domínio `solucoes.receita.fazenda.gov.br`, diferente
  do domínio da Certidão Conjunta e do CPF, apesar de ser "mesma família"
  Receita Federal). Só tem UM campo: CNPJ (com máscara, sem name/id HTML —
  é um reactive form do Angular, por isso preenchemos via seletor CSS +
  eventos 'input'/'change'/'blur', não por name).
- Captcha: hCaptcha em modo "recaptchacompat=true" — a página cria DOIS
  campos de resposta (h-captcha-response e g-recaptcha-response), então
  preenchemos os dois pra cobrir qualquer verificação do backend. O
  sitekey não vem como atributo HTML (`data-sitekey` fica vazio) — só
  aparece na query string do `src` do iframe renderizado pelo hCaptcha,
  por isso extraímos de lá.
- O botão "CONSULTAR" fica `disabled` até o captcha ser resolvido de
  verdade. Testei primeiro só preenchendo a textarea de resposta — não
  bastou: o Angular ignorou (confirmado contra o site real, gastando
  captcha de verdade: deu "O Angular não reconheceu o captcha resolvido").
  O app só sabe que o captcha foi resolvido através do callback que ELE
  MESMO registrou ao chamar `hcaptcha.render({sitekey, callback})` — não
  observando a textarea. Por isso usamos `usar_hook_hcaptcha_callback`
  (ver `AutomacaoNodriverBase`) pra interceptar essa função assim que a
  página carrega, e chamamos ela diretamente com o token resolvido.
- Confirmei a mensagem de validação client-side quando falta captcha:
  "Por favor, complete a verificação do captcha." — útil pra distinguir
  "captcha não aceito" de outros erros.

⚠️ BLOQUEIO CONHECIDO, ainda não resolvido: mesmo com o callback funcionando
de verdade (o Angular passa a reconhecer o token e submete o formulário —
confirmado, gastando captcha real), o backend do portal rejeita o token
com "Erro ao validar captcha. Por favor, tente novamente." Testei a
hipótese de que o backend validasse contra o Google (resquício de
`recaptchacompat=true`) resolvendo como reCAPTCHA v2 em vez de hCaptcha —
descartada: o 2captcha recusou de cara com `ERROR_WRONG_GOOGLEKEY`,
confirmando que o sitekey é hCaptcha "puro", não registrado no Google.

Hipótese mais provável agora: **inconsistência de IP entre quem resolve o
captcha (servidor do 2captcha) e quem submete o formulário (este
container)** — alguns backends validam isso e um token resolvido de um IP
diferente do que submete é rejeitado. Resolver isso exigiria configurar
um proxy pro 2captcha resolver a partir do mesmo IP de saída do worker —
infraestrutura adicional, não só código. Por ora, esse portal fica
pausado nessa etapa; os textos de sucesso em `_interpretar_resultado`
continuam sendo heurística não confirmada.
"""
import asyncio
import json

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class CnpjQsa(AutomacaoNodriverBase):
    portal = "cnpj_qsa"
    url_inicial = "https://solucoes.receita.fazenda.gov.br/servicos/cnpjreva/cnpjreva_solicitacao.asp"
    espera_inicial_segundos = 5  # SPA Angular, leva mais tempo pra montar do que páginas estáticas
    usar_hook_hcaptcha_callback = True  # Angular só reconhece o captcha via callback, não via textarea

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        sitekey = await self._obter_sitekey(page)
        resolvedor = obter_resolvedor()
        token = await resolvedor.resolver_hcaptcha(sitekey, self.url_inicial)

        await self._preencher_cnpj(page, pedido.documento)
        await self._injetar_token_captcha(page, token)
        await self._acionar_callback_hcaptcha(page, token)
        await page.wait(1)
        await self._clicar_consultar(page)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # SPA — igual ao CPF, não está confirmado que o comprovante
            # baixa como PDF automaticamente. Tenta download nativo primeiro
            # (timeout curto) e cai pra gerar o PDF a partir da própria
            # página se nada baixar.
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=3)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _obter_sitekey(self, page) -> str:
        # data-sitekey não vem preenchido no <div class="h-captcha"> (Angular
        # renderiza via JS, não atributo estático) — o valor real só aparece
        # na query string do iframe que o hCaptcha injeta.
        return await page.evaluate("""
            (() => {
                const iframe = document.querySelector('iframe[src*="hcaptcha"]');
                if (!iframe) return '';
                const match = iframe.src.match(/[?&]sitekey=([^&]+)/);
                return match ? decodeURIComponent(match[1]) : '';
            })()
        """)

    async def _preencher_cnpj(self, page, cnpj: str):
        cnpj_js = json.dumps(cnpj)
        await page.evaluate(f"""
            (() => {{
                const input = document.querySelector('input[type="text"]');
                input.focus();
                input.value = {cnpj_js};
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                input.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            }})()
        """)

    async def _injetar_token_captcha(self, page, token: str):
        token_js = json.dumps(token)
        await page.evaluate(f"""
            (() => {{
                const campos = [
                    document.querySelector('textarea[name="h-captcha-response"]'),
                    document.querySelector('textarea[name="g-recaptcha-response"]'),
                ];
                for (const campo of campos) {{
                    if (!campo) continue;
                    campo.value = {token_js};
                    campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    campo.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }})()
        """)

    async def _acionar_callback_hcaptcha(self, page, token: str):
        """Chama diretamente a função que o Angular registrou em
        hcaptcha.render({callback}) — capturada pelo hook instalado em
        AutomacaoNodriverBase.executar() antes da página carregar. Isso faz
        o app reconhecer o captcha como resolvido de verdade, reabilitando
        o botão sozinho (sem precisar forçar disabled=false)."""
        token_js = json.dumps(token)
        await page.evaluate(f"""
            (() => {{
                if (typeof window.__hcaptchaCallback === 'function') {{
                    window.__hcaptchaCallback({token_js});
                }}
            }})()
        """)

    async def _clicar_consultar(self, page):
        # Fallback: se por algum motivo o callback não reabilitou o botão
        # (ex: hook não capturou a tempo), força disabled=false — não é
        # garantido que o clique forçado sozinho funcione nesse caso, já
        # que o app pode não ter registrado o token internamente mesmo assim.
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => b.innerText.includes('CONSULTAR'));
                if (!botao) return;
                if (botao.disabled) botao.removeAttribute('disabled');
                botao.click();
            })()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        texto = await page.evaluate("(() => document.body.innerText)()")
        # page.evaluate() pode devolver um objeto de erro do CDP
        # (ExceptionDetails) em vez de string, se rodar no meio de uma
        # navegação — confirmado em outro worker desse projeto (mesmo
        # padrão), onde isso derrubava o worker com AttributeError.
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        if "complete a verificação do captcha" in texto_lower:
            return {"status": "erro_captcha", "mensagem": "O Angular não reconheceu o captcha resolvido."}
        if any(frase in texto_lower for frase in ["não encontrado", "inválido", "não pôde ser processada", "não é possível"]):
            return {"status": "erro_portal", "mensagem": texto[:1000] or "A Receita Federal recusou a solicitação."}
        if "comprovante" in texto_lower and ("gerad" in texto_lower or "emitid" in texto_lower):
            return {"status": "certidao_emitida", "mensagem": "Comprovante de inscrição e situação cadastral gerado."}
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
    automacao = CnpjQsa()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))