"""Bounded application-path TCP fault proxy. This is NOT a Sentinel/replication proxy.
Each direction is delayed per read chunk; bandwidth is per connection/direction.
Rules auto-expire so killing a scenario does not leave a permanent network fault.
"""
from __future__ import annotations
import asyncio
import hmac
import json
import os
import time

class Rules:
    def __init__(self): self.reset()
    def reset(self):
        self.delay_ms=0;self.kib_per_s=0;self.disconnect=False;self.until=0.
    def set(self,obj):
        if not isinstance(obj,dict) or set(obj)-{'delay_ms','kib_per_s','disconnect','ttl_s'}:
            raise ValueError('Unknown rule fields')
        delay=obj.get('delay_ms',0);bw=obj.get('kib_per_s',0);ttl=obj.get('ttl_s',60);drop=obj.get('disconnect',False)
        if type(delay) is not int or not 0<=delay<=1000: raise ValueError('delay_ms must be 0..1000')
        if type(bw) is not int or not 0<=bw<=10240: raise ValueError('kib_per_s must be 0..10240')
        if type(ttl) is not int or not 1<=ttl<=120: raise ValueError('ttl_s must be 1..120')
        if type(drop) is not bool: raise ValueError('disconnect must be a boolean')
        self.delay_ms,self.kib_per_s,self.disconnect=delay,bw,drop
        self.until=time.monotonic()+ttl
    def get(self):
        if self.until and time.monotonic()>=self.until:self.reset()
        return {'delay_ms':self.delay_ms,'kib_per_s':self.kib_per_s,'disconnect':self.disconnect,
            'remaining_s':round(max(0,self.until-time.monotonic()),3)}

class Proxy:
    def __init__(self,upstream,port=6379,token=''):
        self.upstream,self.port,self.token=upstream,port,token; self.rules=Rules();self.active=0
    async def relay(self,reader,writer):
        while True:
            rule=self.rules.get()
            if rule['disconnect']:return
            data=await reader.read(16384)
            if not data:return
            wait=rule['delay_ms']/1000
            if rule['kib_per_s']: wait+=len(data)/(rule['kib_per_s']*1024)
            if wait:await asyncio.sleep(wait)
            if self.rules.get()['disconnect']:return
            writer.write(data);await writer.drain()
    async def client(self,reader,writer):
        remote=None;tasks=[]
        if self.active>=128:
            writer.close();await writer.wait_closed();return
        self.active+=1
        try:
            if self.rules.get()['disconnect']:return
            r,remote=await asyncio.wait_for(asyncio.open_connection(self.upstream,self.port),3)
            tasks=[asyncio.create_task(self.relay(reader,remote)),asyncio.create_task(self.relay(r,writer))]
            await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        except (OSError,asyncio.TimeoutError): pass
        finally:
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            writer.close()
            if remote:remote.close()
            for w in [writer,remote]:
                if w:
                    try:await w.wait_closed()
                    except OSError:pass
            self.active-=1
    async def control(self,reader,writer):
        status=200;body={}
        try:
            raw=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
            if len(raw)>8192:raise ValueError('Header too large')
            lines=raw.decode('ascii').split('\r\n');method,path,_=lines[0].split()
            headers={k.strip().lower():v.strip() for k,v in (line.split(':',1) for line in lines[1:] if ':' in line)}
            expected='Bearer '+self.token
            if not self.token or not hmac.compare_digest(headers.get('authorization',''),expected):
                status=401;body={'error':'Unauthorized'}
            elif path!='/state':status=404;body={'error':'Not found'}
            elif method=='GET':body={**self.rules.get(),'active_connections':self.active}
            elif method=='POST':
                length=int(headers.get('content-length','0'))
                if not 0<length<=2048:raise ValueError('Body length must be 1..2048')
                obj=json.loads(await asyncio.wait_for(reader.readexactly(length),3))
                self.rules.set(obj);body=self.rules.get()
            else:status=405;body={'error':'Method not allowed'}
        except (ValueError,UnicodeError,asyncio.IncompleteReadError,asyncio.LimitOverrunError,asyncio.TimeoutError) as exc:
            status=400;body={'error':str(exc)}
        data=json.dumps(body).encode()
        try:
            writer.write(f'HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode()+data)
            await writer.drain()
        except OSError:pass
        finally:writer.close()

async def main():
    token=os.environ.get('PROXY_TOKEN','')
    if not token:raise RuntimeError('PROXY_TOKEN required')
    p=Proxy(os.environ.get('PERF_HOST','redis-perf'),token=token)
    a=await asyncio.start_server(p.client,'0.0.0.0',6379)
    b=await asyncio.start_server(p.control,'0.0.0.0',8080,limit=8192)
    print('Internal-only proxy: TCP 6379, authenticated control 8080',flush=True)
    async with a,b:await asyncio.gather(a.serve_forever(),b.serve_forever())

if __name__=='__main__':asyncio.run(main())
