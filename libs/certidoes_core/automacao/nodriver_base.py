"""
Camada de plataforma pra portais automatizados via nodriver (Chromium).
Cuida do ciclo de vida do navegador e garante, pra QUALQUER portal que
herdar daqui, a regra: sempre que o resultado não for sucesso confirmado,
captura evidência (print) antes de fechar o navegador — mesmo que seja
erro de regra de negócio do próprio portal (ex: CNPJ que não é matriz),
não só erro técnico. Isso não depende de cada worker novo lembrar de
chamar capturar_evidencia() — a base já garante.
"""
import asyncio
import base64
import shutil
from abc import abstractmethod
from pathlib import Path

import nodriver as nd

from certidoes_core.config import config
from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.evidencia import capturar_evidencia
from certidoes_core.automacao.base import AutomacaoPortal, ResultadoEmissao

PASTA_CERTIDOES_EMITIDAS = Path("/data/certidoes_emitidas")

# Alguns apps SPA (Angular/React) só sabem que o hCaptcha foi resolvido
# através do callback que ELES MESMOS registraram na hora de chamar
# hcaptcha.render({sitekey, callback}) — só preencher a textarea de
# resposta não é suficiente (confirmado contra o site real do CNPJ+QSA).
# Esse script roda ANTES de qualquer JS da página (via
# Page.addScriptToEvaluateOnNewDocument) e intercepta a atribuição de
# window.hcaptcha pra capturar essa função assim que o app chamar
# .render(), guardando em window.__hcaptchaCallback. Depois de resolver
# via 2captcha, o worker chama window.__hcaptchaCallback(token) — isso
# aciona o mesmo caminho que o widget real acionaria.
HOOK_SCRIPT_HCAPTCHA_CALLBACK = """
(function() {
    let _real;
    try {
        Object.defineProperty(window, "hcaptcha", {
            configurable: true,
            get() { return _real; },
            set(value) {
                _real = value;
                try {
                    const originalRender = value.render.bind(value);
                    value.render = function(container, params) {
                        window.__hcaptchaCallback = params.callback;
                        return originalRender(container, params);
                    };
                } catch (e) {}
            }
        });
    } catch (e) {}
})();
"""

# Mesma ideia do hook de hCaptcha acima, mas pro reCAPTCHA Enterprise em
# modo render=explicit (confirmado no SEFAZ PR): o app registra um
# callback na hora de chamar grecaptcha.enterprise.render({sitekey,
# callback}), e só esse callback aciona o fluxo real de submissão — a
# resposta do 2captcha sozinha não basta. Intercepta a atribuição de
# window.grecaptcha (não window.grecaptcha.enterprise diretamente, porque
# o objeto inteiro é atribuído de uma vez só quando o script do Google
# carrega) pra capturar esse callback em window.__recaptchaEnterpriseCallback.
HOOK_SCRIPT_RECAPTCHA_ENTERPRISE_CALLBACK = """
(function() {
    let _real;
    try {
        Object.defineProperty(window, "grecaptcha", {
            configurable: true,
            get() { return _real; },
            set(value) {
                _real = value;
                try {
                    if (value.enterprise && value.enterprise.render) {
                        const originalRender = value.enterprise.render.bind(value.enterprise);
                        value.enterprise.render = function(container, params) {
                            window.__recaptchaEnterpriseCallback = params.callback;
                            return originalRender(container, params);
                        };
                    }
                } catch (e) {}
            }
        });
    } catch (e) {}
})();
"""


