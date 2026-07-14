"""
Start in order
demo_scheduler_v1_instance
demo_scheduler_v1
demo_scheduler_v1_proxy
demo_scheduler_v1_client(disabled; switch to client.py)
"""

import subprocess
import time
import sys
import socket
import threading


def wait_for_port(host: str, port: int, timeout: float = 30.0):
    """Port readiness probe"""
    start = time.time()
    while True:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                print(f"[OK] {host}:{port} is now connectable")
                return
        except OSError:
            if time.time() - start > timeout:
                raise TimeoutError(f"Waited for {host}:{port} to start for more than {timeout}ss; service appears to have failed to start")
            time.sleep(0.2)


def stream_output(process: subprocess.Popen, name: str):
    """Read child-process output in real time"""
    def _stream(pipe, prefix):
        for line in iter(pipe.readline, b''):
            print(f"{line.decode().rstrip()}")
    threading.Thread(target=_stream, args=(process.stdout, "STDOUT"), daemon=True).start()
    threading.Thread(target=_stream, args=(process.stderr, "STDERR"), daemon=True).start()


def start_service(script: str, name: str) -> subprocess.Popen:
    """Start a background service and keep it running"""
    print(f"[START] Start {name} ({script})")
    p = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        close_fds=True
    )
    stream_output(p, name)
    return p


def run_client(script: str):
    """Start client synchronously"""
    print(f"[START] Start Client ({script})")
    # Client runs in the foreground and exits after completion
    p = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stream_output(p, "Client")
    p.wait()
    print("[DONE] Client execution finished")


if __name__ == "__main__":
    proxy = None
    scheduler = None
    Instance = None

    try:
        # Start KDN server
        kdn = start_service("demo_kdn.py", "KDN")
        wait_for_port("127.0.0.1", 9101)

        # Start Instance
        Instance = start_service("demo_instance.py", "Instance")
        wait_for_port("127.0.0.1", 9001)

        # Start Proxy
        proxy = start_service("demo_proxy.py", "Proxy")
        wait_for_port("127.0.0.1", 8001)

        # Start Scheduler
        scheduler = start_service("demo_scheduler.py", "Scheduler")
        wait_for_port("127.0.0.1", 7001)

        # Start Client; this demo sends two fixed requests, while the new interface runs client/client.py
        # run_client("demo_client.py")

        print("\nThe system is still running; press Ctrl+C to exit.")

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[STOP] Received Ctrl+C, shutting down child processes...")

    finally:
        if kdn and kdn.poll() is None:
            kdn.terminate()
        if scheduler and scheduler.poll() is None:
            scheduler.terminate()
        if proxy and proxy.poll() is None:
            proxy.terminate()
        if Instance and Instance.poll() is None:
            Instance.terminate()
        print("[CLEAN] All processes have ended")
