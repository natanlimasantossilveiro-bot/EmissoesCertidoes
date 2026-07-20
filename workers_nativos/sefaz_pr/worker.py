"""
Worker do portal SEFAZ PR — Certidão de Débitos Tributários e de Dívida
Ativa Estadual. Reaproveita AutomacaoNodriverBase.

🟢 **Reescrito em 16/07 a partir de um script próprio do usuário** (rodando
nativo no Windows há semanas, com dezenas de emissões reais bem-sucedidas
registradas em histórico local) que **nunca resolveu nenhum captcha** pra
esse portal. A versão anterior deste worker vinha forçando a resolução de
um reCAPTCHA Enterprise invisível via 2captcha — e o próprio 2captcha
devolvia `ERROR_CAPTCHA_UNSOLVABLE` nas 3 tentativas testadas, então nunca
chegamos a confirmar se isso sequer era necessário.

O script de referência prova que não era: o reCAPTCHA Enterprise invisível
desse portal roda em modo "score" (mesma família do v3) — o próprio JS do
Google executa sozinho em segundo plano e gera um token internamente,
sem exibir nenhum desafio, **desde que o comportamento pareça humano o
suficiente** pra tirar uma pontuação de risco boa. Não tem desafio pra
resolver — tem comportamento pra não levantar suspeita. Daí a mudança:

- Removido: hook de callback do reCAPTCHA Enterprise, chamada ao
  resolvedor de captcha (2captcha) — nenhuma das duas coisas é usada mais.
- Adicionado: digitação humanizada (`digitar_devagar`, com pausa aleatória
  antes de começar a digitar) em vez de setar `.value` via JS — script de
  referência nunca usou injeção direta de valor, só digitação de verdade.
- Adicionado: clique explícito no botão de download (texto do ícone
  Material `file_save`) antes de aguardar o PDF — o worker anterior nunca
  clicava em nada pra disparar o download, só esperava um arquivo aparecer.

⚠️ **Testado em retry-com-recarga-de-página e descartado**: repetir a
tentativa dentro da MESMA sessão de navegador (`page.get()` de novo após
um bloqueio) deixou o campo de documento sem aceitar digitação na
tentativa seguinte, em teste real. O script de referência nunca faz isso
— ele abre um navegador NOVO a cada tentativa (`emitir_com_retry`). Esse
worker não replica esse retry (deixaria o processamento bem mais lento);
um bloqueio vira ERRO_TECNICO e fica disponível pra nova tentativa manual
pelo painel.
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
    espera_inicial_segundos = 5
    # Rodando nativo (fora de container, não como root) não precisa e não
    # deve usar --no-sandbox — ver worker do receita_federal para o mesmo
    # ajuste e o motivo (flag denuncia automação pro reCAPTCHA Enterprise).
    requer_no_sandbox = False
    # ⚠️ Testado e descartado (20/07): apontar `browser_executable_path`
    # pro Chrome de verdade instalado na máquina (em vez do Chromium que o
    # nodriver baixa sozinho) NÃO resolveu — a primeira tentativa ainda
    # voltou bloqueio de automação, e passar a usar esse binário ainda
    # introduziu um bug novo (campo de documento não aceitando digitação
    # direito via esse Chrome específico). Testado também: trocar de rede
    # (wifi do escritório -> dados móveis) e trocar de documento — nada
    # mudou o resultado. Um acesso 100% manual (sem nodriver) passa de
    # primeira com o MESMO CNPJ, na MESMA máquina — aponta pra detecção da
    # própria conexão de automação (CDP), não de rede/documento/binário.
    # Sem solução conhecida com as ferramentas atuais.

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
            # Clique real (nodriver .click(), com user_gesture=True) antes
            # de digitar — `elem.apply("(elem) => elem.focus()")` sozinho
            # (usado dentro de `digitar_devagar`) vinha deixando o campo
            # sem foco de verdade em boa parte das tentativas de hoje,
            # mesmo com o navegador com tela aberta (campo ficava vazio
            # mesmo digitando devagar). Clicar de verdade primeiro é mais
            # parecido com o que um humano faz, e é mais confiável que só
            # `.focus()` via JS.
            await campo.click()
            await page.wait(0.5)
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
        # Clique via page.evaluate() + JS .click() NÃO carrega
        # `user_gesture=True` no CDP — o script de referência do usuário
        # usa o `.click()` nativo do próprio nodriver (que carrega essa
        # flag), e é o único jeito de clicar que já provou passar do
        # bloqueio real do site. Bem provável que seja exatamente esse
        # detalhe (não rede/documento/binário do Chrome) que o reCAPTCHA
        # Enterprise invisível usa pra decidir se o clique é humano.
        botao = await page.find("EMITIR CERTIDÃO", best_match=True)
        await botao.click()

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