# Mesma ideia dos dois hooks acima, agora pro Cloudflare Turnstile
# (confirmado no MPF): a diretiva Angular do site (`turnstile-captcha`)
# chama `turnstile.render(element, {sitekey, callback})` diretamente — o
# app só sabe que o captcha foi resolvido através desse callback, não
# observando nenhum campo de formulário (o Turnstile nem usa uma textarea
# visível como o hCaptcha/reCAPTCHA clássico).
#
# ⚠️ Diferente dos dois hooks acima: interceptar a ATRIBUIÇÃO de
# `window.turnstile` via `Object.defineProperty` (a mesma técnica usada
# pro hCaptcha/reCAPTCHA Enterprise) NÃO funciona aqui — confirmado
# rodando contra o MPF de verdade (captcha resolvido e injetado, mas o
# Angular nunca reconheceu, com o erro "Por favor, marque o captcha
# antes de continuar."). Causa raiz encontrada lendo o bundle oficial do
# Cloudflare (`challenges.cloudflare.com/turnstile/v0/api.js`): o próprio
# script verifica `"turnstile" in window` pra decidir se já foi carregado
# antes (evitar dupla inicialização) — e `Object.defineProperty` já faz
# essa checagem virar `true` mesmo antes de qualquer valor real existir
# (só de existir o getter/setter, a propriedade "existe" pro operador
# `in`), enganando o Cloudflare pra achar que o Turnstile já tinha sido
# carregado e pulando o caminho normal de inicialização. Por isso aqui
# usamos uma abordagem diferente: não tocamos em `window.turnstile`
# antes do Cloudflare mesmo atribuir — só fazemos polling até o objeto
# aparecer sozinho, e SÓ ENTÃO sobrescrevemos o método `.render` nele já
# existente (mutação depois do fato, não interceptação da atribuição).
HOOK_SCRIPT_TURNSTILE_CALLBACK = """
(function() {
    let jaAplicado = false;
    function tentarAplicar() {
        if (jaAplicado) return;
        if (window.turnstile && typeof window.turnstile.render === "function") {
            jaAplicado = true;
            const originalRender = window.turnstile.render.bind(window.turnstile);
            window.turnstile.render = function(container, params) {
                window.__turnstileCallback = params.callback;
                return originalRender(container, params);
            };
        }
    }
    const intervalo = setInterval(function() {
        tentarAplicar();
        if (jaAplicado) clearInterval(intervalo);
    }, 20);
})();
"""


