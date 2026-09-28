export function allowVoicePermission(trusted: boolean, permission: string, mainFrame: boolean, url: string, shellURL: string, types: string[]) {
  return trusted && mainFrame && url === shellURL && permission === 'media' && types.length === 1 && types[0] === 'audio';
}
