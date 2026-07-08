# Certidões Platform

Plataforma de emissão automatizada de certidões, com um worker independente
por portal (Receita Federal, prefeituras via Atende.Net, TJs, etc).

## Arquitetura

```
Front (front/index.html) ──▶ Gateway (FastAPI) ──▶ RabbitMQ ──▶ Worker do portal X
                              │                                    │
                              ▼                                    ▼
                    Banco central (MySQL)  ◀──── status/evidência ─┘
```

- **Gateway**: recebe pedidos (unitário ou planilha), grava no banco com
  status `pendente`, publica na fila do portal. Não processa nada — só roteia.
- **Fila (RabbitMQ)**: uma fila por portal. Se um worker cair ou travar,
  não afeta os outros. Falhas repetidas vão pra fila `<portal>.dlq` pra
  inspeção manual.
- **Workers**: um serviço por portal, cada um com seu Dockerfile e suas
  dependências específicas (ex: worker do Atende.Net vai precisar do
  2captcha, o da Receita Federal não). Escalam independente:
  `docker compose up --scale worker-receita-federal=3`.
- **Banco central**: única fonte de verdade sobre o status de cada pedido.
  Gateway e front consultam daqui, nunca perguntam direto pro worker.
- **`libs/certidoes_core`**: tudo que é comum entre os workers — conexão
  com fila, banco, captura/upload de evidência (screenshot), config via
  variáveis de ambiente.

## Rodando localmente

```bash
cp .env.example .env
docker compose up --build
```

- Gateway: http://localhost:8000/docs (Swagger automático do FastAPI)
- RabbitMQ management: http://localhost:15672 (guest/guest)
- Front: abra `front/index.html` direto no navegador (não precisa de
  servidor nem build — é uma página só, HTML+JS puro). Se o Gateway
  estiver em outro host/porta, ajuste no campo "Gateway" no topo da
  página. Se `GATEWAY_API_KEY` estiver configurada (ver abaixo), preencha
  também o campo "API Key" — sem ela, toda chamada volta 401. Cria
  pedidos, envia planilha, e acompanha o status em tempo real (atualiza
  sozinho a cada 5s, com contadores de total/andamento/sucesso/erro).
  Cada portal mostra um selo de confiabilidade (✅ validado / 🟡 parcial /
  🧪 sem status catalogado ainda) — mantido manualmente em
  `STATUS_PORTAL` no próprio HTML, a partir do que já foi validado em
  `docs/CATALOGO_PORTAIS.md`. Ainda é uma página única sem multiusuário
  de verdade (todo mundo usa a mesma chave) — o suficiente pra testes
  internos e demonstração, não pra produção no escritório.

### Autenticação do Gateway

O Gateway aceita um header `X-API-Key` em toda chamada. Configure
`GATEWAY_API_KEY` no `.env` (gere uma com `openssl rand -hex 24` ou
similar) — **se deixar em branco, a checagem é pulada** (só pra
facilitar dev local, nunca deixe assim se o Gateway ficar acessível além
da sua máquina). O front tem um campo "API Key" ao lado do "Gateway" pra
mandar esse header automaticamente.

## Testando o fluxo unitário

```bash
curl -X POST http://localhost:8000/pedidos \
  -F "portal=receita_federal" \
  -F "nome=Fulano de Tal" \
  -F "tipo=pf" \
  -F "documento=00000000000" \
  -F "data_nascimento=01/01/1990"
```

Isso retorna um `pedido_id`. Consulte o andamento com:

```bash
curl http://localhost:8000/pedidos/<pedido_id>
```

## Como adicionar um novo portal (ex: Prefeitura de Pinhais / Atende.Net)

Isso é o ponto principal do desenho: adicionar portal novo **não deve
tocar em nada que já existe**. Passo a passo:

1. Adicionar o portal em `PORTAIS_DISPONIVEIS` no `services/gateway/app/main.py`.
2. Criar `services/worker-atendenet-pinhais/` com:
   - `worker.py` (a lógica que já está em `robots/atendenet_v2/robot.py`
     no `SsaMonitorProcessos`, adaptada pro modelo `processar_pedido(pedido_id)`
     igual ao `worker-receita-federal/worker.py`)
   - `requirements.txt`, `Dockerfile` (nesse caso, vai precisar do Playwright
     ou nodriver + dependências do 2captcha)
3. Adicionar o serviço no `docker-compose.yml`, seguindo o mesmo padrão do
   `worker-receita-federal`.
4. Pronto — o Gateway já sabe rotear pedidos `portal=atendenet_pinhais`
   pra fila certa, sem precisar saber como aquele portal funciona por dentro.

## Pendências conhecidas / próximos passos

- [x] Front provisório pra teste (`front/index.html`) — página única sem
      build, cria pedidos/planilha e acompanha status em tempo real
