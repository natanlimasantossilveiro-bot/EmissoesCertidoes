"""
Worker do portal Receita Federal (Certidão Conjunta PF/PJ).
A lógica de automação é praticamente a mesma do projeto original
(emissao_nodriver.py) — o que muda é: em vez de ser chamado direto por um
main.py ou por uma rota FastAPI, ele fica escutando a fila RabbitMQ
'receita_federal' e busca/atualiza o pedido no banco central.

Estrutura de classes (ver certidoes_core.automacao): esta classe só
implementa `preencher_e_emitir()` — abrir navegador, ciclo de
tentativa/retry, nomeação do arquivo final e a regra de "sempre capturar
evidência quando não for sucesso confirmado" já vêm de
AutomacaoNodriverBase, e são compartilhadas por qualquer outro portal que
também rode em cima de nodriver (ex: o próximo, CPF — Situação Cadastral).

Roda como processo isolado (container próprio). Pode ter N réplicas
rodando ao mesmo tempo, cada uma consumindo um pedido por vez.

⚠️ **Duas regressões reais encontradas numa passada de revalidação** (o
site mudou depois da primeira validação deste worker), corrigidas aqui:

1. A Receita Federal passou a mostrar um banner de cookies ("Aceitar")
   cobrindo a tela inicial — sem fechar isso primeiro, o clique em
   "PF"/"PJ" não registrava (o banner interceptava o clique), e o worker
   ficava preso na landing page. Resolvido com `_aceitar_cookies_se_existir()`.
2. Preencher os campos só com `.value = X` + `dispatchEvent('input')`
   parou de ser reconhecido pela validação do site (o campo mostra o
   valor certo na tela, mas a mensagem de erro acusa "não informada") —
   provavelmente o framework da página passou a rastrear o valor via um
   setter próprio. Resolvido usando o setter nativo do `HTMLInputElement`
   (bypassa o setter que o framework possa ter sobrescrito) e disparando
   input+change+blur.

⚠️ **Problema real, ainda em aberto** — a submissão retorna
consistentemente um erro genérico da Receita ("023 - tente novamente
dentro de alguns minutos"), em múltiplas tentativas, com CPFs diferentes,
mesmo rodando com tela (não-headless) via Xvfb dentro do container.

Comparado ao vivo contra um projeto irmão (`Certidoes_PF_PR/Certidao_Conjunta`,
mesma lógica de automação, mas rodando nativo no Windows, sem Docker):
rodando esse projeto irmão na mesma máquina/rede, com o MESMO CPF que
falhava aqui, a emissão funcionou de primeira — duas vezes seguidas,
minutos depois de duas falhas daqui. Isso descarta IP/rede e frequência
de tentativa como causa.

Já testado e descartado como causa isolada:
- Modo headless vs. com tela (Xvfb) — mesmo erro nos dois.
- Flag `--no-sandbox` do Chromium — testado rodando o container como
  usuário comum + `cap_add: SYS_ADMIN` (permitindo o sandbox real do
  Chrome funcionar dentro do Docker) — mesmo erro 023 mesmo assim.
- Chromium open-source (Debian) vs. Google Chrome de verdade — testado
  instalando o `google-chrome-stable` oficial dentro do container —
  mesmo erro 023 mesmo assim.

Ambos os testes acima foram revertidos (ver histórico do
worker.py/Dockerfile/docker-compose.yml) — nenhum resolveu, então não
valia manter a complexidade extra sem benefício.

O que sobra: o ambiente Linux/Docker em si (não o navegador específico)
parece ser o que essa proteção da Receita detecta — algo mais profundo
que flag de navegador não resolve. Único caminho ainda não testado:
rodar esse worker específico fora do Docker (o projeto irmão citado
acima roda assim, nativo no Windows, e funciona toda vez). Isso é uma
exceção arquitetural real — vale a pena confirmar primeiro se o mesmo
problema também acontece rodando Linux nativo (fora do Docker, ex: via
WSL2 direto) antes de decidir se a exceção precisa ser "roda fora do
Docker" ou especificamente "roda fora do Linux".
"""
import asyncio
import json

from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase


