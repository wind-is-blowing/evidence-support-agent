import type { CSSProperties } from 'react';

const paths = {
  service: 'M4 13v-1a8 8 0 0 1 16 0v1M4 12H3v6h4v-6H4Zm16 0h1v6h-4v-6h3Zm0 6v1a2 2 0 0 1-2 2h-5',
  grid: 'M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z',
  message: 'M21 11a8 8 0 0 1-8 8H7l-5 3 1.5-6A8 8 0 0 1 3 7a9 9 0 0 1 18 4ZM7 9h10M7 13h6',
  device: 'M7 3h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm2 4h6M10 17h4',
  shield: 'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4ZM8 12l3 3 5-6',
  check: 'm5 12 4 4L19 6',
  arrow: 'M4 12h16m-6-6 6 6-6 6',
  plus: 'M12 5v14M5 12h14',
  book: 'M12 5v16M3 3h5a4 4 0 0 1 4 2 4 4 0 0 1 4-2h5v16h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3V3Z',
  clock: 'M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
  user: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2',
  bolt: 'm13 2-9 12h7l-1 8 10-13h-7l0-7Z',
  tool: 'M14 6a5 5 0 0 0-6 6l-6 6 4 4 6-6a5 5 0 0 0 6-6l-4 3-3-3 3-4Z',
  alert: 'm12 3 10 18H2L12 3Zm0 6v5m0 3v1',
  image: 'M3 3h18v18H3V3Zm0 13 6-6 5 5 3-3 4 4M16 7h.01',
  lock: 'M6 10h12a2 2 0 0 1 2 2v8H4v-8a2 2 0 0 1 2-2Zm1 0V7a5 5 0 0 1 10 0v3m-5 5v2',
  minus: 'M5 12h14',
  pause: 'M8 5v14M16 5v14',
  spark: 'm12 2 2.7 7.3L22 12l-7.3 2.7L12 22l-2.7-7.3L2 12l7.3-2.7L12 2Z',
  bag: 'M5 8h14l-1 13H6L5 8Zm4 0V6a3 3 0 0 1 6 0v2',
} as const;

export function Icon({
  name,
  size = 20,
  style,
}: {
  name: keyof typeof paths;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  );
}

export function ServiceArt() {
  return (
    <svg viewBox="0 0 420 270" fill="none" aria-hidden="true">
      <circle cx="215" cy="135" r="111" stroke="currentColor" opacity=".1" />
      <circle cx="215" cy="135" r="84" stroke="currentColor" opacity=".1" />
      <path
        d="M51 137h77m173 0h70M215 20v30m0 174v25"
        stroke="currentColor"
        strokeDasharray="4 7"
        opacity=".2"
      />
      <rect
        x="144"
        y="60"
        width="140"
        height="151"
        rx="27"
        fill="currentColor"
        opacity=".07"
        transform="rotate(-9 214 135)"
      />
      <rect x="140" y="54" width="142" height="156" rx="25" fill="#fff" />
      <rect x="155" y="70" width="112" height="95" rx="13" fill="#edf5f4" />
      <path
        d="M184 122v-8a27 27 0 0 1 54 0v8m-54-4h-5v22h13v-22h-8Zm54 0h5v22h-13v-22h8Zm0 22v6h-21"
        stroke="#147d80"
        strokeWidth="5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <rect x="170" y="182" width="57" height="6" rx="3" fill="#bbc9d9" />
      <circle cx="246" cy="185" r="5" fill="#19a68e" />
      <rect x="283" y="54" width="74" height="51" rx="13" fill="#296af3" />
      <path
        d="m308 78 8 8 16-17"
        stroke="white"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <rect x="62" y="172" width="104" height="53" rx="13" fill="#e9f8f4" />
      <circle cx="84" cy="197" r="9" fill="#169d88" />
      <path d="M106 190h41m-41 12h29" stroke="#80b3a9" strokeWidth="5" strokeLinecap="round" />
      <circle cx="324" cy="185" r="9" fill="#e8bc75" />
      <circle cx="103" cy="77" r="5" fill="#69b9ae" />
    </svg>
  );
}
