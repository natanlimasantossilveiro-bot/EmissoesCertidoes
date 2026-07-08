"""
Fila de mensagens (RabbitMQ). Publicação (Gateway) usa uma conexão pika
síncrona e de vida curta: abre, publica, fecha — sem risco de heartbeat.

Consumo (workers) usa aio-pika. O processamento de um pedido envolve abrir
navegador e pode levar bem mais que o heartbeat padrão do RabbitMQ (60s),
principalmente em portais com captcha (2captcha pode levar 15-120s). Com
uma BlockingConnection síncrona processando inline, a thread nunca volta
pro loop de I/O pra responder ao heartbeat e o broker derruba a conexão no
meio do processamento. `consumir_fila` roda callbacks síncronos em thread
separada (asyncio.to_thread) exatamente por isso: o loop de eventos do
aio-pika continua livre pra manter a conexão viva enquanto o navegador
roda em paralelo.

Retry: cada mensagem carrega um header 'x-tentativa'. Se o callback
retornar False e ainda não atingiu MAX_TENTATIVAS, a mensagem é
republicada na mesma fila com o contador incrementado (com um pequeno
backoff). Ao atingir o limite, vai para a DLQ '<portal>.dlq' (dead-letter
já configurado na declaração da fila).
"""
import asyncio
import json

import aio_pika
import pika

from certidoes_core.config import config


def _dlq_nome(portal: str) -> str:
    return f"{portal}.dlq"


def _argumentos_fila(portal: str) -> dict:
    return {
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": _dlq_nome(portal),
    }


def publicar_pedido(portal: str, pedido_id: str):
    """Chamado pelo Gateway após gravar o pedido no banco com status PENDENTE.
    A mensagem carrega só o ID — o worker busca os dados completos no banco,
    assim evitamos inconsistência entre o que está na fila e o que está no
    banco."""
    conexao = pika.BlockingConnection(pika.URLParameters(config.RABBITMQ_URL))
    try:
        canal = conexao.channel()
        canal.queue_declare(queue=_dlq_nome(portal), durable=True)
        canal.queue_declare(queue=portal, durable=True, arguments=_argumentos_fila(portal))

        canal.basic_publish(
            exchange="",
            routing_key=portal,
            body=json.dumps({"pedido_id": pedido_id}),
            properties=pika.BasicProperties(delivery_mode=2, headers={"x-tentativa": 0}),
        )
    finally:
        conexao.close()


async def consumir_fila(portal: str, callback, prefetch: int = 1):
    """Usado pelo worker. `callback(pedido_id: str, tentativa: int) -> bool`
    deve retornar True em caso de sucesso (ack) ou False em caso de falha
    (retry até MAX_TENTATIVAS, depois DLQ). Aceita callback síncrono (roda
    em thread) ou corrotina.

    prefetch=1 significa "não me manda a próxima mensagem antes de eu
    terminar a atual" — importante porque cada pedido abre um navegador,
    não dá pra processar vários em paralelo na mesma instância de worker."""
    conexao = await aio_pika.connect_robust(config.RABBITMQ_URL)
    async with conexao:
        canal = await conexao.channel()
        await canal.set_qos(prefetch_count=prefetch)

        await canal.declare_queue(_dlq_nome(portal), durable=True)
        fila = await canal.declare_queue(portal, durable=True, arguments=_argumentos_fila(portal))

        print(f"[fila] Worker escutando fila '{portal}'. Aguardando pedidos...")

        async with fila.iterator() as mensagens:
            async for mensagem in mensagens:
                await _processar_mensagem(mensagem, portal, canal, callback)


async def _processar_mensagem(mensagem: aio_pika.IncomingMessage, portal: str, canal, callback):
    dados = json.loads(mensagem.body)
    pedido_id = dados["pedido_id"]
    tentativa = int((mensagem.headers or {}).get("x-tentativa", 0)) + 1

    try:
        if asyncio.iscoroutinefunction(callback):
            sucesso = await callback(pedido_id, tentativa)
        else:
            sucesso = await asyncio.to_thread(callback, pedido_id, tentativa)
    except Exception as erro:
        print(f"[fila] Erro inesperado processando {pedido_id}: {erro}")
        sucesso = False

    if sucesso:
        await mensagem.ack()
        return

    if tentativa >= config.MAX_TENTATIVAS:
        print(f"[fila] Pedido {pedido_id} falhou {tentativa}x — indo para a DLQ.")
        await mensagem.reject(requeue=False)
        return

    print(f"[fila] Pedido {pedido_id} falhou (tentativa {tentativa}/{config.MAX_TENTATIVAS}) — reenfileirando.")
    await mensagem.ack()  # remove a entrega atual da fila...
    await asyncio.sleep(min(5 * tentativa, 30))  # pequeno backoff antes de tentar de novo
    await canal.default_exchange.publish(
        aio_pika.Message(
            body=mensagem.body,
            headers={"x-tentativa": tentativa},
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=portal,
    )  # ...e republica com o contador atualizado
