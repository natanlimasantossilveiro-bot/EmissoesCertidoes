"""
Worker do portal Certidão de Tributos Municipais — Imóvel (débitos de
IPTU), Prefeitura de Curitiba. Reaproveita AutomacaoNodriverBase.

Mesmo domínio e mesmo captcha do worker `curitiba_cnd_cpf` (ver aviso
completo lá sobre o Altcha e o desbloqueio do Akamai por User-Agent) —
formulário diferente, porque aqui a certidão é sobre o IMÓVEL, não sobre
a pessoa.

Mecânica confirmada por inspeção ao vivo (nodriver):

- Campo `Escolha` (radio): **0** = identificar o imóvel por Inscrição
  Imobiliária (`#InscricaoImobiliaria`) + Sublote (`#SubLote`); **1** =
  por Indicação Fiscal (`#IndicacaoFiscal`, campo fica desabilitado até
  marcar essa opção). Esse worker usa sempre a opção 1 (Indicação
  Fiscal) — é o dado que os outros dois workers de Curitiba já usam
  nesse projeto (`curitiba_certidao_cadastro_imovel`,
  `curitiba_consulta_debitos_divida_ativa`), então `pedido.documento`
  já vem nesse formato pro grupo Imóvel.
- `#DocumentoProprietario` — CPF/CNPJ do proprietário (`pedido.tipo`
  incorporado no `documento` combinado, ver `_dividir_documento`: esse
  grupo já manda "indicação_fiscal|documento_proprietario" concatenado
  — não, na verdade **não** manda: o grupo Imóvel desse projeto só tem
  um campo `documento` por pedido. Como esse formulário pede os DOIS
  (indicação fiscal E documento do proprietário), usamos
  `pedido.documento` pra Indicação Fiscal e reaproveitamos
  `pedido.data_nascimento` (campo texto livre já existente no modelo,
  não usado por portais de Imóvel) pra guardar o CPF/CNPJ do
  proprietário — evita mexer no schema do banco pra um caso isolado.
- `#cboFinalidade` — obrigatório, sem opção genérica óbvia; seleciona a
  primeira não-vazia (mesmo critério defensivo já usado no worker do
  Pinhais quando só existe 1 opção real por trás).
- Botão de envio: `#btnConsultar` (diferente do worker de CPF, que usa
  `#btnSolicitar` — são páginas parecidas mas não idênticas, confirmado
  inspecionando os dois HTMLs separadamente).

⚠️ Mesma correção do worker de CPF aplicada aqui: preenchimento via
`digitar_devagar()` em vez de `send_keys()` puro, pra não embaralhar os
dígitos na máscara do campo, e o mesmo tratamento do diálogo "Já existe
certidão Emitida" (clica "Gerar Nova Certidão" automaticamente) — ver
avisos completos em `services/worker-curitiba-cnd-cpf/worker.py`.

⚠️ **Problema real de design encontrado testando pelo painel de
verdade** (ainda sem solução — ver conversa com o usuário): o campo
"Documento do Proprietário" precisa de um CPF/CNPJ de verdade, mas o
único campo livre reaproveitado pra isso (`pedido.data_nascimento`) é
formatado como DATA (`dd/mm/aaaa`) pela própria máscara do campo
"Data de nascimento" no front — não dá pra digitar um CPF/CNPJ ali pela
tela. Testando pelo painel, o valor foi enviado sem tratamento nenhum
(nem os `/` da data foram removidos), então o portal recebeu algo como
"310.320.06" em vez de um documento válido. Precisa de uma decisão de
produto: ou adiciona um campo novo no front específico pra esse
segundo documento (só pro grupo Imóvel), ou usa algum outro dado já
disponível.
"""
import asyncio
import json

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase

