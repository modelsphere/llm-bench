<template>
  <span class="group relative inline-flex align-middle">
    <span
      tabindex="0"
      aria-label="More info"
      class="flex h-4 w-4 cursor-help select-none items-center justify-center rounded-full bg-gray-200 text-[10px] font-semibold leading-none text-gray-500 hover:bg-gray-300 hover:text-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400"
    >?</span>
    <span
      role="tooltip"
      class="pointer-events-none invisible absolute bottom-full left-1/2 z-20 mb-2 w-64 -translate-x-1/2 rounded-md bg-gray-800 px-3 py-2 text-xs font-normal normal-case leading-snug text-gray-100 opacity-0 shadow-lg transition-opacity duration-100 group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
    >
      <slot />
      <span class="absolute left-1/2 top-full -translate-x-1/2 border-4 border-transparent border-t-gray-800"></span>
    </span>
  </span>
</template>

<script setup lang="ts">
// Hoverable/focusable "?" that reveals its slot content as a tooltip.
// Keeps verbose field hints out of the page flow on the submit forms.
</script>
