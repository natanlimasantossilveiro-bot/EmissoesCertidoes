"""
Worker do portal Certidão de Cadastro de Imóvel — Prefeitura de Curitiba
(Declaração Unificada de Cadastro de Imóvel). Reaproveita AutomacaoNodriverBase.

Esse portal estava marcado no catálogo original como "link suspeito" (a URL
da planilha tinha um token de sessão embutido, provavelmente expirado) — na
prática, o domínio funciona normalmente, só precisa ser aberto direto na
raiz (`https://declaracaounificadaimovel.curitiba.pr.gov.br/`) em vez do
link antigo, que gera uma sessão nova a cada visita.

Mecânica confirmada por inspeção ao vivo (via `curl` e via Chromium real,
sem gastar nenhum captcha):

- Página inicial tem 2 botões (ASP.NET WebForms clássico, com postback real
  — não é SPA): "Emitir" (`btnEmissaoCadastroImob`) e "Validar"
  (`btnValidarCadastroImon`). Clicar em "Emitir" navega pra
  `Emissao.UNICA.aspx?<token>` — usamos um clique real em vez de montar a
  URL, porque o token muda a cada carga de página.
- Formulário de identificação do imóvel aceita **qualquer um** dos
  seguintes (conforme o próprio label da tela): Inscrição Imobiliária +
  Sublote (`txtNumInscricaoImobiliaria` + `txtSublote`) OU Indicação Fiscal
  completa (`txtNumIndicacaoFiscal`). Usamos só a Indicação Fiscal — é um
  identificador único e mais simples de validar que o par
  inscrição+sublote. `pedido.documento` carrega esse valor (formato
  confirmado contra uma declaração real: "23.018.024.000-0").
- Captcha: **imagem simples própria do sistema** (mesmo padrão do worker do
  TST-CNDT) — `<input type="image" id="imgValidar" src="data:image/png;
  base64,...">`, resposta no campo `txtImgValidacao` (4 caracteres,
  maiúsculo). Usa `resolver_captcha_imagem`.
- Confirmado contra o site real (captcha errado de propósito, sem gastar
  nada): mensagem de erro é "O Código de Validação não corresponde ao
  Código indicado na imagem de validação. Informe novamente!" — o
  formulário volta limpo (title/labels intactos, campos vazios).
- Botão de consulta: `btnConsultar`.

✅ Validado contra o site real (com `TWOCAPTCHA_API_KEY` real, Indicação
Fiscal real vinda de uma declaração já emitida manualmente pelo usuário):
o sistema achou o imóvel certo (mesma Inscrição Imobiliária/Sublote da
declaração de referência). Confirmado também: o resolvedor de captcha de
imagem erra às vezes (primeira tentativa real veio com o código errado,
"O Código de Validação não corresponde...") — o retry automático da fila
(`x-tentativa`) resolveu sozinho na 2ª tentativa, sem intervenção manual.

Igual ao TRF4/JFPR: a consulta bem-sucedida NÃO entrega o PDF direto —
mostra uma tela de CONFIRMAÇÃO ("Confira os dados do Imóvel antes de
imprimir") com os botões "Imprimir Declaração", "Nova Consulta" e
"Voltar". Clicar em "Imprimir Declaração" abre uma ABA NOVA apontando
direto pro arquivo final (ex: `.../Guias/GTM.DECLARACAO.UNICA.<hash>.pdf`).

⚠️ Detalhe importante (só percebido depois de comparar o PDF capturado com
o conteúdo esperado): o Chromium **não baixa PDFs automaticamente** — abre
no visualizador embutido. Chamar `salvar_pagina_como_pdf()` nesse estado
captura a INTERFACE do visualizador (toolbar, indicador de zoom/página),
não o conteúdo real do documento. Por isso, quando a aba nova aponta pra um
`.pdf`, usamos `page.download_file(page.url)` (API do nodriver) pra buscar
os bytes de verdade, e só then caímos pro fluxo normal de
`aguardar_e_mover_pdf`.

✅ **Validado de ponta a ponta** contra o site real (com `TWOCAPTCHA_API_KEY`
real): PDF final baixado corresponde à Declaração Unificada de Cadastro de
Imóvel completa (mesma Inscrição Imobiliária/Sublote/endereço/bairro da
declaração de referência fornecida pelo usuário — inclusive histórico de
logradouros e CIB idênticos). Dois detalhes de mecânica só descobertos
gastando captcha real (documentados aqui pra não repetir a investigação):

1. O tempo entre clicar "Imprimir Declaração" e a aba nova aparecer é
   **variável** (funcionou com ~4s numa tentativa, precisou de mais de 4s
   noutra) — por isso sondamos por até 10s em vez de usar espera fixa.
2. `page.download_file()` do nodriver usa um diretório próprio
   (`/app/downloads`) que **não é** o `BROWSER_DOWNLOAD_DIR` configurado —
   é preciso chamar `page.set_download_path(config.BROWSER_DOWNLOAD_DIR)`
   antes, senão o arquivo baixado nunca é encontrado pelo resto do fluxo
   (`aguardar_e_mover_pdf`) e cai pro fallback errado (print da INTERFACE
   do visualizador de PDF do Chromium, não do conteúdo real).

Nota adicional: o resolvedor de captcha de imagem desse portal tem uma
taxa de erro perceptível (falhou em 2 de 6 tentativas reais) — o retry
automático da fila (`x-tentativa`) cobre isso sem intervenção manual.
"""
import asyncio
import json
import re

