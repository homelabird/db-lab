"""One process, two independent loops: SQL outbox relay and Kafka search consumer."""
import json
import signal
import threading
import time
from .adapters import dependencies
from .core import emit, project_one, relay_once, identifier
from .observability import safe_error


def relay_loop(stop, repo, broker, health=None):
    while not stop.is_set():
        try:
            relayed = relay_once(repo, broker)
            if health is not None:
                health.mark("relay", True)
            if not relayed:
                stop.wait(0.25)
        except Exception as exc:
            if health is not None:
                health.mark("relay", False)
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


def idle_consumer_ready(consumer, topic):
    """An old partition assignment alone cannot establish current broker access."""
    assigned = consumer.assignment()
    if (not isinstance(assigned, list) or len(assigned) != 1
            or assigned[0].topic != topic or type(assigned[0].partition) is not int
            or assigned[0].partition != 0 or getattr(assigned[0], "error", None) is not None):
        return False
    offsets = consumer.get_watermark_offsets(assigned[0], timeout=2, cached=False)
    if (not isinstance(offsets, tuple) or len(offsets) != 2
            or any(type(offset) is not int for offset in offsets)
            or not 0 <= offsets[0] <= offsets[1]):
        raise RuntimeError("Kafka readiness offset query was not confirmed")
    return True


def consume_loop(stop, broker, search, cache, health=None):
    while not stop.is_set():
        consumer = None
        position = {}
        stage = "kafka_poll"
        last_health_probe = float("-inf")
        try:
            consumer = broker.consumer()
            consumer.subscribe([broker.s.kafka_topic])
            while not stop.is_set():
                position = {}
                stage = "kafka_poll"
                message = consumer.poll(1)
                if message is None:
                    if health is not None and time.monotonic() - last_health_probe >= 5:
                        # At most one uncached broker check per five seconds while idle.
                        # A timed-out poll plus cached assignment is not readiness evidence.
                        health.mark("consumer", idle_consumer_ready(consumer, broker.s.kafka_topic))
                        last_health_probe = time.monotonic()
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
                if health is not None:
                    health.mark("consumer", True)
        except Exception as exc:
            if health is not None:
                health.mark("consumer", False)
            emit("consumer_retry", operation=stage, **position, **safe_error(exc))
        finally:
            if health is not None:
                health.mark("consumer", False)
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
    from .worker_health import WorkerHealth
    health = WorkerHealth()
    repo, cache, search, broker = dependencies()
    # Each loop owns its own clients. Do not scale this worker: relay has no distributed lock.
    threads = [threading.Thread(target=relay_loop, args=(stop, repo, broker, health), name="relay"),
               threading.Thread(target=consume_loop, args=(stop, broker, search, cache, health), name="consumer")]
    emit("worker_started", topic=broker.s.kafka_topic, group=broker.s.kafka_group)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    health.close()
    emit("worker_stopped")

if __name__ == "__main__":
    main()