class CertidaoConjunta(AutomacaoNodriverBase):
    portal = "receita_federal"
    url_inicial = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home"

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        pdfs_antes = self._listar_pdfs_downloads()

        # Descoberto numa regressão (não existia quando este worker foi
        # validado pela primeira vez): a Receita Federal passou a mostrar
        # um banner de cookies ("Aceitar") cobrindo a tela inicial. Sem
        # fechar isso primeiro, o clique em "PF"/"PJ" logo abaixo não
        # registra (o banner intercepta o clique), e o worker acaba preso
        # na landing page, caindo em resultado_indefinido/sucesso_provável
        # sem nunca ter de fato tentado emitir nada.
        await self._aceitar_cookies_se_existir(page)

        await self._selecionar_tipo_certidao(page, pedido.tipo)
        if pedido.tipo == "pf":
            await self._preencher_dados_pf(page, pedido.documento, pedido.data_nascimento or "")
        else:
            await self._preencher_dados_pj(page, pedido.documento)

        await self._clicar_botao_emitir(page)
        resultado_bruto = await self._verificar_resultado_emissao(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])

        caminho_certidao = ""
        if status_final in (StatusPedido.SUCESSO_CONFIRMADO, StatusPedido.SUCESSO_PROVAVEL):
            caminho_certidao = await self.aguardar_e_mover_pdf(pedido, pdfs_antes)

        return ResultadoEmissao(
            status=status_final,
            mensagem=resultado_bruto["mensagem"],
            caminho_certidao=caminho_certidao,
        )

    async def _aceitar_cookies_se_existir(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => b.innerText.trim().toLowerCase() === 'aceitar');
                if (botao) botao.click();
            })()
        """)
        await page.wait(1)

    async def _selecionar_tipo_certidao(self, page, tipo):
        seletor = 'a[href="#/home/cpf"]' if tipo.lower() == "pf" else 'a[href="#/home/cnpj"]'
        await page.evaluate(f"""
            (() => {{
                const botao = document.querySelector('{seletor}');
                if (botao) botao.click();
            }})()
        """)
        await page.wait(3)

    # Descoberto numa regressão: preencher só com `.value = X` +
    # `dispatchEvent('input')` passou a não ser reconhecido pelo framework
    # da página (o campo mostra o valor certo na tela, mas a validação do
    # site acusa "não informada"). Usar o setter nativo do input (em vez do
    # setter que o framework pode ter sobrescrito) e disparar input/change/
    # blur cobre tanto React quanto Angular.
    _JS_DEFINIR_VALOR = """
        function definirValorCampo(campo, valor) {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(campo, valor);
            campo.dispatchEvent(new Event('input', { bubbles: true }));
            campo.dispatchEvent(new Event('change', { bubbles: true }));
            campo.dispatchEvent(new Event('blur', { bubbles: true }));
        }
    """

    async def _preencher_dados_pf(self, page, cpf, data_nascimento):
        await page.evaluate(f"""
            (() => {{
                {self._JS_DEFINIR_VALOR}
                const campoCpf = document.querySelector('input[name="niContribuinte"]');
                if (campoCpf) definirValorCampo(campoCpf, "{cpf}");
                const campoData = document.querySelector('input[name="dataNascimento"]');
                if (campoData) definirValorCampo(campoData, "{data_nascimento}");
            }})()
        """)
        await page.wait(2)

    async def _preencher_dados_pj(self, page, cnpj):
        await page.evaluate(f"""
            (() => {{
                {self._JS_DEFINIR_VALOR}
                const campo = document.querySelector('input[name="niContribuinte"]');
                if (campo) definirValorCampo(campo, "{cnpj}");
            }})()
        """)
        await page.wait(2)

    async def _clicar_botao_emitir(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button'));
                const botao = botoes.find(b => b.innerText.includes('Emitir Certidão'));
                if (botao) botao.click();
            })()
        """)
        await page.wait(5)

    async def _verificar_resultado_emissao(self, page):
        # page.evaluate() devolve objetos JS via CDP DeepSerializedValue (uma
        # lista de pares [chave, valor], não um dict) — por isso serializamos
        # pra JSON no lado do JS e desserializamos no lado do Python, evitando
        # depender do formato interno do protocolo.
        resultado_json = await page.evaluate("""
            (() => {
                const bodyText = document.body.innerText;
                let resultado;
                if (bodyText.includes('Certidão Válida Encontrada')) {
                    const botoes = Array.from(document.querySelectorAll('button'));
                    const botao = botoes.find(b => b.innerText.includes('Emitir Nova Certidão'));
                    if (botao) {
                        botao.click();
                        resultado = {status: 'emitindo_nova_certidao', mensagem: 'Certidão válida encontrada.'};
                    }
                }
                if (!resultado && bodyText.includes('Não foi possível concluir a ação')) {
                    resultado = {status: 'erro_receita', mensagem: 'A Receita Federal retornou erro.'};
                }
                if (!resultado && bodyText.includes('Certidão emitida')) {
                    resultado = {status: 'certidao_emitida', mensagem: 'Certidão emitida com sucesso.'};
                }
                if (!resultado) {
                    resultado = {status: 'resultado_indefinido', mensagem: 'Resultado não identificado.'};
                }
                return JSON.stringify(resultado);
            })()
        """)
        resultado = json.loads(resultado_json)
        await page.wait(2)
        return resultado

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao in ["emitindo_nova_certidao", "certidao_emitida"]:
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_receita":
            return StatusPedido.ERRO_PORTAL
        if status_emissao == "resultado_indefinido":
            return StatusPedido.SUCESSO_PROVAVEL
        return StatusPedido.FALHA_INDEFINIDA


if __name__ == "__main__":
    automacao = CertidaoConjunta()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))