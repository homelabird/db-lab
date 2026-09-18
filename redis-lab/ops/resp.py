"""Small RESP2-only lab client. One connection per thread; NEVER replays writes.

Not a production replacement for redis-py. No TLS, Cluster, pubsub abstraction,
transactions or automatic retries. Error replies do not desynchronize a pipeline.
"""
from __future__ import annotations
import os
import socket
from dataclasses import dataclass
from typing import BinaryIO

class RedisError(Exception): pass
class ProtocolError(RedisError): pass
class ServerError(RedisError): pass
class TransportError(RedisError):
    """The command might have reached Redis. Do not assume it was rejected."""


def encode(args) -> bytes:
    if not args: raise ValueError('Empty Redis command')
    parts = []
    for arg in args:
        if isinstance(arg, bytes): item = arg
        elif isinstance(arg, (str, int, float)): item = str(arg).encode()
        else: raise TypeError('Redis arguments must be bytes, str or numbers')
        parts.append(b'$' + str(len(item)).encode() + b'\r\n' + item + b'\r\n')
    return b'*' + str(len(parts)).encode() + b'\r\n' + b''.join(parts)


def read_reply(stream: BinaryIO, depth=0):
    if depth > 32: raise ProtocolError('Excessive RESP nesting')
    line = stream.readline(65537)
    if not line: raise EOFError('Redis closed the connection')
    if len(line) > 65536 or not line.endswith(b'\r\n'): raise ProtocolError('Invalid RESP line')
    kind, data = line[:1], line[1:-2]
    if kind == b'+': return data
    if kind == b'-': return ServerError(data.decode('utf-8', 'replace'))
    if kind == b':': return int(data)
    if kind in (b'$', b'*'):
        size = int(data)
        if size == -1: return None
        if size < 0: raise ProtocolError('Negative RESP length')
        if kind == b'$':
            if size > 128 * 1024 * 1024: raise ProtocolError('Bulk reply exceeds lab limit')
            value = stream.read(size)
            if len(value) != size or stream.read(2) != b'\r\n': raise ProtocolError('Truncated bulk reply')
            return value
        if size > 1000000: raise ProtocolError('Array exceeds lab limit')
        return [read_reply(stream, depth + 1) for _ in range(size)]
    raise ProtocolError(f'Unsupported RESP type {kind!r}; use RESP2')


def text(value):
    if isinstance(value, bytes): return value.decode('utf-8', 'replace')
    if isinstance(value, list): return [text(x) for x in value]
    if isinstance(value, dict): return {text(k): text(v) for k,v in value.items()}
    return value


def pairs(value) -> dict:
    data = text(value)
    if len(data) % 2: raise ProtocolError('Expected alternating key/value list')
    return dict(zip(data[::2], data[1::2]))


def parse_info(raw) -> dict:
    out = {}
    for line in text(raw).splitlines():
        if not line or line.startswith('#') or ':' not in line: continue
        k,v = line.split(':', 1)
        if ',' in v and '=' in v:
            value={}
            for field in v.split(','):
                if '=' in field:
                    a,b=field.split('=',1)
                    try: b=float(b) if '.' in b else int(b)
                    except ValueError: pass
                    value[a]=b
            out[k]=value
        else:
            try: out[k]=float(v) if '.' in v else int(v)
            except ValueError: out[k]=v
    return out


class Connection:
    def __init__(self, host, port=6379, password=None, timeout=2.0):
        self.host, self.port, self.password, self.timeout=host,int(port),password,timeout
        self.sock=None; self.stream=None; self.last_command_send_attempted=False
    def connect(self):
        if self.sock is not None: return
        try:
            self.sock=socket.create_connection((self.host,self.port),timeout=self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.stream=self.sock.makefile('rb')
            if self.password:
                self.sock.sendall(encode(('AUTH',self.password)))
                answer=read_reply(self.stream)
                if isinstance(answer,ServerError): raise answer
                if answer != b'OK': raise ProtocolError('AUTH did not return OK')
        except (OSError, EOFError, ValueError, RedisError) as exc:
            self.close()
            if isinstance(exc,RedisError): raise
            raise TransportError(str(exc)) from exc
    def execute(self,*args):
        return self.pipeline([args])[0]
    def pipeline(self,commands,raise_errors=True):
        commands=list(commands)
        if not commands: return []
        if len(commands)>1024: raise ValueError('Pipeline limit is 1024 commands')
        self.last_command_send_attempted=False
        self.connect()
        try:
            packet=b''.join(encode(args) for args in commands)
            if len(packet)>4*1024*1024: raise ValueError('Pipeline request limit is 4MiB')
            self.last_command_send_attempted=True
            self.sock.sendall(packet)
            replies=[read_reply(self.stream) for _ in commands]
        except (OSError,EOFError,ValueError,ProtocolError) as exc:
            self.close()
            raise TransportError(str(exc)) from exc
        if raise_errors:
            for reply in replies:
                if isinstance(reply,ServerError): raise reply
        return replies
    def info(self,section='all'): return parse_info(self.execute('INFO',section))
    def config(self,*names): return pairs(self.execute('CONFIG','GET',*names))
    def close(self):
        if self.stream is not None:
            try: self.stream.close()
            except OSError: pass
        if self.sock is not None:
            try: self.sock.close()
            except OSError: pass
        self.stream=self.sock=None
    def __enter__(self): return self
    def __exit__(self,*exc): self.close()


def endpoints(value):
    result=[]
    for item in value.split(','):
        host,port=item.rsplit(':',1)
        if not host or not 1<=int(port)<=65535: raise ValueError('Invalid endpoint')
        result.append((host,int(port)))
    return result


@dataclass
class Settings:
    password: str
    sentinel_password: str
    master_name: str
    sentinels: list
    nodes: list
    perf_host: str
    proxy_host: str
    @classmethod
    def env(cls):
        return cls(os.environ.get('REDIS_PASSWORD',''), os.environ.get('SENTINEL_PASSWORD',''),
            os.environ.get('MASTER_NAME','mymaster'),
            endpoints(os.environ.get('SENTINEL_NODES','sentinel-1:26379,sentinel-2:26379,sentinel-3:26379')),
            endpoints(os.environ.get('REDIS_NODES','redis-1:6379,redis-2:6379,redis-3:6379')),
            os.environ.get('PERF_HOST','redis-perf'),os.environ.get('PROXY_HOST','net-proxy'))
    def direct(self,target='perf',timeout=2):
        if target not in ('perf','proxy'): raise ValueError('Direct target must be perf or proxy')
        return Connection(self.perf_host if target=='perf' else self.proxy_host,6379,self.password,timeout)
    def master_address(self):
        errors=[]
        for host,port in self.sentinels:
            try:
                with Connection(host,port,self.sentinel_password,1) as s:
                    a=text(s.execute('SENTINEL','GET-MASTER-ADDR-BY-NAME',self.master_name))
                if not a: continue
                with Connection(a[0],int(a[1]),self.password,1) as r:
                    if r.execute('ROLE')[0] != b'master': continue
                return a[0],int(a[1])
            except RedisError as exc: errors.append(type(exc).__name__)
        raise TransportError('No reachable Sentinel-discovered master: '+','.join(errors))
    def connection(self,target='perf',timeout=2):
        if target=='ha':
            host,port=self.master_address()
            return Connection(host,port,self.password,timeout)
        return self.direct(target,timeout)
