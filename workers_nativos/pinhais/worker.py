"""
Worker do portal Certidão Negativa de Débitos (CND) — Prefeitura de
Pinhais, via plataforma Atende.Net. Reaproveita AutomacaoNodriverBase.

Primeiro worker desse projeto na plataforma Atende.Net — usada por várias
prefeituras (não só Pinhais). Existe um projeto de referência
(`SsaMonitorProcessos/robots/atendenet_v2/robot.py`) que já sabia lidar
com o padrão de iframe dessa plataforma, mas pra **consulta de processo**
(rastreamento), não emissão de certidão — o serviço usado aqui
("Certidão Negativa de Débitos") é outro, achado num reconhecimento novo
no catálogo de serviços do portal (`pinhais.atende.net/autoatendimento`,
seção "MAIS ACESSADOS").

Mecânica confirmada por inspeção ao vivo:

- Página do serviço: `.../autoatendimento/servicos/certidao-negativa-de-debitos`
  (só descrição) → botão "Acessar" → `.../detalhar/1`, que é onde o
  formulário de verdade vive, dentro de um iframe same-origin
  (`servicos/embed/data/<token>/...`) — mesmo padrão que o projeto de
  referência já documentava (`_JS_EMBED_DOC`).
- Campo "Opção de Emissão" (select) tem 4 formas de identificar o
  contribuinte/imóvel: Por CPF/CNPJ, Por Cadastro Imobiliário, Por
  Inscrição Imobiliária, Por Cadastro Econômico. Esse worker usa só
  **Por CPF/CNPJ** por enquanto (valor do option muda a cada carga de
  página — por isso selecionamos pelo texto da opção, não por value fixo).
- Selecionar essa opção revela o campo `input[name="cpfCnpj"]` e
  carrega (via AJAX) as opções do select
  `FinalidadeCertidaoDebito.codigo` — só apareceu 1 opção real
  ("CONTRIBUINTE - Emissão via Portal Autoatendimento"); selecionamos a
  primeira opção não-vazia, pra não depender do texto exato.
- **Sem captcha em nenhum momento** — nem no formulário, nem depois de
  clicar "Confirmar". Portal mais simples que já vimos nesse projeto.
- Botão de envio: dentro do iframe, texto "Confirmar".

⚠️ **Bug real encontrado testando com um CNPJ de contribuinte de verdade**
(fornecido pelo usuário): preencher `cpfCnpj` só com dígitos
("39360333000167") faz o sistema devolver "não possui cadastro único
ativo" mesmo pra um CNPJ que É contribuinte — confirmado comparando
contra o mesmo CNPJ digitado manualmente, formatado
("39.360.333/0001-67"), que funcionou. O campo espera o valor
**formatado** (com pontuação) — provavelmente validação client-side
baseada em máscara. Corrigido em `_preencher_documento`, que formata o
documento antes de preencher (`_formatar_cpf_cnpj`).

✅ **Validado de ponta a ponta** com CNPJ real de contribuinte de Pinhais
(depois da correção acima): certidão real capturada ("CERTIDÃO POSITIVA",
com número de controle, QR code de autenticidade — nome/CNPJ/endereço
batendo). O resultado (positiva ou negativa) não abre em aba nova nem
baixa nativamente — é renderizado num modal "Relatório" via um `<embed>`
com URL `blob:` (visualizador de PDF nativo do Chrome), então
`aguardar_e_mover_pdf`/`salvar_pagina_como_pdf` não servem aqui (não tem
arquivo baixado nem uma "foto de tela" faria sentido — é o PDF de
verdade, só que renderizado inline). Resolvido interceptando a resposta
de rede real (`Network.responseReceived` com `mime_type` contendo "pdf",
capturado ANTES do clique em "Confirmar") e pegando os bytes via
`Network.getResponseBody` — a primeira resposta PDF capturada é o
documento de verdade (a segunda, com URL `blob:`, é só a
re-serialização interna do visualizador, sem conteúdo útil).

⚠️ **Mesmo bloqueio de ambiente já visto na Receita Federal** (ver aviso
em `services/worker-receita-federal/worker.py`) — rodando esse fluxo
completo pelo container Docker (mesmo já com o bug do CNPJ formatado
corrigido), o portal retornou um alerta antifraude genérico ("A
validação automática de segurança (captcha) identificou uma atividade
incomum originada da sua rede... acesso restrito temporariamente",
código `EST-000549`). Rodando o mesmo fluxo nativo no Windows (fora do
Docker), na mesma rede: resultado limpo, certidão real capturada. Mesmo
padrão de ambiente Linux/Docker sendo detectado, agora confirmado em
dois portais diferentes. Despriorizado pelo mesmo motivo (sem máquina
Windows sempre ligada disponível) — o código abaixo já está correto e
validado nativo; só falta esse worker rodar num ambiente que a
prefeitura não marque como suspeito.
"""
import asyncio
import base64
import json
import re

