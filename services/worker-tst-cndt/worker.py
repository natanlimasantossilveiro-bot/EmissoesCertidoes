"""
Worker do portal CNDT — Certidão Negativa de Débitos Trabalhistas (TST).
Reaproveita AutomacaoNodriverBase.

Mecânica confirmada por inspeção ao vivo:

- A página pública (`www.tst.jus.br/certidao1`) é só uma casca — o
  formulário de verdade vive num iframe separado, em
  `https://cndt-certidao.tst.jus.br/`. Navegamos direto pra lá.
- É uma aplicação JSF/RichFaces (Java) antiga — não achei nenhum vestígio
  de reCAPTCHA/hCaptcha. O botão inicial "Emitir Certidão" dispara uma
  chamada AJAX (RichFaces `A4J.AJAX.Submit`) que troca o conteúdo da
  página pelo formulário de emissão, sem navegação — por isso usamos um
  navegador de verdade (nodriver) em vez de tentar reconstruir a chamada
  AJAX manualmente.
- Campo único pra CPF ou CNPJ: `gerarCertidaoForm:cpfCnpj`.
- Captcha: **imagem simples própria do sistema** (não é reCAPTCHA/hCaptcha
  de terceiro) — `<img id="idImgBase64" src="data:image/...;base64,...">`.
  Usa o método `resolver_captcha_imagem` do `certidoes_core.captcha`, que
  já existia pronto na interface (nunca tinha sido usado até agora).
  Resposta vai no campo `idCampoResposta` (name="resposta").
- Confirmei a mensagem de validação de documento inválido: "O CNPJ / CPF
  informado é inválido." (aparece na div `#mensagens`).
- O botão de emitir (`gerarCertidaoForm:btnEmitirCertidao`) tem no title
  "o PDF da certidão será baixado" — download nativo esperado, diferente
  do CPF/CNPJ+QSA da Receita (que renderizam o comprovante inline).

⚠️ Bug real encontrado num teste com sucesso de verdade (captcha certo,
documento sem débito): a tela de "Certidão EMITIDA com sucesso" **não é**
a certidão — é só uma tela de confirmação com botões. O PDF de verdade
sai de uma navegação automática (`window.parent.location =
'/emissaoCertidao?pfnc=NNN'`), disparada sozinha por um evento JS antigo
do RichFaces (`a4j:status onstop`) assim que a certidão é emitida —
confirmado via CDP Network que esse request acontece SOZINHO, com
resposta `200`, `content-type: application/pdf` e
`content-disposition: attachment` (certidão de verdade, token de
uso único).

O motivo do worker não estar salvando esse PDF: a pasta de download só é
configurada no Chrome (`page.set_download_path`) depois do clique em
"Emitir", tarde demais pra pegar esse download automático — o Chrome
não tinha pra onde salvar e descartava silenciosamente. (Tentei também
reextrair a URL e rebaixar manualmente depois — não funciona, o token
já foi consumido pelo download automático, a segunda tentativa sempre
volta vazia.) Corrigido chamando `set_download_path` ANTES de clicar em
"Emitir", pra o Chrome já saber onde salvar quando o evento automático
disparar.
"""
import asyncio
import json

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.config import config
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class TstCndt(AutomacaoNodriverBase):
    portal = "tst_cndt"
    url_inicial = "https://cndt-certidao.tst.jus.br/"

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()
        # Precisa vir ANTES do clique em "Emitir" — o download real é
        # disparado automaticamente pelo próprio site logo depois da
        # emissão (ver aviso no topo do arquivo), então o Chrome já
        # precisa saber pra onde salvar nesse momento.
        await page.set_download_path(config.BROWSER_DOWNLOAD_DIR)

        await self._clicar_emitir_certidao_inicial(page)
        await page.wait(3)

        imagem_base64 = await self._obter_imagem_captcha(page)
        resolvedor = obter_resolvedor()
        resposta_captcha = await resolvedor.resolver_captcha_imagem(imagem_base64)

        await self._preencher_formulario(page, pedido.documento, resposta_captcha)
        await page.wait(1)
        await self._clicar_emitir(page)
        await page.wait(4)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=15)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _clicar_emitir_certidao_inicial(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('input[type=submit]'));
                const botao = botoes.find(b => b.value === 'Emitir Certidão');
                if (botao) botao.click();
            })()
        """)

    async def _obter_imagem_captcha(self, page) -> str:
        src = await page.evaluate("""
            (() => {
                const img = document.getElementById('idImgBase64');
                return img ? img.src : '';
            })()
        """)
        # src vem como "data:image/png;base64,XXXXX" — resolver_captcha_imagem
        # espera só o conteúdo base64, sem o prefixo do data URI.
        if "base64," in src:
            return src.split("base64,", 1)[1]
        return src

    async def _preencher_formulario(self, page, documento: str, resposta_captcha: str):
        documento_js = json.dumps(documento)
        resposta_js = json.dumps(resposta_captcha)
        await page.evaluate(f"""
            (() => {{
                document.getElementById('gerarCertidaoForm:cpfCnpj').value = {documento_js};
                document.getElementById('idCampoResposta').value = {resposta_js};
            }})()
        """)

    async def _clicar_emitir(self, page):
        await page.evaluate("""
            (() => {
                const btn = document.getElementById('gerarCertidaoForm:btnEmitirCertidao');
                if (btn) btn.click();
            })()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        mensagem = await page.evaluate("""
            (() => {
                const el = document.getElementById('mensagens');
                return el ? el.innerText.trim() : '';
            })()
        """)
        # page.evaluate() pode devolver um objeto de erro do CDP
        # (ExceptionDetails) em vez de string, se rodar no meio de uma
        # navegação — confirmado em outro worker desse projeto (mesmo
        # padrão), onde isso derrubava o worker com AttributeError.
        mensagem = mensagem if isinstance(mensagem, str) else ""
        mensagem_lower = mensagem.lower()

        if "inválido" in mensagem_lower or "captcha" in mensagem_lower or "caracteres" in mensagem_lower:
            return {"status": "erro_portal", "mensagem": mensagem}
        if "débito" in mensagem_lower and ("possui" in mensagem_lower or "consta" in mensagem_lower):
            return {"status": "certidao_positiva", "mensagem": mensagem}
        if not mensagem:
            # Sem mensagem de erro visível — provável sucesso (PDF deve ter
            # baixado nativamente; aguardar_e_mover_pdf confirma isso).
            return {"status": "certidao_emitida", "mensagem": "Certidão emitida (sem mensagem de erro na tela)."}
        return {"status": "resultado_indefinido", "mensagem": mensagem}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao in ("certidao_emitida", "certidao_positiva"):
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = TstCndt()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))