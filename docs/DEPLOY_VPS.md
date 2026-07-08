# Deploy em VPS (Hostinger) — passo a passo

Guia pra colocar a plataforma no ar num servidor real, acessível pelos
colaboradores do escritório. Usa HTTPS gratuito via **sslip.io** (não
precisa comprar domínio pra começar — troque pelo domínio de verdade
depois, se comprarem um, é só repetir o passo do certbot com o novo nome).

## 1. Contratar o VPS

Plano recomendado: **KVM 2** (8GB RAM / 2 vCPU / 100GB NVMe) — dá folga
confortável pra MySQL + RabbitMQ + Gateway + 8 workers ociosos + picos de
Chromium sob demanda. O KVM 1 (4GB) é arriscado demais pra rodar tudo
isso junto com o sistema operacional.

Ao criar o VPS na Hostinger, escolha **Ubuntu 24.04 LTS** como sistema
operacional (ou a versão LTS mais recente disponível).

Anote o **IP público** do servidor — vai ser usado no lugar de um
domínio (ex: `123.45.67.89`).

## 2. Primeiro acesso e preparação do servidor

```bash
ssh root@<IP_DO_SERVIDOR>

apt update && apt upgrade -y

# Instala o Docker (script oficial)
curl -fsSL https://get.docker.com | sh

# Firewall básico — só SSH, HTTP e HTTPS liberados de fora
ufw allow 22
ufw allow 80
ufw allow 443
ufw --force enable
```

## 3. Clonar o repositório e configurar o `.env`

```bash
git clone https://github.com/natanlimasantossilveiro-bot/EmissoesCertidoes.git
cd EmissoesCertidoes
cp .env.example .env
nano .env
```

Preencha no `.env`:
- `JWT_SECRET_KEY` — gere com `openssl rand -hex 32`
- `ADMIN_EMAIL` / `ADMIN_SENHA_INICIAL` — credenciais do primeiro admin
  (troque a senha pelo próprio painel assim que logar a primeira vez,
  tem um link "Trocar senha" no topo)
- `TWOCAPTCHA_API_KEY` — a chave real do 2captcha

## 4. Descobrir o domínio temporário (sslip.io)

Não precisa configurar nada — `sslip.io` resolve `<qualquer-coisa>.<IP
com pontos trocados por hifens>.sslip.io` de volta pro próprio IP.
Exemplo: se o IP é `123.45.67.89`, o domínio é:

```
123-45-67-89.sslip.io
```

Anote esse domínio — vai substituir `<DOMINIO>` nos passos abaixo.

## 5. Subir tudo pela primeira vez (sem HTTPS ainda)

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Isso já sobe Gateway, workers, MySQL, RabbitMQ e o Nginx (só HTTP,
porta 80, servindo o `nginx/nginx.conf` original). Confirme que
está no ar:

```bash
curl http://123-45-67-89.sslip.io/portais
```

(deve responder `401` — é o comportamento esperado, só confirma que o
Nginx e o Gateway estão se falando).

## 6. Emitir o certificado HTTPS (Certbot)

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm certbot \
  certonly --webroot -w /var/www/certbot \
  -d 123-45-67-89.sslip.io \
  --email seu-email@escritorio.com.br --agree-tos --no-eff-email
```

## 7. Ativar o HTTPS no Nginx

```bash
cp nginx/nginx.ssl.conf.example nginx/nginx.conf
sed -i 's/<DOMINIO>/123-45-67-89.sslip.io/g' nginx/nginx.conf
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart nginx
```

Acesse `https://123-45-67-89.sslip.io` — deve mostrar a tela de login.

## 8. Renovação automática do certificado

Certificados Let's Encrypt expiram em 90 dias. Adicione um cron:

```bash
crontab -e
```

Adicione a linha (renova e recarrega o Nginx todo domingo às 3h):

```
0 3 * * 0 cd /root/EmissoesCertidoes && docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm certbot renew && docker compose -f docker-compose.yml -f docker-compose.prod.yml restart nginx
```

## 9. Primeiro acesso e criação dos colaboradores

1. Acesse `https://<seu-dominio-sslip>`, logue com `ADMIN_EMAIL` +
   `ADMIN_SENHA_INICIAL`.
2. Clique em **"Trocar senha"** (topo da página) e troque a senha do
   admin — a senha inicial passou pelo `.env` e por este processo de
   configuração, é boa prática trocar assim que possível.
3. Vá em **"Painel de admin"** e crie uma conta pra cada colaborador.

## 10. Deixar o front apontando pro servidor certo

Da primeira vez que qualquer pessoa acessar, o campo "Gateway em outro
endereço?" (dentro do "details" da tela de login) deve ficar **em
branco** — como o Nginx serve o front e faz proxy da API no mesmo
domínio, não precisa apontar pra lugar nenhum (fica tudo em mesma
origem). Só usar esse campo se for testar contra outro Gateway.

## Notas de segurança já aplicadas

- MySQL e RabbitMQ só ficam acessíveis via `127.0.0.1` (nem do próprio
  Nginx, que fala com eles pela rede interna do Docker) — não são
  expostos à internet.
- Todos os serviços têm `restart: unless-stopped` — voltam sozinhos se o
  servidor reiniciar ou algum container cair.
- RabbitMQ agora tem volume persistente — filas e DLQ sobrevivem a
  reinícios do container.
- `.env` nunca é commitado (já está no `.gitignore`).

## Quando comprarem um domínio de verdade

Repita os passos 6 e 7 usando o domínio novo em vez do `sslip.io`
(lembre de apontar o DNS do domínio pro IP do servidor antes).
