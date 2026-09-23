"""Privacy-preserving host and running-container benchmark metadata."""
import hashlib
import json
import os
import platform
import subprocess
import threading
import time


def host_pressure_snapshot():
    snapshot = {'host_load_1m': None, 'host_cpu_ticks_total': None,
                'host_cpu_ticks_idle': None, 'host_memory_bytes': None,
                'host_available_memory_bytes': None, 'host_swap_total_bytes': None,
                'host_swap_free_bytes': None}
    try:
        snapshot['host_load_1m'] = os.getloadavg()[0]
    except (AttributeError, OSError):
        pass
    try:
        with open('/proc/stat', encoding='ascii') as cpuinfo:
            values = [int(value) for value in next(cpuinfo).split()[1:]]
        if len(values) >= 8:
            # guest and guest_nice are already included in user and nice.
            snapshot['host_cpu_ticks_total'] = sum(values[:8])
            snapshot['host_cpu_ticks_idle'] = values[3] + values[4]
    except (OSError, StopIteration, ValueError):
        pass
    wanted = {'MemTotal': 'host_memory_bytes', 'MemAvailable': 'host_available_memory_bytes',
              'SwapTotal': 'host_swap_total_bytes', 'SwapFree': 'host_swap_free_bytes'}
    try:
        with open('/proc/meminfo', encoding='ascii') as meminfo:
            for line in meminfo:
                key, _, value = line.partition(':')
                if key in wanted:
                    snapshot[wanted[key]] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return snapshot


class HostPressureSampler:
    """Bound host-wide pressure sampling for a workload's measured interval."""
    def __init__(self, interval_seconds=1.0):
        self.interval_seconds = interval_seconds
        self.samples = []
        self.next_sample = 0.0
        self._stop = threading.Event()
        self._thread = None

    def sample(self, force=False):
        now = time.monotonic()
        if not force and self.samples and now < self.next_sample:
            return
        self.samples.append({**host_pressure_snapshot(), 'elapsed_seconds': now})
        self.next_sample = now + self.interval_seconds

    def start(self):
        self.sample(force=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            self.sample()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        self.sample(force=True)
        return self.summary()

    def summary(self):
        cpu_busy = []
        for before, after in zip(self.samples, self.samples[1:]):
            total = (after.get('host_cpu_ticks_total') or 0) - (before.get('host_cpu_ticks_total') or 0)
            idle = (after.get('host_cpu_ticks_idle') or 0) - (before.get('host_cpu_ticks_idle') or 0)
            if total > 0 and 0 <= idle <= total:
                cpu_busy.append((total - idle) * 100 / total)
        def observed_min(key):
            values = [sample[key] for sample in self.samples if isinstance(sample.get(key), (int, float))]
            return min(values) if values else None
        def observed_max(key):
            values = [sample[key] for sample in self.samples if isinstance(sample.get(key), (int, float))]
            return max(values) if values else None
        duration = (self.samples[-1]['elapsed_seconds'] - self.samples[0]['elapsed_seconds']) if len(self.samples) > 1 else 0
        return {'scope': 'host', 'sample_interval_seconds': self.interval_seconds,
                'sample_count': len(self.samples), 'coverage_seconds': round(duration, 3),
                'host_cpu_busy_pct_mean': round(sum(cpu_busy) / len(cpu_busy), 2) if cpu_busy else None,
                'host_cpu_busy_pct_peak': round(max(cpu_busy), 2) if cpu_busy else None,
                'host_load_1m_peak': observed_max('host_load_1m'),
                'host_available_memory_bytes_min': observed_min('host_available_memory_bytes'),
                'host_swap_free_bytes_min': observed_min('host_swap_free_bytes'),
                'cpu_intervals_observed': len(cpu_busy)}


def environment(runtime, containers):
    identity='|'.join((platform.node(),platform.system(),platform.release(),platform.machine()))
    pressure=host_pressure_snapshot()
    result={'host_fingerprint':hashlib.sha256(identity.encode()).hexdigest()[:20],
            'host_cpu_count':os.cpu_count(),
            **{key:value for key,value in pressure.items() if not key.startswith('host_cpu_ticks_')},
            'container_limits':None,'container_images':None}
    if runtime not in ('docker','podman') or not containers:
        return result
    limits={};images={}
    for name in containers:
        try:
            raw=subprocess.run([runtime,'inspect',name],capture_output=True,text=True,timeout=5,check=True).stdout
            inspected=json.loads(raw)
            obj=inspected[0] if isinstance(inspected,list) else inspected
            host=obj.get('HostConfig') or {}
            memory_limit=int(host.get('Memory') or 0)
            nano_cpus=int(host.get('NanoCpus') or 0)
            cpu_quota=int(host.get('CpuQuota') or 0)
            cpu_period=int(host.get('CpuPeriod') or 0)
            image_id=obj.get('Image') or obj.get('ImageID')
            if not image_id:
                raise ValueError('container inspect omitted image ID')
            image_raw=subprocess.run([runtime,'image','inspect',image_id],capture_output=True,
                                     text=True,timeout=5,check=True).stdout
            image_result=json.loads(image_raw)
            image=image_result[0] if isinstance(image_result,list) else image_result
            digests=image.get('RepoDigests') or []
            limits[name]={'memory_bytes':memory_limit or None,
                          'cpu_cores':(nano_cpus/1_000_000_000 if nano_cpus else
                                       cpu_quota/cpu_period if cpu_quota>0 and cpu_period>0 else None)}
            images[name]={'image_id':image_id,'repo_digests':sorted(digests)}
        except (OSError,subprocess.SubprocessError,ValueError,TypeError,KeyError):
            return {**result,'container_limits':None,'container_images':None}
    return {**result,'container_limits':limits,'container_images':images}
