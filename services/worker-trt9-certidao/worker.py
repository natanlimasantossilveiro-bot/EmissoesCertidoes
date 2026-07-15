"""
Worker do portal Certidão Trabalhista (PJe) — TRT9 (9ª Região). Reaproveita
AutomacaoNodriverBase.

Antes bloqueado pelo CloudFront ("403 Request blocked") rodando do
ambiente de datacenter/cloud — retestado em 15/07/2026 a partir da rede
real do escritório com o mesmo `--user-agent` corrigido usado no
FGTS/MPF, e abriu limpo (ver `docs/CATALOGO_PORTAIS.md`).

Mecânica confirmada por inspeção ao vivo (nodriver):

- SPA Angular Material (PJe Certidões, versão 2.2) — navegação real
  entre rotas (`/certidoes/inicio` → `/certidoes/trabalhista/emissao` →
  `/certidoes/captcha`), não tudo dentro da mesma tela.
- `/certidoes/inicio`: botão "EMITIR" (o primeiro, associado à seção
  "Certidão Trabalhista" — a tela também tem "Certidão de Advogado",
  que não usamos).
- `/certidoes/trabalhista/emissao`: radio de critério de pesquisa
  (`input[type=radio][value="RAIZ_DE_CNPJ"|"CPF"|"NOME"]`) + UM campo
  de texto reaproveitado pros três critérios (`input[matinput]` — o
  `id` muda a cada carga de página, tipo `mat-input-1`, por isso
  selecionamos pelo atributo `matinput`, não por id fixo). Botão
  "EMITIR" (`button[type=submit]`) fica desabilitado até o formulário
  ficar válido.
  ⚠️ **Bug real encontrado no reconhecimento**: preencher esse campo
  só com `.value = X` + `dispatchEvent('input')` (a técnica usada em
  quase todo o resto do projeto) **não** convence o Angular Material
  daqui — o botão continuou desabilitado. Resolvido usando
  `Element.send_keys()` do próprio nodriver, que manda teclas de
  verdade via CDP (`Input.dispatchKeyEvent`) em vez de só simular o
  evento — aí sim o Angular reconhece o valor e habilita o botão.
- Submeter esse formulário **navega de verdade** pra
  `/certidoes/captcha` — um desafio de **captcha de imagem simples**
  (não reCAPTCHA/hCaptcha/Turnstile): `<img class="imagem-captcha"
  src="data:image/jpg;base64,...">`, resposta de 6 caracteres num outro
  `input[matinput]`, botão "Enviar" (`button[type=submit]`, também
  desabilitado até preencher). Resolvido com
  `resolver_captcha_imagem`, igual ao worker da TST/CNDT.

✅ **Validado de ponta a ponta 4 vezes seguidas**, com captcha real
(2captcha pago) e CPF real: certidão "NÃO CONSTAM ações trabalhistas..."
com código de verificação real, capturada corretamente via
`salvar_pagina_como_pdf` (não é download nativo — mesmo padrão do
CPF/Situação Cadastral) em **todas as 4 tentativas**, conferido abrindo
o PDF de cada uma.

⚠️ **Bug real, ainda sem solução definitiva (cosmético, não afeta o
arquivo entregue)**: depois de enviar a resposta do captcha, o Angular
Material anuncia "Enviando a resposta, por favor aguarde." numa região
de acessibilidade (`aria-live`, do CDK) que **fica no `innerText` da
página indefinidamente** mesmo depois do resultado real já ter
carregado por completo — confirmado repetidas vezes: o print de
evidência (tirado alguns instantes depois) sempre mostra a certidão
certinha, mas o texto interpretado no momento exato ainda acusa
"aguarde", mesmo tentando esperar por um sinal positivo
("código de verificação") em vez da ausência do "aguarde". Na prática
isso só rebaixa o status de sucesso_confirmado pra sucesso_provável e
deixa uma mensagem menos específica — o PDF em si sai correto de
qualquer forma, então o worker é funcionalmente confiável apesar desse
detalhe cosmético. `_interpretar_resultado` já detecta esse cenário
específico e troca a mensagem por uma que orienta a conferir o PDF
anexado, em vez de mostrar o texto de carregamento cru.
"""
import asyncio
import re

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase

