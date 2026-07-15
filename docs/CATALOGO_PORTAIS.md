# Catálogo de Portais — EmissoesCertidoes

Extraído de `PlanilhaLinksCertidoes.xlsx` (links estavam embutidos como botões
clicáveis no Excel, não em texto — foram extraídos via script analisando o
XML interno do arquivo). 28 sistemas únicos identificados a partir das 53
linhas da planilha (PF, PJ e um bloco de Imóvel/Curitiba), consolidados nas
26 linhas da tabela abaixo — dois pares foram agrupados numa linha só:
"Assertiva" + "Assertiva Crédito Mix" (mesma URL) e "Consulta Processual 1º
Grau" + "2º Grau Projudi TJPR" (nenhuma tinha link na planilha pra
diferenciar; separar de novo se o escritório confirmar que são destinos
distintos).

Legenda de status:
- ✅ **Automatizado** — worker já existe
- 🟢 **Simples (confirmado)** — validado manualmente, sem login, sem captcha
  complexo, resultado instantâneo
- 🔴 **Complexo (confirmado)** — login, certificado digital, ou fluxo
  assíncrono (pedido → espera → retorna depois)
- ⚪ **A validar** — não verificado ainda, precisa do checklist abaixo
- 🟡 **Construído, mas bloqueado** — worker existe e a mecânica de preenchimento
  funciona, mas algo impede a emissão de fato (ex: captcha rejeitado pelo
  backend) e precisa de investigação adicional (às vezes infraestrutura, não
  só código)
- ⚠️ **Link suspeito** — URL da planilha pode estar expirada (token de sessão embutido)
- ❌ **Eliminado da fila (decisão do escritório)** — descartado de propósito por ser
  específico/complicado demais pro esforço de automação (login, certificado
  digital, pagamento, ou dependência de terceiros) — não será automatizado
  a menos que o escritório reabra a discussão

## Fila de prioridade por dificuldade

Consolidado depois da varredura de reconhecimento em todos os ⚪
restantes. Da mais fácil (já pronta) pra mais difícil:

**Tier 0 — Automatizados e validados de ponta a ponta (✅)**
1. CPF — Situação Cadastral (Receita Federal) — hCaptcha resolvido de verdade
2. CNDT — Débitos Trabalhistas (TST) — captcha de imagem simples
3. Certidão de Cadastro de Imóvel (Curitiba) — captcha de imagem simples,
   confirmado com dado real fornecido pelo usuário
4. Consulta de Débitos/Dívida Ativa (Curitiba) — mesma plataforma/captcha
   do item 3, validado com dado real (sem débito e, numa tentativa
   anterior, com débito — as duas mensagens diferentes foram
   confirmadas). É consulta informativa, não uma certidão formal, mas
   automatizada a pedido do usuário
5. Situação de Regularidade do Empregador (FGTS — Caixa) — sem captcha
   nenhum (JSF/RichFaces clássico); desbloqueado no reteste de
   2026-07-15 (era WAF por reputação de IP de datacenter). Validado de
   ponta a ponta com CNPJ real (13.316.414/0001-76) — resultado
   "REGULAR" + PDF do CRF capturado. Ver `services/worker-fgts-caixa/worker.py`
6. Certidão Negativa (Ministério Público Federal) — Cloudflare
   Turnstile resolvido de verdade; desbloqueado no reteste de
   2026-07-15 (era WAF por reputação de IP de datacenter). Validado de
   ponta a ponta com CPF real (081.315.299-24) — certidão "NADA CONSTA"
   com selo digital conferido. Ver `services/worker-mpf-certidao/worker.py`
7. Certidão de Tributos Municipais — Pessoa Física / CND (Prefeitura de
   Curitiba) — captcha **Altcha** (prova computacional, resolve sozinho
   no navegador ao clicar o checkbox, sem gastar 2captcha nenhum);
   desbloqueado no reteste de 2026-07-15 (também era User-Agent, não
   bloqueio estrutural do Akamai como se pensava — ver nota mais abaixo).
   Validado de ponta a ponta com CPF real — certidão negativa de
   débitos tributários e dívida ativa municipal nº 13.308.881. Ver
   `services/worker-curitiba-cnd-cpf/worker.py`
