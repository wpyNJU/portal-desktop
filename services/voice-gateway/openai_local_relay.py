"""Loopback CONNECT relay restricted to api.openai.com:443, with SSH forwarding.

TLS remains between the cloud gateway and OpenAI. This relay never reads a Key
or decrypts traffic. Run on the user's computer, not the cloud gateway.
"""
import argparse
import asyncio
import contextlib
import logging

log = logging.getLogger('openai.relay')


async def pump(reader, writer):
    while True:
        data = await asyncio.wait_for(reader.read(65536), 180)
        if not data:return
        writer.write(data)
        await writer.drain()


async def relay(reader, writer):
    upstream = None; jobs = []
    try:
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
        if len(header) > 4096:raise ValueError('header too large')
        first = header.split(b'\r\n', 1)[0].split()
        if len(first) != 3 or first[0] != b'CONNECT' or first[1].lower() != b'api.openai.com:443' or first[2] not in (b'HTTP/1.0', b'HTTP/1.1'):
            writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            await writer.drain();return
        incoming, upstream = await asyncio.wait_for(asyncio.open_connection('api.openai.com', 443), 10)
        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
        jobs = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(incoming, writer))]
        done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        for task in done:task.result()
    except Exception as exc:
        # Do not log request headers, paths, credentials, or TLS bytes.
        log.warning('relay_closed kind=%s', type(exc).__name__)
    finally:
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for stream in (upstream, writer):
            if stream:
                stream.close()
                with contextlib.suppress(Exception):await stream.wait_closed()


def ssh_command(args):
    return ['ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-p', str(args.ssh_port), '-R', f'127.0.0.1:{args.remote_port}:127.0.0.1:{args.port}', args.ssh_target]


async def tunnel(args):
    while True:
        process = await asyncio.create_subprocess_exec(*ssh_command(args), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        log.info('ssh_started pid=%s', process.pid)
        try:
            code = await process.wait()
            log.warning('ssh_disconnected exit=%s; retry in 5 seconds', code)
        finally:
            if process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError):await asyncio.wait_for(process.wait(), 5)
                if process.returncode is None:process.kill();await process.wait()
        await asyncio.sleep(5)


async def main(args):
    limit = asyncio.Semaphore(32)
    async def accepted(reader, writer):
        if limit.locked():writer.close();return
        async with limit:await relay(reader, writer)
    server = await asyncio.start_server(accepted, '127.0.0.1', args.port, limit=4096)
    log.info('ready on loopback port=%s; only api.openai.com:443 allowed', args.port)
    job = asyncio.create_task(tunnel(args)) if args.ssh_target else None
    try:
        async with server:await server.serve_forever()
    finally:
        if job:job.cancel();await asyncio.gather(job, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=22618)
    parser.add_argument('--remote-port', type=int, default=22619)
    parser.add_argument('--ssh-target', help='Existing SSH user@host; uses your existing SSH key and known_hosts')
    parser.add_argument('--ssh-port', type=int, default=22)
    args = parser.parse_args()
    if any(not 1024 <= port <= 65535 for port in (args.port, args.remote_port)) or not 1 <= args.ssh_port <= 65535:
        parser.error('Invalid port')
    if args.ssh_target and (args.ssh_target.startswith('-') or any(c.isspace() for c in args.ssh_target)):
        parser.error('Invalid SSH target')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:asyncio.run(main(args))
    except KeyboardInterrupt:pass