UA_CHROME_REAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class CuritibaCertidaoTributosImovel(AutomacaoNodriverBase):
    portal = "curitiba_certidao_tributos_imovel"
    url_inicial = "https://cnd-cidadao.curitiba.pr.gov.br/Certidao/Solicitar"
    espera_inicial_segundos = 4
    browser_args_extra = [f"--user-agent={UA_CHROME_REAL}"]

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._selecionar_indicacao_fiscal(page)
        campo_if = await page.select("#IndicacaoFiscal")
        await self.digitar_devagar(campo_if, pedido.documento or "")
        await page.wait(1)

        campo_doc = await page.select("#DocumentoProprietario")
        await self.digitar_devagar(campo_doc, pedido.data_nascimento or "")
        await page.wait(1)

        await self._selecionar_finalidade(page)
        await self._resolver_altcha(page)
        await page.wait(1)

        await self._clicar_gerar_certidao(page)
        await self._tratar_aviso_certidao_existente(page)
        # Numa geração NOVA (sem "já existe certidão"), o site processa a
        # certidão de forma assíncrona no servidor — mostra um spinner
        # "Aguardando processamento ..." e só dispara o download real do
        # PDF quando esse processamento termina. Interpretar o resultado
        # antes disso pega o spinner no meio do caminho (confirmado em
        # teste real: virava ERRO_TECNICO por bater no texto do próprio
        # spinner, ou o download real ainda nem tinha começado).
        await self._aguardar_processamento_finalizar(page)
        await page.wait(2)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # Sem esse clique, nada dispara o download real do site — o
            # worker cai direto no fallback de "imprimir a página atual em
            # PDF", que captura a tela inteira (modal, scrollbar, botões)
            # em vez do documento oficial que o próprio "Baixar" gera.
            # Só existe quando o modal "já existe certidão" leva a um
            # visualizador com botão "Baixar"; numa geração nova o próprio
            # site dispara o download sozinho, esse clique só não acha nada.
            await self._clicar_baixar_certidao(page)
            # Janela maior de espera (20s): o download real só começa depois
            # do processamento assíncrono do servidor, confirmado mais lento
            # que os 10s usados originalmente (a mesma janela que funciona
            # bem pros outros portais de Curitiba).
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=20)
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
                const radio = document.querySelector('input[name="Escolha"][value="1"]');
                if (radio) radio.click();
            })()
        """)

    async def _selecionar_finalidade(self, page):
        await page.evaluate("""
            (() => {
                const sel = document.querySelector('#cboFinalidade');
                if (!sel) return;
                const opcao = Array.from(sel.options).find(o => o.value);
                if (!opcao) return;
                sel.value = opcao.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
            })()
        """)

    async def _resolver_altcha(self, page):
        await page.evaluate("""
            (() => {
                const cb = document.querySelector('#altcha-container input[type="checkbox"]');
                if (cb) cb.click();
            })()
        """)
        for _ in range(10):
            await page.wait(1)
            estado = await page.evaluate("""
                (() => {
                    const w = document.querySelector('.altcha');
                    return w ? w.getAttribute('data-state') : null;
                })()
            """)
            if estado == "verified":
                return

    async def _clicar_gerar_certidao(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.querySelector('#btnConsultar');
                if (botao) botao.click();
            })()
        """)

    async def _tratar_aviso_certidao_existente(self, page, tentativas: int = 8):
        # Ver aviso completo em services/worker-curitiba-cnd-cpf/worker.py
        # sobre por que essa checagem precisa repetir (polling), não só
        # tentar uma vez.
        for _ in range(tentativas):
            clicou = await page.evaluate("""
                (() => {
                    const botoes = Array.from(document.querySelectorAll('button, a'));
                    const botao = botoes.find(b => (b.innerText || '').trim() === 'Gerar Nova Certidão');
                    if (botao) { botao.click(); return true; }
                    return false;
                })()
            """)
            if clicou:
                await page.wait(1)
                return
            await page.wait(1)

    async def _aguardar_processamento_finalizar(self, page, tentativas: int = 15):
        for _ in range(tentativas):
            texto = await page.evaluate("(() => document.body.innerText)()")
            texto_lower = (texto or "").lower()
            if "aguardando processamento" not in texto_lower:
                return
            await page.wait(1)

    async def _clicar_baixar_certidao(self, page):
        await page.evaluate("""
            (() => {
                const elementos = Array.from(document.querySelectorAll('button, a'));
                const botao = elementos.find(el => (el.innerText || '').trim() === 'Baixar');
                if (botao) { botao.click(); return true; }
                return false;
            })()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        if "erro 404" in texto_lower or "não pode ser encontrado" in texto_lower:
            return {"status": "erro_tecnico", "mensagem": "Erro técnico do próprio portal (404) após o envio — ver evidência."}
        # Mesmo texto exato confirmado no worker de CPF (mesma plataforma).
        if "não existir pendênc" in texto_lower or "certidão negativa" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão negativa gerada."}
        if "existir pendênc" in texto_lower or "certidão positiva" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão positiva gerada (há pendências)."}
        if "indicação fiscal" in texto_lower and "inválid" in texto_lower:
            return {"status": "erro_portal", "mensagem": "Indicação Fiscal rejeitada pelo portal como inválida."}
        # Confirmado testando com dado real (21/07): o conteúdo da certidão
        # (positiva/negativa) fica dentro de um visualizador de PDF interno
        # ao modal — texto ilegível via document.body.innerText, mas o
        # conjunto de botões "Imprimir/Baixar/Pendências" só aparece quando
        # a certidão foi realmente gerada. O PDF de verdade já é baixado à
        # parte por aguardar_e_mover_pdf logo abaixo; aqui só confirmamos o
        # sucesso, sem tentar adivinhar positiva/negativa pelo texto.
        if "imprimir" in texto_lower and "baixar" in texto_lower and "pendências" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão gerada (positiva ou negativa — conteúdo real está no PDF baixado, ilegível via texto da página por estar num visualizador interno)."}
        if "aguardando processamento" in texto_lower:
            return {
                "status": "erro_tecnico",
                "mensagem": "Portal ficou preso em \"Aguardando processamento\" sem responder — confira se o Documento do Proprietário está num formato válido, ou se é limite de repetição pro mesmo dado testado várias vezes seguidas.",
            }
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
    automacao = CuritibaCertidaoTributosImovel()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