8. Certidão Trabalhista (PJe — TRT9) — captcha de imagem simples via
   2captcha; desbloqueado no reteste de 2026-07-15 (também User-Agent,
   não CloudFront estrutural). Validado de ponta a ponta 4 vezes
   seguidas com CPF real — certidão eletrônica "NÃO CONSTAM" com código
   de verificação real a cada emissão. Ver
   `services/worker-trt9-certidao/worker.py`

**Tier 1 — Construído, falta só destravar (🟡)**
9. Certidão Conjunta (Receita Federal) — submissão funciona (preenche e
   envia certinho), mas esse serviço específico bloqueia consistentemente
   com erro genérico ("023 - tente novamente") **rodando em Linux**,
   com ou sem Docker — confirmado com um projeto irmão que roda a mesma
   lógica nativo no **Windows** e funciona toda vez. Testado e
   descartado: IP/rede, CPF específico, frequência, headless vs. com
   tela, `--no-sandbox`, Chromium vs. Chrome real, e **Docker vs. Linux
   puro (WSL2, sem container nenhum)** — nenhum resolveu, sempre o
   mesmo erro 023 em Linux. Ou seja, não é o Docker — é o Linux em si
   (ou algo correlacionado a ele) que esse serviço detecta. Implica que
   o VPS (Linux) provavelmente também vai esbarrar nisso. Único caminho
   comprovado: rodar esse worker específico num Windows de verdade,
   sempre ligado — **despriorizado por enquanto** (sem máquina
   disponível pra isso); emissão manual continua sendo o caminho pra
   esse portal específico. Ver aviso no topo de
   `services/worker-receita-federal/worker.py`
10. CNPJ+QSA (Receita Federal) — mecânica funciona, backend rejeita o
   token do captcha (suspeita de validação de IP)
