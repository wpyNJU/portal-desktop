"""Standalone mobile site + gateway, with no dependency on the GPU host proxy."""
import argparse
import os
from pathlib import Path

HERE=Path(__file__).resolve().parent


def create_app(data_dir):
    # Set this before importing the gateway: its SQLite/profile paths are per process.
    os.environ['VOICE_DATA_DIR']=str(Path(data_dir).expanduser().resolve())
    from fastapi import HTTPException
    from fastapi.responses import FileResponse,RedirectResponse
    from doubao_server import app,connection,health

    app.add_api_websocket_route('/pipeline/ws',connection)
    app.add_api_route('/pipeline/health',health,methods=['GET'])

    @app.get('/')
    @app.get('/mobile')
    @app.get('/pipeline')
    async def redirect():return RedirectResponse('/mobile/')

    @app.get('/mobile/')
    @app.get('/pipeline/')
    async def page():return FileResponse(HERE/'mobile/index.html',headers={'Cache-Control':'no-store'})

    @app.get('/call-assets/{name}')
    async def asset(name:str):
        # Never expose the source/data directory, profiles, keys or SQLite as static files.
        allowed={'call.js','voice-ui.woff2','voice-sans.woff2','self-test.wav'}
        if name not in allowed:raise HTTPException(status_code=404)
        path=HERE/'mobile'/name
        if not path.is_file():raise HTTPException(status_code=404)
        return FileResponse(path,headers={'Cache-Control':'no-cache' if name=='call.js' else 'public, max-age=604800'})

    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=22601)
    parser.add_argument('--data-dir',default=os.environ.get('VOICE_DATA_DIR',str(HERE/'data')))
    args=parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.data_dir),host=args.host,port=args.port,workers=1,ws_max_size=4_000_000)


if __name__=='__main__':main()
