"""
Worker do portal Consulta de Débitos — Dívida Ativa (Prefeitura de
Curitiba). Reaproveita AutomacaoNodriverBase. Mesma plataforma/família do
worker `curitiba_certidao_cadastro_imovel` (mesmo widget de captcha de
imagem, mesmo estilo ASP.NET WebForms clássico) — construído logo depois
dele, reaproveitando a mesma mecânica já validada.

⚠️ Diferente do worker de Certidão de Cadastro: esse portal é uma
**consulta informativa de débitos em dívida ativa**, não claramente uma
certidão (o catálogo original já marcava isso). Construído mesmo assim a
pedido do usuário, pra reaproveitar o trabalho de reconhecimento já feito
nessa mesma plataforma. Se o escritório decidir que não serve, é só não
habilitar o portal no Gateway.

Mecânica confirmada por inspeção ao vivo (via `curl` e via Chromium real,
sem gastar nenhum captcha):

- Domínio igual ao do worker de Imóvel: também tinha link "suspeito" na
  planilha original (token de sessão ASP.NET clássico, tipo
  `/(S(xxxx))/...`) — mas basta abrir a raiz do domínio que uma sessão
  nova é gerada automaticamente (`Default.aspx` faz um 302 pra
  `/(S(<token>))/Default.aspx`). Não precisa reabrir o link manualmente.
- Da tela inicial ("Parcelamento, Quitação de Débitos ou Custas
  Judiciais"), o card "PARA CONSULTAR SEUS DÉBITOS INSCRITOS EM DÍVIDA
  ATIVA" navega (`location.href`) pra `frmConsultaDebitosContrib.aspx`,
  dentro da mesma sessão.
- Formulário: 5 radios (`name="info"`) pra escolher o tipo de
  identificação — usamos `optIndFiscal` (Indicação Fiscal IPTU),
  preenchido em `txtIndFiscal` (12 dígitos, sem pontuação — diferente do
  worker de Imóvel, que aceitava o valor formatado com pontos/traço).
- Captcha: imagem simples, mesmo padrão (`input[type=image]
  id="imgValidar"`, resposta em `txtImgValidacao`, 4 caracteres).
- Confirmado contra o site real (captcha errado de propósito, sem gastar
  nada): erro é "O Código de Validação informado não corresponde ao
  presente na imagem de validação." — texto ligeiramente diferente do
  worker de Imóvel, mas mesma ideia.
- Botão de consulta: `btnEntrar` ("Consultar").

✅ **Validado de ponta a ponta** com captcha real e dado real: as duas
mensagens de resultado foram confirmadas ("Não foram encontrados débitos
passíveis de parcelamento." pra consulta sem débito; não é gerado PDF
separado — o resultado é a própria página renderizada, capturada via
`salvar_pagina_como_pdf`).

⚠️ Bug real encontrado numa passada de revalidação: `document.body.innerText`
preserva as quebras de linha do layout renderizado — a frase de "sem
débito" às vezes quebra visualmente bem no meio, virando um `\n` onde o
código esperava um espaço, o que fazia o `in` simples falhar mesmo com o
texto certo na tela (o worker caía em `resultado_indefinido` mesmo tendo a
mensagem correta na tela). Corrigido normalizando espaços em branco
(`re.sub(r"\s+", " ", ...)`) antes de comparar — mesma correção aplicada
no worker de Certidão de Cadastro de Imóvel, que usa o mesmo padrão.
"""
import asyncio
import json
import re

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class CuritibaConsultaDebitosDividaAtiva(AutomacaoNodriverBase):
    portal = "curitiba_consulta_debitos_divida_ativa"
    url_inicial = "https://parcelamentoexecutado.curitiba.pr.gov.br/"

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._navegar_ate_formulario(page)
        await page.wait(2)

        imagem_base64 = await self._obter_imagem_captcha(page)
        resolvedor = obter_resolvedor()
        resposta_captcha = await resolvedor.resolver_captcha_imagem(imagem_base64)

        indicacao_fiscal = re.sub(r"\D", "", pedido.documento)
        await self._preencher_e_submeter(page, indicacao_fiscal, resposta_captcha)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # Segue o mesmo padrão defensivo do worker de Imóvel: tenta
            # achar um botão de impressão (caso essa plataforma também use
            # tela de confirmação + "Imprimir"), senão cai pro download
            # nativo, senão printa a própria tela como último recurso.
            await self._clicar_imprimir_se_existir(page)
            await page.wait(2)

            abas = page.browser.tabs
            if len(abas) > 1:
                page = abas[-1]
                await page.wait(2)

            if page.url.lower().split("?")[0].endswith(".pdf"):
                await self.aguardar_download_pdf_direto(page)

            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=8)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _navegar_ate_formulario(self, page):
        await page.wait(2)
        await page.evaluate("""
            (() => { location.href = 'frmConsultaDebitosContrib.aspx'; })()
        """)
        await page.wait(2)

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
                const radio = document.querySelector('input[name="info"][value="optIndFiscal"]');
                if (radio) radio.checked = true;
                document.getElementById('txtIndFiscal').value = {indicacao_js};
                document.getElementById('txtImgValidacao').value = {resposta_js};
                document.getElementById('btnEntrar').click();
            }})()
        """)

    async def _clicar_imprimir_se_existir(self, page):
        await page.evaluate("""
            (() => {
                const alvo = 'imprimir';
                const candidatos = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a'));
                const elemento = candidatos.find(el => {
                    const texto = (el.innerText || el.value || '').trim().toLowerCase();
                    return texto.includes(alvo);
                });
                if (elemento) elemento.click();
            })()
        """)

    async def aguardar_download_pdf_direto(self, page):
        from certidoes_core.config import config
        await page.set_download_path(config.BROWSER_DOWNLOAD_DIR)
        await page.download_file(page.url)
        await page.wait(2)

    async def _interpretar_resultado(self, page) -> dict:
        # Confirmado contra o site real: às vezes page.evaluate() devolve um
        # objeto de erro do CDP (ExceptionDetails) em vez de string — uma
        # corrida de tempo logo após o clique em "Consultar" (postback
        # clássico, recarrega a página inteira; se o evaluate roda no meio
        # da navegação, o contexto de execução é destruído). `(texto or
        # "").strip()` quebrava com AttributeError nesse caso. Forçamos
        # string aqui pra sempre cair no fallback seguro (resultado_indefinido)
        # em vez de derrubar o worker.
        texto_bruto = await page.evaluate("document.body.innerText")
        texto = texto_bruto.strip() if isinstance(texto_bruto, str) else ""
        # innerText preserva quebras de linha do layout renderizado — uma
        # frase que quebra visualmente no meio (ex: "não foram encontrados"
        # numa linha, "débitos..." na linha de baixo) vira um '\n' bem onde
        # o padrão de texto esperava um espaço, fazendo o "in" simples
        # falhar mesmo com o texto certo na tela (confirmado contra o site
        # real). Normalizamos espaços em branco só pra fins de comparação.
        texto_lower = re.sub(r"\s+", " ", texto.lower())

        if "não corresponde ao presente na imagem de validação" in texto_lower:
            return {"status": "erro_captcha", "mensagem": "Código de validação (captcha) não confere."}

        # Confirmado contra o site real: essa é a frase exata de "sem
        # débito" (não "nenhum débito", como eu tinha suposto antes de
        # testar) — checada ANTES da checagem genérica de "débito" logo
        # abaixo, senão cai no caso errado (essa frase também contém a
        # palavra "débitos").
        if "não foram encontrados débitos" in texto_lower or "não consta débito" in texto_lower:
            return {"status": "consulta_sem_debitos", "mensagem": "Consulta realizada — nenhum débito encontrado."}

        if "débito" in texto_lower and ("valor" in texto_lower or "inscrição" in texto_lower):
            return {"status": "consulta_com_debitos", "mensagem": "Consulta realizada — débito(s) encontrado(s)."}

        # Formulário provavelmente ainda na tela, sem erro de captcha
        # reconhecido — não arriscamos adivinhar (ex: Indicação Fiscal não
        # encontrada). Evidência automática garante o print pro humano
        # conferir.
        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao in ("consulta_sem_debitos", "consulta_com_debitos"):
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_captcha":
            return StatusPedido.ERRO_TECNICO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = CuritibaConsultaDebitosDividaAtiva()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