11. Certidão Cível/Criminal JFPR (TRF4) — submissão 100% validada com
   captcha real (5 emissões confirmadas). Passo final bloqueado por um
   **bug do próprio site do TRF4** (link do botão "Visualizar Certidão
   Gerada" está quebrado, 404 confirmado de 3 formas diferentes) — não
   depende mais de nós, só do TRF4 corrigir
12. SEFAZ PR (Certidão de Débitos Tributários e Dívida Ativa) — landing
    page passou a carregar sem bloqueio de borda (mudança desde a
    varredura original), formulário construído (`worker-sefaz-pr`), mas
    bloqueado ainda mais cedo que o CNPJ+QSA: o **2captcha** devolveu
    `ERROR_CAPTCHA_UNSOLVABLE` nas 3 tentativas — reCAPTCHA Enterprise
    invisível parece ser difícil demais pro serviço de resolução atual.
    Não depende de código nosso; precisaria trocar de provedor de captcha
    pra ter alguma chance
13. Certidão Negativa de Débitos (Atende.Net — Prefeitura de Pinhais) —
   primeiro portal na plataforma Atende.Net. Achado num reconhecimento
   novo (serviço "Certidão Negativa de Débitos", separado da "Consulta
   de Processo Digital" que já tinha código de referência pronto).
   **Sem captcha em nenhum ponto do fluxo** — mais simples que os outros
   portais construídos até agora. Caminho de "CPF/CNPJ não é contribuinte
   de Pinhais" validado de ponta a ponta, inclusive rodando em Docker.
   Caminho de sucesso (contribuinte de verdade) ainda não validado —
   nenhum documento de teste disponível é contribuinte de Pinhais — e
   esbarrou no **mesmo bloqueio de ambiente Linux** já visto na
   Receita Federal (alerta antifraude genérico só rodando em container;
   fluxo idêntico nativo no Windows passa limpo). Mesma decisão: worker
   construído e documentado, despriorizado por falta de máquina Windows
   sempre ligada. Ver aviso no topo de
   `services/worker-atendenet-pinhais/worker.py`
14. Certidão de Tributos Municipais — Imóvel/IPTU (Prefeitura de
    Curitiba) — mesma plataforma/captcha Altcha do item 7 (CND/Pessoa
    Física), formulário construído (`worker-curitiba-certidao-tributos-imovel`),
    mas pede **dois dados que não existiam antes** num único pedido
    (Indicação Fiscal do imóvel + CPF/CNPJ do proprietário) — ainda não
    testado de ponta a ponta por falta de um dado real de imóvel de
    Curitiba disponível pra teste
15. Certidão Negativa de Feitos (Ministério Público do Trabalho) —
    formulário e preenchimento funcionam normalmente (desbloqueado do
    WAF no reteste de rede real), mas o captcha é **reCAPTCHA
    Enterprise** (confirmado — a página chegou a avisar "excedendo a
    cota gratuita do reCAPTCHA Enterprise"), e o **2captcha devolveu
    `ERROR_CAPTCHA_UNSOLVABLE`** ao tentar resolver — mesma limitação já
    documentada pro SEFAZ PR (item 12). Não depende de código nosso;
    precisaria de outro provedor de captcha pra ter alguma chance

**Tier 2 — vago.** Nenhum portal 🟢 "pronto pra construir agora" sobrou na
varredura atual.

**Tier 3 — Carregam sem bloqueio, mas precisam de mais um passo de
reconhecimento antes de classificar**
16. Guia Amarela (Curitiba) — inacessível na última tentativa
    (`ERR_CONNECTION_REFUSED`, do Chromium real e de `curl` puro) — pode ser
    instabilidade temporária, revalidar antes de descartar

**Tier 6 — vago.** Os três portais que restavam aqui (TRT9, MPT,
Prefeitura de Curitiba) foram todos **resolvidos em 2026-07-15**: o que
parecia bloqueio estrutural de WAF pesado (Akamai, CloudFront, WAF
genérico) era, nos três casos, só o Chromium headless se denunciando
pelo User-Agent — a mesma causa raiz já corrigida no FGTS/MPF. Ver nota
abaixo e os itens 7, 8 e 15 acima.

⚠️ **Nota importante**: esses 5 bloquearam vindos do mesmo ambiente/IP de
teste (datacenter/cloud) — revalidados novamente e continuam bloqueados
(confirmado com navegador real, não só `curl`). Isso pode ser
específico dessa rede de desenvolvimento — vale re-testar a partir da
rede real de produção do escritório antes de descartar de vez, já que
bloqueio por reputação de IP de datacenter nem sempre se repete numa
rede residencial/corporativa comum.

**Reteste feito em 2026-07-15, a partir da rede real do escritório**
(IP residencial/corporativo, Algar Telecom — não mais datacenter/cloud),
via `curl` direto contra cada domínio:

- **Ministério Público Federal** — **desbloqueou**. HTTP 200, retornou a
  SPA Angular real (`ng-app="cidadao.module"`), sem nenhuma assinatura de
  bloqueio. Confirma a suspeita: o bloqueio anterior era por reputação de
  IP do ambiente de desenvolvimento, não por detecção de automação.
  Status revisado para ⚪ **liberado, worker ainda não construído**.
- **FGTS (Caixa)** — **desbloqueou**. HTTP 200, retornou a página JSF
  real (`consultaEmpregador.jsf`), sem assinatura do ShieldSquare/Radware.
  Status revisado para ⚪ **liberado, worker ainda não construído**.
- **Prefeitura de Curitiba (CND + Imóvel/IPTU)** — **continua bloqueado**.
  Mesmo "Access Denied" do Akamai, mesmo saindo da rede real. Esse bloqueio
  não é por reputação de IP de datacenter — é mais estrutural (regra do
  Akamai pro domínio inteiro, ou geolocalização/ASN específicos).
- **PJe — TRT9** — **continua bloqueado**. Mesmo "403 Request blocked"
  do CloudFront.
- **Ministério Público do Trabalho** — **continua bloqueado**. Ainda 403,
  mas a página de erro mudou de assinatura (antes "Web Page Blocked" com
  Attack ID; agora um 403 Forbidden genérico) — pode ser reconfiguração
  do WAF deles, não necessariamente o mesmo produto/regra de antes.

Ou seja, nessa primeira rodada de reteste (só `curl`, sem browser real
ainda): **2 dos 5 desbloquearam só de sair do ambiente de datacenter**;
os outros 3 (Curitiba, TRT9, MPT) pareciam continuar bloqueados mesmo
na rede real — hipótese na época era bloqueio estrutural (regra do
WAF pro domínio inteiro).

**Atualização de 2026-07-15 (mesmo dia, rodada seguinte): essa hipótese
estava errada.** Testando com o **navegador real (Chromium headless com
o mesmo `--user-agent` corrigido do FGTS/MPF)**, não só `curl`, os três
que "continuavam bloqueados" **também abriram limpos** — Curitiba, TRT9
e MPT tinham exatamente o mesmo problema de User-Agent que o MPF/FGTS,
só que o teste anterior (via `curl`) não bastava pra provar isso, já
que o `curl` continuava sendo bloqueado por outros motivos (falta de
headers/comportamento de navegador de verdade) independente do
User-Agent. **Conclusão final: os 5 bloqueios "de borda pesada" que
pareciam estruturais eram, no fundo, todos o mesmo problema — nenhum
exigiu proxy residencial nem serviço de bypass.**

**MPF e FGTS construídos e validados de ponta a ponta logo em seguida**
(mesmo dia). Dois problemas técnicos reais apareceram só rodando de
verdade com dado real e captcha pago — nenhum dos dois era visível só
pelo reconhecimento via `curl`/leitura de código:

- **Bloqueio por User-Agent, não por IP**: o primeiro teste real de cada
  um caiu de novo na mesma página de bloqueio (WAF do MPF, ShieldSquare
  do FGTS) mesmo já confirmado limpo via `curl` — a página de bloqueio
  do FGTS chegou a citar literalmente `HeadlessChrome/149.0.0.0` no
  User-Agent capturado. Ou seja, o bloqueio nunca foi por IP de
  datacenter — é o próprio Chromium headless se denunciando via
  User-Agent. Resolvido sobrescrevendo o UA (`--user-agent=...`,
  removendo "Headless") nos dois workers.
- **Hook de captcha do MPF (Cloudflare Turnstile)**: a técnica de
  interceptar `window.turnstile` via `Object.defineProperty` (que já
  funciona pro hCaptcha e reCAPTCHA Enterprise noutros workers) não
  funcionou aqui — o próprio bundle do Cloudflare verifica
  `"turnstile" in window` pra decidir se já foi carregado antes, e
  `defineProperty` já faz essa checagem virar `true` antes da hora,
  enganando a inicialização normal. Resolvido com polling (espera o
  Cloudflare atribuir `window.turnstile` sozinho, só então sobrescreve
  `.render` nele) — ver `HOOK_SCRIPT_TURNSTILE_CALLBACK` em
  `libs/certidoes_core/automacao/nodriver_base.py`.
- **Download da certidão do MPF**: o link final tem atributo HTML
  `download`, mas clicar nele via Chromium headless (CDP) não produz
  nem um arquivo baixado nem um evento de rede capturável — diferente
  de um clique num navegador real (validado manualmente) e diferente
  do padrão do Pinhais (blob renderizado via XHR). Resolvido evitando o
  clique: um `fetch()` de dentro da própria página busca o PDF direto
  pela URL do link, com sessão/cookies inclusos, devolvendo os bytes em
  base64 pro Python.

Ver os avisos completos no topo de `services/worker-fgts-caixa/worker.py`
e `services/worker-mpf-certidao/worker.py`.

**Curitiba (CND), TRT9 e MPT construídos logo em seguida, mesmo dia**,
com o mesmo `--user-agent` corrigido:

- **Curitiba CND (Pessoa Física)** — ✅ validado de ponta a ponta com CPF
  real. Descoberta interessante: o captcha desse portal é **Altcha**
  (prova computacional/proof-of-work), não um dos tipos já vistos no
  projeto — resolve sozinho no navegador ao clicar o checkbox, sem
  gastar nenhum crédito de 2captcha. Bug real encontrado: preencher o
  campo de CPF com `Element.send_keys()` do nodriver (teclas via CDP
  sem pausa) saía com os dígitos fora de ordem, porque o campo tem uma
  máscara de formatação em JS que não acompanha teclas rápidas demais
  — corrigido com `digitar_devagar()` (novo, em `AutomacaoNodriverBase`),
  que espaça cada tecla. Ver `services/worker-curitiba-cnd-cpf/worker.py`.
- **TRT9 (Certidão Trabalhista)** — ✅ validado de ponta a ponta 4 vezes
  seguidas com CPF real, cada uma com um código de verificação real
  diferente. Usa captcha de imagem simples (2captcha). Bug real, ainda
  sem solução definitiva (cosmético, não afeta o PDF entregue): o
  Angular Material anuncia o texto de carregamento numa região de
  acessibilidade (`aria-live`) que nunca some do `innerText`, mesmo
  depois do resultado real já ter carregado — em algumas emissões isso
  faz o status sair como sucesso_provável (em vez de confirmado) com
  uma mensagem genérica, mas o arquivo da certidão sai correto de
  qualquer forma (conferido nas 4 tentativas). Ver
  `services/worker-trt9-certidao/worker.py`.
- **MPT (Certidão Negativa de Feitos)** — 🟡 construído, mas com taxa de
  sucesso baixa e inconsistente: o desbloqueio de rede funcionou
  (User-Agent corrigido), o formulário preenche certinho, mas o captcha
  do site é **reCAPTCHA Enterprise**. Dois bugs reais de código já
  corrigidos: (1) resolver como v2 clássico não bastava — corrigido
  usando `resolver_recaptcha_enterprise`; (2) o clique em "Consultar"
  aciona um `form.submit()` de verdade (POST + reload completo), e um
  teste real mostrou que 6s fixos de espera não eram suficientes pra
  essa navegação terminar — corrigido esperando de verdade a URL mudar
  (até 20s). Mesmo com os dois bugs corrigidos, o **2captcha** ainda
  falha a resolver esse captcha na maioria das tentativas
  (`ERROR_CAPTCHA_UNSOLVABLE`, confirmado 3x seguidas numa rodada de
  teste, DLQ) — mesma limitação já documentada pro SEFAZ PR. Numa
  tentativa anterior o 2captcha chegou a resolver de verdade (raro), o
  que confirma que o problema não é mais o nosso código, é a
  dificuldade desse captcha específico pro serviço de resolução atual.
  Ver `services/worker-mpt-certidao/worker.py`.
- **Curitiba Imóvel/Tributos** — 🟡 construído (mesma plataforma/captcha
  Altcha do CND), mas ainda não testado de ponta a ponta — falta um
  dado real de Indicação Fiscal + documento do proprietário de um
  imóvel de Curitiba pra validar. Ver
  `services/worker-curitiba-certidao-tributos-imovel/worker.py`.

## Eliminados da fila (decisão do escritório, 2026-07-03)

Descartados de propósito por serem específicos/complicados demais pro
esforço de automação — não serão automatizados a menos que o escritório
reabra a discussão:

- IBAMA (provável login)
- Distribuidor Justiça Estadual 1º, 2º, 3º e 4º (pagos/assíncronos, ou sem
  link confirmável)
- Consulta Processual 1º/2º Grau Projudi TJPR, Consulta Processual JFPR,
  Consulta Processual TRF4 (sem link na planilha)
- Ministério da Economia — e-processo (login GOV.BR)
- Pesquisa Protesto/CENPROT (login/certificado, entrega em até 60 dias)
- Assertiva / Assertiva Crédito Mix (plataforma paga com login)

## Catálogo completo

| # | Portal | Usado em | URL | Status |
|---|---|---|---|---|
| 1 | Receita Federal — Certidão Conjunta | PF/PJ | `servicos.receitafederal.gov.br` | 🟡 Construído e validado manualmente (fora do Docker), mas bloqueado rodando dentro do container — erro genérico da Receita só nesse ambiente. Despriorizado, emissão manual por enquanto. Ver `services/worker-receita-federal/worker.py` |
| 2 | Receita Federal — CPF (situação cadastral) | PF | `servicos.receita.fazenda.gov.br/servicos/cpf/consultasituacao/consultapublica.asp` | ✅ Automatizado e **validado de ponta a ponta** com captcha real (hCaptcha, não reCAPTCHA como o catálogo original supunha) — comprovante emitido e PDF gerado corretamente |
| 3 | Receita Federal — CNPJ+QSA (cnpjreva) | PJ | `solucoes.receita.fazenda.gov.br/servicos/cnpjreva/cnpjreva_solicitacao.asp` | 🟡 Construído, mas **bloqueado** — o backend rejeita o token do hCaptcha mesmo com a submissão funcionando corretamente (hipótese de reCAPTCHA descartada). Provável causa: validação de IP entre quem resolve o captcha e quem submete, exigindo proxy. Ver aviso no topo de `services/worker-cnpj-qsa/worker.py` |
| 4 | Pesquisa Protesto (CENPROT) | PF/PJ | `pesquisaprotesto.com.br` | ❌ **Eliminado da fila** (decisão do escritório) — login/certificado digital, entrega assíncrona (até 60 dias) |
| 5 | SEFAZ PR | PF/PJ | Link da planilha morto (404) — atual: `cdwfazenda.paas.pr.gov.br/cdwportal/certidao/automatica` | 🟡 **Reconhecimento atualizado**: a landing page passou a carregar sem bloqueio de borda (antes rejeitava a sessão automatizada de cara). Worker construído (`worker-sefaz-pr`, reCAPTCHA Enterprise com hook de callback), mas testado com captcha real e bloqueado num ponto anterior ao do CNPJ+QSA: o próprio **2captcha** devolveu `ERROR_CAPTCHA_UNSOLVABLE` (não conseguiu nem gerar um token pra tentar). Não depende de código nosso — precisaria de outro provedor de captcha |
| 6 | Prefeitura de Curitiba (CND) | PF/PJ | `cnd-cidadao.curitiba.pr.gov.br/Certidao/SolicitarCpf` | ✅ Automatizado e **validado de ponta a ponta** com CPF real — certidão negativa de débitos tributários e dívida ativa municipal nº 13.308.881. O bloqueio Akamai era User-Agent do Chromium headless, não IP — resolvido com `--user-agent` corrigido. Captcha Altcha (prova computacional, sem custo). Ver `services/worker-curitiba-cnd-cpf/worker.py` |
| 7 | Distribuidor Justiça Estadual 1º | PF/PJ | `1distribuidorcuritiba.com.br/default/` | ❌ **Eliminado da fila** (decisão do escritório) — serviço pago e assíncrono (mesmo operador do item 8) |
| 8 | Distribuidor Justiça Estadual 2º | PF/PJ | `2distribuidorcuritiba.com.br/default/` | ❌ **Eliminado da fila** (decisão do escritório) — serviço pago e assíncrono (o próprio site avisa que a elaboração só ocorre no dia seguinte à confirmação do pagamento bancário), fluxo multi-etapa |
| 9 | Distribuidor Justiça Estadual 3º | PF/PJ | `3distrib.com.br` | ❌ **Eliminado da fila** (decisão do escritório) — serviço pago, entrega em até 24h após pagamento |
| 10 | Distribuidor Justiça Estadual 4º | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 11 | Consulta Processual 1º/2º Grau Projudi TJPR | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 12 | Certidão Cível/Criminal JFPR (TRF4) | PF/PJ | `www2.trf4.jus.br/trf4/processos/certidao/index.php` | 🟡 **Submissão 100% validada** com captcha real (5 emissões confirmadas — nome/CPF batendo com a Receita Federal, certidão gerada no sistema do TRF4, sem erro). O último passo (baixar o PDF assinado) esbarra num **bug do próprio site do TRF4**: o botão "Visualizar Certidão Gerada" aponta pra um caminho quebrado — testado de 3 formas independentes (clique no navegador, `curl` isolado, navegação direta com URL resolvida corretamente via `urljoin`), sempre 404/Bad Request. Não é limitação do nosso código; só falta o TRF4 corrigir o link deles. Ver aviso no topo de `services/worker-trf4-certidao/worker.py` |
| 13 | Consulta Processual JFPR | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 14 | Consulta Processual TRF4 | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 15 | Débitos Trabalhistas (TST) | PF/PJ | `tst.jus.br/certidao` (formulário real: `cndt-certidao.tst.jus.br`) | ✅ Automatizado e **validado de ponta a ponta** com captcha real e certidão real conferida (nome/CPF/número da certidão batendo) — captcha de imagem simples (não reCAPTCHA/hCaptcha), sistema JSF/RichFaces antigo. ⚠️ Já existiu um bug real aqui: a tela de "sucesso" não é a certidão, e o download automático (disparado pelo próprio site) só é salvo se `set_download_path` for chamado ANTES do clique em emitir — corrigido, ver aviso no topo de `services/worker-tst-cndt/worker.py` |
| 16 | Ações Trabalhistas (PJe TRT9) | PF/PJ | `pje.trt9.jus.br/certidoes/inicio` | ✅ Automatizado e **validado de ponta a ponta** 4 vezes com CPF real — certidão eletrônica de ações trabalhistas "NÃO CONSTAM", código de verificação real a cada emissão. O bloqueio CloudFront era User-Agent, não IP — resolvido com `--user-agent` corrigido. Captcha de imagem simples via 2captcha. Ver `services/worker-trt9-certidao/worker.py` |
| 17 | IBAMA | PF/PJ | `servicos.ibama.gov.br/sicafiext/` | ❌ **Eliminado da fila** (decisão do escritório) — landing page é 100% casca (SPA), provável login |
| 18 | Ministério Público Federal | PF/PJ | `aplicativos.mpf.mp.br/ouvidoria/app/cidadao/certidao` | ✅ Automatizado e **validado de ponta a ponta** com captcha real (Cloudflare Turnstile) — certidão "NADA CONSTA" com selo digital conferido. Desbloqueou no reteste de 2026-07-15 (antes WAF por reputação de IP de datacenter). Ver `services/worker-mpf-certidao/worker.py` |
| 19 | Ministério Público do Trabalho | PF/PJ | `prt9.mpt.mp.br/servicos/certidao-positiva-negativa` | 🟡 Construído (`worker-mpt-certidao`), formulário e navegação corrigidos (2 bugs reais resolvidos), mas o **2captcha** falha a maioria das vezes em resolver o reCAPTCHA Enterprise (`ERROR_CAPTCHA_UNSOLVABLE`) — mesma limitação do SEFAZ PR. Já funcionou pelo menos 1 vez, mas não é confiável o bastante pra uso normal |
| 20 | Ministério da Economia (e-processo) | PF/PJ | `eprocesso.sit.trabalho.gov.br/Certidao/Emitir` | ❌ **Eliminado da fila** (decisão do escritório) — exige login via GOV.BR (SSO) |
| 21 | FGTS (Caixa) | PJ | `consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf` | ✅ Automatizado e **validado de ponta a ponta** com CNPJ real — resultado "REGULAR" no FGTS com PDF do CRF capturado. Sem captcha nenhum. Desbloqueou no reteste de 2026-07-15 (antes WAF por reputação de IP de datacenter). Ver `services/worker-fgts-caixa/worker.py` |
| 22 | Assertiva / Assertiva Crédito Mix | PF/PJ | `painel.assertivasolucoes.com.br/login` | ❌ **Eliminado da fila** (decisão do escritório) — plataforma paga com login |
| 23 | Guia Amarela (Curitiba) | Imóvel | `www5.curitiba.pr.gov.br/gtm/gam/Default.aspx` | ⚪ **Inacessível na tentativa mais recente** (`ERR_CONNECTION_REFUSED`, tanto do Chromium real quanto de `curl` direto do host — não é bloqueio de WAF, o servidor não respondeu) — pode ser instabilidade temporária do site; revalidar depois. Ainda não confirmado se emite certidão de verdade ou é só consulta informativa de zoneamento |
| 24 | Certidão de Débitos do Imóvel/IPTU | Imóvel | `cnd-cidadao.curitiba.pr.gov.br/Certidao/Solicitar` | 🟡 Construído (`worker-curitiba-certidao-tributos-imovel`), mesma plataforma/captcha Altcha do item 6 — mesmo bloqueio Akamai já resolvido (era User-Agent). Ainda não testado de ponta a ponta: falta um dado real de Indicação Fiscal + documento do proprietário de um imóvel de Curitiba |
| 25 | Certidão de Cadastro (Curitiba) | Imóvel | `declaracaounificadaimovel.curitiba.pr.gov.br/` (link antigo da planilha, com token de sessão, estava morto — abrir direto na raiz funciona) | ✅ Automatizado e **validado de ponta a ponta** com captcha real e dado real (Indicação Fiscal fornecida pelo usuário) — captcha de imagem simples, PDF final baixado corresponde exatamente à declaração de referência (mesmo endereço/bairro/histórico). Ver `services/worker-curitiba-cadastro-imovel/worker.py` |
| 26 | Consulta de Débitos (parcelamento) | Imóvel | `parcelamentoexecutado.curitiba.pr.gov.br/` (idem — abrir na raiz gera sessão nova automaticamente) | ✅ Automatizado e **validado de ponta a ponta** com captcha real e dado real — mesma plataforma/captcha do item 25 (`worker-curitiba-debitos-divida-ativa`). É consulta informativa de débitos em dívida ativa, não uma certidão formal, mas automatizada a pedido do usuário. Confirmadas as duas mensagens de resultado (com e sem débito) |
| 27 | Certidão Negativa de Débitos (Prefeitura de Pinhais) | PF/PJ | `pinhais.atende.net/autoatendimento/servicos/certidao-negativa-de-debitos` | 🟡 Construído (`worker-atendenet-pinhais`), sem captcha em todo o fluxo. Caminho de "CPF/CNPJ não é contribuinte" validado de ponta a ponta, inclusive em Docker. Caminho de sucesso não confirmado (sem documento de teste que seja contribuinte de Pinhais) e esbarrou no mesmo bloqueio de ambiente Linux/Docker já visto no item 1 — despriorizado pelo mesmo motivo |

## Checklist de reconhecimento (2 min por portal)

Pra transformar um ⚪ em 🟢 ou 🔴, abra o portal manualmente e responda:

1. **Login/cadastro necessário?** (sim = 🔴, aumenta bastante a complexidade)
2. **Tem captcha? Qual tipo?** (checkbox simples = fácil; reCAPTCHA v2/v3 com
   imagens ou hCaptcha = precisa do módulo `certidoes_core.captcha`)
3. **Resultado é instantâneo ou assíncrono?** (assíncrono = precisa de
   worker com reconsulta periódica, não só "abrir → preencher → emitir")
4. **Quantos campos o formulário pede?** (só documento = mais simples;
   pede endereço, inscrição municipal, etc. = mais complexo)
5. **O link da planilha ainda funciona, sem erro de sessão expirada?**

Preenchendo isso pros 18 portais em ⚪, dá pra montar a fila de prioridade
real (mais simples primeiro), sem eu ficar advinhando.
