"""
Worker do portal Guia Amarela / Consulta Informativa de Lote (Secretaria
Municipal do Urbanismo, Prefeitura de Curitiba). Reaproveita
AutomacaoNodriverBase. Mesma família ASP.NET WebForms clássico + captcha de
imagem simples dos outros workers de Curitiba (`curitiba_certidao_cadastro_
imovel`, `curitiba_consulta_debitos_divida_ativa`).

⚠️ **Não é uma certidão formal** — é um documento informativo com os
parâmetros de zoneamento/uso do solo do imóvel (confirmado contra o site
real, testado manualmente pelo usuário): "Consulta Informativa do Lote",
usado pra elaboração de projetos, não tem valor de certidão.

⚠️ **HTTPS não funciona** — confirmado via `curl -v`: a conexão na porta
443 trava e nunca completa handshake TLS (10s+ sem resposta), enquanto a
porta 80 (HTTP puro) responde normalmente (200 OK). Por isso `url_inicial`
usa `http://`, não `https://` — provavelmente um bloqueio de rede
específico dessa porta pra esse host, não o site fora do ar (era a
suspeita anterior, registrada no catálogo como "pode ser instabilidade
temporária").

Mecânica confirmada por inspeção do HTML real (via `curl`, sem gastar
nenhum captcha):

- Formulário já está na própria `Default.aspx` (não precisa de um clique
  inicial "Emitir" pra navegar, diferente do worker de Cadastro de
  Imóvel).
- Radio `_ctl0:MainContent:rdoTipoDado`: `rbImo` (Inscrição Imobiliária,
  `txtInscrImob`, marcado por padrão) ou `rbIF` (Indicação Fiscal,
  `txtIndFiscal`). Usamos Indicação Fiscal — mesmo padrão já usado nos
  outros portais de Imóvel desse projeto (`pedido.documento`).
- `txtIndFiscal` (maxlength 12): aceita a forma resumida (8 dígitos) ou
  completa com sublote e dígito verificador (12 dígitos), só números —
  mesmo formato usado no worker de Débitos.
- `txtInscrSublote` (maxlength 4): campo opcional, não usado aqui (só
  restringe a consulta a um sublote específico).
- `rblFinalidade`: só existe UMA opção na tela ("Construção e Parcelamento
  do Solo", valor "1"), já vem marcada por padrão — não precisa de ação.
- Captcha: imagem simples, mas **diferente dos outros dois workers dessa
  família** — aqui o `<input type="image" id="imgValidar">` aponta pra uma
  URL (`frmCaptcha.aspx?...`), não uma imagem em base64 embutida direto no
  `src`. Buscamos os bytes via `fetch()` dentro da própria página
  (mesma técnica de `baixar_blob_url`, adaptada pra uma URL comum) e
  convertemos pra base64 antes de mandar pro resolvedor de captcha.
- Botão de consulta: `btnEmitir` (nome completo
  `_ctl0:MainContent:btnEmitir`).

✅ **Mecânica de preenchimento/envio confirmada correta** em teste real
(rádio marcado, Indicação Fiscal e resposta do captcha lidos de volta do
DOM antes de enviar, sempre certos). ⚠️ **Taxa de acerto do captcha baixa**
nos testes reais (2captcha errou a maioria das tentativas, incluindo
devolver só dígitos mesmo com a dica `numeric=4` pedindo letras+números) —
esse site, diferente dos outros dois workers da família, NÃO mostra
nenhuma mensagem de erro visível quando o captcha está errado: só reseta a
página em silêncio. Detectado pelo texto instrucional que só existe no
formulário (ver `_interpretar_resultado`), virando ERRO_TECNICO pra
acionar o retry automático da fila.

⚠️ **Bug real corrigido**: numa das tentativas (captcha aparentemente
correto), o worker travou indefinidamente depois do envio — sem nenhum
erro, sem nenhum progresso, consumindo CPU/rede continuamente. Causa
exata não isolada (suspeita: `download_file` esperando por um download
que o CDP nunca reconhece como concluído, ou a própria navegação pós-envio
demorando demais). Corrigido com `asyncio.wait_for(..., timeout=20)`
envolvendo tanto `_interpretar_resultado` quanto `download_file` — sem
isso, um travamento assim persiste pra sempre (prefetch=1 trava a fila
inteira, já que nenhuma mensagem nova é consumida enquanto a atual não
termina).

Fluxo pós-submissão usa a mesma cadeia defensiva dos outros dois workers
da família: tenta achar botão de impressão, checa se abriu aba nova, checa
se navegou pra uma URL `.pdf` (usa `download_file` nesse caso, com
timeout), senão printa a própria página como último recurso.

✅ **Validado de ponta a ponta** com captcha real e dado real (Indicação
Fiscal `81.644.023.000-8`): PDF final baixado corresponde à Consulta
Informativa do Lote completa (mesmos dados da consulta feita manualmente
pelo usuário como referência — Inscrição Imobiliária, Indicação Fiscal,
Zoneamento etc.). Dois bugs reais encontrados e corrigidos no caminho:

1. Num envio bem-sucedido, o site tenta abrir o resultado numa POP-UP
   automática (`target="_blank"`) — bloqueada pelo Chromium em contexto
   automatizado, sem erro nenhum, só não abre. Nesse caso o site mostra um
   banner verde temporário "Caso a guia não seja aberta automaticamente,
   clique aqui." com um link manual pro mesmo PDF (`_clicar_link_manual_
   se_existir`) — o mesmo que um humano clicaria. Faltava reconhecer esse
   banner como SUCESSO em `_interpretar_resultado` (o texto só aparece
   depois de um envio correto, nunca no carregamento inicial — confirmado
   via `curl`) — sem isso, virava "resultado_indefinido" mesmo com a
   consulta genuinamente emitida.
2. `_listar_pdfs_downloads` (em `AutomacaoNodriverBase`, comum a todos os
   workers) usava `glob("*.pdf")` — case-sensitive no Linux. Esse site
   serve o arquivo com extensão MAIÚSCULA (`.PDF`), então o download real
   ficava esquecido em `BROWSER_DOWNLOAD_DIR` pra sempre, e o worker caía
   sempre no fallback de imprimir a página (que ainda captura o conteúdo
   certo, via o visualizador de PDF do Chrome, mas não é o arquivo
   original). Corrigido na base pra também buscar `*.PDF` — beneficia
   qualquer outro worker que algum dia encontre esse mesmo padrão.
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


class CuritibaGuiaAmarela(AutomacaoNodriverBase):
    portal = "curitiba_guia_amarela"
    url_inicial = "http://www5.curitiba.pr.gov.br/gtm/gam/Default.aspx"

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._selecionar_indicacao_fiscal(page)

        imagem_base64 = await self._obter_imagem_captcha(page)
        resolvedor = obter_resolvedor()
        # Dicas pro 2captcha (confirmado visualmente contra o captcha real:
        # sempre 5 caracteres, mistura de letras e números, ex: "NE9TK") —
        # reduz o espaço de busca do OCR. Mesmo assim, taxa de acerto
        # observada em teste real ficou baixa (a maioria das tentativas
        # erra a leitura) — o retry automático da fila cobre isso.
        resposta_captcha = await resolvedor.resolver_captcha_imagem(
            imagem_base64, numeric=4, minLen=5, maxLen=5
        )

        indicacao_fiscal = re.sub(r"\D", "", pedido.documento or "")
        await self._preencher(page, indicacao_fiscal, resposta_captcha)
        await self._submeter(page)
        await page.wait(4)

        # ⚠️ Bug real encontrado em teste real: quando o captcha acerta e o
        # site navega pra gerar o PDF, `_interpretar_resultado` (ou depois,
        # `download_file`) pode travar indefinidamente — o site demora
        # bastante ou o download não é reconhecido como concluído pelo CDP.
        # Sem esse timeout, o worker fica preso pra sempre, nunca solta a
        # mensagem da fila (prefetch=1 trava todo o resto). Vira
        # ERRO_TECNICO (retry disponível) em vez de travar.
        try:
            resultado_bruto = await asyncio.wait_for(self._interpretar_resultado(page), timeout=20)
        except asyncio.TimeoutError:
            resultado_bruto = {"status": "erro_tecnico_timeout", "mensagem": "Interpretação do resultado travou (timeout)."}
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # Mesma cadeia defensiva usada nos outros workers dessa família:
            # tenta imprimir, checa aba nova, checa URL de PDF, senão
            # printa a própria tela como último recurso. Esse portal
            # especificamente tenta abrir o resultado numa POP-UP
            # automática (bloqueada pelo Chromium) — o link manual "clique
            # aqui" do banner é o mesmo alvo que um humano usaria nesse caso.
            await self._clicar_link_manual_se_existir(page)
            await self._clicar_imprimir_se_existir(page)
            await page.wait(2)

            abas = page.browser.tabs
            if len(abas) > 1:
                page = abas[-1]
                await page.wait(2)

            if page.url.lower().split("?")[0].endswith(".pdf"):
                await page.set_download_path(config.BROWSER_DOWNLOAD_DIR)
                try:
                    await asyncio.wait_for(page.download_file(page.url), timeout=20)
                except asyncio.TimeoutError:
                    print(f"[{self.portal}] download_file travou (timeout 20s) — seguindo pro fallback de print da página")
                await page.wait(2)

            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=8)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _selecionar_indicacao_fiscal(self, page):
        await page.evaluate("""
            (() => {
                const radio = document.getElementById('_ctl0_MainContent_rbIF');
                if (radio) radio.click();
            })()
        """)
        await page.wait(1)

    async def _obter_imagem_captcha(self, page) -> str:
        # Diferente dos outros dois workers dessa família (base64 já
        # embutido no src) — aqui o src é uma URL própria do site
        # (`frmCaptcha.aspx?...`). Busca os bytes via fetch() de dentro da
        # página (mesma sessão/cookies) e converte pra base64 com
        # FileReader, igual à técnica usada em `baixar_blob_url`.
        src = await page.evaluate("""
            (() => {
                const img = document.getElementById('_ctl0_MainContent_imgValidar');
                return img ? img.src : '';
            })()
        """)
        if not src:
            return ""
        if "base64," in src:
            return src.split("base64,", 1)[1]
        base64_dados = await page.evaluate(f"""
            (async () => {{
                const resp = await fetch({json.dumps(src)});
                const blob = await resp.blob();
                return await new Promise((resolve) => {{
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result.split(',')[1]);
                    reader.readAsDataURL(blob);
                }});
            }})()
        """, await_promise=True)
        return base64_dados or ""

    async def _preencher(self, page, indicacao_fiscal: str, resposta_captcha: str):
        indicacao_js = json.dumps(indicacao_fiscal)
        resposta_js = json.dumps(resposta_captcha)
        await page.evaluate(f"""
            (() => {{
                document.getElementById('_ctl0_MainContent_txtIndFiscal').value = {indicacao_js};
                document.getElementById('_ctl0_MainContent_txtImgValidacao').value = {resposta_js};
            }})()
        """)

    async def _submeter(self, page):
        await page.evaluate("""
            (() => {
                document.getElementById('_ctl0_MainContent_btnEmitir').click();
            })()
        """)

    async def _clicar_link_manual_se_existir(self, page):
        # Confirmado em teste real: após um envio bem-sucedido, o site tenta
        # abrir o resultado numa pop-up automática — bloqueada pelo
        # Chromium em contexto automatizado (sem erro nenhum, só não abre).
        # Nesse caso o site mostra um banner verde "Caso a guia não seja
        # aberta automaticamente, clique aqui." com um link manual — o
        # mesmo que um humano clicaria nessa situação.
        await page.evaluate("""
            (() => {
                const candidatos = Array.from(document.querySelectorAll('a'));
                const elemento = candidatos.find(el => (el.innerText || '').toLowerCase().includes('clique aqui'));
                if (elemento) elemento.click();
            })()
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

    async def _interpretar_resultado(self, page) -> dict:
        texto_bruto = await page.evaluate("document.body.innerText")
        # page.evaluate() pode devolver um objeto de erro do CDP em vez de
        # string, se rodar no meio de uma navegação (mesmo padrão já
        # confirmado nos outros workers dessa família) — força string aqui
        # pra nunca derrubar o worker com AttributeError.
        texto = texto_bruto.strip() if isinstance(texto_bruto, str) else ""
        # innerText preserva quebras de linha do layout renderizado — uma
        # frase que quebra visualmente no meio vira um '\n' onde o padrão
        # esperava um espaço (mesmo bug já corrigido nos outros workers
        # dessa família). Normaliza espaços em branco antes de comparar.
        texto_lower = re.sub(r"\s+", " ", texto.lower())

        if "não corresponde" in texto_lower and ("captcha" in texto_lower or "validação" in texto_lower or "valida" in texto_lower):
            return {"status": "erro_captcha", "mensagem": "Código de validação (captcha) não confere."}

        if ("inválid" in texto_lower or "não encontrad" in texto_lower) and "indicação fiscal" in texto_lower:
            return {"status": "erro_portal", "mensagem": "Indicação Fiscal rejeitada ou não encontrada pelo portal."}

        # Marcador exclusivo da página de resultado (confirmado contra uma
        # consulta real feita manualmente pelo usuário) — não aparece no
        # formulário de entrada.
        if "parâmetros da lei de zoneamento" in texto_lower or "consulta informativa do lote" in texto_lower:
            return {"status": "consulta_emitida", "mensagem": "Consulta informativa do lote emitida."}

        # ⚠️ Confirmado em teste real: num envio bem-sucedido, o site tenta
        # abrir o resultado numa pop-up automática — bloqueada pelo
        # Chromium — e volta pra ESSA MESMA página (formulário em branco,
        # pronta pra "nova consulta") só com esse banner verde extra
        # indicando sucesso. Esse texto só aparece depois de um envio
        # correto — nunca no carregamento inicial da página (confirmado
        # via `curl`, sem gastar nenhum captcha). Precisa vir ANTES da
        # checagem de "captcha errado" logo abaixo, porque as duas
        # combinam o mesmo texto instrucional do formulário — só esse
        # banner extra diferencia sucesso de reset silencioso por engano.
        if "guia não seja aberta automaticamente" in texto_lower:
            return {"status": "consulta_emitida", "mensagem": "Consulta emitida — pop-up automática bloqueada, aberta via link manual."}

        # ⚠️ Confirmado em teste real: diferente dos outros dois workers
        # dessa família, esse site NÃO mostra nenhuma mensagem de erro
        # visível quando o captcha está errado — só reseta a página de
        # volta pro formulário em branco, em silêncio. Sem esse
        # reconhecimento, isso caía em "resultado_indefinido"
        # (sucesso_provável), escondendo o problema e nunca acionando o
        # retry automático da fila. Detecta pelo texto instrucional que só
        # existe no formulário de entrada, nunca na página de resultado.
        if "emissão e impressão da consulta informativa de lote" in texto_lower:
            return {
                "status": "erro_captcha",
                "mensagem": "Formulário voltou em branco após o envio — provável captcha incorreto (esse site não mostra erro visível, só reseta a página).",
            }

        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao == "consulta_emitida":
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao in ("erro_captcha", "erro_tecnico_timeout"):
            return StatusPedido.ERRO_TECNICO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = CuritibaGuiaAmarela()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
