"""One process, two independent loops: SQL outbox relay and Kafka search consumer."""
import json
import signal
import threading
from .adapters import dependencies
from .core import emit, project_one, relay_once, identifier
from .observability import safe_error


def relay_loop(stop, repo, broker):
    while not stop.is_set():
        try:
            if not relay_once(repo, broker):
                stop.wait(0.25)
        except Exception as exc:
            emit("relay_retry", dependency="mariadb_or_kafka", **safe_error(exc))
            stop.wait(2)


class CheckpointError(RuntimeError):
    code = "kafka_checkpoint_unconfirmed"


def acknowledge(consumer, message):
    results = consumer.commit(message=message, asynchronous=False)
    # Synchronous commit returns the committed TopicPartition list. Empty/mismatched
    # results are not confirmation; keep the existing replay-on-error path.
    if not isinstance(results, list) or len(results) != 1:
        raise CheckpointError("Kafka checkpoint was not confirmed")
    position = results[0]
    if (getattr(position, "error", None) is not None or
            getattr(position, "topic", None) != message.topic() or
            type(getattr(position, "partition", None)) is not int or
            position.partition != message.partition() or
            type(getattr(position, "offset", None)) is not int or
            position.offset != message.offset() + 1):
        raise CheckpointError("Kafka checkpoint identity/offset disagrees with processed message")


def consume_loop(stop, broker, search, cache):
    while not stop.is_set():
        consumer = None
        position = {}
        stage = "kafka_poll"
        try:
            consumer = broker.consumer()
            consumer.subscribe([broker.s.kafka_topic])
            while not stop.is_set():
                position = {}
                stage = "kafka_poll"
                message = consumer.poll(1)
                if message is None:
                    continue
                if message.error():
                    raise RuntimeError("Kafka poll error")
                position = {"partition": message.partition(), "offset": message.offset()}
                stage = "decode_event"
                event = json.loads(message.value())
                if isinstance(event, dict):
                    try:
                        position["event_id"] = identifier(event.get("event_id"))
                    except Exception:
                        pass
                emit("event_received", **position)
                stage = "validate_project_commit"
                # On any failure, close WITHOUT auto-commit and rejoin from committed offset.
                # Never poll/commit a later message past this failed event.
                project_one(event, search, cache,
                            lambda: acknowledge(consumer, message))
        except Exception as exc:
            emit("consumer_retry", operation=stage, **position, **safe_error(exc))
        finally:
            if consumer is not None:
                try:
                    consumer.close()
                except Exception as exc:
                    emit("consumer_close_failed", error=type(exc).__name__)
        stop.wait(2)


def main():
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    repo, cache, search, broker = dependencies()
    # Each loop owns its own clients. Do not scale this worker: relay has no distributed lock.
    threads = [threading.Thread(target=relay_loop, args=(stop, repo, broker), name="relay"),
               threading.Thread(target=consume_loop, args=(stop, broker, search, cache), name="consumer")]
    emit("worker_started", topic=broker.s.kafka_topic, group=broker.s.kafka_group)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    emit("worker_stopped")

if __name__ == "__main__":
    main()