class AutomacaoNodriverBase(AutomacaoPortal):
    url_inicial: str
    browser_args_extra: list = []
    espera_inicial_segundos: int = 3  # portais mais pesados (ex: SPA Angular) podem sobrescrever
    usar_hook_hcaptcha_callback: bool = False  # ver HOOK_SCRIPT_HCAPTCHA_CALLBACK acima
    usar_hook_recaptcha_enterprise_callback: bool = False  # ver HOOK_SCRIPT_RECAPTCHA_ENTERPRISE_CALLBACK acima
    usar_hook_turnstile_callback: bool = False  # ver HOOK_SCRIPT_TURNSTILE_CALLBACK acima
    # `--no-sandbox` é uma flag que só automação/servidor usa — nenhum
    # usuário comum roda o Chrome assim, e é um sinal conhecido de
    # detecção antifraude (confirmado contra a Receita Federal: o mesmo
    # CPF que apanhava dentro do container funcionou de primeira rodando
    # nativo, sem essa flag). Container que roda como root PRECISA dela
    # (Chromium recusa abrir); um worker cujo Dockerfile roda como
    # usuário comum pode e deve setar isso como False.
    requer_no_sandbox: bool = True
    # None = usa o Chromium que o próprio nodriver baixa (padrão). Setar
    # pra um caminho real (ex: instalação normal do Chrome no Windows)
    # quando o fingerprint desse Chromium bundled estiver sendo detectado
    # como automação mesmo rodando nativo, com tela e sem --no-sandbox —
    # caso confirmado no SEFAZ PR: acesso manual no Chrome instalado da
    # máquina passou de primeira, o Chromium do nodriver não.
    browser_executable_path: str = None

    @abstractmethod
    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        """Implementado por cada portal concreto: navega, preenche
        formulário, interpreta o resultado. Não precisa se preocupar com
        evidência nem com abrir/fechar navegador — a base cuida disso."""

    async def executar(self, pedido: PedidoCertidao) -> ResultadoEmissao:
        browser_args = [
            f"--download-directory={config.BROWSER_DOWNLOAD_DIR}",
            "--disable-dev-shm-usage",
            *self.browser_args_extra,
        ]
        if self.requer_no_sandbox:
            browser_args.insert(1, "--no-sandbox")
        browser = await nd.start(
            headless=config.BROWSER_HEADLESS,
            browser_args=browser_args,
            browser_executable_path=self.browser_executable_path,
        )
        try:
            page = await browser.get("about:blank")
            # A flag `--download-directory` (acima) não é mais respeitada
            # pelo Chrome/Chromium atual — confirmado num teste real
            # (Curitiba Imóvel): o PDF baixava de verdade, só que ia parar
            # no Downloads padrão do perfil (`/root/Downloads` no
            # container), não na pasta que `aguardar_e_mover_pdf` observa,
            # fazendo o worker cair sempre no fallback de screenshot mesmo
            # com o download real tendo funcionado. `Browser.setDownloadBehavior`
            # via CDP é o mecanismo atual suportado — substitui a flag.
            await page.send(nd.cdp.browser.set_download_behavior(
                behavior="allow", download_path=str(config.BROWSER_DOWNLOAD_DIR)
            ))
            if (self.usar_hook_hcaptcha_callback or self.usar_hook_recaptcha_enterprise_callback
                    or self.usar_hook_turnstile_callback):
                # Page.enable é obrigatório aqui — sem isso,
                # addScriptToEvaluateOnNewDocument "sucede" (retorna um id)
                # mas não tem efeito nenhum na prática (confirmado testando).
                await page.send(nd.cdp.page.enable())
                if self.usar_hook_hcaptcha_callback:
                    await page.send(nd.cdp.page.add_script_to_evaluate_on_new_document(
                        source=HOOK_SCRIPT_HCAPTCHA_CALLBACK
                    ))
                if self.usar_hook_recaptcha_enterprise_callback:
                    await page.send(nd.cdp.page.add_script_to_evaluate_on_new_document(
                        source=HOOK_SCRIPT_RECAPTCHA_ENTERPRISE_CALLBACK
                    ))
                if self.usar_hook_turnstile_callback:
                    await page.send(nd.cdp.page.add_script_to_evaluate_on_new_document(
                        source=HOOK_SCRIPT_TURNSTILE_CALLBACK
                    ))

            await page.get(self.url_inicial)
            await page.wait(self.espera_inicial_segundos)

            resultado = await self.preencher_e_emitir(page, pedido)

            if resultado.status != StatusPedido.SUCESSO_CONFIRMADO and not resultado.url_evidencia:
                resultado.url_evidencia = await capturar_evidencia(
                    page, pedido.nome, pedido.documento, self.portal, motivo=resultado.status.value
                )

            return resultado
        finally:
            try:
                browser.stop()
            except Exception as erro:
                print(f"[{self.portal}] Aviso ao fechar navegador: {erro}")

    # ---------- helpers de download, comuns a qualquer portal via nodriver ----------

    def _listar_pdfs_downloads(self) -> set:
        # glob() é case-sensitive no Linux — um site que sirva o arquivo
        # com extensão maiúscula (".PDF", confirmado num teste real contra
        # o Guia Amarela de Curitiba) nunca aparecia aqui, fazendo o
        # arquivo baixado de verdade ficar esquecido em BROWSER_DOWNLOAD_DIR
        # e o worker cair sempre no fallback de print da página em vez de
        # mover o PDF real.
        return set(config.BROWSER_DOWNLOAD_DIR.glob("*.pdf")) | set(config.BROWSER_DOWNLOAD_DIR.glob("*.PDF"))

    @staticmethod
    async def digitar_devagar(elemento, texto: str, atraso_segundos: float = 0.12):
        """Alternativa ao `Element.send_keys()` do próprio nodriver pra
        campos com máscara de formatação em JS (ex: CPF ganhando pontos e
        traço enquanto digita). Confirmado num teste real (Curitiba CND):
        `send_keys()` manda os caracteres via CDP um atrás do outro sem
        nenhuma pausa, rápido demais pro script de máscara reformatar o
        campo entre uma tecla e outra — o valor final saía com os dígitos
        fora de ordem. Aqui simplesmente espaça cada tecla."""
        await elemento.apply("(elem) => elem.focus()")
        for caractere in texto:
            await elemento.tab.send(nd.cdp.input_.dispatch_key_event("char", text=caractere))
            await asyncio.sleep(atraso_segundos)

    async def aguardar_e_mover_pdf(self, pedido: PedidoCertidao, pdfs_antes: set, tentativas: int = 30) -> str:
        """Espera o navegador terminar de baixar um PDF novo e move pro
        destino final já com o nome padronizado (nome + portal + documento).
        Retorna "" se nenhum PDF novo aparecer dentro do prazo."""
        PASTA_CERTIDOES_EMITIDAS.mkdir(parents=True, exist_ok=True)
        destino = PASTA_CERTIDOES_EMITIDAS / self.nome_arquivo_certidao(pedido)

        for _ in range(tentativas):
            novos = self._listar_pdfs_downloads() - pdfs_antes
            if novos:
                arquivo_recente = max(novos, key=lambda p: p.stat().st_ctime)
                shutil.move(str(arquivo_recente), str(destino))
                return str(destino)
            await asyncio.sleep(1)
        return ""

    async def baixar_blob_url(self, page, blob_url: str) -> bytes | None:
        """Lê o conteúdo de uma URL `blob:` (PDF gerado em memória no
        próprio JS da página, comum em links com atributo `download`) via
        `fetch()` dentro da página, convertendo pra base64 com FileReader.
        Bypassa por completo o gerenciador de download do Chrome — usado
        quando um clique real (nativo, com user_gesture) no link ainda
        assim não resulta em nenhum arquivo na pasta observada por
        `aguardar_e_mover_pdf` (confirmado num teste real: Receita
        Federal, link com `href="blob:...", download="Certidao...pdf"`,
        clique nativo sem erro nenhum, mas nada aparece no disco)."""
        if not blob_url or not blob_url.startswith("blob:"):
            return None
        try:
            base64_dados = await page.evaluate(f"""
                (async () => {{
                    const resp = await fetch("{blob_url}");
                    const blob = await resp.blob();
                    return await new Promise((resolve) => {{
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result.split(',')[1]);
                        reader.readAsDataURL(blob);
                    }});
                }})()
            """, await_promise=True)
            if not base64_dados:
                return None
            return base64.b64decode(base64_dados)
        except Exception as erro:
            print(f"[{self.portal}] Falha ao ler blob URL: {erro}")
            return None

    async def salvar_bytes_como_pdf(self, pedido: PedidoCertidao, dados: bytes) -> str:
        """Mesmo destino/nomeação de `salvar_pagina_como_pdf`, mas a partir
        de bytes já obtidos (ex: via `baixar_blob_url`), sem precisar de
        `page.send(cdp.page.print_to_pdf(...))`."""
        PASTA_CERTIDOES_EMITIDAS.mkdir(parents=True, exist_ok=True)
        destino = PASTA_CERTIDOES_EMITIDAS / self.nome_arquivo_certidao(pedido)
        destino.write_bytes(dados)
        return str(destino)

    async def salvar_pagina_como_pdf(self, page, pedido: PedidoCertidao) -> str:
        """Alguns portais (ex: CPF) não disparam um download de PDF de
        verdade — só renderizam o comprovante como página HTML. Nesses
        casos, geramos o PDF a partir da própria página renderizada (via
        CDP Page.printToPDF) em vez de depender de um arquivo baixado."""
        PASTA_CERTIDOES_EMITIDAS.mkdir(parents=True, exist_ok=True)
        destino = PASTA_CERTIDOES_EMITIDAS / self.nome_arquivo_certidao(pedido)
        try:
            dados_b64, _ = await page.send(nd.cdp.page.print_to_pdf(print_background=True))
            destino.write_bytes(base64.b64decode(dados_b64))
            return str(destino)
        except Exception as erro:
            print(f"[{self.portal}] Falha ao gerar PDF da página: {erro}")
            return ""