- [x] Visual do front melhorado pra apresentação (header com marca,
      contadores de resumo, selo de confiabilidade por portal, nomes
      amigáveis na tabela em vez do slug interno) — testado num
      navegador real (Chromium via nodriver), não só revisado por
      leitura de código
- [ ] Front "de verdade", pensado pro uso do escritório (autenticação,
      multiusuário, produção — o atual ainda é só uma página estática
      sem servidor próprio)
- [x] Autenticação simples no Gateway (header `X-API-Key`, via
      `GATEWAY_API_KEY` no `.env` — vazio desativa a checagem, pra não
      travar dev local). Testado de ponta a ponta num navegador real:
      sem chave → 401, chave errada → 401, chave certa → 200. O caso mais
      arriscado (o botão "Baixar relatório", que usa um link puro sem
      controle de header) foi corrigido pra buscar via `fetch` + blob em
      vez de `<a href download>`, que não consegue mandar headers
      customizados
- [x] Endpoint de relatório consolidado por lote (`GET /lotes/{lote_id}/relatorio`,
      `.xlsx` com uma linha por pedido: nome, documento, tipo, status
      traduzido, mensagem, nomes dos arquivos de certidão/evidência,
      solicitante, datas). Pode ser baixado a qualquer momento, mesmo com
      pedidos ainda pendente/processando. Front ganhou um card "Baixar
      relatório" por lote enviado, persistido no localStorage. No caminho,
      corrigido um bug pré-existente no `POST /pedidos/planilha`
      (`DetachedInstanceError` ao acessar `lote.id` depois que a sessão já
      tinha expirado o objeto por causa dos commits do loop) — o endpoint
      de planilha nunca tinha sido testado de ponta a ponta antes disso
- [x] Visibilidade de DLQ: endpoint `GET /dlq/status` (consulta a API de
      management do RabbitMQ, sem dependência nova — usa `urllib` da
      biblioteca padrão) devolve a contagem de mensagens presas em cada
      fila `<portal>.dlq`. Front mostra um banner vermelho no topo quando
      qualquer portal tem mensagem parada, checando a cada 20s (mais
      espaçado que os 5s da tabela de pedidos, já que DLQ muda bem mais
      devagar). Não é alerta "empurrado" (sem Slack/e-mail configurado
      nesse ambiente) — é visibilidade direta no painel. Testado num
      navegador real com mensagens reais na DLQ (deixadas por falhas de
      teste anteriores nesta mesma sessão)
- [ ] Migrar o worker do Atende.Net a partir do `SsaMonitorProcessos`
- [x] **Passada de regressão** (`docker compose down` + `up --build` do
      zero, depois retestando os 5 portais validados um por um com
      captcha/dado real): encontradas e corrigidas 3 regressões reais
      causadas por mudanças nos próprios sites (não por código nosso
      quebrado):
      1. Receita Federal (Certidão Conjunta) passou a mostrar um banner
         de cookies que bloqueava o clique inicial — resolvido fechando o
         banner antes de prosseguir.
      2. O mesmo worker também parou de ter os campos preenchidos
         reconhecidos pela validação do site (`.value = X` deixou de
         bastar) — resolvido usando o setter nativo do `HTMLInputElement`
         mais eventos input/change/blur.
      3. Certidão de Cadastro de Imóvel e Consulta de Débitos (mesmo
         padrão de código) caíam ocasionalmente em `resultado_indefinido`
         mesmo com a mensagem certa na tela, porque `innerText` quebra a
         frase com `\n` no meio — resolvido normalizando espaços em
         branco antes de comparar.
      Confirmado que os 5 portais continuam funcionando depois das
      correções (CPF, TST-CNDT, Certidão de Cadastro de Imóvel e Consulta
      de Débitos com `sucesso_confirmado` real; Certidão Conjunta com o
      mecanismo comprovadamente corrigido, mas sem `sucesso_confirmado`
      nessa rodada por um bloqueio de anti-abuso aparentemente ligado ao
      CPF de teste reutilizado à exaustão nesta sessão, não ao código)
- [x] Worker de Consulta de Débitos/Dívida Ativa (Prefeitura de Curitiba)
      validado de ponta a ponta com captcha real e dado real — mesma
      plataforma/captcha do worker de Certidão de Cadastro de Imóvel
      (`worker-curitiba-debitos-divida-ativa`). É consulta informativa,
      não uma certidão formal, mas automatizada a pedido do usuário.
      Corrigidos dois bugs reais no caminho: (1) a mensagem "sem débito"
      real ("Não foram encontrados débitos...") não batia com o texto que
      eu tinha suposto ("nenhum débito"), fazendo a consulta ser
      classificada errado como "com débito"; (2) `page.evaluate()` às
      vezes devolve um objeto de erro do CDP em vez de string quando roda
      no meio de uma navegação, derrubando o worker com
      `AttributeError` — corrigido aqui e replicado defensivamente nos
      workers de CPF, CNPJ+QSA, TRF4, TST-CNDT e Certidão de Cadastro de
      Imóvel, que tinham o mesmo padrão frágil
