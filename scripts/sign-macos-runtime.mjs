import { execFileSync } from 'node:child_process';
import { copyFile, mkdtemp, readFile, rename, rm } from 'node:fs/promises';
import path from 'node:path';
import { writeRuntimeBundle } from './runtime-bundle.mjs';
import { requirePortalSource } from './portal-source.mjs';

if (process.platform !== 'darwin') throw new Error('macOS signing must run on macOS.');
const localTest = process.env.PORTAL_DESKTOP_MAC_LOCAL_TEST === '1';
if (localTest && process.env.GITHUB_REF?.startsWith('refs/tags/')) throw new Error('Local-test signing is forbidden for release tags.');
const adhocRelease = process.env.PORTAL_DESKTOP_MAC_ADHOC_RELEASE === '1';
const adhoc = localTest || adhocRelease;
const signing = JSON.parse(await readFile(new URL('../desktop/macos-signing.json', import.meta.url), 'utf8'));
const resources = path.resolve('resources');
const stage = await mkdtemp(path.join(resources, '.portal-sign-'));
try {
  // Never modify an executable in place: a local Portal may still be using it.
  const candidate = path.join(stage, 'heart-portal');
  if (adhoc) {
    await copyFile(path.join(resources, 'heart-portal'), candidate);
    execFileSync('/usr/bin/codesign', ['--force', '--sign', '-', '--identifier', signing.portalIdentifier,
      '--options', 'runtime', '--timestamp=none', candidate], { stdio: 'inherit' });
  } else {
    // Use the pinned upstream signing implementation and its release checks.
    const source = await requirePortalSource();
    const arch = process.arch === 'arm64' ? ['aarch64-apple-darwin', 'macos-arm64'] : ['x86_64-apple-darwin', 'macos-x86_64'];
    execFileSync('python3', [path.join(source, 'scripts/package-portal-macos.py'), '--binary', path.join(resources, 'heart-portal'),
      '--target', arch[0], '--output', stage,
      ...(process.env.PORTAL_DESKTOP_MAC_KEYCHAIN ? ['--keychain', process.env.PORTAL_DESKTOP_MAC_KEYCHAIN] : [])], { stdio: 'inherit', timeout: 900_000 });
    await rename(path.join(stage, `heart-portal-${arch[1]}`), candidate);
  }
  execFileSync('/usr/bin/codesign', ['--verify', '--strict', candidate], { stdio: 'inherit' });
  await rename(candidate, path.join(resources, 'heart-portal'));
  await writeRuntimeBundle();
  console.log(`Signed Portal before generating its manifest (${adhoc ? 'ad-hoc signature; no Developer ID; manual installation only' : 'upstream Developer ID policy; notarization deferred'}).`);
} finally {
  await rm(stage, { recursive: true, force: true });
}
