import asyncio
import unittest
from unittest.mock import patch
from openai_local_relay import relay


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await asyncio.start_server(relay, '127.0.0.1', 0, limit=4096)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close();await self.server.wait_closed()

    async def test_other_destinations_never_connect(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        with patch('openai_local_relay.asyncio.open_connection', side_effect=AssertionError('Forbidden upstream')):
            writer.write(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n');await writer.drain()
            self.assertIn(b'403 Forbidden', await asyncio.wait_for(reader.read(), 1))
        writer.close();await writer.wait_closed()

    async def test_exact_openai_target_tunnels_bidirectional_bytes(self):
        async def echo(reader, writer):
            writer.write(await reader.read(100));await writer.drain();writer.close();await writer.wait_closed()
        echo_server = await asyncio.start_server(echo, '127.0.0.1', 0)
        connect = asyncio.open_connection
        async def upstream(host, port):
            self.assertEqual((host, port), ('api.openai.com', 443))
            return await connect('127.0.0.1', echo_server.sockets[0].getsockname()[1])
        reader, writer = await connect('127.0.0.1', self.port)
        try:
            with patch('openai_local_relay.asyncio.open_connection', side_effect=upstream):
                writer.write(b'CONNECT api.openai.com:443 HTTP/1.1\r\n\r\n');await writer.drain()
                self.assertIn(b'200 Connection Established', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 1))
                writer.write(b'opaque-tls-fixture');await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(18), 1), b'opaque-tls-fixture')
        finally:
            writer.close();await writer.wait_closed();echo_server.close();await echo_server.wait_closed()


if __name__ == '__main__':unittest.main()
