"""REAL local TCP transport tests against a tiny test double, NOT a Redis server.
The tests exercise framing, pipelines, auth errors, scheduling and proxy delays.
They do not establish compatibility with actual Redis or Podman.
"""
import asyncio
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from ops.resp import Connection,ServerError,TransportError,read_reply,Settings
from ops.proxy import Proxy
from ops.common import Report
from ops.load import exercise

def reply(value):
    if value is None:return b'$-1\r\n'
    if isinstance(value,ServerError):return b'-'+str(value).encode()+b'\r\n'
    if isinstance(value,int):return b':'+str(value).encode()+b'\r\n'
    if isinstance(value,list):return b'*'+str(len(value)).encode()+b'\r\n'+b''.join(reply(v) for v in value)
    if isinstance(value,str):value=value.encode()
    return b'$'+str(len(value)).encode()+b'\r\n'+value+b'\r\n'

class FakeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            try:args=read_reply(self.rfile)
            except (EOFError,OSError):return
            cmd=args[0].upper();s=self.server
            with s.lock:
                if cmd==b'AUTH':answer=b'OK' if args[1]==b'password' else ServerError('WRONGPASS test password')
                elif cmd==b'PING':answer=b'PONG'
                elif cmd==b'SET':s.data[args[1]]=args[2];answer=b'OK'
                elif cmd==b'GET':answer=s.data.get(args[1])
                elif cmd==b'INFO':answer=b'used_memory:1000000\r\nmaxmemory:268435456\r\nrole:master\r\n'
                elif cmd==b'FAIL':answer=ServerError('ERR injected')
                elif cmd==b'DROP':s.drop_count+=1;return
                elif cmd==b'SCAN':
                    prefix=args[args.index(b'MATCH')+1].rstrip(b'*')
                    answer=[b'0',[k for k in s.data if k.startswith(prefix)]]
                elif cmd==b'UNLINK':
                    answer=sum(1 for k in args[1:] if s.data.pop(k,None) is not None)
                else:answer=ServerError('ERR unsupported test command')
            try:self.wfile.write(reply(answer));self.wfile.flush()
            except OSError:return

class FakeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True
    def __init__(self):
        super().__init__(('127.0.0.1',0),FakeHandler);self.lock=threading.Lock();self.data={};self.drop_count=0

class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=FakeServer();cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def connection(self,password='password'):return Connection(*self.server.server_address,password=password,timeout=1)
    def test_real_tcp_get_set_binary(self):
        with self.connection() as c:
            self.assertEqual(c.execute('SET','binary',b'\0\xff\r\n'),b'OK');self.assertEqual(c.execute('GET','binary'),b'\0\xff\r\n')
    def test_wrong_auth_is_server_error(self):
        with self.connection('bad') as c:
            with self.assertRaises(ServerError):c.execute('PING')
    def test_pipeline_drains_errors(self):
        with self.connection() as c:
            with self.assertRaises(ServerError):c.pipeline([('PING',),('FAIL',),('PING',)])
            self.assertEqual(c.execute('PING'),b'PONG')
    def test_pipeline_return_errors(self):
        with self.connection() as c:
            result=c.pipeline([('PING',),('FAIL',)],raise_errors=False)
            self.assertEqual(result[0],b'PONG');self.assertIsInstance(result[1],ServerError)
    def test_no_replay_after_connection_drop(self):
        before=self.server.drop_count
        with self.connection() as c:
            with self.assertRaises(TransportError):c.execute('DROP')
            self.assertEqual(self.server.drop_count-before,1)
            self.assertEqual(c.execute('PING'),b'PONG')
    def test_threaded_load_runs_over_real_tcp(self):
        class TestSettings:
            def connection(_,target='perf',timeout=1):return Connection(*self.server.server_address,password='password',timeout=timeout)
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'OPS_RESULTS':directory}):
            report=Report('tcp-test')
            data=exercise(TestSettings(),report,seconds=1,rate=60,workers=4,keys=30,payload=64)
            self.assertGreater(data['counts']['success'],10)
            self.assertEqual(data['counts'].get('error',0),0)
            self.assertEqual(data['counts']['command_send_attempted'],data['counts']['attempted'])
            self.assertEqual(data['counts']['success']+data['counts'].get('error',0),data['counts']['attempted'])
            self.assertGreater(data['cleanup_keys'],0)

class ProxyTcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def echo(reader,writer):
            try:
                while True:
                    data=await reader.read(4096)
                    if not data:break
                    writer.write(data);await writer.drain()
            finally:writer.close()
        self.echo=await asyncio.start_server(echo,'127.0.0.1',0)
        port=self.echo.sockets[0].getsockname()[1]
        self.proxy=Proxy('127.0.0.1',port,'secret')
        self.front=await asyncio.start_server(self.proxy.client,'127.0.0.1',0)
        self.control=await asyncio.start_server(self.proxy.control,'127.0.0.1',0)
        self.front_port=self.front.sockets[0].getsockname()[1];self.control_port=self.control.sockets[0].getsockname()[1]
    async def asyncTearDown(self):
        for server in (self.front,self.control,self.echo):server.close();await server.wait_closed()
        await asyncio.sleep(.05)
    async def test_bidirectional_delay(self):
        self.proxy.rules.set({'delay_ms':30,'ttl_s':10})
        reader,writer=await asyncio.open_connection('127.0.0.1',self.front_port)
        start=time.monotonic();writer.write(b'hello');await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5),2),b'hello')
        self.assertGreaterEqual(time.monotonic()-start,.055)
        writer.close();await writer.wait_closed()
    async def test_drop_rule_closes_connection(self):
        self.proxy.rules.set({'disconnect':True})
        reader,writer=await asyncio.open_connection('127.0.0.1',self.front_port)
        self.assertEqual(await asyncio.wait_for(reader.read(),1),b'')
        writer.close();await writer.wait_closed()
    async def test_control_rejects_no_auth(self):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.control_port)
        writer.write(b'GET /state HTTP/1.1\r\nHost: localhost\r\n\r\n');await writer.drain()
        response=await reader.read();self.assertIn(b'401',response)
        writer.close();await writer.wait_closed()
    async def test_authenticated_control_updates_rule(self):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.control_port)
        body=b'{"delay_ms":25,"ttl_s":5}'
        writer.write(b'POST /state HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer secret\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        await writer.drain();response=await reader.read();self.assertIn(b'200',response);self.assertEqual(self.proxy.rules.get()['delay_ms'],25)
        writer.close();await writer.wait_closed()
