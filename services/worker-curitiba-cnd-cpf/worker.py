"""
Worker do portal Certidão de Tributos Municipais — Pessoa Física (CND),
Prefeitura de Curitiba. Reaproveita AutomacaoNodriverBase.

Antes bloqueado pelo Akamai Bot Manager (domínio inteiro, "Access Denied")
rodando do ambiente de datacenter/cloud — retestado em 15/07/2026 a partir
da rede real do escritório com o mesmo `--user-agent` corrigido já usado
no FGTS/MPF, e abriu limpo (ver `docs/CATALOGO_PORTAIS.md`).

Mecânica confirmada por inspeção ao vivo (nodriver, sem gastar 2captcha —
esse portal não usa 2captcha nenhum, ver abaixo):

- Sistema clássico ASP.NET MVC + jQuery (não é SPA) — formulário
  `#frmCadastro`, campo `#DocumentoCpf` (texto, só dígitos, `maxlength=14`).
- **Captcha: Altcha, não 2captcha.** É um captcha de prova computacional
  (proof-of-work) — o widget (`<altcha-widget>`) resolve sozinho no
  navegador assim que o checkbox "Não sou um robô" é clicado, sem
  precisar de nenhum serviço de resolução externo. Confirmado ao vivo:
  clicar o checkbox (`#altcha-container input[type="checkbox"]`) muda o
  atributo `data-state` do widget de "unverified" pra "verified" em
  menos de 1 segundo, populando sozinho um campo hidden com o payload
  assinado (challenge/signature). Widget tem `auto="off"`, por isso
  precisa do clique — não resolve sozinho sem interação nenhuma.
- Botão de envio: `#btnSolicitar` (um `<a>` estilizado como botão, não
  um `<button>`/`<input type=submit>` de verdade — clicar via JS
  `.click()` aciona o handler jQuery do site normalmente).

⚠️ **Bug real encontrado no primeiro teste real**: preencher o campo
`#DocumentoCpf` com `Element.send_keys()` (teclas via CDP, sem pausa
entre uma e outra) saiu com os dígitos fora de ordem — o campo tem uma
máscara de formatação em JS que não consegue acompanhar teclas
disparadas rápido demais uma atrás da outra (confirmado comparando o
valor final no print de evidência: "081.152.924-93" em vez de
"081.315.299-24"). Corrigido com `digitar_devagar()` (novo, em
`AutomacaoNodriverBase`), que espaça cada tecla.

⚠️ **Bug real encontrado testando pelo painel de verdade**: se já existe
uma certidão emitida recentemente pro mesmo CPF, o site mostra um
diálogo "Aviso — Já existe certidão Emitida para este CPF." com dois
botões ("Visualizar" / "Gerar Nova Certidão") **em vez de** gerar a
certidão direto — sem tratar isso, o worker ficava parado nesse
diálogo e a captura de PDF pegava só a tela do aviso, não a certidão.
Corrigido clicando "Gerar Nova Certidão" automaticamente quando esse
diálogo aparece (`_tratar_aviso_certidao_existente`), garantindo uma
via nova a cada pedido em vez de reaproveitar a anterior.
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


class CuritibaCndCpf(AutomacaoNodriverBase):
    portal = "curitiba_cnd_cpf"
    url_inicial = "https://cnd-cidadao.curitiba.pr.gov.br/Certidao/SolicitarCpf"
    espera_inicial_segundos = 4
    # Mesmo ajuste do FGTS/MPF: o Akamai bloqueava o Chromium headless
    # pelo User-Agent, não por IP — ver aviso no topo do arquivo.
    browser_args_extra = [f"--user-agent={UA_CHROME_REAL}"]

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        campo = await page.select("#DocumentoCpf")
        digitos = re.sub(r"\D", "", pedido.documento or "")
        await self.digitar_devagar(campo, digitos)
        await page.wait(1)

        await self._resolver_altcha(page)
        await page.wait(1)

        await self._clicar_gerar_certidao(page)
        await self._tratar_aviso_certidao_existente(page)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=10)
            if not caminho_certidao:
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
        # Se já existe uma certidão emitida recentemente pro mesmo CPF,
        # o site mostra um diálogo "Já existe certidão Emitida para este
        # CPF" com botões "Visualizar"/"Gerar Nova Certidão" em vez de
        # gerar direto. Clica "Gerar Nova Certidão" pra sempre conseguir
        # uma via nova, em vez de ficar parado nesse diálogo.
        # ⚠️ Bug real: uma checagem única (sem repetir) perdia o diálogo
        # quando ele demorava mais que o esperado pra aparecer — a
        # interpretação seguinte acabava lendo o texto do próprio
        # diálogo em vez do resultado. Corrigido com polling.
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

    async def _interpretar_resultado(self, page) -> dict:
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        if "erro 404" in texto_lower or "não pode ser encontrado" in texto_lower:
            return {"status": "erro_tecnico", "mensagem": "Erro técnico do próprio portal (404) após o envio — ver evidência."}
        # Confirmado contra o site real: o texto exato da certidão negativa
        # é "certificamos não existir pendências em nome do contribuinte".
        if "não existir pendênc" in texto_lower or "certidão negativa" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão negativa gerada."}
        if "existir pendênc" in texto_lower or "certidão positiva" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão positiva gerada (há pendências)."}
        if "cpf" in texto_lower and "inválid" in texto_lower:
            return {"status": "erro_portal", "mensagem": "CPF rejeitado pelo portal como inválido."}
        # Confirmado num teste real: às vezes o portal fica preso em
        # "Aguardando processamento..." indefinidamente sem erro nem
        # resultado — suspeita de limite de repetição pro mesmo CPF
        # testado várias vezes seguidas. Mensagem própria pra facilitar
        # o diagnóstico, em vez de aparecer como "resultado não
        # identificado" genérico.
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
    automacao = CuritibaCndCpf()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
