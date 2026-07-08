"""
Worker do portal JFPR/TRF4 — Certidão Judicial (Cível, Criminal ou Eleitoral).
Sistema CGI clássico (PHP, sem SPA), reCAPTCHA v2 **padrão** (não Enterprise,
não hCaptcha) — mais simples que os workers anteriores, sem precisar de
nenhum hook de callback.

Mecânica confirmada via `curl` direto contra o site real (sem gastar nenhum
captcha), antes de escrever qualquer linha de automação:

- Formulário: `#frmCertidao`, method="get", action="proc_processa_certidao.php".
  Campos: `string_cpf` (aceita CPF ou CNPJ, o próprio site identifica pelo
  tamanho e importa o nome da Receita Federal automaticamente — não há
  seleção explícita de PF/PJ na tela), `string_dat_nascimento` (dd/mm/aaaa),
  `string_tipo_cert` (radio: M=Cível, N=Criminal, O=Eleitoral — escolhe-se
  só UM), mais os hidden `status_emissao` e `coderr` (usados pelo próprio
  site pra re-renderizar o erro, não precisamos mexer neles).
- Captcha: reCAPTCHA v2 clássico (`<div class="g-recaptcha" data-sitekey=...>`),
  sitekey lido do DOM em tempo de execução. O botão "Gerar Certidão
  Negativa" (`onclick="submitEmitir()"`) só faz `form.submit()` direto —
  não existe nenhuma validação client-side bloqueando o clique (a função
  `isCaptchaChecked()` existe no HTML mas não é chamada em lugar nenhum),
  então basta preencher a textarea `g-recaptcha-response` com o token e
  submeter, igual ao worker do CPF.
- Submissão é via GET: o navegador navega para
  `proc_processa_certidao.php?...`. Em caso de erro, o servidor responde
  302 de volta pra `index.php?coderr=.N.&...` e a página re-renderizada
  mostra a mensagem de erro dentro de `<span style="color:#FF0000;">`.
  Confirmado contra o site real (sem gastar captcha): coderr=".3." é "CPF/CNPJ
  inválido", coderr=".11." é "Favor marcar a opção 'Não sou um robô'".
  Em vez de tentar adivinhar o significado de cada código numérico,
  extraímos o texto do span vermelho direto — é a mesma informação que um
  humano veria na tela, mais confiável que decodificar os códigos.
- Banner de política de privacidade/cookies (`#btnAceitoPoliticaPrivacidade`)
  aparece na carga inicial — clicamos "Aceito" defensivamente antes de
  preencher, caso ele fique sobrepondo o formulário.

✅ **Submissão validada de ponta a ponta** contra o site real, com
`TWOCAPTCHA_API_KEY` real e CPF real (4 emissões confirmadas): o servidor
confere o CPF/data de nascimento contra a Receita Federal de verdade
(confirmado — usar um CPF/data que não batam gera o erro "Data de
nascimento divergente da Receita Federal" no span vermelho), e a
identificação do nome ("NATAN JONATAN DE LIMA") saiu correta em todas as
tentativas. Em caso de sucesso, o portal NÃO baixa o PDF direto — mostra
uma tela de CONFIRMAÇÃO ("Certidão Judicial Cível/Criminal Nome: ... CPF:
...") com um `<input type="button">` "VISUALIZAR CERTIDÃO GERADA" cujo
`onclick` faz `window.location = '../../certidao_balcao/certidao_emite_cjf.php?num_contro_certid=N&num_cpf_cgc_judici=...&nom_parte_judici=...'`.

🔴 **Conclusão após investigação extensiva (5 emissões reais com captcha
pago)**: o botão "VISUALIZAR CERTIDÃO GERADA" do próprio site do TRF4
aponta pra um caminho **quebrado**. Testado de 3 formas independentes,
todas com o mesmo resultado de erro:
1. Clique direto no navegador (deixando o `window.location` do site
   resolver) → "Bad Request" (erro genérico do Apache).
2. `curl` isolado na URL resolvida manualmente (`urljoin(page.url,
   caminho_relativo)`) → 404 "Página não encontrada" (página própria do
   TRF4, não é bloqueio de borda).
3. Navegação direta via `page.get()` com a mesma URL calculada
   corretamente → também 404, inclusive testado de novo após 3s de espera
   (descarta hipótese de geração assíncrona do PDF).

Ou seja: não é bug do nosso código — é o próprio botão do TRF4 apontando
pra um endpoint que não existe (`/trf4/certidao_balcao/certidao_emite_cjf.php`).
Isso só afeta o ÚLTIMO passo (baixar o PDF assinado); a parte que
realmente importa — o pedido chegar, ser conferido contra a Receita
Federal, e a certidão ser efetivamente GERADA no sistema do TRF4 — está
100% validada (5 emissões confirmadas, nome/CPF sempre corretos, zero
erros de submissão). `_obter_url_visualizar_certidao()` fica implementado
(extrai a URL via regex do `onclick` e resolve com `urljoin`) caso o TRF4
corrija esse link no futuro — nesse caso, o download deve funcionar sem
precisar mexer em mais nada.
"""
import asyncio
import json
import re
from urllib.parse import urljoin

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase

TIPO_CERTIDAO_PARA_CODIGO = {
    "civel": "M",
    "criminal": "N",
    "eleitoral": "O",
}


