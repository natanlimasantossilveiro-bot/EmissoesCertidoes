"""
Worker do portal Certidão Negativa — Ministério Público Federal. Reaproveita
AutomacaoNodriverBase.

Antes bloqueado por WAF genérico ("Web Page Blocked", Attack ID) rodando
do ambiente de datacenter/cloud usado nas primeiras varreduras —
retestado em 15/07/2026 a partir da rede real do escritório e abriu
limpo (ver `docs/CATALOGO_PORTAIS.md`). Validado manualmente pelo
usuário de ponta a ponta (CPF real, certidão "NADA CONSTA" emitida e
selo digital conferido).

Mecânica confirmada lendo o bundle JS servido pela SPA Angular (sem
gastar nada — `cidadao.module.js` → rota `certidao.**` →
`certidao/certidao.controller.js` + `certidao/certidao.html`):

- **3 chamadas REST**, todas GET, todas no controller
  (`certidao.controller.js`):
  1. `/ouvidoria/rest/v1/publico/certidao/consultar?tipoPessoa=F|J&documento=...`
     — só busca o nome/razão social. **Não usa captcha nenhum** (o
     controller nem envia o token nessa chamada).
  2. `/ouvidoria/rest/v1/publico/certidao/emitir?tipoPessoa=...&documento=...&recaptcha=<token>`
     — essa sim exige o token do Turnstile (`ctrl.recaptcha.response`).
     Devolve um hash em `response.data.data`.
  3. `/ouvidoria/rest/v1/publico/certidao/download/<hash>` — é pra onde
     aponta o link final (`ctrl.downloadLink`), renderizado como
     `<a href="..." download="certidaoMPF.pdf">`.

⚠️ **Bug real encontrado testando de ponta a ponta, 3 tentativas
seguidas com captcha de verdade (2captcha pago)** — nenhuma das duas
formas "normais" de capturar esse PDF funcionou:
1. Download nativo de arquivo (clicar o link e esperar em
   `config.BROWSER_DOWNLOAD_DIR`, via `aguardar_e_mover_pdf`) — nunca
   apareceu nenhum arquivo, mesmo aumentando o prazo de espera.
2. Interceptar a resposta via `Network.responseReceived` (mesma técnica
   que resolveu o Pinhais) — também não capturou nada; o clique num
   `<a download>` no Chromium headless via CDP não passa pelo pipeline
   normal de rede da aba (é tratado como download do navegador, num
   caminho que não gera esse evento pro Network domain da própria
   página) — diferente do caso do Pinhais, que era um blob renderizado
   por XHR/fetch de verdade, não um clique em link com atributo
   `download`.

Um teste caiu no fallback `salvar_pagina_como_pdf`, que gerava um PDF da
própria SPA (a tela com o botão "Download"), não a certidão de verdade
— **enganoso**, parecia sucesso mas o arquivo salvo não era o documento
correto; removido.

No navegador REAL do usuário (teste manual) o link baixa o PDF
normalmente — a diferença é específica de como o Chromium headless via
CDP trata cliques em `<a download>`, não do site.

**Resolvido evitando o clique inteiramente**: em vez de clicar o link,
lemos a URL final resolvida (`document.querySelector('#botaoDownload').href`)
e fazemos um `fetch()` **de dentro da própria página** (mesma origem,
cookies/sessão inclusos automaticamente), convertendo a resposta pra
base64 e devolvendo a string pro Python via `page.evaluate(...,
await_promise=True)` — um `fetch()` de verdade gera eventos de rede
normais (diferente do clique em `download`), então essa abordagem tem
mais chance de repetir o padrão que já funciona nos outros portais, sem
depender do subsistema de download de arquivo do navegador nem de
interceptar eventos que não chegam a acontecer nesse caso específico.
- Como o app é dirigido por eventos Angular (`ng-click`), preenchemos
  via clique real nos elementos do DOM (não chamamos os métodos do
  controller diretamente) — dispara o ciclo de digest do Angular
  normalmente, mesmo padrão defensivo já usado nos outros workers desse
  projeto pra reactive forms.
- **Captcha**: Cloudflare Turnstile, sitekey **fixo no HTML**
  (`0x4AAAAAACMhejJkLsBWVaMb`, no atributo `sitekey` da diretiva
  `turnstile-captcha`) — não precisa extrair de iframe como no
  CNPJ+QSA. A diretiva Angular chama `turnstile.render(el, {sitekey,
  callback})` — mesmo padrão de callback já resolvido pelo hook
  `usar_hook_hcaptcha_callback`/`usar_hook_recaptcha_enterprise_callback`,
  agora com `usar_hook_turnstile_callback` (novo, em
  `AutomacaoNodriverBase`).
- **Só 1 captcha resolvido, não 2** (apesar de parecer 2 passos "Consultar"
  → "Emitir" nas telas): o widget já fica visível assim que o formulário
  abre, ANTES de clicar Consultar — resolvemos ele logo no início e o
  mesmo token serve pra chamada de "emitir" depois (só ela usa o token
  de verdade; "consultar" não manda `recaptcha` na query). O reset do
  captcha que aparece na tela depois de "Emitir" é só limpeza da UI
  (`ctrl.resetCaptcha()`), não uma segunda exigência.
- Fluxo na tela: botão "Emitir Certidão" (topo) → radio Pessoa
  Física/Jurídica (`#tipoPf` ou o radio irmão com `value="J"`) → campo
  `#cpf`/`#cnpj` (tem diretiva de máscara Angular, `ui-br-cpf-mask` —
  preenchido via setter nativo + eventos, igual a outros formulários
  reativos desse projeto) → resolve o Turnstile → botão "Consultar"
  (`ng-click="ctrl.consultaNome(...)"`) → aparece o nome + botão "Emitir"
  (`ng-click="ctrl.geraCertidao()"`) → aparece o link "Download"
  (`#botaoDownload`) → clicar aciona a chamada de rede, capturada via
  CDP (ver aviso acima) em vez de esperar um arquivo baixado.
"""
import asyncio
import base64
import json

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.captcha import obter_resolvedor
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase, PASTA_CERTIDOES_EMITIDAS

