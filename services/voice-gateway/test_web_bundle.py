"""Offline package and real ASGI route tests; install requirements.txt first."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock,patch
import package as package_builder


class PackageTests(unittest.TestCase):
    def test_allowlist_excludes_runtime_files_and_manifest_matches_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'source';root.mkdir()
            for name in json.loads((package_builder.HERE/'package-files.json').read_text()):
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(package_builder.HERE/name,target)
            (root/'data').mkdir();(root/'data'/'agent-tasks.sqlite3').write_text('private fixture')
            (root/'doubao-api-key.txt').write_text('fixture-secret')
            (root/'debug-dump.json').write_text('private fixture')
            (root/'mobile'/'self-test.wav').write_bytes(b'private recording fixture')
            with patch.object(package_builder,'HERE',root):archive=package_builder.build(Path(folder)/'bundle.zip')
            with zipfile.ZipFile(archive) as bundle:
                manifest=json.loads(bundle.read('being-voice-web/MANIFEST.json'))
                self.assertEqual(len(bundle.namelist()),len(manifest['files'])+1)
                for name,metadata in manifest['files'].items():
                    content=bundle.read('being-voice-web/'+name)
                    self.assertEqual(metadata['sha256'],hashlib.sha256(content).hexdigest())
                    self.assertEqual(metadata['bytes'],len(content))
                self.assertIn('mobile/voice-sans.woff2',manifest['files'])
                self.assertIn('FLOW.md',manifest['files'])
                self.assertFalse(any('fixture-secret' in bundle.read(name).decode(errors='ignore') for name in bundle.namelist() if name.endswith('.txt')))
                self.assertNotIn('doubao-api-key.txt',manifest['files'])
                self.assertNotIn('debug-dump.json',manifest['files'])
                self.assertNotIn('mobile/self-test.wav',manifest['files'])

    def test_even_an_accidental_allowlist_entry_cannot_export_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'package-files.json').write_text('["doubao-api-key.txt"]')
            (root/'doubao-api-key.txt').write_text('fixture-secret')
            with patch.object(package_builder,'HERE',root),self.assertRaisesRegex(ValueError,'Private runtime'):
                package_builder.build(root/'export.zip')


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory();cls.old_data=os.environ.get('VOICE_DATA_DIR')
        from serve import create_app
        from fastapi.testclient import TestClient
        cls.app=create_app(Path(cls.directory.name)/'data')
        cls.client=TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        import doubao_server
        doubao_server.task_store.db.close()
        cls.directory.cleanup()
        if cls.old_data is None:os.environ.pop('VOICE_DATA_DIR',None)
        else:os.environ['VOICE_DATA_DIR']=cls.old_data

    def test_web_assets_and_keyless_start(self):
        response=self.client.get('/mobile/')
        self.assertEqual(response.status_code,200);self.assertIn('点击开始通话',response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.client.get('/').url.path,'/mobile/')
        for path in ('call.js','voice-ui.woff2','voice-sans.woff2'):
            response=self.client.get('/call-assets/'+path)
            self.assertEqual(response.status_code,200);self.assertTrue(response.content)
        self.assertFalse(self.client.get('/pipeline/health').json()['ready'])
        self.assertTrue((Path(self.directory.name)/'data'/'agent-tasks.sqlite3').exists())

    def test_runtime_and_source_files_are_never_served(self):
        for path in ('/call-assets/doubao-api-key.txt','/call-assets/agent-tasks.sqlite3',
                     '/call-assets/../doubao_config.py','/data/agent-tasks.sqlite3','/doubao_config.py'):
            self.assertEqual(self.client.get(path).status_code,404)

    def test_websocket_rejects_invalid_links_before_any_upstream_connection(self):
        with self.client.websocket_connect('/pipeline/ws') as socket:
            socket.send_json({'type':'agent.configure','agent_url':'invalid'})
            self.assertEqual(socket.receive_json()['type'],'error')

    def test_new_install_configures_link_and_reads_profile_without_default_agent_file(self):
        with patch('agent_bridge.AgentBridge.verify_access',new=AsyncMock()) as verify:
            link='https://echo.beings.town/fixture/?token=test-link-1234'
            with self.client.websocket_connect('/pipeline/ws') as socket:
                socket.send_json({'type':'agent.configure','agent_url':link})
                result=socket.receive_json()
                self.assertEqual(result['type'],'agent.configured');self.assertFalse(result['legacy'])
                self.assertEqual(result['endpoint'],'/fixture/api/chat/stream')
                self.assertNotIn('test-link-1234',json.dumps(result))
            with self.client.websocket_connect('/pipeline/ws') as socket:
                socket.send_json({'type':'profile.status','agent_url':link})
                self.assertEqual(socket.receive_json(),{'type':'profile.status','updated_at':None})
            self.assertEqual(verify.await_count,2)


if __name__=='__main__':unittest.main()
