"""
Worker do portal Certidão de Tributos Municipais — Pessoa Jurídica (CND),
Prefeitura de Curitiba. Reaproveita AutomacaoNodriverBase.

Mesma plataforma (mesmo domínio `cnd-cidadao.curitiba.pr.gov.br`), mesmo
captcha (Altcha) e essencialmente o mesmo HTML/JS do worker
`curitiba_cnd_cpf` — confirmado por inspeção ao vivo via nodriver (sem
gastar nenhum captcha, só carregando a página): mesmo formulário
`#frmCadastro`, mesmo botão `#btnSolicitar`, mesmo widget Altcha, mesmo
diálogo "Já existe certidão Emitida para este CNPJ." e mesma mensagem de
sucesso "Certidão gerada. Verifique o arquivo PDF criado na sua pasta de
download." — só troca o campo (`#DocumentoCnpj`, `maxlength=18` por causa
da máscara com pontuação, mas só dígitos são enviados) e a URL
(`/Certidao/SolicitarCnpj` em vez de `/Certidao/SolicitarCpf`).

Como é a mesma plataforma, todos os bugs já corrigidos no worker de CPF
(digitação com `digitar_devagar`, diálogo de certidão já existente,
processamento assíncrono depois de "Gerar Nova Certidão", clique real no
botão "Baixar" pra pegar o PDF de verdade em vez do fallback) já vêm
aplicados aqui desde o início, sem precisar redescobrir nada.
"""
import asyncio
import re

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase

UA_CHROME_REAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class CuritibaCndCnpj(AutomacaoNodriverBase):
    portal = "curitiba_cnd_cnpj"
    url_inicial = "https://cnd-cidadao.curitiba.pr.gov.br/Certidao/SolicitarCnpj"
    espera_inicial_segundos = 4
    # Mesmo ajuste do worker de CPF/FGTS/MPF: o Akamai bloqueia o Chromium
    # headless pelo User-Agent, não por IP.
    browser_args_extra = [f"--user-agent={UA_CHROME_REAL}"]

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        campo = await page.select("#DocumentoCnpj")
        digitos = re.sub(r"\D", "", pedido.documento or "")
        await self.digitar_devagar(campo, digitos)
        await page.wait(1)

        await self._resolver_altcha(page)
        await page.wait(1)

        await self._clicar_gerar_certidao(page)
        await self._tratar_aviso_certidao_existente(page)
        # Mesma correção do worker de CPF/Imóvel (mesma plataforma): depois
        # de "Gerar Nova Certidão", o site processa de forma assíncrona —
        # interpretar cedo demais pega esse processamento no meio do
        # caminho, às vezes como um "Erro 404" transitório.
        await self._aguardar_processamento_finalizar(page)
        await page.wait(2)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            # Sem esse clique, nada dispara o download real do site — ele só
            # mostra a certidão dentro de um modal com um botão "Baixar",
            # não baixa sozinho (mesmo bug já corrigido no worker de CPF).
            await self._clicar_baixar_certidao(page)
            # Bug real confirmado (PDF de evidência do usuário): clicar
            # "Baixar" pode reabrir o mesmo spinner "Aguardando
            # processamento ..." (o servidor prepara o arquivo de novo antes
            # de servir) — sem esperar isso terminar, o fallback de
            # screenshot capturava o spinner no meio do caminho em vez da
            # certidão pronta.
            await self._aguardar_processamento_finalizar(page)
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=20)
            if not caminho_certidao:
                await self._aguardar_processamento_finalizar(page)
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

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
                const botao = document.querySelector('#btnSolicitar');
                if (botao) botao.click();
            })()
        """)

    async def _tratar_aviso_certidao_existente(self, page, tentativas: int = 8):
        # Se já existe uma certidão emitida recentemente pro mesmo CNPJ, o
        # site mostra um diálogo "Já existe certidão Emitida para este
        # CNPJ" com botões "Visualizar"/"Gerar Nova Certidão" em vez de
        # gerar direto. Clica "Gerar Nova Certidão" pra sempre conseguir
        # uma via nova. Polling porque o diálogo pode demorar mais que o
        # esperado pra aparecer (mesmo bug já visto no worker de CPF).
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
            return {
                "status": "erro_tecnico",
                "mensagem": "Erro técnico do próprio portal (404) após o envio — provável limite de repetição "
                             "pro mesmo CNPJ testado poucos minutos antes, não bloqueio permanente. Ver evidência.",
            }
        if "não existir pendênc" in texto_lower or "certidão negativa" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão negativa gerada."}
        if "existir pendênc" in texto_lower or "certidão positiva" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão positiva gerada (há pendências)."}
        # Mesmo caso do worker de CPF/Imóvel: quando já existia certidão e
        # "Gerar Nova Certidão" é clicado, o conteúdo real fica dentro de
        # um visualizador de PDF interno, ilegível via innerText — só o
        # conjunto de botões (Imprimir/Baixar) denuncia que deu certo.
        if "imprimir" in texto_lower and "baixar" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão gerada (positiva ou negativa — conteúdo real está no PDF baixado)."}
        if "cnpj" in texto_lower and "inválid" in texto_lower:
            return {"status": "erro_portal", "mensagem": "CNPJ rejeitado pelo portal como inválido."}
        if "aguardando processamento" in texto_lower:
            return {
                "status": "erro_tecnico",
                "mensagem": "Portal ficou preso em \"Aguardando processamento\" sem responder — pode ser limite de repetição pro mesmo documento testado várias vezes seguidas.",
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
    automacao = CuritibaCndCnpj()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
