"""
Worker do portal CPF — Situação Cadastral (Receita Federal). Página ASP
clássica (não é a mesma SPA da Certidão Conjunta), com captcha hCaptcha.
Reaproveita a mesma AutomacaoNodriverBase do worker da Certidão Conjunta —
mesmo ciclo de tentativa/retry, mesma regra de evidência automática, mesma
nomeação de arquivo.

Mecânica de submissão verificada diretamente contra o site real (não havia
projeto de referência pra copiar, como havia pra Certidão Conjunta):

- Campos do formulário: txtCPF, txtDataNascimento (aceita "dd/mm/aaaa" — o
  próprio JS do site remove os separadores antes de validar).
- Captcha: hCaptcha (rotulado como "Anti-Robô" na tela, mas tecnicamente é
  hCaptcha, não reCAPTCHA — confirmado pelo <script src="hcaptcha.com/...">
  e pelo atributo data-sitekey). O sitekey é lido do DOM em tempo de
  execução, não fixado no código, pra sobreviver a uma eventual rotação.
- O botão "Consultar" (onclick="return ValidarDados('Recaptcha')") checa
  um elemento com id "h-recaptcha-response" que NÃO existe no DOM real —
  o hCaptcha cria a textarea de resposta com name="h-captcha-response" e
  um id com sufixo aleatório. Isso parece um bug legado no próprio site
  (clicar o botão de verdade pode nunca validar de fato). Por isso
  submetemos o formulário diretamente via JS (form.submit()), preenchendo
  o campo pelo NOME — mais determinístico do que depender desse handler.
- Envio inválido (captcha errado, ou CPF+data não conferem) redireciona de
  volta pra ConsultaPublica.asp com "?Error=N" e mostra uma mensagem no
  corpo da página — foi assim que confirmei o texto exato do erro de
  captcha ("O Anti-Robô não foi preenchido corretamente...").

✅ Validado de ponta a ponta contra o site real (com TWOCAPTCHA_API_KEY
real): hCaptcha resolvido, comprovante emitido com "Situação Cadastral:
REGULAR", e PDF gerado corretamente. Esse portal não dispara download
nativo de PDF — só renderiza o comprovante como página HTML — por isso
`salvar_pagina_como_pdf()` (via CDP Page.printToPDF) é usado como
resultado principal, com `aguardar_e_mover_pdf()` como tentativa prévia
caso o comportamento do site mude no futuro.

Ainda não confirmados contra um caso real: os textos de CPF irregular/
suspenso/cancelado em `_interpretar_resultado` (só vi o caso "REGULAR" e
o de captcha inválido) — se aparecerem, provavelmente caem em
`resultado_indefinido` (sucesso_provável) e o humano confere pela
evidência, que é o comportamento seguro por padrão.
"""
import asyncio
import json

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class CpfSituacaoCadastral(AutomacaoNodriverBase):
    portal = "cpf_situacao_cadastral"
    url_inicial = "https://servicos.receita.fazenda.gov.br/servicos/cpf/consultasituacao/consultapublica.asp"

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        sitekey = await self._obter_sitekey(page)
        resolvedor = obter_resolvedor()
        token = await resolvedor.resolver_hcaptcha(sitekey, self.url_inicial)

        await self._preencher_e_submeter(page, pedido, token)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # Confirmado contra o site real: esse portal não dispara download
            # de PDF — só renderiza o comprovante como página HTML. Tentamos
            # o download nativo primeiro (timeout curto, caso mude no
            # futuro) e caímos pra gerar o PDF a partir da própria página.
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=3)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _obter_sitekey(self, page) -> str:
        return await page.evaluate("""
            (() => {
                const el = document.querySelector('.h-captcha');
                return el ? el.getAttribute('data-sitekey') : '';
            })()
        """)

    async def _preencher_e_submeter(self, page, pedido: PedidoCertidao, token: str):
        cpf_js = json.dumps(pedido.documento)
        data_js = json.dumps(pedido.data_nascimento or "")
        token_js = json.dumps(token)

        await page.evaluate(f"""
            (() => {{
                document.getElementsByName('txtCPF')[0].value = {cpf_js};
                document.getElementsByName('txtDataNascimento')[0].value = {data_js};

                let campoToken = document.querySelector('textarea[name="h-captcha-response"]');
                if (!campoToken) {{
                    campoToken = document.createElement('textarea');
                    campoToken.name = 'h-captcha-response';
                    campoToken.style.display = 'none';
                    document.forms['frmConsultaPublica'].appendChild(campoToken);
                }}
                campoToken.value = {token_js};

                const idChecked = document.getElementsByName('idCheckedReCaptcha')[0];
                if (idChecked) idChecked.value = "true";

                document.forms['frmConsultaPublica'].submit();
            }})()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        # document.body.innerText inteiro vem dominado pelo cabeçalho/menu do
        # site (centenas de caracteres de boilerplate) antes de chegar no
        # conteúdo específico da página — por isso focamos no container
        # principal (mesmo id em todas as páginas desse sistema) e só caímos
        # pro body inteiro se por algum motivo esse container não existir (a
        # página de resultado, ConsultaPublicaExibir.asp, não usa esse id —
        # cai no body mesmo mas mantém a mensagem específica no final).
        texto = await page.evaluate("""
            (() => {
                const container = document.querySelector('#rfb-main-container') || document.body;
                return container.innerText;
            })()
        """)
        # page.evaluate() pode devolver um objeto de erro do CDP
        # (ExceptionDetails) em vez de string, se rodar no meio de uma
        # navegação — confirmado em outro worker desse projeto (mesmo
        # padrão), onde isso derrubava o worker com AttributeError.
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        # Confirmado contra o site real: o texto do comprovante (nome, CPF,
        # "Situação Cadastral: REGULAR") é um sinal muito mais confiável que
        # a URL — checamos ele primeiro. A checagem de URL abaixo já deu
        # falso positivo num comprovante emitido com sucesso (a query string
        # continha "error=" por outro motivo, sem indicar falha real).
        if "situação cadastral" in texto_lower and ("regular" in texto_lower or "comprovante emitido" in texto_lower):
            return {"status": "certidao_emitida", "mensagem": texto[:1000] or "Comprovante de situação cadastral gerado."}
        if "anti-rob" in texto_lower and ("não foi preenchido" in texto_lower or "expirou" in texto_lower):
            return {"status": "erro_captcha", "mensagem": "Falha ao validar o Anti-Robô (hCaptcha)."}

        url_atual = (page.url or "").lower().rstrip("/")
        if "error=" in url_atual or url_atual.endswith("consultapublica.asp"):
            return {"status": "erro_portal", "mensagem": texto[:1000] or "A Receita Federal recusou a solicitação."}
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
    automacao = CpfSituacaoCadastral()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))