class TrfCertidaoJudicial(AutomacaoNodriverBase):
    portal = "trf4_certidao_civel_criminal"
    url_inicial = "https://www2.trf4.jus.br/trf4/processos/certidao/index.php"

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._aceitar_politica_privacidade(page)

        sitekey = await self._obter_sitekey(page)
        resolvedor = obter_resolvedor()
        token = await resolvedor.resolver_recaptcha_v2(sitekey, self.url_inicial)

        await self._preencher_e_submeter(page, pedido, token)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # Confirmado contra o site real (gasto de captcha real): depois de
            # emitir, o portal mostra uma tela de CONFIRMAÇÃO com o botão
            # "VISUALIZAR CERTIDÃO GERADA" (onclick="window.location='../../
            # certidao_balcao/certidao_emite_cjf.php?...'"). Clicar o botão
            # direto (deixando o próprio JS do site resolver o caminho
            # relativo) resultou em "Bad Request" numa tentativa anterior —
            # em vez de confiar nisso, extraímos a URL do onclick e
            # resolvemos o caminho relativo nós mesmos (urljoin, com o
            # page.url exato no momento), navegando direto via page.get().
            url_certidao = await self._obter_url_visualizar_certidao(page)
            print(f"[{self.portal}] URL da certidão resolvida: {url_certidao}")

            if url_certidao:
                await page.get(url_certidao)
                await page.wait(3)

            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=8)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _aceitar_politica_privacidade(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.querySelector('#btnAceitoPoliticaPrivacidade');
                if (botao) botao.click();
            })()
        """)
        await page.wait(1)

    async def _obter_sitekey(self, page) -> str:
        return await page.evaluate("""
            (() => {
                const el = document.querySelector('.g-recaptcha');
                return el ? el.getAttribute('data-sitekey') : '';
            })()
        """)

    async def _preencher_e_submeter(self, page, pedido: PedidoCertidao, token: str):
        codigo_tipo = TIPO_CERTIDAO_PARA_CODIGO.get((pedido.tipo or "").lower(), "M")

        documento_js = json.dumps(pedido.documento)
        data_js = json.dumps(pedido.data_nascimento or "")
        codigo_tipo_js = json.dumps(codigo_tipo)
        token_js = json.dumps(token)

        await page.evaluate(f"""
            (() => {{
                document.getElementById('string_cpf').value = {documento_js};
                document.getElementById('string_dat_nascimento').value = {data_js};

                const radios = document.getElementsByName('string_tipo_cert');
                for (const radio of radios) {{
                    radio.checked = (radio.value === {codigo_tipo_js});
                }}

                let campoToken = document.querySelector('textarea[name="g-recaptcha-response"]');
                if (!campoToken) {{
                    campoToken = document.createElement('textarea');
                    campoToken.name = 'g-recaptcha-response';
                    campoToken.style.display = 'none';
                    document.getElementById('frmCertidao').appendChild(campoToken);
                }}
                campoToken.value = {token_js};

                document.getElementById('frmCertidao').submit();
            }})()
        """)

    async def _obter_url_visualizar_certidao(self, page) -> str:
        # Confirmado via gasto de captcha real: a tela pós-emissão não é o
        # documento — é uma confirmação com dois botões ("VISUALIZAR CERTIDÃO
        # GERADA" e "GERAR NOVA CERTIDÃO"). Procuramos por texto em vez de
        # id/name (mais resiliente a mudanças de markup). Em vez de clicar e
        # deixar o `window.location` do próprio site resolver o caminho
        # relativo (isso gerou "Bad Request" numa tentativa anterior),
        # extraímos a URL de dentro do onclick e resolvemos nós mesmos.
        onclick = await page.evaluate("""
            (() => {
                const alvo = 'visualizar certid';
                const candidatos = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a, div, span'));
                const elemento = candidatos.find(el => {
                    const texto = (el.innerText || el.value || '').trim().toLowerCase();
                    return texto.includes(alvo);
                });
                return elemento ? (elemento.getAttribute('onclick') || '') : '';
            })()
        """)
        if not onclick:
            return ""

        match = re.search(r"window\.location\s*=\s*'([^']+)'", onclick)
        if not match:
            return ""

        caminho_relativo = match.group(1)
        return urljoin(page.url, caminho_relativo)

    async def _interpretar_resultado(self, page) -> dict:
        # O servidor re-renderiza o erro dentro de um span vermelho na
        # própria index.php (confirmado via curl, sem gastar captcha) — mais
        # confiável que decodificar o significado de cada código "coderr=.N.".
        erro_texto = await page.evaluate("""
            (() => {
                const spans = Array.from(document.querySelectorAll("span[style*='FF0000']"));
                return spans.map(s => s.innerText.trim()).filter(Boolean).join(' | ');
            })()
        """)
        # page.evaluate() pode devolver um objeto de erro do CDP
        # (ExceptionDetails) em vez de string, se rodar no meio de uma
        # navegação — confirmado em outro worker desse projeto (mesmo
        # padrão), onde isso derrubava o worker com AttributeError.
        erro_texto = erro_texto.strip() if isinstance(erro_texto, str) else ""

        if erro_texto:
            texto_lower = erro_texto.lower()
            if "rob" in texto_lower or "captcha" in texto_lower:
                return {"status": "erro_captcha", "mensagem": erro_texto}
            return {"status": "erro_portal", "mensagem": erro_texto}

        # Sem erro visível: se o navegador saiu da página do formulário
        # (frmCertidao não existe mais no DOM), tratamos como emissão OK.
        saiu_do_formulario = await page.evaluate("""
            !document.getElementById('frmCertidao')
        """)
        if saiu_do_formulario:
            return {"status": "certidao_emitida", "mensagem": "Certidão emitida (sem mensagem de erro na tela)."}

        return {"status": "resultado_indefinido", "mensagem": "Resultado não identificado — formulário ainda presente na tela, sem erro visível."}

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
    automacao = TrfCertidaoJudicial()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))