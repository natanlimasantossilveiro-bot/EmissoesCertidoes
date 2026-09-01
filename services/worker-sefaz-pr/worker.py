"""
Worker do portal SEFAZ PR — Certidão de Débitos Tributários e de Dívida
Ativa Estadual. Reaproveita AutomacaoNodriverBase.

🔴 **Bloqueado rodando em container (Linux, headless)** — reescrito em
16/07 a partir de um script próprio do usuário (rodando nativo no Windows
há semanas, com dezenas de emissões reais bem-sucedidas registradas em
histórico local) que **nunca resolveu nenhum captcha** pra esse portal. A
versão anterior deste worker vinha forçando a resolução de um reCAPTCHA
Enterprise invisível via 2captcha — e o próprio 2captcha devolvia
`ERROR_CAPTCHA_UNSOLVABLE` nas 3 tentativas testadas, então nunca chegamos
a confirmar se isso sequer era necessário.

O script de referência prova que não era: o reCAPTCHA Enterprise invisível
desse portal roda em modo "score" (mesma família do v3) — o próprio JS do
Google executa sozinho em segundo plano e gera um token internamente, sem
exibir nenhum desafio, **desde que o comportamento pareça humano o
suficiente** pra tirar uma pontuação de risco boa. Testado ao vivo: mesmo
sem tentar resolver captcha nenhum, o clique em "Emitir" não leva a nada
dentro do container (Chromium headless em Linux tira nota baixa demais).
**Esse worker só funciona rodando nativo, num Windows de verdade** — ver
`workers_nativos/sefaz_pr/worker.py`, onde a mesma lógica abaixo já foi
validada com um pedido real (chegou a receber uma resposta genuína de
bloqueio por automação do próprio portal, prova de que o reCAPTCHA estava
sendo passado). Este arquivo aqui fica como o worker "de container"
formalmente registrado no docker-compose, mas hoje não é o que roda de
fato — o container correspondente está parado (`docker compose stop
worker-sefaz-pr`) pra não competir pela fila com o worker nativo.
"""
import asyncio
import random
import re

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class SefazPrCertidaoDebitos(AutomacaoNodriverBase):
    portal = "sefaz_pr_certidao_debitos"
    url_inicial = "https://cdwfazenda.paas.pr.gov.br/cdwportal/certidao/automatica"
    # Sem solução de automação garantida (bloqueio por fingerprint avançado
    # tipo Akamai, confirmado mesmo rodando nativo do IP real do escritório
    # — não é detecção de SO nem reputação de IP). Na última tentativa, cai
    # pra AGUARDANDO_MANUAL em vez de DLQ — ver AutomacaoPortal.processar_pedido.
    url_fallback_manual = url_inicial
    espera_inicial_segundos = 5

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        # Só uma tentativa por navegador aberto: uma recarga de página
        # (`page.get()`) dentro da MESMA sessão, depois de um bloqueio,
        # provou em teste real deixar o campo de documento inutilizável
        # (não aceita mais digitação) — diferente do script de referência,
        # que abre um NAVEGADOR NOVO a cada tentativa. Em vez de replicar
        # esse custo aqui, um bloqueio vira ERRO_TECNICO e fica disponível
        # pra nova tentativa manual pelo painel (mesmo padrão já usado
        # nesse projeto pra outras falhas transitórias).
        await self._aceitar_cookies(page)
        await self._preencher_documento(page, pedido.documento)
        await self._clicar_emitir(page)
        await page.wait(5)

        resultado_bruto = await self._interpretar_resultado(page)

        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            await self._clicar_baixar_pdf(page)
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=8)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _aceitar_cookies(self, page):
        try:
            clicou = await page.evaluate("""
                (() => {
                    const botoes = Array.from(document.querySelectorAll('button'));
                    const botao = botoes.find(b => b.innerText && b.innerText.includes('Aceitar tudo'));
                    if (botao) { botao.click(); return true; }
                    return false;
                })()
            """)
            if clicou:
                await page.wait(2)
        except Exception:
            pass

    async def _preencher_documento(self, page, documento: str):
        for tentativa in range(1, 4):
            campo = await page.select('input[type="text"]')
            await page.wait(random.uniform(1.5, 3.5))
            await self.digitar_devagar(campo, documento, atraso_segundos=random.uniform(0.1, 0.3))
            await page.wait(1)

            valor_digitado = await page.evaluate("""
                (() => {
                    const campo = document.querySelector('input[type="text"]');
                    return campo ? campo.value : '';
                })()
            """)
            digitos_ok = re.sub(r"\D", "", valor_digitado or "")
            if digitos_ok == documento:
                return
            print(f"[{self.portal}] Campo veio vazio/incompleto na tentativa {tentativa} "
                  f"(esperado {documento}, leu {valor_digitado!r}) — tentando de novo.")
            await page.wait(1.5)

    async def _clicar_emitir(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => b.innerText && b.innerText.toUpperCase().includes('EMITIR'));
                if (botao) botao.click();
            })()
        """)

    async def _clicar_baixar_pdf(self, page):
        await page.wait(3)
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => b.innerText && b.innerText.includes('file_save'));
                if (botao) { botao.click(); return true; }
                return false;
            })()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        texto_bruto = await page.evaluate("document.body.innerText")
        texto = texto_bruto.strip() if isinstance(texto_bruto, str) else ""
        texto_lower = re.sub(r"\s+", " ", texto.lower())

        if "cpf inválido" in texto_lower or "cnpj inválido" in texto_lower:
            return {"status": "erro_portal", "mensagem": texto[:500]}

        if "certidões recentes emitidas para o requerente" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": texto[:1000]}

        if "consultas automatizadas" in texto_lower or "não podemos processar sua solicitação" in texto_lower:
            return {"status": "bloqueio_automacao", "mensagem": texto[:500]}

        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao == "certidao_emitida":
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        if status_emissao == "bloqueio_automacao":
            return StatusPedido.ERRO_TECNICO
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = SefazPrCertidaoDebitos()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