import nodriver as nd
from certidoes_core.banco import PedidoCertidao, StatusPedido
from certidoes_core.fila import consumir_fila
from certidoes_core.automacao.base import ResultadoEmissao
from certidoes_core.automacao.nodriver_base import AutomacaoNodriverBase, PASTA_CERTIDOES_EMITIDAS

# Acha o documento do formulário dentro do iframe same-origin
# ("servicos/embed/data/...") — padrão da plataforma Atende.Net, já
# mapeado no projeto de referência (SsaMonitorProcessos/atendenet_v2).
_JS_EMBED_DOC = """
const __embedDoc = () => {
    for (const iframe of document.querySelectorAll('iframe')) {
        const src = iframe.src || '';
        if (!src.includes('embed/data')) continue;
        try {
            const doc = iframe.contentDocument;
            if (!doc) continue;
            const nested = doc.querySelectorAll('iframe[src*="embed/data"]');
            if (nested.length > 0 && nested[0].contentDocument)
                return [nested[0].contentDocument, nested[0].contentWindow];
            return [doc, iframe.contentWindow];
        } catch(e) { continue; }
    }
    return [null, null];
};
"""


class AtendeNetPinhaisCnd(AutomacaoNodriverBase):
    portal = "atendenet_pinhais_cnd"
    url_inicial = "https://pinhais.atende.net/autoatendimento/servicos/certidao-negativa-de-debitos/detalhar/1"
    espera_inicial_segundos = 5  # SPA da plataforma Atende.Net é mais pesada que os ASP.NET clássicos
    # Rodando nativo (fora de container, não como root) não precisa e não
    # deve usar --no-sandbox — mesmo ajuste do receita_federal/sefaz_pr
    # nativos (flag denuncia automação pro antifraude da plataforma).
    requer_no_sandbox = False

    async def preencher_e_emitir(self, page, pedido: PedidoCertidao) -> ResultadoEmissao:
        # Precisa ser registrado ANTES do clique em "Confirmar" — o
        # resultado não baixa nativamente nem abre aba nova, é
        # renderizado inline via blob: (ver aviso no topo do arquivo).
        # Só a captura de rede em tempo real pega o conteúdo de verdade.
        respostas_pdf = []

        def on_response(evt: nd.cdp.network.ResponseReceived):
            mime = (evt.response.mime_type or "").lower()
            if "pdf" in mime:
                respostas_pdf.append(evt.request_id)

        page.add_handler(nd.cdp.network.ResponseReceived, on_response)
        await page.send(nd.cdp.network.enable())

        await self._aceitar_cookies_se_existir(page)
        await self._aguardar_formulario(page)

        await self._selecionar_opcao_cpf_cnpj(page)
        await page.wait(2)
        await self._preencher_documento(page, pedido.documento)
        await self._selecionar_finalidade(page)
        await page.wait(1)
        await self._clicar_confirmar(page)
        await page.wait(6)

        if respostas_pdf:
            caminho_certidao = await self._salvar_pdf_capturado(page, respostas_pdf[0], pedido)
            if caminho_certidao:
                return ResultadoEmissao(
                    status=StatusPedido.SUCESSO_CONFIRMADO,
                    mensagem="Certidão emitida.",
                    caminho_certidao=caminho_certidao,
                )

        resultado_bruto = await self._interpretar_resultado(page)
        status_final = self._determinar_status_final(resultado_bruto["status"])
        return ResultadoEmissao(status=status_final, mensagem=resultado_bruto["mensagem"])

    async def _salvar_pdf_capturado(self, page, request_id, pedido: PedidoCertidao) -> str:
        try:
            corpo, eh_base64 = await page.send(nd.cdp.network.get_response_body(request_id))
        except Exception as erro:
            print(f"[{self.portal}] Falha ao capturar corpo da resposta PDF: {erro}")
            return ""
        dados = base64.b64decode(corpo) if eh_base64 else corpo.encode()
        if len(dados) < 1024:  # PDF de verdade nunca é tão pequeno — provável re-serialização vazia do blob
            return ""
        PASTA_CERTIDOES_EMITIDAS.mkdir(parents=True, exist_ok=True)
        destino = PASTA_CERTIDOES_EMITIDAS / self.nome_arquivo_certidao(pedido)
        destino.write_bytes(dados)
        return str(destino)

    async def _aceitar_cookies_se_existir(self, page):
        await page.evaluate("""
            (() => {
                const botoes = Array.from(document.querySelectorAll('button, a'));
                const botao = botoes.find(b => (b.innerText || '').trim().toLowerCase() === 'aceitar');
                if (botao) botao.click();
            })()
        """)
        await page.wait(1)

    async def _aguardar_formulario(self, page):
        for _ in range(10):
            quantidade = await page.evaluate(f"""
                (() => {{ {_JS_EMBED_DOC} const [doc] = __embedDoc();
                return doc ? doc.querySelectorAll('select[name="opcaoEmissao"]').length : 0; }})()
            """)
            if isinstance(quantidade, (int, float)) and quantidade > 0:
                return
            await page.wait(2)

    async def _selecionar_opcao_cpf_cnpj(self, page):
        # O `value` da option é um hash que muda a cada carga de página
        # (confirmado no reconhecimento) — seleciona pelo texto, não
        # pelo value fixo.
        await page.evaluate(f"""
            (() => {{
                {_JS_EMBED_DOC}
                const [doc] = __embedDoc();
                if (!doc) return;
                const sel = doc.querySelector('select[name="opcaoEmissao"]');
                if (!sel) return;
                const opcao = Array.from(sel.options).find(o => o.text.toLowerCase().includes('cpf'));
                if (!opcao) return;
                sel.value = opcao.value;
                sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})()
        """)

    @staticmethod
    def _formatar_cpf_cnpj(documento: str) -> str:
        # Confirmado contra o site real: o campo só aceita o valor
        # FORMATADO (com pontuação) — mandar só dígitos faz o sistema
        # devolver "não possui cadastro único" mesmo pra um documento
        # que é contribuinte de verdade. Ver aviso no topo do arquivo.
        digitos = re.sub(r"\D", "", documento or "")
        if len(digitos) == 11:  # CPF
            return f"{digitos[0:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:11]}"
        if len(digitos) == 14:  # CNPJ
            return f"{digitos[0:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:14]}"
        return documento  # formato inesperado — manda como veio, sem arriscar formatar errado

    async def _preencher_documento(self, page, documento: str):
        documento_formatado = self._formatar_cpf_cnpj(documento)
        documento_js = json.dumps(documento_formatado)
        await page.evaluate(f"""
            (() => {{
                {_JS_EMBED_DOC}
                const [doc] = __embedDoc();
                if (!doc) return;
                const campo = doc.querySelector('input[name="cpfCnpj"]');
                if (!campo) return;
                campo.value = {documento_js};
                campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
                campo.dispatchEvent(new Event('change', {{ bubbles: true }}));
                campo.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            }})()
        """)

    async def _selecionar_finalidade(self, page):
        # Só existia 1 opção real no reconhecimento ("CONTRIBUINTE -
        # Emissão via Portal Autoatendimento") — seleciona a primeira
        # não-vazia em vez de fixar o texto, pra não quebrar se a
        # prefeitura adicionar mais opções.
        await page.evaluate(f"""
            (() => {{
                {_JS_EMBED_DOC}
                const [doc] = __embedDoc();
                if (!doc) return;
                const sel = doc.querySelector('select[name="FinalidadeCertidaoDebito.codigo"]');
                if (!sel) return;
                const opcao = Array.from(sel.options).find(o => o.value);
                if (!opcao) return;
                sel.value = opcao.value;
                sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})()
        """)

    async def _clicar_confirmar(self, page):
        await page.evaluate(f"""
            (() => {{
                {_JS_EMBED_DOC}
                const [doc] = __embedDoc();
                if (!doc) return;
                const botoes = Array.from(doc.querySelectorAll('button, input[type="button"], input[type="submit"]'));
                const botao = botoes.find(b => (b.innerText || b.value || '').trim().toLowerCase().includes('confirmar'));
                if (botao) botao.click();
            }})()
        """)

    async def _interpretar_resultado(self, page) -> dict:
        texto_bruto = await page.evaluate(f"""
            (() => {{
                {_JS_EMBED_DOC}
                const [doc] = __embedDoc();
                return doc && doc.body ? doc.body.innerText : '';
            }})()
        """)
        # page.evaluate() pode devolver um objeto de erro do CDP em vez
        # de string, se rodar no meio de uma navegação — mesmo padrão
        # defensivo usado nos outros workers desse projeto.
        texto = texto_bruto.strip() if isinstance(texto_bruto, str) else ""
        texto_lower = texto.lower()

        # Confirmado contra o site real (rodando em Docker, com um CNPJ
        # que É contribuinte de verdade): alerta antifraude genérico da
        # plataforma, não específico desse serviço — ver aviso no topo
        # do arquivo sobre o bloqueio de ambiente Linux/Docker.
        if "atividade incomum" in texto_lower or "acesso foi restrito" in texto_lower:
            return {
                "status": "bloqueio_ambiente",
                "mensagem": "Portal recusou o acesso por suspeita antifraude (alerta EST-000549) — ambiente de execução provavelmente sinalizado, não é erro do documento.",
            }

        # Confirmado contra o site real (CNPJ que não é contribuinte de
        # Pinhais): mensagem exata do modal "Aviso", código WGT-000764.
        if "não possui cadastro único ativo" in texto_lower:
            return {
                "status": "nao_encontrado",
                "mensagem": "CPF/CNPJ não possui cadastro único ativo na prefeitura de Pinhais.",
            }

        if "cpf/cnpj" in texto_lower and "inválido" in texto_lower:
            return {"status": "erro_portal", "mensagem": "CPF/CNPJ rejeitado pelo portal como inválido."}

        return {"status": "resultado_indefinido", "mensagem": texto[:1000] or "Resultado não identificado."}

    @staticmethod
    def _determinar_status_final(status_emissao: str) -> StatusPedido:
        if status_emissao == "nao_encontrado":
            return StatusPedido.SUCESSO_CONFIRMADO
        if status_emissao == "erro_portal":
            return StatusPedido.ERRO_PORTAL
        if status_emissao == "bloqueio_ambiente":
            return StatusPedido.ERRO_TECNICO
        return StatusPedido.SUCESSO_PROVAVEL


if __name__ == "__main__":
    automacao = AtendeNetPinhaisCnd()
    asyncio.run(consumir_fila(automacao.portal, automacao.processar_pedido, prefetch=1))
