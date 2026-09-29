"""Build a source/deployment ZIP from an explicit allowlist, never runtime data."""
import argparse
import hashlib
import json
import subprocess
import zipfile
from fnmatch import fnmatch
from pathlib import Path,PurePosixPath

HERE=Path(__file__).resolve().parent


def build(output):
    source=HERE.resolve()
    paths=json.loads((source/'package-files.json').read_text(encoding='utf-8'))
    if not isinstance(paths,list) or len(paths)!=len(set(paths)):raise ValueError('Invalid package allowlist')
    entries={}
    for name in paths:
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:raise ValueError('Invalid package path')
        if any(part in {'data','logs','backups','__pycache__','.venv'} for part in relative.parts):raise ValueError('Runtime directory cannot be packaged')
        if any(fnmatch(relative.name,pattern) for pattern in ('doubao-api-key.txt','agent-url.txt','.env*','*.env','*.sqlite3*','*.db*','*.jsonl','assistant-profile*.json','self-test.wav')):
            raise ValueError('Private runtime file cannot be packaged')
        path=source.joinpath(*relative.parts)
        if path.is_symlink() or not path.resolve().is_relative_to(source):raise ValueError('Package path escapes source')
        entries[name]=path.read_bytes()
    try:
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,stderr=subprocess.DEVNULL,text=True).strip()
        dirty=bool(subprocess.check_output(['git','status','--porcelain','--','.'],cwd=source,text=True).strip())
    except (OSError,subprocess.CalledProcessError):revision=None;dirty=None
    manifest={'format':1,'source_revision':revision,'uncommitted_changes':dirty,
              'files':{name:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in entries.items()}}
    output=Path(output).resolve()
    if output in [source.joinpath(*PurePosixPath(name).parts).resolve() for name in paths]:raise ValueError('Output would overwrite source')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,data in entries.items():archive.writestr('being-voice-web/'+name,data)
        archive.writestr('being-voice-web/MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'archive':str(output),'files':len(entries)+1,'bytes':output.stat().st_size,
                      'sha256':hashlib.sha256(output.read_bytes()).hexdigest()},ensure_ascii=False))
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='out/being-voice-web.zip')
    build(parser.parse_args().output)