- [x] Worker do CPF — Situação Cadastral validado de ponta a ponta contra
      o site real, com `TWOCAPTCHA_API_KEY` real: hCaptcha resolvido,
      comprovante emitido com sucesso ("Situação Cadastral: REGULAR") e
      PDF gerado corretamente (via `Page.printToPDF`, já que esse portal
      não dispara download nativo — só renderiza o comprovante como HTML)
- [ ] **Worker do CNPJ+QSA bloqueado**: a mecânica de preenchimento e
      submissão funciona (inclusive interceptação do callback do hCaptcha,
      necessária porque o Angular não reconhece a resposta só pela
      textarea), mas o backend rejeita o token do captcha mesmo assim.
      Hipótese de reCAPTCHA descartada (`ERROR_WRONG_GOOGLEKEY`). Suspeita
      principal: validação de IP entre quem resolve o captcha (2captcha) e
      quem submete (este worker) — precisaria de proxy, não só código. Ver
      aviso no topo de `services/worker-cnpj-qsa/worker.py`
- [x] Worker da CNDT (TST) validado de ponta a ponta contra o site real,
      com `TWOCAPTCHA_API_KEY` real: captcha de imagem simples (não
      reCAPTCHA/hCaptcha) resolvido, certidão emitida e PDF baixado com
      sucesso — sistema JSF/RichFaces antigo, sem proteção de borda
- [ ] **Worker do TRF4/JFPR (Certidão Cível/Criminal/Eleitoral) — submissão
      100% validada com captcha real** (5 emissões confirmadas contra o
      site real: nome/CPF batendo com a Receita Federal, certidão gerada
      no sistema do TRF4, sem erro). O último passo (baixar o PDF
      assinado) esbarra num **bug do próprio site do TRF4**: o botão
      "Visualizar Certidão Gerada" aponta pra um caminho quebrado —
      confirmado de 3 formas independentes (clique no navegador, `curl`
      isolado, navegação direta com a URL resolvida corretamente via
      `urljoin`), sempre 404/Bad Request. Não depende mais do nosso
      código — só falta o TRF4 corrigir o link deles. Ver aviso no topo de
      `services/worker-trf4-certidao/worker.py`
- [x] Worker da Certidão de Cadastro de Imóvel (Prefeitura de Curitiba)
      validado de ponta a ponta com captcha real e dado real (Indicação
      Fiscal fornecida pelo usuário a partir de uma declaração já emitida
      manualmente): captcha de imagem simples resolvido, PDF final baixado
      corresponde exatamente à declaração de referência. Esse portal
      estava marcado como "link suspeito" (token de sessão expirado) —
      bastou abrir direto na raiz do domínio pra funcionar normalmente.
      Duas mecânicas não óbvias documentadas no topo do worker: (1) a
      consulta bem-sucedida abre uma tela de confirmação, não o documento —
      precisa clicar "Imprimir Declaração"; (2) esse clique abre uma aba
      nova apontando pro PDF, mas o Chromium não baixa PDFs sozinho (abre
      no visualizador embutido) — foi preciso usar `page.download_file()`
      com `page.set_download_path()` explícito
- [ ] SEFAZ PR e Prefeitura de Curitiba (CND) ficaram **bloqueados** por
      proteção de borda pesada (reCAPTCHA Enterprise com pontuação de
      risco, e Akamai Bot Manager por IP de datacenter, respectivamente)
      — não é problema de código, precisaria de infraestrutura adicional
      (proxy residencial). Documentado em `docs/CATALOGO_PORTAIS.md`
- [ ] Ver `docs/CATALOGO_PORTAIS.md` para a lista completa de portais a
      automatizar, com status de reconhecimento e prioridade sugerida

## Arquitetura de classes dos workers (`certidoes_core.automacao`)

Todo worker baseado em navegador herda de `AutomacaoPortal` (contrato:
banco, tentativas, retry) → `AutomacaoNodriverBase` (ciclo de vida do
Chromium via nodriver + regra fixa de "sempre captura evidência quando o
resultado não for sucesso confirmado", inclusive `sucesso_provável`) →
classe concreta do portal, que só implementa `preencher_e_emitir()`. Ver
`services/worker-receita-federal/worker.py` e `services/worker-cpf/worker.py`
como exemplos. Nomeação de certidão/evidência é padronizada via
`certidoes_core.nomenclatura` (`NOME_PESSOA_portal_documento.pdf`).
