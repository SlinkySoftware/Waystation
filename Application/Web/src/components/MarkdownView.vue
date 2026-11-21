<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Renders untrusted Markdown (package READMEs) safely: utils/markdown.ts turns it into VNodes with raw HTML off,
     an element allow-list, safe links only and no image loads. Never v-html. -->
<script setup lang="ts">
import { computed } from 'vue'

import { renderMarkdown } from '@/utils/markdown'

const props = withDefaults(defineProps<{ source: string; headingOffset?: number }>(), { headingOffset: 2 })
const nodes = computed(() => renderMarkdown(props.source, props.headingOffset))
const Content = () => nodes.value
</script>

<template>
  <div class="markdown"><Content /></div>
</template>

<style scoped>
.markdown {
  font-size: var(--fs-14);
  line-height: 1.6;
  color: var(--text-primary);
  overflow-wrap: anywhere;
  min-width: 0;
}

.markdown :deep(h3),
.markdown :deep(h4),
.markdown :deep(h5),
.markdown :deep(h6) {
  margin: 1.2em 0 0.5em;
  font-weight: 650;
  line-height: 1.3;
}

.markdown :deep(h3) {
  font-size: var(--fs-20);
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

.markdown :deep(h4) {
  font-size: var(--fs-16);
}

.markdown :deep(h5),
.markdown :deep(h6) {
  font-size: var(--fs-14);
}

.markdown > :deep(:first-child) {
  margin-top: 0;
}

.markdown :deep(p),
.markdown :deep(ul),
.markdown :deep(ol),
.markdown :deep(blockquote),
.markdown :deep(.md-table),
.markdown :deep(pre) {
  margin: 0 0 0.9em;
}

.markdown :deep(ul),
.markdown :deep(ol) {
  padding-left: 1.5em;
}

.markdown :deep(blockquote) {
  padding: 2px 14px;
  border-left: 3px solid var(--border-strong);
  color: var(--text-secondary);
}

.markdown :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--surface-sunken);
  font-family: var(--font-mono);
  font-size: 0.88em;
}

.markdown :deep(pre) {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  overflow-x: auto;
}

.markdown :deep(pre code) {
  padding: 0;
  background: none;
  white-space: pre;
  font-size: var(--fs-13);
}

.markdown :deep(a) {
  color: var(--link);
}

.markdown :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 1.2em 0;
}

.markdown :deep(.md-table) {
  overflow-x: auto;
}

.markdown :deep(table) {
  border-collapse: collapse;
  font-size: var(--fs-13);
}

.markdown :deep(th),
.markdown :deep(td) {
  padding: 6px 12px;
  border: 1px solid var(--border);
  text-align: left;
}

.markdown :deep(th) {
  background: var(--surface-subtle);
  font-weight: 600;
}

.markdown :deep(.md-align-right) {
  text-align: right;
}

.markdown :deep(.md-align-center) {
  text-align: center;
}
</style>