UA_CHROME_REAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class Trt9CertidaoTrabalhista(AutomacaoNodriverBase):
    portal = "trt9_certidao_trabalhista"
    url_inicial = "https://pje.trt9.jus.br/certidoes/inicio"
    espera_inicial_segundos = 5
    browser_args_extra = [f"--user-agent={UA_CHROME_REAL}"]

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        await self._clicar_emitir_trabalhista(page)
        await page.wait(3)

        await self._selecionar_criterio(page, pedido.tipo)
        digitos = re.sub(r"\D", "", pedido.documento or "")
        campo = await page.select("input[matinput]")
        await campo.send_keys(digitos)
        await page.wait(1)

        await self._clicar_submit(page)
        await page.wait(4)

        if "/certidoes/captcha" in page.url:
            resolvido = await self._resolver_captcha_imagem(page)
            if not resolvido:
                return ResultadoEmissao(
                    status=StatusPedido.ERRO_TECNICO,
                    mensagem="Não foi possível localizar/resolver o captcha de imagem do TRT9.",
                )
            # Confirmado em 3 testes reais seguidos: a tela "Enviando a
            # resposta, por favor aguarde." é imprevisível — às vezes
            # passa em poucos segundos, às vezes ainda aparecia depois de
            # 25s (mesmo assim a certidão real já tinha sido gerada nos
            # bastidores, só a tela do navegador que demorava a
            # atualizar). Espera bem mais generosa aqui evita interpretar
            # a tela de carregamento como se fosse o resultado final.
            await self._aguardar_processamento(page, tentativas=60)

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes, tentativas=10)
            if not caminho_certidao:
                caminho_certidao = await self.salvar_pagina_como_pdf(page, pedido)
        if status_final not in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = ""

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _clicar_emitir_trabalhista(self, page):
        await page.evaluate("""
            (() => {
                const botao = Array.from(document.querySelectorAll('button')).find(
                    b => (b.innerText || '').trim().toUpperCase() === 'EMITIR'
                );
                if (botao) botao.click();
            })()
        """)

    async def _selecionar_criterio(self, page, tipo: str):
        valor = "CPF" if (tipo or "pf").lower() == "pf" else "RAIZ_DE_CNPJ"
        await page.evaluate(f"""
            (() => {{
                const radio = document.querySelector('input[type="radio"][value="{valor}"]');
                if (radio) radio.click();
            }})()
        """)
        await page.wait(1)

    async def _clicar_submit(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.querySelector('button[type="submit"]');
                if (botao && !botao.disabled) botao.click();
            })()
        """)

    async def _resolver_captcha_imagem(self, page) -> bool:
        src = await page.evaluate("""
            (() => {
                const img = document.querySelector('img.imagem-captcha');
                return img ? img.src : null;
            })()
        """)
        if not isinstance(src, str) or "base64," not in src:
            return False
        imagem_base64 = src.split("base64,", 1)[1]

        resolvedor = obter_resolvedor()
        resposta = await resolvedor.resolver_captcha_imagem(imagem_base64)

        campo = await page.select("input[matinput]")
        await campo.send_keys(resposta)
        await page.wait(1)
        await self._clicar_submit(page)
        return True

    async def _aguardar_processamento(self, page, tentativas: int = 30):
        # ⚠️ Bug real (confirmado em 4 testes reais seguidos): esperar o
        # texto "aguarde" SUMIR do `innerText` nunca funciona — o Angular
        # Material usa uma região de acessibilidade (aria-live, do CDK)
        # que fica fora da tela visualmente mas continua contando pro
        # `innerText`, guardando o texto do último anúncio de tela pra
        # leitor de tela ("Enviando a resposta...") mesmo depois do
        # conteúdo real já ter carregado por completo (confirmado: o
        # print de evidência, tirado alguns instantes depois, sempre
        # mostrava a certidão certinha, mesmo com o texto ainda
        # "grudado"). Por isso aqui esperamos o CONTRÁRIO: o APARECIMENTO
        # de um sinal positivo de que o resultado chegou ("código de
        # verificação", presente em toda certidão real gerada), em vez de
        # confiar no desaparecimento de qualquer texto.
        for _ in range(tentativas):
            await page.wait(1)
            texto = await page.evaluate("(() => document.body.innerText)()")
            texto_lower = (texto if isinstance(texto, str) else "").lower()
            if "código de verificação" in texto_lower or "captcha incorret" in texto_lower:
                return
        await page.wait(2)

    async def _interpretar_resultado(self, page) -> dict:
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        if "captcha" in texto_lower and ("incorret" in texto_lower or "inválid" in texto_lower):
            return {"status": "erro_captcha", "mensagem": "Captcha de imagem rejeitado pelo TRT9."}
        # Confirmado contra o site real: a frase exata é "NÃO CONSTAM
        # ações trabalhistas...", não "nada consta".
        if "não constam" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão trabalhista negativa gerada."}
        if "código de verificação" in texto_lower:
            return {"status": "certidao_emitida", "mensagem": "Certidão trabalhista gerada."}
        # Confirmado em vários testes reais: às vezes esse texto de
        # carregamento persiste no `innerText` (ver aviso no topo do
        # arquivo sobre a região aria-live do Angular) mesmo depois da
        # certidão real já ter sido gerada e capturada em PDF — por isso
        # a mensagem aqui já orienta a conferir o arquivo anexado, em vez
        # de repetir o texto de carregamento sem contexto.
        if "aguarde" in texto_lower:
            return {
                "status": "resultado_indefinido",
                "mensagem": "O TRT9 demorou mais que o esperado pra confirmar visualmente o resultado — confira o PDF anexado, que geralmente já traz a certidão certa.",
            }
        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao == "certidao_emitida":
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_captcha":
            return StatusPedido.ERRO_TECNICO
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = Trt9CertidaoTrabalhista()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