from certidoes_core.config import config
from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class CuritibaCertidaoCadastroImovel(AutomacaoNodriverBase):
    portal = "curitiba_certidao_cadastro_imovel"
    url_inicial = "https://declaracaounificadaimovel.curitiba.pr.gov.br/"

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._clicar_emitir_inicial(page)
        await page.wait(3)

        imagem_base64 = await self._obter_imagem_captcha(page)
        resolvedor = obter_resolvedor()
        resposta_captcha = await resolvedor.resolver_captcha_imagem(imagem_base64)

        await self._preencher_e_submeter(page, pedido.documento, resposta_captcha)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # Tela de confirmação, não o documento — precisa clicar em
            # "Imprimir Declaração" antes de tentar capturar o PDF. Confirmado
            # contra o site real: o clique abre uma ABA NOVA apontando direto
            # pro arquivo PDF final (ex: .../Guias/GTM.DECLARACAO.UNICA.<hash>.pdf).
            info_clique = await self._clicar_imprimir_declaracao(page)
            print(f"[{self.portal}] Botão 'Imprimir Declaração': {info_clique}")

            # A aba nova demora um tempo variável pra aparecer (confirmado
            # contra o site real: às vezes 4s bastam, às vezes não) — em vez
            # de uma espera fixa, sondamos por até 10s.
            for _ in range(10):
                await page.wait(1)
                if len(page.browser.tabs) > 1:
                    break

            abas = page.browser.tabs
            if len(abas) > 1:
                page = abas[-1]
                await page.wait(2)
                print(f"[{self.portal}] Nova aba detectada: {page.url}")

            if page.url.lower().split("?")[0].endswith(".pdf"):
                # Confirmado contra o site real: Chromium abre PDFs no
                # visualizador embutido em vez de baixar sozinho — chamar
                # salvar_pagina_como_pdf aqui capturaria a INTERFACE do
                # visualizador (toolbar, zoom, contador de página), não o
                # conteúdo real. `download_file` busca os bytes de verdade.
                # Sem isso, download_file() usa um diretório próprio
                # (/app/downloads) diferente do que aguardar_e_mover_pdf
                # verifica (BROWSER_DOWNLOAD_DIR) — confirmado contra o site
                # real (o aviso do nodriver "no download path set" apareceu
                # e o arquivo baixado nunca foi encontrado pelo resto do
                # fluxo).
                await page.set_download_path(config.BROWSER_DOWNLOAD_DIR)
                await page.download_file(page.url)
                await page.wait(2)

            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=8)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _clicar_emitir_inicial(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.getElementById('btnEmissaoCadastroImob');
                if (botao) botao.click();
            })()
        """)

    async def _obter_imagem_captcha(self, page) -> str:
        src = await page.evaluate("""
            (() => {
                const img = document.getElementById('imgValidar');
                return img ? img.src : '';
            })()
        """)
        if "base64," in src:
            return src.split("base64,", 1)[1]
        return src

    async def _preencher_e_submeter(self, page, indicacao_fiscal: str, resposta_captcha: str):
        indicacao_js = json.dumps(indicacao_fiscal)
        resposta_js = json.dumps(resposta_captcha)
        await page.evaluate(f"""
            (() => {{
                document.getElementById('txtNumIndicacaoFiscal').value = {indicacao_js};
                document.getElementById('txtImgValidacao').value = {resposta_js};
                document.getElementById('btnConsultar').click();
            }})()
        """)

    async def _clicar_imprimir_declaracao(self, page) -> dict:
        info_json = await page.evaluate("""
            (() => {
                const alvo = 'imprimir declara';
                const candidatos = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a, div, span'));
                const elemento = candidatos.find(el => {
                    const texto = (el.innerText || el.value || '').trim().toLowerCase();
                    return texto.includes(alvo);
                });
                if (!elemento) return JSON.stringify({encontrado: false});
                const info = {
                    encontrado: true,
                    tag: elemento.tagName,
                    onclick: elemento.getAttribute('onclick') || '',
                    href: elemento.getAttribute('href') || '',
                };
                elemento.click();
                return JSON.stringify(info);
            })()
        """)
        return json.loads(info_json)

    async def _interpretar_resultado(self, page) -> dict:
        texto_bruto = await page.evaluate("document.body.innerText")
        # page.evaluate() pode devolver um objeto de erro do CDP
        # (ExceptionDetails) em vez de string, se rodar no meio de uma
        # navegação — confirmado em outro worker desse projeto (mesmo
        # padrão), onde isso derrubava o worker com AttributeError.
        texto = texto_bruto.strip() if isinstance(texto_bruto, str) else ""
        # innerText preserva quebras de linha do layout renderizado — uma
        # frase que quebra visualmente no meio vira um '\n' bem onde o
        # padrão esperava um espaço, fazendo o "in" simples falhar mesmo
        # com o texto certo na tela (confirmado no worker de Débitos, que
        # usa o mesmo padrão de comparação). Normalizamos espaços em
        # branco só pra fins de comparação.
        texto_lower = re.sub(r"\s+", " ", texto.lower())

        # Confirmado contra o site real (captcha errado de propósito, sem
        # gastar nada): essa é a mensagem exata de captcha inválido.
        if "código de validação não corresponde" in texto_lower:
            return {"status": "erro_captcha", "mensagem": "Código de validação (captcha) não confere."}

        # Tela de confirmação pré-impressão (confirmado contra o site real) —
        # ainda não é o documento final, mas já é sucesso: o imóvel foi
        # encontrado e falta só clicar "Imprimir Declaração".
        if "confira os dados do imóvel antes de imprimir" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Imóvel encontrado, declaração pronta pra impressão."}

        if "declaração unificada de cadastro de imóvel" in texto_lower and "identificação do imóvel" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Declaração de cadastro de imóvel emitida."}

        # Formulário ainda presente, sem erro de captcha reconhecido — pode
        # ser Indicação Fiscal não encontrada ou outro erro do portal ainda
        # não catalogado. Não arriscamos adivinhar o texto: cai pra
        # sucesso_provável (evidência automática garante o print pro humano
        # conferir).
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
    automacao = CuritibaCertidaoCadastroImovel()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
