"""
Consulta a API de management do RabbitMQ pra saber quantas mensagens estão
presas nas filas `<portal>.dlq` — sem isso, a única forma de saber que um
portal está falhando repetidamente é abrir o painel do RabbitMQ na mão.
Não há credenciais de Slack/e-mail configuradas nesse ambiente, então a
"alerta" aqui é visibilidade direta (endpoint + indicador no front), não
uma notificação empurrada.

Usa `urllib` (biblioteca padrão) em vez de `requests`/`httpx` de propósito,
pra não precisar adicionar mais uma dependência só por isso.
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from certidoes_core.config import config


def _management_base_url() -> str:
    partes = urllib.parse.urlparse(config.RABBITMQ_URL)
    host = partes.hostname or "localhost"
    return f"http://{host}:15672"


def _auth_header() -> str:
    partes = urllib.parse.urlparse(config.RABBITMQ_URL)
    usuario = partes.username or "guest"
    senha = partes.password or "guest"
    credenciais = base64.b64encode(f"{usuario}:{senha}".encode()).decode()
    return f"Basic {credenciais}"


def contar_mensagens_dlq(portal: str) -> int:
    """Número de mensagens na fila `<portal>.dlq`. Devolve 0 se a fila
    ainda não existe (portal nunca teve falha) ou se o RabbitMQ management
    não estiver acessível — não deve derrubar o endpoint principal por
    causa disso."""
    fila = f"{portal}.dlq"
    url = f"{_management_base_url()}/api/queues/%2F/{urllib.parse.quote(fila, safe='')}"
    requisicao = urllib.request.Request(url, headers={"Authorization": _auth_header()})
    try:
        with urllib.request.urlopen(requisicao, timeout=5) as resposta:
            dados = json.loads(resposta.read())
            return dados.get("messages", 0)
    except urllib.error.HTTPError as erro:
        if erro.code == 404:
            return 0
        return 0
    except Exception:
        return 0


def status_dlq_todos_portais(portais: list) -> dict:
    return {portal: contar_mensagens_dlq(portal) for portal in portais}
