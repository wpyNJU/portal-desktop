export function PhoneIcon({ hangup = false }: { hangup?: boolean }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true" className={hangup ? 'phone-hangup' : undefined}>
    <path d="M6.5 3.5 9 8 7.3 9.8a15 15 0 0 0 6.9 6.9L16 15l4.5 2.5v2.2a1.7 1.7 0 0 1-1.9 1.7A19.5 19.5 0 0 1 2.6 5.4 1.7 1.7 0 0 1 4.3 3.5Z" />
  </svg>;
}
export function MicIcon({ muted = false }: { muted?: boolean }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true">
    <rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11v1a7 7 0 0 0 14 0v-1M12 19v3M8 22h8" />
    {muted && <path d="m3 3 18 18" />}
  </svg>;
}
