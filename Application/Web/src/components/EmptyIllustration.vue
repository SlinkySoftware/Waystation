<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Empty-state illustrations (UI_DESIGN §3, §8): one line weight, a brand-tinted backdrop, one accent. Inline SVG
     painted only through theme variables, so they follow light/dark with no extra requests. Decorative. -->
<template>
  <svg viewBox="0 0 160 120" class="illo" aria-hidden="true" focusable="false" :data-illustration="name">
    <ellipse class="illo__bg" cx="80" cy="60" rx="62" ry="52" />
    <ellipse class="illo__ground" cx="80" cy="106" rx="42" ry="4" />

    <g v-if="name === 'repositories'">
      <rect class="illo__faint illo__dash" x="40" y="26" width="72" height="18" rx="4" />
      <rect class="illo__faint illo__dash" x="40" y="50" width="72" height="18" rx="4" />
      <rect class="illo__obj" x="40" y="74" width="72" height="18" rx="4" />
      <circle class="illo__soft" cx="51" cy="83" r="3.5" />
      <path class="illo__faint" d="M61 83H92" />
      <circle class="illo__accent" cx="114" cy="84" r="12" />
      <path class="illo__on-accent" d="M114 78V90M108 84H120" />
    </g>

    <g v-else-if="name === 'cache'">
      <path class="illo__obj" d="M52 64 80 52 72 38 44 50ZM80 52 108 64 116 50 88 38Z" />
      <path class="illo__soft illo__stroke" d="M52 64 80 52 108 64 80 76Z" />
      <path class="illo__obj" d="M52 64 80 76V104L52 92ZM80 76 108 64V92L80 104Z" />
      <path class="illo__accent-line illo__dash" d="M80 10V40" />
      <path class="illo__accent-line" d="M73 34 80 42 87 34" />
    </g>

    <g v-else-if="name === 'search'">
      <rect class="illo__obj" x="36" y="24" width="64" height="76" rx="6" />
      <path class="illo__faint" d="M46 40H88M46 52H78M46 64H84M46 76H70" />
      <circle class="illo__lens" cx="102" cy="72" r="17" />
      <path class="illo__accent-line illo__thick" d="M114.5 84.5 127 97" />
      <path class="illo__accent-line" d="M96 72H108" />
    </g>

    <g v-else-if="name === 'activity'">
      <rect class="illo__obj" x="30" y="30" width="100" height="62" rx="6" />
      <path class="illo__faint" d="M30 46H130" />
      <circle class="illo__dot" cx="40" cy="38" r="2.5" />
      <circle class="illo__dot" cx="48" cy="38" r="2.5" />
      <circle class="illo__dot" cx="56" cy="38" r="2.5" />
      <path class="illo__faint illo__dash" d="M42 70H106" />
      <circle class="illo__soft" cx="114" cy="70" r="9" />
      <circle class="illo__accent" cx="114" cy="70" r="4" />
    </g>

    <g v-else-if="name === 'error'">
      <path class="illo__line" d="M18 92C30 92 34 64 52 64" />
      <rect class="illo__obj" x="52" y="54" width="22" height="20" rx="4" />
      <path class="illo__line" d="M74 59H82M74 69H82" />
      <path class="illo__line" d="M142 92C130 92 126 64 108 64" />
      <rect class="illo__obj" x="88" y="54" width="20" height="20" rx="4" />
      <path class="illo__warn" d="M86 30 81 41H88L83 52" />
    </g>

    <g v-else-if="name === 'notFound'">
      <path class="illo__faint illo__dash" d="M24 100C40 100 42 84 58 86S78 98 90 84" />
      <path class="illo__obj illo__marker" transform="translate(66 8) scale(2)" d="M16 2 25.5 7.5V18.5L16 29 6.5 18.5V7.5Z" />
      <path class="illo__accent-line" d="M92.5 30.5A6 6 0 1 1 100.5 36C98.6 37 98 38 98 40.5" />
      <circle class="illo__accent" cx="98" cy="47" r="2.2" />
    </g>

    <g v-else>
      <path class="illo__faint illo__dash" d="M52 64 80 52 108 64 80 76ZM52 64V92L80 104 108 92V64M80 76V104" />
      <path class="illo__line" d="M80 8V30" />
      <path class="illo__accent-line" d="M80 30V38A5 5 0 1 1 75 43" />
      <path class="illo__soft" d="M118 26 120.5 32.5 127 35 120.5 37.5 118 44 115.5 37.5 109 35 115.5 32.5Z" />
    </g>
  </svg>
</template>

<script setup lang="ts">
import type { IllustrationName } from './illustrations'

defineProps<{ name: IllustrationName }>()
</script>

<style scoped>
.illo {
  --illo-line: var(--text-tertiary);
  --illo-faint: var(--border-strong);

  display: block;
  width: 100%;
  height: 100%;
  fill: none;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.illo__bg {
  fill: var(--surface-brand);
  stroke: none;
}

.illo__ground {
  fill: var(--chart-track);
  stroke: none;
}

.illo__obj,
.illo__lens {
  fill: var(--surface);
  stroke: var(--illo-line);
}

.illo__line {
  stroke: var(--illo-line);
}

.illo__faint {
  stroke: var(--illo-faint);
}

.illo__dash {
  stroke-dasharray: 4 5;
}

.illo__soft {
  fill: var(--brand-100);
  stroke: none;
}

.illo__soft.illo__stroke {
  stroke: var(--illo-line);
}

.illo__accent {
  fill: var(--brand-500);
  stroke: none;
}

.illo__on-accent {
  stroke: var(--text-on-brand);
  stroke-width: 2.5;
}

.illo__accent-line {
  stroke: var(--brand-500);
  stroke-width: 3;
}

.illo__lens {
  stroke: var(--brand-500);
  stroke-width: 4;
}

.illo__thick {
  stroke-width: 6;
}

.illo__dot {
  fill: var(--illo-faint);
  stroke: none;
}

.illo__marker {
  stroke-width: 1;
}

.illo__warn {
  stroke: var(--status-degraded);
  stroke-width: 3;
}
</style>
