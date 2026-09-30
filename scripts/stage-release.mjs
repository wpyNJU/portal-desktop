import { createHash } from 'node:crypto';
import { copyFile, mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

const [input, output, version, only] = process.argv.slice(2);
const targets = only ? new Set(only.split(',')) : null;
if (!input || !output || !/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Usage: stage-release.mjs artifacts output X.Y.Z');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
async function files(root) {
  const result = [];
  for (const item of await readdir(root, { withFileTypes: true })) {
    const file = path.join(root, item.name);
    result.push(...(item.isDirectory() ? await files(file) : [file]));
  }
  return result;
}
function exactlyOne(items, label) {
  if (items.length !== 1) throw new Error(`Expected exactly one ${label}, got ${items.length}`);
  return items[0];
}
await mkdir(output, { recursive: true });
if ((await readdir(output)).length) throw new Error('Release staging directory must be empty; refusing to include stale assets.');
for (const [artifact, platform, arch, label] of [
  ['portal-desktop-macos-14', 'darwin', 'arm64', 'macos-arm64'],
  ['portal-desktop-macos-15-intel', 'darwin', 'x64', 'macos-x64'],
  ['portal-desktop-windows-latest', 'win32', 'x64', 'windows-x64'],
]) {
  if (targets && !targets.has(label)) continue;
  const contents = await files(path.join(input, artifact));
  const metadata = exactlyOne(contents.filter(f => path.basename(f) === 'runtime-bundle.json'), 'runtime manifest');
  const bundle = JSON.parse(await readFile(metadata, 'utf8'));
  if (bundle.schema !== 1 || bundle.clientVersion !== version || bundle.platform !== platform || bundle.arch !== arch) throw new Error(`Mismatched ${label} manifest`);
  const zip = exactlyOne(contents.filter(f => f.endsWith('.zip')), 'platform ZIP');
  // .NET's Windows ZIP writer can use backslashes. Match normalized names,
  // but read the original entry literally without extracting archive paths.
  const archive = JSON.parse(execFileSync('python3', ['-c', String.raw`
import hashlib, json, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    names = [(name, name.replace('\\', '/')) for name in z.namelist()]
    def one(suffix):
        found = [raw for raw, normalized in names if normalized.lower().endswith(suffix.lower())]
        if len(found) != 1: raise ValueError('Expected exactly one entry ending in ' + suffix + ', found ' + str(len(found)))
        return found[0]
    manifest = z.read(one('resources/runtime-bundle.json'))
    binary = one('resources/heart-portal' + ('.exe' if sys.argv[2] == 'win32' else ''))
    digest = hashlib.sha256()
    with z.open(binary) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    print(json.dumps({'manifest': hashlib.sha256(manifest).hexdigest(), 'binary': digest.hexdigest()}))
`, zip, platform], { encoding: 'utf8' }));
  if (archive.manifest !== sha(await readFile(metadata))) throw new Error(`${label} packaged manifest mismatch`);
  if (archive.binary !== bundle.sha256) throw new Error(`${label} engine checksum mismatch`);
  await copyFile(zip, path.join(output, `portal-desktop-${version}-${label}.zip`));
  await copyFile(metadata, path.join(output, `runtime-bundle-${label}.json`));
  if (platform === 'darwin') {
    // Signature, mount/copy installation and ZIP parity are checked on macOS
    // before this artifact is uploaded. Both formats ship in the same release.
    const dmg = exactlyOne(contents.filter(f => f.endsWith('.dmg')), 'macOS DMG');
    await copyFile(dmg, path.join(output, `portal-desktop-${version}-${label}.dmg`));
  }
  if (platform === 'win32') {
    const setup = exactlyOne(contents.filter(f => /setup\.exe$/i.test(f)), 'Windows Setup');
    await copyFile(setup, path.join(output, `portal-desktop-${version}-${label}-Setup.exe`));
  }
}
const sums = [];
for (const name of (await readdir(output)).sort()) sums.push(`${sha(await readFile(path.join(output, name)))}  ${name}`);
await writeFile(path.join(output, 'SHA256SUMS.txt'), sums.join('\n') + '\n');
console.log(`Validated and staged ${sums.length} release assets for ${version}.`);