SITEKEY_TURNSTILE_CERTIDAO = "0x4AAAAAACMhejJkLsBWVaMb"


class MpfCertidaoNegativa(AutomacaoNodriverBase):
    portal = "mpf_certidao_negativa"
    url_inicial = "https://aplicativos.mpf.mp.br/ouvidoria/app/cidadao/certidao"
    espera_inicial_segundos = 5  # SPA Angular, leva mais tempo pra montar do que páginas estáticas
    usar_hook_turnstile_callback = True
    # Mesmo ajuste aplicado no worker do FGTS: o WAF do MPF bloqueou o
    # primeiro teste real (mesma página "Web Page Blocked", Attack ID
    # 20000051, já visto no reconhecimento original) mesmo vindo de um IP
    # já confirmado limpo via `curl` — indício de que o Chromium headless
    # está sendo detectado pelo User-Agent, não pelo IP. Sobrescreve o UA
    # removendo "Headless" antes de concluir que é bloqueio de ambiente.
    browser_args_extra = [
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ]

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        await self._clicar_emitir_certidao(page)
        await page.wait(1)
        await self._selecionar_tipo_pessoa(page, pedido.tipo)
        await self._preencher_documento(page, pedido.tipo, pedido.documento)

        resolvedor = obter_resolvedor()
        token = await resolvedor.resolver_turnstile(SITEKEY_TURNSTILE_CERTIDAO, self.url_inicial)
        await self._acionar_callback_turnstile(page, token)
        await page.wait(1)

        await self._clicar_consultar(page)
        await page.wait(3)

        resultado_consulta = await self._interpretar_consulta(page)
        if resultado_consulta["status"] != "nome_encontrado":
            status_final = self._determinar_status_final(resultado_consulta["status"])
            return ResultadoEmissao(status=status_final, mensagem=resultado_consulta["mensagem"])

        await self._clicar_emitir(page)
        await page.wait(4)

        resultado_emissao = await self._interpretar_emissao(page)
        status_final = self._determinar_status_final(resultado_emissao["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self._baixar_certidao_via_fetch(page, pedido)
            # Confirmado no site que a certidão foi gerada não serve de
            # muito sem o arquivo em mãos — rebaixa pra sucesso_provável
            # (mesmo padrão já usado no worker de Cadastro de Imóvel de
            # Curitiba) pra isso ficar visível pra revisão humana, em vez
            # de aparecer como "sucesso" sem nenhum PDF anexado. Não usa
            # `salvar_pagina_como_pdf` como último recurso aqui de
            # propósito — já confirmado que isso captura a SPA, não a
            # certidão (ver aviso no topo do arquivo).
            if not caminho_certidao:
                status_final = StatusPedido.SUCESSO_PROVAVEL

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_emissao["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _clicar_emitir_certidao(self, page):
        await page.evaluate("""
            (() => {
                const botao = document.querySelector('#botaoEmitir');
                if (botao) botao.click();
            })()
        """)

    async def _selecionar_tipo_pessoa(self, page, tipo: str):
        valor = "F" if (tipo or "pf").lower() == "pf" else "J"
        await page.evaluate(f"""
            (() => {{
                const radio = document.querySelector('input[name="radioTipoPessoa"][value="{valor}"]');
                if (!radio) return;
                radio.click();
                radio.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})()
        """)

    _JS_DEFINIR_VALOR = """
        function definirValorCampo(campo, valor) {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(campo, valor);
            campo.dispatchEvent(new Event('input', { bubbles: true }));
            campo.dispatchEvent(new Event('change', { bubbles: true }));
            campo.dispatchEvent(new Event('blur', { bubbles: true }));
        }
    """

    async def _preencher_documento(self, page, tipo: str, documento: str):
        seletor = "#cpf" if (tipo or "pf").lower() == "pf" else "#cnpj"
        documento_js = json.dumps(documento)
        await page.evaluate(f"""
            (() => {{
                {self._JS_DEFINIR_VALOR}
                const campo = document.querySelector('{seletor}');
                if (campo) definirValorCampo(campo, {documento_js});
            }})()
        """)

    async def _acionar_callback_turnstile(self, page, token: str):
        token_js = json.dumps(token)
        await page.evaluate(f"""
            (() => {{
                if (typeof window.__turnstileCallback === 'function') {{
                    window.__turnstileCallback({token_js});
                }}
            }})()
        """)

    async def _clicar_consultar(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => (b.innerText || '').trim() === 'Consultar');
                if (botao) botao.click();
            })()
        """)

    async def _interpretar_consulta(self, page) -> dict:
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto = texto.strip() if isinstance(texto, str) else ""
        texto_lower = texto.lower()

        tem_botao_emitir = await page.evaluate("""
            (() => !!Array.from(document.querySelectorAll('button')).find(
                b => (b.innerText || '').trim() === 'Emitir'
            ))()
        """)
        if tem_botao_emitir:
            return {"status": "nome_encontrado", "mensagem": "Nome/razão social localizado."}
        if "falha na solicitação" in texto_lower or "inválido" in texto_lower:
            return {"status": "erro_portal", "mensagem": "O MPF recusou a consulta — confira o documento informado."}
        return {"status": "resultado_indefinido", "mensagem": "Não foi possível confirmar o nome/razão social."}

    async def _clicar_emitir(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => (b.innerText || '').trim() === 'Emitir');
                if (botao) botao.click();
            })()
        """)

    async def _interpretar_emissao(self, page) -> dict:
        tem_download = await page.evaluate("(() => !!document.querySelector('#botaoDownload'))()")
        if tem_download:
            return {"status": "certidao_emitida", "mensagem": "Certidão negativa do MPF gerada com sucesso."}
        texto = await page.evaluate("(() => document.body.innerText)()")
        texto = texto.strip() if isinstance(texto, str) else ""
        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado da emissão não identificado."}

    async def _baixar_certidao_via_fetch(self, page, pedido: PedidoCertidao) -> str:
        # Evita clicar no `<a download>` (ver aviso no topo do arquivo —
        # esse clique não passa pelo pipeline normal de rede/download no
        # Chromium headless via CDP). Em vez disso, busca o PDF via
        # `fetch()` de dentro da própria página (mesma origem, cookies
        # inclusos automaticamente) e devolve os bytes em base64.
        #
        # Bug real encontrado em uso de produção: pra CNPJ (pessoa
        # jurídica), esse fetch vinha falhando silenciosamente mesmo com
        # o site confirmando "certidão gerada com sucesso" e o botão
        # Download visível — suspeita de uma corrida (o `href` do link
        # ainda não estava totalmente resolvido no instante do fetch,
        # já que a consulta de Razão Social tende a ser mais lenta que a
        # de nome de PF). Adicionado espera antes da primeira tentativa e
        # retry com diagnóstico (motivo exato da falha registrado no
        # log), em vez de só devolver vazio sem explicação.
        await page.wait(2)
        for tentativa in range(1, 4):
            resultado_json = await page.evaluate("""
                (async () => {
                    const link = document.querySelector('#botaoDownload');
                    if (!link || !link.href) return JSON.stringify({erro: 'link_ausente'});
                    try {
                        const resposta = await fetch(link.href, { credentials: 'include' });
                        if (!resposta.ok) return JSON.stringify({erro: `http_${resposta.status}`});
                        const buffer = await resposta.arrayBuffer();
                        const bytes = new Uint8Array(buffer);
                        let binario = '';
                        for (let i = 0; i < bytes.length; i++) binario += String.fromCharCode(bytes[i]);
                        return JSON.stringify({dados: btoa(binario)});
                    } catch (erro) {
                        return JSON.stringify({erro: String(erro)});
                    }
                })()
            """, await_promise=True)

            resultado = json.loads(resultado_json) if isinstance(resultado_json, str) else {}
            base64_pdf = resultado.get("dados")
            if isinstance(base64_pdf, str) and base64_pdf:
                dados = base64.b64decode(base64_pdf)
                if len(dados) >= 1024:
                    PASTA_CERTIDOES_EMITIDAS.mkdir(parents=True, exist_ok=True)
                    destino = PASTA_CERTIDOES_EMITIDAS / self.nome_arquivo_certidao(pedido)
                    destino.write_bytes(dados)
                    return str(destino)

            print(f"[{self.portal}] Falha ao baixar certidão via fetch na tentativa {tentativa}/3: "
                  f"{resultado.get('erro', 'motivo desconhecido')}")
            await page.wait(2)

        return ""

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao == "certidao_emitida":
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = MpfCertidaoNegativa()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
