<!--
  Platform wordmark: [llm]bench with a colored "load-bar" underline.
  Inlined as a component (not an <img>) so it scales crisply, inherits CSS
  sizing (set height via a `class` like "h-7 w-auto"), and can take click
  handlers. Geist Mono falls back to the system monospace stack if absent.
-->
<template>
  <svg
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 311.2 82"
    role="img"
    aria-label="llm bench"
    preserveAspectRatio="xMinYMid meet"
  >
    <text x="0" y="50" font-family="Geist Mono, ui-monospace, monospace" font-size="54.1" font-weight="500" letter-spacing="-1.35" fill="oklch(0.78 0.18 138)">[</text>
    <text x="31.1" y="50" font-family="Geist Mono, ui-monospace, monospace" font-size="54.1" font-weight="600" letter-spacing="-1.35" fill="#0e1013">llm</text>
    <text x="124.4" y="50" font-family="Geist Mono, ui-monospace, monospace" font-size="54.1" font-weight="500" letter-spacing="-1.35" fill="oklch(0.78 0.18 138)">]</text>
    <text x="155.5" y="50" font-family="Geist Mono, ui-monospace, monospace" font-size="54.1" font-weight="600" letter-spacing="-1.35" fill="#8a8d93">bench</text>
    <g transform="translate(0, 64)">
      <path d="M 0 0 H 52 V 5.4 a 3.6 3.6 0 0 1 0 7.2 V 18 H 0 V 0 Z" fill="oklch(0.78 0.18 138)"/>
    </g>
    <g transform="translate(48.4, 64)">
      <path d="M 0 0 H 78 V 5.4 a 3.6 3.6 0 0 1 0 7.2 V 18 H 0 V 12.6 a 3.6 3.6 0 0 1 0 -7.2 V 0 Z" fill="#2a2e35"/>
    </g>
    <g transform="translate(122.8, 64)">
      <path d="M 0 0 H 46 V 5.4 a 3.6 3.6 0 0 1 0 7.2 V 18 H 0 V 12.6 a 3.6 3.6 0 0 1 0 -7.2 V 0 Z" fill="oklch(0.62 0.19 28)"/>
    </g>
    <g transform="translate(165.2, 64)">
      <path d="M 0 0 H 88 V 5.4 a 3.6 3.6 0 0 1 0 7.2 V 18 H 0 V 12.6 a 3.6 3.6 0 0 1 0 -7.2 V 0 Z" fill="#2a2e35"/>
    </g>
    <g transform="translate(249.6, 64)">
      <path d="M 0 0 H 58 V 18 H 0 V 12.6 a 3.6 3.6 0 0 1 0 -7.2 V 0 Z" fill="oklch(0.70 0.16 250)"/>
    </g>
  </svg>
</template>
