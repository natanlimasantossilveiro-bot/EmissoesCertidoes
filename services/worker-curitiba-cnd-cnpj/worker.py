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
(digitação com `digitar_devagar`, processamento assíncrono depois de um
clique, clique real no botão "Baixar" pra pegar o PDF de verdade em vez
do fallback) já vêm aplicados aqui desde o início, sem precisar
redescobrir nada.

🔴 **Bug real confirmado em produção (14/09/2026), mesmo bug do worker de
CPF antes de ser corrigido lá**: clicar "Gerar Nova Certidão" quando já
existe certidão emitida pro CNPJ sempre falha (o portal só permite uma
emissão por CNPJ) — vira "Aguardando processamento" preso ou Erro 404
(reproduzido ao vivo com o CNPJ do próprio escritório, minutos depois de
uma emissão bem-sucedida). Corrigido do mesmo jeito, confirmado por
reconhecimento ao vivo (sem gastar nada): quando aparece o aviso de
certidão existente, segue pra "Emitir Segunda Via"
(`/Certidao/SegundaViaCnpj` — mesmo formulário `#DocumentoCnpj`/Altcha/
`#btnSolicitar`, texto "Imprimir"), pega a certidão mais recente da
listagem (`#tblListaCertidoes`) e baixa pelo mesmo modal
Imprimir/Baixar/Fechar do fluxo principal. Ver `_emitir_segunda_via`.
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
    url_segunda_via = "https://cnd-cidadao.curitiba.pr.gov.br/Certidao/SegundaViaCnpj"
    espera_inicial_segundos = 4
    # Mesmo ajuste do worker de CPF/FGTS/MPF: o Akamai bloqueia o Chromium
    # headless pelo User-Agent, não por IP.
    browser_args_extra = [f"--user-agent={UA_CHROME_REAL}"]
    # Mesma proteção genérica aplicada no TST CNDT (17/09/2026, ver
    # AutomacaoPortal): cancela e vira ERRO_TECNICO se travar sem
    # terminar, em vez de bloquear pra sempre o único slot da fila
    # (prefetch=1) até alguém perceber e reiniciar o container manualmente.
    timeout_execucao_segundos = 300

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        campo = await page.select("#DocumentoCnpj")
        digitos = re.sub(r"\D", "", pedido.documento or "")
        await self.digitar_devagar(campo, digitos)
        await page.wait(1)

        await self._resolver_altcha(page)
        await page.wait(1)

        await self._clicar_gerar_certidao(page)

        if await self._aviso_certidao_existente_apareceu(page):
            return await self._emitir_segunda_via(page, pedido, pdfs_antes)

        # Mesma correção do worker de CPF/Imóvel (mesma plataforma): o site
        # processa de forma assíncrona — interpretar cedo demais pega esse
        # processamento no meio do caminho, às vezes como um "Erro 404"
        # transitório.
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
                # Mesmo bug corrigido no worker de CPF: confere se a página
                # ainda mostra o resultado antes de aceitar o print como
                # certidão válida (senão pode gravar SUCESSO com um PDF do
                # formulário vazio, se a página já tiver voltado sozinha).
                if not await self._pagina_ainda_mostra_resultado(page):
                    return ResultadoEmissao(
                        status=StatusPedido.ERRO_TECNICO,
                        mensagem="O portal confirmou a certidão, mas a página voltou ao formulário inicial antes de "
                                 "conseguirmos capturar o arquivo final — tente novamente.",
                        caminho_certidao="",
                    )
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

    async def _aviso_certidao_existente_apareceu(self, page, tentativas: int = 8) -> bool:
        """Detecta (sem clicar em nada) se o diálogo "Já existe certidão
        Emitida para este CNPJ" apareceu. ⚠️ Pedir uma via nova nesse caso
        sempre falha nesse portal — o caminho certo é `_emitir_segunda_via`.
        Polling porque o diálogo pode demorar mais que o esperado pra
        aparecer."""
        for _ in range(tentativas):
            existe = await page.evaluate("""
                (() => {
                    const botoes = Array.from(document.querySelectorAll('button, a'));
                    return botoes.some(b => (b.innerText || '').trim() === 'Gerar Nova Certidão');
                })()
            """)
            if existe:
                return True
            await page.wait(1)
        return False

    async def _emitir_segunda_via(self, page, pedido: PedidoCertidao, pdfs_antes: set) -> ResultadoEmissao:
        """Já existe certidão emitida recentemente pro CNPJ — recupera a
        última via já emitida pela tela "Emitir Segunda Via", em vez de
        tentar gerar uma nova (que sempre falha). Mesma estrutura do
        worker de CPF, confirmada por reconhecimento ao vivo em 14/09/2026."""
        await page.get(self.url_segunda_via)
        await page.wait(3)

        campo = await page.select("#DocumentoCnpj")
        digitos = re.sub(r"\D", "", pedido.documento or "")
        await self.digitar_devagar(campo, digitos)
        await page.wait(1)

        await self._resolver_altcha(page)
        await page.wait(1)

        await self._clicar_gerar_certidao(page)  # mesmo #btnSolicitar, texto "Imprimir" aqui

        if not await self._aguardar_listagem_segunda_via(page):
            return ResultadoEmissao(
                status=StatusPedido.ERRO_TECNICO,
                mensagem="Já existe certidão emitida pra esse CNPJ, mas a listagem da segunda via não carregou.",
                caminho_certidao="",
            )

        await self._clicar_primeira_certidao_listagem(page)
        await page.wait(2)
        await self._clicar_baixar_certidao(page)
        await self._aguardar_processamento_finalizar(page)

        caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=15)
        if not caminho_certidao:
            if not await self._pagina_ainda_mostra_resultado(page):
                return ResultadoEmissao(
                    status=StatusPedido.ERRO_TECNICO,
                    mensagem="Já existe certidão emitida pra esse CNPJ, mas não foi possível capturar o arquivo da "
                             "segunda via — tente novamente.",
                    caminho_certidao="",
                )
            caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=StatusPedido.SUCESSO_CONFIRMADO,
            mensagem="Certidão recuperada via 'Emitir Segunda Via' (já existia uma emissão recente pra esse CNPJ).",
            caminho_certidao=caminho_certidao,
        )

    async def _aguardar_listagem_segunda_via(self, page, tentativas: int = 15) -> bool:
        for _ in range(tentativas):
            existe = await page.evaluate("""
                (() => !!document.querySelector('#tblListaCertidoes tbody tr td button[data-action="imprimir"]'))()
            """)
            if existe:
                return True
            await page.wait(1)
        return False

    async def _clicar_primeira_certidao_listagem(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.querySelector('#tblListaCertidoes tbody tr td button[data-action="imprimir"]');
                if (botao) botao.click();
            })()
        """)

    async def _pagina_ainda_mostra_resultado(self, page) -> bool:
        """Mesmos padrões de texto usados em `_interpretar_resultado` pra
        confirmar sucesso — reaproveitados aqui só pra checar se a página
        ainda está no mesmo estado (não voltou pro formulário inicial)
        antes de aceitar um print de fallback como certidão válida."""
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto_lower = (texto or "").lower()
        return (
            "não existir pendênc" in texto_lower
            or "existir pendênc" in texto_lower
            or "certidão negativa" in texto_lower
            or "certidão positiva" in texto_lower
            or ("imprimir" in texto_lower and "baixar" in texto_lower)
        )

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
